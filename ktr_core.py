from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET


class KtrError(Exception):
    """Base error for KTR parsing and writing."""


class ExternalModificationError(KtrError):
    """Raised when the source file changed after it was opened."""


@dataclass(frozen=True)
class Hop:
    index: int
    from_step: str
    to_step: str
    enabled: bool


@dataclass(frozen=True)
class Branch:
    index: int
    label: str
    step_names: tuple[str, ...]
    hop_indices: tuple[int, ...]

    @property
    def display_name(self) -> str:
        return f"B{self.index + 1} {self.label}"


@dataclass(frozen=True)
class HopChange:
    hop_index: int
    branch_index: int
    from_step: str
    to_step: str
    before: bool
    after: bool


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def is_safe(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class SaveResult:
    path: Path
    backup_path: Path | None
    changed_count: int


@dataclass(frozen=True)
class _Encoding:
    codec: str
    bom: bytes


_XML_ENCODING_RE = re.compile(
    br"^\s*<\?xml[^>]*encoding\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_ORDER_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?order\b[^>]*>.*?</(?:[A-Za-z_][\w.-]*:)?order\s*>",
    re.DOTALL,
)
_HOP_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?hop\b[^>]*>.*?</(?:[A-Za-z_][\w.-]*:)?hop\s*>",
    re.DOTALL,
)
_ENABLED_RE = re.compile(
    r"(<(?:[A-Za-z_][\w.-]*:)?enabled\b[^>]*>\s*)([YN])(\s*</(?:[A-Za-z_][\w.-]*:)?enabled\s*>)",
    re.DOTALL,
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _child_text(element: ET.Element, name: str) -> str:
    child = _direct_child(element, name)
    return "" if child is None or child.text is None else child.text


def _decode_bytes(raw: bytes) -> tuple[str, _Encoding]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), _Encoding("utf-8", b"\xef\xbb\xbf")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le"), _Encoding("utf-16-le", b"\xff\xfe")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be"), _Encoding("utf-16-be", b"\xfe\xff")

    match = _XML_ENCODING_RE.search(raw[:256])
    codec = match.group(1).decode("ascii", errors="strict") if match else "utf-8"
    try:
        return raw.decode(codec), _Encoding(codec, b"")
    except (LookupError, UnicodeDecodeError) as exc:
        raise KtrError(f"无法按 XML 声明的编码 {codec!r} 读取文件：{exc}") from exc


def _encode_text(text: str, encoding: _Encoding) -> bytes:
    try:
        return encoding.bom + text.encode(encoding.codec)
    except (LookupError, UnicodeEncodeError) as exc:
        raise KtrError(f"无法按原编码 {encoding.codec!r} 写回文件：{exc}") from exc


