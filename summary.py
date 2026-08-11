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
DEFAULT_TASK_INDEX_START = 10000
TASK_INDEX_STEP = 5


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


def _openai_json(
    messages: list[dict[str, str]],
    context: dict[str, str],
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
        "response_format": response_format or {"type": "json_object"},
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


def _read_next_task_index(context: dict[str, str]) -> int:
    path = _task_index_file(context)
    if path.exists():
        try:
            index = int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            index = DEFAULT_TASK_INDEX_START
        return max(index, DEFAULT_TASK_INDEX_START)
    return DEFAULT_TASK_INDEX_START


def _write_next_task_index(context: dict[str, str], index: int) -> None:
    path = _task_index_file(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(index), encoding="utf-8")


def allocate_task_index(context: dict[str, str]) -> int:
    index = _read_next_task_index(context)
    _write_next_task_index(context, index + TASK_INDEX_STEP)
    return index


def reset_task_index(context: dict[str, str]) -> int:
    """
    把下一个任务排序编号重置为 10000。

    守护：committed 池非空时拒绝重置。池内 items 的编号与新任务同出一个单调计数器，
    倒回计数器会让后续 commit 领到与池内重复的编号，破坏 check / chat / edit 依赖的编号唯一性。
    需先 log push 清空池，才能安全重置。
    """
    pool_items = _load_existing_pool_items(context)
    if pool_items:
        _render_error(
            "编号无法重置",
            f"committed 池中还有 {len(pool_items)} 条任务；重置计数器会使新任务编号与池内重复。\n"
            "请先执行 log push 清空池，再运行 log indexreset。",
        )
        return 1

    _write_next_task_index(context, DEFAULT_TASK_INDEX_START)
    _render_info("编号已重置", f"下一个新任务编号将从 {DEFAULT_TASK_INDEX_START} 开始。")
    return 0


def _extract_item_identity(item: dict[str, Any]) -> str:
    return str(item.get("source_key") or f"{item.get('task_group_id', '')}:{item.get('周几', '')}")


def _load_existing_pool_items(context: dict[str, str]) -> list[dict[str, Any]]:
    """
    读取已有 committed 池（commit_preview.txt）的 items。

    生产-消费模型下，commit 是把本次校准好的新任务“追加”进这个池子，
    因此每次 commit 前先把池内已有 items 原样取回，绝不重算、也绝不刷新它们的编号。
    池为空 / 不存在 / 无法解析时，视为空池返回空列表。
    """
    preview_path = Path(context.get("commit_preview_file") or "")
    if not preview_path.exists():
        return []

    text = preview_path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    payload = _extract_commit_payload(text)
    if not payload or not isinstance(payload.get("items"), list):
        return []

    return [item for item in payload["items"] if isinstance(item, dict)]


def _move_task_index_to_end(item: dict[str, Any]) -> None:
    index = _coerce_task_index(item.pop("编号", None) or item.pop("task_index", None))
    if index is not None:
        item["编号"] = index


def _assign_new_item_indexes(
    new_items: list[dict[str, Any]],
    reserved_indexes: set[int],
    context: dict[str, str],
) -> None:
    """
    只给“本次新增” items 分配编号，保证与池内已有编号（reserved_indexes）以及彼此之间都不冲突。

    编号是累积 committed 池的行标识——check / chat / edit 都按它定位任务，必须全局唯一。
    - 新 item 若自带有效且未被占用的编号（log start 时领的 task_index），沿用之，保持编号稳定；
    - 否则从单调计数器 task_index.txt 领新号，并跳过任何已被占用的号。
    """
    used = set(reserved_indexes)
    next_index = _read_next_task_index(context)
    changed_counter = False

    for item in new_items:
        current = _coerce_task_index(item.get("编号") or item.get("task_index"))
        if current is None or current in used:
            while next_index in used:
                next_index += TASK_INDEX_STEP
            current = next_index
            next_index += TASK_INDEX_STEP
            changed_counter = True
        used.add(current)
        item["编号"] = current
        _move_task_index_to_end(item)

    if changed_counter:
        _write_next_task_index(context, next_index)


def _drain_uncommitted_sessions(context: dict[str, str], commit_id: str) -> int:
    """
    排干未committed池：把 uncommit_tasks.txt 里所有 committed:False 的 session 标为 committed:True。

    不物理删除，保留原始历史，与 start / end / cont 的 `committed is False` 过滤保持一致，
    被标记后即从“未committed”视图消失，等价于把任务移交到 committed 池。

    仅在 committed 池成功写盘后调用（事务顺序：先落池，再排干）。调用前已用
    _find_unfinished_sessions 确认没有未结束 session，因此排干的都是已消费进池的已结束任务。
    """
    records = read_txt_records(context["uncommit_file"])
    committed_at = datetime.now().isoformat(timespec="seconds")
    changed = 0
    for record in records:
        if record.get("type") == "session" and record.get("committed") is False:
            record["committed"] = True
            record["committed_at"] = committed_at
            record["commit_id"] = commit_id
            record["updated_at"] = committed_at
            changed += 1

    if changed:
        write_txt_records(context["uncommit_file"], records)
    return changed

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
                "task_index": _coerce_task_index(record.get("task_index") or record.get("编号")),
                "task_group_id": group_id,
                "source_session_ids": [],
                "session_names": [],
                # session_id -> 详细描述 文本；多个 session 的描述合并为映射，不拼接。
                "详细描述": {},
                "start_time": record.get("start_time"),
                "end_time": record.get("end_time"),
                "开始时间": record.get("start_time"),
                "结束时间": record.get("end_time"),
            },
        )

        record_index = _coerce_task_index(record.get("task_index") or record.get("编号"))
        if record_index is not None and bucket.get("task_index") is None:
            bucket["task_index"] = record_index

        bucket["持续小时"] = round(float(bucket["持续小时"]) + duration_hours, 2)
        if record.get("session_id"):
            bucket["source_session_ids"].append(record["session_id"])
        name = str(record.get("task_name") or "").strip()
        if name and name not in bucket["session_names"]:
            bucket["session_names"].append(name)
        description = str(record.get("详细描述") or record.get("detailed_description") or "").strip()
        if description and record.get("session_id"):
            bucket["详细描述"][str(record["session_id"])] = description
        if record.get("start_time") and (not bucket.get("start_time") or record["start_time"] < bucket["start_time"]):
            bucket["start_time"] = record["start_time"]
            bucket["开始时间"] = record["start_time"]
        if record.get("end_time") and (not bucket.get("end_time") or record["end_time"] > bucket["end_time"]):
            bucket["end_time"] = record["end_time"]
            bucket["结束时间"] = record["end_time"]

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

    # 白名单字段：详细描述 等隐私/长文本元数据不发送给 LLM，只在本地随 item 透传。
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
        "| 任务名 | 周几 | 开始时间 | 结束时间 | 持续小时 | 类别 | 编号 |",
        "|---|---:|---|---|---:|---|---:|",
    ]

    for item in items:
        preview_lines.append(
            f"| {item.get('任务名', '')} | {item.get('周几', '')} | {item.get('开始时间') or item.get('start_time') or ''} | {item.get('结束时间') or item.get('end_time') or ''} | {_format_hours(float(item.get('持续小时') or 0))} | {item.get('类别', '')} | {item.get('编号', '')} |"
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
                f"   - 开始时间: {item.get('开始时间') or item.get('start_time') or ''}",
                f"   - 结束时间: {item.get('结束时间') or item.get('end_time') or ''}",
                f"   - 持续小时: {_format_hours(float(item.get('持续小时') or 0))}",
                f"   - 类别: {item.get('类别', '')}",
                f"   - 编号: {item.get('编号', '')}",
                f"   - 任务组 ID: {item.get('task_group_id', '')}",
                f"   - Session IDs: {', '.join(map(str, item.get('source_session_ids') or []))}",
            ]
        )
        descriptions = item.get("详细描述")
        if isinstance(descriptions, dict) and descriptions:
            preview_lines.append("   - 详细描述:")
            for session_id, text in descriptions.items():
                preview_lines.append(f"     - {session_id}: {text}")
        preview_lines.append("")

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


