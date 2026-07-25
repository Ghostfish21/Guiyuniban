from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any

from PushAgent.OpenAiServices import ChatCompletionRequest


_TARGET_PAGE = "page"
_TARGET_DATABASE = "database"
_DEFAULT_DATABASE_CONTENT_PROPERTY = "__page_content"


@dataclass
class DatabasePropertyInstruction:
    name: str
    valueType: str
    stringValue: str | None = None
    numberValue: float | int | None = None
    booleanValue: bool | None = None
    arrayValue: list[str] = field(default_factory=list)
    dateStart: str | None = None
    dateEnd: str | None = None


@dataclass
class DatabaseRowInstruction:
    properties: list[DatabasePropertyInstruction] = field(default_factory=list)
    pageContent: str | None = None


@dataclass
class TaskWriteInstruction:
    targetType: str
    pageContent: str | None = None
    pageHeading: str | None = None
    pageAppendDivider: bool = False
    pagePositionType: str | None = "end"
    pageAfterBlockId: str | None = None
    databaseTitleProperty: str | None = None
    databaseContentProperty: str | None = None
    databaseRows: list[DatabaseRowInstruction] = field(default_factory=list)
    reason: str = ""

    @property
    def shouldWritePage(self) -> bool:
        return self.targetType == _TARGET_PAGE

    @property
    def shouldWriteDatabase(self) -> bool:
        return self.targetType == _TARGET_DATABASE

    def ToDict(self) -> dict[str, Any]:
        return asdict(self)

    def ToDatabaseRows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        contentProperty = (self.databaseContentProperty or "").strip()

        for rowInstruction in self.databaseRows:
            row: dict[str, Any] = {}
            for propertyInstruction in rowInstruction.properties:
                name = propertyInstruction.name.strip()
                if not name:
                    continue
                row[name] = _DatabasePropertyValueToSimpleValue(propertyInstruction)

            pageContent = (rowInstruction.pageContent or "").strip()
            if contentProperty and pageContent:
                row[contentProperty] = pageContent

            if row:
                rows.append(row)

        return rows


SystemPrompt: str = (
    "你是一个 Notion 任务最终写入参数生成器。你会收到：目标写入页/数据库的完整内容、目标类型、"
    "已经形成的经验记忆、用户本次要写入的 commitPreview。前序 Agent 已经完成寻址；你的任务不是继续导航，"
    "也不是创建或复制 Notion 对象，而是生成宿主程序调用 WritePageService 或 WriteDatabaseService 所需的 JSON 参数。"
    "你必须严格匹配目标类型：目标类型是 page 时只能生成 page 写入参数；目标类型是 database 时只能生成 database 写入参数。"
    "你必须保留 commitPreview 中的所有待写入任务信息，不要遗漏、不要发明未出现的任务。"
    "如果 commitPreview 中包含机器可读 JSON payload，应优先以 payload 的 items 为准；若没有 payload，再根据可读文本提取。"
    "必须尽量贴合目标页/数据库的现有结构、字段名、语言和格式。"
    "对于 database，只能使用目标数据库 Properties 中存在或根据经验记忆明确应使用的字段名；标题字段必须放入 databaseTitleProperty。"
    "对于 page，生成可直接转换为 Notion blocks 的 Markdown 内容，避免包含调试说明。"
    "必须只返回 JSON。"
)


JsonSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "notion_task_final_write_instruction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "targetType": {
                    "type": "string",
                    "enum": [_TARGET_PAGE, _TARGET_DATABASE],
                    "description": "宿主程序应调用的写入目标类型。必须与输入目标类型一致。",
                },
                "pageContent": {
                    "type": ["string", "null"],
                    "description": "写入 page 的 Markdown 内容；targetType=database 时为 null。",
                },
                "pageHeading": {
                    "type": ["string", "null"],
                    "description": "写入 page 时附加的二级标题；不需要则为 null；targetType=database 时为 null。",
                },
                "pageAppendDivider": {
                    "type": "boolean",
                    "description": "写入 page 前是否追加 divider；targetType=database 时必须为 false。",
                },
                "pagePositionType": {
                    "type": ["string", "null"],
                    "enum": ["end", "start", "after_block", None],
                    "description": "写入 page 的位置；默认 end。targetType=database 时为 null。",
                },
                "pageAfterBlockId": {
                    "type": ["string", "null"],
                    "description": "pagePositionType=after_block 时使用的已有 block id；必须来自目标页内容。其他情况为 null。",
                },
                "databaseTitleProperty": {
                    "type": ["string", "null"],
                    "description": "写入 database 时使用的标题属性名，例如 任务名/Name；targetType=page 时为 null。",
                },
                "databaseContentProperty": {
                    "type": ["string", "null"],
                    "description": "如果需要把每条任务的正文写入 database row page content，填写 __page_content；否则为 null。targetType=page 时为 null。",
                },
                "databaseRows": {
                    "type": "array",
                    "description": "写入 database 的 rows；targetType=page 时必须为空数组。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "properties": {
                                "type": "array",
                                "description": "一行数据库属性值。每个 name 必须是数据库字段名。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "数据库属性名。"},
                                        "valueType": {
                                            "type": "string",
                                            "enum": [
                                                "string",
                                                "number",
                                                "boolean",
                                                "multi_select",
                                                "select",
                                                "status",
                                                "date",
                                                "url",
                                                "email",
                                                "phone_number",
                                                "empty",
                                            ],
                                            "description": "属性值类型，应尽量匹配数据库 schema。",
                                        },
                                        "stringValue": {
                                            "type": ["string", "null"],
                                            "description": "string/select/status/url/email/phone_number 的值；其他类型为 null。",
                                        },
                                        "numberValue": {
                                            "type": ["number", "null"],
                                            "description": "number 的值；其他类型为 null。",
                                        },
                                        "booleanValue": {
                                            "type": ["boolean", "null"],
                                            "description": "boolean 的值；其他类型为 null。",
                                        },
                                        "arrayValue": {
                                            "type": "array",
                                            "description": "multi_select 的选项列表；其他类型为空数组。",
                                            "items": {"type": "string"},
                                        },
                                        "dateStart": {
                                            "type": ["string", "null"],
                                            "description": "date 的 start，ISO 日期或日期时间；其他类型为 null。",
                                        },
                                        "dateEnd": {
                                            "type": ["string", "null"],
                                            "description": "date 的 end，可为空；其他类型为 null。",
                                        },
                                    },
                                    "required": [
                                        "name",
                                        "valueType",
                                        "stringValue",
                                        "numberValue",
                                        "booleanValue",
                                        "arrayValue",
                                        "dateStart",
                                        "dateEnd",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            "pageContent": {
                                "type": ["string", "null"],
                                "description": "可选的 database row 页面正文 Markdown；不需要则为 null。",
                            },
                        },
                        "required": ["properties", "pageContent"],
                        "additionalProperties": False,
                    },
                },
                "reason": {
                    "type": "string",
                    "description": "简短说明写入格式和字段选择，便于调试。",
                },
            },
            "required": [
                "targetType",
                "pageContent",
                "pageHeading",
                "pageAppendDivider",
                "pagePositionType",
                "pageAfterBlockId",
                "databaseTitleProperty",
                "databaseContentProperty",
                "databaseRows",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
}


def NormalizeTargetObjectType(targetObjectType: str) -> str:
    normalized = str(targetObjectType or "").strip().lower()
    return _TARGET_DATABASE if normalized == _TARGET_DATABASE else _TARGET_PAGE


def GetPrompt(
    targetContent: str,
    targetObjectType: str,
    memoriesText: str,
    commitPreview: str,
) -> str:
    normalizedTargetType = NormalizeTargetObjectType(targetObjectType)
    typeSpecificInstruction = _GetDatabasePromptSection() if normalizedTargetType == _TARGET_DATABASE else _GetPagePromptSection()

    return f"""
【已经形成的经验记忆】
{memoriesText}

【用户本次要写入的 commitPreview】
{commitPreview}

【目标写入对象类型】
{normalizedTargetType}

【目标写入对象完整内容】
{targetContent}

{typeSpecificInstruction}

请根据目标类型生成最终写入参数 JSON。
"""


def _GetPagePromptSection() -> str:
    return """
【WritePageService 参数 JSON 语义】
目标类型是 page。宿主程序会调用：
WritePageService.WriteContentToPage(pageId=targetWriteId, content=pageContent, heading=pageHeading, appendDivider=pageAppendDivider, position=pagePosition)

page 写入规则：
1. targetType 必须为 "page"。
2. pageContent 必须是最终要写入的 Markdown 正文，应该能被 MarkdownToBlocks 转成 Notion blocks。
3. pageHeading 可以为 "任务记录"、日期、commit 标题、或 null；应贴合目标页已有格式。
4. pageAppendDivider 根据目标页格式决定；若目标页通常用分割线分隔记录，设为 true，否则 false。
5. pagePositionType 默认用 "end"；只有目标页内容明确显示新内容应写到最前面时用 "start"；只有能从目标页内容中找到明确 block id 且必须插入其后时才用 "after_block"。
6. 如果 pagePositionType="after_block"，pageAfterBlockId 必须来自【目标写入对象完整内容】；否则 pageAfterBlockId 必须为 null。
7. databaseTitleProperty、databaseContentProperty 必须为 null；databaseRows 必须为 []。
"""


