"""
“自动调整起止时间”策略接口 + 默认实现（需求点 12）。

设计成可替换接口：GUI 只依赖 AutoAdjustStrategy.adjust()，算法可整体替换。

    strategy = get_default_strategy()
    new_start, new_end = strategy.adjust(target, others)

约束：返回的 (new_start, new_end) 必须满足 new_end - new_start == 目标持续小时。

默认实现 DefaultAutoAdjust 按目标与其它任务的重叠情况分四种情形（点 12）：

  A. 目标被“某一个任务”完全包含        -> 用两个时间中点算权重，围绕自身中点加权扩缩
  B. 目标与“不止一个任务”重叠          -> 围绕自身中点均匀扩缩
  C. 目标只与“一个任务”部分重叠        -> 按中点方向把目标挤到相邻空位，预留 10~min(g_max,60) 秒
                                          随机间隔（保证不与他人重叠）；放不下则回退到 A 的加权扩缩
  D. 目标与任何任务都不重叠            -> 空间够则均匀扩张；某端先碰到邻居则预留 10~30 秒随机间隔，
                                          再调另一端使 end-start==duration

说明：点 12 的“10~max(60,n)”里 n 指“预留后仍不重叠所允许的最大间隔”。在“不得重叠”这一硬约束下，
取值上界实际收敛为 min(g_max, 60)（g_max=该方向可用余量）；这里据此实现，GAP_* 常量可调。
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from .store import TaskItem

GAP_MIN_SECONDS = 10          # 预留间隔下限
GAP_SOFT_CAP_SECONDS = 60     # 情形 C 预留间隔软上限（对应 max(60,n) 在不重叠约束下的效果）
GAP_D_MAX_SECONDS = 30        # 情形 D 碰到邻居时预留 10~30 秒


def _mid(start: datetime, end: datetime) -> datetime:
    return start + (end - start) / 2


def _round(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


class AutoAdjustStrategy(ABC):
    """可替换接口：给定目标任务和其它任务，算出满足时长的新起止时间。"""

    @abstractmethod
    def adjust(
        self, target: TaskItem, others: list[TaskItem]
    ) -> tuple[datetime, datetime]:
        raise NotImplementedError


class DefaultAutoAdjust(AutoAdjustStrategy):
    """点 12 的默认实现。rng 可注入以便测试复现。"""

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()

    # ---------------------------------------------------------------- 入口
    def adjust(
        self, target: TaskItem, others: list[TaskItem]
    ) -> tuple[datetime, datetime]:
        duration = timedelta(hours=max(target.duration_hours, 0.0))
        ts, te = target.start, target.end
        if ts is None or te is None:
            anchor = ts or te or datetime.now().astimezone()
            return _round(anchor), _round(anchor + duration)

        m_self = _mid(ts, te)
        valid = [(o.start, o.end) for o in others if o.start is not None and o.end is not None]
        overlappers = [(s, e) for (s, e) in valid if s < te and ts < e]  # 严格重叠（触碰不算）

        if not overlappers:
            start, end = self._case_no_overlap(ts, te, m_self, duration, valid)
        elif len(overlappers) >= 2:
            start, end = self._even(m_self, duration)               # 情形 B
        else:
            os_, oe_ = overlappers[0]
            if os_ <= ts and te <= oe_:
                start, end = self._weighted(m_self, duration, os_, oe_)   # 情形 A
            else:
                start, end = self._case_partial(m_self, duration, os_, oe_, valid)  # 情形 C
        return _round(start), _round(end)

    # ---------------------------------------------------------------- 基元
    def _even(self, m_self: datetime, duration: timedelta) -> tuple[datetime, datetime]:
        half = duration / 2
        return m_self - half, m_self + half

    def _weighted(
        self, m_self: datetime, duration: timedelta, os_: datetime, oe_: datetime
    ) -> tuple[datetime, datetime]:
        span = (oe_ - os_).total_seconds()
        if span <= 0:
            return self._even(m_self, duration)
        m_other = _mid(os_, oe_)
        offset = (m_self - m_other).total_seconds() / span   # 约 [-0.5, 0.5]
        w_left = min(max(0.5 + offset, 0.0), 1.0)            # 目标偏右则更多向左扩，趋向包含者中心
        start = m_self - duration * w_left
        return start, start + duration

    def _left_boundary(self, point: datetime, tasks: list[tuple[datetime, datetime]]) -> Optional[datetime]:
        """point 左侧最近的“障碍”时间（有任务占据处）；None 表示左侧无限空。"""
        bound: Optional[datetime] = None
        for s, e in tasks:
            if s >= point:
                continue
            bp = e if e < point else point
            bound = bp if bound is None else max(bound, bp)
        return bound

    def _right_boundary(self, point: datetime, tasks: list[tuple[datetime, datetime]]) -> Optional[datetime]:
        bound: Optional[datetime] = None
        for s, e in tasks:
            if e <= point:
                continue
            bp = s if s > point else point
            bound = bp if bound is None else min(bound, bp)
        return bound

    def _pick_gap(self, g_max: Optional[float], cap: float) -> float:
        upper = cap if g_max is None else min(g_max, cap)
        upper = max(GAP_MIN_SECONDS, upper)
        return self._rng.uniform(GAP_MIN_SECONDS, upper)

    # ---------------------------------------------------------------- 情形 C
    def _case_partial(
        self,
        m_self: datetime,
        duration: timedelta,
        os_: datetime,
        oe_: datetime,
        valid: list[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime]:
        dur_s = duration.total_seconds()
        # 排除“正在重叠的那一条”，只看其它任务作为空位边界。
        others = [pair for pair in valid if pair != (os_, oe_)]
        m_other = _mid(os_, oe_)
        push_right = m_self >= m_other

        if push_right:
            bound = self._right_boundary(oe_, others)
            slot = (bound - oe_).total_seconds() if bound is not None else None
            g_max = None if slot is None else slot - dur_s
            if g_max is None or g_max >= GAP_MIN_SECONDS:
                g = self._pick_gap(g_max, GAP_SOFT_CAP_SECONDS)
                start = oe_ + timedelta(seconds=g)
                return start, start + duration
        else:
            bound = self._left_boundary(os_, others)
            slot = (os_ - bound).total_seconds() if bound is not None else None
            g_max = None if slot is None else slot - dur_s
            if g_max is None or g_max >= GAP_MIN_SECONDS:
                g = self._pick_gap(g_max, GAP_SOFT_CAP_SECONDS)
                end = os_ - timedelta(seconds=g)
                return end - duration, end

        # 放不下：回退到加权扩缩
        return self._weighted(m_self, duration, os_, oe_)

    # ---------------------------------------------------------------- 情形 D
    def _case_no_overlap(
        self,
        ts: datetime,
        te: datetime,
        m_self: datetime,
        duration: timedelta,
        valid: list[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime]:
        dur_s = duration.total_seconds()
        lb = self._left_boundary(ts, valid)     # 目标左侧最近障碍
        rb = self._right_boundary(te, valid)    # 目标右侧最近障碍
        slot_s = None
        if lb is not None and rb is not None:
            slot_s = (rb - lb).total_seconds()

        # 空间不足以容纳目标：尽力均匀扩张（可能仍偏挤，交给用户）
        if slot_s is not None and slot_s < dur_s:
            return self._even(m_self, duration)

        start, end = self._even(m_self, duration)
        left_hit = lb is not None and start < lb
        right_hit = rb is not None and end > rb

        if left_hit and not right_hit:
            avail = None if rb is None else (rb - lb).total_seconds() - dur_s
            g = self._pick_gap(avail, GAP_D_MAX_SECONDS)
            start = lb + timedelta(seconds=g)
            end = start + duration
        elif right_hit and not left_hit:
            avail = None if lb is None else (rb - lb).total_seconds() - dur_s
            g = self._pick_gap(avail, GAP_D_MAX_SECONDS)
            end = rb - timedelta(seconds=g)
            start = end - duration
        # 两端都不碰 或 两端都碰（空间恰好/不足）：用均匀扩张结果
        return start, end


class SymmetricAutoAdjust(AutoAdjustStrategy):
    """备用：仅围绕原时间中点对称扩缩，不做碰撞检测。"""

    def adjust(
        self, target: TaskItem, others: list[TaskItem]
    ) -> tuple[datetime, datetime]:
        duration = timedelta(hours=max(target.duration_hours, 0.0))
        start, end = target.start, target.end
        if start is not None and end is not None:
            midpoint = _mid(start, end)
            return _round(midpoint - duration / 2), _round(midpoint + duration / 2)
        anchor = start or end or datetime.now().astimezone()
        return _round(anchor), _round(anchor + duration)


# 模块级默认策略；替换算法时改这里（或在 GUI 构造时注入）即可。
DEFAULT_STRATEGY: AutoAdjustStrategy = DefaultAutoAdjust()


def get_default_strategy() -> AutoAdjustStrategy:
    return DEFAULT_STRATEGY
