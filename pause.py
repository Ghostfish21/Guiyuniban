"""
log pause / log resume —— 强制暂停子系统。

暂停不是任务，是任务时间轴上的一个**空档**：这段时间里什么都没做，结算时从当时
正在进行的任务里扣掉，不产出任何真实任务。

与 costart 共用同一份 uncommit_tasks.txt，但 `type` 是 "pause_gap"：全项目所有读取
该文件的地方都带 `type == "session"` 过滤，所以暂停记录对它们完全不可见。同样不带
session_id / task_group_id / committed，误伤面为零。

关键设计：暂停窗口是**阻塞**的（全屏置顶，只能点 resume 退出），所以暂停期间同一个
终端里发不出任何 log 命令。这条性质让暂停区间天然原子——不会出现「暂停到一半又
costart 了一个任务」这种分叉，扣减只需要认准一个归属节点。

归属节点 = `log pause` 那一刻当前路径上最内层仍在进行的任务（可能是 costart 任务），
没有并发任务时就是顶层真实任务本身。**只从这一层扣**：costart 的结算模型里父任务
算的是净时长（已经扣掉了子任务的整个窗口，而暂停就在那个窗口里），再给父任务扣一次
会把同一段时间扣掉两遍。见 costart.settle_on_end。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from typing import Any, Optional

from costart import (
    CO_SHIFT_KEY,
    RICH_AVAILABLE,
    _co_id,
    _render_error,
    _render_info,
    _render_panel,
    active_co_tasks,
    console,
    shift_of,
)
from end import (
    _format_duration_seconds,
    _parse_dt,
    find_active_session,
    now_iso,
    read_txt_records,
    write_txt_records,
)
from start import append_txt_record

if RICH_AVAILABLE:
    from rich.table import Table


# 暂停记录的 type 值；和 costart 的 CO_TYPE 一样，这一个字符串就是整道隔离墙。
PAUSE_TYPE = "pause_gap"


# --------------------------------------------------------------------------- #
# 记录访问
# --------------------------------------------------------------------------- #
def _pause_id(record: dict[str, Any]) -> str:
    return str(record.get("pause_id") or "")


def live_pauses(records: list[dict[str, Any]], root_session_id: str) -> list[dict[str, Any]]:
    """属于当前顶层真实任务、尚未被结算掉的暂停记录，按开始时间排序。"""
    return sorted(
        (
            record
            for record in records
            if record.get("type") == PAUSE_TYPE
            and str(record.get("root_session_id") or "") == root_session_id
        ),
        key=lambda record: str(record.get("start_time") or ""),
    )


def active_pause(records: list[dict[str, Any]], root_session_id: str) -> Optional[dict[str, Any]]:
    """
    仍未 resume 的暂停记录。

    正常情况下最多只有一条：窗口阻塞期间发不出第二条 log pause。多于一条时取最早的
    那条继续收口，剩下的下一次 log pause 再处理，总之不会静默丢掉。
    """
    for record in live_pauses(records, root_session_id):
        if record.get("end_time") is None:
            return record
    return None


def elapsed_seconds(record: dict[str, Any], reference: datetime) -> int:
    start = _parse_dt(record.get("start_time"))
    end = _parse_dt(record.get("end_time")) or reference
    if start is None:
        return 0
    return max(0, int((end - start).total_seconds()))


# --------------------------------------------------------------------------- #
# 供 costart.settle_on_end 使用
# --------------------------------------------------------------------------- #
def gap_windows(
    records: list[dict[str, Any]],
    root_session_id: str,
) -> dict[str, list[tuple[datetime, datetime]]]:
    """
    把暂停记录整理成 {归属节点 id: [(起, 止), ...]}，供结算时按层扣减。

    只收已经 resume 的记录；未结束的暂停由调用方先行拦截（见 settle_on_end），
    因为「暂停到一半就结束任务」的语义不明确，宁可报错让用户先 log resume。
    """
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for record in live_pauses(records, root_session_id):
        start = _parse_dt(record.get("start_time"))
        end = _parse_dt(record.get("end_time"))
        if start is None or end is None:
            continue
        windows.setdefault(str(record.get("parent_id") or ""), []).append((start, max(start, end)))
    return windows


# --------------------------------------------------------------------------- #
# log pause
# --------------------------------------------------------------------------- #
def _parent_of_new_pause(
    records: list[dict[str, Any]],
    root: dict[str, Any],
    root_id: str,
) -> tuple[str, str, dict[str, Any]]:
    """返回 (归属节点 id, 归属节点名, 归属节点记录)。"""
    chain = active_co_tasks(records, root_id)
    if chain:
        node = chain[-1]
        return _co_id(node), str(node.get("task_name") or "未命名任务"), node
    name = str(root.get("canonical_task_name") or root.get("task_name") or "未命名任务")
    return root_id, name, root


def _open_pause_window(rows: dict[str, Any]) -> bool:
    """
    弹出全屏暂停窗口，阻塞到用户点 resume。返回 True 表示正常 resume。

    PySide6 不可用时退回终端等待回车；非交互环境下直接返回 False，由调用方提示
    用 log resume 收口。
    """
    try:
        from taskeditor.ui.pausewindow import run_pause_window
    except ImportError:
        return _wait_in_terminal(rows)

    return run_pause_window(rows)


def _wait_in_terminal(rows: dict[str, Any]) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        _render_error(
            "无法打开暂停窗口",
            "缺少 PySide6，当前又不是交互终端，没法等待 resume。\n"
            "暂停已记录；结束暂停请在交互终端里执行：log resume\n"
            "安装图形界面：python -m pip install PySide6-Essentials",
        )
        return False

    message = f"已暂停：{rows.get('task_name')}　（{rows.get('start_time')} 开始）"
    if RICH_AVAILABLE:
        console.print(f"[bold yellow]{message}[/bold yellow]")
        try:
            console.input("[dim]按回车 resume……[/dim]")
        except (EOFError, KeyboardInterrupt):
            return False
    else:
        print(message)
        try:
            input("按回车 resume……")
        except (EOFError, KeyboardInterrupt):
            return False
    return True


def pause_task(context: dict[str, str]) -> int:
    """
    处理:
      log pause

    暂停当前正在进行的任务（含所有层的并发任务），弹出全屏置顶窗口，点 resume 结束。
    这段时间会在 log end 时从归属任务的时长里扣掉。
    """
    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is None:
        _render_error(
            "没有正在进行的任务",
            "log pause 只能在已有任务进行时使用。请先 log start 任务名，再 log pause。",
        )
        return 1

    root = records[active_index]
    root_id = str(root.get("session_id") or "")
    if not root_id:
        _render_error("当前任务缺少 session_id", "无法记录暂停；请检查 uncommit_tasks.txt。")
        return 1

    existing = active_pause(records, root_id)
    if existing is not None:
        # 上一次暂停没收口（窗口被强杀 / 进程崩了）。同一段暂停继续，不新建记录，
        # 否则那段时间会被记两遍。
        record = existing
        reference = _parse_dt(now_iso(context)) or datetime.now()
        already = elapsed_seconds(record, reference + shift_of(record))
        _render_info(
            "继续上一次未结束的暂停",
            f"检测到 {record.get('start_time')} 开始、尚未 resume 的暂停，"
            f"已进行 {_format_duration_seconds(already)}。\n本次直接接着它计时，不会重复记录。",
        )
    else:
        parent_id, parent_name, parent = _parent_of_new_pause(records, root, root_id)
        # 归属节点被并发结算整体后移过时（co_shift_seconds），暂停的起止也要加上同样的
        # 偏移，否则这段区间会落在记录时间轴上的任务窗口之外，扣减静默失效。
        shift = shift_of(parent)
        now = _parse_dt(now_iso(context))
        assert now is not None
        start_time = (now + shift).isoformat(timespec="seconds")
        record = {
            "type": PAUSE_TYPE,
            "pause_id": str(uuid.uuid4()),
            "parent_id": parent_id,
            "parent_name": parent_name,
            "root_session_id": root_id,
            "start_time": start_time,
            "end_time": None,
            "created_at": now_iso(context),
            "updated_at": now_iso(context),
        }
        if shift:
            record[CO_SHIFT_KEY] = int(shift.total_seconds())
        append_txt_record(context["uncommit_file"], record)
        already = 0

    rows = {
        "task_name": str(record.get("parent_name") or "当前任务"),
        "start_time": str(record.get("start_time") or ""),
        "elapsed_seconds": already,
    }
    resumed = _open_pause_window(rows)
    if not resumed:
        _render_error(
            "暂停未收口",
            "暂停窗口没有正常 resume，结束时间还没写入。\n"
            "请执行 log resume 结束这次暂停；在此之前 log end 会拒绝结算。",
        )
        return 1

    return _close_active_pause(context, title="暂停已结束")


# --------------------------------------------------------------------------- #
# log resume
# --------------------------------------------------------------------------- #
def _close_active_pause(context: dict[str, str], title: str) -> int:
    """给当前未结束的暂停写上 end_time。重新读盘，避免覆盖别处已经写好的收口。"""
    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is None:
        _render_error("没有正在进行的任务", "找不到暂停所依附的任务，无法收口。")
        return 1

    root = records[active_index]
    root_id = str(root.get("session_id") or "")
    target = active_pause(records, root_id)
    if target is None:
        _render_info("没有进行中的暂停", "当前没有需要 resume 的暂停。")
        return 0

    # 起点加过偏移的，终点也要加同样的秒数：时长才是真实的墙上时钟时长。
    now = _parse_dt(now_iso(context))
    assert now is not None
    end_time = (now + shift_of(target)).isoformat(timespec="seconds")
    start = _parse_dt(target.get("start_time"))
    clamped = False
    if start is not None and _parse_dt(end_time) < start:  # type: ignore[operator]
        # 补过偏移之后正常到不了这里；留着兜被手工改过的脏记录。
        end_time = str(target.get("start_time"))
        clamped = True

    target["end_time"] = end_time
    target["updated_at"] = now_iso(context)
    write_txt_records(context["uncommit_file"], records)

    reference = _parse_dt(end_time) or datetime.now()
    duration = elapsed_seconds(target, reference)
    table = Table.grid(padding=(0, 2)) if RICH_AVAILABLE else None
    if table is not None:
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="white")
        table.add_row("暂停归属", str(target.get("parent_name") or "当前任务"))
        table.add_row("开始时间", str(target.get("start_time") or ""))
        table.add_row("结束时间", end_time)
        table.add_row("暂停时长", _format_duration_seconds(duration))
    note = "这段时间会在 log end 时从归属任务的时长里扣掉，不产出任何任务。"
    if clamped:
        note += "\n注意：这条记录的开始时间晚于结束时间（可能被手工改过），本次按 0 时长收口。"
    _render_panel(title, table, note, "yellow")
    return 0


def resume_task(context: dict[str, str]) -> int:
    """
    处理:
      log resume

    命令行收口：暂停窗口被强杀、或在另一个终端里想结束暂停时用它。
    正常流程下 log pause 的窗口点 resume 就已经收口了，不需要再执行这条。
    """
    return _close_active_pause(context, title="暂停已结束（log resume）")