def _GetDatabasePromptSection() -> str:
    return f"""
【WriteDatabaseService 参数 JSON 语义】
目标类型是 database。宿主程序会先把 databaseRows 转成 rows，再调用：
WriteDatabaseService.WriteRowsToDatabase(databaseId=targetWriteId, rows=rows, titleProperty=databaseTitleProperty, contentProperty=databaseContentProperty)

转换规则：
- databaseRows[*].properties 会被转换为 rows 中的属性键值。
- valueType="string" 会写成 rich_text，除非该字段是 databaseTitleProperty，此时会写成 title。
- valueType="number" 会写成 number。
- valueType="boolean" 会写成 checkbox。
- valueType="multi_select" 会写成 multi_select。
- valueType="select" 会写成 select。
- valueType="status" 会写成 status。
- valueType="date" 会写成 date，dateStart 必须有值。
- valueType="url" / "email" / "phone_number" 会写成对应 Notion 属性类型。
- valueType="empty" 会写成空 rich_text；除非字段不必要，否则尽量不要输出 empty。
- 如果 databaseContentProperty="{_DEFAULT_DATABASE_CONTENT_PROPERTY}"，宿主程序会把每行 pageContent 写入新建 row 的页面正文，而不是作为 database property。

Database 写入规则：
1. targetType 必须为 "database"。
2. databaseTitleProperty 必须是目标数据库 Properties 中的 title 字段名；如果内容里显示 "任务名: `title`"，就用 "任务名"；如果显示 "Name: `title`"，就用 "Name"。
3. 每条待写入任务至少生成一个 databaseRows 项，不要把多条任务合并为一行。
4. properties 中的 name 尽量只使用目标数据库已有字段；不要凭空创建新字段。
5. 如果 commitPreview 的某些信息没有对应数据库字段，可以放入 pageContent，并把 databaseContentProperty 设为 "{_DEFAULT_DATABASE_CONTENT_PROPERTY}"；如果不需要 row 页面正文，databaseContentProperty 为 null 且 pageContent 为 null。
6. pageContent 可以用 Markdown，适合存放备注、原始任务描述、session_id、commit_id 等不适合放入属性的信息。
7. pageContent 字段只在 databaseContentProperty="{_DEFAULT_DATABASE_CONTENT_PROPERTY}" 时写入；否则每行 pageContent 应为 null。
8. pageContent、pageHeading、pagePositionType、pageAfterBlockId 必须为 null；pageAppendDivider 必须为 false。
"""


def _DatabasePropertyValueToSimpleValue(propertyInstruction: DatabasePropertyInstruction) -> Any:
    valueType = propertyInstruction.valueType.strip().lower()
    stringValue = propertyInstruction.stringValue

    if valueType == "number":
        return propertyInstruction.numberValue if propertyInstruction.numberValue is not None else 0
    if valueType == "boolean":
        return bool(propertyInstruction.booleanValue)
    if valueType == "multi_select":
        return [str(item) for item in propertyInstruction.arrayValue if str(item).strip()]
    if valueType == "select":
        return {"select": {"name": str(stringValue or "")}}
    if valueType == "status":
        return {"status": {"name": str(stringValue or "")}}
    if valueType == "date":
        start = str(propertyInstruction.dateStart or stringValue or "").strip()
        if not start:
            return None
        dateValue: dict[str, Any] = {"start": start}
        end = str(propertyInstruction.dateEnd or "").strip()
        if end:
            dateValue["end"] = end
        return {"date": dateValue}
    if valueType == "url":
        return {"url": str(stringValue or "")}
    if valueType == "email":
        return {"email": str(stringValue or "")}
    if valueType == "phone_number":
        return {"phone_number": str(stringValue or "")}
    if valueType == "empty":
        return None
    return str(stringValue or "")


def _BuildPropertyInstruction(data: dict[str, Any]) -> DatabasePropertyInstruction:
    rawArrayValue = data.get("arrayValue")
    return DatabasePropertyInstruction(
        name=str(data.get("name") or ""),
        valueType=str(data.get("valueType") or "string"),
        stringValue=None if data.get("stringValue") is None else str(data.get("stringValue")),
        numberValue=data.get("numberValue") if isinstance(data.get("numberValue"), (int, float)) else None,
        booleanValue=data.get("booleanValue") if isinstance(data.get("booleanValue"), bool) else None,
        arrayValue=[str(item) for item in rawArrayValue] if isinstance(rawArrayValue, list) else [],
        dateStart=None if data.get("dateStart") is None else str(data.get("dateStart")),
        dateEnd=None if data.get("dateEnd") is None else str(data.get("dateEnd")),
    )


def _BuildRowInstruction(data: dict[str, Any]) -> DatabaseRowInstruction:
    rawProperties = data.get("properties") if isinstance(data.get("properties"), list) else []
    return DatabaseRowInstruction(
        properties=[_BuildPropertyInstruction(item) for item in rawProperties if isinstance(item, dict)],
        pageContent=None if data.get("pageContent") is None else str(data.get("pageContent")),
    )


