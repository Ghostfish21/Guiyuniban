"""
log status —— committed 池 + 未 commit 任务与本天时间的统计。

统计口径：
- 数据源 = commit_preview.txt 的 committed 池 items + uncommit_tasks.txt 中
  已结束但未 commit 的 session（后者用 summary._build_base_commit_items 按
  task_group + 周几 聚合，与 log commit 的分组口径一致，类别记为「未分类」）；
- committed 任务的用时取校准后的「持续小时」字段，按天分组直接看「周几」记录，
  不重新用开始/结束时间计算；
- 「本天」= 当前时刻按 07:00 日界归属的周几；本天时间 =
  committed 本天总和 + 未 commit 本天总和 + 当前进行中任务（若存在）的已持续时间。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from rich.console import Console
    from rich.padding import Padding
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    console = Console()
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - 只在未安装 rich 的环境中触发
    console = None
    RICH_AVAILABLE = False

# 复用 summary.py 的池读取 / 周几边界 / 分组 / 格式化逻辑，保持单一事实来源。
from summary import (
    DAY_BOUNDARY,
    WEEKDAYS,
    _build_base_commit_items,
    _effective_weekday,
    _format_hours,
    _load_existing_pool_items,
    _parse_dt,
    read_txt_records,
)


# --------------------------------------------------------------------------- #
# 字段读取与聚合
# --------------------------------------------------------------------------- #
def _item_hours(item: dict[str, Any]) -> float:
    try:
        return float(item.get("持续小时") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _item_category(item: dict[str, Any]) -> str:
    return str(item.get("类别") or "未分类")


def _item_weekday(item: dict[str, Any]) -> str:
    return str(item.get("周几") or "未知")


def _aggregate(items: list[dict[str, Any]]) -> tuple[float, int]:
    """返回 (总用时小时, 项数)。"""
    return sum(_item_hours(item) for item in items), len(items)


def _group_by_category(items: list[dict[str, Any]]) -> list[tuple[str, float, int]]:
    """按类别聚合，返回 [(类别, 用时, 项数)]，用时降序。"""
    buckets: dict[str, tuple[float, int]] = {}
    for item in items:
        category = _item_category(item)
        hours, count = buckets.get(category, (0.0, 0))
        buckets[category] = (hours + _item_hours(item), count + 1)
    return sorted(
        ((category, hours, count) for category, (hours, count) in buckets.items()),
        key=lambda row: row[1],
        reverse=True,
    )


def _group_by_weekday(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """按「周几」字段分组，按周一→周日排序，未知的排最后。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(_item_weekday(item), []).append(item)

    def _order(weekday: str) -> int:
        return WEEKDAYS.index(weekday) if weekday in WEEKDAYS else len(WEEKDAYS)

    return sorted(buckets.items(), key=lambda pair: _order(pair[0]))


def _fmt_hm(hours: float) -> str:
    """把小时数格式化成「X 小时 Y 分钟」。"""
    total_minutes = int(round(max(0.0, hours) * 60))
    whole_hours, minutes = divmod(total_minutes, 60)
    if whole_hours and minutes:
        return f"{whole_hours} 小时 {minutes} 分钟"
    if whole_hours:
        return f"{whole_hours} 小时"
    return f"{minutes} 分钟"


# --------------------------------------------------------------------------- #
# 本天：committed 归属 + 进行中任务
# --------------------------------------------------------------------------- #
def _effective_date(dt: datetime) -> str:
    """dt 按 07:00 日界归属的 log 日期（ISO），与周几归属规则一致。"""
    effective = dt
    if dt.timetz().replace(tzinfo=None) <= DAY_BOUNDARY:
        effective = dt - timedelta(days=1)
    return effective.date().isoformat()


