"""
committed 数据的读写与任务模型。

本工具里“committed 数据”= commit_preview.txt 中 JSON payload 的 items，
也就是 `log commit` 生成、`log push` 消费的那批任务。`log edit` 只把改动写回这里，
不动 uncommit_tasks.txt / task_index.txt，规则与 `log chat` 落盘保持一致：
    - commit_id 不变
    - generated_at 刷新为当前时间
    - 编号绝不重新分配
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 复用 summary.py 里已经过验证的序列化 / 解析 / 周几逻辑，避免重复实现。
from summary import (
    _build_commit_preview_text,
    _effective_weekday,
    _extract_commit_payload,
    _parse_dt,
)


class CommitLoadError(Exception):
    """读取 commit_preview.txt 失败，message 面向用户。"""


def _to_iso(dt: datetime) -> str:
    """保留时区的 ISO 8601，与 commit 生成时的格式一致。"""
    return dt.isoformat()


class TaskItem:
    """
    包装 commit_preview payload 中的一条任务。

    - 始终持有原始 dict（`raw`），保留 task_group_id / source_session_ids /
      session_names / source_key 等元数据，落盘时原样带回。
    - 中文字段是权威字段；同时同步维护英文 start_time / end_time，避免
      `_build_commit_preview_text` 生成时中英不一致。
    """

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    # ---- 标识 ----
    @property
    def item_id(self) -> Any:
        return self.raw.get("编号")

    @property
    def id_text(self) -> str:
        value = self.raw.get("编号")
        return "" if value is None else str(value)

    # ---- 任务名 ----
    @property
    def name(self) -> str:
        return str(self.raw.get("任务名") or "")

    @name.setter
    def name(self, value: str) -> None:
        self.raw["任务名"] = value

    # ---- 类别 ----
    @property
    def category(self) -> str:
        return str(self.raw.get("类别") or "")

    @category.setter
    def category(self, value: str) -> None:
        self.raw["类别"] = value

    # ---- 周几（由开始时间派生，复用 summary 的边界规则）----
    @property
    def weekday(self) -> str:
        return str(self.raw.get("周几") or "")

    def refresh_weekday(self) -> None:
        start = self.start
        if start is not None:
            self.raw["周几"] = _effective_weekday(start)

    # ---- 持续小时 ----
    @property
    def duration_hours(self) -> float:
        try:
            return float(self.raw.get("持续小时") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @duration_hours.setter
    def duration_hours(self, value: float) -> None:
        self.raw["持续小时"] = round(float(value), 4)

    # ---- 开始 / 结束时间 ----
    @property
    def start_iso(self) -> str:
        return str(self.raw.get("开始时间") or self.raw.get("start_time") or "")

    @property
    def end_iso(self) -> str:
        return str(self.raw.get("结束时间") or self.raw.get("end_time") or "")

    @property
    def start(self) -> Optional[datetime]:
        return _parse_dt(self.start_iso or None)

    @property
    def end(self) -> Optional[datetime]:
        return _parse_dt(self.end_iso or None)

    def set_start(self, dt: datetime) -> None:
        iso = _to_iso(dt)
        self.raw["开始时间"] = iso
        self.raw["start_time"] = iso

    def set_end(self, dt: datetime) -> None:
        iso = _to_iso(dt)
        self.raw["结束时间"] = iso
        self.raw["end_time"] = iso

    def set_span(self, start: datetime, end: datetime) -> None:
        """同时更新起止时间并刷新周几；不改变持续小时。"""
        self.set_start(start)
        self.set_end(end)
        self.refresh_weekday()

    @property
    def measured_hours(self) -> Optional[float]:
        """结束-开始 折算的小时数；无法解析时返回 None。"""
        start, end = self.start, self.end
        if start is None or end is None:
            return None
        return round((end - start).total_seconds() / 3600, 2)

    def clone(self) -> "TaskItem":
        return TaskItem(copy.deepcopy(self.raw))


@dataclass
class CommitData:
    """一次 commit 预览的整体：payload（含 commit_id / generated_at）+ 任务列表。"""

    payload: dict[str, Any]
    items: list[TaskItem]

    @property
    def commit_id(self) -> Any:
        return self.payload.get("commit_id")

    @property
    def generated_at(self) -> Any:
        return self.payload.get("generated_at")

    def clone_items(self) -> list[TaskItem]:
        """深拷贝一份任务列表，用于“编辑中”的工作副本。"""
        return [item.clone() for item in self.items]


def load_commit_data(context: dict[str, str]) -> CommitData:
    """
    读取 commit_preview.txt，返回 CommitData。

    失败时抛 CommitLoadError（message 面向用户），与 `log push` 的报错语义对齐。
    """
    preview_path = Path(context.get("commit_preview_file") or "")
    if not preview_path.exists():
        raise CommitLoadError(
            f"未找到 {preview_path}。请先运行 log commit 生成预览。"
        )

    text = preview_path.read_text(encoding="utf-8")
    if not text.strip():
        raise CommitLoadError("commit 预览为空。请先运行 log commit 生成预览。")

    payload = _extract_commit_payload(text)
    if not payload:
        raise CommitLoadError(
            "无法从 commit_preview.txt 解析机器可读 payload。请重新运行 log commit。"
        )

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise CommitLoadError("当前预览中没有任务，无需 check / edit。")

    items = [TaskItem(raw) for raw in raw_items if isinstance(raw, dict)]
    return CommitData(payload=payload, items=items)


def save_commit_data(context: dict[str, str], data: CommitData) -> None:
    """
    把改动写回 commit_preview.txt。

    - 保留 commit_id
    - 刷新 generated_at
    - 保留每条任务的编号与全部元数据
    """
    raw_items = [item.raw for item in data.items]
    data.payload["items"] = raw_items
    data.payload["generated_at"] = datetime.now().isoformat(timespec="seconds")

    text = _build_commit_preview_text(raw_items, data.payload)
    preview_path = Path(context["commit_preview_file"])
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(text, encoding="utf-8")
