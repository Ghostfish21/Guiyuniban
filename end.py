from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import json
import os
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

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


DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_OPENAI_MODEL = "gpt-5.4"


CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}


def read_config(config_file: str | None) -> dict[str, str]:
    if not config_file:
        return {}

    path = Path(config_file)
    if not path.exists():
        return {}

    config: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def get_timezone(context: dict[str, str] | None = None) -> ZoneInfo:
    config = read_config((context or {}).get("config_file"))
    timezone_name = (
        os.getenv("GUIYUNIBAN_TIMEZONE")
        or config.get("timezone")
        or DEFAULT_TIMEZONE
    )
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_dt(context: dict[str, str] | None = None) -> datetime:
    return datetime.now(get_timezone(context))


def now_iso(context: dict[str, str] | None = None) -> str:
    return now_dt(context).isoformat(timespec="seconds")


def read_txt_records(file_path: str) -> list[dict[str, Any]]:
    path = Path(file_path)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{file_path} 第 {line_no} 行不是合法 JSON: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def write_txt_records(file_path: str, records: list[dict[str, Any]]) -> None:
    """
    整份重写 uncommit_tasks.txt。

    先写同目录临时文件再 os.replace：这个文件是全量历史，直接以 "w" 打开会先截断，
    写到一半进程被杀就只剩半截历史。与 summary._atomic_write_text 保持同样的落盘方式。
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _preview_text(value: str, limit: int = 1200) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _render_remote_ai_thinking(model: str) -> None:
    message = f"{model} 思考中..."
    if not RICH_AVAILABLE:
        print(message)
        return

    console.print(
        Panel(
            Text(message, style="bold cyan"),
            title=Text("远程 AI 请求", style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_remote_ai_error(title: str, model: str, message: str) -> None:
    detail = f"模型: {model}\n{message}"
    if not RICH_AVAILABLE:
        print(title)
        print(detail)
        return

    console.print(
        Panel(
            Text(detail, style="red"),
            title=Text(title, style="bold red"),
            border_style="red",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _openai_json(messages: list[dict[str, str]], context: dict[str, str]) -> dict[str, Any] | None:
    config = read_config(context.get("config_file"))
    model = os.getenv("OPENAI_MODEL") or config.get("openai_model") or DEFAULT_OPENAI_MODEL
    api_key = os.getenv("OPENAI_API_KEY") or config.get("openai_api_key")
    if not api_key:
        _render_remote_ai_error(
            "远程 AI 未配置",
            model,
            "未找到 OPENAI_API_KEY，也未在 config.txt 中找到 openai_api_key；已跳过远程 LLM 请求。",
        )
        return None

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    _render_remote_ai_thinking(model)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        _render_remote_ai_error(
            "远程 AI HTTP 请求失败",
            model,
            f"HTTP {exc.code} {exc.reason}\n{_preview_text(body)}",
        )
        return None
    except urllib.error.URLError as exc:
        _render_remote_ai_error(
            "远程 AI 连接失败",
            model,
            f"{type(exc).__name__}: {exc.reason}",
        )
        return None
    except TimeoutError as exc:
        _render_remote_ai_error(
            "远程 AI 请求超时",
            model,
            f"{type(exc).__name__}: {exc}",
        )
        return None
    except OSError as exc:
        _render_remote_ai_error(
            "远程 AI 请求异常",
            model,
            f"{type(exc).__name__}: {exc}",
        )
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _render_remote_ai_error(
            "远程 AI 响应解析失败",
            model,
            f"OpenAI 返回内容不是合法 JSON: {exc}\n响应片段: {_preview_text(raw)}",
        )
        return None

    if isinstance(data, dict) and data.get("error"):
        _render_remote_ai_error(
            "远程 AI 返回错误",
            model,
            _preview_text(json.dumps(data["error"], ensure_ascii=False)),
        )
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        _render_remote_ai_error(
            "远程 AI 回复格式异常",
            model,
            f"无法从响应中读取 choices[0].message.content: {type(exc).__name__}: {exc}\n响应片段: {_preview_text(json.dumps(data, ensure_ascii=False))}",
        )
        return None

    if not isinstance(content, str) or not content.strip():
        _render_remote_ai_error(
            "远程 AI 回复为空",
            model,
            "已收到响应，但 choices[0].message.content 为空；无法获取远程 LLM 回复。",
        )
        return None

    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        _render_remote_ai_error(
            "远程 AI 回复 JSON 解析失败",
            model,
            f"模型回复不是合法 JSON: {exc}\n回复片段: {_preview_text(content)}",
        )
        return None

    if not isinstance(result, dict):
        _render_remote_ai_error(
            "远程 AI 回复类型异常",
            model,
            f"期望模型返回 JSON object，但实际返回: {type(result).__name__}",
        )
        return None

    return result


def _format_duration_seconds(seconds: int) -> str:
    if seconds < 0:
        return "未知"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if seconds or not parts:
        parts.append(f"{seconds} 秒")
    return " ".join(parts)


def _read_ai_duration_seconds(result: dict[str, Any], model: str) -> int:
    """只校验 AI 返回的持续时间；不从 raw_time 做本地自然语言解析。"""

    if "duration_seconds" not in result:
        _render_remote_ai_error(
            "远程 AI 解析结果不可用",
            model,
            "模型回复缺少 duration_seconds 字段；已停止，不再使用本地规则兜底。\n"
            f"回复: {_preview_text(json.dumps(result, ensure_ascii=False))}",
        )
        raise ValueError("远程 AI 未返回 duration_seconds，无法计算结束时间。")

    value = result.get("duration_seconds")
    if isinstance(value, bool):
        value = None

    try:
        seconds_float = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _render_remote_ai_error(
            "远程 AI 解析结果不可用",
            model,
            "模型返回的 duration_seconds 不是数字；已停止，不再使用本地规则兜底。\n"
            f"duration_seconds: {value!r}",
        )
        raise ValueError("远程 AI 返回的 duration_seconds 不是数字，无法计算结束时间。")

    if not seconds_float >= 0:
        _render_remote_ai_error(
            "远程 AI 解析结果不可用",
            model,
            "模型返回的 duration_seconds 小于 0；已停止，不再使用本地规则兜底。\n"
            f"duration_seconds: {value!r}",
        )
        raise ValueError("远程 AI 返回的 duration_seconds 小于 0，无法计算结束时间。")

    return int(round(seconds_float))


def parse_end_time_with_llm(raw_time: str, active_record: dict[str, Any], context: dict[str, str]) -> tuple[str, int, str]:
    """
    使用远程 AI 从自然语言中解析“持续时间”，再由程序按 active session 的 start_time 计算 end_time。

    重要行为:
      - raw_time 非空时只调用 AI 解析持续时间。
      - 不再使用本地规则解析自然语言时间。
      - AI 不可用、返回格式不对或 duration_seconds 不合法时直接失败。
    """

    start = _parse_dt(active_record.get("start_time"))
    if start is None:
        raise ValueError("active session 的 start_time 不合法，无法计算结束时间。")

    current = now_dt(context)
    config = read_config(context.get("config_file"))
    model = os.getenv("OPENAI_MODEL") or config.get("openai_model") or DEFAULT_OPENAI_MODEL

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个任务日志 CLI 的自然语言持续时间解析器。"
                "你的唯一任务是从用户输入中推断任务实际持续了多久。"
                "不要直接返回结束时间，不要返回 Markdown，必须返回 JSON object。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "raw_time": raw_time,
                    "active_session_start_time": active_record.get("start_time"),
                    "current_system_time": current.isoformat(timespec="seconds"),
                    "timezone": str(current.tzinfo),
                    "rules": [
                        "只返回任务持续时间 duration_seconds；不要返回 end_time。",
                        "如果 raw_time 描述了历史开始和历史结束，请计算二者之间的时间差，作为 duration_seconds。",
                        "如果 raw_time 描述了相对持续时间，例如 做了两小时、开始之后三个小时五十三分后，请直接换算为 duration_seconds。",
                        "如果 raw_time 只描述了一个结束钟点，例如 下午五点结束，请以 active_session_start_time 为起点推断持续时间；若同日结束时间早于起点，则按下一天处理。",
                        "active_session_start_time 是当前 active session 的实际开始时间；它只用于把持续时间换算为最终结束时间，不应被当成 raw_time 中的历史开始时间。",
                        "如果无法可靠推断持续时间，请返回 duration_seconds: null，并在 reason 中说明原因。",
                    ],
                    "output_schema": {
                        "duration_seconds": "number | null，任务持续秒数",
                        "confidence": "0 到 1",
                        "reason": "一句中文说明",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = _openai_json(messages, context)
    if result is None:
        raise ValueError("远程 AI 未返回可用持续时间；已停止，不再使用本地规则兜底。")

    duration_seconds = _read_ai_duration_seconds(result, model)
    end_time = (start + timedelta(seconds=duration_seconds)).isoformat(timespec="seconds")
    reason = str(result.get("reason") or "AI 已返回持续时间。")
    return end_time, duration_seconds, reason


def find_active_session(records: list[dict[str, Any]]) -> int | None:
    """
    找到当前正在进行的任务。
    简化规则:
    - type == session
    - end_time is None
    - committed is False
    """
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if (
            record.get("type") == "session"
            and record.get("end_time") is None
            and record.get("committed") is False
        ):
            return index

    return None


def _format_duration(start_time: str | None, end_time: str | None) -> str:
    if not start_time or not end_time:
        return "未知"

    try:
        start = datetime.fromisoformat(start_time)
        end = datetime.fromisoformat(end_time)
    except ValueError:
        return "未知"

    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return "结束时间早于开始时间"

    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def _print_plain(title: str, lines: list[str]) -> None:
    print(title)
    for line in lines:
        print(line)


def _render_error(title: str, message: str) -> None:
    if not RICH_AVAILABLE:
        _print_plain(title, [message])
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


def _render_natural_time_notice(raw_time: str, duration_seconds: int, end_time: str, reason: str) -> None:
    message = "AI 已将自然语言解析为任务持续时间；程序已按 active session 开始时间计算结束时间。"
    duration_text = _format_duration_seconds(duration_seconds)

    if not RICH_AVAILABLE:
        print(f"收到输入: {raw_time}")
        print(f"AI 持续时间: {duration_text} ({duration_seconds} 秒)")
        print(f"计算结果: {end_time}")
        if reason:
            print(f"AI 说明: {reason}")
        return

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold yellow", no_wrap=True)
    table.add_column(style="white")
    table.add_row("收到输入", raw_time)
    table.add_row("AI 持续时间", f"{duration_text} ({duration_seconds} 秒)")
    table.add_row("计算结果", end_time)
    if reason:
        table.add_row("AI 说明", reason)

    console.print(
        Panel(
            Group(Text(message, style="dim"), "", table),
            title=Text("AI 持续时间已解析", style="bold yellow"),
            border_style="yellow",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_ended_task(active_record: dict[str, Any]) -> None:
    start_time = active_record.get("start_time")
    end_time = active_record.get("end_time")
    duration = _format_duration(start_time, end_time)

    if not RICH_AVAILABLE:
        _print_plain(
            "已结束任务",
            [
                f"任务名: {active_record.get('task_name')}",
                f"开始时间: {start_time}",
                f"结束时间: {end_time}",
                f"持续时间: {duration}",
            ],
        )
        return

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="white")
    table.add_row("任务名", str(active_record.get("task_name") or ""))
    if active_record.get("canonical_task_name"):
        table.add_row("任务组名", str(active_record.get("canonical_task_name") or ""))
    table.add_row("开始时间", str(start_time or ""))
    table.add_row("结束时间", str(end_time or ""))
    table.add_row("持续时间", duration)
    table.add_row("任务组 ID", str(active_record.get("task_group_id") or ""))
    table.add_row("Session ID", str(active_record.get("session_id") or ""))

    body = Group(Text("active session 已写回 uncommit 文件。", style="dim"), "", table)
    console.print(
        Panel(
            body,
            title=Text("任务已结束", style="bold green"),
            border_style="green",
            box=box.ROUNDED,
            expand=False,
        )
    )


def end_task(raw_time: str, context: dict[str, str]) -> int:
    """
    处理:
      log end
      log end 时间

    raw_time:
      - 空字符串: 使用当前系统时间
      - 非空: 使用远程 AI 解析持续时间；失败时直接报错，不再使用本地规则兜底
    """

    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)

    if active_index is None:
        _render_error("没有正在进行的任务", "当前没有 active session 可结束。")
        return 1

    active_record = records[active_index]

    try:
        if raw_time:
            # 这条路上 end_time = 起点 + AI 给的时长。起点里已经含了并发结算的偏移，
            # 所以不需要再补一次，否则会重复计入。
            end_time, duration_seconds, ai_reason = parse_end_time_with_llm(raw_time, active_record, context)
            _render_natural_time_notice(raw_time, duration_seconds, end_time, ai_reason)
        else:
            # 以墙上时钟结束：如果这个任务是并发结算时整体后移过的，终点要跟着后移
            # 同样的秒数——起点挪了终点没挪，时长就会凭空少掉那一截。
            from costart import wall_clock_end

            end_time = wall_clock_end(active_record, context)
    except ValueError as exc:
        _render_error("结束时间解析失败", str(exc))
        return 1

    start = _parse_dt(active_record.get("start_time"))
    end = _parse_dt(end_time)
    if start is not None and end is not None and end < start:
        _render_error("结束时间早于开始时间", f"开始时间: {start.isoformat()}\n结束时间: {end.isoformat()}")
        return 1

    active_record["end_time"] = end_time
    active_record["updated_at"] = now_iso(context)
    records[active_index] = active_record

    # 并发任务（log costart）在这里一次性结算：展平重排会把 active_record 的 end_time
    # 改短、并落成若干条真实任务，所以必须排在渲染和写盘之前。
    # 结算失败时整次 log end 都不落盘，宁可让用户重来，也不要写出半套时间轴。
    from costart import SettleError, render_settle_report, settle_on_end

    try:
        records, co_report = settle_on_end(records, active_index, context)
    except SettleError as exc:
        _render_error("并发任务结算失败", f"{exc}\n\n本次 log end 未写入任何改动。")
        return 1

    write_txt_records(context["uncommit_file"], records)
    _render_ended_task(active_record)
    if co_report is not None:
        render_settle_report(co_report)
    return 0
