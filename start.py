from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

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
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_TASK_INDEX_START = 10000
TASK_INDEX_STEP = 5


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


def now_iso(context: dict[str, str] | None = None) -> str:
    return datetime.now(get_timezone(context)).isoformat(timespec="seconds")


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


def append_txt_record(file_path: str, record: dict[str, Any]) -> None:
    """
    用 JSON Lines 写入 .txt。
    每一行是一个 JSON 对象，但文件扩展名仍然是 .txt。
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")



def _task_index_file(context: dict[str, str]) -> Path:
    if context.get("task_index_file"):
        return Path(context["task_index_file"])
    if context.get("data_dir"):
        return Path(context["data_dir"]) / "task_index.txt"
    return Path(context["uncommit_file"]).parent / "task_index.txt"


def _coerce_task_index(value: Any) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _max_existing_task_index(records: list[dict[str, Any]]) -> int | None:
    indexes = [
        index
        for record in records
        for index in (_coerce_task_index(record.get("task_index") or record.get("编号")),)
        if index is not None
    ]
    return max(indexes) if indexes else None


def _read_next_task_index(context: dict[str, str], records: list[dict[str, Any]] | None = None) -> int:
    path = _task_index_file(context)
    if path.exists():
        try:
            index = int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            index = DEFAULT_TASK_INDEX_START
        return max(index, DEFAULT_TASK_INDEX_START)

    max_existing = _max_existing_task_index(records or [])
    if max_existing is not None:
        return max_existing + TASK_INDEX_STEP
    return DEFAULT_TASK_INDEX_START


def allocate_task_index(context: dict[str, str], records: list[dict[str, Any]] | None = None) -> int:
    """
    为一个新任务组分配排序编号。

    编号从 10000 开始，每次新任务增加 5；编号只用于排序，不参与任务合并。
    """
    index = _read_next_task_index(context, records)
    path = _task_index_file(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(index + TASK_INDEX_STEP), encoding="utf-8")
    return index


def _group_task_index(records: list[dict[str, Any]], group_id: str) -> int | None:
    for record in records:
        if str(record.get("task_group_id") or "") != group_id:
            continue
        index = _coerce_task_index(record.get("task_index") or record.get("编号"))
        if index is not None:
            return index
    return None

def find_active_session(records: list[dict[str, Any]]) -> int | None:
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if (
            record.get("type") == "session"
            and record.get("end_time") is None
            and record.get("committed") is False
        ):
            return index
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _display_time(value: str | None) -> str:
    return value or ""


def _print_fallback(title: str, rows: list[tuple[str, str]], note: str | None = None) -> None:
    print(title)
    if note:
        print(note)
    for label, value in rows:
        print(f"{label}: {value}")


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


def _task_detail_table(record: dict[str, Any], uncommit_file: str) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="white")

    table.add_row("任务名", str(record.get("task_name") or ""))
    if record.get("canonical_task_name"):
        table.add_row("合并任务名", str(record.get("canonical_task_name") or ""))
    table.add_row("开始时间", _display_time(record.get("start_time")))
    table.add_row("任务组 ID", str(record.get("task_group_id") or ""))
    table.add_row("Session ID", str(record.get("session_id") or ""))
    table.add_row("写入文件", uncommit_file)
    return table


def _render_started_task(record: dict[str, Any], uncommit_file: str) -> None:
    rows = [
        ("任务名", str(record.get("task_name") or "")),
        ("开始时间", _display_time(record.get("start_time"))),
        ("任务组 ID", str(record.get("task_group_id") or "")),
        ("Session ID", str(record.get("session_id") or "")),
        ("写入文件", uncommit_file),
    ]

    if not RICH_AVAILABLE:
        _print_fallback("已开始任务", rows)
        return

    header = Text("任务已开始", style="bold green")
    subtitle = Text("当前 session 已记录到 uncommit 文件", style="dim")
    body = Group(subtitle, "", _task_detail_table(record, uncommit_file))

    console.print(
        Panel(
            body,
            title=header,
            border_style="green",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_continued_task(record: dict[str, Any], uncommit_file: str, match: dict[str, Any]) -> None:
    if not RICH_AVAILABLE:
        _print_fallback(
            "已继续任务",
            [
                ("输入任务名", str(record.get("task_name") or "")),
                ("合并到", str(record.get("canonical_task_name") or record.get("task_name") or "")),
                ("开始时间", _display_time(record.get("start_time"))),
                ("任务组 ID", str(record.get("task_group_id") or "")),
                ("匹配方式", str(match.get("method") or "")),
                ("置信度", str(match.get("confidence") or "")),
                ("写入文件", uncommit_file),
            ],
            note=str(match.get("reason") or ""),
        )
        return

    table = _task_detail_table(record, uncommit_file)
    table.add_row("匹配方式", str(match.get("method") or ""))
    if match.get("confidence") is not None:
        table.add_row("置信度", str(match.get("confidence")))
    if match.get("reason"):
        table.add_row("匹配说明", str(match.get("reason")))

    body = Group(Text("已在未 commit 任务中选择最相似的任务组并追加新 session。", style="dim"), "", table)
    console.print(
        Panel(
            body,
            title=Text("任务已继续", style="bold green"),
            border_style="green",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _active_task_message(record: dict[str, Any]) -> str:
    return (
        "当前已有正在进行的任务，请先执行 log end。\n"
        f"任务名: {record.get('task_name')}\n"
        f"开始时间: {record.get('start_time')}\n"
        f"任务组 ID: {record.get('task_group_id')}"
    )


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


def _similarity(a: str, b: str) -> float:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0

    sequence_score = SequenceMatcher(None, a, b).ratio()
    a_chars = {c for c in a if not c.isspace()}
    b_chars = {c for c in b if not c.isspace()}
    overlap = len(a_chars & b_chars) / max(len(a_chars | b_chars), 1)
    return round(max(sequence_score, overlap), 4)


def _build_uncommitted_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "session" or record.get("committed") is not False:
            continue
        if not record.get("task_group_id"):
            continue

        group_id = str(record["task_group_id"])
        group = groups.setdefault(
            group_id,
            {
                "task_group_id": group_id,
                "canonical_task_name": record.get("canonical_task_name") or record.get("task_name") or "",
                "task_names": [],
                "sessions": [],
                "latest_time": "",
                "task_index": None,
            },
        )

        task_index = _coerce_task_index(record.get("task_index") or record.get("编号"))
        if task_index is not None and group.get("task_index") is None:
            group["task_index"] = task_index

        name = str(record.get("task_name") or "").strip()
        canonical = str(record.get("canonical_task_name") or "").strip()
        if canonical and canonical not in group["task_names"]:
            group["task_names"].append(canonical)
        if name and name not in group["task_names"]:
            group["task_names"].append(name)
        group["sessions"].append(
            {
                "session_id": record.get("session_id"),
                "task_name": name,
                "start_time": record.get("start_time"),
                "end_time": record.get("end_time"),
            }
        )
        latest = record.get("end_time") or record.get("start_time") or ""
        if latest > group.get("latest_time", ""):
            group["latest_time"] = latest
            group["canonical_task_name"] = canonical or name or group["canonical_task_name"]

    return sorted(groups.values(), key=lambda item: item.get("latest_time") or "", reverse=True)


def _choose_group_fallback(task_name: str, groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not groups:
        return None

    best_group: dict[str, Any] | None = None
    best_name = ""
    best_score = -1.0

    for group in groups:
        candidate_names = group.get("task_names") or [group.get("canonical_task_name") or ""]
        for candidate_name in candidate_names:
            score = _similarity(task_name, str(candidate_name))
            if score > best_score:
                best_group = group
                best_name = str(candidate_name)
                best_score = score

    if best_group is None:
        return None

    return {
        "task_group_id": best_group["task_group_id"],
        "canonical_task_name": best_group.get("canonical_task_name") or best_name or task_name,
        "matched_task_name": best_name,
        "confidence": best_score,
        "reason": "未检测到可用 LLM，已使用本地文本相似度选择最相近的未 commit 任务。",
        "method": "fallback_similarity",
        "task_index": best_group.get("task_index"),
    }


def choose_task_group_with_llm(task_name: str, groups: list[dict[str, Any]], context: dict[str, str]) -> dict[str, Any] | None:
    if not groups:
        return None

    compact_groups = [
        {
            "task_group_id": group["task_group_id"],
            "canonical_task_name": group.get("canonical_task_name") or "",
            "task_names": group.get("task_names") or [],
            "session_count": len(group.get("sessions") or []),
            "latest_time": group.get("latest_time") or "",
        }
        for group in groups
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个个人任务日志命令行工具里的匹配器。"
                "用户输入一个想继续记录的任务名，你需要从未 commit 的任务组中选择最适合合并的一个。"
                "只返回 JSON，不要返回 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "input_task_name": task_name,
                    "candidates": compact_groups,
                    "output_schema": {
                        "task_group_id": "必须是 candidates 中的一个 task_group_id",
                        "canonical_task_name": "合并后的任务名，通常使用候选任务组已有的主任务名",
                        "matched_task_name": "你认为匹配上的候选名称",
                        "confidence": "0 到 1 的数字",
                        "reason": "一句中文说明",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = _openai_json(messages, context)
    valid_group_ids = {group["task_group_id"] for group in groups}
    if result and result.get("task_group_id") in valid_group_ids:
        selected_group = next(
            (group for group in groups if group["task_group_id"] == result.get("task_group_id")),
            {},
        )
        return {
            "task_group_id": str(result.get("task_group_id")),
            "canonical_task_name": str(result.get("canonical_task_name") or task_name),
            "matched_task_name": str(result.get("matched_task_name") or ""),
            "confidence": result.get("confidence"),
            "reason": str(result.get("reason") or "LLM 已选择最相似的未 commit 任务。"),
            "method": "llm",
            "task_index": selected_group.get("task_index"),
        }

    return _choose_group_fallback(task_name, groups)


def _choose_latest_ended_group(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    log cont 不带任务名时使用。

    选择最近一次已经 log end、尚未 commit 的 session，直接沿用它的 task_group。
    这样用户可以在刚结束一个任务后输入 `log cont`，不必重复写任务名。
    """

    for record in reversed(records):
        if record.get("type") != "session" or record.get("committed") is not False:
            continue
        if not record.get("end_time"):
            continue

        group_id = str(record.get("task_group_id") or record.get("session_id") or "").strip()
        if not group_id:
            continue

        canonical_task_name = str(
            record.get("canonical_task_name")
            or record.get("task_name")
            or "未命名任务"
        ).strip() or "未命名任务"
        task_name = str(record.get("task_name") or canonical_task_name).strip() or canonical_task_name

        return {
            "task_group_id": group_id,
            "canonical_task_name": canonical_task_name,
            "matched_task_name": task_name,
            "confidence": 1.0,
            "reason": "未提供任务名，已自动继续最近一次 log end 结束的任务。",
            "method": "latest_ended",
            "source_session_id": record.get("session_id") or "",
            "source_end_time": record.get("end_time") or "",
            "task_index": _coerce_task_index(record.get("task_index") or record.get("编号")),
        }

    return None


