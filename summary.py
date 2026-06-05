from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
import json
import os
import re
import sys
import urllib.error
import urllib.parse
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


DEFAULT_OPENAI_MODEL = "gpt-5.4"
NOTION_VERSION = "2022-06-28"
COMMIT_JSON_START = "<!-- guiyuniban_commit_json"
COMMIT_JSON_END = "guiyuniban_commit_json -->"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# 用户需求: 早上 7:00 及以前都算前一天，7:01 开始算当天。
DAY_BOUNDARY = time(hour=7, minute=0, second=0)


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
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_text(file_path: str, content: str) -> None:
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text(content, encoding="utf-8")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_hours(hours: float) -> str:
    text = f"{hours:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_push_hours(hours: float) -> str:
    """
    Notion push 展示用时长格式：数字后紧跟大写 H，例如 1.5H。
    commit 预览仍保留纯数字，避免影响机器可读 payload。
    """
    return f"{_format_hours(hours)}H"


def _format_duration_hours(start_time: str | None, end_time: str | None) -> float | None:
    start = _parse_dt(start_time)
    end = _parse_dt(end_time)
    if start is None or end is None:
        return None
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return None
    return round(seconds / 3600, 2)


def _format_duration(start_time: str | None, end_time: str | None) -> str:
    hours = _format_duration_hours(start_time, end_time)
    if hours is None:
        return "未结束"
    whole_hours = int(hours)
    minutes = int(round((hours - whole_hours) * 60))
    if whole_hours and minutes:
        return f"{whole_hours} 小时 {minutes} 分钟"
    if whole_hours:
        return f"{whole_hours} 小时"
    return f"{minutes} 分钟"


def _effective_weekday(dt: datetime) -> str:
    effective = dt
    if dt.timetz().replace(tzinfo=None) <= DAY_BOUNDARY:
        effective = dt - timedelta(days=1)
    return WEEKDAYS[effective.weekday()]


def _session_weekday(record: dict[str, Any]) -> str:
    start = _parse_dt(record.get("start_time"))
    if start is None:
        return "未知"
    return _effective_weekday(start)


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
        with urllib.request.urlopen(request, timeout=45) as response:
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


def _notion_token(context: dict[str, str]) -> str:
    config = read_config(context.get("config_file"))
    return os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY") or config.get("notion_token") or ""


def _notion_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"https://api.notion.com/v1{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Notion API 请求失败 {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Notion API 请求失败: {exc}") from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Notion API 响应不是合法 JSON: {exc}") from exc


def _plain_text_from_rich_text(rich_text: list[dict[str, Any]] | None) -> str:
    if not rich_text:
        return ""
    return "".join(str(part.get("plain_text") or part.get("text", {}).get("content") or "") for part in rich_text)