class KtrDocument:
    """A conservative editor that changes only transformation-hop Y/N values."""

    def __init__(self, path: Path, raw: bytes):
        self.path = path.resolve()
        self._raw = raw
        self._source_hash = hashlib.sha256(raw).hexdigest()
        self._text, self._encoding = _decode_bytes(raw)
        self.name, self.hops, self._state_spans, self.step_names = self._parse(self._text)
        self.states = [hop.enabled for hop in self.hops]
        self.branches = self._build_branches()
        self.hop_to_branch = {
            hop_index: branch.index
            for branch in self.branches
            for hop_index in branch.hop_indices
        }

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "KtrDocument":
        source = Path(path)
        if source.suffix.lower() != ".ktr":
            raise KtrError("请选择 .ktr 转换文件。")
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise KtrError(f"无法读取文件：{exc}") from exc
        return cls(source, raw)

    @staticmethod
    def _parse(text: str) -> tuple[str, list[Hop], list[tuple[int, int]], list[str]]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise KtrError(f"KTR 不是有效的 XML：{exc}") from exc
        if _local_name(root.tag) != "transformation":
            raise KtrError("文件根节点不是 <transformation>，它可能不是 KTR 转换文件。")

        info = _direct_child(root, "info")
        name = _child_text(info, "name") if info is not None else ""
        step_nodes = [child for child in root if _local_name(child.tag) == "step"]
        step_names = [_child_text(step, "name") for step in step_nodes]

        order = _direct_child(root, "order")
        if order is None:
            raise KtrError("KTR 中没有 <order> 节点，无法读取 hop。")

        hop_nodes = [child for child in order if _local_name(child.tag) == "hop"]
        if not hop_nodes:
            raise KtrError("KTR 中没有可管理的 hop。")

        order_match = _ORDER_RE.search(text)
        if order_match is None:
            raise KtrError("找到了 XML hop，但无法安全定位其原始文本。已停止，未修改文件。")

        hop_matches = list(_HOP_RE.finditer(order_match.group(0)))
        if len(hop_matches) != len(hop_nodes):
            raise KtrError(
                f"XML 中有 {len(hop_nodes)} 条 hop，但只安全定位到 {len(hop_matches)} 条。已停止，未修改文件。"
            )

        hops: list[Hop] = []
        spans: list[tuple[int, int]] = []
        order_start = order_match.start()
        for index, (node, hop_match) in enumerate(zip(hop_nodes, hop_matches, strict=True)):
            from_step = _child_text(node, "from")
            to_step = _child_text(node, "to")
            enabled_value = _child_text(node, "enabled").strip().upper()
            if not from_step or not to_step:
                raise KtrError(f"第 {index + 1} 条 hop 缺少 from 或 to。")
            if enabled_value not in {"Y", "N"}:
                raise KtrError(f"第 {index + 1} 条 hop 的 enabled 值不是 Y/N：{enabled_value!r}")

            enabled_matches = list(_ENABLED_RE.finditer(hop_match.group(0)))
            if len(enabled_matches) != 1:
                raise KtrError(
                    f"第 {index + 1} 条 hop 中安全定位到 {len(enabled_matches)} 个 enabled 值。已停止，未修改文件。"
                )
            value_match = enabled_matches[0]
            start = order_start + hop_match.start() + value_match.start(2)
            end = order_start + hop_match.start() + value_match.end(2)
            if text[start:end] != enabled_value:
                raise KtrError(f"第 {index + 1} 条 hop 的 XML 解析结果与原始文本不一致。")

            hops.append(Hop(index, from_step, to_step, enabled_value == "Y"))
            spans.append((start, end))

        return name, hops, spans, step_names

    def _build_branches(self) -> list[Branch]:
        """Build weakly connected components from every hop in the KTR."""
        neighbors: dict[str, set[str]] = {}
        first_seen: dict[str, int] = {}
        for position, name in enumerate(self.step_names):
            if name and name not in first_seen:
                first_seen[name] = position
        next_rank = max(first_seen.values(), default=-1) + 1
        for hop in self.hops:
            for name in (hop.from_step, hop.to_step):
                neighbors.setdefault(name, set())
                if name not in first_seen:
                    first_seen[name] = next_rank
                    next_rank += 1
            neighbors[hop.from_step].add(hop.to_step)
            neighbors[hop.to_step].add(hop.from_step)

        components: list[tuple[int, tuple[str, ...], tuple[int, ...], str]] = []
        visited: set[str] = set()
        for start in sorted(neighbors, key=lambda item: first_seen[item]):
            if start in visited:
                continue
            stack = [start]
            visited.add(start)
            nodes: set[str] = set()
            while stack:
                node = stack.pop()
                nodes.add(node)
                for neighbor in neighbors[node]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)

            hop_indices = tuple(
                hop.index
                for hop in self.hops
                if hop.from_step in nodes and hop.to_step in nodes
            )
            if not hop_indices:
                continue
            ordered_nodes = tuple(sorted(nodes, key=lambda item: first_seen[item]))
            indegree = {node: 0 for node in nodes}
            for hop_index in hop_indices:
                indegree[self.hops[hop_index].to_step] += 1
            roots = sorted(
                (node for node, degree in indegree.items() if degree == 0),
                key=lambda item: first_seen[item],
            )
            if len(roots) == 1:
                label = roots[0]
            elif roots:
                label = f"{roots[0]} 等{len(roots)}个入口"
            else:
                label = ordered_nodes[0]
            components.append((min(hop_indices), ordered_nodes, hop_indices, label))

        components.sort(key=lambda item: item[0])
        return [
            Branch(index, label, step_names, hop_indices)
            for index, (_, step_names, hop_indices, label) in enumerate(components)
        ]

    @property
    def changed_count(self) -> int:
        return len(self.changes)

    @property
    def changes(self) -> tuple[HopChange, ...]:
        return tuple(
            HopChange(
                hop.index,
                self.hop_to_branch[hop.index],
                hop.from_step,
                hop.to_step,
                hop.enabled,
                state,
            )
            for hop, state in zip(self.hops, self.states, strict=True)
            if hop.enabled != state
        )

    def branch_state(self, branch_index: int) -> str:
        states = [self.states[index] for index in self.branches[branch_index].hop_indices]
        if all(states):
            return "enabled"
        if not any(states):
            return "disabled"
        return "mixed"

    def branch_indices_for_hops(self, hop_indices: list[int] | tuple[int, ...] | set[int]) -> list[int]:
        return sorted({self.hop_to_branch[index] for index in hop_indices})

    def set_state(self, indices: list[int] | tuple[int, ...] | set[int], enabled: bool) -> None:
        for index in indices:
            if index < 0 or index >= len(self.states):
                raise IndexError(index)
            self.states[index] = enabled

    def only_enable(self, indices: list[int] | tuple[int, ...] | set[int]) -> None:
        selected = set(indices)
        if any(index < 0 or index >= len(self.states) for index in selected):
            raise IndexError("hop index out of range")
        self.states = [index in selected for index in range(len(self.states))]

    def set_branches(self, branch_indices: list[int] | tuple[int, ...] | set[int], enabled: bool) -> None:
        selected = set(branch_indices)
        if any(index < 0 or index >= len(self.branches) for index in selected):
            raise IndexError("branch index out of range")
        hop_indices = {
            hop_index
            for branch_index in selected
            for hop_index in self.branches[branch_index].hop_indices
        }
        self.set_state(hop_indices, enabled)

    def only_enable_branches(self, branch_indices: list[int] | tuple[int, ...] | set[int]) -> None:
        selected = set(branch_indices)
        if any(index < 0 or index >= len(self.branches) for index in selected):
            raise IndexError("branch index out of range")
        enabled_hops = {
            hop_index
            for branch_index in selected
            for hop_index in self.branches[branch_index].hop_indices
        }
        self.only_enable(enabled_hops)

    def toggle(self, index: int) -> None:
        self.states[index] = not self.states[index]

    def reset(self) -> None:
        self.states = [hop.enabled for hop in self.hops]

    def _cyclic_components(self) -> list[tuple[str, ...]]:
        """Return strongly connected components that form enabled cycles."""
        adjacency: dict[str, list[str]] = {}
        for hop, state in zip(self.hops, self.states, strict=True):
            if state:
                adjacency.setdefault(hop.from_step, []).append(hop.to_step)
                adjacency.setdefault(hop.to_step, [])

        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        components: list[tuple[str, ...]] = []

        def visit(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)

            for target in adjacency[node]:
                if target not in indices:
                    visit(target)
                    lowlinks[node] = min(lowlinks[node], lowlinks[target])
                elif target in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[target])

            if lowlinks[node] != indices[node]:
                return
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            has_self_loop = len(component) == 1 and component[0] in adjacency.get(component[0], [])
            if len(component) > 1 or has_self_loop:
                components.append(tuple(sorted(component)))

        for node in tuple(adjacency):
            if node not in indices:
                visit(node)
        return components

    def validate_plan(self) -> ValidationReport:
        issues: list[ValidationIssue] = []
        step_counts = Counter(self.step_names)
        empty_steps = step_counts.pop("", 0)
        if empty_steps:
            issues.append(ValidationIssue("error", "empty-step-name", f"发现 {empty_steps} 个没有名称的步骤。"))
        duplicates = sorted(name for name, count in step_counts.items() if count > 1)
        if duplicates:
            preview = "、".join(duplicates[:5])
            suffix = "…" if len(duplicates) > 5 else ""
            issues.append(ValidationIssue("error", "duplicate-step-name", f"发现重复步骤名：{preview}{suffix}"))

        known_steps = set(step_counts)
        missing_messages: list[str] = []
        for hop in self.hops:
            missing: list[str] = []
            if hop.from_step not in known_steps:
                missing.append(f"来源步骤“{hop.from_step}”")
            if hop.to_step not in known_steps:
                missing.append(f"目标步骤“{hop.to_step}”")
            if missing:
                missing_messages.append(f"Hop #{hop.index + 1} 缺少 {' 和 '.join(missing)}")
        for message in missing_messages[:10]:
            issues.append(ValidationIssue("error", "dangling-hop", message))
        if len(missing_messages) > 10:
            issues.append(
                ValidationIssue("error", "dangling-hop-more", f"另有 {len(missing_messages) - 10} 条悬空 hop 未展开。")
            )

        pair_counts = Counter((hop.from_step, hop.to_step) for hop in self.hops)
        duplicate_pairs = sorted(pair for pair, count in pair_counts.items() if count > 1)
        for from_step, to_step in duplicate_pairs[:10]:
            issues.append(ValidationIssue("error", "duplicate-hop", f"发现重复 hop：“{from_step}” → “{to_step}”。"))
        if len(duplicate_pairs) > 10:
            issues.append(
                ValidationIssue("error", "duplicate-hop-more", f"另有 {len(duplicate_pairs) - 10} 组重复 hop 未展开。")
            )

        for component in self._cyclic_components():
            names = " → ".join(component[:6])
            suffix = "…" if len(component) > 6 else ""
            issues.append(
                ValidationIssue("error", "enabled-cycle", f"计划启用后的数据流形成循环，涉及：{names}{suffix}")
            )

        for branch in self.branches:
            if len(branch.hop_indices) > 1 and self.branch_state(branch.index) == "mixed":
                enabled_count = sum(self.states[index] for index in branch.hop_indices)
                issues.append(
                    ValidationIssue(
                        "warning",
                        "partial-branch",
                        f"{branch.display_name} 部分启用：{enabled_count}/{len(branch.hop_indices)} 条 hop。",
                    )
                )

        return ValidationReport(tuple(issues))

    def render(self) -> bytes:
        report = self.validate_plan()
        if report.errors:
            detail = "\n".join(f"• {issue.message}" for issue in report.errors[:12])
            raise KtrError(f"安全校验失败，已停止保存：\n{detail}")

        rendered = self._text
        for (start, end), enabled in reversed(list(zip(self._state_spans, self.states, strict=True))):
            rendered = rendered[:start] + ("Y" if enabled else "N") + rendered[end:]

        diffs = [i for i, (before, after) in enumerate(zip(self._text, rendered, strict=True)) if before != after]
        expected = {
            start
            for (start, _), original, current in zip(self._state_spans, self.hops, self.states, strict=True)
            if original.enabled != current
        }
        if len(rendered) != len(self._text) or set(diffs) != expected:
            raise KtrError("内部安全校验失败：检测到 enabled 以外的文本变化。")

        parsed_name, parsed_hops, _, parsed_steps = self._parse(rendered)
        if parsed_name != self.name:
            raise KtrError("保存前校验失败：转换名称发生变化。")
        if parsed_steps != self.step_names:
            raise KtrError("保存前校验失败：步骤结构发生变化。")
        if [(h.from_step, h.to_step) for h in parsed_hops] != [
            (h.from_step, h.to_step) for h in self.hops
        ]:
            raise KtrError("保存前校验失败：hop 的 from/to 结构发生变化。")
        if [h.enabled for h in parsed_hops] != self.states:
            raise KtrError("保存前校验失败：hop 状态与计划值不一致。")

        encoded = _encode_text(rendered, self._encoding)
        if len(encoded) != len(self._raw):
            raise KtrError("保存前校验失败：文件字节长度意外变化。")
        return encoded

    def source_is_unchanged(self) -> bool:
        try:
            current = self.path.read_bytes()
        except OSError:
            return False
        return hashlib.sha256(current).hexdigest() == self._source_hash

    def save(self, target: str | os.PathLike[str], backup_if_overwrite: bool = True) -> SaveResult:
        target_path = Path(target).resolve()
        same_file = os.path.normcase(str(target_path)) == os.path.normcase(str(self.path))
        if same_file and not self.source_is_unchanged():
            raise ExternalModificationError(
                "源 KTR 在打开后已被其他程序修改。请重新打开文件，避免覆盖 Spoon 或其他工具的新更改。"
            )

        output = self.render()
        changed_count = self.changed_count
        backup_path: Path | None = None
        if same_file and backup_if_overwrite:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = self.path.with_name(f"{self.path.name}.bak-{stamp}")
            counter = 1
            while backup_path.exists():
                backup_path = self.path.with_name(f"{self.path.name}.bak-{stamp}-{counter}")
                counter += 1
            try:
                shutil.copy2(self.path, backup_path)
            except OSError as exc:
                raise KtrError(f"无法创建备份，已停止保存：{exc}") from exc

        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target_path)
            temp_name = None
        except OSError as exc:
            raise KtrError(f"保存失败：{exc}") from exc
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

        return SaveResult(target_path, backup_path, changed_count)