def _render_commit_preview(
    items: list[dict[str, Any]],
    preview_file: str,
    category_text: str,
    new_count: int | None = None,
) -> None:
    if not RICH_AVAILABLE:
        if new_count is not None:
            print(f"committed 池已更新: 本次新增 {new_count} 条，池内共 {len(items)} 条 -> {preview_file}")
        else:
            print(f"committed 池已写入: {preview_file}")
        for item in items:
            print(f"{item.get('任务名')} | {item.get('周几')} | {_format_hours(float(item.get('持续小时') or 0))} | {item.get('类别')} | {item.get('编号')}")
        return

    table = Table(
        "#",
        "任务名",
        "周几",
        "持续小时",
        "类别",
        "任务组 ID",
        "编号",
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
            str(item.get("编号") or ""),
        )

    footer = Table.grid(padding=(0, 2))
    footer.add_column(style="bold cyan", no_wrap=True)
    footer.add_column(style="white")
    if new_count is not None:
        footer.add_row("本次新增", str(new_count))
    footer.add_row("池内任务数", str(len(items)))
    footer.add_row("池文件", preview_file)
    footer.add_row("任务分类来源", "Notion" if category_text else "未读取到 Notion 分类，已用未分类/本地兜底")
    footer.add_row("周几规则", "07:00 及以前算前一天；07:01 起算当天")

    body = Group(table, "", footer)
    console.print(
        Panel(
            body,
            title=Text("committed 任务池（累积，push 后清空）", style="bold green"),
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

    生产-消费模型（累积追加，不再全量覆盖）:
    - 读取 uncommit_tasks.txt 中未 commit 且已结束的任务
    - 从 Notion 读取“任务分类”说明，用 LLM 对这批**新增**任务做初步校准
    - 取回已有 committed 池（commit_preview.txt）的 items，把新增 items 追加进去
      （池内旧任务原样保留、编号不变；新增只领新号，保证编号全局唯一）
    - 整份池写回 commit_preview.txt
    - 写盘成功后排干未committed池：把这批 session 标 committed:True
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

    new_items = classify_tasks_with_llm(uncommitted, category_text, context)
    if not new_items:
        _render_empty_state("当前没有可 commit 的已结束任务。")
        return 0

    # 生产-消费模型：先取回已有 committed 池，本次新增只做“追加”，绝不刷新池内旧任务。
    existing_items = _load_existing_pool_items(context)
    existing_keys = {_extract_item_identity(item) for item in existing_items}
    # 防御性去重：正常流程下已排干未committed池，新增不会与池内重复；此处兜底防止重复追加。
    new_items = [item for item in new_items if _extract_item_identity(item) not in existing_keys]

    # 池内已有编号原样保留；只给新增 item 领新号，且保证与池内、彼此都不冲突。
    reserved = {
        idx
        for idx in (_coerce_task_index(item.get("编号")) for item in existing_items)
        if idx is not None
    }
    _assign_new_item_indexes(new_items, reserved, context)

    pool_items = existing_items + new_items

    commit_id = str(uuid.uuid4())
    commit_payload = {
        "commit_id": commit_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": pool_items,
    }
    preview = _build_commit_preview_text(pool_items, commit_payload)
    write_text(context["commit_preview_file"], preview)

    # 事务顺序：committed 池成功落盘后，才排干未committed池（标 committed:True）。
    _drain_uncommitted_sessions(context, commit_id)

    _render_commit_preview(
        pool_items,
        context["commit_preview_file"],
        category_text,
        new_count=len(new_items),
    )
    return 0


# ---------------------------------------------------------------------------
# PushAgent-based push implementation
# ---------------------------------------------------------------------------


def _normalize_notion_id(value: str | None) -> str:
    """支持在 config/env 中填写 Notion 原始 ID 或页面 URL。"""
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


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def build_push_config(context: dict[str, str]) -> dict[str, str]:
    """
    整理 PushAgent.PushTasks 所需配置。

    这个函数把原 push_agent_main.py 的独立入口逻辑内置到 log push，
    因此 log push 不再调用 summary.py 里的旧 Notion push 代码。
    """
    config_file = context.get("config_file") or os.getenv("CONFIG_FILE") or ""
    config = read_config(config_file)

    merged: dict[str, str] = {**config, **{k: v for k, v in context.items() if isinstance(v, str)}}
    if config_file:
        merged["config_file"] = config_file

    data_dir = Path(
        _first_non_empty(
            os.getenv("LOG_DATA_DIR"),
            merged.get("data_dir"),
            str(Path(merged.get("commit_preview_file") or ".").expanduser().parent),
        )
    ).expanduser()

    merged["commit_preview_file"] = _first_non_empty(
        os.getenv("COMMIT_PREVIEW_FILE"),
        os.getenv("COMMIT_PREVIEW_PATH"),
        merged.get("commit_preview_file"),
        str(data_dir / "commit_preview.txt"),
    )

    merged["openai_api_key"] = _first_non_empty(
        os.getenv("OPENAI_API_KEY"),
        merged.get("openai_api_key"),
    )

    merged["notion_token"] = _first_non_empty(
        os.getenv("NOTION_TOKEN"),
        os.getenv("NOTION_API_KEY"),
        merged.get("notion_token"),
    )

    merged["notion_commit_page_id"] = _resolve_config_value(
        os.getenv("NOTION_COMMIT_PAGE_ID"),
        os.getenv("NOTION_LOG_PAGE_ID"),
        os.getenv("NOTION_PUSH_PAGE_ID"),
        merged.get("notion_commit_page_id"),
        merged.get("notion_log_page_id"),
        merged.get("notion_push_page_id"),
    )

    return merged


def validate_push_config(config: dict[str, str]) -> None:
    """与原 push_agent_main.py 一致，校验 PushAgent.PushTasks 的必要配置。"""
    missing = [
        key
        for key in ("openai_api_key", "notion_commit_page_id")
        if not config.get(key)
    ]
    if missing:
        raise RuntimeError(
            "缺少必要配置: "
            + ", ".join(missing)
            + "。已读取配置文件: "
            + str(config.get("config_file") or "未找到")
            + "。请检查该文件、环境变量或字段名。"
        )


def _run_push_agent(commit_preview: str, config: dict[str, str]) -> None:
    try:
        from PushAgent.Agent import Agent
    except ImportError as exc:
        raise RuntimeError(
            "无法导入 PushAgent.Agent.Agent。请确认 PushAgent 包在当前 Python 环境的 import path 中。"
        ) from exc

    agent = Agent(configFile=config.get("config_file"), overrides=config)
    agent.PushTasks(commit_preview, config)


def _backup_and_clear_pool(context: dict[str, str], preview_text: str) -> Path:
    """
    push 的消费动作：把整份 committed 池按时间戳备份到本地，再清空池。

    - 备份仅为“信息上保证可还原”：文件内容 = 完整池预览文本（含机器可读 payload），
      未来若要还原，把它整体追加回池、编号冲突自动领新号即可（本工具不内置还原命令）。
    - 备份成功写盘后才清空 commit_preview.txt，避免中途失败丢数据。
    """
    data_dir = Path(context.get("data_dir") or Path(context["commit_preview_file"]).parent)
    backup_dir = data_dir / "commits"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"push_{stamp}.txt"
    # 极端情况下同一秒内多次 push，加 uuid 短后缀避免覆盖。
    if backup_file.exists():
        backup_file = backup_dir / f"push_{stamp}_{uuid.uuid4().hex[:8]}.txt"
    backup_file.write_text(preview_text, encoding="utf-8")

    Path(context["commit_preview_file"]).write_text("", encoding="utf-8")
    return backup_file


def push_tasks(context: dict[str, str]) -> int:
    """
    处理:
      log push

    生产-消费模型的消费端：读取整份 committed 池 → 调用 PushAgent.Agent.PushTasks 同步 Notion →
    把整份池按时间戳备份到本地 → 清空 committed 池。

    注意：未committed → committed 的排干已在 log commit 完成，push 不再触碰 uncommit_tasks.txt。
    """
    preview_path = Path(context["commit_preview_file"])

    if not preview_path.exists() or not preview_path.read_text(encoding="utf-8").strip():
        _render_error("没有可 push 的 committed 池", "请先执行 log commit。")
        return 1

    preview_text = preview_path.read_text(encoding="utf-8")
    commit_payload = _extract_commit_payload(preview_text)
    if not commit_payload:
        _render_error("committed 池格式不完整", "请重新执行 log commit 生成带机器可读 payload 的池。")
        return 1

    items = commit_payload.get("items") or []
    if not isinstance(items, list) or not items:
        _render_error("committed 池为空", "没有可 push 的任务。")
        return 1

    try:
        push_config = build_push_config(context)
        validate_push_config(push_config)
        _run_push_agent(preview_text, push_config)
    except Exception as exc:
        _render_error("PushAgent 同步失败", str(exc))
        return 1

    backup_file = _backup_and_clear_pool(context, preview_text)
    _render_info(
        "push 完成",
        f"已通过 PushAgent 同步 {len(items)} 条任务到 Notion；"
        f"整份池已备份到 {backup_file} 并清空。",
    )
    return 0

