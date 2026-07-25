"""
committed 任务的两类问题检测：

1. 时间重叠：两条及以上任务的 [开始, 结束] 区间有交集（闭区间，触碰也算，
   与 chat.py / log_chat_requirements.md 第 6 节保持一致）。
2. 持续时间不一致：任意任务的 (结束 - 开始) 折算小时 != 声明的持续小时。

检测结果统一用 Problem 表达，供终端报告和 GUI 的 Problems 面板复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .store import TaskItem

# 持续时间比对容差（小时）。0.01h = 36 秒，与 chat.py 的 _check_consistency 对齐。
DURATION_TOLERANCE_HOURS = 0.01


class Severity(str, Enum):
    ERROR = "error"      # 时间重叠 —— JetBrains 风格红色 ❌
    WARNING = "warning"  # 持续时间不一致 —— 黄色 ⚠


@dataclass
class Problem:
    severity: Severity
    message: str
    item_ids: tuple[Any, ...] = field(default_factory=tuple)

    @property
    def is_overlap(self) -> bool:
        return self.severity is Severity.ERROR


def _fmt_dt(dt: datetime) -> str:
    """时间轴/报告里的紧凑时间格式：MM-DD HH:MM。"""
    return dt.strftime("%m-%d %H:%M")


def find_overlaps(items: list[TaskItem]) -> list[Problem]:
    """两两比较，闭区间有交集即算重叠。"""
    problems: list[Problem] = []
    count = len(items)
    for i in range(count):
        for j in range(i + 1, count):
            a, b = items[i], items[j]
            sa, ea, sb, eb = a.start, a.end, b.start, b.end
            if sa is None or ea is None or sb is None or eb is None:
                continue
            # 闭区间相交：sa <= eb and sb <= ea
            if sa <= eb and sb <= ea:
                lo = max(sa, sb)
                hi = min(ea, eb)
                problems.append(
                    Problem(
                        Severity.ERROR,
                        f"{a.id_text}:{a.name} 与 {b.id_text}:{b.name} 时间重叠"
                        f"（{_fmt_dt(lo)} ~ {_fmt_dt(hi)}）",
                        (a.item_id, b.item_id),
                    )
                )
    return problems


def find_duration_mismatches(
    items: list[TaskItem], tolerance_hours: float = DURATION_TOLERANCE_HOURS
) -> list[Problem]:
    """结束-开始 与 声明持续小时 不符（或时间不可解析）时产出一条 warning。"""
    problems: list[Problem] = []
    for item in items:
        measured = item.measured_hours
        stated = item.duration_hours
        if measured is None:
            problems.append(
                Problem(
                    Severity.WARNING,
                    f"{item.id_text}:{item.name} 时间格式非法，无法核对持续时间"
                    f"（开始={item.start_iso or '空'}，结束={item.end_iso or '空'}）",
                    (item.item_id,),
                )
            )
            continue
        if abs(measured - stated) > tolerance_hours:
            problems.append(
                Problem(
                    Severity.WARNING,
                    f"{item.id_text}:{item.name} 持续时间不一致"
                    f"（声明 {stated:.2f} 小时，实际 {measured:.2f} 小时）",
                    (item.item_id,),
                )
            )
    return problems


def analyze(items: list[TaskItem]) -> list[Problem]:
    """重叠在前（error），持续不一致在后（warning）。"""
    return find_overlaps(items) + find_duration_mismatches(items)
