"""
任务配色。

需求点 5：不同任务颜色不同、颜色不重复、透明度固定 0.75（选中时 0.9）。
同一任务在“编辑前 / 编辑中”两条时间轴上必须是同一颜色，所以按 item_id 稳定分配。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor

# 透明度：普通 0.75、选中 0.9（点 5 / 点 10）。
OPACITY_NORMAL = 0.75
OPACITY_SELECTED = 0.90


def build_color_map(item_ids: list[Any]) -> dict[Any, QColor]:
    """
    为每个 item_id 分配一个互不相同的基础色（不含透明度）。

    在 HSV 色环上均匀取色，保证任意数量任务之间的色相间隔最大化、易区分。
    """
    unique_ids = list(dict.fromkeys(item_ids))  # 去重且保持顺序
    total = max(len(unique_ids), 1)
    color_map: dict[Any, QColor] = {}
    for index, item_id in enumerate(unique_ids):
        hue = (index / total) % 1.0
        # 饱和度/明度偏中性，适配深色背景，条上白字可读。
        color_map[item_id] = QColor.fromHsvF(hue, 0.58, 0.85)
    return color_map


def with_opacity(color: QColor, opacity: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(opacity)
    return out


def blend_toward(color: QColor, target: QColor, t: float) -> QColor:
    """color 向 target 线性过渡 t（0~1）后的不透明颜色。t=0 原色、t=1 完全变成 target。"""
    t = max(0.0, min(1.0, t))
    r = round(color.red() + (target.red() - color.red()) * t)
    g = round(color.green() + (target.green() - color.green()) * t)
    b = round(color.blue() + (target.blue() - color.blue()) * t)
    return QColor(r, g, b)
