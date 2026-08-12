"""
`log coend` 的选择窗口（PySide6）。

同时有多个并发任务在进行时，coend 需要知道结束哪一个。这里按嵌套层级缩进列出
当前所有进行中的并发任务，默认选中最内层的那个（最可能是用户此刻正在做的事）。

不提供按任务名结束的命令行形式——名字太长不好打，所以走窗口。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .window import DARK_QSS

WINDOW_WIDTH = 560
WINDOW_HEIGHT = 420
INDENT = "    "


class CoTaskPickerDialog(QDialog):
    def __init__(self, rows: list[dict[str, Any]]):
        super().__init__()
        self.rows = rows
        self._picked: Optional[str] = None

        self.setWindowTitle("log coend —— 选择要结束的并发任务")
        self.setStyleSheet(DARK_QSS)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self._build_ui()
        # 默认选最内层：列表按开始时间排序，最后一条就是当前路径的最深处。
        self.task_list.setCurrentRow(len(rows) - 1 if rows else 0)
        self.task_list.setFocus()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(8)

        title = QLabel("当前有多个并发任务在进行，选择要结束的一个：")
        title_font = QFont()
        title_font.setBold(True)
        title.setFont(title_font)
        root.addWidget(title)

        hint = QLabel("缩进表示嵌套层级；双击或按回车确认，Esc 取消。")
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        self.task_list = QListWidget()
        self.task_list.itemDoubleClicked.connect(lambda _item: self._accept_current())
        root.addWidget(self.task_list, 1)

        for row in self.rows:
            depth = max(1, int(row.get("depth") or 1))
            label = f"{INDENT * (depth - 1)}{row.get('name') or '未命名任务'}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, str(row.get("co_id") or ""))
            detail = f"已进行 {row.get('elapsed_text') or '未知'} · {row.get('start_time') or ''} 开始"
            item.setToolTip(detail)
            item.setForeground(QColor("#e8eaed") if depth == 1 else QColor("#c7cdd4"))
            self.task_list.addItem(item)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: #9aa0a6;")
        root.addWidget(self.detail_label)
        self.task_list.currentRowChanged.connect(self._update_detail)
        self._update_detail(self.task_list.currentRow())

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        confirm = QPushButton("结束该任务")
        confirm.setDefault(True)
        confirm.clicked.connect(self._accept_current)
        buttons.addWidget(confirm)
        root.addLayout(buttons)

    # -------------------------------------------------------------- 行为
    def _update_detail(self, row_index: int) -> None:
        if 0 <= row_index < len(self.rows):
            row = self.rows[row_index]
            self.detail_label.setText(
                f"已进行 {row.get('elapsed_text') or '未知'}　·　{row.get('start_time') or ''} 开始"
            )
        else:
            self.detail_label.setText("")

    def _accept_current(self) -> None:
        item = self.task_list.currentItem()
        if item is None:
            return
        self._picked = str(item.data(Qt.UserRole) or "")
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 列表里按回车默认不会触发 defaultButton，这里显式接管。
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._accept_current()
            return
        super().keyPressEvent(event)

    @property
    def picked(self) -> Optional[str]:
        return self._picked


# 持有 QApplication 引用：在 log 窗口里可能多次开面板，避免实例被回收。
_app: Any = None


def run_co_picker(rows: list[dict[str, Any]]) -> Optional[str]:
    """
    弹出选择窗口，阻塞直到关闭。返回选中的 co_id；取消返回 None。
    """
    global _app
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _app = app

    dialog = CoTaskPickerDialog(rows)
    dialog.exec()
    return dialog.picked
