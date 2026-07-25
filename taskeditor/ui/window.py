"""
Task Editor 主窗口（PySide6）。

三大区（对应需求）：
  1. 上部：选中任务详情，左=原信息、右=新信息。新信息里 任务名/持续小时/类别
     可编辑，光标离开文本框（editingFinished）即保存；含“自动调整起止时间”按钮。
  2. 中部：上下两条 Premiere 风格时间轴，共享缩放/平移；上=编辑前，下=编辑中。
  3. 下部：JetBrains 风格 Problems 面板（可随窗口高度变化、文本换行、可滚动）。
  底部：应用 / 取消。应用只把改动写回 committed 数据（commit_preview.txt）。

Stage 1 覆盖上述骨架 + 顶层点击选中。深度选中 / 拖动 / 边缘 trim / 右键改时长 /
完整自动调整算法在 Stage 2、3 落地。
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..analysis import Problem, Severity, analyze
from ..autoadjust import AutoAdjustStrategy, get_default_strategy
from ..store import CommitData, TaskItem, save_commit_data
from .colors import build_color_map
from .timeline import WIDGET_HEIGHT, TimelineWidget, compute_state

# 顶信息区固定/最小高度（远大于“显示全部信息”所需，不会裁剪任何字段）。
TOP_ZONE_HEIGHT = 460
MIDDLE_HEIGHT = 2 * WIDGET_HEIGHT + 2 * 18 + 16 + 12  # 两条轴 + 两个标题 + 滚动条 + 边距
# 上部块被 Problems 手柄向上挤压时的下限 = 顶信息“显示全部信息所需高度” + 分割线 + 少量时间轴余量。
# 顶信息区自身永不被压缩/裁剪；被挤压时只裁剪下方时间轴。
FLOOR_TIMELINE_SLIVER = 8

DARK_QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }
QDialog { background: #2b2d30; color: #dfe1e5; }
QLabel { color: #dfe1e5; }
QLabel[role="caption"] { color: #9aa0a6; }
QLabel[role="header"] { color: #ffffff; font-weight: bold; }
QLabel[role="note"] { color: #7f8489; }
QLabel[role="title"] { color: #ffffff; font-weight: bold; font-size: 13px; }
QLineEdit {
    background: #1e1f22; color: #dfe1e5; border: 1px solid #4a4d50;
    border-radius: 3px; padding: 3px 6px;
}
QLineEdit:read-only { background: #2b2d30; border: 1px solid #3a3d40; color: #b7bbc0; }
QPushButton {
    background: #3c3f41; color: #dfe1e5; border: 1px solid #4a4d50;
    border-radius: 4px; padding: 6px 14px;
}
QPushButton:hover { background: #45494c; }
QPushButton#apply { background: #365880; border-color: #4a6b96; }
QPushButton#apply:hover { background: #3d648f; }
QFrame#zone { background: #2b2d30; }
QFrame#divider { background: #3c3f41; }
QFrame#hdivider { background: #3c3f41; max-height: 1px; min-height: 1px; }
QSplitter::handle:vertical { height: 7px; background: #3c3f41; }
QSplitter::handle:vertical:hover { background: #4a6b96; }
QListWidget {
    background: #1e1f22; color: #dfe1e5; border: 1px solid #3a3d40;
    border-radius: 3px;
}
QScrollBar:horizontal { background: #1e1f22; height: 14px; margin: 0; }
QScrollBar::handle:horizontal { background: #4a4d50; border-radius: 6px; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""

# 信息区字段：(caption, key)。key 用于取值。
_INFO_FIELDS = [
    ("任务名", "name"),
    ("周几", "weekday"),
    ("开始时间", "start"),
    ("结束时间", "end"),
    ("持续小时", "duration"),
    ("类别", "category"),
    ("编号", "id"),
]


def _fmt_dt_full(item_dt) -> str:
    return item_dt.strftime("%Y-%m-%d %H:%M") if item_dt else "—"


class TaskEditorDialog(QDialog):
    def __init__(
        self,
        commit_data: CommitData,
        context: dict[str, str],
        strategy: Optional[AutoAdjustStrategy] = None,
    ):
        super().__init__()
        self.context = context
        self.payload = commit_data.payload
        self.strategy = strategy or get_default_strategy()

        # 编辑前（只读展示 + 上时间轴）与 编辑中（可改 + 下时间轴）两份数据。
        self.original_items: list[TaskItem] = commit_data.items
        self.working_items: list[TaskItem] = commit_data.clone_items()
        self._orig_by_id = {it.item_id: it for it in self.original_items}
        self._work_by_id = {it.item_id: it for it in self.working_items}

        self.selected_id: Optional[Any] = None
        self._syncing_scroll = False
        self._applied = False

        # 撤销/重做：整表快照栈。_baseline 始终等于“当前已记录状态”，
        # 每次离散改动与它比对去重，避免拖动过程中的中间态被记进历史。
        self._undo_stack: list[list[dict]] = []
        self._redo_stack: list[list[dict]] = []
        self._baseline: list[dict] = self._snapshot()

        self.color_map = build_color_map([it.item_id for it in self.original_items])
        self.timeline_state = compute_state(self.original_items + self.working_items)

        self.setWindowTitle("Task Editor —— 编辑 committed 任务")
        self.setStyleSheet(DARK_QSS)
        self.resize(1040, 900)  # 顶信息区 460px 后的合理默认高度，Problems 仍有空间

        self._build_ui()
        self._select_default()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # 上部块 = 顶信息 + 分割线 + 中部时间轴。SetNoConstraint 允许被向上挤压时裁剪，
        # 但内部各控件自身不缩放（顶/中“不改尺寸”）。
        upper = QWidget()
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(6)
        upper_layout.setSizeConstraint(QLayout.SetNoConstraint)
        upper_layout.addWidget(self._build_top_zone())
        upper_layout.addWidget(self._make_hdivider())
        upper_layout.addWidget(self._build_middle_zone())
        self.upper = upper
        # 挤压下限 = 顶信息(固定) + 分割线 + 少量时间轴余量；顶信息永远完整可见。
        upper.setMinimumHeight(TOP_ZONE_HEIGHT + 1 + FLOOR_TIMELINE_SLIVER)

        # Problems 顶边 = 可拖动手柄；向上拖挤压上部块，窗口变高则 Problems 变高。
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setHandleWidth(7)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(upper)
        self.splitter.addWidget(self._build_problems_zone())
        self.splitter.setStretchFactor(0, 0)  # 窗口变高时，多出的高度给 Problems
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([TOP_ZONE_HEIGHT + 1 + MIDDLE_HEIGHT + 12, 220])

        root.addWidget(self.splitter, stretch=1)
        root.addLayout(self._build_button_row())

    def _make_hdivider(self) -> QFrame:
        line = QFrame()
        line.setObjectName("hdivider")
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        return line

    def _build_top_zone(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("zone")
        # 顶信息区固定高度 460px，不压缩、不裁剪（远大于内容所需，不会裁字段）。
        frame.setFixedHeight(TOP_ZONE_HEIGHT)
        self.top_zone = frame
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        title = QLabel("选中任务详情")
        title.setProperty("role", "title")
        layout.addWidget(title)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_original_column(), stretch=1)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)
        columns.addWidget(divider)

        columns.addWidget(self._build_new_column(), stretch=1)
        layout.addLayout(columns)
        return frame

    def _build_original_column(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        header = QLabel("原信息")
        header.setProperty("role", "header")
        v.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        self._orig_value_labels: dict[str, QLabel] = {}
        for row, (caption, key) in enumerate(_INFO_FIELDS):
            cap = QLabel(f"{caption}:")
            cap.setProperty("role", "caption")
            value = QLabel("—")
            value.setWordWrap(True)
            grid.addWidget(cap, row, 0, Qt.AlignTop)
            grid.addWidget(value, row, 1)
            self._orig_value_labels[key] = value
        v.addLayout(grid)
        v.addStretch(1)
        return box

    def _build_new_column(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        header = QLabel("新信息")
        header.setProperty("role", "header")
        v.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)

        # 可编辑：任务名 / 持续小时 / 类别；其余为只读展示
        self.edit_name = QLineEdit()
        self.edit_duration = QLineEdit()
        self.edit_category = QLineEdit()
        self.new_weekday = QLabel("—")
        self.new_start = QLabel("—")
        self.new_end = QLabel("—")
        self.new_id = QLabel("—")
        for lbl in (self.new_weekday, self.new_start, self.new_end, self.new_id):
            lbl.setWordWrap(True)

        widgets = {
            "name": self.edit_name,
            "weekday": self.new_weekday,
            "start": self.new_start,
            "end": self.new_end,
            "duration": self.edit_duration,
            "category": self.edit_category,
            "id": self.new_id,
        }
        for row, (caption, key) in enumerate(_INFO_FIELDS):
            cap = QLabel(f"{caption}:")
            cap.setProperty("role", "caption")
            grid.addWidget(cap, row, 0, Qt.AlignTop)
            grid.addWidget(widgets[key], row, 1)
        v.addLayout(grid)

        note = QLabel("* 任务名 / 持续小时 / 类别 可编辑，光标离开输入框即保存")
        note.setProperty("role", "note")
        note.setWordWrap(True)
        v.addWidget(note)

        self.btn_auto = QPushButton("自动调整起止时间（结束 - 开始 = 持续小时）")
        self.btn_auto.clicked.connect(self._on_auto_adjust)
        v.addWidget(self.btn_auto)
        v.addStretch(1)

        # 光标离开即保存
        self.edit_name.editingFinished.connect(self._on_name_edited)
        self.edit_duration.editingFinished.connect(self._on_duration_edited)
        self.edit_category.editingFinished.connect(self._on_category_edited)
        return box

    def _build_middle_zone(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("zone")
        frame.setFixedHeight(MIDDLE_HEIGHT)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(2)

        label_before = QLabel("编辑前（只读参考；右键任务可改持续时间）")
        label_before.setProperty("role", "caption")
        v.addWidget(label_before)
        self.timeline_before = TimelineWidget(
            self.timeline_state,
            lambda: self.original_items,
            self.color_map,
            lambda: self.selected_id,
            editable=False,
        )
        self.timeline_before.barClicked.connect(self._on_bar_clicked)
        v.addWidget(self.timeline_before)

        label_current = QLabel("编辑中（拖动移动 · 拖两端对齐到持续时间 · 右键改持续时间）")
        label_current.setProperty("role", "caption")
        v.addWidget(label_current)
        self.timeline_current = TimelineWidget(
            self.timeline_state,
            lambda: self.working_items,
            self.color_map,
            lambda: self.selected_id,
            editable=True,
        )
        self.timeline_current.barClicked.connect(self._on_bar_clicked)
        self.timeline_current.liveEdited.connect(self._on_live_edited)
        self.timeline_current.editCommitted.connect(self._record_undo)
        self.timeline_current.durationEditRequested.connect(self._on_duration_edit_requested)
        v.addWidget(self.timeline_current)

        # 共享横向滚动条（两条轴同步平移）
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_moved)
        v.addWidget(self.scrollbar)

        self.timeline_state.changed.connect(self._sync_scrollbar)
        return frame

    def _build_problems_zone(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("zone")
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 2, 4, 2)
        v.setSpacing(4)
        header = QLabel("Problems")
        header.setProperty("role", "title")
        v.addWidget(header)

        self.problems_list = QListWidget()
        self.problems_list.setWordWrap(True)
        self.problems_list.setMinimumHeight(80)
        self.problems_list.itemClicked.connect(self._on_problem_clicked)
        v.addWidget(self.problems_list, stretch=1)
        return frame

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.btn_undo = QPushButton("撤销")
        self.btn_undo.setToolTip("撤销上一步（Ctrl+Z）")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self._undo)
        self.btn_redo = QPushButton("重做")
        self.btn_redo.setToolTip("重做（Ctrl+Y / Ctrl+Shift+Z）")
        self.btn_redo.setEnabled(False)
        self.btn_redo.clicked.connect(self._redo)
        row.addWidget(self.btn_undo)
        row.addWidget(self.btn_redo)
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setObjectName("apply")
        self.btn_apply.clicked.connect(self._on_apply)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_apply)

        # 键盘快捷键：撤销 / 重做（Windows 上重做惯用 Ctrl+Y，同时兼容 Ctrl+Shift+Z）
        self._sc_undo = QShortcut(QKeySequence.Undo, self)
        self._sc_undo.activated.connect(self._undo)
        self._sc_redo = QShortcut(QKeySequence.Redo, self)
        self._sc_redo.activated.connect(self._redo)
        self._sc_redo_y = QShortcut(QKeySequence("Ctrl+Y"), self)
        self._sc_redo_y.activated.connect(self._redo)
        self._sc_redo_z = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self._sc_redo_z.activated.connect(self._redo)
        return row

    # -------------------------------------------------------------- selection
    def _select_default(self) -> None:
        # 优先选中“有问题”的第一条任务，方便用户直接开始修
        problems = analyze(self.working_items)
        target_id = None
        for problem in problems:
            if problem.item_ids:
                target_id = problem.item_ids[0]
                break
        if target_id is None and self.working_items:
            target_id = self.working_items[0].item_id
        self._set_selected(target_id)
        self.refresh_problems()

    def _on_bar_clicked(self, item_id: Optional[Any]) -> None:
        self._set_selected(item_id)

    def _set_selected(self, item_id: Optional[Any]) -> None:
        self.selected_id = item_id
        self._populate_info()
        self.timeline_before.update()
        self.timeline_current.update()

    def _populate_info(self) -> None:
        orig = self._orig_by_id.get(self.selected_id)
        work = self._work_by_id.get(self.selected_id)

        # 原信息（只读）
        if orig is None:
            for lbl in self._orig_value_labels.values():
                lbl.setText("—")
        else:
            self._orig_value_labels["name"].setText(orig.name or "—")
            self._orig_value_labels["weekday"].setText(orig.weekday or "—")
            self._orig_value_labels["start"].setText(_fmt_dt_full(orig.start))
            self._orig_value_labels["end"].setText(_fmt_dt_full(orig.end))
            self._orig_value_labels["duration"].setText(f"{orig.duration_hours:.2f}")
            self._orig_value_labels["category"].setText(orig.category or "—")
            self._orig_value_labels["id"].setText(orig.id_text or "—")

        # 新信息（可编辑）——用 blockSignals 避免 setText 误触发保存
        has_sel = work is not None
        for widget in (self.edit_name, self.edit_duration, self.edit_category):
            widget.setEnabled(has_sel)
        self.btn_auto.setEnabled(has_sel)

        if work is None:
            for widget in (self.edit_name, self.edit_duration, self.edit_category):
                widget.blockSignals(True)
                widget.setText("")
                widget.blockSignals(False)
            for lbl in (self.new_weekday, self.new_start, self.new_end, self.new_id):
                lbl.setText("—")
            return

        for widget, value in (
            (self.edit_name, work.name),
            (self.edit_duration, f"{work.duration_hours:.2f}"),
            (self.edit_category, work.category),
        ):
            widget.blockSignals(True)
            widget.setText(value)
            widget.blockSignals(False)
        self._refresh_new_time_labels()

    def _refresh_new_time_labels(self) -> None:
        work = self._work_by_id.get(self.selected_id)
        if work is None:
            return
        self.new_weekday.setText(work.weekday or "—")
        self.new_start.setText(_fmt_dt_full(work.start))
        self.new_end.setText(_fmt_dt_full(work.end))
        self.new_id.setText(work.id_text or "—")

    # ------------------------------------------------------------- edit saves
    def _on_name_edited(self) -> None:
        work = self._work_by_id.get(self.selected_id)
        if work is None:
            return
        work.name = self.edit_name.text().strip()
        self._after_working_change()

    def _on_category_edited(self) -> None:
        work = self._work_by_id.get(self.selected_id)
        if work is None:
            return
        work.category = self.edit_category.text().strip()
        self._after_working_change()

    def _on_duration_edited(self) -> None:
        work = self._work_by_id.get(self.selected_id)
        if work is None:
            return
        raw = self.edit_duration.text().strip()
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            # 非法输入：还原为当前值，不保存
            self.edit_duration.blockSignals(True)
            self.edit_duration.setText(f"{work.duration_hours:.2f}")
            self.edit_duration.blockSignals(False)
            return
        work.duration_hours = value
        self._after_working_change()

    def _on_duration_edit_requested(self, item_id: Any) -> None:
        """右键任务条弹出的小窗：仅更改持续小时（不动起止时间；一致性交给自动调整）。"""
        work = self._work_by_id.get(item_id)
        if work is None:
            return
        self._set_selected(item_id)
        value, ok = QInputDialog.getDouble(
            self, "修改持续时间", "持续小时:", float(work.duration_hours), 0.0, 100000.0, 2
        )
        if not ok:
            return
        work.duration_hours = value
        self.edit_duration.blockSignals(True)
        self.edit_duration.setText(f"{work.duration_hours:.2f}")
        self.edit_duration.blockSignals(False)
        self._after_working_change()

    def _on_auto_adjust(self) -> None:
        work = self._work_by_id.get(self.selected_id)
        if work is None:
            return
        others = [it for it in self.working_items if it.item_id != work.item_id]
        new_start, new_end = self.strategy.adjust(work, others)
        work.set_span(new_start, new_end)
        self._refresh_new_time_labels()
        self._after_working_change()

    def _after_working_change(self) -> None:
        self._refresh_new_time_labels()
        self.timeline_current.update()
        self.refresh_problems()
        self._record_undo()

    def _on_live_edited(self) -> None:
        """时间轴拖动/边缘对齐过程中：刷新新信息里的时间与时长、以及 Problems。"""
        work = self._work_by_id.get(self.selected_id)
        if work is not None:
            self._refresh_new_time_labels()
            self.edit_duration.blockSignals(True)
            self.edit_duration.setText(f"{work.duration_hours:.2f}")
            self.edit_duration.blockSignals(False)
        self.refresh_problems()

    # ------------------------------------------------------------ undo / redo
    def _snapshot(self) -> list[dict]:
        """整表工作副本的深拷贝快照（按 working_items 顺序）。"""
        return [copy.deepcopy(it.raw) for it in self.working_items]

    def _restore(self, snap: list[dict]) -> None:
        """把快照写回各任务（按编号匹配，不改对象身份），并刷新全部视图。"""
        by_id = {raw.get("编号"): raw for raw in snap}
        for item in self.working_items:
            raw = by_id.get(item.item_id)
            if raw is not None:
                item.raw = copy.deepcopy(raw)
        self.timeline_before.update()
        self.timeline_current.update()
        self._populate_info()
        self.refresh_problems()

    def _record_undo(self) -> None:
        """一次离散改动完成后调用：与基线比对，有净变化才压栈。"""
        snap = self._snapshot()
        if snap == self._baseline:
            return  # 无净变化（拖动后回缩、失焦未改等），不记历史
        self._undo_stack.append(self._baseline)
        self._redo_stack.clear()  # 新分支产生，重做历史作废
        self._baseline = snap
        self._update_undo_buttons()

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(self._baseline)
        self._baseline = self._undo_stack.pop()
        self._restore(self._baseline)
        self._update_undo_buttons()

    def _redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(self._baseline)
        self._baseline = self._redo_stack.pop()
        self._restore(self._baseline)
        self._update_undo_buttons()

    def _update_undo_buttons(self) -> None:
        self.btn_undo.setEnabled(bool(self._undo_stack))
        self.btn_redo.setEnabled(bool(self._redo_stack))

    # -------------------------------------------------------------- problems
    def refresh_problems(self) -> None:
        self.problems_list.clear()
        problems = analyze(self.working_items)
        if not problems:
            item = QListWidgetItem("✓ 未发现时间重叠或持续时间不一致问题")
            item.setForeground(QColor("#6a9955"))
            self.problems_list.addItem(item)
            return
        for problem in problems:
            self._add_problem_row(problem)

    def _add_problem_row(self, problem: Problem) -> None:
        if problem.severity is Severity.ERROR:
            icon, color = "❌", QColor("#e06c75")
        else:
            icon, color = "⚠", QColor("#e5c07b")
        item = QListWidgetItem(f"{icon} {problem.message}")
        item.setForeground(color)
        # 存下涉及的任务，点击可跳转选中
        item.setData(Qt.UserRole, problem.item_ids)
        self.problems_list.addItem(item)

    def _on_problem_clicked(self, item: QListWidgetItem) -> None:
        item_ids = item.data(Qt.UserRole)
        if item_ids:
            self._set_selected(item_ids[0])

    # -------------------------------------------------------------- scrolling
    def _on_scrollbar_moved(self, value: int) -> None:
        if self._syncing_scroll:
            return
        self.timeline_state.set_offset_seconds(float(value))

    def _sync_scrollbar(self) -> None:
        self._syncing_scroll = True
        try:
            max_off = int(self.timeline_state.max_offset())
            self.scrollbar.setRange(0, max_off)
            self.scrollbar.setPageStep(int(self.timeline_state.visible_seconds()))
            self.scrollbar.setSingleStep(max(int(self.timeline_state.visible_seconds() / 10), 1))
            self.scrollbar.setValue(int(self.timeline_state.offset_seconds))
        finally:
            self._syncing_scroll = False

    # ----------------------------------------------------------------- apply
    def _on_apply(self) -> None:
        data = CommitData(payload=self.payload, items=self.working_items)
        save_commit_data(self.context, data)
        self._applied = True
        self.accept()

    @property
    def applied(self) -> bool:
        return self._applied


def run_editor(commit_data: CommitData, context: dict[str, str]) -> bool:
    """
    打开编辑器，阻塞直到关闭。返回 True 表示用户点了“应用”并已写回。
    """
    app = QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QApplication([])

    dialog = TaskEditorDialog(commit_data, context)
    dialog.exec()
    return dialog.applied
