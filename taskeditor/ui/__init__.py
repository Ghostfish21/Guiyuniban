"""PySide6 图形界面子包。仅在 `log edit` / 有问题的 `log check` 时才被导入。"""

from .window import run_editor

__all__ = ["run_editor"]
