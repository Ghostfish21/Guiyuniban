"""
achievements —— 独立的“成就条”模块。

在 `log start` / `log cont` / `log end` 成功后触发，用类似游戏成就条的 UI 弹出提示。
本模块只被 guiyuniban_control.py 的 dispatch 在命令成功后调用，不改动 start.py / end.py 的内部逻辑。

五条成就（均按 log 系统自身的“周几边界”划分一天，而不是午夜 12:00）：

1. 当天第一个任务【开始】时：           💀 惊悚恐怖骷髅头
2. 当天第一个任务【结束】时：           🕯 永恒不再
3. 任务结束时，当天累计已完成时长首次 > 7 小时：   🔥 强者愈弱……
4. 任务结束时，当天累计已完成时长首次 > 11 小时：  ⚡ ……而弱者愈强
5. 任务结束时，若 本日理论剩余时间 * 0.75 + 已完成时间 < 5 小时： 🤡 别逗你Y哥笑了

说明：
- “一天”的边界复用 summary.DAY_BOUNDARY（07:00 及以前算前一天，与周几归属一致）。
- “本日理论剩余时间”= 从任务结束时刻到当天日界（下一个 07:00）的墙钟小时数，最低取 0。
- 每条成就在同一 log 日内至多弹出一次（成就本就是一次性的），状态记在 DATA_DIR/achievements_state.txt。
- 任何异常都被吞掉，绝不影响 start / end 主流程。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.text import Text

    _console = Console()
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - 只在未安装 rich 的环境中触发
    _console = None
    RICH_AVAILABLE = False

# 复用 log 系统“自身区分周几”的日界（07:00 及以前算前一天），保持单一事实来源。
from summary import DAY_BOUNDARY


STATE_FILENAME = "achievements_state.txt"

# 阈值（小时）
OVER7_HOURS = 7.0
OVER11_HOURS = 11.0
CANT_REACH_TARGET_HOURS = 5.0
CANT_REACH_REMAINING_WEIGHT = 0.75

# 每条成就的展示元数据
ACHIEVEMENTS: dict[str, dict[str, str]] = {
    "first_start": {
        "emoji": "💀",
        "title": "惊悚恐怖骷髅头",
        "accent": "bright_red",
        "subtitle": "今日第一个任务开始",
    },
    "first_end": {
        "emoji": "🕯",
        "title": "永恒不再",
        "accent": "magenta",
        "subtitle": "今日第一个任务结束",
    },
    "over7": {
        "emoji": "🔥",
        "title": "强者愈弱……",
        "accent": "dark_orange",
        "subtitle": "今日累计完成已越过 7 小时线",
    },
    "over11": {
        "emoji": "⚡",
        "title": "……而弱者愈强",
        "accent": "gold1",
        "subtitle": "今日累计完成已越过 11 小时线",
    },
    "cant_reach_5": {
        "emoji": "🤡",
        "title": "别逗你Y哥笑了",
        "accent": "yellow",
        "subtitle": "本日目标已无力回天",
    },
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _read_records(file_path: str) -> list[dict[str, Any]]:
    path = Path(file_path)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _find_active_session(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(records):
        if (
            record.get("type") == "session"
            and record.get("end_time") is None
            and record.get("committed") is False
        ):
            return record
    return None


def _find_session_by_id(
    records: list[dict[str, Any]], session_id: str | None
) -> dict[str, Any] | None:
    if session_id:
        for record in records:
            if str(record.get("session_id")) == str(session_id):
                return record

    # 兜底：拿最近一次“已结束”的 session（按 updated_at / end_time 最大）。
    ended = [
        record
        for record in records
        if record.get("type") == "session" and record.get("end_time")
    ]
    if not ended:
        return None
    return max(ended, key=lambda r: str(r.get("updated_at") or r.get("end_time") or ""))


# --------------------------------------------------------------------------- #
# “一天”的边界（复用 log 系统自身的 07:00 周几边界）
# --------------------------------------------------------------------------- #
def _active_day_key(dt: datetime) -> str:
    """dt 归属的 log 日键（与 summary._effective_weekday 完全一致的归属规则）。"""
    effective = dt
    if dt.timetz().replace(tzinfo=None) <= DAY_BOUNDARY:
        effective = dt - timedelta(days=1)
    return effective.date().isoformat()


def _day_end(dt: datetime) -> datetime:
    """dt 所属 log 日的结束时刻（下一个 07:00 日界）。"""
    if dt.timetz().replace(tzinfo=None) <= DAY_BOUNDARY:
        end_date = dt.date()
    else:
        end_date = dt.date() + timedelta(days=1)
    return datetime.combine(end_date, DAY_BOUNDARY, tzinfo=dt.tzinfo)


# --------------------------------------------------------------------------- #
# 成就状态（每 log 日一份，跨日自动重置）
# --------------------------------------------------------------------------- #
def _state_path(context: dict[str, str]) -> Path:
    data_dir = context.get("data_dir")
    if data_dir:
        return Path(data_dir) / STATE_FILENAME
    return Path(context["uncommit_file"]).parent / STATE_FILENAME


def _load_state(context: dict[str, str]) -> dict[str, Any]:
    path = _state_path(context)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(context: dict[str, str], state: dict[str, Any]) -> None:
    path = _state_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_day(state: dict[str, Any], day_key: str) -> dict[str, Any]:
    """把 state 对齐到 day_key；跨日则重置累计与已弹出记录。"""
    if state.get("day_key") != day_key:
        return {"day_key": day_key, "completed_seconds": 0.0, "fired": {}}
    state.setdefault("completed_seconds", 0.0)
    state.setdefault("fired", {})
    return state


def _already_fired(state: dict[str, Any], key: str) -> bool:
    return bool(state.get("fired", {}).get(key))


def _mark_fired(state: dict[str, Any], key: str) -> None:
    state.setdefault("fired", {})[key] = True


# --------------------------------------------------------------------------- #
# 成就条 UI
# --------------------------------------------------------------------------- #
def _show_achievement(key: str, detail: str | None = None) -> None:
    meta = ACHIEVEMENTS.get(key)
    if not meta:
        return

    emoji = meta["emoji"]
    title = meta["title"]
    accent = meta["accent"]
    subtitle = detail if detail is not None else meta.get("subtitle", "")

    if not RICH_AVAILABLE:
        print()
        print(f"🏆 成就达成   {emoji} {title}")
        if subtitle:
            print(f"            {subtitle}")
        print()
        return

    lines = [Text(f"{emoji}  {title}", style=f"bold {accent}")]
    if subtitle:
        lines.append(Text(subtitle, style="dim"))

    _console.print(
        Panel(
            Group(*lines),
            title=Text("🏆 成就达成", style=f"bold {accent}"),
            border_style=accent,
            box=box.DOUBLE,
            expand=False,
            padding=(1, 4),
        )
    )


# --------------------------------------------------------------------------- #
# 对外入口：由 guiyuniban_control.dispatch 在命令成功后调用
# --------------------------------------------------------------------------- #
def active_session_id(context: dict[str, str]) -> str | None:
    """在 log end 执行【前】捕获当前 active session id，供结束后定位刚结束的任务。"""
    try:
        records = _read_records(context["uncommit_file"])
        session = _find_active_session(records)
        if session is None:
            return None
        sid = session.get("session_id")
        return str(sid) if sid is not None else None
    except Exception:
        return None


def notify_start(context: dict[str, str]) -> None:
    """log start / log cont 成功后调用：处理“当天第一个任务开始”。"""
    try:
        records = _read_records(context["uncommit_file"])
        session = _find_active_session(records)
        if session is None:
            return
        start = _parse_dt(session.get("start_time"))
        if start is None:
            return

        day_key = _active_day_key(start)
        state = _ensure_day(_load_state(context), day_key)

        if not _already_fired(state, "first_start"):
            _mark_fired(state, "first_start")
            _show_achievement("first_start")

        _save_state(context, state)
    except Exception:
        # 成就模块是附加功能，任何异常都不应影响主流程。
        pass


def notify_end(context: dict[str, str], session_id: str | None) -> None:
    """log end 成功后调用：处理“第一个任务结束”“累计 7/11 小时”“无力回天”四条成就。"""
    try:
        records = _read_records(context["uncommit_file"])
        session = _find_session_by_id(records, session_id)
        if session is None:
            return

        start = _parse_dt(session.get("start_time"))
        end = _parse_dt(session.get("end_time"))
        if start is None or end is None:
            return

        day_key = _active_day_key(start)
        state = _ensure_day(_load_state(context), day_key)

        # 累加这条已完成任务的时长
        duration_seconds = max(0.0, (end - start).total_seconds())
        state["completed_seconds"] = float(state.get("completed_seconds", 0.0)) + duration_seconds

        completed_hours = state["completed_seconds"] / 3600.0
        remaining_hours = max(0.0, (_day_end(end) - end).total_seconds() / 3600.0)

        # 2. 当天第一个任务结束
        if not _already_fired(state, "first_end"):
            _mark_fired(state, "first_end")
            _show_achievement("first_end")

        # 3. 累计首次 > 7 小时
        if completed_hours > OVER7_HOURS and not _already_fired(state, "over7"):
            _mark_fired(state, "over7")
            _show_achievement("over7", f"今日累计完成 {completed_hours:.1f} 小时，已越过 7 小时线")

        # 4. 累计首次 > 11 小时
        if completed_hours > OVER11_HOURS and not _already_fired(state, "over11"):
            _mark_fired(state, "over11")
            _show_achievement("over11", f"今日累计完成 {completed_hours:.1f} 小时，已越过 11 小时线")

        # 5. 本日理论剩余时间 * 0.75 + 已完成时间 < 5 小时
        projected = remaining_hours * CANT_REACH_REMAINING_WEIGHT + completed_hours
        if projected < CANT_REACH_TARGET_HOURS and not _already_fired(state, "cant_reach_5"):
            _mark_fired(state, "cant_reach_5")
            _show_achievement(
                "cant_reach_5",
                f"剩余 {remaining_hours:.1f}h × 0.75 + 已完成 {completed_hours:.1f}h ≈ {projected:.1f}h < 5h",
            )

        _save_state(context, state)
    except Exception:
        pass
