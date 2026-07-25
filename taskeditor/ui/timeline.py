"""
Premiere 风格时间轴控件（自绘 QWidget）。

自绘而非 QGraphicsView：需要完全掌控“横向缩放但条高/字号不变”、“重叠任务同一横带
堆叠 + 逐次点击穿透选中下层”、“边缘对齐带起步距离/回缩”这些交互。

已实现：
  - 标尺 + 任务条渲染；横向缩放（Ctrl+滚轮，光标处为锚点）；横向平移（滚轮/滚动条）
  - 上下两条时间轴共享缩放与平移
  - 深度选中（点 10）：同一位置重复点击逐层向下，越过最底层则取消选中
  - 拖动移动（点 11.1）：整体平移，松手落到工作副本
  - 边缘对齐（点 11.2）：抓任务条两端拖拽，把该端时间吸附到 结束-开始 == 持续小时。
    绝不改动 持续小时（只有 结束-开始 ≠ 持续小时 时才有意义）。有一段起步距离：
      * 拖动 < 阈值：只画“长度虚影”预览，数据不变；
      * 拖动 ≥ 阈值：立刻吸附（只动被抓的一端，另一端与持续小时不动），没有跟手中间态；
      * 松手前拖回阈值内则回缩到原样。
    虚影两态：结束-开始 < 持续（虚影向外伸长，半透明幻影）；结束-开始 > 持续
    （虚影落在条内部会歧义，改用 原色向背景过渡 25% 的实色区分“将被裁掉”的部分）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from PySide6.QtCore import QRect, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .colors import OPACITY_NORMAL, OPACITY_SELECTED, blend_toward, with_opacity

# ---- 布局常量（任务行高度已减半：46 -> 23）----
RULER_HEIGHT = 22
BAND_TOP = RULER_HEIGHT + 5
BAND_HEIGHT = 23
WIDGET_HEIGHT = BAND_TOP + BAND_HEIGHT + 6
LEFT_PAD = 8
RIGHT_PAD = 8
BAR_TEXT_PAD = 6

# ---- 缩放范围（像素/秒）----
MIN_PPS = 0.001
MAX_PPS = 5.0
TARGET_TICK_PX = 90

# 条上任务名/标尺用字体。指定 CJK 字体族保证中文渲染；缺失时 Qt 自动回退。
BAR_FONT_FAMILY = "Microsoft YaHei UI"

# ---- 交互阈值 ----
SAME_CLICK_PX = 4          # 深度选中：判定“鼠标没动”的容差
MOVE_THRESHOLD = 4         # 超过则从点击升级为拖动移动
RECONCILE_THRESHOLD = 20   # 边缘对齐的“起步距离”x：拖过它才吸附，回到它以内则回缩
EDGE_PX = 8                # 命中任务条边缘的像素宽度（条较薄，稍放宽便于抓取）

# 边缘对齐虚影的样式
GHOST_EXTEND_OPACITY = 0.30  # 伸长态：半透明幻影
GHOST_SHRINK_BLEND = 0.25    # 收缩态：原色向背景过渡的比例

_TICK_STEPS = [
    60, 120, 300, 600, 900, 1800,
    3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600,
]

# ---- 深色主题配色 ----
COLOR_BG = QColor("#2b2d30")
COLOR_RULER_BG = QColor("#1e1f22")
COLOR_RULER_LINE = QColor("#3c3f41")
COLOR_RULER_TEXT = QColor("#9aa0a6")
COLOR_BAND_BG = QColor("#313335")
COLOR_BAR_TEXT = QColor("#ffffff")
COLOR_SELECT_BORDER = QColor("#ffffff")
COLOR_THRESHOLD = QColor("#e5c07b")  # 边缘对齐时“起步距离 x”的刻度线颜色


def _choose_step(target_seconds: float) -> int:
    for step in _TICK_STEPS:
        if step >= target_seconds:
            return step
    return _TICK_STEPS[-1]


def _round_to_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


class TimelineState(QObject):
    """两条时间轴共享的缩放/平移状态。任一改变都通过 changed 通知全部订阅者。"""

    changed = Signal()

    def __init__(self, t0: datetime, content_seconds: float):
        super().__init__()
        self.t0 = t0
        self.content_seconds = max(content_seconds, 60.0)
        self.pixels_per_second = 0.02
        self.offset_seconds = 0.0
        self.viewport_width = 900
        self._fitted = False

    # ---- 坐标映射 ----
    def sec_of(self, dt: datetime) -> float:
        return (dt - self.t0).total_seconds()

    def x_for_sec(self, sec: float) -> float:
        return LEFT_PAD + (sec - self.offset_seconds) * self.pixels_per_second

    def x_for(self, dt: datetime) -> float:
        return self.x_for_sec(self.sec_of(dt))

    def sec_for_x(self, x: float) -> float:
        return (x - LEFT_PAD) / self.pixels_per_second + self.offset_seconds

    def dt_for_x(self, x: float) -> datetime:
        return self.t0 + timedelta(seconds=self.sec_for_x(x))

    # ---- 可视范围 ----
    def _content_px(self) -> float:
        return max(self.viewport_width - LEFT_PAD - RIGHT_PAD, 1)

    def visible_seconds(self) -> float:
        return self._content_px() / self.pixels_per_second

    def max_offset(self) -> float:
        return max(0.0, self.content_seconds - self.visible_seconds())

    def _clamp_offset(self) -> None:
        self.offset_seconds = min(max(self.offset_seconds, 0.0), self.max_offset())

    # ---- 交互 ----
    def fit(self, force: bool = False) -> None:
        if self._fitted and not force:
            return
        pps = self._content_px() / self.content_seconds
        self.pixels_per_second = min(max(pps, MIN_PPS), MAX_PPS)
        self.offset_seconds = 0.0
        self._fitted = True
        self.changed.emit()

    def set_viewport_width(self, width: int) -> None:
        changed = width != self.viewport_width
        self.viewport_width = max(width, 1)
        if not self._fitted:
            self.fit()
        elif changed:
            self._clamp_offset()
            self.changed.emit()

    def zoom_at(self, x_widget: float, factor: float) -> None:
        anchor_sec = self.sec_for_x(x_widget)
        self.pixels_per_second = min(max(self.pixels_per_second * factor, MIN_PPS), MAX_PPS)
        self.offset_seconds = anchor_sec - (x_widget - LEFT_PAD) / self.pixels_per_second
        self._clamp_offset()
        self.changed.emit()

    def pan_by_px(self, dx_px: float) -> None:
        self.offset_seconds += dx_px / self.pixels_per_second
        self._clamp_offset()
        self.changed.emit()

    def set_offset_seconds(self, sec: float) -> None:
        self.offset_seconds = sec
        self._clamp_offset()
        self.changed.emit()


class TimelineWidget(QWidget):
    """
    一条时间轴。items_provider 每次绘制时实时取任务列表（“编辑中”轴反映改动）。
    选中态由窗口统一持有，用 get_selected_id 读取，点击/拖动时发相应信号。
    只有 editable=True 的时间轴（编辑中）支持拖动移动 / 边缘对齐。
    """

    barClicked = Signal(object)  # item_id 或 None
    liveEdited = Signal()        # 拖动/边缘对齐过程中或提交后，通知窗口刷新信息与问题
    editCommitted = Signal()     # 一次拖动/边缘对齐手势结束（松手），供窗口记录一步 undo
    durationEditRequested = Signal(object)  # 右键任务条：请求弹出修改持续时间小窗（item_id）

    def __init__(
        self,
        state: TimelineState,
        items_provider: Callable[[], list],
        color_map: dict[Any, QColor],
        get_selected_id: Callable[[], Optional[Any]],
        editable: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.state = state
        self.items_provider = items_provider
        self.color_map = color_map
        self.get_selected_id = get_selected_id
        self.editable = editable

        self.setFixedHeight(WIDGET_HEIGHT)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.state.changed.connect(self.update)

        self.font_bar = QFont(BAR_FONT_FAMILY, 9)
        self.font_ruler = QFont(BAR_FONT_FAMILY, 8)

        # 深度选中状态
        self._deep_index = 0
        self._last_hits: Optional[list] = None
        self._last_press_pos = None

        # 拖动 / 边缘对齐 状态机
        self._mode: Optional[str] = None      # None / 'pending' / 'move' / 'edge'
        self._press_x = 0.0
        self._target = None                   # 被移动 / 边缘对齐的 TaskItem
        self._edge: Optional[str] = None      # 'start' / 'end'
        self._orig_start: Optional[datetime] = None
        self._orig_end: Optional[datetime] = None
        self._orig_duration = 0.0
        self._edge_engaged = False            # 是否已越过起步距离、完成吸附
        # 边缘对齐的“长度虚影”：None 或 (start_dt, end_dt, shrink: bool, base: QColor)
        self._ghost: Optional[tuple] = None

    # ---- 尺寸同步 ----
    def resizeEvent(self, event) -> None:  # noqa: N802
        self.state.set_viewport_width(self.width())
        super().resizeEvent(event)

    # ---- 命中测试 ----
    def _hit_items_at(self, x: float, y: float) -> list:
        """返回命中该点的任务，按绘制顺序（后=更靠上）。"""
        if not (BAND_TOP <= y <= BAND_TOP + BAND_HEIGHT):
            return []
        hits = []
        for item in self.items_provider():
            start, end = item.start, item.end
            if start is None or end is None:
                continue
            x1, x2 = self.state.x_for(start), self.state.x_for(end)
            if x2 < x1:
                x1, x2 = x2, x1
            if x1 <= x <= x2:
                hits.append(item)
        return hits

    def _selected_item(self):
        sel = self.get_selected_id()
        for item in self.items_provider():
            if item.item_id == sel:
                return item
        return None

    def _edge_hit(self, x: float, y: float):
        """光标是否落在任意任务条的左/右边缘（顶层优先）。返回 (item, 'start'/'end') 或 (None, None)。"""
        if not (BAND_TOP <= y <= BAND_TOP + BAND_HEIGHT):
            return None, None
        found = (None, None)
        for item in self.items_provider():  # 绘制顺序，后者在上层；覆盖取顶层
            start, end = item.start, item.end
            if start is None or end is None:
                continue
            xs, xe = self.state.x_for(start), self.state.x_for(end)
            if abs(x - xs) <= EDGE_PX:
                found = (item, "start")
            elif abs(x - xe) <= EDGE_PX:
                found = (item, "end")
        return found

    # ---- 鼠标按下 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 右键任务条 -> 弹出“修改持续时间”小窗（两条时间轴都可；用右键避开深度选中连点冲突）。
        if event.button() == Qt.RightButton:
            pos = event.position()
            hits = list(reversed(self._hit_items_at(pos.x(), pos.y())))
            if hits:
                sel = self.get_selected_id()
                chosen = next((it for it in hits if it.item_id == sel), hits[0])
                self.barClicked.emit(chosen.item_id)
                self.durationEditRequested.emit(chosen.item_id)
            return
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        x, y = pos.x(), pos.y()
        self._press_x = x

        # 1) 编辑轴上、抓住任意任务条的边缘 -> 自动选中并准备边缘对齐（无需先选中）
        if self.editable:
            edge_item, edge = self._edge_hit(x, y)
            if edge_item is not None:
                if self.get_selected_id() != edge_item.item_id:
                    self.barClicked.emit(edge_item.item_id)
                self._begin_edge(edge_item, edge)
                return

        # 2) 其它情况：记录“待定”，松手判定为点击（深度选中），拖动判定为移动
        selected = self._selected_item()
        hits_top_first = list(reversed(self._hit_items_at(x, y)))
        top_item = hits_top_first[0] if hits_top_first else None
        if selected is not None and any(it.item_id == selected.item_id for it in hits_top_first):
            move_target = selected
        else:
            move_target = top_item
        self._mode = "pending"
        self._target = move_target
        super().mousePressEvent(event)

    def _begin_edge(self, item, edge: str) -> None:
        self._mode = "edge"
        self._target = item
        self._edge = edge
        self._orig_start = item.start
        self._orig_end = item.end
        self._orig_duration = item.duration_hours
        self._edge_engaged = False
        self._ghost = None
        self.setCursor(Qt.SizeHorCursor)

    # ---- 鼠标移动 ----
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        x = pos.x()

        if self._mode == "pending":
            if abs(x - self._press_x) >= MOVE_THRESHOLD and self.editable and self._target is not None:
                # 升级为移动；若目标未选中则先选中
                if self.get_selected_id() != self._target.item_id:
                    self.barClicked.emit(self._target.item_id)
                self._mode = "move"
                self._orig_start = self._target.start
                self._orig_end = self._target.end
                self._last_hits = None  # 打断深度选中序列
            else:
                self._update_hover_cursor(pos)
                return

        if self._mode == "move":
            self._do_move(x)
            return
        if self._mode == "edge":
            self._do_edge(x)
            return

        self._update_hover_cursor(pos)

    def _update_hover_cursor(self, pos) -> None:
        if self.editable:
            edge_item, _ = self._edge_hit(pos.x(), pos.y())
            if edge_item is not None:
                self.setCursor(Qt.SizeHorCursor)
                return
        self.setCursor(Qt.ArrowCursor)

    def _do_move(self, x: float) -> None:
        if self._target is None or self._orig_start is None or self._orig_end is None:
            return
        dx_seconds = (x - self._press_x) / self.state.pixels_per_second
        delta = timedelta(seconds=dx_seconds)
        self._target.set_start(_round_to_second(self._orig_start + delta))
        self._target.set_end(_round_to_second(self._orig_end + delta))
        self._target.refresh_weekday()
        self.update()
        self.liveEdited.emit()

    def _do_edge(self, x: float) -> None:
        """边缘对齐：起步距离内只画长度虚影；越过后立刻吸附到 结束-开始 == 持续小时。"""
        if self._target is None or self._orig_start is None or self._orig_end is None:
            return
        if abs(x - self._press_x) < RECONCILE_THRESHOLD:
            # 起步距离内：不真正改动，只显示长度虚影（若之前已吸附则回缩）
            if self._edge_engaged:
                self._restore_edge()
                self._edge_engaged = False
            self._ghost = self._compute_ghost()
            self.update()
            self.liveEdited.emit()
            return
        # 越过起步距离：立刻吸附（只动被抓的一端，另一端与持续小时都不动），无跟手中间态
        self._edge_engaged = True
        self._ghost = None
        self._apply_reconcile()
        self.update()
        self.liveEdited.emit()

    def _reconciled_span(self) -> tuple[datetime, datetime]:
        """按 持续小时 与被抓的另一端算出对齐后的 (start, end)。"""
        dur = timedelta(hours=self._orig_duration)
        if self._edge == "start":
            # 抓左端：保持右端不动，左端 = 右端 - 持续
            return _round_to_second(self._orig_end - dur), self._orig_end
        # 抓右端：保持左端不动，右端 = 左端 + 持续
        return self._orig_start, _round_to_second(self._orig_start + dur)

    def _compute_ghost(self) -> Optional[tuple]:
        """长度虚影 (start_dt, end_dt, shrink, base)；已一致时返回 None（无需预览）。"""
        span = (self._orig_end - self._orig_start).total_seconds()
        dur = self._orig_duration * 3600.0
        if abs(span - dur) < 1.0:
            return None  # 结束-开始 == 持续，功能无意义
        g_start, g_end = self._reconciled_span()
        base = self.color_map.get(self._target.item_id, QColor("#888888"))
        return (g_start, g_end, span > dur, base)

    def _apply_reconcile(self) -> None:
        new_start, new_end = self._reconciled_span()
        self._target.set_start(new_start)
        self._target.set_end(new_end)
        # 关键：持续小时保持不变（本操作只对齐时间，绝不改时长）
        self._target.refresh_weekday()

    def _restore_edge(self) -> None:
        self._target.set_start(self._orig_start)
        self._target.set_end(self._orig_end)
        # 持续小时全程未动，无需还原
        self._target.refresh_weekday()

    # ---- 鼠标松开 ----
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return

        if self._mode == "pending":
            # 纯点击 -> 深度选中
            self._deep_select(event.position())
        elif self._mode == "edge":
            if not self._edge_engaged:
                self._restore_edge()  # 没越过起步距离：不改动
            # 已越过则数据早已吸附，这里只需收起虚影
            self._ghost = None
            self.update()
            self.liveEdited.emit()
            self.editCommitted.emit()  # 是否真有净变化交给窗口去重
        elif self._mode == "move":
            self.liveEdited.emit()
            self.editCommitted.emit()

        self._mode = None
        self._target = None
        self._edge = None
        self._edge_engaged = False
        self._ghost = None
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _deep_select(self, pos) -> None:
        x, y = pos.x(), pos.y()
        hits = list(reversed(self._hit_items_at(x, y)))  # 顶层在前
        same_spot = (
            self._last_press_pos is not None
            and abs(x - self._last_press_pos[0]) <= SAME_CLICK_PX
            and abs(y - self._last_press_pos[1]) <= SAME_CLICK_PX
        )
        ids = [it.item_id for it in hits]
        last_ids = [it.item_id for it in (self._last_hits or [])]
        if same_spot and ids == last_ids and hits:
            self._deep_index += 1
        else:
            self._deep_index = 0
            self._last_hits = hits
        self._last_press_pos = (x, y)

        if not hits:
            self.barClicked.emit(None)
            return
        cycle = len(hits) + 1  # 多出的一格 = 取消选中
        idx = self._deep_index % cycle
        chosen = None if idx == len(hits) else hits[idx]
        self.barClicked.emit(chosen.item_id if chosen is not None else None)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if delta > 0 else 1 / 1.15
            self.state.zoom_at(event.position().x(), factor)
        else:
            self.state.pan_by_px(-delta * 0.6)
        event.accept()

    # ---- 绘制 ----
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        width = self.width()
        painter.fillRect(0, 0, width, WIDGET_HEIGHT, COLOR_BG)
        painter.fillRect(0, BAND_TOP, width, BAND_HEIGHT, COLOR_BAND_BG)
        self._paint_ruler(painter, width)
        self._paint_bars(painter, width)
        self._paint_ghost(painter, width)
        self._paint_threshold_ticks(painter, width)
        painter.end()

    def _paint_ruler(self, painter: QPainter, width: int) -> None:
        painter.fillRect(0, 0, width, RULER_HEIGHT, COLOR_RULER_BG)
        painter.setFont(self.font_ruler)
        fm = QFontMetrics(self.font_ruler)
        step = _choose_step(TARGET_TICK_PX / self.state.pixels_per_second)
        t0 = self.state.t0
        midnight = t0.replace(hour=0, minute=0, second=0, microsecond=0)
        anchor_sec = (midnight - t0).total_seconds()
        left_sec = self.state.sec_for_x(0)
        right_sec = self.state.sec_for_x(width)
        k = int((left_sec - anchor_sec) // step)
        pen_line = QPen(COLOR_RULER_LINE)
        while True:
            tick_sec = anchor_sec + k * step
            if tick_sec > right_sec + step:
                break
            x = self.state.x_for_sec(tick_sec)
            k += 1
            if x < -50 or x > width + 50:
                continue
            painter.setPen(pen_line)
            painter.drawLine(int(x), 0, int(x), WIDGET_HEIGHT)
            tick_dt = t0 + timedelta(seconds=tick_sec)
            label = tick_dt.strftime("%m-%d") if (tick_dt.hour == 0 and tick_dt.minute == 0) else tick_dt.strftime("%H:%M")
            painter.setPen(COLOR_RULER_TEXT)
            painter.drawText(int(x) + 3, fm.ascent() + 2, label)

    def _paint_bars(self, painter: QPainter, width: int) -> None:
        painter.setFont(self.font_bar)
        fm = QFontMetrics(self.font_bar)
        selected_id = self.get_selected_id()
        items = self.items_provider()
        ordered = [it for it in items if it.item_id != selected_id]
        ordered += [it for it in items if it.item_id == selected_id]  # 选中的浮到最上

        for item in ordered:
            start, end = item.start, item.end
            if start is None or end is None:
                continue
            x1, x2 = self.state.x_for(start), self.state.x_for(end)
            if x2 < x1:
                x1, x2 = x2, x1
            if x2 < 0 or x1 > width:
                continue
            bar_w = max(x2 - x1, 2.0)
            is_selected = item.item_id == selected_id
            base = self.color_map.get(item.item_id, QColor("#888888"))
            opacity = OPACITY_SELECTED if is_selected else OPACITY_NORMAL
            painter.fillRect(QRect(int(x1), BAND_TOP, int(bar_w), BAND_HEIGHT), with_opacity(base, opacity))
            painter.setPen(QPen(COLOR_SELECT_BORDER, 2) if is_selected else QPen(base.darker(140), 1))
            painter.drawRect(int(x1), BAND_TOP, int(bar_w), BAND_HEIGHT - 1)

            text_rect = QRect(int(x1) + BAR_TEXT_PAD, BAND_TOP, int(bar_w) - 2 * BAR_TEXT_PAD, BAND_HEIGHT)
            if text_rect.width() > 8:
                elided = fm.elidedText(item.name, Qt.ElideRight, text_rect.width())
                painter.setPen(COLOR_BAR_TEXT)
                painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_ghost(self, painter: QPainter, width: int) -> None:
        """边缘对齐的长度虚影：预览吸附后的条（长度 == 持续小时）。"""
        if self._ghost is None:
            return
        g_start, g_end, shrink, base = self._ghost
        x1, x2 = self.state.x_for(g_start), self.state.x_for(g_end)
        if x2 < x1:
            x1, x2 = x2, x1
        if x2 < 0 or x1 > width:
            return
        bar_w = max(x2 - x1, 2.0)
        if shrink:
            # 结束-开始 > 持续：虚影落在条内部，用 原色向背景过渡 25% 的实色区分
            fill = blend_toward(base, COLOR_BG, GHOST_SHRINK_BLEND)
        else:
            # 结束-开始 < 持续：虚影向外伸长，半透明幻影
            fill = with_opacity(base, GHOST_EXTEND_OPACITY)
        painter.fillRect(QRect(int(x1), BAND_TOP, int(bar_w), BAND_HEIGHT), fill)
        painter.setPen(QPen(base, 1, Qt.DashLine))
        painter.drawRect(int(x1), BAND_TOP, int(bar_w), BAND_HEIGHT - 1)

    def _paint_threshold_ticks(self, painter: QPainter, width: int) -> None:
        """边缘对齐拖拽中：把“起步距离 x”的两侧临界点位刻度画到时间轴上。"""
        if self._mode != "edge":
            return
        for xt in (self._press_x - RECONCILE_THRESHOLD, self._press_x + RECONCILE_THRESHOLD):
            if xt < 0 or xt > width:
                continue
            painter.setPen(QPen(COLOR_THRESHOLD, 1, Qt.DashLine))
            painter.drawLine(int(xt), 0, int(xt), WIDGET_HEIGHT)
            painter.setPen(QPen(COLOR_THRESHOLD, 2))  # 顶部实心刻度帽，突出临界点
            painter.drawLine(int(xt), 0, int(xt), 6)


def compute_state(all_items: list, pad_seconds: float = 1800.0) -> TimelineState:
    """t0 = 最早开始 - pad，content = 到 最晚结束 + pad。"""
    starts = [it.start for it in all_items if it.start is not None]
    ends = [it.end for it in all_items if it.end is not None]
    if starts and ends:
        t_min, t_max = min(starts), max(ends)
    elif starts:
        t_min = min(starts)
        t_max = t_min + timedelta(hours=1)
    else:
        now = datetime.now().astimezone()
        t_min, t_max = now, now + timedelta(hours=1)
    t0 = t_min - timedelta(seconds=pad_seconds)
    content_seconds = (t_max - t0).total_seconds() + pad_seconds
    return TimelineState(t0=t0, content_seconds=content_seconds)
