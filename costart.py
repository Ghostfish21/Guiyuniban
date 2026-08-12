"""
log costart / log coend —— 并发任务子系统。

这套东西刻意**不是**普通任务系统的一部分：

- 虚拟任务写在同一个 uncommit_tasks.txt 里，但 `type` 是 "co_session"。
  全项目所有读取该文件的地方都带 `type == "session"` 过滤（start / cont / end /
  status / achievements / commit / desc），所以虚拟任务在结算之前对它们完全不可见。
- 虚拟记录**绝不带 `session_id` 这个键**。achievements._find_session_by_id 是唯一
  一处不带 type 过滤的按 id 查找，只要没有这个字段就永远匹配不上。同理不带
  task_group_id / task_index / committed，把误伤面收到零。

两条不变式（下面多处直接依赖，破坏时宁可报错也不要写出脏数据）：

1. 未结束的虚拟任务永远构成一条单链。因为 costart 总是挂到「当前路径上最内层
   仍在进行的任务」，任意时刻只有链尾在活跃。
2. 同一父级的直接子任务互不重叠。前一个直接子任务结束前，新的 costart 只会挂到
   它下面而不是成为兄弟；由此 Σ直接子任务 ≤ 父任务时长恒成立，净时长不会为负。

结算只在 `log end` 发生一次（见 settle_on_end）：递归展平整棵树，把已结束的虚拟
任务落成真实 session，把仍在进行的那条链的链首落成新的真实 active session、其余
继续留在本子系统里。coend 只写自己的 end_time，既不提升子任务也不排时间轴——
提升会抹掉层级归属，而挪 end_time 时恰恰需要知道每条已结束的任务原本挂在哪一级。
"""

from __future__ import annotations

import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from end import (
    _format_duration_seconds,
    _parse_dt,
    find_active_session,
    now_iso,
    read_txt_records,
    write_txt_records,
)
from start import allocate_task_index, append_txt_record

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - 只在未安装 rich 的环境中触发
    console = None
    RICH_AVAILABLE = False


# 虚拟任务的 type 值；这一个字符串就是与真实任务之间的整道隔离墙。
CO_TYPE = "co_session"

# 重排时段与段之间插入的随机间隔。taskeditor.analysis 的重叠检测是闭区间，
# 相触即算重叠，所以间隔必须 >= 1 秒；这里沿用 autoadjust 的 10~60 秒约定。
GAP_MIN_SECONDS = 10
GAP_MAX_SECONDS = 60

# 结算会把仍在进行的那条链整体后移「各段间隔之和」秒。已结束的任务是整段平移
# （起止一起挪，时长不变），但进行中的任务此刻还没有终点，于是把这个偏移量记在
# 记录上：等它真正结束时，终点也加上同样的秒数，时长才不会凭空少掉这几十秒。
#
# 偏移会累积——同一条血脉被结算多次就加多次，所以每次都是「原有偏移 + 本次间隔和」。
CO_SHIFT_KEY = "co_shift_seconds"

_DEFAULT_RNG = random.Random()


