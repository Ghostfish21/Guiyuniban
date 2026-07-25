import json
from typing import Any

from PushAgent.OpenAiServices import ChatCompletionRequest
from PushAgent.ConceptMemories import ChildPageTypeEntry, ConceptMemory


DEFAULT_CONCEPT_NAME = "NotionRoot"
ADDRESSING_FALLBACK_TARGET = "Notion 根"


SystemPrompt: str = (
    "你是一个 Notion 页经验记忆生成器。你会收到一份 '当前 Notion 页内容' 和一份 '用户本次要写入的 commitPreview'，"
    "'当前 Notion 页' 是一个按某种结构记录工作任务的页。你生成的经验记忆会提供给另一个 Agent，"
    "那个 Agent 的任务目标是将用户提供的任务条目信息写入到当前 Notion 页内。"
    "由于 Notion 页可能有多样的结构，所以你需要提前整理好页结构，方便后续 Agent 理解和寻址。"
    "经验记忆包含三部分：页总体结构概括、用户可能希望我如何寻址并找到真正记录任务的地方、页内有的子页类型。"
    "子页类型是一个列表，但是每个项的数据是自然语言的总结，包含：a. 该类型是否有效 b. 该类型的名字 c. 该类型的描述。"
    "你的任务是阅读当前 Notion 页，抽象出它的稳定页结构，并推断后续 Agent 在写入用户任务条目时，"
    "最可能应该写入到哪里、应该如何寻址，并结合本次 commitPreview 判断当前页是否可能就是最终写入点。"
    "经验记忆必须只描述稳定结构和寻址策略，不要复述大量具体任务内容。"
    "不要把普通任务条目的增删、日期变化、临时状态变化写成结构变化。"
    "如果页面中存在可打开子页类型，你必须归纳为 subPageTypes 列表；"
    "子页类型是一个抽象的类型，它不需要你将 Notion 页中每一个结构列出，它需要你按其功能分类并提供 有A类子页、B类子页 等。"
    "每个子页类型都必须包含：available=true、name、description。"
    "如果没有可用的可打开子页类型，subPageTypes 必须返回空列表。"
    "可打开子页类型只包括页面结构中可进入、可继续写入的子页；"
    "普通标题、普通列表项、checkbox、纯文本分组不应该被当作子页类型。"
    "必须根据页结构和内容猜测用户期望写入的位置，例如根页面、某类子页、某个数据库条目、"
    "某个日期/项目/状态分区，或者需要先打开某种子页再写入。"
    "如果认为当前Notion页就应该是写入的最终落点，必须把 shouldWriteToCurrent 设为 true；不然的话它应该是 false"
    "必须只返回 JSON。"
)


JsonSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "notion_page_experience_memory_build",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "pageStructureSummary": {
                    "type": "string",
                    "description": "对当前 Notion 页稳定结构的自然语言概括。",
                },
                "writeTargetGuess": {
                    "type": "string",
                    "description": "推断用户后续希望 Agent 把任务条目写入到哪里，以及如何寻址。",
                },
                "subPageTypes": {
                    "type": "array",
                    "description": "页内可打开子页类型列表；没有则为空列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "available": {
                                "type": "boolean",
                                "description": "该子页类型当前是否可用；有效类型必须为 true。",
                            },
                            "name": {
                                "type": "string",
                                "description": "该可打开子页类型的简短名称。",
                            },
                            "description": {
                                "type": "string",
                                "description": "说明这种子页是什么、通常承载什么内容、后续何时应该打开它。",
                            },
                        },
                        "required": ["available", "name", "description"],
                        "additionalProperties": False,
                    },
                },
                "addressingRuleUncertain": {
                    "type": "boolean",
                    "description": "如果只能弱猜测写入位置、页面缺少稳定寻址线索、或本次 commitPreview 对应多个同等可能位置，该值为 true。",
                },
                "shouldWriteToCurrent": {
                    "type": "boolean",
                    "description": "结合本次 commitPreview 判断：如果当前页适合作为最终写入落点，该值为 true；如果需要进入子页/数据库再写入，该值为 false。"
                }
            },
            "required": [
                "pageStructureSummary",
                "writeTargetGuess",
                "subPageTypes",
                "addressingRuleUncertain",
                "shouldWriteToCurrent"
            ],
            "additionalProperties": False,
        },
    },
}