def BuildTaskWriteInstruction(data: dict[str, Any], expectedTargetObjectType: str | None = None) -> TaskWriteInstruction:
    databaseRowsRaw = data.get("databaseRows") if isinstance(data.get("databaseRows"), list) else []
    instruction = TaskWriteInstruction(
        targetType=NormalizeTargetObjectType(str(data.get("targetType") or "")),
        pageContent=None if data.get("pageContent") is None else str(data.get("pageContent")),
        pageHeading=None if data.get("pageHeading") is None else str(data.get("pageHeading")),
        pageAppendDivider=bool(data.get("pageAppendDivider", False)),
        pagePositionType=None if data.get("pagePositionType") is None else str(data.get("pagePositionType")),
        pageAfterBlockId=None if data.get("pageAfterBlockId") is None else str(data.get("pageAfterBlockId")),
        databaseTitleProperty=None if data.get("databaseTitleProperty") is None else str(data.get("databaseTitleProperty")),
        databaseContentProperty=None if data.get("databaseContentProperty") is None else str(data.get("databaseContentProperty")),
        databaseRows=[_BuildRowInstruction(item) for item in databaseRowsRaw if isinstance(item, dict)],
        reason=str(data.get("reason") or ""),
    )

    expectedTargetType = NormalizeTargetObjectType(expectedTargetObjectType or instruction.targetType)
    ValidateTaskWriteInstruction(instruction, expectedTargetType=expectedTargetType)
    return instruction


def ValidateTaskWriteInstruction(instruction: TaskWriteInstruction, expectedTargetType: str) -> None:
    if instruction.targetType != expectedTargetType:
        raise RuntimeError(f"Prompt3 返回 targetType={instruction.targetType!r}，但目标类型是 {expectedTargetType!r}。")

    if instruction.shouldWritePage:
        if not (instruction.pageContent or "").strip():
            raise RuntimeError("Prompt3 返回 page 写入，但 pageContent 为空。")
        if instruction.databaseRows:
            raise RuntimeError("Prompt3 返回 page 写入，但 databaseRows 非空。")
        if instruction.pagePositionType == "after_block" and not (instruction.pageAfterBlockId or "").strip():
            raise RuntimeError("Prompt3 返回 after_block 写入，但 pageAfterBlockId 为空。")
        return

    if instruction.shouldWriteDatabase:
        if not (instruction.databaseTitleProperty or "").strip():
            raise RuntimeError("Prompt3 返回 database 写入，但 databaseTitleProperty 为空。")
        rows = instruction.ToDatabaseRows()
        if not rows:
            raise RuntimeError("Prompt3 返回 database 写入，但 databaseRows 为空。")
        if instruction.pageContent is not None or instruction.pageHeading is not None:
            raise RuntimeError("Prompt3 返回 database 写入，但 pageContent/pageHeading 非空。")
        return

    raise RuntimeError(f"Prompt3 返回未知 targetType: {instruction.targetType!r}")


def RunRaw(
    targetContent: str,
    targetObjectType: str,
    memoriesText: str,
    commitPreview: str,
    openAiApiKey: str,
) -> dict[str, Any]:
    prompt = GetPrompt(
        targetContent=targetContent,
        targetObjectType=targetObjectType,
        memoriesText=memoriesText,
        commitPreview=commitPreview,
    )
    cgs = ChatCompletionRequest(apiKey=openAiApiKey, model="gpt-5.5")
    cgs.SetTimeout(1000)
    cgs.SetSystem(SystemPrompt)
    cgs.SetPrompt(prompt)
    cgs.SetTemperature(1)
    cgs.SetResponseFormat(JsonSchema)

    result = cgs.SendAndWait()
    data: dict[str, Any] = json.loads(result.text)
    return data


def Run(
    targetContent: str,
    targetObjectType: str,
    memoriesText: str,
    commitPreview: str,
    openAiApiKey: str,
) -> TaskWriteInstruction:
    data = RunRaw(
        targetContent=targetContent,
        targetObjectType=targetObjectType,
        memoriesText=memoriesText,
        commitPreview=commitPreview,
        openAiApiKey=openAiApiKey,
    )
    return BuildTaskWriteInstruction(data, expectedTargetObjectType=targetObjectType)


__all__ = [
    "BuildTaskWriteInstruction",
    "DatabasePropertyInstruction",
    "DatabaseRowInstruction",
    "GetPrompt",
    "JsonSchema",
    "NormalizeTargetObjectType",
    "Run",
    "RunRaw",
    "SystemPrompt",
    "TaskWriteInstruction",
    "ValidateTaskWriteInstruction",
]
