from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import os
import sys

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
except ImportError:  # pragma: no cover
    console = None
    RICH_AVAILABLE = False

from summary import (
    DEFAULT_OPENAI_MODEL,
    _build_commit_preview_text,
    _extract_commit_payload,
    _openai_json,
    _render_error,
    read_config,
)


CHINESE_EDITABLE_FIELDS: tuple[str, ...] = (
    "任务名",
    "周几",
    "持续小时",
    "开始时间",
    "结束时间",
    "类别",
)


CHAT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "log_chat_modifications",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "modified_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "编号": {"type": "integer"},
                            "任务名": {"type": "string"},
                            "周几": {"type": "string"},
                            "持续小时": {"type": "number"},
                            "开始时间": {"type": "string"},
                            "结束时间": {"type": "string"},
                            "类别": {"type": "string"},
                        },
                        "required": [
                            "编号",
                            "任务名",
                            "周几",
                            "持续小时",
                            "开始时间",
                            "结束时间",
                            "类别",
                        ],
                        "additionalProperties": False,
                    },
                },
                "reason": {
                    "type": "string",
                    "description": "一句中文说明本次改动理由，用于调试与用户查看。",
                },
            },
            "required": ["modified_tasks", "reason"],
            "additionalProperties": False,
        },
    },
}


SYSTEM_PROMPT = (
    "你是任务日志修改助手。你会收到用户对当前 commit_preview 的中文修改指令，"
    "以及当前 commit_preview 中的所有任务列表。你的任务是根据用户指令，输出需要修改的任务的完整字段。\n\n"
    "严格规则：\n"
    "1. **绝对不允许修改「编号」字段**。编号是任务的唯一标识，你返回的编号必须是原任务列表中已经存在的编号。"
    "严禁自行发明新的编号，也严禁把某个编号改成别的编号。任何试图变更编号的行为都会被本地过滤掉。\n"
    "2. 只返回需要修改的任务，未修改的任务不要放进 modified_tasks。\n"
    "3. 每条 modified_tasks 必须完整返回 7 个字段（未变的字段填原值）。\n"
    "4. 如果改了 开始时间 / 结束时间 / 持续小时 中的任何一个，必须保证 结束时间 - 开始时间 == 持续小时。\n"
    "5. 开始时间和结束时间必须保持 ISO 8601 带时区格式，例如 2026-07-14T09:00:00-04:00。\n"
    "6. 类别建议优先从任务列表中已存在的类别中选择，但不强制。\n"
    "7. reason 是整批修改的一句总结，不是 per-task。\n"
)


def _resolve_model(context: dict[str, str]) -> str:
    config = read_config(context.get("config_file"))
    return os.getenv("OPENAI_MODEL") or config.get("openai_model") or DEFAULT_OPENAI_MODEL


def _original_field_value(item: dict[str, Any], field: str) -> Any:
    if field == "开始时间":
        return item.get("开始时间") or item.get("start_time")
    if field == "结束时间":
        return item.get("结束时间") or item.get("end_time")
    return item.get(field)