def _find_active_session(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(records):
        if (
            record.get("type") == "session"
            and record.get("end_time") is None
            and record.get("committed") is False
        ):
            return record
    return None


def _active_task_info(records: list[dict[str, Any]]) -> tuple[str, datetime, float] | None:
    """返回进行中任务的 (任务名, 开始时间, 已持续小时)；没有则返回 None。"""
    session = _find_active_session(records)
    if session is None:
        return None

    start = _parse_dt(session.get("start_time"))
    if start is None:
        return None

    now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
    elapsed_hours = max(0.0, (now - start).total_seconds() / 3600)
    name = str(session.get("canonical_task_name") or session.get("task_name") or "未命名任务")
    return name, start, elapsed_hours


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _hours_cell(hours: float) -> str:
    return f"{_fmt_hm(hours)}（{_format_hours(hours)} 小时）"


def _category_table(rows: list[tuple[str, float, int]], total_hours: float, *, show_percent: bool) -> "Table":
    table = Table(
        show_header=True,
        header_style="bold bright_cyan",
        show_edge=False,
        box=None,
        pad_edge=False,
    )
    table.add_column("类别", style="bright_magenta", no_wrap=True)
    table.add_column("项数", justify="right")
    table.add_column("用时")
    if show_percent:
        table.add_column("占比", justify="right", style="dim")

    for category, hours, count in rows:
        cells = [category, f"{count} 项", _hours_cell(hours)]
        if show_percent:
            percent = (hours / total_hours * 100) if total_hours > 0 else 0.0
            cells.append(f"{percent:.1f}%")
        table.add_row(*cells)
    return table


def _print_section(title: str, icon: str) -> None:
    console.print(Rule(Text(f"{icon} {title}", style="bold bright_cyan"), style="bright_black"))


def _render_rich(
    committed_items: list[dict[str, Any]],
    uncommit_items: list[dict[str, Any]],
    active: tuple[str, datetime, float] | None,
    today_weekday: str,
    today_date: str,
) -> None:
    items = committed_items + uncommit_items
    total_hours, total_count = _aggregate(items)
    committed_hours, committed_count = _aggregate(committed_items)
    uncommit_hours, uncommit_count = _aggregate(uncommit_items)

    # 1. 总览
    _print_section("总览 · committed + 未 commit", "📊")
    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="dim", no_wrap=True)
    overview.add_column()
    overview.add_row(
        "任务总数",
        f"{total_count} 项 = committed {committed_count} 项 + 未 commit {uncommit_count} 项",
    )
    overview.add_row(
        "总用时",
        f"{_hours_cell(total_hours)} = committed {_fmt_hm(committed_hours)} + 未 commit {_fmt_hm(uncommit_hours)}",
    )
    console.print(Padding(overview, (0, 0, 0, 2)))

    if not items:
        console.print(Padding(Text("没有任何已结束的任务记录。", style="dim"), (0, 0, 0, 2)))

    if items:
        # 2. 类别统计
        _print_section("类别统计", "🗂")
        console.print(
            Padding(_category_table(_group_by_category(items), total_hours, show_percent=True), (0, 0, 0, 2))
        )

        # 3. 按天统计（按每条任务的「周几」记录分组）
        _print_section("按天统计", "📅")
        for weekday, day_items in _group_by_weekday(items):
            day_hours, day_count = _aggregate(day_items)
            header = Text()
            header.append(f"{weekday}", style="bold bright_magenta")
            header.append(f"  ·  {day_count} 项  ·  {_hours_cell(day_hours)}", style="bold")
            console.print(Padding(header, (0, 0, 0, 2)))
            console.print(
                Padding(_category_table(_group_by_category(day_items), day_hours, show_percent=False), (0, 0, 0, 4))
            )

    # 4. 本天时间统计
    today_committed_hours, today_committed_count = _aggregate(
        [item for item in committed_items if _item_weekday(item) == today_weekday]
    )
    today_uncommit_hours, today_uncommit_count = _aggregate(
        [item for item in uncommit_items if _item_weekday(item) == today_weekday]
    )
    active_hours = active[2] if active else 0.0

    _print_section(f"本天 · {today_weekday}（log 日 {today_date}）", "⏱")
    today = Table.grid(padding=(0, 2))
    today.add_column(style="dim", no_wrap=True)
    today.add_column()
    today.add_row("已 committed", f"{_hours_cell(today_committed_hours)} · {today_committed_count} 项")
    today.add_row("未 commit（已结束）", f"{_hours_cell(today_uncommit_hours)} · {today_uncommit_count} 项")
    if active:
        name, start, _ = active
        today.add_row(
            "进行中任务",
            f"{name} · {start.strftime('%H:%M')} 开始 · 已持续 {_fmt_hm(active_hours)}",
        )
    else:
        today.add_row("进行中任务", Text("无", style="dim"))
    today.add_row(
        "本天合计",
        Text(_hours_cell(today_committed_hours + today_uncommit_hours + active_hours), style="bold bright_green"),
    )
    console.print(Padding(today, (0, 0, 0, 2)))