def start_task(task_name: str, context: dict[str, str]) -> int:
    """
    处理:
      log start 任务名
    """

    task_name = task_name.strip()
    if not task_name:
        _render_error("任务名为空", "请提供任务名，例如: log start 写代码")
        return 2

    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is not None:
        _render_error("已有正在进行的任务", _active_task_message(records[active_index]))
        return 1

    task_index = allocate_task_index(context, records)
    now = now_iso(context)
    record = {
        "type": "session",
        "task_group_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "command": "start",
        "task_name": task_name,
        "canonical_task_name": task_name,
        "start_time": now,
        "end_time": None,
        "committed": False,
        "task_index": task_index,
        "created_at": now,
        "updated_at": now,
    }

    append_txt_record(context["uncommit_file"], record)
    _render_started_task(record, context["uncommit_file"])
    return 0


def cont_task(task_name: str, context: dict[str, str]) -> int:
    """
    处理:
      log cont [任务名]

    行为:
    - 如果提供任务名：在当前未 commit 的任务组里找最相似的一组继续记录
    - 如果不提供任务名：直接继续最近一次已经 log end、尚未 commit 的任务
    - 如果提供任务名但没有任何未 commit 任务组，降级为 start_task
    """

    task_name = task_name.strip()

    records = read_txt_records(context["uncommit_file"])
    active_index = find_active_session(records)
    if active_index is not None:
        _render_error("已有正在进行的任务", _active_task_message(records[active_index]))
        return 1

    if not task_name:
        match = _choose_latest_ended_group(records)
        if match is None:
            _render_error(
                "没有可继续的任务",
                "没有找到最近已结束且未 commit 的任务；请使用 log cont 任务名 或 log start 任务名。",
            )
            return 1
        task_name = str(match.get("matched_task_name") or match.get("canonical_task_name") or "未命名任务")
    else:
        groups = _build_uncommitted_groups(records)
        match = choose_task_group_with_llm(task_name, groups, context)
        if match is None:
            return start_task(task_name=task_name, context=context)

    task_index = _coerce_task_index(match.get("task_index"))
    if task_index is None:
        existing_index = _group_task_index(records, str(match.get("task_group_id") or ""))
        task_index = existing_index if existing_index is not None else allocate_task_index(context, records)

    now = now_iso(context)
    record = {
        "type": "session",
        "task_group_id": match["task_group_id"],
        "session_id": str(uuid.uuid4()),
        "command": "cont",
        "task_name": task_name,
        "canonical_task_name": match.get("canonical_task_name") or task_name,
        "matched_task_name": match.get("matched_task_name") or "",
        "start_time": now,
        "end_time": None,
        "committed": False,
        "task_index": task_index,
        "created_at": now,
        "updated_at": now,
        "cont_match": match,
    }

    append_txt_record(context["uncommit_file"], record)
    _render_continued_task(record, context["uncommit_file"], match)
    return 0