def _values_differ(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return abs(float(a) - float(b)) > 1e-9
        except (TypeError, ValueError):
            return a != b
    return str(a if a is not None else "") != str(b if b is not None else "")


def _diff_fields(original: dict[str, Any], modified: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    diffs: list[tuple[str, Any, Any]] = []
    for field in CHINESE_EDITABLE_FIELDS:
        old = _original_field_value(original, field)
        new = modified.get(field, old)
        if _values_differ(old, new):
            diffs.append((field, old, new))
    return diffs


def _format_items_for_prompt(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        parts = [
            f"编号: {item.get('编号', '')}",
            f"任务名: {item.get('任务名', '')}",
            f"周几: {item.get('周几', '')}",
            f"持续小时: {item.get('持续小时', '')}",
            f"开始时间: {_original_field_value(item, '开始时间') or ''}",
            f"结束时间: {_original_field_value(item, '结束时间') or ''}",
            f"类别: {item.get('类别', '')}",
        ]
        lines.append(f"{index}. " + "；".join(parts))
    return "\n".join(lines)


def _render_chat_request_panel(model: str, instruction: str, count: int) -> None:
    if not RICH_AVAILABLE:
        print(f"[log chat 请求] 模型: {model}；指令: {instruction}；任务数: {count}")
        return

    body = Group(
        Text(f"模型: {model}", style="bold cyan"),
        Text(f"用户指令: {instruction}", style="white"),
        Text(f"任务数: {count}", style="dim"),
    )
    console.print(
        Panel(
            body,
            title=Text("log chat 请求", style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_warning_line(text: str) -> None:
    if not RICH_AVAILABLE:
        print(text)
        return
    console.print(Text(text, style="yellow"))


def _render_info_line(text: str) -> None:
    if not RICH_AVAILABLE:
        print(text)
        return
    console.print(Text(text, style="cyan"))


def _check_consistency(item: dict[str, Any]) -> str | None:
    start_str = _original_field_value(item, "开始时间")
    end_str = _original_field_value(item, "结束时间")
    hours = item.get("持续小时")
    item_no = item.get("编号")
    task_name = item.get("任务名") or ""

    try:
        start = datetime.fromisoformat(str(start_str))
        end = datetime.fromisoformat(str(end_str))
    except (ValueError, TypeError):
        return (
            f"⚠ 编号 {item_no} {task_name} 时间格式不合法："
            f"开始时间={start_str}，结束时间={end_str}，声明持续小时={hours}"
        )

    try:
        expected = round((end - start).total_seconds() / 3600, 2)
        stated = round(float(hours), 2)
    except (TypeError, ValueError):
        return (
            f"⚠ 编号 {item_no} {task_name} 持续小时非法："
            f"开始时间={start_str}，结束时间={end_str}，声明持续小时={hours}"
        )

    if abs(expected - stated) > 0.01:
        return (
            f"⚠ 编号 {item_no} {task_name} 一致性违反："
            f"结束-开始={expected}h，声明持续小时={stated}h"
        )
    return None


def _render_diff_panel(
    *,
    index: int,
    total: int,
    item_no: Any,
    task_name: str,
    diffs: list[tuple[str, Any, Any]],
    warning: str | None,
) -> None:
    title = f"变更 {index}/{total}  编号 {item_no}  {task_name}"

    if not RICH_AVAILABLE:
        print(f"=== {title} ===")
        if warning:
            print(warning)
        for field, old, new in diffs:
            print(f"  {field}: {old!s}  ->  {new!s}")
        return

    body_children: list[Any] = []
    if warning:
        body_children.append(Text(warning, style="bold red"))

    table = Table(box=box.SIMPLE, show_lines=False, expand=False, pad_edge=False)
    table.add_column("字段", style="bold cyan", no_wrap=False, overflow="fold")
    table.add_column("变更前", overflow="fold", no_wrap=False)
    table.add_column("变更后", overflow="fold", no_wrap=False)
    for field, old, new in diffs:
        table.add_row(field, "" if old is None else str(old), "" if new is None else str(new))
    body_children.append(table)

    console.print(
        Panel(
            Group(*body_children),
            title=Text(title, style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _prompt_yes_no() -> bool:
    try:
        raw = input("[y/n] ").strip().lower()
    except EOFError:
        return False
    return raw == "y"


def _merge_item(original: dict[str, Any], modified: dict[str, Any]) -> dict[str, Any]:
    merged = dict(original)
    for field in CHINESE_EDITABLE_FIELDS:
        if field in modified:
            merged[field] = modified[field]
    # 编号不可变，强制沿用原编号
    merged["编号"] = original.get("编号")
    # 同步英文时间字段，避免 preview 生成时与中文字段不一致
    if "开始时间" in modified:
        merged["start_time"] = modified["开始时间"]
    if "结束时间" in modified:
        merged["end_time"] = modified["结束时间"]
    return merged


def _render_final_status(
    *,
    accepted: int,
    rejected: int,
    unchanged: int,
    generated_at: str | None,
    saved: bool,
) -> None:
    if not saved:
        message = (
            f"本次未接受任何修改，commit_preview 未变更。"
            f"接受 {accepted}，拒绝 {rejected}，未变 {unchanged}。"
        )
        if not RICH_AVAILABLE:
            print(message)
            return
        console.print(
            Panel(
                Text(message, style="yellow"),
                title=Text("commit_preview 未更新", style="bold yellow"),
                border_style="yellow",
                box=box.ROUNDED,
                expand=False,
            )
        )
        return

    detail = (
        f"接受: {accepted}\n"
        f"拒绝: {rejected}\n"
        f"未变: {unchanged}\n"
        f"generated_at: {generated_at or ''}"
    )
    if not RICH_AVAILABLE:
        print("commit_preview 已更新")
        print(detail)
        return

    console.print(
        Panel(
            Text(detail, style="green"),
            title=Text("commit_preview 已更新", style="bold green"),
            border_style="green",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _render_overlap_check(items: list[dict[str, Any]]) -> None:
    parsed: list[tuple[datetime, datetime, dict[str, Any]] | None] = []
    for item in items:
        s = _original_field_value(item, "开始时间")
        e = _original_field_value(item, "结束时间")
        try:
            start_dt = datetime.fromisoformat(str(s))
            end_dt = datetime.fromisoformat(str(e))
        except (ValueError, TypeError):
            parsed.append(None)
            continue
        parsed.append((start_dt, end_dt, item))

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            left = parsed[i]
            right = parsed[j]
            if left is None or right is None:
                continue
            sa, ea, ia = left
            sb, eb, ib = right
            # 闭区间：只要有交集就算重叠
            if sa <= eb and sb <= ea:
                pairs.append((ia, ib))

    if not pairs:
        if RICH_AVAILABLE:
            console.print(Text("未检测到时间重叠", style="dim"))
        else:
            print("未检测到时间重叠")
        return

    lines: list[str] = []
    for a, b in pairs:
        a_start = _original_field_value(a, "开始时间")
        a_end = _original_field_value(a, "结束时间")
        b_start = _original_field_value(b, "开始时间")
        b_end = _original_field_value(b, "结束时间")
        lines.append(
            f"编号 {a.get('编号')} ({a.get('任务名')}) [{a_start} ~ {a_end}]\n"
            f"  与 编号 {b.get('编号')} ({b.get('任务名')}) [{b_start} ~ {b_end}] 重叠"
        )
    body_text = "\n\n".join(lines)
    if not RICH_AVAILABLE:
        print("⚠ 时间重叠")
        print(body_text)
        return
    console.print(
        Panel(
            Text(body_text, style="yellow"),
            title=Text("⚠ 时间重叠", style="bold yellow"),
            border_style="yellow",
            box=box.ROUNDED,
            expand=False,
        )
    )


def _coerce_index(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def chat_task(instruction: str, context: dict[str, str]) -> int:
    """
    log chat <用户中文指令>

    参考 log_chat_requirements.md 的完整流程。
    """
    # Step 1: 前置校验
    if not instruction or not instruction.strip():
        _render_error("log chat 参数错误", "缺少中文指令。用法: log chat <你的中文指令>")
        return 2

    preview_path = Path(context.get("commit_preview_file") or "")
    if not preview_path.exists():
        _render_error(
            "commit 预览不存在",
            f"未找到 {preview_path}。请先运行 log commit 生成预览。",
        )
        return 1

    preview_text = preview_path.read_text(encoding="utf-8")
    if not preview_text.strip():
        _render_error("commit 预览为空", "请先运行 log commit 生成预览。")
        return 1

    commit_payload = _extract_commit_payload(preview_text)
    if not commit_payload:
        _render_error(
            "commit 预览缺少 JSON payload",
            "无法从 commit_preview.txt 中解析机器可读 payload。请重新运行 log commit。",
        )
        return 1

    items = commit_payload.get("items")
    if not isinstance(items, list) or not items:
        _render_error("commit 预览无任务", "当前预览中没有任务，log chat 无需执行。")
        return 1

    # Step 2: 自然语言列表
    natural_list = _format_items_for_prompt(items)

    # Step 3: 调 LLM
    model = _resolve_model(context)
    _render_chat_request_panel(model, instruction, len(items))

    user_prompt = (
        f"用户指令：{instruction}\n\n"
        f"当前 commit preview（共 {len(items)} 条）：\n{natural_list}\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    result = _openai_json(messages, context, response_format=CHAT_RESPONSE_FORMAT)
    if result is None:
        return 1

    modified_list = result.get("modified_tasks")
    reason = result.get("reason")
    if isinstance(reason, str) and reason.strip():
        _render_info_line(f"LLM 修改理由：{reason.strip()}")

    if not isinstance(modified_list, list) or not modified_list:
        _render_info_line("LLM 未提出任何修改")
        return 0

    # Step 4: 本地过滤
    original_by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        key = _coerce_index(item.get("编号"))
        if key is not None:
            original_by_index[key] = item

    filtered: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in modified_list:
        if not isinstance(candidate, dict):
            continue

        idx_int = _coerce_index(candidate.get("编号"))
        if idx_int is None or idx_int not in original_by_index:
            _render_warning_line(f"⚠ LLM 返回的编号 {candidate.get('编号')} 不在预览内，已忽略")
            continue

        original = original_by_index[idx_int]

        # 保险比对：即便 structured output 要求返回编号，也确认编号未被变更
        original_idx = _coerce_index(original.get("编号"))
        if original_idx is None or original_idx != idx_int:
            _render_warning_line(
                f"⚠ LLM 试图修改编号 {original.get('编号')}→{candidate.get('编号')}，已忽略该条"
            )
            continue

        diffs = _diff_fields(original, candidate)
        if not diffs:
            _render_warning_line(f"编号 {idx_int} LLM 未提出实际改动，跳过")
            continue

        filtered.append((original, candidate))

    if not filtered:
        _render_info_line("LLM 未提出任何修改")
        return 0

    # Step 5: 一致性违反检测（针对最终提议）
    consistency_warnings: dict[int, str | None] = {}
    for original, modified in filtered:
        merged_preview = _merge_item(original, modified)
        consistency_warnings[int(original.get("编号"))] = _check_consistency(merged_preview)

    # Step 6: 逐条 y/n 审核
    accepted_map: dict[int, dict[str, Any]] = {}
    accepted_count = 0
    rejected_count = 0
    total = len(filtered)

    for index, (original, modified) in enumerate(filtered, start=1):
        item_no = original.get("编号")
        diffs = _diff_fields(original, modified)
        task_name = str(modified.get("任务名") or original.get("任务名") or "")
        warning = consistency_warnings.get(int(item_no))
        _render_diff_panel(
            index=index,
            total=total,
            item_no=item_no,
            task_name=task_name,
            diffs=diffs,
            warning=warning,
        )
        if _prompt_yes_no():
            accepted_map[int(item_no)] = _merge_item(original, modified)
            accepted_count += 1
        else:
            rejected_count += 1

    # Step 7: 构建最终 items 并落盘
    if accepted_count == 0:
        _render_final_status(
            accepted=accepted_count,
            rejected=rejected_count,
            unchanged=len(items) - accepted_count - rejected_count,
            generated_at=commit_payload.get("generated_at"),
            saved=False,
        )
        final_items = items
    else:
        final_items = []
        for item in items:
            key = _coerce_index(item.get("编号"))
            if key is not None and key in accepted_map:
                final_items.append(accepted_map[key])
            else:
                final_items.append(item)

        commit_payload["items"] = final_items
        commit_payload["generated_at"] = datetime.now().isoformat(timespec="seconds")

        preview_new_text = _build_commit_preview_text(final_items, commit_payload)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(preview_new_text, encoding="utf-8")

        _render_final_status(
            accepted=accepted_count,
            rejected=rejected_count,
            unchanged=len(items) - accepted_count - rejected_count,
            generated_at=commit_payload.get("generated_at"),
            saved=True,
        )

    # Step 8: 兜底重叠检查
    _render_overlap_check(final_items)
    return 0