def _render_plain(
    committed_items: list[dict[str, Any]],
    uncommit_items: list[dict[str, Any]],
    active: tuple[str, datetime, float] | None,
    today_weekday: str,
    today_date: str,
) -> None:
    items = committed_items + uncommit_items
    total_hours, total_count = _aggregate(items)
    committed_hours, committed_count = _aggregate(committed_items)
    uncommit_hours, uncommit_count = _aggregate(uncommit_items)

    print("== 总览 · committed + 未 commit ==")
    print(f"任务总数: {total_count} 项 = committed {committed_count} 项 + 未 commit {uncommit_count} 项")
    print(f"总用时: {_hours_cell(total_hours)} = committed {_fmt_hm(committed_hours)} + 未 commit {_fmt_hm(uncommit_hours)}")

    if items:
        print("== 类别统计 ==")
        for category, hours, count in _group_by_category(items):
            print(f"{category}: {count} 项, {_hours_cell(hours)}")
        print("== 按天统计 ==")
        for weekday, day_items in _group_by_weekday(items):
            day_hours, day_count = _aggregate(day_items)
            print(f"{weekday}: {day_count} 项, {_hours_cell(day_hours)}")
            for category, hours, count in _group_by_category(day_items):
                print(f"  - {category}: {count} 项, {_hours_cell(hours)}")

    today_committed_hours, today_committed_count = _aggregate(
        [item for item in committed_items if _item_weekday(item) == today_weekday]
    )
    today_uncommit_hours, today_uncommit_count = _aggregate(
        [item for item in uncommit_items if _item_weekday(item) == today_weekday]
    )
    active_hours = active[2] if active else 0.0

    print(f"== 本天 · {today_weekday}（log 日 {today_date}）==")
    print(f"已 committed: {_hours_cell(today_committed_hours)} · {today_committed_count} 项")
    print(f"未 commit（已结束）: {_hours_cell(today_uncommit_hours)} · {today_uncommit_count} 项")
    if active:
        name, start, _ = active
        print(f"进行中任务: {name} · {start.strftime('%H:%M')} 开始 · 已持续 {_fmt_hm(active_hours)}")
    else:
        print("进行中任务: 无")
    print(f"本天合计: {_hours_cell(today_committed_hours + today_uncommit_hours + active_hours)}")


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def status_task(context: dict[str, str]) -> int:
    """log status —— 输出 committed + 未 commit 的总览 / 类别 / 按天统计与本天时间。"""
    committed_items = _load_existing_pool_items(context)

    try:
        records = read_txt_records(context["uncommit_file"])
    except (ValueError, KeyError, OSError):
        records = []
    # 已结束但未 commit 的 session，按 commit 相同口径聚合；进行中任务不在其中。
    uncommit_items = _build_base_commit_items(records)
    active = _active_task_info(records)

    now = datetime.now()
    today_weekday = _effective_weekday(now)
    today_date = _effective_date(now)

    if RICH_AVAILABLE:
        _render_rich(committed_items, uncommit_items, active, today_weekday, today_date)
    else:
        _render_plain(committed_items, uncommit_items, active, today_weekday, today_date)
    return 0