def GetPrompt(notionMarkdown: str, commitPreview: str) -> str:
    return f"""
【用户本次要写入的 commitPreview】
{commitPreview}

【当前 Notion 页内容】
{notionMarkdown}

请结合本次 commitPreview，为当前 Notion 页生成该页的经验记忆。

要求：
1. pageStructureSummary：总结稳定页结构，不要罗列普通任务内容。
2. writeTargetGuess：根据页结构、内容和本次 commitPreview，猜测后续写入用户任务条目时应该写到哪里，并说明寻址方式。
3. addressingRuleUncertain：
   - 如果能从页面结构稳定判断写入位置和寻址方式，返回 false。
   - 如果只能给出弱猜测、页面缺少稳定分区/数据库/子页类型线索、或者存在多个同等可能的写入位置，返回 true。
   - 返回 true 时，writeTargetGuess 仍然写出最佳猜测和不确定原因；程序会记录 fallback：寻址到当前页。
4. subPageTypes：列出可打开子页类型。每一项必须包含：
   - available: true
   - name: 子页类型名称，注意，子页类型名称会作为文件名，所以它一定不能够包含不能作为文件名的符号
   - description: 这种子页是什么、承载什么内容、什么时候应该打开
   如果没有任何可打开子页类型，返回 []。
5. shouldWriteToCurrent：结合本次 commitPreview 判断当前页是否就是最终写入落点；如果需要进入子页/数据库再写入，返回 false。

返回字段：
- pageStructureSummary: string
- writeTargetGuess: string
- addressingRuleUncertain: boolean
- subPageTypes: array
- shouldWriteToCurrent: boolean
"""


def _NormalizeChildPageTypes(rawChildPageTypes: Any) -> list[ChildPageTypeEntry]:
    if not isinstance(rawChildPageTypes, list):
        return []

    result: list[ChildPageTypeEntry] = []
    seenNames: set[str] = set()

    for rawChildPageType in rawChildPageTypes:
        if not isinstance(rawChildPageType, dict):
            continue

        childPageType = ChildPageTypeEntry.FromDict(rawChildPageType)
        childPageType.name = childPageType.name.strip()

        if not childPageType.name:
            continue

        if childPageType.name in seenNames:
            continue

        seenNames.add(childPageType.name)
        result.append(childPageType)

    return result


def _BuildPossibleWriteTarget(writeTargetGuess: str, addressingRuleUncertain: bool) -> str:
    possibleWriteTarget = str(writeTargetGuess or "").strip()

    if not addressingRuleUncertain:
        return possibleWriteTarget

    fallbackText = f"寻址规则不确定：true\nfallback：寻址到{ADDRESSING_FALLBACK_TARGET}"

    if not possibleWriteTarget:
        return fallbackText

    return f"{possibleWriteTarget}\n\n{fallbackText}"


def BuildConceptMemory(data: dict[str, Any], conceptName: str = DEFAULT_CONCEPT_NAME) -> ConceptMemory:
    addressingRuleUncertain = bool(data.get("addressingRuleUncertain", False))

    return ConceptMemory(
        conceptName=str(conceptName or DEFAULT_CONCEPT_NAME).strip(),
        pageStructureSummary=str(data.get("pageStructureSummary", "")).strip(),
        possibleWriteTarget=_BuildPossibleWriteTarget(
            writeTargetGuess=str(data.get("writeTargetGuess", "")),
            addressingRuleUncertain=addressingRuleUncertain,
        ),
        childPageTypes=_NormalizeChildPageTypes(data.get("subPageTypes", [])),
    )


def RunRaw(notionMarkdown: str, commitPreview: str, openAiApiKey: str) -> dict[str, Any]:
    prompt = GetPrompt(notionMarkdown=notionMarkdown, commitPreview=commitPreview)
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
    notionMarkdown: str,
    commitPreview: str,
    openAiApiKey: str,
    conceptName: str = DEFAULT_CONCEPT_NAME,
) -> tuple[ConceptMemory, bool]:
    data = RunRaw(notionMarkdown=notionMarkdown, commitPreview=commitPreview, openAiApiKey=openAiApiKey)
    return BuildConceptMemory(data=data, conceptName=conceptName), data["shouldWriteToCurrent"]


__all__ = [
    "ADDRESSING_FALLBACK_TARGET",
    "BuildConceptMemory",
    "DEFAULT_CONCEPT_NAME",
    "GetPrompt",
    "JsonSchema",
    "Run",
    "RunRaw",
    "SystemPrompt",
]
