import json
from typing import Any

from PushAgent.OpenAiServices import ChatCompletionRequest


UNKNOWN_PAGE_TYPE = "UnknownChildPageType"


SystemPrompt: str = (
    "你是一个严格的 Notion 子页类型识别器。你会收到一份 '父页经验记忆'、"
    "'父页经验记忆中已经存在的子页类型' 和一份 '当前子页标题和内容'。"
    "父页经验记忆来自 Notion 页经验记忆生成器，包含父页稳定结构、写入寻址策略、以及可打开子页类型列表。"
    "你的任务是判断当前子页属于父页下的哪一种子页类型，并返回一个稳定、简短、可复用的类型名。"
    "子页类型是抽象类型，不是当前页面的具体标题；例如项目页、日期页、任务数据库条目、归档页等。"
    "如果当前子页明显匹配父页经验记忆中已有的某个子页类型，必须原样返回该类型的 name。"
    "如果父页经验记忆没有可用子页类型，或者当前子页确实不匹配任何已有类型，"
    "才允许根据当前页稳定结构归纳一个新的短类型名。"
    "不要因为普通任务条目增删、日期变化、状态变化、临时内容变化而改变类型判断。"
    "不要返回具体 Notion 页标题、页面 id、父页名称、路径，也不要在类型名中包含 '$'。"
    "必须只返回 JSON。"
)


JsonSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "notion_child_page_type_recognize",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "pageType": {
                    "type": "string",
                    "description": "当前子页的抽象类型名；优先原样使用父页已有子页类型的 name。",
                },
                "matchedExistingType": {
                    "type": "boolean",
                    "description": "是否匹配了父页经验记忆中已经存在的子页类型。",
                },
                "reason": {
                    "type": "string",
                    "description": "简要说明为什么判断为该类型。",
                },
            },
            "required": ["pageType", "matchedExistingType", "reason"],
            "additionalProperties": False,
        },
    },
}


def GetPrompt(
    parentMemory: str,
    notionMarkdown: str,
    childPageTypesText: str | None = None,
) -> str:
    normalizedChildPageTypesText = str(childPageTypesText or "").strip()
    if not normalizedChildPageTypesText:
        normalizedChildPageTypesText = "（父页经验记忆中没有额外传入的可用子页类型；请以父页经验记忆正文为准。）"

    return f"""
【父页经验记忆】
{parentMemory}

【父页经验记忆中已经存在的子页类型】
{normalizedChildPageTypesText}

【当前子页标题和内容】
{notionMarkdown}

请判断当前子页属于哪一种子页类型。

要求：
1. pageType 是类型名，不是当前页具体标题。
2. 如果能匹配父页已有子页类型，pageType 必须原样返回已有类型的 name，matchedExistingType 返回 true。
3. 如果无法匹配已有类型，但当前页存在稳定、可复用的结构特征，可以归纳一个新的简短类型名，matchedExistingType 返回 false。
4. 如果信息不足，也必须给出最合理的抽象类型名，不要返回空字符串。
5. pageType 不要包含父页 conceptName、'$'、Notion id、路径符号或冗长解释。

返回字段：
- pageType: string
- matchedExistingType: boolean
- reason: string
"""


def _NormalizePageType(pageType: Any) -> str:
    normalized = str(pageType or "").strip()
    normalized = normalized.replace("$", "").strip()
    return normalized or UNKNOWN_PAGE_TYPE


def RunRaw(
    parentMemory: str,
    notionMarkdown: str,
    openAiApiKey: str,
    childPageTypesText: str | None = None,
) -> dict[str, Any]:
    prompt = GetPrompt(
        parentMemory=parentMemory,
        notionMarkdown=notionMarkdown,
        childPageTypesText=childPageTypesText,
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
    parentMemory: str,
    notionMarkdown: str,
    openAiApiKey: str,
    childPageTypesText: str | None = None,
) -> str:
    data = RunRaw(
        parentMemory=parentMemory,
        notionMarkdown=notionMarkdown,
        openAiApiKey=openAiApiKey,
        childPageTypesText=childPageTypesText,
    )
    return _NormalizePageType(data.get("pageType"))


__all__ = [
    "GetPrompt",
    "JsonSchema",
    "Run",
    "RunRaw",
    "SystemPrompt",
    "UNKNOWN_PAGE_TYPE",
]