def _block_to_text(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    if not block_type:
        return ""

    value = block.get(block_type) or {}
    text = _plain_text_from_rich_text(value.get("rich_text"))
    if text:
        prefix_map = {
            "heading_1": "# ",
            "heading_2": "## ",
            "heading_3": "### ",
            "bulleted_list_item": "- ",
            "numbered_list_item": "- ",
            "to_do": "- ",
            "quote": "> ",
        }
        return f"{prefix_map.get(block_type, '')}{text}"

    if block_type == "child_page":
        return f"# {value.get('title') or ''}".strip()
    if block_type == "child_database":
        return f"# {value.get('title') or ''}".strip()
    return ""


def _fetch_block_children_text(block_id: str, token: str, depth: int = 0, max_depth: int = 3) -> list[str]:
    if depth > max_depth:
        return []

    lines: list[str] = []
    cursor = ""
    while True:
        query = "?page_size=100"
        if cursor:
            query += "&start_cursor=" + urllib.parse.quote(cursor)
        data = _notion_request("GET", f"/blocks/{block_id}/children{query}", token)
        for block in data.get("results", []):
            text = _block_to_text(block)
            if text:
                lines.append(text)
            if block.get("has_children"):
                lines.extend(_fetch_block_children_text(block["id"], token, depth + 1, max_depth))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    return lines


def _search_notion_page(title: str, token: str) -> str:
    payload = {
        "query": title,
        "filter": {"property": "object", "value": "page"},
        "page_size": 10,
    }
    data = _notion_request("POST", "/search", token, payload)
    for item in data.get("results", []):
        if item.get("object") == "page" and item.get("id"):
            return str(item["id"])
    return ""


def read_task_categories_from_notion(context: dict[str, str]) -> str:
    """
    访问 Notion 中叫做“任务分类”的页面，读取所有任务分类的细节说明。

    配置优先级:
    - 环境变量 NOTION_TASK_CATEGORY_PAGE_ID
    - config.txt 中 notion_task_category_page_id
    - 如果都没有，则用 Notion Search API 搜索标题“任务分类”

    注意：commit 依赖这份分类信息。读取失败时抛出 RuntimeError，
    由 commit_tasks 终止本次 commit，避免生成类别不可靠的预览。
    """

    config = read_config(context.get("config_file"))
    token = _notion_token(context)
    if not token:
        raise RuntimeError(
            "缺少 Notion token。请设置环境变量 NOTION_TOKEN / NOTION_API_KEY，"
            "或在 config.txt 中设置 notion_token=...。"
        )

    page_id = os.getenv("NOTION_TASK_CATEGORY_PAGE_ID") or config.get("notion_task_category_page_id") or ""
    if not page_id:
        page_id = _search_notion_page("任务分类", token)
    if not page_id:
        raise RuntimeError(
            "未找到 Notion 页面“任务分类”。请设置环境变量 NOTION_TASK_CATEGORY_PAGE_ID，"
            "或在 config.txt 中设置 notion_task_category_page_id=...。"
        )

    lines = _fetch_block_children_text(page_id, token)
    category_text = "\n".join(line for line in lines if line.strip()).strip()
    if not category_text:
        raise RuntimeError("Notion 页面“任务分类”内容为空，无法进行可靠分类。")
    return category_text


def _extract_category_names(category_text: str) -> list[str]:
    names: list[str] = []
    for raw_line in category_text.splitlines():
        line = raw_line.strip(" -#\t")
        if not line:
            continue
        line = re.split(r"[:：—-]", line, maxsplit=1)[0].strip()
        if line and len(line) <= 30 and line not in names:
            names.append(line)
    return names


def _fallback_category(task_name: str, category_text: str) -> str:
    category_names = _extract_category_names(category_text)
    if not category_names:
        return "未分类"

    lowered_task = task_name.lower()
    best = category_names[0]
    best_score = -1
    for category in category_names:
        score = 0
        if category.lower() in lowered_task:
            score += 100
        for char in set(category):
            if char and char in task_name:
                score += 1
        if score > best_score:
            best_score = score
            best = category
    return best if best_score > 0 else "未分类"


def _build_base_commit_items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    把未 commit 且已结束的 session 整理为 task_group + 周几 维度的基础汇总。

    如果一个 task_group 跨多个有效周几，会拆成多行，避免把“周几”变成含糊的列表。
    """

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "session" or record.get("committed") is not False:
            continue

        start = _parse_dt(record.get("start_time"))
        end = _parse_dt(record.get("end_time"))
        if start is None or end is None or end < start:
            continue

        group_id = str(record.get("task_group_id") or record.get("session_id") or uuid.uuid4())
        weekday = _session_weekday(record)
        key = (group_id, weekday)
        duration_hours = round((end - start).total_seconds() / 3600, 4)

        bucket = buckets.setdefault(
            key,
            {
                "source_key": f"{group_id}:{weekday}",
                "任务名": record.get("canonical_task_name") or record.get("task_name") or "未命名任务",
                "周几": weekday,
                "持续小时": 0.0,
                "类别": "未分类",
                "task_group_id": group_id,
                "source_session_ids": [],
                "session_names": [],
                "start_time": record.get("start_time"),
                "end_time": record.get("end_time"),
            },
        )

        bucket["持续小时"] = round(float(bucket["持续小时"]) + duration_hours, 2)
        if record.get("session_id"):
            bucket["source_session_ids"].append(record["session_id"])
        name = str(record.get("task_name") or "").strip()
        if name and name not in bucket["session_names"]:
            bucket["session_names"].append(name)
        if record.get("start_time") and (not bucket.get("start_time") or record["start_time"] < bucket["start_time"]):
            bucket["start_time"] = record["start_time"]
        if record.get("end_time") and (not bucket.get("end_time") or record["end_time"] > bucket["end_time"]):
            bucket["end_time"] = record["end_time"]

    return sorted(buckets.values(), key=lambda item: (item.get("start_time") or "", item.get("任务名") or ""))


def classify_tasks_with_llm(records: list[dict[str, Any]], category_text: str, context: dict[str, str]) -> list[dict[str, Any]]:
    """
    使用 LLM 将所有 uncommit 任务整理成四个核心属性:
    - 任务名
    - 周几
    - 持续小时
    - 类别

    LLM 不负责计算时间；时间和周几先由本地确定，LLM 负责根据 Notion 分类说明归类、润色任务名。
    """

    base_items = _build_base_commit_items(records)
    if not base_items:
        return []

    compact_items = [
        {
            "source_key": item["source_key"],
            "任务名": item["任务名"],
            "周几": item["周几"],
            "持续小时": item["持续小时"],
            "session_names": item.get("session_names") or [],
        }
        for item in base_items
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是个人任务日志整理器。你会收到任务记录和 Notion 页面中的任务分类说明。"
                "请保持 source_key、周几、持续小时不变，只能润色任务名并选择最合适类别。"
                "必须返回 JSON，不要返回 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_category_text_from_notion": category_text or "未提供分类说明；无法判断时类别写未分类。",
                    "time_rule": "早上 7:00 及以前计入前一天；7:01 起计入当天。周几已由程序按该规则算好。",
                    "items": compact_items,
                    "output_schema": {
                        "items": [
                            {
                                "source_key": "必须等于输入中的 source_key",
                                "任务名": "精简后的任务名",
                                "周几": "必须等于输入中的周几",
                                "持续小时": "必须等于输入中的持续小时",
                                "类别": "从任务分类说明中选择；不确定则写未分类",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = _openai_json(messages, context)
    by_key = {item["source_key"]: item for item in base_items}
    if result and isinstance(result.get("items"), list):
        for classified in result["items"]:
            if not isinstance(classified, dict):
                continue
            source_key = classified.get("source_key")
            if source_key not in by_key:
                continue
            item = by_key[source_key]
            if classified.get("任务名"):
                item["任务名"] = str(classified["任务名"])
            if classified.get("类别"):
                item["类别"] = str(classified["类别"])
            # 安全起见，周几和持续小时使用本地计算结果，不采纳 LLM 修改。

    else:
        for item in base_items:
            item["类别"] = _fallback_category(str(item.get("任务名") or ""), category_text)

    return base_items


def _build_commit_preview_text(items: list[dict[str, Any]], commit_payload: dict[str, Any]) -> str:
    preview_lines = [
        "# 本次 commit 预览",
        "",
        "| 任务名 | 周几 | 持续小时 | 类别 |",
        "|---|---:|---:|---|",
    ]

    for item in items:
        preview_lines.append(
            f"| {item.get('任务名', '')} | {item.get('周几', '')} | {_format_hours(float(item.get('持续小时') or 0))} | {item.get('类别', '')} |"
        )

    preview_lines.extend(
        [
            "",
            "## 明细",
            "",
        ]
    )

    for index, item in enumerate(items, start=1):
        preview_lines.extend(
            [
                f"{index}. {item.get('任务名', '')}",
                f"   - 周几: {item.get('周几', '')}",
                f"   - 持续小时: {_format_hours(float(item.get('持续小时') or 0))}",
                f"   - 类别: {item.get('类别', '')}",
                f"   - 任务组 ID: {item.get('task_group_id', '')}",
                f"   - Session IDs: {', '.join(map(str, item.get('source_session_ids') or []))}",
                "",
            ]
        )

    preview_lines.extend(
        [
            COMMIT_JSON_START,
            json.dumps(commit_payload, ensure_ascii=False, indent=2),
            COMMIT_JSON_END,
            "",
        ]
    )

    return "\n".join(preview_lines)


def _extract_commit_payload(preview_text: str) -> dict[str, Any] | None:
    start_index = preview_text.find(COMMIT_JSON_START)
    end_index = preview_text.find(COMMIT_JSON_END)
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        return None
    json_text = preview_text[start_index + len(COMMIT_JSON_START):end_index].strip()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _render_empty_state(message: str) -> None:
    if not RICH_AVAILABLE:
        print(message)
        return

    console.print(
        Panel(
            Text(message, style="dim"),
            title=Text("无可处理任务", style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        )
    )


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
            Text(message, style="yellow"),
            title=Text(title, style="bold yellow"),
            border_style="yellow",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_commit_preview(items: list[dict[str, Any]], preview_file: str, category_text: str) -> None:
    if not RICH_AVAILABLE:
        print(f"commit 预览已写入: {preview_file}")
        for item in items:
            print(f"{item.get('任务名')} | {item.get('周几')} | {_format_hours(float(item.get('持续小时') or 0))} | {item.get('类别')}")
        return

    table = Table(
        "#",
        "任务名",
        "周几",
        "持续小时",
        "类别",
        "任务组 ID",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
    )

    for index, item in enumerate(items, start=1):
        table.add_row(
            str(index),
            str(item.get("任务名") or ""),
            str(item.get("周几") or ""),
            _format_hours(float(item.get("持续小时") or 0)),
            str(item.get("类别") or ""),
            str(item.get("task_group_id") or ""),
        )

    footer = Table.grid(padding=(0, 2))
    footer.add_column(style="bold cyan", no_wrap=True)
    footer.add_column(style="white")
    footer.add_row("任务数", str(len(items)))
    footer.add_row("预览文件", preview_file)
    footer.add_row("任务分类来源", "Notion" if category_text else "未读取到 Notion 分类，已用未分类/本地兜底")
    footer.add_row("周几规则", "07:00 及以前算前一天；07:01 起算当天")

    body = Group(table, "", footer)
    console.print(
        Panel(
            body,
            title=Text("本次 commit 预览", style="bold green"),
            border_style="green",
            box=box.ROUNDED,
        )
    )


def _find_unfinished_sessions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("type") == "session"
        and record.get("committed") is False
        and record.get("end_time") is None
    ]


def commit_tasks(context: dict[str, str]) -> int:
    """
    处理:
      log commit

    目标:
    - 读取 uncommit_tasks.txt
    - 整理未 commit 且已结束的任务
    - 从 Notion 读取“任务分类”说明
    - 用 LLM 分类
    - 输出预览
    - 写入 commit_preview.txt
    """

    records = read_txt_records(context["uncommit_file"])

    if not records:
        _render_empty_state("当前没有未 commit 的任务。")
        return 0

    uncommitted = [
        record
        for record in records
        if record.get("type") == "session" and record.get("committed") is False
    ]
    if not uncommitted:
        _render_empty_state("当前没有未 commit 的任务。")
        return 0

    unfinished = _find_unfinished_sessions(records)
    if unfinished:
        names = ", ".join(str(record.get("task_name") or "未命名任务") for record in unfinished)
        _render_error("仍有任务未结束", f"请先执行 log end 再 commit。未结束任务: {names}")
        return 1

    try:
        category_text = read_task_categories_from_notion(context)
    except RuntimeError as exc:
        _render_error("Notion 分类读取失败", f"已终止 commit，未生成 commit 预览。\n{exc}")
        return 1

    items = classify_tasks_with_llm(uncommitted, category_text, context)
    if not items:
        _render_empty_state("当前没有可 commit 的已结束任务。")
        return 0

    commit_payload = {
        "commit_id": str(uuid.uuid4()),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
    }
    preview = _build_commit_preview_text(items, commit_payload)
    write_text(context["commit_preview_file"], preview)

    _render_commit_preview(items, context["commit_preview_file"], category_text)
    return 0


def _text_to_rich_text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _append_blocks_to_page(page_id: str, blocks: list[dict[str, Any]], token: str) -> None:
    for start in range(0, len(blocks), 100):
        chunk = blocks[start:start + 100]
        _notion_request("PATCH", f"/blocks/{page_id}/children", token, {"children": chunk})


_VALID_NOTION_COLORS = {
    "default",
    "gray",
    "brown",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "red",
    "gray_background",
    "brown_background",
    "orange_background",
    "yellow_background",
    "green_background",
    "blue_background",
    "purple_background",
    "pink_background",
    "red_background",
}

_RICH_TEXT_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
}

_SUPPORTED_LLM_BLOCK_TYPES = _RICH_TEXT_BLOCK_TYPES | {"divider", "table", "table_row"}


def _normalize_notion_id(value: str | None) -> str:
    """
    支持在 config/env 中填写 Notion 原始 ID 或页面 URL。
    """

    raw = (value or "").strip()
    if not raw:
        return ""

    dashed = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    )
    if dashed:
        return dashed.group(0)

    compact_matches = re.findall(r"[0-9a-fA-F]{32}", raw)
    if compact_matches:
        compact = compact_matches[-1]
        return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"

    return raw


def _resolve_config_value(*values: str | None) -> str:
    for value in values:
        normalized = _normalize_notion_id(value)
        if normalized:
            return normalized
    return ""


def _page_title_from_page_data(page_data: dict[str, Any]) -> str:
    for prop in (page_data.get("properties") or {}).values():
        if not isinstance(prop, dict):
            continue
        if prop.get("type") == "title":
            title = _plain_text_from_rich_text(prop.get("title"))
            if title:
                return title
    return ""


def _rich_text_preview(rich_text: list[dict[str, Any]] | None, limit: int = 500) -> str:
    return _preview_text(_plain_text_from_rich_text(rich_text), limit=limit)


def _table_cells_preview(cells: list[Any] | None) -> list[str]:
    if not cells:
        return []
    preview: list[str] = []
    for cell in cells:
        if isinstance(cell, list):
            preview.append(_rich_text_preview(cell, limit=160))
        else:
            preview.append(_preview_text(str(cell), limit=160))
    return preview


def _simplify_block_for_format(block: dict[str, Any]) -> dict[str, Any]:
    block_type = str(block.get("type") or "")
    value = block.get(block_type) if block_type else None
    if not isinstance(value, dict):
        value = {}

    simplified: dict[str, Any] = {"type": block_type or "unknown"}
    text = _rich_text_preview(value.get("rich_text"), limit=500)
    if text:
        simplified["text"] = text

    if block_type == "child_page":
        title = str(value.get("title") or "").strip()
        if title:
            simplified["title"] = title
            simplified["text"] = f"子页: {title}"

    if block_type == "child_database":
        title = str(value.get("title") or "").strip()
        if title:
            simplified["title"] = title
            simplified["text"] = f"子数据库: {title}"

    for key in ("color", "checked", "language", "has_column_header", "has_row_header", "table_width"):
        if key in value:
            simplified[key] = value[key]

    if block_type == "table_row":
        simplified["cells"] = _table_cells_preview(value.get("cells"))

    if block.get("has_children"):
        simplified["has_children"] = True

    return simplified


def _fetch_top_level_blocks(block_id: str, token: str, max_blocks: int = 500) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cursor = ""
    while len(blocks) < max_blocks:
        page_size = min(100, max_blocks - len(blocks))
        query = f"?page_size={page_size}"
        if cursor:
            query += "&start_cursor=" + urllib.parse.quote(cursor)
        data = _notion_request("GET", f"/blocks/{block_id}/children{query}", token)
        results = data.get("results") or []
        blocks.extend(block for block in results if isinstance(block, dict))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor") or ""
        if not cursor:
            break
    return blocks


def _truncate_format_blocks(blocks: list[dict[str, Any]], max_blocks: int = 120) -> list[dict[str, Any]]:
    if len(blocks) <= max_blocks:
        return blocks

    head_count = min(25, max_blocks // 3)
    tail_count = max_blocks - head_count - 1
    omitted = len(blocks) - head_count - tail_count
    return (
        blocks[:head_count]
        + [{"type": "omitted_blocks", "text": f"中间省略 {omitted} 个 block"}]
        + blocks[-tail_count:]
    )


def _fetch_page_format_sample(
    page_id: str,
    token: str,
    *,
    max_top_level_blocks: int = 500,
    max_sample_blocks: int = 120,
    max_child_blocks: int = 20,
) -> dict[str, Any]:
    """
    读取目标 Notion 页现有内容，让 LLM 根据真实页面格式生成 push blocks。

    只把 block 的类型、文本、颜色、勾选状态、表格结构等格式相关字段交给 LLM，
    不把 Notion 内部 id / created_time / last_edited_time 传给 LLM。
    """

    page_data = _notion_request("GET", f"/pages/{page_id}", token)
    raw_blocks = _fetch_top_level_blocks(page_id, token, max_blocks=max_top_level_blocks)
    retained_blocks = _truncate_format_blocks(raw_blocks, max_blocks=max_sample_blocks)

    simplified_blocks: list[dict[str, Any]] = []
    for raw_block in retained_blocks:
        if raw_block.get("type") == "omitted_blocks":
            simplified_blocks.append(raw_block)
            continue

        simplified = _simplify_block_for_format(raw_block)
        if raw_block.get("has_children") and raw_block.get("id"):
            child_blocks = _fetch_top_level_blocks(str(raw_block["id"]), token, max_blocks=max_child_blocks)
            if child_blocks:
                simplified["children"] = [_simplify_block_for_format(child) for child in child_blocks]
        simplified_blocks.append(simplified)

    plain_lines: list[str] = []
    for block in simplified_blocks:
        block_type = block.get("type")
        text = str(block.get("text") or "")
        if block_type == "table_row" and block.get("cells"):
            text = " | ".join(map(str, block.get("cells") or []))
        if text:
            plain_lines.append(f"{block_type}: {text}")

    return {
        "page_title": _page_title_from_page_data(page_data),
        "sampled_top_level_block_count": len(simplified_blocks),
        "source_top_level_block_count_limit": max_top_level_blocks,
        "format_blocks": simplified_blocks,
        "plain_text_outline": "\n".join(plain_lines[-80:]),
    }




def _read_int_config(
    context: dict[str, str],
    env_name: str,
    config_name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 8,
) -> int:
    config = read_config(context.get("config_file"))
    raw = os.getenv(env_name) or config.get(config_name) or str(default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _get_push_max_explore_steps(context: dict[str, str]) -> int:
    """
    AI 选择 Notion push 落点时最多允许探索多少轮。

    配置优先级:
    - 环境变量 NOTION_PUSH_MAX_EXPLORE_STEPS
    - config.txt 中 notion_push_max_explore_steps
    - 默认 3

    设为 0 时完全不探索子页，直接写入当前页。
    """

    return _read_int_config(
        context,
        "NOTION_PUSH_MAX_EXPLORE_STEPS",
        "notion_push_max_explore_steps",
        3,
        minimum=0,
        maximum=8,
    )


def _extract_child_page_candidates(raw_blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for block in raw_blocks:
        if block.get("type") != "child_page" or not block.get("id"):
            continue
        child_page = block.get("child_page") or {}
        if not isinstance(child_page, dict):
            child_page = {}
        title = str(child_page.get("title") or "未命名子页").strip() or "未命名子页"
        candidates.append(
            {
                "child_index": str(len(candidates) + 1),
                "page_id": str(block["id"]),
                "title": title,
            }
        )
    return candidates


def _fetch_push_page_state(page_id: str, token: str) -> dict[str, Any]:
    """
    读取当前候选页，让 AI 决定是写入当前页，还是继续探索某个 child_page。

    page_id 只在程序内部保存；传给 LLM 的 child page 只暴露 child_index + title，
    避免模型直接拼 Notion API 参数。
    """

    page_data = _notion_request("GET", f"/pages/{page_id}", token)
    raw_blocks = _fetch_top_level_blocks(page_id, token, max_blocks=160)
    child_pages = _extract_child_page_candidates(raw_blocks)
    sample_blocks = _truncate_format_blocks(raw_blocks, max_blocks=60)
    simplified_blocks = [_simplify_block_for_format(block) for block in sample_blocks]

    plain_lines: list[str] = []
    for block in simplified_blocks:
        block_type = str(block.get("type") or "")
        text = str(block.get("text") or block.get("title") or "")
        if block_type == "table_row" and block.get("cells"):
            text = " | ".join(map(str, block.get("cells") or []))
        if text:
            plain_lines.append(f"{block_type}: {text}")

    return {
        "page_id": page_id,
        "page_title": _page_title_from_page_data(page_data) or "未命名页面",
        "top_level_block_count_sampled": len(simplified_blocks),
        "child_pages": child_pages,
        "format_blocks": simplified_blocks,
        "plain_text_outline": "\n".join(plain_lines[-80:]),
    }


def _safe_short_page_id(page_id: str) -> str:
    text = str(page_id or "")
    return text[:8] + "…" if len(text) > 8 else text


def _render_ai_push_action(
    step: int,
    max_steps: int,
    page_state: dict[str, Any],
    decision: dict[str, Any],
    *,
    selected_child_title: str = "",
    note: str = "",
) -> None:
    action = str(decision.get("action") or "write_current")
    action_label = "写入当前页" if action == "write_current" else "继续探索子页"
    page_title = str(page_state.get("page_title") or "未命名页面")
    page_id = _safe_short_page_id(str(page_state.get("page_id") or ""))
    page_kind = str(decision.get("page_kind") or "unknown")
    observation = str(decision.get("observation") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    confidence = decision.get("confidence")
    confidence_text = ""
    if isinstance(confidence, (int, float)):
        confidence_text = f"{float(confidence):.2f}"

    if not RICH_AVAILABLE:
        print(f"AI 探索 Notion 落点 {step}/{max_steps}")
        print(f"页面: {page_title} ({page_id})")
        print(f"判断: {page_kind}")
        print(f"动作: {action_label}")
        if selected_child_title:
            print(f"子页: {selected_child_title}")
        if confidence_text:
            print(f"置信度: {confidence_text}")
        if observation:
            print(f"观察: {observation}")
        if reason:
            print(f"理由: {reason}")
        if note:
            print(f"备注: {note}")
        return

    border_style = "cyan" if action == "explore_child" else "green"
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    # 第二列必须 fold 换行，不能使用 Rich 默认 ellipsis，否则长观察/理由会显示为“.../…”
    table.add_column(style="white", overflow="fold", no_wrap=False, ratio=1)
    table.add_row("当前页", f"{page_title} ({page_id})")
    table.add_row("页面判断", page_kind)
    table.add_row("AI 动作", action_label)
    if selected_child_title:
        table.add_row("探索子页", selected_child_title)
    if confidence_text:
        table.add_row("置信度", confidence_text)
    if observation:
        table.add_row("观察", Text(observation, style="white", overflow="fold", no_wrap=False))
    if reason:
        table.add_row("理由", Text(reason, style="white", overflow="fold", no_wrap=False))
    if note:
        table.add_row("备注", Text(note, style="white", overflow="fold", no_wrap=False))

    console.print(
        Panel(
            table,
            title=Text(f"AI 探索 Notion 落点 {step}/{max_steps}", style=f"bold {border_style}"),
            border_style=border_style,
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_push_target_selected(page_state: dict[str, Any], blocks_count: int | None = None) -> None:
    page_title = str(page_state.get("page_title") or "未命名页面")
    page_id = _safe_short_page_id(str(page_state.get("page_id") or ""))
    suffix = f"\n待追加 blocks: {blocks_count}" if blocks_count is not None else ""
    _render_info("Notion push 落点", f"最终写入页面: {page_title} ({page_id}){suffix}")


def _coerce_explore_decision(result: dict[str, Any] | None, fallback_reason: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "page_kind": "unknown",
            "observation": "AI 未返回有效 JSON 决策。",
            "action": "write_current",
            "reason": fallback_reason,
            "confidence": 0,
        }

    action = str(result.get("action") or "write_current").strip()
    if action not in {"write_current", "explore_child"}:
        action = "write_current"

    decision = {
        "page_kind": str(result.get("page_kind") or "unknown"),
        "observation": str(result.get("observation") or ""),
        "action": action,
        "reason": str(result.get("reason") or fallback_reason),
        "confidence": result.get("confidence", 0),
    }
    if result.get("child_index") is not None:
        decision["child_index"] = result.get("child_index")
    return decision


def _decide_next_push_page(
    page_state: dict[str, Any],
    items: list[dict[str, Any]],
    commit_id: str,
    context: dict[str, str],
    *,
    step: int,
    max_steps: int,
    visited_titles: list[str],
) -> dict[str, Any]:
    child_pages_for_llm = [
        {"child_index": child["child_index"], "title": child["title"]}
        for child in page_state.get("child_pages") or []
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Notion 任务记录 push 落点选择器。你每一轮只能做一个动作: "
                "write_current 或 explore_child。当前页永远可以作为 fallback 写入目标，"
                "即使格式不完全正确也可以写入当前页。只有当当前页明显是目录页、总览页、"
                "日/周/月索引页，并且某个子页标题明显更像本次 commit 的精确落点时，才选择 explore_child。"
                "如果当前页看起来就是任务记录页，必须 write_current。若不确定，必须 write_current。"
                "如果没有子页，必须 write_current。不要为了追求完美格式而过度探索。必须返回 JSON object。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loop": {
                        "step": step,
                        "max_steps": max_steps,
                        "is_last_step": step >= max_steps,
                        "visited_page_titles": visited_titles,
                    },
                    "current_page": {
                        "title": page_state.get("page_title"),
                        "sampled_block_count": page_state.get("top_level_block_count_sampled"),
                        "plain_text_outline": page_state.get("plain_text_outline"),
                        "format_blocks": page_state.get("format_blocks"),
                        "child_pages": child_pages_for_llm,
                    },
                    "commit": {
                        "commit_id": commit_id,
                        "items": _compact_commit_items_for_llm(items),
                    },
                    "decision_rules": [
                        "write_current 永远是安全动作；当前页是 fallback 目标。",
                        "当前页像任务记录页、时间日志页、日报页、周报页、任务详情页时，选择 write_current。",
                        "当前页格式不对或看不懂时，也选择 write_current，不要失败。",
                        "只有当前页明显像目录/总览/日周月索引，并且 child_pages 中有明显更精确落点，才选择 explore_child。",
                        "如果 is_last_step=true，必须选择 write_current，不能继续探索。",
                        "如果选择 explore_child，child_index 必须来自 child_pages。",
                    ],
                    "output_schema": {
                        "page_kind": "task_record | directory_or_index | wrong_format | empty | unknown",
                        "observation": "一句话说明看到了什么格式",
                        "action": "write_current | explore_child",
                        "child_index": "仅当 action=explore_child 时填写 child_pages 中的编号",
                        "reason": "一句话说明为什么这样做",
                        "confidence": "0 到 1 的数字",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    return _coerce_explore_decision(
        _openai_json(messages, context),
        "为了保证 push 不被卡住，回退到当前页写入。",
    )


def _select_push_target_page_with_llm(
    initial_page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> dict[str, Any]:
    max_steps = _get_push_max_explore_steps(context)
    current_page_id = initial_page_id
    visited_page_ids: set[str] = set()
    visited_titles: list[str] = []

    if max_steps <= 0:
        page_state = _fetch_push_page_state(current_page_id, token)
        _render_ai_push_action(
            0,
            0,
            page_state,
            {
                "page_kind": "not_checked",
                "observation": "配置将 Notion push 探索步数设为 0。",
                "action": "write_current",
                "reason": "按配置直接写入当前页。",
                "confidence": 1,
            },
        )
        return page_state

    last_state: dict[str, Any] | None = None
    for step in range(1, max_steps + 1):
        page_state = _fetch_push_page_state(current_page_id, token)
        last_state = page_state
        visited_page_ids.add(current_page_id)
        title = str(page_state.get("page_title") or "未命名页面")
        if title not in visited_titles:
            visited_titles.append(title)

        decision = _decide_next_push_page(
            page_state,
            items,
            commit_id,
            context,
            step=step,
            max_steps=max_steps,
            visited_titles=visited_titles,
        )

        child_pages = page_state.get("child_pages") or []
        child_by_index = {str(child.get("child_index")): child for child in child_pages}
        chosen_child: dict[str, str] | None = None
        if decision.get("child_index") is not None:
            chosen_child = child_by_index.get(str(decision.get("child_index")))

        if decision.get("action") != "explore_child":
            _render_ai_push_action(step, max_steps, page_state, decision)
            return page_state

        if step >= max_steps:
            decision["action"] = "write_current"
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note="AI 想继续探索，但已经达到探索上限；改为写入当前页。",
            )
            return page_state

        if not chosen_child:
            decision["action"] = "write_current"
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note="AI 选择的 child_index 不存在；按 fallback 规则写入当前页。",
            )
            return page_state

        child_id = str(chosen_child.get("page_id") or "")
        child_title = str(chosen_child.get("title") or "未命名子页")
        if not child_id or child_id in visited_page_ids:
            decision["action"] = "write_current"
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                selected_child_title=child_title,
                note="AI 选择的子页为空或已经访问过；按 fallback 规则写入当前页。",
            )
            return page_state

        _render_ai_push_action(
            step,
            max_steps,
            page_state,
            decision,
            selected_child_title=child_title,
        )
        current_page_id = child_id

    if last_state is None:
        last_state = _fetch_push_page_state(current_page_id, token)
    _render_info(
        "Notion push 探索结束",
        f"已达到 {max_steps} 步探索上限；按 fallback 规则写入当前页: {last_state.get('page_title') or '未命名页面'}。",
    )
    return last_state


def _split_rich_text_content(content: str) -> list[dict[str, Any]]:
    text = str(content or "")
    if not text:
        return []
    return [
        {"type": "text", "text": {"content": text[index:index + 2000]}}
        for index in range(0, len(text), 2000)
    ]


def _sanitize_annotations(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    annotations: dict[str, Any] = {}
    for key in ("bold", "italic", "strikethrough", "underline", "code"):
        if key in value:
            annotations[key] = bool(value[key])
    color = value.get("color")
    if isinstance(color, str) and color in _VALID_NOTION_COLORS:
        annotations["color"] = color
    return annotations


def _sanitize_rich_text(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []

    if isinstance(value, str):
        return _split_rich_text_content(value)

    if isinstance(value, dict):
        if "rich_text" in value:
            return _sanitize_rich_text(value.get("rich_text"))
        if "text" in value or "content" in value or "plain_text" in value:
            value = [value]
        else:
            return []

    if not isinstance(value, list):
        return _split_rich_text_content(str(value))

    result: list[dict[str, Any]] = []
    for part in value:
        if isinstance(part, str):
            result.extend(_split_rich_text_content(part))
            continue
        if not isinstance(part, dict):
            result.extend(_split_rich_text_content(str(part)))
            continue

        content = (
            part.get("plain_text")
            or part.get("content")
            or (part.get("text") or {}).get("content")
        )
        if content is None:
            continue

        chunks = _split_rich_text_content(str(content))
        annotations = _sanitize_annotations(part.get("annotations"))
        href = part.get("href") or (part.get("text") or {}).get("link", {}).get("url")
        for chunk in chunks:
            if annotations:
                chunk["annotations"] = annotations
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                chunk["text"]["link"] = {"url": href}
            result.append(chunk)

    return result


def _first_present_value(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _sanitize_text_like_block(block_type: str, block: dict[str, Any], depth: int) -> dict[str, Any] | None:
    source = block.get(block_type)
    if not isinstance(source, dict):
        source = block

    rich_text_value = _first_present_value(
        source.get("rich_text"),
        source.get("text"),
        source.get("content"),
        block.get("rich_text"),
        block.get("text"),
        block.get("content"),
    )
    rich_text = _sanitize_rich_text(rich_text_value)

    if block_type != "divider" and not rich_text and block_type != "callout":
        return None

    value: dict[str, Any] = {"rich_text": rich_text}
    color = source.get("color") or block.get("color")
    if isinstance(color, str) and color in _VALID_NOTION_COLORS:
        value["color"] = color

    if block_type == "to_do":
        value["checked"] = bool(source.get("checked", block.get("checked", False)))

    if block_type == "code":
        language = source.get("language") or block.get("language") or "plain text"
        value["language"] = str(language)

    if block_type == "callout":
        icon = source.get("icon") or block.get("icon")
        if isinstance(icon, dict) and icon.get("type") in {"emoji", "external"}:
            value["icon"] = icon

    clean_block: dict[str, Any] = {"object": "block", "type": block_type, block_type: value}
    child_source = source.get("children") if isinstance(source, dict) else None
    children = _sanitize_notion_blocks(block.get("children") or child_source, depth=depth + 1)
    if children and block_type in {"bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"}:
        clean_block["children"] = children
    return clean_block


def _sanitize_table_row(block: dict[str, Any], width: int | None = None) -> dict[str, Any] | None:
    source = block.get("table_row")
    if not isinstance(source, dict):
        source = block

    cells = source.get("cells") or block.get("cells") or []
    if not isinstance(cells, list):
        return None

    clean_cells = [_sanitize_rich_text(cell) for cell in cells]
    if width is not None:
        while len(clean_cells) < width:
            clean_cells.append([])
        if len(clean_cells) > width:
            clean_cells = clean_cells[:width]

    if not clean_cells:
        return None

    return {"object": "block", "type": "table_row", "table_row": {"cells": clean_cells}}


def _rows_from_plain_table(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []

    row_blocks: list[dict[str, Any]] = []
    width = 0
    normalized_rows: list[list[Any]] = []
    for row in rows:
        if isinstance(row, dict):
            row = row.get("cells") or row.get("row")
        if not isinstance(row, list):
            continue
        normalized_rows.append(row)
        width = max(width, len(row))

    for row in normalized_rows:
        row_block = _sanitize_table_row({"cells": row}, width=width)
        if row_block:
            row_blocks.append(row_block)
    return row_blocks


def _sanitize_table_block(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("table")
    if not isinstance(source, dict):
        source = block

    raw_children = block.get("children") or source.get("children") or []
    row_blocks: list[dict[str, Any]] = []
    if isinstance(raw_children, list):
        for child in raw_children:
            if isinstance(child, dict):
                row_block = _sanitize_table_row(child)
                if row_block:
                    row_blocks.append(row_block)
    if not row_blocks:
        row_blocks = _rows_from_plain_table(source.get("rows") or block.get("rows"))

    if not row_blocks:
        return None

    width = max(len(row["table_row"]["cells"]) for row in row_blocks)
    normalized_rows: list[dict[str, Any]] = []
    for row in row_blocks:
        normalized = _sanitize_table_row(row, width=width)
        if normalized:
            normalized_rows.append(normalized)

    table_width = source.get("table_width") or block.get("table_width") or width
    try:
        table_width = int(table_width)
    except (TypeError, ValueError):
        table_width = width

    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": max(1, table_width),
            "has_column_header": bool(source.get("has_column_header", block.get("has_column_header", False))),
            "has_row_header": bool(source.get("has_row_header", block.get("has_row_header", False))),
        },
        "children": normalized_rows,
    }


def _sanitize_notion_block(block: dict[str, Any], depth: int = 0) -> dict[str, Any] | None:
    block_type = str(block.get("type") or block.get("block_type") or "").strip()
    if not block_type:
        for candidate in _SUPPORTED_LLM_BLOCK_TYPES:
            if candidate in block:
                block_type = candidate
                break

    if block_type not in _SUPPORTED_LLM_BLOCK_TYPES:
        return None

    if block_type == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    if block_type == "table":
        return _sanitize_table_block(block)
    if block_type == "table_row":
        # table_row 只能作为 table 的子 block；顶层 table_row 会导致 Notion API 报错。
        return _sanitize_table_row(block) if depth > 0 else None
    return _sanitize_text_like_block(block_type, block, depth=depth)


def _sanitize_notion_blocks(blocks: Any, depth: int = 0, max_depth: int = 2) -> list[dict[str, Any]]:
    if depth > max_depth or not isinstance(blocks, list):
        return []

    clean_blocks: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        clean_block = _sanitize_notion_block(block, depth=depth)
        if clean_block:
            clean_blocks.append(clean_block)
    return clean_blocks


def _markdown_line_to_block(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("### "):
        return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _text_to_rich_text(stripped[4:])}}
    if stripped.startswith("## "):
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _text_to_rich_text(stripped[3:])}}
    if stripped.startswith("# "):
        return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": _text_to_rich_text(stripped[2:])}}
    if stripped.startswith(("- ", "* ")):
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _text_to_rich_text(stripped[2:])}}
    if re.match(r"^\d+[.)]\s+", stripped):
        text = re.sub(r"^\d+[.)]\s+", "", stripped, count=1)
        return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _text_to_rich_text(text)}}
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _text_to_rich_text(stripped)}}


def _markdown_to_basic_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        block = _markdown_line_to_block(line)
        if block:
            blocks.append(block)
    return blocks


def _compact_commit_items_for_llm(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_items: list[dict[str, Any]] = []
    for item in items:
        compact_items.append(
            {
                "任务名": item.get("任务名"),
                "周几": item.get("周几"),
                "持续小时": _format_hours(float(item.get("持续小时") or 0)),
                "持续小时文本": _format_push_hours(float(item.get("持续小时") or 0)),
                "类别": item.get("类别"),
                "任务组 ID": item.get("task_group_id"),
                "Session IDs": item.get("source_session_ids") or [],
                "开始时间": item.get("start_time"),
                "结束时间": item.get("end_time"),
                "原始任务名": item.get("session_names") or [],
            }
        )
    return compact_items


def _compact_commit_items_for_push(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    给 Notion 页面 push 使用的紧凑数据。
    页面展示时，持续小时必须是 1.5H 这种格式；同时保留持续小时数值，方便 LLM 理解。
    """
    compact_items: list[dict[str, Any]] = []
    for item in items:
        hours = float(item.get("持续小时") or 0)
        compact_items.append(
            {
                "任务名": item.get("任务名"),
                "周几": item.get("周几"),
                "持续小时": _format_push_hours(hours),
                "持续小时数值": _format_hours(hours),
                "类别": item.get("类别"),
                "任务组 ID": item.get("task_group_id"),
                "Session IDs": item.get("source_session_ids") or [],
                "开始时间": item.get("start_time"),
                "结束时间": item.get("end_time"),
                "原始任务名": item.get("session_names") or [],
            }
        )
    return compact_items


def _blocks_from_llm_for_page_format(
    page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> list[dict[str, Any]]:
    format_sample = _fetch_page_format_sample(page_id, token)
    _render_info(
        "Notion 格式检查",
        f"已读取目标页格式样例: {format_sample.get('sampled_top_level_block_count', 0)} 个顶层 block；正在调用 LLM 生成匹配格式的 commit 内容。",
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Notion 页面格式适配器。你会收到目标 Notion 页的现有 block 格式样例，以及需要写入的任务 commit 数据。"
                "请先根据样例推断该页面记录任务/时间的格式，再生成可以直接追加到 Notion 页面 children 的 blocks。"
                "必须返回 JSON object，不要返回 Markdown，不要解释。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "target_notion_page_format_sample": format_sample,
                    "commit": {
                        "commit_id": commit_id,
                        "generated_at": datetime.now().isoformat(timespec="seconds"),
                        "items": _compact_commit_items_for_push(items),
                    },
                    "rules": [
                        "输出必须是 Notion append block API 可接受的 blocks，字段不要包含 id、parent、created_time、last_edited_time。",
                        "优先模仿 target_notion_page_format_sample 里已有记录的结构、标题层级、列表/表格/段落风格、字段顺序和中文用词。",
                        "不要重复已有页面内容，只生成本次 commit 需要追加的新内容。",
                        "不要改动任务时长和周几；持续小时必须使用输入中的 H 格式，例如 1.5H，不要写成 1.5 小时 或纯数字。",
                        "可以使用的 block 类型: paragraph, heading_1, heading_2, heading_3, bulleted_list_item, numbered_list_item, to_do, toggle, quote, callout, code, divider, table。",
                        "rich_text 必须使用 [{type:'text', text:{content:'...'}}] 结构；单段 content 不超过 2000 字符。",
                    ],
                    "output_schema": {
                        "blocks": [
                            {
                                "object": "block",
                                "type": "bulleted_list_item",
                                "bulleted_list_item": {
                                    "rich_text": [
                                        {"type": "text", "text": {"content": "示例：任务名 | 周几 | 1.5H | 类别"}}
                                    ]
                                },
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = _openai_json(messages, context)
    if not result:
        raise RuntimeError("LLM 未返回可用结果；为避免写入错误格式，已取消 push。")

    blocks = _sanitize_notion_blocks(result.get("blocks"))
    if not blocks:
        markdown = result.get("markdown") or result.get("content")
        if isinstance(markdown, str):
            blocks = _markdown_to_basic_blocks(markdown)

    if not blocks:
        raise RuntimeError("LLM 返回结果中没有可追加到 Notion 的有效 blocks；已取消 push。")

    return blocks


def _build_notion_blocks_for_items(items: list[dict[str, Any]], commit_id: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _text_to_rich_text("任务时间记录")},
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _text_to_rich_text(f"commit_id: {commit_id}")},
        },
    ]

    for item in items:
        line = (
            f"{item.get('任务名', '')} | {item.get('周几', '')} | "
            f"{_format_push_hours(float(item.get('持续小时') or 0))} | {item.get('类别', '')}"
        )
        blocks.append(
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _text_to_rich_text(line)},
            }
        )
    return blocks


def _get_database_title_property(database_id: str, token: str, config: dict[str, str]) -> str:
    configured = config.get("notion_task_name_property") or config.get("notion_title_property")
    if configured:
        return configured

    data = _notion_request("GET", f"/databases/{database_id}", token)
    for prop_name, prop in (data.get("properties") or {}).items():
        if prop.get("type") == "title":
            return str(prop_name)
    raise RuntimeError("无法识别 Notion database 的 title 属性，请在 config.txt 设置 notion_task_name_property=你的标题属性名。")


def _query_task_page(database_id: str, title_property: str, task_name: str, token: str) -> str:
    payload = {
        "page_size": 10,
        "filter": {
            "property": title_property,
            "title": {"contains": task_name},
        },
    }
    data = _notion_request("POST", f"/databases/{database_id}/query", token, payload)
    results = data.get("results") or []
    if not results:
        return ""
    return str(results[0].get("id") or "")



# ---------------------------------------------------------------------------
# Notion push AI 循环思考 / 额外指令执行层
#
# 说明：上面保留了基础的“探索子页 + 按页面格式生成 blocks”能力；这里重新定义
# 若干同名函数，让运行时使用更完整的版本：
# - 当前页始终可以作为 fallback 写入目标；
# - 只有明显是目录/总览/索引页时才继续探索；
# - 每轮读取 `::|` 开头的额外指令；
# - 允许 AI 在有限循环内创建页、复制页、复制空页；
# - 每轮动作都用 Rich UI 输出。
# ---------------------------------------------------------------------------

_EXTRA_INSTRUCTION_PREFIX = "::|"
_AI_PAGE_STRUCTURE_ACTIONS = {
    "create_child_page",
    "duplicate_page",
    "duplicate_page_without_content",
}
_AI_PUSH_LOOP_ACTIONS = {"write_current", "explore_child", "write_database"} | _AI_PAGE_STRUCTURE_ACTIONS


def _block_text_fragments(block: dict[str, Any]) -> list[str]:
    block_type = str(block.get("type") or "")
    value = block.get(block_type) if block_type else None
    if not isinstance(value, dict):
        value = {}

    fragments: list[str] = []
    text = _plain_text_from_rich_text(value.get("rich_text"))
    if text:
        fragments.append(text)

    if block_type == "table_row":
        for cell in value.get("cells") or []:
            if isinstance(cell, list):
                cell_text = _plain_text_from_rich_text(cell)
                if cell_text:
                    fragments.append(cell_text)

    if block_type == "child_page" and value.get("title"):
        fragments.append(str(value.get("title") or ""))

    return fragments


def _block_has_extra_instruction(block: dict[str, Any]) -> bool:
    for fragment in _block_text_fragments(block):
        for line in fragment.splitlines():
            if line.strip().startswith(_EXTRA_INSTRUCTION_PREFIX):
                return True
    return False


def _extract_extra_instructions_from_blocks(raw_blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    从当前页顶层 blocks 中提取 `::|` 开头的自动化指令。

    指令本身仍交给 LLM 理解；程序只负责识别、编号、传入上下文，并提供安全动作集合。
    """

    instructions: list[dict[str, str]] = []
    for block in raw_blocks:
        block_type = str(block.get("type") or "unknown")
        for fragment in _block_text_fragments(block):
            for raw_line in fragment.splitlines():
                line = raw_line.strip()
                if not line.startswith(_EXTRA_INSTRUCTION_PREFIX):
                    continue
                instruction = line[len(_EXTRA_INSTRUCTION_PREFIX):].strip()
                if not instruction:
                    continue
                instructions.append(
                    {
                        "instruction_index": str(len(instructions) + 1),
                        "instruction": instruction,
                        "source_block_type": block_type,
                    }
                )
    return instructions




def _fetch_extra_instructions_from_page_blocks(
    raw_blocks: list[dict[str, Any]],
    token: str,
    *,
    max_depth: int = 2,
    max_child_blocks: int = 100,
) -> list[dict[str, str]]:
    """
    递归读取当前页内的 `::|` 指令。这样指令可以放在 toggle/list/table 等较深位置，
    但仍有 max_depth 上限，避免为了读指令无限展开。
    """

    instructions: list[dict[str, str]] = []

    def collect(blocks: list[dict[str, Any]], depth: int, path_prefix: str) -> None:
        for index, block in enumerate(blocks, start=1):
            block_type = str(block.get("type") or "unknown")
            block_path = f"{path_prefix}{index}" if path_prefix else str(index)
            for fragment in _block_text_fragments(block):
                for raw_line in fragment.splitlines():
                    line = raw_line.strip()
                    if not line.startswith(_EXTRA_INSTRUCTION_PREFIX):
                        continue
                    instruction = line[len(_EXTRA_INSTRUCTION_PREFIX):].strip()
                    if not instruction:
                        continue
                    instructions.append(
                        {
                            "instruction_index": str(len(instructions) + 1),
                            "instruction": instruction,
                            "source_block_type": block_type,
                            "source_path": block_path,
                        }
                    )

            if depth >= max_depth or not block.get("has_children") or not block.get("id"):
                continue
            child_blocks = _fetch_top_level_blocks(str(block["id"]), token, max_blocks=max_child_blocks)
            collect(child_blocks, depth + 1, f"{block_path}.")

    collect(raw_blocks, 0, "")
    return instructions

def _simplify_block_for_format_with_instruction_flag(block: dict[str, Any]) -> dict[str, Any]:
    simplified = _simplify_block_for_format(block)
    if _block_has_extra_instruction(block):
        simplified["is_extra_instruction"] = True
    return simplified


def _fetch_page_format_sample(
    page_id: str,
    token: str,
    *,
    max_top_level_blocks: int = 500,
    max_sample_blocks: int = 120,
    max_child_blocks: int = 20,
) -> dict[str, Any]:
    """
    读取目标 Notion 页现有内容，让 LLM 根据真实页面格式生成 push blocks。

    与基础版相比，这里会把 `::|` 额外指令单独提取出来，避免模型把控制指令误当作
    普通任务记录格式去模仿。
    """

    page_data = _notion_request("GET", f"/pages/{page_id}", token)
    raw_blocks = _fetch_top_level_blocks(page_id, token, max_blocks=max_top_level_blocks)
    extra_instructions = _fetch_extra_instructions_from_page_blocks(raw_blocks, token)
    retained_blocks = _truncate_format_blocks(raw_blocks, max_blocks=max_sample_blocks)

    simplified_blocks: list[dict[str, Any]] = []
    for raw_block in retained_blocks:
        if raw_block.get("type") == "omitted_blocks":
            simplified_blocks.append(raw_block)
            continue

        simplified = _simplify_block_for_format_with_instruction_flag(raw_block)
        if raw_block.get("has_children") and raw_block.get("id"):
            child_blocks = _fetch_top_level_blocks(str(raw_block["id"]), token, max_blocks=max_child_blocks)
            if child_blocks:
                simplified["children"] = [
                    _simplify_block_for_format_with_instruction_flag(child)
                    for child in child_blocks
                    if not _block_has_extra_instruction(child)
                ]
        simplified_blocks.append(simplified)

    plain_lines: list[str] = []
    for block in simplified_blocks:
        if block.get("is_extra_instruction"):
            continue
        block_type = block.get("type")
        text = str(block.get("text") or "")
        if block_type == "table_row" and block.get("cells"):
            text = " | ".join(map(str, block.get("cells") or []))
        if text:
            plain_lines.append(f"{block_type}: {text}")

    return {
        "page_title": _page_title_from_page_data(page_data),
        "sampled_top_level_block_count": len(simplified_blocks),
        "source_top_level_block_count_limit": max_top_level_blocks,
        "format_blocks": simplified_blocks,
        "extra_instructions": extra_instructions,
        "plain_text_outline": "\n".join(plain_lines[-80:]),
    }


def _fetch_push_page_state(page_id: str, token: str) -> dict[str, Any]:
    """
    读取当前候选页，让 AI 决定下一步动作。

    page_id 只在程序内部保存；传给 LLM 的页面引用使用 current / child_index，避免模型直接
    拼 Notion API 参数。
    """

    page_data = _notion_request("GET", f"/pages/{page_id}", token)
    raw_blocks = _fetch_top_level_blocks(page_id, token, max_blocks=200)
    child_pages = _extract_child_page_candidates(raw_blocks)
    extra_instructions = _fetch_extra_instructions_from_page_blocks(raw_blocks, token)
    sample_blocks = _truncate_format_blocks(raw_blocks, max_blocks=70)
    simplified_blocks = [_simplify_block_for_format_with_instruction_flag(block) for block in sample_blocks]

    plain_lines: list[str] = []
    for block in simplified_blocks:
        if block.get("is_extra_instruction"):
            continue
        block_type = str(block.get("type") or "")
        text = str(block.get("text") or block.get("title") or "")
        if block_type == "table_row" and block.get("cells"):
            text = " | ".join(map(str, block.get("cells") or []))
        if text:
            plain_lines.append(f"{block_type}: {text}")

    page_title = _page_title_from_page_data(page_data) or "未命名页面"
    available_pages = [{"page_ref": "current", "page_id": page_id, "title": page_title, "role": "current_page"}]
    available_pages.extend(
        {
            "page_ref": child["child_index"],
            "page_id": child.get("page_id"),
            "title": child["title"],
            "role": "child_page",
        }
        for child in child_pages
    )

    return {
        "page_id": page_id,
        "page_title": page_title,
        "top_level_block_count_sampled": len(simplified_blocks),
        "child_pages": child_pages,
        "available_pages": available_pages,
        "extra_instructions": extra_instructions,
        "format_blocks": simplified_blocks,
        "plain_text_outline": "\n".join(plain_lines[-80:]),
    }


def _ai_action_label(action: str) -> str:
    return {
        "write_current": "写入当前页",
        "write_database": "写入已有 database",
        "explore_child": "继续探索子页",
        "create_child_page": "创建新子页",
        "duplicate_page": "复制现存页",
        "duplicate_page_without_content": "复制空页",
    }.get(action, action or "未知动作")


def _render_ai_push_action(
    step: int,
    max_steps: int,
    page_state: dict[str, Any],
    decision: dict[str, Any],
    *,
    selected_child_title: str = "",
    note: str = "",
) -> None:
    action = str(decision.get("action") or "write_current")
    action_label = _ai_action_label(action)
    page_title = str(page_state.get("page_title") or "未命名页面")
    page_id = _safe_short_page_id(str(page_state.get("page_id") or ""))
    page_kind = str(decision.get("page_kind") or "unknown")
    observation = str(decision.get("observation") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    new_page_title = str(decision.get("new_page_title") or decision.get("title") or "").strip()
    source_page = str(decision.get("source_page") or decision.get("source_child_index") or "").strip()
    target_parent = str(decision.get("target_parent") or decision.get("target_parent_index") or "").strip()
    instruction_refs = decision.get("instruction_refs") or decision.get("instruction_index") or ""
    confidence = decision.get("confidence")
    confidence_text = ""
    if isinstance(confidence, (int, float)):
        confidence_text = f"{float(confidence):.2f}"

    instruction_count = len(page_state.get("extra_instructions") or [])

    if not RICH_AVAILABLE:
        print(f"AI Notion push 循环 {step}/{max_steps}")
        print(f"页面: {page_title} ({page_id})")
        print(f"判断: {page_kind}")
        print(f"动作: {action_label}")
        if instruction_count:
            print(f"额外指令: {instruction_count} 条")
        if selected_child_title:
            print(f"子页: {selected_child_title}")
        if source_page:
            print(f"来源页引用: {source_page}")
        if target_parent:
            print(f"目标父页引用: {target_parent}")
        if new_page_title:
            print(f"新页标题: {new_page_title}")
        if instruction_refs:
            print(f"依据指令: {instruction_refs}")
        if confidence_text:
            print(f"置信度: {confidence_text}")
        if observation:
            print(f"观察: {observation}")
        if reason:
            print(f"理由: {reason}")
        if note:
            print(f"结果: {note}")
        return

    if action in _AI_PAGE_STRUCTURE_ACTIONS:
        border_style = "magenta"
    elif action == "explore_child":
        border_style = "cyan"
    elif action == "write_database":
        border_style = "blue"
    else:
        border_style = "green"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    # 第二列必须 fold 换行，不能使用 Rich 默认 ellipsis，否则长观察/理由会显示为“.../…”
    table.add_column(style="white", overflow="fold", no_wrap=False, ratio=1)
    table.add_row("当前页", f"{page_title} ({page_id})")
    table.add_row("页面判断", page_kind)
    table.add_row("AI 动作", action_label)
    if instruction_count:
        table.add_row("额外指令", f"{instruction_count} 条")
    if selected_child_title:
        table.add_row("探索子页", selected_child_title)
    if source_page:
        table.add_row("来源页引用", source_page)
    if target_parent:
        table.add_row("目标父页引用", target_parent)
    if new_page_title:
        table.add_row("新页标题", _preview_text(new_page_title, 300))
    if instruction_refs:
        table.add_row("依据指令", _preview_text(str(instruction_refs), 300))
    if confidence_text:
        table.add_row("置信度", confidence_text)
    if observation:
        table.add_row("观察", Text(observation, style="white", overflow="fold", no_wrap=False))
    if reason:
        table.add_row("理由", Text(reason, style="white", overflow="fold", no_wrap=False))
    if note:
        table.add_row("执行结果", Text(note, style="white", overflow="fold", no_wrap=False))

    console.print(
        Panel(
            table,
            title=Text(f"AI Notion push 循环 {step}/{max_steps}", style=f"bold {border_style}"),
            border_style=border_style,
            box=box.ROUNDED,
            expand=False,
        )
    )


def _coerce_explore_decision(result: dict[str, Any] | None, fallback_reason: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "page_kind": "unknown",
            "observation": "AI 未返回有效 JSON 决策。",
            "action": "write_current",
            "reason": fallback_reason,
            "confidence": 0,
        }

    raw_action = str(result.get("action") or "write_current").strip()
    action_aliases = {
        "duplicate_without_content": "duplicate_page_without_content",
        "duplicate_empty_page": "duplicate_page_without_content",
        "duplicate_page_empty": "duplicate_page_without_content",
        "create_page": "create_child_page",
    }
    action = action_aliases.get(raw_action, raw_action)
    if action not in _AI_PUSH_LOOP_ACTIONS:
        action = "write_current"

    decision: dict[str, Any] = {
        "page_kind": str(result.get("page_kind") or "unknown"),
        "observation": str(result.get("observation") or ""),
        "action": action,
        "reason": str(result.get("reason") or fallback_reason),
        "confidence": result.get("confidence", 0),
    }
    passthrough_keys = (
        "child_index",
        "source_page",
        "source_child_index",
        "target_parent",
        "target_parent_index",
        "new_page_title",
        "title",
        "seed_markdown",
        "initial_markdown",
        "instruction_refs",
        "instruction_index",
    )
    for key in passthrough_keys:
        if result.get(key) is not None:
            decision[key] = result.get(key)
    return decision


def _decide_next_push_page(
    page_state: dict[str, Any],
    items: list[dict[str, Any]],
    commit_id: str,
    context: dict[str, str],
    *,
    step: int,
    max_steps: int,
    visited_titles: list[str],
    executed_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    child_pages_for_llm = [
        {"child_index": child["child_index"], "title": child["title"]}
        for child in page_state.get("child_pages") or []
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Notion 任务记录 push 自动落点与页面结构操作器。你每一轮只能选择一个动作。"
                "当前页永远可以作为 fallback 写入目标：当前页像任务记录页时直接 write_current；"
                "当前页格式不对、看不懂或没有更好落点时也 write_current。"
                "当前页明显是目录页、总览页、日/周/月索引页，并且已有匹配 child_database 时，选择 write_database；如果匹配的是 child_page，才 explore_child。"
                "如果当前页中存在以 ::| 开头的额外指令，你必须阅读并判断是否需要执行页面结构动作。"
                "页面结构动作仅在额外指令明确要求时使用：create_child_page、duplicate_page、duplicate_page_without_content。"
                "不要重复执行 already_executed_actions 中已经完成的同类指令。"
                "如果不确定、动作参数不明确、没有对应页面引用，必须 write_current，不能卡住。"
                "如果 is_last_step=true，必须 write_current。必须返回 JSON object。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "now": datetime.now().isoformat(timespec="seconds"),
                    "loop": {
                        "step": step,
                        "max_steps": max_steps,
                        "is_last_step": step >= max_steps,
                        "visited_page_titles": visited_titles,
                        "already_executed_actions": executed_actions or [],
                    },
                    "current_page": {
                        "title": page_state.get("page_title"),
                        "sampled_block_count": page_state.get("top_level_block_count_sampled"),
                        "plain_text_outline": page_state.get("plain_text_outline"),
                        "format_blocks": page_state.get("format_blocks"),
                        "child_pages": child_pages_for_llm,
                        "available_page_refs": page_state.get("available_pages"),
                        "extra_instructions": page_state.get("extra_instructions") or [],
                    },
                    "commit": {
                        "commit_id": commit_id,
                        "items": _compact_commit_items_for_llm(items),
                    },
                    "available_actions": {
                        "write_current": "把本次 commit 写入当前页；这是永远可用的 fallback。",
                        "explore_child": "进入某个 child_page；当 ::| 指令要求已有正确时间子页则写入该子页，或当前页明显是目录/总览/索引页且有更精确子页时使用。需要 child_index。",
                        "create_child_page": "按 ::| 指令，在 target_parent 下创建新页；target_parent 可为 current 或 child_index；需要 new_page_title，可选 seed_markdown。创建后下一轮会评估新页。",
                        "duplicate_page": "按 ::| 指令，复制 source_page 的内容到 target_parent 下的新页；source_page/target_parent 可为 current 或 child_index；可选 new_page_title。控制指令块不会被复制。",
                        "duplicate_page_without_content": "按 ::| 指令，以 source_page 为模板只创建同名/指定标题空页，不复制内容。",
                    },
                    "decision_rules": [
                        "write_current 永远是安全动作；当前页是 fallback 目标。",
                        "当前页像任务记录页、时间日志页、日报页、周报页、任务详情页时，选择 write_current。",
                        "当前页格式不对或看不懂时，也选择 write_current，不要失败。",
                        "只有当前页明显像目录/总览/日周月索引，并且 child_pages 中有明显更精确落点，才选择 explore_child。",
                        "create/duplicate 动作只在 extra_instructions 明确要求时使用，不能自行发明页面结构操作。",
                        "如果 is_last_step=true，不能继续探索或创建/复制页面；若已有明确匹配 child_database 可以选择 write_database，否则选择 write_current。",
                        "所有页面引用都必须来自 available_page_refs；不要输出 Notion id。",
                    ],
                    "output_schema": {
                        "page_kind": "task_record | directory_or_index | wrong_format | empty | unknown",
                        "observation": "一句话说明看到了什么格式/指令",
                        "action": "write_current | explore_child | create_child_page | duplicate_page | duplicate_page_without_content",
                        "child_index": "仅当 action=explore_child 时填写 child_pages 中的编号",
                        "source_page": "duplicate 时填写 current 或 child_index",
                        "target_parent": "create/duplicate 时填写 current 或 child_index；默认 current",
                        "new_page_title": "create/duplicate 时的新页标题",
                        "seed_markdown": "create_child_page 时可选，用基础 Markdown 初始化新页，不要放本次 commit 内容",
                        "instruction_refs": "使用了哪些 ::| 指令编号",
                        "reason": "一句话说明为什么这样做",
                        "confidence": "0 到 1 的数字",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    return _coerce_explore_decision(
        _openai_json(messages, context),
        "为了保证 push 不被卡住，回退到当前页写入。",
    )


def _resolve_page_ref(page_state: dict[str, Any], page_ref: Any) -> dict[str, str] | None:
    ref = str(page_ref or "current").strip()
    ref = ref.replace("child:", "").replace("child_", "").strip()
    if ref in {"", "0", "current", "current_page", "当前页"}:
        return {
            "page_id": str(page_state.get("page_id") or ""),
            "title": str(page_state.get("page_title") or "未命名页面"),
            "page_ref": "current",
        }

    # 先按 LLM 可见的统一 available_page_refs 解析，避免只能引用 child_pages。
    # 这仍然只是“引用解析”，不是用传统程序替 LLM 判断应该复制哪个页面。
    for page in page_state.get("available_pages") or []:
        if str(page.get("page_ref") or "") == ref:
            return {
                "page_id": str(page.get("page_id") or ""),
                "title": str(page.get("title") or "未命名页面"),
                "page_ref": ref,
            }

    # 兼容旧模型只返回 child_index 的情况。
    for child in page_state.get("child_pages") or []:
        if str(child.get("child_index")) == ref:
            return {
                "page_id": str(child.get("page_id") or ""),
                "title": str(child.get("title") or "未命名子页"),
                "page_ref": ref,
            }
    return None


def _decision_target_parent_ref(decision: dict[str, Any]) -> Any:
    return decision.get("target_parent") or decision.get("target_parent_index") or "current"


def _decision_source_page_ref(decision: dict[str, Any]) -> Any:
    return decision.get("source_page") or decision.get("source_child_index") or "current"


def _decision_new_page_title(decision: dict[str, Any], fallback_title: str) -> str:
    title = str(decision.get("new_page_title") or decision.get("title") or "").strip()
    if title:
        return title[:2000]
    fallback = (fallback_title or "新页面").strip()
    return f"{fallback} Copy"[:2000]


def _create_child_page(parent_page_id: str, title: str, token: str, children: list[dict[str, Any]] | None = None) -> str:
    if not parent_page_id:
        raise RuntimeError("创建 Notion 子页失败：缺少目标父页。")

    clean_title = (title or "未命名页面").strip()[:2000] or "未命名页面"
    # Notion 对 page parent 的 title 属性在不同 API 示例中有两种写法；先用官方 page-parent
    # 简洁写法，若账号/API 返回校验错误，再尝试 title property 包装写法。
    payloads = [
        {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": _text_to_rich_text(clean_title)},
        },
        {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": _text_to_rich_text(clean_title)}},
        },
    ]

    last_error: RuntimeError | None = None
    data: dict[str, Any] = {}
    for payload in payloads:
        try:
            data = _notion_request("POST", "/pages", token, payload)
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    new_page_id = str(data.get("id") or "")
    if not new_page_id:
        raise RuntimeError("创建 Notion 子页失败：Notion API 未返回新页面 id。")

    clean_children = _sanitize_notion_blocks(children or [])
    if clean_children:
        _append_blocks_to_page(new_page_id, clean_children, token)
    return new_page_id


def _get_duplicate_max_blocks(context: dict[str, str]) -> int:
    return _read_int_config(
        context,
        "NOTION_PUSH_DUPLICATE_MAX_BLOCKS",
        "notion_push_duplicate_max_blocks",
        100,
        minimum=0,
        maximum=500,
    )


def _get_duplicate_max_depth(context: dict[str, str]) -> int:
    return _read_int_config(
        context,
        "NOTION_PUSH_DUPLICATE_MAX_DEPTH",
        "notion_push_duplicate_max_depth",
        2,
        minimum=0,
        maximum=5,
    )


def _clone_block_for_append(raw_block: dict[str, Any], token: str, *, depth: int, max_depth: int) -> dict[str, Any] | None:
    if _block_has_extra_instruction(raw_block):
        # 控制指令用于本轮 push，不作为页面模板内容复制，避免复制出的页面再次触发同一指令。
        return None

    block_type = str(raw_block.get("type") or "")
    if block_type == "table" and raw_block.get("id"):
        table_value = raw_block.get("table") if isinstance(raw_block.get("table"), dict) else {}
        rows = _fetch_top_level_blocks(str(raw_block["id"]), token, max_blocks=100)
        return _sanitize_table_block({"type": "table", "table": table_value, "children": rows})

    clean_block = _sanitize_notion_block(raw_block, depth=depth)
    if not clean_block:
        return None

    child_capable_types = {"bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"}
    if depth < max_depth and raw_block.get("has_children") and raw_block.get("id") and block_type in child_capable_types:
        children: list[dict[str, Any]] = []
        for child_raw in _fetch_top_level_blocks(str(raw_block["id"]), token, max_blocks=100):
            child_clean = _clone_block_for_append(child_raw, token, depth=depth + 1, max_depth=max_depth)
            if child_clean:
                children.append(child_clean)
        if children:
            clean_block["children"] = children[:100]

    return clean_block


def _clone_page_blocks_for_append(
    source_page_id: str,
    token: str,
    *,
    max_blocks: int,
    max_depth: int,
) -> list[dict[str, Any]]:
    if max_blocks <= 0:
        return []

    clean_blocks: list[dict[str, Any]] = []
    for raw_block in _fetch_top_level_blocks(source_page_id, token, max_blocks=max_blocks):
        clean = _clone_block_for_append(raw_block, token, depth=0, max_depth=max_depth)
        if clean:
            clean_blocks.append(clean)
    return clean_blocks


def _execute_page_structure_action(
    decision: dict[str, Any],
    page_state: dict[str, Any],
    token: str,
    context: dict[str, str],
) -> tuple[dict[str, Any], str]:
    action = str(decision.get("action") or "")
    target_parent = _resolve_page_ref(page_state, _decision_target_parent_ref(decision))
    if not target_parent or not target_parent.get("page_id"):
        raise RuntimeError("页面结构动作缺少有效 target_parent；已回退到当前页。")

    if action == "create_child_page":
        title = _decision_new_page_title(decision, "新任务记录页")
        seed_markdown = str(decision.get("seed_markdown") or decision.get("initial_markdown") or "").strip()
        seed_blocks = _markdown_to_basic_blocks(seed_markdown) if seed_markdown else []
        new_page_id = _create_child_page(target_parent["page_id"], title, token, seed_blocks)
        new_state = _fetch_push_page_state(new_page_id, token)
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下创建新页：{title}。Notion API 会把新页追加到目标父页末尾。",
        )

    if action in {"duplicate_page", "duplicate_page_without_content"}:
        source_page = _resolve_page_ref(page_state, _decision_source_page_ref(decision))
        if not source_page or not source_page.get("page_id"):
            raise RuntimeError("复制页面动作缺少有效 source_page；已回退到当前页。")

        title = _decision_new_page_title(decision, str(source_page.get("title") or "模板页"))
        children: list[dict[str, Any]] = []
        if action == "duplicate_page":
            children = _clone_page_blocks_for_append(
                source_page["page_id"],
                token,
                max_blocks=_get_duplicate_max_blocks(context),
                max_depth=_get_duplicate_max_depth(context),
            )
        new_page_id = _create_child_page(target_parent["page_id"], title, token, children)
        new_state = _fetch_push_page_state(new_page_id, token)
        mode = "复制内容" if action == "duplicate_page" else "不复制内容"
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下基于 {source_page.get('title')} 创建页面：{title}（{mode}）。",
        )

    raise RuntimeError(f"未知页面结构动作: {action}")


def _select_push_target_page_with_llm(
    initial_page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> dict[str, Any]:
    max_steps = _get_push_max_explore_steps(context)
    current_page_id = initial_page_id
    visited_page_ids: set[str] = set()
    visited_titles: list[str] = []
    executed_actions: list[dict[str, Any]] = []

    if max_steps <= 0:
        page_state = _fetch_push_page_state(current_page_id, token)
        _render_ai_push_action(
            0,
            0,
            page_state,
            {
                "page_kind": "not_checked",
                "observation": "配置将 Notion push 探索步数设为 0。",
                "action": "write_current",
                "reason": "按配置直接写入当前页。",
                "confidence": 1,
            },
        )
        return page_state

    last_state: dict[str, Any] | None = None
    for step in range(1, max_steps + 1):
        page_state = _fetch_push_page_state(current_page_id, token)
        last_state = page_state
        visited_page_ids.add(current_page_id)
        title = str(page_state.get("page_title") or "未命名页面")
        if title not in visited_titles:
            visited_titles.append(title)

        decision = _decide_next_push_page(
            page_state,
            items,
            commit_id,
            context,
            step=step,
            max_steps=max_steps,
            visited_titles=visited_titles,
            executed_actions=executed_actions,
        )

        action = str(decision.get("action") or "write_current")

        if step >= max_steps and action not in {"write_current", "write_database"}:
            decision["action"] = "write_current"
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note="AI 想继续探索或执行页面结构动作，但已经达到循环上限；改为写入当前页。",
            )
            return page_state

        if action == "write_current":
            _render_ai_push_action(step, max_steps, page_state, decision)
            return page_state

        if action == "write_database":
            database_ref = (
                decision.get("source_block")
                or decision.get("source_block_ref")
                or decision.get("database_block")
                or decision.get("source_database")
                or "last_database"
            )
            source_database = _resolve_database_ref(page_state, database_ref)
            if not source_database:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="AI 选择的 database 引用不存在或不是 child_database；按 fallback 规则写入当前页。",
                )
                return page_state

            target_state = _database_target_state_from_block(page_state, source_database)
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note=(
                    f"已锁定已有 database：{source_database.get('title')} "
                    f"({_safe_short_page_id(str(source_database.get('block_id') or ''))})；"
                    "接下来将把本次 commit 写入该 database rows，而不是写回父页。"
                ),
            )
            return target_state

        if action == "explore_child":
            child_pages = page_state.get("child_pages") or []
            child_by_index = {str(child.get("child_index")): child for child in child_pages}
            chosen_child = child_by_index.get(str(decision.get("child_index")))
            if not chosen_child:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="AI 选择的 child_index 不存在；按 fallback 规则写入当前页。",
                )
                return page_state

            child_id = str(chosen_child.get("page_id") or "")
            child_title = str(chosen_child.get("title") or "未命名子页")
            if not child_id or child_id in visited_page_ids:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    selected_child_title=child_title,
                    note="AI 选择的子页为空或已经访问过；按 fallback 规则写入当前页。",
                )
                return page_state

            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                selected_child_title=child_title,
                note="已切换到该子页，下一轮继续检查。",
            )
            executed_actions.append({"step": step, "action": "explore_child", "target": child_title})
            current_page_id = child_id
            continue

        if action in _AI_PAGE_STRUCTURE_ACTIONS:
            if not page_state.get("extra_instructions"):
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="当前页没有 ::| 额外指令，拒绝执行页面结构动作；按 fallback 写入当前页。",
                )
                return page_state

            try:
                new_state, result_note = _execute_page_structure_action(decision, page_state, token, context)
            except RuntimeError as exc:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note=f"页面结构动作失败：{exc} 按 fallback 写入当前页。",
                )
                return page_state

            _render_ai_push_action(step, max_steps, page_state, decision, note=result_note)
            executed_actions.append(
                {
                    "step": step,
                    "action": action,
                    "new_page_title": new_state.get("page_title"),
                    "instruction_refs": decision.get("instruction_refs") or decision.get("instruction_index"),
                }
            )
            current_page_id = str(new_state.get("page_id") or current_page_id)
            continue

        decision["action"] = "write_current"
        _render_ai_push_action(
            step,
            max_steps,
            page_state,
            decision,
            note="AI 返回未知动作；按 fallback 写入当前页。",
        )
        return page_state

    if last_state is None:
        last_state = _fetch_push_page_state(current_page_id, token)
    _render_info(
        "Notion push 探索结束",
        f"已达到 {max_steps} 步循环上限；按 fallback 规则写入当前页: {last_state.get('page_title') or '未命名页面'}。",
    )
    return last_state

def _push_items_to_page_with_ai(
    initial_page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> None:
    target_state = _select_push_target_page_with_llm(initial_page_id, items, commit_id, token, context)
    target_page_id = str(target_state.get("page_id") or initial_page_id)
    blocks = _blocks_from_llm_for_page_format(target_page_id, items, commit_id, token, context)
    _render_push_target_selected(target_state, blocks_count=len(blocks))
    _append_blocks_to_page(target_page_id, blocks, token)


def _push_to_notion(items: list[dict[str, Any]], commit_id: str, context: dict[str, str]) -> None:
    config = read_config(context.get("config_file"))
    token = _notion_token(context)
    if not token:
        raise RuntimeError(
            "缺少 Notion token。请设置环境变量 NOTION_TOKEN，或在 config.txt 中设置 notion_token=..."
        )

    # 模式 1: 直接追加到指定 Notion 页。
    # 新增 notion_commit_page_id / NOTION_COMMIT_PAGE_ID，兼容原来的 log/push page 配置。
    push_page_id = _resolve_config_value(
        os.getenv("NOTION_COMMIT_PAGE_ID"),
        os.getenv("NOTION_LOG_PAGE_ID"),
        os.getenv("NOTION_PUSH_PAGE_ID"),
        config.get("notion_commit_page_id"),
        config.get("notion_log_page_id"),
        config.get("notion_push_page_id"),
    )
    if push_page_id:
        _push_items_to_page_with_ai(push_page_id, items, commit_id, token, context)
        return

    # 模式 2: 到任务 database 中按任务名寻找对应详情页，并逐个读取详情页格式后追加。
    database_id = _resolve_config_value(
        os.getenv("NOTION_TASK_DATABASE_ID"),
        os.getenv("NOTION_DATABASE_ID"),
        config.get("notion_task_database_id"),
        config.get("notion_database_id"),
    )
    if database_id:
        title_property = _get_database_title_property(database_id, token, config)
        missing: list[str] = []
        for item in items:
            task_name = str(item.get("任务名") or "").strip()
            page_id = _query_task_page(database_id, title_property, task_name, token)
            if not page_id:
                missing.append(task_name)
                continue
            _push_items_to_page_with_ai(page_id, [item], commit_id, token, context)
        if missing:
            raise RuntimeError("以下任务没有在 Notion database 中找到对应详情页，未标记为 committed: " + ", ".join(missing))
        return

    # 模式 3: 显式提供单个任务页，作为保底。
    single_page_id = _resolve_config_value(os.getenv("NOTION_TASK_PAGE_ID"), config.get("notion_task_page_id"))
    if single_page_id:
        _push_items_to_page_with_ai(single_page_id, items, commit_id, token, context)
        return

    raise RuntimeError(
        "缺少 Notion push 目标。请设置 notion_commit_page_id / notion_log_page_id / notion_push_page_id，"
        "或设置 notion_task_database_id/notion_database_id。"
    )


def _mark_records_committed(context: dict[str, str], commit_payload: dict[str, Any]) -> int:
    records = read_txt_records(context["uncommit_file"])
    commit_id = str(commit_payload.get("commit_id") or "")
    session_ids: set[str] = set()
    for item in commit_payload.get("items") or []:
        for session_id in item.get("source_session_ids") or []:
            session_ids.add(str(session_id))

    committed_at = datetime.now().isoformat(timespec="seconds")
    changed = 0
    for record in records:
        if str(record.get("session_id") or "") in session_ids:
            record["committed"] = True
            record["committed_at"] = committed_at
            record["commit_id"] = commit_id
            record["updated_at"] = committed_at
            changed += 1

    write_txt_records(context["uncommit_file"], records)
    return changed


def _archive_commit_preview(context: dict[str, str], commit_payload: dict[str, Any], preview_text: str) -> None:
    data_dir = Path(context.get("data_dir") or Path(context["commit_preview_file"]).parent)
    archive_dir = data_dir / "commits"
    archive_dir.mkdir(parents=True, exist_ok=True)
    commit_id = str(commit_payload.get("commit_id") or uuid.uuid4())
    archive_file = archive_dir / f"{commit_id}.txt"
    archive_file.write_text(preview_text, encoding="utf-8")
    Path(context["commit_preview_file"]).write_text("", encoding="utf-8")


def push_tasks(context: dict[str, str]) -> int:
    """
    处理:
      log push

    行为:
    - 读取 commit_preview.txt 中的机器可读 payload
    - 将 commit 结果同步到 Notion 对应任务详情页或指定日志页
    - 成功后，把 uncommit_tasks.txt 中对应 session 标记为 committed=True
    """

    preview_path = Path(context["commit_preview_file"])

    if not preview_path.exists() or not preview_path.read_text(encoding="utf-8").strip():
        _render_error("没有可 push 的 commit 预览", "请先执行 log commit。")
        return 1

    preview_text = preview_path.read_text(encoding="utf-8")
    commit_payload = _extract_commit_payload(preview_text)
    if not commit_payload:
        _render_error("commit 预览格式不完整", "请重新执行 log commit 生成带机器可读 payload 的预览。")
        return 1

    items = commit_payload.get("items") or []
    if not isinstance(items, list) or not items:
        _render_error("commit 预览为空", "没有可 push 的任务。")
        return 1

    push_items = list(reversed(items))

    try:
        _push_to_notion(push_items, str(commit_payload.get("commit_id") or ""), context)
    except RuntimeError as exc:
        _render_error("Notion 同步失败", str(exc))
        return 1

    changed = _mark_records_committed(context, commit_payload)
    _archive_commit_preview(context, commit_payload, preview_text)
    _render_info("push 完成", f"已同步到 Notion，并标记 {changed} 条 session 为 committed。")
    return 0


# ---------------------------------------------------------------------------
# Override: 统一 page / 非 page block 的结构动作
#
# 规则：
# - create_empty_page / create_child_page: 保留新建空页能力；
# - duplicate_page: page 类 block 复制 with content；
# - duplicate_page_without_content: page 类 block 复制 without content；
# - duplicate_block: 非 page block 只做 duplicate，不区分 with/without content。
#   对 child_database，Notion API 没有公开的“duplicate database block”端点，
#   因此这里用“创建同 schema 的 inline database、但不复制 rows”的方式模拟 duplicate。
# ---------------------------------------------------------------------------

_AI_PAGE_STRUCTURE_ACTIONS = {
    "create_empty_page",
    "create_child_page",
    "duplicate_page",
    "duplicate_page_without_content",
    "duplicate_block",
}
_AI_PUSH_LOOP_ACTIONS = {"write_current", "explore_child", "write_database"} | _AI_PAGE_STRUCTURE_ACTIONS


def _block_text_fragments(block: dict[str, Any]) -> list[str]:
    block_type = str(block.get("type") or "")
    value = block.get(block_type) if block_type else None
    if not isinstance(value, dict):
        value = {}

    fragments: list[str] = []
    text = _plain_text_from_rich_text(value.get("rich_text"))
    if text:
        fragments.append(text)

    if block_type == "table_row":
        for cell in value.get("cells") or []:
            if isinstance(cell, list):
                cell_text = _plain_text_from_rich_text(cell)
                if cell_text:
                    fragments.append(cell_text)

    if block_type in {"child_page", "child_database"} and value.get("title"):
        fragments.append(str(value.get("title") or ""))

    return fragments


def _block_candidate_title(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "unknown")
    value = block.get(block_type) if block_type else None
    if not isinstance(value, dict):
        value = {}

    if block_type in {"child_page", "child_database"}:
        title = str(value.get("title") or "").strip()
        if title:
            return title

    if block_type == "table":
        return "table"

    if block_type == "divider":
        return "divider"

    fragments = _block_text_fragments(block)
    if fragments:
        return _preview_text(" / ".join(fragment.strip() for fragment in fragments if fragment.strip()), 120)
    return block_type


def _extract_child_page_candidates(raw_blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for block in raw_blocks:
        if block.get("type") != "child_page" or not block.get("id"):
            continue
        child_page = block.get("child_page") or {}
        if not isinstance(child_page, dict):
            child_page = {}
        title = str(child_page.get("title") or "未命名子页").strip() or "未命名子页"
        candidates.append(
            {
                "child_index": str(len(candidates) + 1),
                "page_id": str(block["id"]),
                "title": title,
                "block_type": "child_page",
            }
        )
    return candidates


def _extract_block_candidates(raw_blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    把当前页顶层 blocks 作为可引用候选暴露给 AI。

    - child_page 作为 page-like block，可走 page duplicate/explore；
    - child_database 作为 database-like block，可直接 write_database，也可按 schema duplicate；
    - table / list / paragraph 等非 page block 只能走 duplicate_block；
    - block_ref 按页面中的出现顺序递增，因此可以稳定表示“最靠下”的 block。
    """

    candidates: list[dict[str, Any]] = []
    raw_by_ref: dict[str, dict[str, Any]] = {}
    for raw_index, block in enumerate(raw_blocks, start=1):
        block_id = str(block.get("id") or "")
        block_type = str(block.get("type") or "unknown")
        if not block_id or block_type in {"unsupported", "synced_block", "column_list", "column"}:
            continue

        block_ref = str(len(candidates) + 1)
        title = _block_candidate_title(block)
        is_page_like = block_type == "child_page"
        is_database_like = block_type == "child_database"
        candidate = {
            "block_ref": block_ref,
            "position_index": raw_index,
            "block_id": block_id,
            "block_type": block_type,
            "title": title,
            "is_page_like": is_page_like,
            "is_database_like": is_database_like,
            "can_explore": is_page_like,
            "can_write_database": is_database_like,
            "can_duplicate_page": is_page_like,
            "can_duplicate_block": not is_page_like,
        }
        if block_type == "child_database":
            candidate["write_note"] = "可作为已有 database 写入目标；不会写回父页。"
            candidate["duplicate_note"] = "按 database schema duplicate；不复制 rows。"
        candidates.append(candidate)
        raw_by_ref[block_ref] = block

    return candidates, raw_by_ref


def _public_block_candidates_for_llm(block_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for candidate in block_candidates:
        public.append(
            {
                "block_ref": candidate.get("block_ref"),
                "position_index": candidate.get("position_index"),
                "block_type": candidate.get("block_type"),
                "title": candidate.get("title"),
                "is_page_like": candidate.get("is_page_like"),
                "can_explore": candidate.get("can_explore"),
                "can_write_database": candidate.get("can_write_database", False),
                "can_duplicate_page": candidate.get("can_duplicate_page"),
                "can_duplicate_block": candidate.get("can_duplicate_block"),
                "is_database_like": candidate.get("is_database_like", False),
                "write_note": candidate.get("write_note", ""),
                "duplicate_note": candidate.get("duplicate_note", ""),
            }
        )
    return public


def _fetch_push_page_state(page_id: str, token: str) -> dict[str, Any]:
    """
    读取当前候选页，让 AI 决定下一步动作。

    LLM 只看到 page/block 的稳定引用和标题，不直接看到 Notion id。
    程序内部保留 _raw_blocks_by_ref，用于执行 duplicate_block。
    """

    page_data = _notion_request("GET", f"/pages/{page_id}", token)
    raw_blocks = _fetch_top_level_blocks(page_id, token, max_blocks=240)
    child_pages = _extract_child_page_candidates(raw_blocks)
    block_candidates, raw_blocks_by_ref = _extract_block_candidates(raw_blocks)
    extra_instructions = _fetch_extra_instructions_from_page_blocks(raw_blocks, token)
    sample_blocks = _truncate_format_blocks(raw_blocks, max_blocks=80)
    simplified_blocks = [_simplify_block_for_format_with_instruction_flag(block) for block in sample_blocks]

    plain_lines: list[str] = []
    for block in simplified_blocks:
        if block.get("is_extra_instruction"):
            continue
        block_type = str(block.get("type") or "")
        text = str(block.get("text") or block.get("title") or "")
        if block_type == "table_row" and block.get("cells"):
            text = " | ".join(map(str, block.get("cells") or []))
        if text:
            plain_lines.append(f"{block_type}: {text}")

    page_title = _page_title_from_page_data(page_data) or "未命名页面"
    available_pages = [{"page_ref": "current", "page_id": page_id, "title": page_title, "role": "current_page"}]
    available_pages.extend(
        {
            "page_ref": child["child_index"],
            "page_id": child.get("page_id"),
            "title": child["title"],
            "role": "child_page",
        }
        for child in child_pages
    )

    return {
        "page_id": page_id,
        "page_title": page_title,
        "top_level_block_count_sampled": len(simplified_blocks),
        "child_pages": child_pages,
        "available_pages": available_pages,
        "available_blocks": _public_block_candidates_for_llm(block_candidates),
        "_block_candidates": block_candidates,
        "_raw_blocks_by_ref": raw_blocks_by_ref,
        "extra_instructions": extra_instructions,
        "format_blocks": simplified_blocks,
        "plain_text_outline": "\n".join(plain_lines[-90:]),
    }


def _ai_action_label(action: str) -> str:
    return {
        "write_current": "写入当前页",
        "write_database": "写入已有 database",
        "explore_child": "继续探索子页",
        "create_empty_page": "新建空页",
        "create_child_page": "新建子页",
        "duplicate_page": "复制页（with content）",
        "duplicate_page_without_content": "复制页（without content）",
        "duplicate_block": "复制非 page block",
    }.get(action, action or "未知动作")


def _render_ai_push_action(
    step: int,
    max_steps: int,
    page_state: dict[str, Any],
    decision: dict[str, Any],
    *,
    selected_child_title: str = "",
    note: str = "",
) -> None:
    action = str(decision.get("action") or "write_current")
    action_label = _ai_action_label(action)
    page_title = str(page_state.get("page_title") or "未命名页面")
    page_id = _safe_short_page_id(str(page_state.get("page_id") or ""))
    page_kind = str(decision.get("page_kind") or "unknown")
    observation = str(decision.get("observation") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    new_page_title = str(decision.get("new_page_title") or decision.get("title") or "").strip()
    source_page = str(decision.get("source_page") or decision.get("source_child_index") or "").strip()
    source_block = str(
        decision.get("source_block")
        or decision.get("source_block_ref")
        or decision.get("database_block")
        or decision.get("source_database")
        or ""
    ).strip()
    target_parent = str(decision.get("target_parent") or decision.get("target_parent_index") or "").strip()
    instruction_refs = decision.get("instruction_refs") or decision.get("instruction_index") or ""
    confidence = decision.get("confidence")
    confidence_text = ""
    if isinstance(confidence, (int, float)):
        confidence_text = f"{float(confidence):.2f}"

    instruction_count = len(page_state.get("extra_instructions") or [])

    if not RICH_AVAILABLE:
        print(f"AI Notion push 循环 {step}/{max_steps}")
        print(f"页面: {page_title} ({page_id})")
        print(f"判断: {page_kind}")
        print(f"动作: {action_label}")
        if instruction_count:
            print(f"额外指令: {instruction_count} 条")
        if selected_child_title:
            print(f"子页: {selected_child_title}")
        if source_page:
            print(f"来源页引用: {source_page}")
        if source_block:
            print(f"来源 block 引用: {source_block}")
        if target_parent:
            print(f"目标父页引用: {target_parent}")
        if new_page_title:
            print(f"新页标题: {new_page_title}")
        if instruction_refs:
            print(f"依据指令: {instruction_refs}")
        if confidence_text:
            print(f"置信度: {confidence_text}")
        if observation:
            print(f"观察: {observation}")
        if reason:
            print(f"理由: {reason}")
        if note:
            print(f"结果: {note}")
        return

    if action in _AI_PAGE_STRUCTURE_ACTIONS:
        border_style = "magenta"
    elif action == "explore_child":
        border_style = "cyan"
    elif action == "write_database":
        border_style = "blue"
    else:
        border_style = "green"

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    # 第二列必须 fold 换行，不能使用 Rich 默认 ellipsis，否则长观察/理由会显示为“.../…”
    table.add_column(style="white", overflow="fold", no_wrap=False, ratio=1)
    table.add_row("当前页", f"{page_title} ({page_id})")
    table.add_row("页面判断", page_kind)
    table.add_row("AI 动作", action_label)
    if instruction_count:
        table.add_row("额外指令", f"{instruction_count} 条")
    if selected_child_title:
        table.add_row("探索子页", selected_child_title)
    if source_page:
        table.add_row("来源页引用", source_page)
    if source_block:
        table.add_row("来源 block 引用", source_block)
    if target_parent:
        table.add_row("目标父页引用", target_parent)
    if new_page_title:
        table.add_row("新页标题", _preview_text(new_page_title, 300))
    if instruction_refs:
        table.add_row("依据指令", _preview_text(str(instruction_refs), 300))
    if confidence_text:
        table.add_row("置信度", confidence_text)
    if observation:
        table.add_row("观察", Text(observation, style="white", overflow="fold", no_wrap=False))
    if reason:
        table.add_row("理由", Text(reason, style="white", overflow="fold", no_wrap=False))
    if note:
        table.add_row("执行结果", Text(note, style="white", overflow="fold", no_wrap=False))

    console.print(
        Panel(
            table,
            title=Text(f"AI Notion push 循环 {step}/{max_steps}", style=f"bold {border_style}"),
            border_style=border_style,
            box=box.ROUNDED,
            expand=False,
        )
    )


def _coerce_explore_decision(result: dict[str, Any] | None, fallback_reason: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "page_kind": "unknown",
            "observation": "AI 未返回有效 JSON 决策。",
            "action": "write_current",
            "reason": fallback_reason,
            "confidence": 0,
        }

    raw_action = str(result.get("action") or "write_current").strip()
    action_aliases = {
        "create_page": "create_empty_page",
        "create_empty_child_page": "create_empty_page",
        "new_empty_page": "create_empty_page",
        "duplicate_with_content": "duplicate_page",
        "duplicate_page_with_content": "duplicate_page",
        "duplicate_without_content": "duplicate_page_without_content",
        "duplicate_empty_page": "duplicate_page_without_content",
        "duplicate_page_empty": "duplicate_page_without_content",
        "duplicate_non_page": "duplicate_block",
        "duplicate_non_page_block": "duplicate_block",
        "write_child_database": "write_database",
        "write_existing_database": "write_database",
        "route_database": "write_database",
        "route_to_database": "write_database",
        "use_database": "write_database",
        "duplicate_database": "duplicate_block",
        "duplicate_database_without_content": "duplicate_block",
        "duplicate_block_without_content": "duplicate_block",
    }
    action = action_aliases.get(raw_action, raw_action)
    if action not in _AI_PUSH_LOOP_ACTIONS:
        action = "write_current"

    decision: dict[str, Any] = {
        "page_kind": str(result.get("page_kind") or "unknown"),
        "observation": str(result.get("observation") or ""),
        "action": action,
        "reason": str(result.get("reason") or fallback_reason),
        "confidence": result.get("confidence", 0),
    }
    passthrough_keys = (
        "child_index",
        "source_page",
        "source_child_index",
        "source_block",
        "source_block_ref",
        "database_block",
        "source_database",
        "target_parent",
        "target_parent_index",
        "new_page_title",
        "title",
        "seed_markdown",
        "initial_markdown",
        "instruction_refs",
        "instruction_index",
    )
    for key in passthrough_keys:
        if result.get(key) is not None:
            decision[key] = result.get(key)
    return decision


def _decide_next_push_page(
    page_state: dict[str, Any],
    items: list[dict[str, Any]],
    commit_id: str,
    context: dict[str, str],
    *,
    step: int,
    max_steps: int,
    visited_titles: list[str],
    executed_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    child_pages_for_llm = [
        {"child_index": child["child_index"], "title": child["title"]}
        for child in page_state.get("child_pages") or []
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Notion 任务记录 push 自动落点与页面结构操作器。你每一轮只能选择一个动作。"
                "::| 额外指令既是页面结构操作指令，也是本次 commit 的落点路由规则；不能只把它理解为创建/复制动作。"
                "优先级从高到低：1) 明确适用的 ::| 路由/结构指令；2) 明显更精确的 child_database 写入或子页探索；3) write_current fallback。"
                "当前页永远可以作为 fallback 写入目标，但 fallback 不能压过已经满足条件且引用可解析的 ::| 指令。"
                "只有在没有适用的 ::| 路由/页面结构指令、或缺少可解析引用/必要参数时，才因为不确定而 write_current。"
                "当前页像任务记录页时通常 write_current；当前页格式不对、看不懂或没有更好落点时也 write_current。"
                "如果 ::| 指令包含“已经存在正确时间的子页就写入该子页/复制到该子页”这类条件，"
                "你必须先根据 commit.items 的开始/结束时间推断目标时间段，再检查 available_block_refs/child_pages/available_page_refs 中是否已有匹配标题；"
                "若已有匹配 child_database，必须选择 write_database 并给出 source_block，不要 duplicate，也不要 write_current。"
                "若已有匹配 child_page，必须选择 explore_child 并给出 child_index，不要 duplicate，也不要 write_current。"
                "只有确认不存在匹配 child_page / child_database，且 ::| 明确要求创建/复制缺失页时，才执行 create/duplicate 动作。"
                "当前页明显是目录页、总览页、日/周/月索引页，并且已有匹配 child_database 时，选择 write_database；如果匹配的是 child_page，才 explore_child。"
                "如果当前页中存在以 ::| 开头的额外指令，你必须先根据指令语义判断是否需要路由到已有子页，或执行页面结构动作。"
                "页面结构动作仅在额外指令明确要求且没有可用目标子页时使用。动作语义如下："
                "create_empty_page/create_child_page=新建空页；"
                "duplicate_page=复制 page with content；"
                "duplicate_page_without_content=复制 page without content；"
                "write_database=写入已有 child_database；"
                "duplicate_block=复制非 page block/inline database，不区分 with/without content。"
                "如果指令说 Page/页面，但当前页可见候选里对应周标题实际是 child_database，"
                "应优先把它理解为用户口语里的页面/数据库落点，选择 write_database 写入已有 database；只有指令明确要求复制缺失目标时才 duplicate_block。"
                "如果指令要求复制 database、最靠下的 Database、或对非 page block duplicate without content，"
                "你必须选择 duplicate_block；source_block 用 available_block_refs 中对应 block_ref，无法精确匹配时可用 last_database。"
                "若指令说“最后的一个标题为 X月 第X周”，你可以根据 title 和 position_index 选择最靠下/最后一个周标题候选。"
                "若指令要求缺失周页面/周数据库，new_page_title 应根据 commit.items 的开始/结束时间推断目标周标题。"
                "不要重复执行 already_executed_actions 中已经完成的同类指令。"
                "如果不确定、动作参数不明确、没有对应页面/block 引用，必须 write_current，不能卡住。"
                "如果 is_last_step=true，必须 write_current。必须返回 JSON object。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "now": datetime.now().isoformat(timespec="seconds"),
                    "loop": {
                        "step": step,
                        "max_steps": max_steps,
                        "is_last_step": step >= max_steps,
                        "visited_page_titles": visited_titles,
                        "already_executed_actions": executed_actions or [],
                    },
                    "current_page": {
                        "title": page_state.get("page_title"),
                        "sampled_block_count": page_state.get("top_level_block_count_sampled"),
                        "plain_text_outline": page_state.get("plain_text_outline"),
                        "format_blocks": page_state.get("format_blocks"),
                        "child_pages": child_pages_for_llm,
                        "available_page_refs": page_state.get("available_pages"),
                        "available_block_refs": page_state.get("available_blocks"),
                        "extra_instructions": page_state.get("extra_instructions") or [],
                    },
                    "commit": {
                        "commit_id": commit_id,
                        "items": _compact_commit_items_for_llm(items),
                    },
                    "available_actions": {
                        "write_current": "把本次 commit 写入当前页；这是永远可用的 fallback。",
                        "write_database": "把本次 commit 作为 rows 写入已有 child_database；source_block 来自 available_block_refs 中 can_write_database=true 的 block_ref，或使用 last_database。",
                        "explore_child": "进入某个 child_page；仅当当前页明显是目录/总览/索引页时使用。需要 child_index。",
                        "create_empty_page": "按 ::| 指令，在 target_parent 下创建空白 page；target_parent 来自 available_page_refs；需要 new_page_title。",
                        "create_child_page": "create_empty_page 的兼容别名；默认不要放 seed_markdown，除非指令明确要求初始化内容。",
                        "duplicate_page": "按 ::| 指令复制 page with content；source_page/target_parent 来自 available_page_refs；可选 new_page_title。",
                        "duplicate_page_without_content": "按 ::| 指令复制 page without content；source_page/target_parent 来自 available_page_refs；可选 new_page_title。",
                        "duplicate_block": "按 ::| 指令复制非 page block；source_block 来自 available_block_refs，或使用 last_database/last_non_page/last_block；target_parent 来自 available_page_refs。",
                    },
                    "decision_rules": [
                        "write_current 是安全 fallback，但当 extra_instructions 明确要求且条件满足、引用可解析时，必须先执行对应路由或 create/duplicate 动作。",
                        "extra_instructions 不只用于 create/duplicate；若指令规定“已有正确时间子页则写入该子页”，它也是 write_database / explore_child 的依据。",
                        "先根据 commit.items 的结束时间/开始时间推断目标周/月标题，再与 child_pages、available_page_refs、available_block_refs 的 title 比较。",
                        "如果已存在正确时间的 child_database，必须选择 write_database，source_block 使用匹配 database 的 block_ref；不要 duplicate，不要 write_current。",
                        "如果已存在正确时间的 child_page，必须选择 explore_child，child_index 使用匹配子页编号；不要 duplicate，不要 write_current。",
                        "只有确认没有正确时间的 child_page / child_database，且 ::| 指令要求缺失时复制/创建，才选择 create/duplicate。",
                        "当前页像任务记录页、时间日志页、日报页、周报页、任务详情页，且没有适用 ::| 路由或结构指令时，选择 write_current。",
                        "当前页格式不对或看不懂时，也选择 write_current，不要失败。",
                        "当前页明显像目录/总览/日周月索引，且 available_block_refs 中有正确时间 child_database 时，选择 write_database。",
                        "当前页明显像目录/总览/日周月索引，且 child_pages 中有正确时间或明显更精确落点时，选择 explore_child。",
                        "create/duplicate 动作只在 extra_instructions 明确要求且找不到已有正确目标时使用，不能自行发明页面结构操作。",
                        "如果 extra_instructions 要求复制最靠下/最后一个周标题 Page，但实际候选是 child_database/非 page block，选择 duplicate_block，并设置 source_block 为对应 block_ref 或 last_database。",
                        "如果 extra_instructions 要求复制最靠下的 Database，选择 duplicate_block，并设置 source_block=last_database。",
                        "如果 extra_instructions 要求 duplicate without content，但来源不是 page，选择 duplicate_block；不要创建空页。",
                        "若要创建缺失的周页面/周数据库标题，由 LLM 根据 commit.items 时间与 ::| 指令推断 new_page_title。",
                        "如果 is_last_step=true，不能继续探索或创建/复制页面；若已有明确匹配 child_database 可以选择 write_database，否则选择 write_current。",
                        "所有 page 引用都必须来自 available_page_refs；所有 block 引用都必须来自 available_block_refs 或 last_database/last_non_page/last_block；不要输出 Notion id。",
                    ],
                    "output_schema": {
                        "page_kind": "task_record | directory_or_index | wrong_format | empty | unknown",
                        "observation": "一句话说明看到了什么格式/指令",
                        "action": "write_current | write_database | explore_child | create_empty_page | create_child_page | duplicate_page | duplicate_page_without_content | duplicate_block",
                        "child_index": "仅当 action=explore_child 时填写 child_pages 中的编号",
                        "source_page": "duplicate_page / duplicate_page_without_content 时填写 current 或 child_index",
                        "source_block": "write_database / duplicate_block 时填写 block_ref；write_database 只能使用 child_database 的 block_ref，也可用 last_database",
                        "target_parent": "create/duplicate 时填写 current 或 child_index；默认 current",
                        "new_page_title": "create/duplicate 时的新页面或新 database 标题",
                        "seed_markdown": "仅 create_child_page 明确要求初始化内容时使用；不要放本次 commit 内容",
                        "instruction_refs": "使用了哪些 ::| 指令编号",
                        "reason": "一句话说明为什么这样做",
                        "confidence": "0 到 1 的数字",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    return _coerce_explore_decision(
        _openai_json(messages, context),
        "为了保证 push 不被卡住，回退到当前页写入。",
    )


def _resolve_block_ref(page_state: dict[str, Any], block_ref: Any) -> dict[str, Any] | None:
    ref = str(block_ref or "").strip()
    ref = ref.replace("block:", "").replace("block_", "").strip()
    candidates = page_state.get("_block_candidates") or []

    def public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        raw = (page_state.get("_raw_blocks_by_ref") or {}).get(str(candidate.get("block_ref")))
        return {
            "block_ref": str(candidate.get("block_ref") or ""),
            "block_id": str(candidate.get("block_id") or ""),
            "block_type": str(candidate.get("block_type") or "unknown"),
            "title": str(candidate.get("title") or "未命名 block"),
            "position_index": int(candidate.get("position_index") or 0),
            "raw_block": raw,
        }

    if not candidates:
        return None

    lowered = ref.lower().replace(" ", "_")
    if lowered in {"last_database", "last_child_database", "bottom_database", "last_db", "最靠下的database", "最下面的database", "最下方的database"}:
        for candidate in reversed(candidates):
            if str(candidate.get("block_type")) == "child_database":
                return public_candidate(candidate)
        return None

    if lowered in {"last_non_page", "last_non_page_block", "bottom_non_page", "最靠下的非page", "最靠下的非页面"}:
        for candidate in reversed(candidates):
            if not candidate.get("is_page_like"):
                return public_candidate(candidate)
        return None

    if lowered in {"last_block", "bottom_block", "最靠下的block", "最下面的block"}:
        return public_candidate(candidates[-1])

    for candidate in candidates:
        if str(candidate.get("block_ref")) == ref:
            return public_candidate(candidate)

    # 允许模型偶尔用标题作为引用，但仍然只在候选内匹配。
    for candidate in candidates:
        title = str(candidate.get("title") or "")
        if ref and ref == title:
            return public_candidate(candidate)
    return None


def _resolve_database_ref(page_state: dict[str, Any], database_ref: Any) -> dict[str, Any] | None:
    """
    将 LLM 返回的 source_block/source_database/database_block 安全解析为已有 child_database。

    只允许当前页 available_block_refs 中已经暴露的 child_database，避免模型伪造 Notion id。
    """

    source_block = _resolve_block_ref(page_state, database_ref)
    if not source_block:
        return None
    if str(source_block.get("block_type") or "") != "child_database":
        return None
    if not str(source_block.get("block_id") or ""):
        return None
    return source_block


def _database_target_state_from_block(page_state: dict[str, Any], source_database: dict[str, Any]) -> dict[str, Any]:
    target_title = str(source_database.get("title") or "未命名 database")
    target_state = dict(page_state)
    target_state["_write_target_type"] = "database"
    target_state["_write_target_id"] = str(source_database.get("block_id") or "")
    target_state["_write_target_title"] = target_title
    target_state["page_title"] = target_title
    return target_state


def _strip_schema_ids(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, nested in value.items():
            # id 是 Notion 现有 schema 的内部标识，创建新 database 时不能复用。
            if key == "id":
                continue
            clean[key] = _strip_schema_ids(nested)
        return clean
    if isinstance(value, list):
        return [_strip_schema_ids(item) for item in value]
    return value


def _database_title_from_data(database_data: dict[str, Any]) -> str:
    title = _plain_text_from_rich_text(database_data.get("title"))
    if title:
        return title
    return str(database_data.get("id") or "Database")


def _clone_database_schema_without_rows(
    source_database_id: str,
    target_parent_page_id: str,
    title: str,
    token: str,
) -> str:
    """
    Notion 公共 API 没有直接 duplicate database block 的端点。
    对 child_database 的 duplicate 采用“复制 schema、创建空 inline database”的方式实现。
    """

    database_data = _notion_request("GET", f"/databases/{source_database_id}", token)
    source_properties = database_data.get("properties") or {}
    if not isinstance(source_properties, dict) or not source_properties:
        raise RuntimeError("复制 database 失败：源 database 没有可复制的 properties。")

    properties: dict[str, Any] = {}
    for prop_name, prop in source_properties.items():
        if not isinstance(prop, dict):
            continue
        prop_type = str(prop.get("type") or "")
        if not prop_type:
            continue
        properties[str(prop_name)] = _strip_schema_ids({prop_type: prop.get(prop_type) or {}})

    if not properties:
        raise RuntimeError("复制 database 失败：没有可创建的新 database schema。")

    clean_title = (title or _database_title_from_data(database_data) or "Database Copy").strip()[:2000]
    payload: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": target_parent_page_id},
        "title": _text_to_rich_text(clean_title),
        "properties": properties,
        "is_inline": bool(database_data.get("is_inline", True)),
    }
    created = _notion_request("POST", "/databases", token, payload)
    new_database_id = str(created.get("id") or "")
    if not new_database_id:
        raise RuntimeError("复制 database 失败：Notion API 未返回新 database id。")
    return new_database_id


def _append_duplicate_non_page_block(
    source: dict[str, Any],
    target_parent_page_id: str,
    token: str,
    context: dict[str, str],
    *,
    title: str = "",
) -> str:
    block_type = str(source.get("block_type") or "")
    block_id = str(source.get("block_id") or "")
    raw_block = source.get("raw_block")
    if not block_id:
        raise RuntimeError("复制 block 失败：缺少 source_block id。")

    if block_type == "child_page":
        raise RuntimeError("source_block 指向 page。page 请使用 duplicate_page 或 duplicate_page_without_content。")

    if block_type == "child_database":
        new_database_id = _clone_database_schema_without_rows(
            block_id,
            target_parent_page_id,
            title or str(source.get("title") or "Database Copy"),
            token,
        )
        return f"已复制 database schema 为新的 inline database：{title or source.get('title') or 'Database Copy'} ({_safe_short_page_id(new_database_id)})。"

    if not isinstance(raw_block, dict):
        raw_block = _notion_request("GET", f"/blocks/{block_id}", token)

    clean_block = _clone_block_for_append(
        raw_block,
        token,
        depth=0,
        max_depth=_get_duplicate_max_depth(context),
    )
    if not clean_block:
        raise RuntimeError(f"复制 block 失败：暂不支持复制 block 类型 {block_type}。")

    _append_blocks_to_page(target_parent_page_id, [clean_block], token)
    return f"已复制非 page block：{source.get('title') or block_type}。"


def _execute_page_structure_action(
    decision: dict[str, Any],
    page_state: dict[str, Any],
    token: str,
    context: dict[str, str],
) -> tuple[dict[str, Any], str]:
    action = str(decision.get("action") or "")
    target_parent = _resolve_page_ref(page_state, _decision_target_parent_ref(decision))
    if not target_parent or not target_parent.get("page_id"):
        raise RuntimeError("页面结构动作缺少有效 target_parent；已回退到当前页。")

    if action in {"create_empty_page", "create_child_page"}:
        title = _decision_new_page_title(decision, "新任务记录页")
        seed_markdown = ""
        if action == "create_child_page":
            seed_markdown = str(decision.get("seed_markdown") or decision.get("initial_markdown") or "").strip()
        seed_blocks = _markdown_to_basic_blocks(seed_markdown) if seed_markdown else []
        new_page_id = _create_child_page(target_parent["page_id"], title, token, seed_blocks)
        new_state = _fetch_push_page_state(new_page_id, token)
        seed_note = "，并写入初始化内容" if seed_blocks else ""
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下创建空页：{title}{seed_note}。Notion API 会把新页追加到目标父页末尾。",
        )

    if action in {"duplicate_page", "duplicate_page_without_content"}:
        source_page = _resolve_page_ref(page_state, _decision_source_page_ref(decision))
        if not source_page or not source_page.get("page_id"):
            raise RuntimeError("复制 page 动作缺少有效 source_page；已回退到当前页。")

        title = _decision_new_page_title(decision, str(source_page.get("title") or "模板页"))
        children: list[dict[str, Any]] = []
        if action == "duplicate_page":
            children = _clone_page_blocks_for_append(
                source_page["page_id"],
                token,
                max_blocks=_get_duplicate_max_blocks(context),
                max_depth=_get_duplicate_max_depth(context),
            )
        new_page_id = _create_child_page(target_parent["page_id"], title, token, children)
        new_state = _fetch_push_page_state(new_page_id, token)
        mode = "with content" if action == "duplicate_page" else "without content"
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下基于 {source_page.get('title')} 复制 page：{title}（{mode}）。",
        )

    if action == "duplicate_block":
        source_block_ref = decision.get("source_block") or decision.get("source_block_ref")
        source_block = _resolve_block_ref(page_state, source_block_ref)
        if not source_block:
            raise RuntimeError("复制 block 动作缺少有效 source_block；已回退到当前页。")
        if str(source_block.get("block_type")) == "child_page":
            raise RuntimeError("source_block 是 page；请使用 duplicate_page / duplicate_page_without_content。")

        title = _decision_new_page_title(decision, str(source_block.get("title") or "Block Copy"))
        result_note = _append_duplicate_non_page_block(
            source_block,
            target_parent["page_id"],
            token,
            context,
            title=title,
        )
        refreshed_parent_state = _fetch_push_page_state(target_parent["page_id"], token)
        return (
            refreshed_parent_state,
            f"{result_note} 目标父页：{target_parent.get('title')}。",
        )

    raise RuntimeError(f"未知页面结构动作: {action}")



# ---------------------------------------------------------------------------
# V2 修正：页面结构动作创建/复制出的新对象，必须成为本次 commit 的写入目标。
# - page/create/duplicate 后直接锁定新 page，不再让下一轮 fallback 回父页。
# - child_database duplicate 后锁定新 database，并把 commit 写成 database rows，避免写回父页。
# ---------------------------------------------------------------------------


def _append_duplicate_non_page_block(
    source: dict[str, Any],
    target_parent_page_id: str,
    token: str,
    context: dict[str, str],
    *,
    title: str = "",
) -> dict[str, Any]:
    """
    复制非 page block，并返回结构化目标信息。

    旧版只返回字符串 note。对 child_database 来说，这会让执行层只能刷新父页，
    后续 commit 就容易写回总览页。新版返回 target_type/target_id，供 push 层锁定新目标。
    """

    block_type = str(source.get("block_type") or "")
    block_id = str(source.get("block_id") or "")
    raw_block = source.get("raw_block")
    if not block_id:
        raise RuntimeError("复制 block 失败：缺少 source_block id。")

    if block_type == "child_page":
        raise RuntimeError("source_block 指向 page。page 请使用 duplicate_page 或 duplicate_page_without_content。")

    if block_type == "child_database":
        clean_title = title or str(source.get("title") or "Database Copy")
        new_database_id = _clone_database_schema_without_rows(
            block_id,
            target_parent_page_id,
            clean_title,
            token,
        )
        return {
            "note": f"已复制 database schema 为新的 inline database：{clean_title} ({_safe_short_page_id(new_database_id)})。",
            "target_type": "database",
            "target_id": new_database_id,
            "target_title": clean_title,
        }

    if not isinstance(raw_block, dict):
        raw_block = _notion_request("GET", f"/blocks/{block_id}", token)

    clean_block = _clone_block_for_append(
        raw_block,
        token,
        depth=0,
        max_depth=_get_duplicate_max_depth(context),
    )
    if not clean_block:
        raise RuntimeError(f"复制 block 失败：暂不支持复制 block 类型 {block_type}。")

    _append_blocks_to_page(target_parent_page_id, [clean_block], token)
    return {
        "note": f"已复制非 page block：{source.get('title') or block_type}。",
        "target_type": "page",
        "target_id": target_parent_page_id,
        "target_title": str(source.get("title") or block_type),
    }


def _simplify_database_schema_for_llm(database_data: dict[str, Any]) -> dict[str, Any]:
    properties: list[dict[str, Any]] = []
    for prop_name, prop in (database_data.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        prop_type = str(prop.get("type") or "")
        if not prop_type:
            continue
        entry: dict[str, Any] = {"name": str(prop_name), "type": prop_type}
        if prop_type in {"select", "multi_select", "status"}:
            options = (prop.get(prop_type) or {}).get("options") or []
            entry["options"] = [str(option.get("name") or "") for option in options if isinstance(option, dict)]
        properties.append(entry)
    return {
        "database_title": _database_title_from_data(database_data),
        "properties": properties,
    }


def _notion_property_from_simple_value(prop_type: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None

    if isinstance(value, dict):
        # LLM 可返回 {type: ..., value: ...} 或 Notion 原生结构。优先使用 value。
        if prop_type in value and isinstance(value.get(prop_type), (dict, list, str, int, float, bool)):
            # 已经像 Notion 原生结构时，做最小信任包装。
            return {prop_type: value.get(prop_type)}
        value = value.get("value", value.get("text", value.get("name", value.get("content"))))

    if prop_type == "title":
        text = str(value or "").strip()
        return {"title": _split_rich_text_content(text[:2000])} if text else None

    if prop_type == "rich_text":
        text = str(value or "").strip()
        return {"rich_text": _split_rich_text_content(text)} if text else None

    if prop_type == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None

    if prop_type == "select":
        name = str(value or "").strip()
        return {"select": {"name": name}} if name else None

    if prop_type == "status":
        name = str(value or "").strip()
        return {"status": {"name": name}} if name else None

    if prop_type == "multi_select":
        if isinstance(value, str):
            names = [part.strip() for part in re.split(r"[,，、]", value) if part.strip()]
        elif isinstance(value, list):
            names = [str(part).strip() for part in value if str(part).strip()]
        else:
            names = []
        return {"multi_select": [{"name": name} for name in names]} if names else None

    if prop_type == "date":
        if isinstance(value, dict):
            start = value.get("start") or value.get("value")
            end = value.get("end")
        else:
            start = value
            end = None
        start_text = str(start or "").strip()
        if not start_text:
            return None
        payload: dict[str, Any] = {"start": start_text}
        if end:
            payload["end"] = str(end)
        return {"date": payload}

    if prop_type == "checkbox":
        if isinstance(value, bool):
            checked = value
        else:
            checked = str(value).strip().lower() in {"1", "true", "yes", "y", "是", "已完成", "完成"}
        return {"checkbox": checked}

    if prop_type in {"url", "email", "phone_number"}:
        text = str(value or "").strip()
        return {prop_type: text} if text else None

    # formula/rollup/created_time/last_edited_time 等不可写属性跳过。
    return None


def _sanitize_database_entry_properties(raw_properties: Any, database_data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_properties, dict):
        return {}

    schema = database_data.get("properties") or {}
    clean: dict[str, Any] = {}
    for prop_name, raw_value in raw_properties.items():
        prop = schema.get(str(prop_name))
        if not isinstance(prop, dict):
            continue
        prop_type = str(prop.get("type") or "")
        notion_value = _notion_property_from_simple_value(prop_type, raw_value)
        if notion_value is not None:
            clean[str(prop_name)] = notion_value
    return clean


def _default_database_entry_properties(item: dict[str, Any], database_data: dict[str, Any]) -> dict[str, Any]:
    """LLM 不可用时的保底，只负责属性落盘，不参与 ::| 指令决策。"""

    schema = database_data.get("properties") or {}
    raw: dict[str, Any] = {}
    for prop_name, prop in schema.items():
        if not isinstance(prop, dict):
            continue
        prop_type = str(prop.get("type") or "")
        name = str(prop_name)
        lowered = name.lower()
        value: Any = None
        if prop_type == "title":
            value = item.get("任务名") or "未命名任务"
        elif "周" in name:
            value = item.get("周几")
        elif any(key in name for key in ("小时", "时长", "持续")) or "hour" in lowered or "duration" in lowered:
            value = float(item.get("持续小时") or 0) if prop_type == "number" else _format_push_hours(float(item.get("持续小时") or 0))
        elif "类别" in name or "分类" in name or "category" in lowered:
            value = item.get("类别")
        elif "开始" in name or "start" in lowered:
            value = item.get("start_time")
        elif "结束" in name or "end" in lowered:
            value = item.get("end_time")
        elif "commit" in lowered:
            value = item.get("commit_id")
        if value is not None:
            raw[name] = value
    return _sanitize_database_entry_properties(raw, database_data)


def _database_rows_from_llm(
    database_data: dict[str, Any],
    items: list[dict[str, Any]],
    commit_id: str,
    context: dict[str, str],
) -> list[dict[str, Any]]:
    compact_items = _compact_commit_items_for_llm(items)
    for item in compact_items:
        item["commit_id"] = commit_id

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Notion database 写入格式适配器。你会收到 database schema 和 commit items。"
                "请只根据 schema 把每个 commit item 映射成 Notion database row properties。"
                "不要改变任务名、周几、持续小时、类别和时间。必须返回 JSON object，不要 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "database_schema": _simplify_database_schema_for_llm(database_data),
                    "commit": {"commit_id": commit_id, "items": compact_items},
                    "rules": [
                        "每个输入 item 输出一行 row。",
                        "properties 的 key 必须是 database_schema.properties 中真实存在的属性名。",
                        "每个属性值用 {type: 属性类型, value: 值}，也可以直接给 value。",
                        "title 属性通常写任务名；number 类型的时长写数字；非 number 类型的时长写持续小时文本，格式如 1.5H；date 类型可写 start/end。",
                        "无法可靠映射的属性跳过，不要发明不存在的属性。",
                    ],
                    "output_schema": {
                        "rows": [
                            {
                                "source_index": 1,
                                "properties": {
                                    "任务名": {"type": "title", "value": "示例任务"},
                                    "持续小时": {"type": "number", "value": 1.5},
                                },
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result = _openai_json(messages, context)
    rows: list[dict[str, Any]] = []
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        for row in result["rows"]:
            if not isinstance(row, dict):
                continue
            props = _sanitize_database_entry_properties(row.get("properties"), database_data)
            if props:
                rows.append(props)

    if len(rows) != len(items):
        rows = []
        for item in items:
            enriched = dict(item)
            enriched["commit_id"] = commit_id
            props = _default_database_entry_properties(enriched, database_data)
            if props:
                rows.append(props)

    return rows


def _push_items_to_database_with_ai(
    database_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> None:
    database_data = _notion_request("GET", f"/databases/{database_id}", token)
    rows = _database_rows_from_llm(database_data, items, commit_id, context)
    if not rows:
        raise RuntimeError("无法根据新 database 的 schema 生成可写入的 commit rows；已取消 push，避免写回父页。")

    for props in rows:
        _notion_request(
            "POST",
            "/pages",
            token,
            {
                "parent": {"database_id": database_id},
                "properties": props,
            },
        )

    _render_info(
        "Notion push 落点",
        f"最终写入 database: {_database_title_from_data(database_data)} ({_safe_short_page_id(database_id)})\n已创建 rows: {len(rows)}",
    )


def _execute_page_structure_action(
    decision: dict[str, Any],
    page_state: dict[str, Any],
    token: str,
    context: dict[str, str],
) -> tuple[dict[str, Any], str]:
    action = str(decision.get("action") or "")
    target_parent = _resolve_page_ref(page_state, _decision_target_parent_ref(decision))
    if not target_parent or not target_parent.get("page_id"):
        raise RuntimeError("页面结构动作缺少有效 target_parent；已回退到当前页。")

    if action in {"create_empty_page", "create_child_page"}:
        title = _decision_new_page_title(decision, "新任务记录页")
        seed_markdown = ""
        if action == "create_child_page":
            seed_markdown = str(decision.get("seed_markdown") or decision.get("initial_markdown") or "").strip()
        seed_blocks = _markdown_to_basic_blocks(seed_markdown) if seed_markdown else []
        new_page_id = _create_child_page(target_parent["page_id"], title, token, seed_blocks)
        new_state = _fetch_push_page_state(new_page_id, token)
        new_state["_created_by_structure_action"] = action
        seed_note = "，并写入初始化内容" if seed_blocks else ""
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下创建空页：{title}{seed_note}。接下来将把本次 commit 写入该新页。",
        )

    if action in {"duplicate_page", "duplicate_page_without_content"}:
        source_page = _resolve_page_ref(page_state, _decision_source_page_ref(decision))
        if not source_page or not source_page.get("page_id"):
            raise RuntimeError("复制 page 动作缺少有效 source_page；已回退到当前页。")

        title = _decision_new_page_title(decision, str(source_page.get("title") or "模板页"))
        children: list[dict[str, Any]] = []
        if action == "duplicate_page":
            children = _clone_page_blocks_for_append(
                source_page["page_id"],
                token,
                max_blocks=_get_duplicate_max_blocks(context),
                max_depth=_get_duplicate_max_depth(context),
            )
        new_page_id = _create_child_page(target_parent["page_id"], title, token, children)
        new_state = _fetch_push_page_state(new_page_id, token)
        new_state["_created_by_structure_action"] = action
        mode = "with content" if action == "duplicate_page" else "without content"
        return (
            new_state,
            f"已在 {target_parent.get('title')} 下基于 {source_page.get('title')} 复制 page：{title}（{mode}）。接下来将把本次 commit 写入该新页。",
        )

    if action == "duplicate_block":
        source_block_ref = decision.get("source_block") or decision.get("source_block_ref")
        source_block = _resolve_block_ref(page_state, source_block_ref)
        if not source_block:
            raise RuntimeError("复制 block 动作缺少有效 source_block；已回退到当前页。")
        if str(source_block.get("block_type")) == "child_page":
            raise RuntimeError("source_block 是 page；请使用 duplicate_page / duplicate_page_without_content。")

        title = _decision_new_page_title(decision, str(source_block.get("title") or "Block Copy"))
        result = _append_duplicate_non_page_block(
            source_block,
            target_parent["page_id"],
            token,
            context,
            title=title,
        )
        if result.get("target_type") == "database":
            target_state = _fetch_push_page_state(target_parent["page_id"], token)
            target_state["_write_target_type"] = "database"
            target_state["_write_target_id"] = str(result.get("target_id") or "")
            target_state["_write_target_title"] = str(result.get("target_title") or title)
            target_state["page_title"] = str(result.get("target_title") or title)
            return (
                target_state,
                f"{result.get('note')} 接下来将把本次 commit 写入这个新 database，而不是写回父页。",
            )

        refreshed_parent_state = _fetch_push_page_state(target_parent["page_id"], token)
        refreshed_parent_state["_created_by_structure_action"] = action
        return (
            refreshed_parent_state,
            f"{result.get('note')} 目标父页：{target_parent.get('title')}。",
        )

    raise RuntimeError(f"未知页面结构动作: {action}")


def _select_push_target_page_with_llm(
    initial_page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> dict[str, Any]:
    max_steps = _get_push_max_explore_steps(context)
    current_page_id = initial_page_id
    visited_page_ids: set[str] = set()
    visited_titles: list[str] = []
    executed_actions: list[dict[str, Any]] = []

    if max_steps <= 0:
        page_state = _fetch_push_page_state(current_page_id, token)
        _render_ai_push_action(
            0,
            0,
            page_state,
            {
                "page_kind": "not_checked",
                "observation": "配置将 Notion push 探索步数设为 0。",
                "action": "write_current",
                "reason": "按配置直接写入当前页。",
                "confidence": 1,
            },
        )
        return page_state

    last_state: dict[str, Any] | None = None
    for step in range(1, max_steps + 1):
        page_state = _fetch_push_page_state(current_page_id, token)
        last_state = page_state
        visited_page_ids.add(current_page_id)
        title = str(page_state.get("page_title") or "未命名页面")
        if title not in visited_titles:
            visited_titles.append(title)

        decision = _decide_next_push_page(
            page_state,
            items,
            commit_id,
            context,
            step=step,
            max_steps=max_steps,
            visited_titles=visited_titles,
            executed_actions=executed_actions,
        )

        action = str(decision.get("action") or "write_current")

        if step >= max_steps and action not in {"write_current", "write_database"}:
            decision["action"] = "write_current"
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note="AI 想继续探索或执行页面结构动作，但已经达到循环上限；改为写入当前页。",
            )
            return page_state

        if action == "write_current":
            _render_ai_push_action(step, max_steps, page_state, decision)
            return page_state

        if action == "write_database":
            database_ref = (
                decision.get("source_block")
                or decision.get("source_block_ref")
                or decision.get("database_block")
                or decision.get("source_database")
                or "last_database"
            )
            source_database = _resolve_database_ref(page_state, database_ref)
            if not source_database:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="AI 选择的 database 引用不存在或不是 child_database；按 fallback 规则写入当前页。",
                )
                return page_state

            target_state = _database_target_state_from_block(page_state, source_database)
            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                note=(
                    f"已锁定已有 database：{source_database.get('title')} "
                    f"({_safe_short_page_id(str(source_database.get('block_id') or ''))})；"
                    "接下来将把本次 commit 写入该 database rows，而不是写回父页。"
                ),
            )
            return target_state

        if action == "explore_child":
            child_pages = page_state.get("child_pages") or []
            child_by_index = {str(child.get("child_index")): child for child in child_pages}
            chosen_child = child_by_index.get(str(decision.get("child_index")))
            if not chosen_child:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="AI 选择的 child_index 不存在；按 fallback 规则写入当前页。",
                )
                return page_state

            child_id = str(chosen_child.get("page_id") or "")
            child_title = str(chosen_child.get("title") or "未命名子页")
            if not child_id or child_id in visited_page_ids:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    selected_child_title=child_title,
                    note="AI 选择的子页为空或已经访问过；按 fallback 规则写入当前页。",
                )
                return page_state

            _render_ai_push_action(
                step,
                max_steps,
                page_state,
                decision,
                selected_child_title=child_title,
                note="已切换到该子页，下一轮继续检查。",
            )
            executed_actions.append({"step": step, "action": "explore_child", "target": child_title})
            current_page_id = child_id
            continue

        if action in _AI_PAGE_STRUCTURE_ACTIONS:
            if not page_state.get("extra_instructions"):
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note="当前页没有 ::| 额外指令，拒绝执行页面结构动作；按 fallback 写入当前页。",
                )
                return page_state

            try:
                new_state, result_note = _execute_page_structure_action(decision, page_state, token, context)
            except RuntimeError as exc:
                decision["action"] = "write_current"
                _render_ai_push_action(
                    step,
                    max_steps,
                    page_state,
                    decision,
                    note=f"页面结构动作失败：{exc} 按 fallback 写入当前页。",
                )
                return page_state

            _render_ai_push_action(step, max_steps, page_state, decision, note=result_note)
            executed_actions.append(
                {
                    "step": step,
                    "action": action,
                    "new_target_title": new_state.get("_write_target_title") or new_state.get("page_title"),
                    "instruction_refs": decision.get("instruction_refs") or decision.get("instruction_index"),
                }
            )

            # 核心修正：create/duplicate 是为了给本次 commit 准备新落点。
            # 一旦成功创建/复制出新 page 或 new database，就直接锁定为最终目标，
            # 不再进入下一轮让 fallback 有机会把目标改回父页。
            if action in {"create_empty_page", "create_child_page", "duplicate_page", "duplicate_page_without_content"}:
                return new_state
            if new_state.get("_write_target_type") == "database":
                return new_state

            current_page_id = str(new_state.get("page_id") or current_page_id)
            continue

        decision["action"] = "write_current"
        _render_ai_push_action(
            step,
            max_steps,
            page_state,
            decision,
            note="AI 返回未知动作；按 fallback 写入当前页。",
        )
        return page_state

    if last_state is None:
        last_state = _fetch_push_page_state(current_page_id, token)
    _render_info(
        "Notion push 探索结束",
        f"已达到 {max_steps} 步循环上限；按 fallback 规则写入当前页: {last_state.get('page_title') or '未命名页面'}。",
    )
    return last_state


def _push_items_to_page_with_ai(
    initial_page_id: str,
    items: list[dict[str, Any]],
    commit_id: str,
    token: str,
    context: dict[str, str],
) -> None:
    target_state = _select_push_target_page_with_llm(initial_page_id, items, commit_id, token, context)

    if target_state.get("_write_target_type") == "database":
        database_id = str(target_state.get("_write_target_id") or "")
        if not database_id:
            raise RuntimeError("结构动作返回了 database 写入目标，但缺少 database_id；已取消 push，避免写回父页。")
        _push_items_to_database_with_ai(database_id, items, commit_id, token, context)
        return

    target_page_id = str(target_state.get("page_id") or initial_page_id)
    blocks = _blocks_from_llm_for_page_format(target_page_id, items, commit_id, token, context)
    _render_push_target_selected(target_state, blocks_count=len(blocks))
    _append_blocks_to_page(target_page_id, blocks, token)
