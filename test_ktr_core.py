from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ktr_core import ExternalModificationError, KtrDocument, KtrError


SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<transformation>
  <info>
    <name>测试转换</name>
    <trans-log-table><field><enabled>Y</enabled></field></trans-log-table>
  </info>
  <order>
    <hop>
      <from>来源A</from>
      <to>目标A</to>
      <enabled>Y</enabled>
    </hop>
    <hop>
      <from>来源B</from>
      <to>目标B</to>
      <enabled>N</enabled>
    </hop>
  </order>
  <step><name>来源A</name><type>TableInput</type></step>
  <step><name>目标A</name><type>InsertUpdate</type></step>
  <step><name>来源B</name><type>TableInput</type></step>
  <step><name>目标B</name><type>InsertUpdate</type></step>
</transformation>
"""


def make_ktr(steps: list[str], hops: list[tuple[str, str, bool]]) -> str:
    hop_xml = "\n".join(
        f"    <hop><from>{source}</from><to>{target}</to><enabled>{'Y' if enabled else 'N'}</enabled></hop>"
        for source, target, enabled in hops
    )
    step_xml = "\n".join(f"  <step><name>{name}</name><type>Dummy</type></step>" for name in steps)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<transformation>\n"
        "  <info><name>图测试</name></info>\n"
        f"  <order>\n{hop_xml}\n  </order>\n"
        f"{step_xml}\n"
        "</transformation>\n"
    )


class KtrDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sample.ktr"
        self.path.write_bytes(SAMPLE.encode("utf-8"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_graph(self, steps: list[str], hops: list[tuple[str, str, bool]]) -> KtrDocument:
        self.path.write_text(make_ktr(steps, hops), encoding="utf-8")
        return KtrDocument.open(self.path)

    def test_reads_hops_and_only_changes_hop_enabled(self) -> None:
        doc = KtrDocument.open(self.path)
        self.assertEqual(doc.name, "测试转换")
        self.assertEqual([hop.enabled for hop in doc.hops], [True, False])
        self.assertEqual(len(doc.branches), 2)
        doc.only_enable([1])
        output = doc.render().decode("utf-8")
        self.assertIn("<trans-log-table><field><enabled>Y</enabled>", output)
        self.assertIn("<from>来源A</from>\n      <to>目标A</to>\n      <enabled>N</enabled>", output)
        self.assertIn("<from>来源B</from>\n      <to>目标B</to>\n      <enabled>Y</enabled>", output)
        self.assertEqual(len(output), len(SAMPLE))

    def test_detects_and_controls_complete_branches(self) -> None:
        doc = self.write_graph(
            ["A", "B", "C", "D", "E"],
            [("A", "B", True), ("B", "C", False), ("D", "E", True)],
        )
        self.assertEqual(len(doc.branches), 2)
        self.assertEqual(doc.branches[0].hop_indices, (0, 1))
        self.assertEqual(doc.branches[1].hop_indices, (2,))
        self.assertEqual(doc.branch_state(0), "mixed")
        self.assertTrue(any(issue.code == "partial-branch" for issue in doc.validate_plan().warnings))

        doc.set_branches([0], True)
        self.assertEqual(doc.states, [True, True, True])
        doc.only_enable_branches([1])
        self.assertEqual(doc.states, [False, False, True])

    def test_enabled_cycle_blocks_render(self) -> None:
        doc = self.write_graph(
            ["A", "B", "C"],
            [("A", "B", True), ("B", "C", True), ("C", "A", False)],
        )
        self.assertFalse(any(issue.code == "enabled-cycle" for issue in doc.validate_plan().errors))
        doc.set_state([2], True)
        self.assertTrue(any(issue.code == "enabled-cycle" for issue in doc.validate_plan().errors))
        with self.assertRaises(KtrError):
            doc.render()

    def test_dangling_hop_and_duplicate_step_are_errors(self) -> None:
        doc = self.write_graph(["A", "A"], [("A", "Missing", False)])
        error_codes = {issue.code for issue in doc.validate_plan().errors}
        self.assertIn("duplicate-step-name", error_codes)
        self.assertIn("dangling-hop", error_codes)
        with self.assertRaises(KtrError):
            doc.render()

    def test_change_list_contains_before_after_and_branch(self) -> None:
        doc = KtrDocument.open(self.path)
        doc.only_enable([1])
        self.assertEqual(len(doc.changes), 2)
        self.assertEqual((doc.changes[0].before, doc.changes[0].after), (True, False))
        self.assertEqual((doc.changes[1].before, doc.changes[1].after), (False, True))
        self.assertEqual(doc.changes[0].branch_index, 0)
        self.assertEqual(doc.changes[1].branch_index, 1)

    def test_overwrite_creates_backup(self) -> None:
        doc = KtrDocument.open(self.path)
        doc.set_state([0], False)
        result = doc.save(self.path)
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.read_text(encoding="utf-8"), SAMPLE)
        self.assertFalse(KtrDocument.open(self.path).hops[0].enabled)

    def test_external_change_blocks_overwrite(self) -> None:
        doc = KtrDocument.open(self.path)
        self.path.write_text(SAMPLE.replace("测试转换", "外部修改"), encoding="utf-8")
        with self.assertRaises(ExternalModificationError):
            doc.save(self.path)

    def test_save_copy_preserves_source(self) -> None:
        doc = KtrDocument.open(self.path)
        doc.set_state([1], True)
        copy = self.path.with_name("copy.ktr")
        result = doc.save(copy)
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE)
        self.assertTrue(KtrDocument.open(copy).hops[1].enabled)


if __name__ == "__main__":
    unittest.main()
