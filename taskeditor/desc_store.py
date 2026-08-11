"""
`log desc` 的数据层：commit **之前** 的任务描述。

和 `log check` / `log edit` 不同，这里操作的不是 committed 池（commit_preview.txt），
而是 uncommit_tasks.txt 里还没 commit 的 session 记录：

    - 描述写在 session 记录的 `详细描述` 字段上；
    - `log commit` 时由 summary._build_base_commit_items 收集为
      committed item 的 `详细描述`（session_id -> 文本 映射），随任务进入池中；
    - 因此在 commit 前写好描述，commit / push 都不需要再补。

写回时只改 `详细描述` 与 `updated_at`，其余字段（含已 commit 的历史记录）原样保留。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

# 复用 summary.py 已验证的读写 / 解析 / 周几逻辑，保持单一事实来源。
from summary import (
    _effective_weekday,
    _parse_dt,
    read_txt_records,
    write_txt_records,
)

# session 记录里描述字段的主键名与兼容别名
DESCRIPTION_KEY = "详细描述"
DESCRIPTION_ALIAS = "detailed_description"


class DescLoadError(Exception):
    """读取 uncommit_tasks.txt 失败，message 面向用户。"""


class SessionItem:
    """
    包装 uncommit_tasks.txt 中的一条未 commit session。

    始终持有原始 dict（`raw`），改动直接落在 raw 上，落盘时整份记录原样写回。
    """

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    # ---- 标识 ----
    @property
    def session_id(self) -> str:
        return str(self.raw.get("session_id") or "")

    @property
    def task_group_id(self) -> str:
        return str(self.raw.get("task_group_id") or "")

    @property
    def command(self) -> str:
        return str(self.raw.get("command") or "")

    # ---- 任务名 ----
    @property
    def name(self) -> str:
        """展示用任务名：优先 commit 时会采用的 canonical 名。"""
        return str(
            self.raw.get("canonical_task_name")
            or self.raw.get("task_name")
            or "未命名任务"
        )

    @property
    def typed_name(self) -> str:
        """本次实际输入的任务名（cont 时可能与 canonical 不同）。"""
        return str(self.raw.get("task_name") or "")

    # ---- 时间 ----
    @property
    def start(self) -> Optional[datetime]:
        return _parse_dt(self.raw.get("start_time"))

    @property
    def end(self) -> Optional[datetime]:
        return _parse_dt(self.raw.get("end_time"))

    @property
    def start_iso(self) -> str:
        return str(self.raw.get("start_time") or "")

    @property
    def running(self) -> bool:
        """还没 log end 的进行中 session。"""
        return self.end is None

    @property
    def weekday(self) -> str:
        start = self.start
        return _effective_weekday(start) if start is not None else "未知"

    @property
    def duration_hours(self) -> Optional[float]:
        """结束-开始 折算的小时数；进行中或时间非法时返回 None。"""
        start, end = self.start, self.end
        if start is None or end is None or end < start:
            return None
        return round((end - start).total_seconds() / 3600, 2)

    # ---- 描述 ----
    @property
    def description(self) -> str:
        value = self.raw.get(DESCRIPTION_KEY)
        if value is None:
            value = self.raw.get(DESCRIPTION_ALIAS)
        return str(value or "")

    @description.setter
    def description(self, value: str) -> None:
        text = str(value or "")
        # 只保留主键名；空描述直接删字段，不留空串脏数据。
        self.raw.pop(DESCRIPTION_ALIAS, None)
        if text.strip():
            self.raw[DESCRIPTION_KEY] = text
        else:
            self.raw.pop(DESCRIPTION_KEY, None)


class DescData:
    """一次 `log desc` 的整体：全部记录（写回用）+ 未 commit 的 session 列表。"""

    def __init__(self, records: list[dict[str, Any]], sessions: list[SessionItem]):
        self.records = records
        self.sessions = sessions

    def snapshot(self) -> dict[str, str]:
        """当前各 session 的描述，用于判断用户是否真的改了东西。"""
        return {item.session_id: item.description for item in self.sessions}


def _is_uncommitted_session(record: dict[str, Any]) -> bool:
    return record.get("type") == "session" and record.get("committed") is False


def load_desc_data(context: dict[str, str]) -> DescData:
    """
    读取 uncommit_tasks.txt，返回全部记录 + 未 commit 的 session（按开始时间排序）。

    失败时抛 DescLoadError（message 面向用户）。
    """
    uncommit_file = context.get("uncommit_file") or ""
    if not uncommit_file:
        raise DescLoadError("运行上下文缺少 uncommit_file 路径。")

    try:
        records = read_txt_records(uncommit_file)
    except ValueError as exc:
        raise DescLoadError(str(exc)) from exc

    sessions = [SessionItem(raw) for raw in records if _is_uncommitted_session(raw)]
    # 进行中的 session 没有结束时间，按开始时间排即可
    sessions.sort(key=lambda item: (item.start_iso, item.name))
    return DescData(records=records, sessions=sessions)


def save_desc_data(context: dict[str, str], data: DescData, changed_ids: set[str]) -> None:
    """
    把描述改动写回 uncommit_tasks.txt。

    - 整份记录原样写回（含已 commit 的历史行），只有被改过的 session 刷新 updated_at
    - 不动 task_index / committed / 起止时间等任何其他字段
    """
    if changed_ids:
        now = datetime.now().isoformat(timespec="seconds")
        for item in data.sessions:
            if item.session_id in changed_ids:
                item.raw["updated_at"] = now

    write_txt_records(context["uncommit_file"], data.records)