class SettleError(ValueError):
    """并发任务结算失败。调用方必须放弃整次写盘，并把原因原样报给用户。"""


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _render_error(title: str, message: str) -> None:
    if not RICH_AVAILABLE:
        print(title)
        print(message)
        return
    console.print(
        Panel(
            Text(message, style="red"),
            title=Text(title, style="bold red"),
            border_style="red",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_info(title: str, message: str) -> None:
    if not RICH_AVAILABLE:
        print(title)
        print(message)
        return
    console.print(
        Panel(
            Text(message, style="white"),
            title=Text(title, style="bold blue"),
            border_style="blue",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_panel(title: str, table: Any, note: str, style: str) -> None:
    if not RICH_AVAILABLE:
        print(title)
        print(note)
        return
    console.print(
        Panel(
            Group(Text(note, style="dim"), "", table),
            title=Text(title, style=f"bold {style}"),
            border_style=style,
            box=box.ROUNDED,
            expand=False,
        )
    )


# --------------------------------------------------------------------------- #
# 记录访问
# --------------------------------------------------------------------------- #
def _co_id(record: dict[str, Any]) -> str:
    return str(record.get("co_id") or "")


def _name(record: dict[str, Any]) -> str:
    return str(record.get("task_name") or "未命名任务")


def _live_co_records(records: list[dict[str, Any]], root_session_id: str) -> list[dict[str, Any]]:
    """属于当前顶层真实任务、尚未被结算掉的虚拟任务。"""
    return [
        record
        for record in records
        if record.get("type") == CO_TYPE
        and str(record.get("root_session_id") or "") == root_session_id
    ]


def _children(records: list[dict[str, Any]], parent_id: str) -> list[dict[str, Any]]:
    """某个节点的直接子任务，按真实开始时间排序（= 展平后的时间轴顺序）。"""
    return sorted(
        (
            record
            for record in records
            if record.get("type") == CO_TYPE and str(record.get("parent_id") or "") == parent_id
        ),
        key=lambda record: str(record.get("start_time") or ""),
    )


def _subtree(records: list[dict[str, Any]], node: dict[str, Any]) -> list[dict[str, Any]]:
    """node 的全部后代（不含自身），前序。"""
    result: list[dict[str, Any]] = []
    for child in _children(records, _co_id(node)):
        result.append(child)
        result.extend(_subtree(records, child))
    return result


def active_co_tasks(records: list[dict[str, Any]], root_session_id: str) -> list[dict[str, Any]]:
    """
    仍在进行的虚拟任务，按开始时间排序（= 由外层到内层）。

    注意这里不沿着「未结束的子任务」往下走：coend 之后父节点已结束、子节点却还活着，
    走 parent 链会在父节点处断掉。直接取全部未结束的即可，不变式 1 保证它们成链。
    """
    return sorted(
        (record for record in _live_co_records(records, root_session_id) if record.get("end_time") is None),
        key=lambda record: str(record.get("start_time") or ""),
    )


def _depth_of(records: list[dict[str, Any]], node: dict[str, Any], root_session_id: str) -> int:
    """节点相对顶层真实任务的层数（直接子任务为 1）。用于选择窗口的缩进。"""
    by_id = {_co_id(record): record for record in records if record.get("type") == CO_TYPE}
    depth = 1
    current = str(node.get("parent_id") or "")
    seen: set[str] = set()
    while current and current != root_session_id and current in by_id and current not in seen:
        seen.add(current)
        depth += 1
        current = str(by_id[current].get("parent_id") or "")
    return depth


def shift_of(record: dict[str, Any]) -> timedelta:
    """这条记录被结算整体后移了多少秒。没有该字段（普通任务、老记录）就是 0。"""
    try:
        return timedelta(seconds=int(record.get(CO_SHIFT_KEY) or 0))
    except (TypeError, ValueError):
        return timedelta()


def wall_clock_end(record: dict[str, Any], context: dict[str, str]) -> str:
    """
    用「当前时刻」给一条记录收尾时该写的结束时间。

    如果这条记录的起点被结算后移过，终点必须后移同样的秒数，否则时长会凭空少掉那一截。
    只有以墙上时钟结束时才需要这样补——按自然语言时长结束时终点是「起点 + 时长」
    算出来的，起点里已经含了偏移，再补一次就重复了。
    """
    return (_parse_dt(now_iso(context)) + shift_of(record)).isoformat(timespec="seconds")  # type: ignore[union-attr]


def _elapsed_seconds(record: dict[str, Any], reference: datetime) -> int:
    start = _parse_dt(record.get("start_time"))
    end = _parse_dt(record.get("end_time")) or reference
    if start is None:
        return 0
    return max(0, int((end - start).total_seconds()))


# --------------------------------------------------------------------------- #
# log costart
# --------------------------------------------------------------------------- #
def _chain_path(records: list[dict[str, Any]], node: dict[str, Any], root: dict[str, Any]) -> str:
    """把「顶层任务 › 父 › 自己」拼成一行，让用户看清自己挂在哪一层。"""
    by_id = {_co_id(record): record for record in records if record.get("type") == CO_TYPE}
    names = [_name(node)]
    current = str(node.get("parent_id") or "")
    seen: set[str] = set()
    while current and current in by_id and current not in seen:
        seen.add(current)
        names.append(_name(by_id[current]))
        current = str(by_id[current].get("parent_id") or "")
    names.append(str(root.get("canonical_task_name") or root.get("task_name") or "未命名任务"))
    return " › ".join(reversed(names))


def costart_task(task_name: str, context: dict[str, str]) -> int:
    """
    处理:
      log costart 任务名

    在当前正在进行的任务之上再开一个并发任务。父级 = 当前路径上最内层仍在进行的
    任务（可能是另一个 costart 任务），没有并发任务时就是顶层真实任务本身。
    """
    task_name = task_name.strip()
    if not task_name:
        _render_error("任务名为空", "请提供任务名，例如: log costart 写代码")
        return 2

    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is None:
        _render_error(
            "没有正在进行的任务",
            "log costart 只能在已有任务进行时使用。请先 log start 任务名，再 log costart。",
        )
        return 1

    root = records[active_index]
    root_id = str(root.get("session_id") or "")
    if not root_id:
        _render_error("当前任务缺少 session_id", "无法挂载并发任务；请检查 uncommit_tasks.txt。")
        return 1

    chain = active_co_tasks(records, root_id)
    parent_id = _co_id(chain[-1]) if chain else root_id

    now = now_iso(context)
    record = {
        "type": CO_TYPE,
        "co_id": str(uuid.uuid4()),
        "parent_id": parent_id,
        "root_session_id": root_id,
        "task_name": task_name,
        "start_time": now,
        "end_time": None,
        "created_at": now,
        "updated_at": now,
    }

    append_txt_record(context["uncommit_file"], record)

    records.append(record)
    table = Table.grid(padding=(0, 2)) if RICH_AVAILABLE else None
    if table is not None:
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="white")
        table.add_row("任务名", task_name)
        table.add_row("开始时间", now)
        table.add_row("并发链", _chain_path(records, record, root))
        table.add_row("层级", f"第 {_depth_of(records, record, root_id)} 层")
    _render_panel(
        "并发任务已开始",
        table,
        "这是一个并发（虚拟）任务：log status / log commit 在 log end 结算前都看不到它。",
        "magenta",
    )
    return 0


# --------------------------------------------------------------------------- #
# log coend
# --------------------------------------------------------------------------- #
def _pick_target_in_terminal(rows: list[dict[str, Any]]) -> Optional[str]:
    """PySide6 缺失时的兜底：终端编号选择。非交互场景下直接放弃。"""
    if not RICH_AVAILABLE or not sys.stdin or not sys.stdin.isatty():
        _render_error(
            "无法选择要结束的并发任务",
            "当前有多个进行中的并发任务，但既没有 PySide6 选择窗口、也不是交互终端。\n"
            "请安装 PySide6 后重试：python -m pip install PySide6-Essentials",
        )
        return None

    console.print("[bold]请选择要结束的并发任务：[/bold]")
    for number, row in enumerate(rows, start=1):
        indent = "  " * (int(row["depth"]) - 1)
        console.print(f"  [bright_cyan]{number}[/bright_cyan]. {indent}{row['name']}  [dim]已进行 {row['elapsed_text']}[/dim]")

    try:
        answer = console.input("[dim]输入编号（回车取消）：[/dim] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not answer:
        return None
    try:
        choice = int(answer)
    except ValueError:
        _render_error("输入不是编号", f"收到：{answer!r}")
        return None
    if not 1 <= choice <= len(rows):
        _render_error("编号超出范围", f"可选 1 ~ {len(rows)}，收到 {choice}。")
        return None
    return str(rows[choice - 1]["co_id"])


def _pick_target(rows: list[dict[str, Any]]) -> Optional[str]:
    """弹出 PySide6 选择窗口；Qt 不可用时回退到终端编号选择。"""
    try:
        from taskeditor.ui.copicker import run_co_picker
    except ImportError:
        return _pick_target_in_terminal(rows)

    return run_co_picker(rows)


def coend_task(context: dict[str, str]) -> int:
    """
    处理:
      log coend

    结束一个并发任务。只有一个候选时直接结束；多个候选时弹选择窗口。
    这里只写 end_time：子任务原地保留嵌套关系，整棵树的展平推迟到 log end。
    """
    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is None:
        _render_error(
            "没有正在进行的任务",
            "并发任务只依附于真实任务；当前没有可结束的并发任务。",
        )
        return 1

    root = records[active_index]
    root_id = str(root.get("session_id") or "")
    candidates = active_co_tasks(records, root_id)
    if not candidates:
        _render_error(
            "没有进行中的并发任务",
            "当前没有 log costart 建立的并发任务。要结束正在进行的任务请用 log end。",
        )
        return 1

    if len(candidates) == 1:
        target: Optional[dict[str, Any]] = candidates[0]
    else:
        reference = _parse_dt(now_iso(context)) or datetime.now()
        rows = [
            {
                "co_id": _co_id(record),
                "name": _name(record),
                "depth": _depth_of(records, record, root_id),
                "start_time": str(record.get("start_time") or ""),
                "elapsed_text": _format_duration_seconds(_elapsed_seconds(record, reference)),
            }
            for record in candidates
        ]
        picked = _pick_target(rows)
        if not picked:
            _render_info("已取消", "没有结束任何并发任务。")
            return 0
        target = next((record for record in candidates if _co_id(record) == picked), None)
        if target is None:
            _render_error("选择无效", "选中的并发任务已不存在，请重试。")
            return 1

    # 起点被结算整体后移过时，终点必须后移同样的秒数：时长才是真实的，
    # 也就不会出现「真实时间还没走到新起点」而写出 end < start 的坏记录。
    now = wall_clock_end(target, context)
    clamped = False
    start = _parse_dt(target.get("start_time"))
    end = _parse_dt(now)
    if start is not None and end is not None and end < start:
        # 补过偏移之后正常到不了这里；留着兜被手工改过的脏记录。
        now = str(target.get("start_time"))
        clamped = True

    target["end_time"] = now
    target["updated_at"] = now_iso(context)
    write_txt_records(context["uncommit_file"], records)

    remaining = active_co_tasks(records, root_id)
    table = Table.grid(padding=(0, 2)) if RICH_AVAILABLE else None
    if table is not None:
        reference = _parse_dt(now) or datetime.now()
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="white")
        table.add_row("任务名", _name(target))
        table.add_row("开始时间", str(target.get("start_time") or ""))
        table.add_row("结束时间", now)
        table.add_row("已进行", _format_duration_seconds(_elapsed_seconds(target, reference)))
        table.add_row(
            "仍在进行的并发任务",
            "、".join(_name(record) for record in remaining) if remaining else "无",
        )
    note = "时长与时间轴要等到 log end 结算顶层任务时才会一次性排定。"
    if clamped:
        note += (
            "\n注意：这条记录的开始时间晚于补过偏移后的结束时间（可能被手工改过），"
            "本次按 0 时长结算。"
        )
    _render_panel("并发任务已结束", table, note, "magenta")
    return 0


# --------------------------------------------------------------------------- #
# 结算：递归展平 + 一次性重排
# --------------------------------------------------------------------------- #
@dataclass
class _Segment:
    """展平后的一段：某个虚拟任务扣掉其子树之后自己净占用的时长。"""

    co: dict[str, Any]
    duration: timedelta


@dataclass
class SettleReport:
    root_name: str
    root_old_end: str
    root_new_end: str
    materialized: list[tuple[str, str, str]] = field(default_factory=list)
    carried: Optional[tuple[str, str]] = None
    carry_shift_seconds: int = 0
    weekday_shifts: list[str] = field(default_factory=list)


def _validate_chain(unfinished: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """
    校验不变式 1，并返回活跃链的链首（最外层那个未结束的虚拟任务）。

    链首之后要落成新的真实 active session；若出现多条并行的未结束链，就会产出多个
    真实 active session，直接破坏「同时只有一个进行中任务」的全局前提，宁可报错。
    """
    if not unfinished:
        return None

    by_id = {_co_id(record): record for record in unfinished}
    heads = [record for record in unfinished if str(record.get("parent_id") or "") not in by_id]
    if len(heads) != 1:
        names = "、".join(_name(record) for record in heads) or "（无）"
        raise SettleError(
            f"检测到 {len(heads)} 条并行的未结束并发任务链：{names}。\n"
            "正常操作不会产生这种状态；请用 log coend 逐个结束，或检查 uncommit_tasks.txt 是否被手工改过。"
        )

    for record in unfinished:
        kids = [other for other in unfinished if str(other.get("parent_id") or "") == _co_id(record)]
        if len(kids) > 1:
            names = "、".join(_name(kid) for kid in kids)
            raise SettleError(f"并发任务「{_name(record)}」下同时有多个未结束的子任务：{names}。")

    return heads[0]


def _flatten(
    records: list[dict[str, Any]],
    co: dict[str, Any],
    t_end: datetime,
    carry_id: str,
) -> tuple[list[_Segment], datetime]:
    """
    后序展平一棵虚拟任务子树。

    返回 (segments, subtree_end)：
    - segments 按真实发生顺序排列（节点自身的净时长段排在它的子树之前）；
    - subtree_end 是这棵子树在真实时间轴上占用的右端点——子任务可能比父任务晚结束
      （coend 父级之后子级还在跑），父级扣减时要按这个端点裁剪。

    carry_id 指向活跃链链首：那棵子树整体顺延给结算后新建的真实任务，
    不参与本次展平，但仍要把右端点报给父级，父级才能正确扣时长。
    """
    start = _parse_dt(co.get("start_time"))
    if start is None:
        raise SettleError(f"并发任务「{_name(co)}」的开始时间不合法：{co.get('start_time')!r}")

    if _co_id(co) == carry_id:
        return [], t_end

    own_end = _parse_dt(co.get("end_time")) or t_end
    if own_end < start:
        raise SettleError(f"并发任务「{_name(co)}」的结束时间早于开始时间。")
    if own_end > t_end:
        raise SettleError(
            f"并发任务「{_name(co)}」结束于 {co.get('end_time')}，晚于本次 log end 的结束时间 "
            f"{t_end.isoformat(timespec='seconds')}。请改用更晚的结束时间。"
        )

    segments: list[_Segment] = []
    occupied = timedelta()
    subtree_end = own_end

    for child in _children(records, _co_id(co)):
        child_start = _parse_dt(child.get("start_time"))
        if child_start is None:
            raise SettleError(f"并发任务「{_name(child)}」的开始时间不合法：{child.get('start_time')!r}")
        if child_start < start:
            raise SettleError(f"并发任务「{_name(child)}」的开始时间早于其父任务「{_name(co)}」。")

        child_segments, child_end = _flatten(records, child, t_end, carry_id)
        segments.extend(child_segments)
        # 子树只有落在父级窗口内的那一段才从父级扣除；溢出的部分排在父级之后。
        # 取 max(0, ...) 是为了兜住零时长的父级（coend 的收口路径）：此时子级窗口
        # 与父级窗口完全不相交，交集应当算 0 而不是负数。
        occupied += max(timedelta(), min(child_end, own_end) - child_start)
        subtree_end = max(subtree_end, child_end)

    self_duration = (own_end - start) - occupied
    if self_duration < timedelta(0):
        raise SettleError(
            f"并发任务「{_name(co)}」的子任务总时长超过了它自身的时长；"
            "这违反了并发任务的嵌套规则，可能是 uncommit_tasks.txt 被手工改过。"
        )

    return [_Segment(co, self_duration)] + segments, subtree_end


def _materialize(
    co: dict[str, Any],
    start: datetime,
    end: Optional[datetime],
    context: dict[str, str],
    records: list[dict[str, Any]],
    session_id: str,
    shift_seconds: int = 0,
) -> dict[str, Any]:
    """把一个虚拟任务落成真实 session 记录。"""
    now = now_iso(context)
    name = _name(co)
    record = {
        "type": "session",
        "task_group_id": str(uuid.uuid4()),
        "session_id": session_id,
        "command": "costart",
        "task_name": name,
        "canonical_task_name": name,
        "start_time": start.isoformat(timespec="seconds"),
        "end_time": end.isoformat(timespec="seconds") if end is not None else None,
        "committed": False,
        "task_index": allocate_task_index(context, records),
        "created_at": now,
        "updated_at": now,
        # 留档：这条真实任务是从哪个并发任务、哪段真实时间重排出来的。
        "co_origin": {
            "co_id": _co_id(co),
            "parent_id": str(co.get("parent_id") or ""),
            "real_start_time": co.get("start_time"),
            "real_end_time": co.get("end_time"),
        },
    }
    if shift_seconds:
        # 只有仍在进行的那一条需要：它的终点还没写，结束时要补回同样的偏移。
        record[CO_SHIFT_KEY] = shift_seconds
    return record


def _weekday_of(value: datetime) -> str:
    from summary import _effective_weekday

    return _effective_weekday(value)


def settle_on_end(
    records: list[dict[str, Any]],
    active_index: int,
    context: dict[str, str],
    rng: Optional[random.Random] = None,
) -> tuple[list[dict[str, Any]], Optional[SettleReport]]:
    """
    在 log end 写盘之前结算并发任务。

    调用约定：records[active_index] 的 end_time 已经填好（可能是 AI 解析出的历史时间）。
    本函数会就地把它改短，并返回新的 records 列表；抛 SettleError 时调用方必须放弃写盘。

    没有并发任务时原样返回，report 为 None。
    """
    rng = rng or _DEFAULT_RNG
    root = records[active_index]
    root_id = str(root.get("session_id") or "")
    if not root_id:
        return records, None

    live = _live_co_records(records, root_id)
    if not live:
        return records, None

    root_start = _parse_dt(root.get("start_time"))
    root_old_end = _parse_dt(root.get("end_time"))
    if root_start is None or root_old_end is None:
        raise SettleError("顶层任务的开始/结束时间不合法，无法结算并发任务。")
    t_end = root_old_end

    for record in live:
        start = _parse_dt(record.get("start_time"))
        if start is None:
            raise SettleError(f"并发任务「{_name(record)}」的开始时间不合法。")
        if start < root_start:
            raise SettleError(f"并发任务「{_name(record)}」的开始时间早于顶层任务的开始时间。")
        if start > t_end:
            raise SettleError(
                f"并发任务「{_name(record)}」开始于 {record.get('start_time')}，"
                f"晚于本次 log end 的结束时间 {t_end.isoformat(timespec='seconds')}。\n"
                "如果刚刚才结算过一次并发任务，这只是重排间隔造成的几十秒偏移，"
                "稍等片刻再 log end 即可。"
            )

    carry = _validate_chain([record for record in live if record.get("end_time") is None])
    carry_id = _co_id(carry) if carry is not None else ""

    # 1. 展平顶层任务的直接子树
    segments: list[_Segment] = []
    occupied = timedelta()
    for child in _children(records, root_id):
        child_start = _parse_dt(child.get("start_time"))
        if child_start is None:
            raise SettleError(f"并发任务「{_name(child)}」的开始时间不合法。")
        child_segments, child_end = _flatten(records, child, t_end, carry_id)
        segments.extend(child_segments)
        occupied += max(timedelta(), min(child_end, t_end) - child_start)

    root_net = (t_end - root_start) - occupied
    if root_net < timedelta(0):
        raise SettleError(
            "并发任务的总时长超过了顶层任务本身的时长；"
            "这违反了并发任务的嵌套规则，可能是 uncommit_tasks.txt 被手工改过。"
        )

    # 连通性校验：孤立的虚拟记录（parent 指向不存在的节点）不会被展平到，
    # 留在文件里会永远变成幽灵数据，宁可现在报错。
    reachable = {_co_id(segment.co) for segment in segments}
    if carry is not None:
        reachable.add(carry_id)
        reachable.update(_co_id(node) for node in _subtree(records, carry))
    orphans = [record for record in live if _co_id(record) not in reachable]
    if orphans:
        names = "、".join(_name(record) for record in orphans)
        raise SettleError(f"这些并发任务挂在不存在的父级上，无法结算：{names}。")

    report = SettleReport(
        root_name=str(root.get("canonical_task_name") or root.get("task_name") or "未命名任务"),
        root_old_end=t_end.isoformat(timespec="seconds"),
        root_new_end="",
    )

    # 2. 铺时间轴：顶层任务的净时长在最前，已结束的段依次跟上，段间插随机间隔
    cursor = root_start + root_net
    root["end_time"] = cursor.isoformat(timespec="seconds")
    root["updated_at"] = now_iso(context)
    report.root_new_end = root["end_time"]

    materialized: list[dict[str, Any]] = []
    for segment in segments:
        gap = timedelta(seconds=rng.randint(GAP_MIN_SECONDS, GAP_MAX_SECONDS))
        slot_start = cursor + gap
        slot_end = slot_start + segment.duration
        cursor = slot_end
        materialized.append(
            _materialize(segment.co, slot_start, slot_end, context, records, str(uuid.uuid4()))
        )
        report.materialized.append(
            (
                _name(segment.co),
                slot_start.isoformat(timespec="seconds"),
                slot_end.isoformat(timespec="seconds"),
            )
        )
        real_start = _parse_dt(segment.co.get("start_time"))
        if real_start is not None and _weekday_of(real_start) != _weekday_of(slot_start):
            report.weekday_shifts.append(
                f"{_name(segment.co)}：{_weekday_of(real_start)} → {_weekday_of(slot_start)}"
            )

    # 3. 活跃链链首落成新的真实 active session，它的整棵子树整体平移、改挂过去
    carry_real: Optional[dict[str, Any]] = None
    if carry is not None:
        gap = timedelta(seconds=rng.randint(GAP_MIN_SECONDS, GAP_MAX_SECONDS))
        carry_start = cursor + gap
        carry_real_start = _parse_dt(carry.get("start_time"))
        assert carry_real_start is not None  # 上面已逐条校验过
        delta = carry_start - carry_real_start
        new_session_id = str(uuid.uuid4())
        carry_real = _materialize(
            carry,
            carry_start,
            None,
            context,
            records,
            new_session_id,
            shift_seconds=int((shift_of(carry) + delta).total_seconds()),
        )
        report.carried = (_name(carry), carry_start.isoformat(timespec="seconds"))
        report.carry_shift_seconds = int((shift_of(carry) + delta).total_seconds())
        if _weekday_of(carry_real_start) != _weekday_of(carry_start):
            report.weekday_shifts.append(
                f"{_name(carry)}：{_weekday_of(carry_real_start)} → {_weekday_of(carry_start)}"
            )

        for node in _subtree(records, carry):
            for key in ("start_time", "end_time"):
                moment = _parse_dt(node.get(key))
                if moment is not None:
                    node[key] = (moment + delta).isoformat(timespec="seconds")
            node["root_session_id"] = new_session_id
            if str(node.get("parent_id") or "") == carry_id:
                node["parent_id"] = new_session_id
            # 这一群是整体平移的：已结束的起止都挪了、时长不变；仍在进行的把偏移
            # 累加记下来，等它自己 coend 时终点也补上同样的秒数。
            node[CO_SHIFT_KEY] = int((shift_of(node) + delta).total_seconds())
            node["updated_at"] = now_iso(context)

    # 4. 组装新的 records：消费掉的虚拟记录删除，落成的真实记录插入
    consumed = {_co_id(segment.co) for segment in segments}
    if carry is not None:
        consumed.add(carry_id)

    result: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") == CO_TYPE and _co_id(record) in consumed:
            continue
        if record is root:
            # 落成的已结束任务插在顶层任务**之前**：这样文件里最后一条已结束的记录
            # 仍然是顶层任务，log end 之后紧接着 log cont 才会继续它、而不是继续
            # 某段被重排出来的并发任务（start._choose_latest_ended_group 倒序取第一条）。
            result.extend(materialized)
        result.append(record)

    if carry_real is not None:
        # 新的 active session 放在最末：find_active_session 是倒序扫描。
        result.append(carry_real)

    return result, report


def render_settle_report(report: SettleReport) -> None:
    """log end 结算完之后打印重排结果。"""
    if not RICH_AVAILABLE:
        print("并发任务已结算")
        print(f"{report.root_name}: 结束时间 {report.root_old_end} → {report.root_new_end}")
        for name, start, end in report.materialized:
            print(f"  {name}: {start} ~ {end}")
        if report.carried:
            print(f"  {report.carried[0]}: {report.carried[1]} ~ 进行中")
        for line in report.weekday_shifts:
            print(f"  周几归属变化: {line}")
        return

    table = Table(show_header=True, header_style="bold bright_cyan", box=None, pad_edge=False)
    table.add_column("任务", style="bright_magenta", no_wrap=True)
    table.add_column("开始")
    table.add_column("结束")

    table.add_row(report.root_name, "（不变）", f"{report.root_old_end} → {report.root_new_end}")
    for name, start, end in report.materialized:
        table.add_row(name, start, end)
    if report.carried:
        table.add_row(report.carried[0], report.carried[1], Text("进行中", style="bold yellow"))

    note = (
        "并发任务已展平重排：顶层任务扣掉并发占用的时间，各并发任务依次排在其后，"
        "段间留 10~60 秒随机间隔。"
    )
    body: list[Any] = [Text(note, style="dim"), "", table]
    if report.carried:
        lines = [f"「{report.carried[0]}」已落成真实任务并仍在进行；结束它请用 log end。"]
        if report.carry_shift_seconds:
            lines.append(
                f"它连同下属的并发任务整体后移了 {report.carry_shift_seconds} 秒"
                "（重排间隔之和），结束时会自动把终点也后移同样的秒数，时长不受影响。"
            )
        body.extend(["", Text("\n".join(lines), style="bold yellow")])
    if report.weekday_shifts:
        body.extend(
            [
                "",
                Text("以下任务因重排改变了「周几」归属：", style="yellow"),
                Text("\n".join(f"· {line}" for line in report.weekday_shifts), style="yellow"),
            ]
        )

    console.print(
        Panel(
            Group(*body),
            title=Text("并发任务已结算", style="bold magenta"),
            border_style="magenta",
            box=box.ROUNDED,
            expand=False,
        )
    )
