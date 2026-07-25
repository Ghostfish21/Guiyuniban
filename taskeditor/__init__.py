"""
taskeditor —— `log check` / `log edit` 的实现。

- `store`      : committed 数据（commit_preview.txt）的读写与 TaskItem 模型
- `analysis`   : 时间重叠 / 持续时间不一致 的问题检测
- `autoadjust` : 自动调整起止时间的可替换策略接口 + 默认实现
- `commands`   : check_task / edit_task 两个命令入口
- `ui`         : PySide6 图形界面（仅在需要时才导入，避免无 GUI 场景强依赖 Qt）
"""

from .commands import check_task, edit_task

__all__ = ["check_task", "edit_task"]
