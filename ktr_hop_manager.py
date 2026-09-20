from __future__ import annotations

import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ktr_core import ExternalModificationError, KtrDocument, KtrError, ValidationReport


APP_NAME = "Ktr启停管理工具"
APP_VERSION = "0.3.0"
WINDOWS_APP_ID = "LocalTools.KtrToggleManager.0.3"


def resource_path(relative_path: str) -> Path:
    """Resolve bundled assets in both source and PyInstaller one-file modes."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def configure_windows_app_identity() -> None:
    """Give Windows a stable identity so the taskbar uses the custom icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):
        pass


def _issue_text(report: ValidationReport, limit: int = 14) -> str:
    lines = [
        f"{'错误' if issue.severity == 'error' else '提示'}：{issue.message}"
        for issue in report.issues[:limit]
    ]
    if len(report.issues) > limit:
        lines.append(f"另有 {len(report.issues) - limit} 项未展开。")
    return "\n".join(lines)


class ChangePreviewDialog:
    """Modal, read-only preview of every hop state change before saving."""

    def __init__(self, parent: tk.Tk, doc: KtrDocument, report: ValidationReport) -> None:
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.title("保存前变更预览")
        self.window.geometry("1120x580")
        self.window.minsize(820, 440)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        icon_path = resource_path("assets/app_icon.ico")
        if icon_path.exists():
            try:
                self.window.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

        changes = doc.changes
        enabled_count = sum(change.after for change in changes)
        disabled_count = len(changes) - enabled_count
        header = ttk.Frame(self.window, padding=(16, 14, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text="保存前变更预览", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        summary = (
            f"共修改 {len(changes)} 条 hop：{enabled_count} 条将变为启用，{disabled_count} 条将变为禁用。"
            if changes
            else "当前没有 hop 状态变化；继续后将创建内容相同的副本。"
        )
        ttk.Label(header, text=summary, foreground="#334155").pack(anchor="w", pady=(6, 0))

        if report.warnings:
            warning_text = "；".join(issue.message for issue in report.warnings[:3])
            if len(report.warnings) > 3:
                warning_text += f"；另有 {len(report.warnings) - 3} 项提示"
            warning = ttk.Label(
                self.window,
                text=f"结构提示：{warning_text}",
                foreground="#9a6700",
                wraplength=1060,
                padding=(16, 4, 16, 8),
            )
            warning.pack(fill="x")

        table_frame = ttk.Frame(self.window, padding=(16, 4, 16, 8))
        table_frame.pack(fill="both", expand=True)
        columns = ("number", "branch", "from", "to", "before", "after")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        tree.heading("number", text="#")
        tree.heading("branch", text="完整分支")
        tree.heading("from", text="来源步骤")
        tree.heading("to", text="目标步骤")
        tree.heading("before", text="原状态")
        tree.heading("after", text="保存后")
        tree.column("number", width=52, anchor="center", stretch=False)
        tree.column("branch", width=190, minwidth=130)
        tree.column("from", width=330, minwidth=200)
        tree.column("to", width=300, minwidth=200)
        tree.column("before", width=80, anchor="center", stretch=False)
        tree.column("after", width=80, anchor="center", stretch=False)
        tree.tag_configure("enable", foreground="#137333")
        tree.tag_configure("disable", foreground="#9b1c1c")
        for change in changes:
            branch = doc.branches[change.branch_index]
            tree.insert(
                "",
                "end",
                values=(
                    change.hop_index + 1,
                    branch.display_name,
                    change.from_step,
                    change.to_step,
                    "启用" if change.before else "禁用",
                    "启用" if change.after else "禁用",
                ),
                tags=("enable" if change.after else "disable",),
            )
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        buttons = ttk.Frame(self.window, padding=(16, 6, 16, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="确认并继续保存", style="Accent.TButton", command=self.confirm).pack(side="right")
        ttk.Label(buttons, text="结构安全检查已通过", foreground="#137333").pack(side="left")

        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.bind("<Return>", lambda _event: self.confirm())
        self.window.grab_set()

    def confirm(self) -> None:
        self.result = True
        self.window.destroy()

    def cancel(self) -> None:
        self.result = False
        self.window.destroy()

    def show(self) -> bool:
        self.window.wait_window()
        return self.result


class KtrHopManager(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        icon_path = resource_path("assets/app_icon.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.geometry("1280x860")
        self.minsize(980, 700)
        self.doc: KtrDocument | None = None
        self.visible_indices: list[int] = []

        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="全部")
        self.file_var = tk.StringVar(value="尚未打开 KTR 文件")
        self.summary_var = tk.StringVar(value="请选择一个 .ktr 转换文件")
        self.detail_var = tk.StringVar(
            value="本工具只修改 <order><hop><enabled>Y/N</enabled>，不会连接数据库，也不会执行转换。"
        )

        self._configure_style()
        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self.refresh_tree())
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if len(sys.argv) > 1:
            candidate = Path(sys.argv[1])
            if candidate.is_file():
                self.after(100, lambda: self.load_file(candidate))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 6))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=(12, 7))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="打开 KTR", style="Accent.TButton", command=self.open_dialog).pack(side="right")

        file_bar = ttk.Frame(self, padding=(16, 0, 16, 8))
        file_bar.pack(fill="x")
        ttk.Label(file_bar, textvariable=self.file_var, foreground="#334155").pack(side="left", fill="x", expand=True)

        controls = ttk.Frame(self, padding=(16, 4, 16, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="搜索：").pack(side="left")
        ttk.Entry(controls, textvariable=self.search_var, width=34).pack(side="left", padx=(0, 12))
        ttk.Label(controls, text="筛选：").pack(side="left")
        state_filter = ttk.Combobox(
            controls,
            textvariable=self.filter_var,
            values=("全部", "当前启用", "当前禁用", "已修改", "分支部分启用"),
            state="readonly",
            width=13,
        )
        state_filter.pack(side="left")
        state_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh_tree())
        ttk.Button(controls, text="选择当前列表", command=self.select_visible).pack(side="right")

        table_frame = ttk.Frame(self, padding=(16, 0, 16, 8))
        table_frame.pack(fill="both", expand=True)
        columns = ("number", "branch", "state", "changed", "from", "to")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("number", text="#")
        self.tree.heading("branch", text="完整分支")
        self.tree.heading("state", text="当前状态")
        self.tree.heading("changed", text="更改")
        self.tree.heading("from", text="来源步骤")
        self.tree.heading("to", text="目标步骤")
        self.tree.column("number", width=50, minwidth=44, anchor="center", stretch=False)
        self.tree.column("branch", width=210, minwidth=140)
        self.tree.column("state", width=92, minwidth=84, anchor="center", stretch=False)
        self.tree.column("changed", width=60, minwidth=54, anchor="center", stretch=False)
        self.tree.column("from", width=390, minwidth=220)
        self.tree.column("to", width=350, minwidth=210)
        self.tree.tag_configure("enabled", foreground="#137333")
        self.tree.tag_configure("disabled", foreground="#64748b")
        self.tree.tag_configure("changed", background="#fff7d6")
        self.tree.tag_configure("partial", background="#fff0df")

        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_double_click)

        actions = ttk.Frame(self, padding=(16, 2, 16, 6))
        actions.pack(fill="x")
        hop_actions = ttk.LabelFrame(actions, text="单条 Hop 操作", padding=(8, 6))
        hop_actions.pack(fill="x", pady=(0, 5))
        ttk.Button(hop_actions, text="启用选中", command=lambda: self.set_selected(True)).pack(side="left", padx=(0, 5))
        ttk.Button(hop_actions, text="停用选中", command=lambda: self.set_selected(False)).pack(side="left", padx=5)
        ttk.Button(hop_actions, text="仅启用选中", command=self.only_enable_selected).pack(side="left", padx=5)
        ttk.Button(hop_actions, text="全部启用", command=lambda: self.set_all(True)).pack(side="left", padx=(20, 5))
        ttk.Button(hop_actions, text="全部停用", command=lambda: self.set_all(False)).pack(side="left", padx=5)

        branch_actions = ttk.LabelFrame(actions, text="完整分支操作（自动识别连通分支）", padding=(8, 6))
        branch_actions.pack(fill="x", pady=(0, 5))
        ttk.Button(branch_actions, text="启用所选分支", command=lambda: self.set_selected_branches(True)).pack(
            side="left", padx=(0, 5)
        )
        ttk.Button(branch_actions, text="停用所选分支", command=lambda: self.set_selected_branches(False)).pack(
            side="left", padx=5
        )
        ttk.Button(
            branch_actions,
            text="仅启用所选分支",
            style="Accent.TButton",
            command=self.only_enable_selected_branches,
        ).pack(side="left", padx=5)
        ttk.Label(branch_actions, text="选中分支内任意 hop 即可", foreground="#64748b").pack(side="left", padx=14)

        save_actions = ttk.Frame(actions)
        save_actions.pack(fill="x")
        ttk.Button(save_actions, text="撤销未保存更改", command=self.reset_changes).pack(side="left")
        ttk.Button(save_actions, text="保存副本…", style="Accent.TButton", command=self.save_copy).pack(side="right")
        ttk.Button(save_actions, text="备份并覆盖原文件", command=self.overwrite_source).pack(side="right", padx=6)
        ttk.Button(save_actions, text="安全校验", command=self.validate_plan).pack(side="right", padx=6)

        footer = ttk.Frame(self, padding=(16, 4, 16, 14))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.summary_var, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(footer, textvariable=self.detail_var, foreground="#64748b").pack(anchor="w", pady=(4, 0))

    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 KTR 转换文件",
            filetypes=(("Kettle 转换", "*.ktr"), ("所有文件", "*.*")),
        )
        if path:
            self.load_file(Path(path))

    def confirm_discard(self) -> bool:
        if self.doc and self.doc.changed_count:
            return messagebox.askyesno("放弃未保存更改？", f"当前有 {self.doc.changed_count} 条状态更改尚未保存，是否放弃？")
        return True

    def load_file(self, path: Path, ask_discard: bool = True) -> None:
        if ask_discard and not self.confirm_discard():
            return
        try:
            doc = KtrDocument.open(path)
        except KtrError as exc:
            messagebox.showerror("无法打开 KTR", str(exc))
            return
        self.doc = doc
        self.search_var.set("")
        self.filter_var.set("全部")
        self.file_var.set(f"{doc.path}    |    转换：{doc.name or '(未命名)'}")
        self.refresh_tree()
        report = doc.validate_plan()
        if report.errors:
            messagebox.showwarning(
                "发现结构安全问题",
                "文件已以只读分析方式打开，但保存会被阻止，直至问题被修复：\n\n" + _issue_text(report),
            )

    def refresh_tree(self) -> None:
        selected = {int(item) for item in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        self.visible_indices.clear()
        if not self.doc:
            return

        needle = self.search_var.get().strip().casefold()
        state_filter = self.filter_var.get()
        for hop, state in zip(self.doc.hops, self.doc.states, strict=True):
            branch = self.doc.branches[self.doc.hop_to_branch[hop.index]]
            branch_state = self.doc.branch_state(branch.index)
            changed = state != hop.enabled
            searchable = f"{branch.display_name}\n{hop.from_step}\n{hop.to_step}".casefold()
            if needle and needle not in searchable:
                continue
            if state_filter == "当前启用" and not state:
                continue
            if state_filter == "当前禁用" and state:
                continue
            if state_filter == "已修改" and not changed:
                continue
            if state_filter == "分支部分启用" and branch_state != "mixed":
                continue
            self.visible_indices.append(hop.index)
            tags: list[str] = []
            if changed:
                tags.append("changed")
            elif branch_state == "mixed":
                tags.append("partial")
            else:
                tags.append("enabled" if state else "disabled")
            self.tree.insert(
                "",
                "end",
                iid=str(hop.index),
                values=(
                    hop.index + 1,
                    branch.display_name,
                    "✓ 启用" if state else "— 禁用",
                    "是" if changed else "",
                    hop.from_step,
                    hop.to_step,
                ),
                tags=tuple(tags),
            )
        for index in selected:
            if self.tree.exists(str(index)):
                self.tree.selection_add(str(index))
        self.update_summary()

    def update_summary(self) -> None:
        if not self.doc:
            self.summary_var.set("请选择一个 .ktr 转换文件")
            return
        enabled = sum(self.doc.states)
        total = len(self.doc.states)
        branch_states = [self.doc.branch_state(branch.index) for branch in self.doc.branches]
        mixed = branch_states.count("mixed")
        report = self.doc.validate_plan()
        self.summary_var.set(
            f"{total} 条 hop：启用 {enabled}，禁用 {total - enabled}，待保存 {self.doc.changed_count}；"
            f"完整分支 {len(self.doc.branches)}（部分启用 {mixed}）；当前显示 {len(self.visible_indices)} 条"
        )
        if report.errors:
            self.detail_var.set(f"安全校验：{len(report.errors)} 项错误，当前状态禁止保存。请点击“安全校验”查看。")
        elif report.warnings:
            self.detail_var.set(f"安全校验通过，但有 {len(report.warnings)} 项结构提示；保存预览时会再次显示。")
        else:
            self.detail_var.set("安全校验通过：未发现循环、悬空 hop、缺失步骤、重复步骤名或重复 hop。")

    def selected_indices(self) -> list[int]:
        return [int(item) for item in self.tree.selection()]

    def selected_branch_indices(self) -> list[int]:
        if not self.doc:
            return []
        return self.doc.branch_indices_for_hops(self.selected_indices())

    def select_visible(self) -> None:
        self.tree.selection_set([str(index) for index in self.visible_indices])

    def _apply_state_change(self, change) -> bool:
        assert self.doc is not None
        previous = self.doc.states.copy()
        previous_cycles = {
            issue.message for issue in self.doc.validate_plan().errors if issue.code == "enabled-cycle"
        }
        change()
        report = self.doc.validate_plan()
        new_cycles = [
            issue.message
            for issue in report.errors
            if issue.code == "enabled-cycle" and issue.message not in previous_cycles
        ]
        if new_cycles:
            self.doc.states = previous
            messagebox.showerror(
                "已阻止形成循环",
                "本次操作会让启用的数据流形成循环，因此已自动撤销：\n\n" + "\n".join(new_cycles),
            )
            self.refresh_tree()
            return False
        self.refresh_tree()
        return True

    def set_selected(self, enabled: bool) -> None:
        if not self.doc:
            return
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("未选择", "请先选择一条或多条 hop。")
            return
        self._apply_state_change(lambda: self.doc.set_state(indices, enabled))

    def only_enable_selected(self) -> None:
        if not self.doc:
            return
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("未选择", "请先选择要保留启用的一条或多条 hop。")
            return
        if not messagebox.askyesno(
            "仅启用选中项",
            f"将启用选中的 {len(indices)} 条 hop，并停用其余 {len(self.doc.hops) - len(indices)} 条。继续吗？",
        ):
            return
        if self._apply_state_change(lambda: self.doc.only_enable(indices)):
            self.filter_var.set("全部")
            self.refresh_tree()

    def set_all(self, enabled: bool) -> None:
        if not self.doc:
            return
        action = "启用" if enabled else "停用"
        if not messagebox.askyesno(f"全部{action}", f"确定要{action}全部 {len(self.doc.hops)} 条 hop 吗？"):
            return
        if self._apply_state_change(lambda: self.doc.set_state(list(range(len(self.doc.hops))), enabled)):
            self.filter_var.set("全部")
            self.refresh_tree()

    def set_selected_branches(self, enabled: bool) -> None:
        if not self.doc:
            return
        branch_indices = self.selected_branch_indices()
        if not branch_indices:
            messagebox.showinfo("未选择", "请先选择一个或多个分支中的任意 hop。")
            return
        hop_count = sum(len(self.doc.branches[index].hop_indices) for index in branch_indices)
        action = "启用" if enabled else "停用"
        if not messagebox.askyesno(
            f"{action}完整分支",
            f"将{action} {len(branch_indices)} 个完整分支，共 {hop_count} 条 hop。继续吗？",
        ):
            return
        self._apply_state_change(lambda: self.doc.set_branches(branch_indices, enabled))

    def only_enable_selected_branches(self) -> None:
        if not self.doc:
            return
        branch_indices = self.selected_branch_indices()
        if not branch_indices:
            messagebox.showinfo("未选择", "请先选择要保留启用的分支中的任意 hop。")
            return
        enabled_hops = sum(len(self.doc.branches[index].hop_indices) for index in branch_indices)
        if not messagebox.askyesno(
            "仅启用所选完整分支",
            f"将完整启用 {len(branch_indices)} 个分支（{enabled_hops} 条 hop），并停用其他全部分支。继续吗？",
        ):
            return
        if self._apply_state_change(lambda: self.doc.only_enable_branches(branch_indices)):
            self.filter_var.set("全部")
            self.refresh_tree()

    def on_double_click(self, event: tk.Event) -> None:
        if not self.doc:
            return
        row = self.tree.identify_row(event.y)
        if row:
            if self._apply_state_change(lambda: self.doc.toggle(int(row))) and self.tree.exists(row):
                self.tree.selection_set(row)

    def reset_changes(self) -> None:
        if not self.doc or not self.doc.changed_count:
            return
        if messagebox.askyesno("撤销更改", f"撤销全部 {self.doc.changed_count} 条未保存更改？"):
            self.doc.reset()
            self.filter_var.set("全部")
            self.refresh_tree()

    def validate_plan(self) -> None:
        if not self.doc:
            return
        report = self.doc.validate_plan()
        if report.errors:
            messagebox.showerror("安全校验失败", _issue_text(report))
            return
        try:
            self.doc.render()
        except KtrError as exc:
            messagebox.showerror("安全校验失败", str(exc))
            return
        if report.warnings:
            messagebox.showwarning(
                "安全校验通过（有提示）",
                f"未发现阻止保存的问题；检测到 {len(report.warnings)} 项提示：\n\n{_issue_text(report)}",
            )
        else:
            messagebox.showinfo(
                "安全校验通过",
                f"XML 可解析；{len(self.doc.step_names)} 个步骤、{len(self.doc.hops)} 条 hop、"
                f"{len(self.doc.branches)} 个完整分支结构有效；计划修改 {self.doc.changed_count} 个 Y/N 状态值。",
            )

    def _confirm_change_preview(self) -> bool:
        assert self.doc is not None
        report = self.doc.validate_plan()
        if report.errors:
            messagebox.showerror("禁止保存：安全校验失败", _issue_text(report))
            return False
        try:
            self.doc.render()
        except KtrError as exc:
            messagebox.showerror("禁止保存：安全校验失败", str(exc))
            return False
        return ChangePreviewDialog(self, self.doc, report).show()

    def save_copy(self) -> None:
        if not self.doc or not self._confirm_change_preview():
            return
        source = self.doc.path
        suggested = f"{source.stem}_已调整{source.suffix}"
        target = filedialog.asksaveasfilename(
            title="保存 KTR 副本",
            initialdir=source.parent,
            initialfile=suggested,
            defaultextension=".ktr",
            filetypes=(("Kettle 转换", "*.ktr"),),
        )
        if not target:
            return
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(str(source)):
            messagebox.showwarning("请选择其他文件名", "“保存副本”不能覆盖当前原文件；如需覆盖，请使用“备份并覆盖原文件”。")
            return
        self._save(Path(target), overwrite=False)

    def overwrite_source(self) -> None:
        if not self.doc:
            return
        if not self.doc.changed_count:
            messagebox.showinfo("没有更改", "当前没有需要保存的状态更改。")
            return
        if not self._confirm_change_preview():
            return
        if not messagebox.askyesno(
            "备份并覆盖原文件",
            "保存前会在原目录创建带时间戳的 .bak 备份。\n\n请确认 Spoon 没有同时保存这个文件。继续吗？",
        ):
            return
        self._save(self.doc.path, overwrite=True)

    def _save(self, target: Path, overwrite: bool) -> None:
        assert self.doc is not None
        try:
            result = self.doc.save(target, backup_if_overwrite=overwrite)
        except ExternalModificationError as exc:
            messagebox.showerror("检测到外部修改", str(exc))
            return
        except KtrError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        backup = f"\n备份：{result.backup_path}" if result.backup_path else ""
        messagebox.showinfo("保存成功", f"已保存：{result.path}\n修改 hop 状态：{result.changed_count} 条{backup}")
        self.load_file(result.path, ask_discard=False)

    def on_close(self) -> None:
        if self.confirm_discard():
            self.destroy()


if __name__ == "__main__":
    configure_windows_app_identity()
    KtrHopManager().mainloop()
