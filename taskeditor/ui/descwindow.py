"""
`log desc` 的窗口（PySide6）：commit 前编辑任务描述。

结构（对应需求）：
  - 上半部分：**只显示**任务信息（左=未 commit 的 session 列表，右=选中项只读信息）
  - 下半部分：编辑该 session 的详细描述
  - 上下高度约 1:2，中间是可拖动的分割手柄

底部：保存 / 取消。保存只把 `详细描述` 写回 uncommit_tasks.txt，
之后 `log commit` 会把它带进 committed 池。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..desc_store import DescData, SessionItem, save_desc_data
from .window import DARK_QSS

# 上下 1:2。窗口默认高度按这个比例分配，之后用户可拖动手柄自行调整。
TOP_RATIO = 1
BOTTOM_RATIO = 2
WINDOW_WIDTH = 940
WINDOW_HEIGHT = 780
TOP_MIN_HEIGHT = 180
BOTTOM_MIN_HEIGHT = 160

# 有描述 / 无描述的列表前缀，用来一眼看出哪些任务已经写过描述
MARK_FILLED = "✎ "
MARK_EMPTY = "　 "

# 信息区字段：(caption, key)
_INFO_FIELDS = [
    ("任务名", "name"),
    ("本次输入名", "typed_name"),
    ("周几", "weekday"),
    ("开始时间", "start"),
    ("结束时间", "end"),
    ("持续", "duration"),
    ("指令", "command"),
    ("Session ID", "session_id"),
    ("任务组 ID", "task_group_id"),
]


def _fmt_dt(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def _fmt_time(value: Optional[datetime]) -> str:
    return value.strftime("%H:%M") if value else "--:--"


def _fmt_duration(hours: Optional[float]) -> str:
    if hours is None:
        return "进行中"
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    if whole and minutes:
        return f"{whole} 小时 {minutes} 分钟（{hours:.2f} h）"
    if whole:
        return f"{whole} 小时（{hours:.2f} h）"
    return f"{minutes} 分钟（{hours:.2f} h）"


class TaskDescriptionDialog(QDialog):
    def __init__(self, data: DescData, context: dict[str, str]):
        super().__init__()
        self.context = context
        self.data = data
        self.sessions: list[SessionItem] = data.sessions
        self._initial = data.snapshot()  # 用于判断哪些 session 真被改过
        self._saved = False
        self._changed_count = 0

        self.setWindowTitle("Task Description —— commit 前编辑任务描述")
        self.setStyleSheet(DARK_QSS)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self._build_ui()
        self._fill_session_list()
        if self.sessions:
            self.session_list.setCurrentRow(0)
        else:
            self._populate_info(None)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setHandleWidth(7)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_top_zone())
        self.splitter.addWidget(self._build_bottom_zone())
        # 上:下 = 1:2，窗口变高时也按这个比例分配多出来的高度
        self.splitter.setStretchFactor(0, TOP_RATIO)
        self.splitter.setStretchFactor(1, BOTTOM_RATIO)
        usable = WINDOW_HEIGHT - 90  # 去掉按钮行与边距后的可分配高度
        top = usable * TOP_RATIO // (TOP_RATIO + BOTTOM_RATIO)
        self.splitter.setSizes([top, usable - top])

        root.addWidget(self.splitter, stretch=1)
        root.addLayout(self._build_button_row())

    def _build_top_zone(self) -> QWidget:
        """上半部分：只读展示任务信息（左列表 + 右信息表）。"""
        frame = QFrame()
        frame.setObjectName("zone")
        frame.setMinimumHeight(TOP_MIN_HEIGHT)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 2, 4, 2)
        v.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("任务信息")
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setProperty("role", "note")
        header.addWidget(self.count_label)
        v.addLayout(header)

        columns = QHBoxLayout()
        columns.setSpacing(12)

        self.session_list = QListWidget()
        self.session_list.setMinimumWidth(300)
        self.session_list.currentRowChanged.connect(self._on_session_changed)
        columns.addWidget(self.session_list, stretch=2)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)
        columns.addWidget(divider)

        columns.addWidget(self._build_info_column(), stretch=3)
        v.addLayout(columns, stretch=1)
        return frame

    def _build_info_column(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        header = QLabel("任务信息（只读）")
        header.setProperty("role", "header")
        v.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        self._info_labels: dict[str, QLabel] = {}
        for row, (caption, key) in enumerate(_INFO_FIELDS):
            cap = QLabel(f"{caption}:")
            cap.setProperty("role", "caption")
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(cap, row, 0, Qt.AlignTop)
            grid.addWidget(value, row, 1)
            self._info_labels[key] = value
        v.addLayout(grid)
        v.addStretch(1)
        return box

    def _build_bottom_zone(self) -> QWidget:
        """下半部分：编辑选中 session 的详细描述。"""
        frame = QFrame()
        frame.setObjectName("zone")
        frame.setMinimumHeight(BOTTOM_MIN_HEIGHT)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 2, 4, 2)
        v.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("编辑描述")
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addStretch(1)
        self.desc_target_label = QLabel("—")
        self.desc_target_label.setProperty("role", "caption")
        header.addWidget(self.desc_target_label)
        v.addLayout(header)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText(
            "为这一段任务写详细描述；留空即不记录描述。\n"
            "描述会在 log commit 时随任务带入 committed 池（按 session 分别保存）。"
        )
        self.desc_edit.textChanged.connect(self._on_desc_text_changed)
        v.addWidget(self.desc_edit, stretch=1)

        note = QLabel("* 切换任务即保留当前输入；点击“保存”写回未 commit 记录（Ctrl+S）。")
        note.setProperty("role", "note")
        note.setWordWrap(True)
        v.addWidget(note)
        return frame

    def _build_button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.hint_label = QLabel("")
        self.hint_label.setProperty("role", "note")
        row.addWidget(self.hint_label)
        row.addStretch(1)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("apply")
        self.btn_save.clicked.connect(self._on_save)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_save)

        self._sc_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self._sc_save.activated.connect(self._on_save)
        return row

    # -------------------------------------------------------------- 列表填充
    def _list_text(self, item: SessionItem) -> str:
        mark = MARK_FILLED if item.description.strip() else MARK_EMPTY
        if item.running:
            span = f"{_fmt_time(item.start)}– 进行中"
        else:
            span = f"{_fmt_time(item.start)}–{_fmt_time(item.end)}"
        return f"{mark}{item.name}  ·  {item.weekday} {span}"

    def _fill_session_list(self) -> None:
        self.session_list.clear()
        for index, item in enumerate(self.sessions):
            row = QListWidgetItem(self._list_text(item))
            row.setData(Qt.UserRole, index)
            if item.running:
                row.setForeground(QColor("#e5c07b"))
            self.session_list.addItem(row)
        self.count_label.setText(f"未 commit session：{len(self.sessions)} 条")
        self._update_hint()

    def _refresh_current_row_text(self) -> None:
        row = self.session_list.currentRow()
        item = self._current_session()
        if item is None or row < 0:
            return
        list_item = self.session_list.item(row)
        if list_item is not None:
            list_item.setText(self._list_text(item))

    # ------------------------------------------------------------ 选中与展示
    def _current_session(self) -> Optional[SessionItem]:
        row = self.session_list.currentRow()
        if 0 <= row < len(self.sessions):
            return self.sessions[row]
        return None

    def _on_session_changed(self, _row: int) -> None:
        item = self._current_session()
        self._populate_info(item)
        self._load_desc_text(item)

    def _populate_info(self, item: Optional[SessionItem]) -> None:
        if item is None:
            for label in self._info_labels.values():
                label.setText("—")
            return

        typed = item.typed_name
        values = {
            "name": item.name or "—",
            "typed_name": typed if typed and typed != item.name else "—",
            "weekday": item.weekday,
            "start": _fmt_dt(item.start),
            "end": "进行中（尚未 log end）" if item.running else _fmt_dt(item.end),
            "duration": _fmt_duration(item.duration_hours),
            "command": item.command or "—",
            "session_id": item.session_id or "—",
            "task_group_id": item.task_group_id or "—",
        }
        for key, text in values.items():
            self._info_labels[key].setText(text)

    def _load_desc_text(self, item: Optional[SessionItem]) -> None:
        """把选中 session 的描述填进编辑框；blockSignals 避免误触发保存。"""
        self.desc_edit.blockSignals(True)
        self.desc_edit.setPlainText(item.description if item is not None else "")
        self.desc_edit.blockSignals(False)
        self.desc_edit.setEnabled(item is not None)
        self.desc_target_label.setText(item.name if item is not None else "—")

    def _on_desc_text_changed(self) -> None:
        item = self._current_session()
        if item is None:
            return
        item.description = self.desc_edit.toPlainText()
        self._refresh_current_row_text()
        self._update_hint()

    def _changed_ids(self) -> set[str]:
        current = self.data.snapshot()
        return {
            session_id
            for session_id, text in current.items()
            if text != self._initial.get(session_id, "")
        }

    def _update_hint(self) -> None:
        count = len(self._changed_ids())
        self.hint_label.setText(f"待保存改动：{count} 条" if count else "暂无改动")

    # ----------------------------------------------------------------- 保存
    def _on_save(self) -> None:
        changed = self._changed_ids()
        save_desc_data(self.context, self.data, changed)
        self._saved = True
        self._changed_count = len(changed)
        self.accept()

    @property
    def saved(self) -> bool:
        return self._saved

    @property
    def changed_count(self) -> int:
        return self._changed_count


# 持有 QApplication 引用：在 log 窗口里可能多次开面板，避免实例被回收。
_app: Any = None


def run_description_editor(data: DescData, context: dict[str, str]) -> tuple[bool, int]:
    """
    打开描述编辑面板，阻塞直到关闭。

    返回 (是否保存, 改动条数)。
    """
    global _app
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _app = app

    dialog = TaskDescriptionDialog(data, context)
    dialog.exec()
    return dialog.saved, dialog.changed_count
