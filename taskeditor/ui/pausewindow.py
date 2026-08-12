"""
`log pause` 的全屏暂停窗口（PySide6）。

无边框、全屏、置顶，只有一个 resume 按钮：暂停期间挡住屏幕，逼着人真的离开。
关闭事件和 Esc 都被吞掉，只有点 resume 才退出——这条性质是 pause 子系统的前提，
它保证暂停区间里发不出任何 log 命令，扣减时不用处理区间内的分叉。

置顶只能做到 Qt 层面的 WindowStaysOnTopHint：Windows 上仍然按得动 Alt+Tab / Win 键，
拦不住也不该拦（拦了会连系统弹窗一起挡掉）。默认只覆盖主屏。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .window import DARK_QSS

PAUSE_QSS = """
QDialog { background: #1b1c1e; }
QLabel#headline { color: #ffffff; font-size: 44px; font-weight: bold; }
QLabel#elapsed { color: #f5c542; font-size: 72px; font-weight: bold; }
QLabel#detail { color: #9aa0a6; font-size: 15px; }
QLabel#note { color: #6f7276; font-size: 13px; }
QPushButton#resume {
    background: #365880; color: #ffffff; border: 1px solid #4a6b96;
    border-radius: 6px; padding: 14px 60px; font-size: 20px; font-weight: bold;
}
QPushButton#resume:hover { background: #3d648f; }
"""


def _format_clock(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class PauseDialog(QDialog):
    def __init__(self, rows: dict[str, Any]):
        super().__init__()
        self.rows = rows
        self._resumed = False
        # 复用记录里已经走过的秒数：窗口被强杀后重新 log pause 时接着走，不从零开始。
        self._seconds = int(rows.get("elapsed_seconds") or 0)

        self.setWindowTitle("log pause —— 暂停中")
        self.setStyleSheet(DARK_QSS + PAUSE_QSS)
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(60, 60, 60, 60)
        root.setSpacing(18)
        root.addStretch(1)

        headline = QLabel("已暂停")
        headline.setObjectName("headline")
        headline.setAlignment(Qt.AlignCenter)
        root.addWidget(headline)

        self.elapsed_label = QLabel(_format_clock(self._seconds))
        self.elapsed_label.setObjectName("elapsed")
        self.elapsed_label.setAlignment(Qt.AlignCenter)
        self.elapsed_label.setFont(QFont("Consolas"))
        root.addWidget(self.elapsed_label)

        detail = QLabel(
            f"暂停归属：{self.rows.get('task_name') or '当前任务'}"
            f"　·　{self.rows.get('start_time') or ''} 开始"
        )
        detail.setObjectName("detail")
        detail.setAlignment(Qt.AlignCenter)
        root.addWidget(detail)

        note = QLabel("这段时间会在 log end 时从任务时长里扣掉，不产出任何任务。")
        note.setObjectName("note")
        note.setAlignment(Qt.AlignCenter)
        root.addWidget(note)

        root.addSpacing(20)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        resume = QPushButton("resume")
        resume.setObjectName("resume")
        resume.setDefault(True)
        resume.setAutoDefault(True)
        resume.clicked.connect(self._accept_resume)
        buttons.addWidget(resume)
        buttons.addStretch(1)
        root.addLayout(buttons)

        root.addStretch(1)
        self.resume_button = resume
        resume.setFocus()

    # -------------------------------------------------------------- 行为
    def _tick(self) -> None:
        self._seconds += 1
        self.elapsed_label.setText(_format_clock(self._seconds))

    def _accept_resume(self) -> None:
        self._resumed = True
        self._timer.stop()
        self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # Alt+F4 / 窗口管理器的关闭都走这里。没点 resume 就不许走，
        # 否则会留下没有 end_time 的暂停记录。
        if self._resumed:
            super().closeEvent(event)
            return
        event.ignore()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # QDialog 默认 Esc = reject，会绕过 resume 直接关掉。
        if event.key() == Qt.Key_Escape:
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        # 同上：任何非 resume 的退出路径都堵掉。
        if self._resumed:
            super().reject()

    @property
    def resumed(self) -> bool:
        return self._resumed


# 持有 QApplication 引用：在 log 窗口里可能多次开面板，避免实例被回收。
_app: Any = None


def run_pause_window(rows: dict[str, Any]) -> bool:
    """
    弹出全屏暂停窗口，阻塞到用户点 resume。

    返回 True 表示正常 resume；False 表示窗口被外部强行结束（进程被杀时根本不会
    返回，这里主要兜 Qt 自身异常关闭），调用方据此提示用 log resume 收口。
    """
    global _app
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _app = app

    dialog = PauseDialog(rows)
    dialog.showFullScreen()
    dialog.raise_()
    dialog.activateWindow()
    dialog.exec()
    return dialog.resumed
