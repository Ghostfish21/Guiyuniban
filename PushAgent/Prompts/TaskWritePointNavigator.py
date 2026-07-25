import json
from dataclasses import dataclass
from typing import Any

from PushAgent.OpenAiServices import ChatCompletionRequest


_CREATE_CHILD_PAGE = "child_page"
_CREATE_CHILD_DATABASE = "child_database"
_VALID_CREATE_CHILD_TYPES = {_CREATE_CHILD_PAGE, _CREATE_CHILD_DATABASE}

_DUPLICATE_NON_DATABASE = "duplicate"
_DUPLICATE_DATABASE_WITH_CONTENT = "duplicate_with_content"
_DUPLICATE_DATABASE_WITHOUT_CONTENT = "duplicate_without_content"
_VALID_DUPLICATE_SERVICE_TYPES = {
    _DUPLICATE_NON_DATABASE,
    _DUPLICATE_DATABASE_WITH_CONTENT,
    _DUPLICATE_DATABASE_WITHOUT_CONTENT,
}


@dataclass
class TaskWritePointNavigationAction:
    shouldWriteToCurrent: bool
    goBackFlag: bool
    exploreTarget: str | None
    createChildType: str | None = None
    createTitle: str | None = None
    createDatabaseTitleProperty: str | None = None
    duplicateServiceType: str | None = None
    duplicateSourceId: str | None = None
    duplicateSourceViewId: str | None = None
    duplicateTitle: str | None = None
    reason: str = ""

    @property
    def shouldCreateChildPage(self) -> bool:
        return self.createChildType == _CREATE_CHILD_PAGE

    @property
    def shouldCreateChildDatabase(self) -> bool:
        return self.createChildType == _CREATE_CHILD_DATABASE

    @property
    def shouldCreateChild(self) -> bool:
        return self.createChildType in _VALID_CREATE_CHILD_TYPES

    @property
    def shouldDuplicate(self) -> bool:
        return self.duplicateServiceType in _VALID_DUPLICATE_SERVICE_TYPES and self.duplicateSourceId is not None

    @property
    def shouldDuplicateNonDatabase(self) -> bool:
        return self.duplicateServiceType == _DUPLICATE_NON_DATABASE and self.duplicateSourceId is not None

    @property
    def shouldDuplicateDatabaseWithContent(self) -> bool:
        return self.duplicateServiceType == _DUPLICATE_DATABASE_WITH_CONTENT and self.duplicateSourceId is not None

    @property
    def shouldDuplicateDatabaseWithoutContent(self) -> bool:
        return self.duplicateServiceType == _DUPLICATE_DATABASE_WITHOUT_CONTENT and self.duplicateSourceId is not None


SystemPrompt: str = (
    "你是一个 Notion 任务写入点导航器。你会收到 '当前 Notion 页内容'、'用户本次要写入的 commitPreview'、"
    "'已经形成的经验记忆' 和 '当前页禁止再次进入的子页/数据库 ID 列表'。"
    "另一个 Agent 的最终目标是把 commitPreview 中的任务条目信息写入到最合适的 Notion 位置。"
    "你的任务不是写入任务条目内容，而是判断导航下一步。"
    "你必须在八种行动中选择一种："
    "1. 如果当前页就是最合适的最终写入落点，返回 shouldWriteToCurrent=true；"
    "2. 如果应该继续打开当前页内已有的某个子页或子数据库，返回 exploreTarget 为当前页内容中明确出现的目标 ID；"
    "3. 如果当前页应该拥有一个新的任务承载子页，但当前页内容中尚不存在合适子页，返回 createChildType='child_page' 和 createTitle；"
    "4. 如果当前页应该拥有一个新的任务承载子数据库，但当前页内容中尚不存在合适子数据库，返回 createChildType='child_database' 和 createTitle；"
    "5. 如果当前页内容里有一个适合作模板的非 database 块，应复制该块，返回 duplicateServiceType='duplicate'、duplicateSourceId 和 duplicateTitle（duplicateSourceViewId=null）；"
    "6. 如果当前页内容里有一个适合作模板的 database 块，并且需要复制 schema 与 rows/page content，返回 duplicateServiceType='duplicate_with_content'、duplicateSourceId、duplicateSourceViewId 和 duplicateTitle；"
    "7. 如果当前页内容里有一个适合作模板的 database 块，但只需要复制 schema/空数据库，不复制 rows/content，返回 duplicateServiceType='duplicate_without_content'、duplicateSourceId、duplicateSourceViewId 和 duplicateTitle；"
    "8. 如果你认为虽然另一个Agent因为当前页的标题很值得探索，但当前页不适合写入，也不应创建或复制目标，返回 goBackFlag=true。"
    "经验记忆中的 pageStructureSummary、possibleWriteTarget、childPageTypes 是主要依据，同时必须结合 commitPreview 的实际内容判断本次任务更适合写到哪里；"
    "exploreTarget、duplicateSourceId 与 duplicateSourceViewId 必须是当前页内容中明确出现的 page id/database id/database_view_id/block id，不能编造，不能返回标题、类型名或描述。"
    "只有当当前页确实是合理父级，且当前页没有现成、未禁入、合适的子页/子数据库时，才允许创建新的 child_page 或 child_database。"
    "只有当经验记忆或当前页内容明确要求 duplicate/复制已有模板时，才允许使用 duplicateServiceType。"
    "duplicateServiceType='duplicate' 只能用于非 database 块；child_database/database 必须使用 duplicate_with_content 或 duplicate_without_content。"
    "createTitle 和 duplicateTitle 必须是简短、稳定、可复用的 Notion 标题，不能包含临时状态、完整任务正文或过长描述。"
    "如果创建 child_database，createDatabaseTitleProperty 通常填写 'Name'，除非经验记忆或当前页结构明确要求其他标题字段名。"
    "如果某个 ID 出现在 forbiddenIds 中，绝对不能把它作为 exploreTarget 或 duplicateSourceId；"
    "如果最佳目标被禁入，应选择其他合理目标、创建/复制新的合理目标，或在没有其他目标时回到上一页。"
    "八种行动必须互斥：写当前页、探索已有目标、创建 child_page、创建 child_database、复制非 database、复制 database with content、复制 database without content、回退只能选择一种。"
    "不要因为普通任务条目变化、日期变化、临时状态变化而偏离经验记忆中的稳定寻址策略。"
    "必须只返回 JSON。"
)


JsonSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "notion_task_write_point_navigation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "shouldWriteToCurrent": {
                    "type": "boolean",
                    "description": "当前页是否就是最终任务写入点。",
                },
                "goBackFlag": {
                    "type": "boolean",
                    "description": "是否应该放弃当前页并回到上一页。",
                },
                "exploreTarget": {
                    "type": ["string", "null"],
                    "description": "下一步要探索的当前页内已有子页/子数据库/块 ID；没有则为 null。",
                },
                "createChildType": {
                    "type": ["string", "null"],
                    "enum": ["child_page", "child_database", None],
                    "description": "是否需要在当前页下创建新的 child_page 或 child_database；不创建则为 null。",
                },
                "createTitle": {
                    "type": ["string", "null"],
                    "description": "需要创建的新 child_page/child_database 标题；不创建则为 null。",
                },
                "createDatabaseTitleProperty": {
                    "type": ["string", "null"],
                    "description": "创建 child_database 时使用的标题属性名，通常为 Name；非数据库创建时为 null。",
                },
                "duplicateServiceType": {
                    "type": ["string", "null"],
                    "enum": ["duplicate", "duplicate_with_content", "duplicate_without_content", None],
                    "description": "是否需要复制已有模板；非 database 块用 duplicate，database 块用 duplicate_with_content 或 duplicate_without_content。",
                },
                "duplicateSourceId": {
                    "type": ["string", "null"],
                    "description": "当前页内容中明确出现、需要被复制的 block/page/database ID；不复制则为 null。",
                },
                "duplicateSourceViewId": {
                    "type": ["string", "null"],
                    "description": "复制 database 时对应的 Notion database_view_id；必须来自当前页内容中明确出现的 database_view_id。非 database 复制或无法读取时为 null。",
                },
                "duplicateTitle": {
                    "type": ["string", "null"],
                    "description": "复制后目标的新标题；没有标题变更需求则为 null。",
                },
                "reason": {
                    "type": "string",
                    "description": "简短说明为什么选择该行动，便于调试。",
                },
            },
            "required": [
                "shouldWriteToCurrent",
                "goBackFlag",
                "exploreTarget",
                "createChildType",
                "createTitle",
                "createDatabaseTitleProperty",
                "duplicateServiceType",
                "duplicateSourceId",
                "duplicateSourceViewId",
                "duplicateTitle",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
}


def GetPrompt(
    currentContent: str,
    memoriesText: str,
    commitPreview: str,
    forbiddenIds: list[str] | set[str] | tuple[str, ...] | None = None,
) -> str:
    normalizedForbiddenIds = sorted(str(x).strip() for x in (forbiddenIds or []) if str(x).strip())

    return f"""
【已经形成的经验记忆，注意，这里可能包含父页的经验记忆】
{memoriesText}

【用户本次要写入的 commitPreview】
{commitPreview}

【当前 Notion 页内容】
{currentContent}

【当前页 forbiddenIds】
{json.dumps(normalizedForbiddenIds, ensure_ascii=False)}

请判断下一步行动。

行动规则：
1. 写入当前页：
   - 只有当当前页符合经验记忆中的最终写入位置，且适合承载本次 commitPreview，或者当前页本身就是任务条目稳定承载处时，返回：
     shouldWriteToCurrent=true, goBackFlag=false, exploreTarget=null, createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType=null, duplicateSourceId=null, duplicateSourceViewId=null, duplicateTitle=null。
2. 探索已有子页或子数据库：
   - 只有当经验记忆、当前页结构或本次 commitPreview 表明应该先进入某个已有子页/子数据库/块后再写入时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget="目标ID", createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType=null, duplicateSourceId=null, duplicateSourceViewId=null, duplicateTitle=null。
   - exploreTarget 必须来自【当前 Notion 页内容】中明确出现的 ID。
   - exploreTarget 不能属于 forbiddenIds。
   - 不要返回子页类型名、标题、自然语言描述或不存在的 ID。
3. 创建新的 child_page：
   - 只有当当前页是合适父级，且应该新增一个稳定、可复用的任务承载子页时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget=null, createChildType="child_page", createTitle="新子页标题", createDatabaseTitleProperty=null, duplicateServiceType=null, duplicateSourceId=null, duplicateSourceViewId=null, duplicateTitle=null。
   - createTitle 必须简短稳定，例如一个项目名、分类名或固定任务容器名；不要把完整任务正文当标题。
4. 创建新的 child_database：
   - 只有当当前页是合适父级，且任务更适合写入结构化数据库，同时当前页没有现成合适数据库时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget=null, createChildType="child_database", createTitle="新数据库标题", createDatabaseTitleProperty="Name", duplicateServiceType=null, duplicateSourceId=null, duplicateSourceViewId=null, duplicateTitle=null。
   - 如果经验记忆或当前页结构明确使用其他标题字段名，可以用该字段名；否则使用 Name。
5. 复制非 database 块：
   - 只有当当前页已有一个非 database 模板块适合复制成新目标时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget=null, createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType="duplicate", duplicateSourceId="模板块ID", duplicateSourceViewId=null, duplicateTitle="复制后的标题或null"。
   - duplicateSourceId 必须来自【当前 Notion 页内容】中明确出现的非 database block/page ID，且不能属于 forbiddenIds。
   - 绝对不要把 child_database/database ID 交给 duplicate；database 必须使用 duplicate_with_content 或 duplicate_without_content。
6. 复制 database，包含内容：
   - 只有当当前页已有一个 database 模板，且明确需要复制 schema、rows 以及 rows 的 page content 时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget=null, createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType="duplicate_with_content", duplicateSourceId="模板databaseID", duplicateSourceViewId="模板databaseViewID或null", duplicateTitle="复制后的数据库标题或null"。
   - duplicateSourceId 必须来自【当前 Notion 页内容】中明确出现的 database ID，且不能属于 forbiddenIds。
   - 如果当前页内容同时给出了 [Notion database_view_id: ...]，duplicateSourceViewId 必须填写该 view ID；否则填写 null，程序会尝试从 Notion Views API 补查。
7. 复制 database，不包含内容：
   - 只有当当前页已有一个 database 模板，但只应复制 schema/空数据库，不应复制 rows/content 时，返回：
     shouldWriteToCurrent=false, goBackFlag=false, exploreTarget=null, createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType="duplicate_without_content", duplicateSourceId="模板databaseID", duplicateSourceViewId="模板databaseViewID或null", duplicateTitle="复制后的数据库标题或null"。
   - 当当前页说明、经验记忆或用户意图出现 duplicate without content / 复制但不要内容 / 创建同结构空数据库 时，优先使用该动作。
   - 该动作表示复制 database 页/视图结构，不是把 inline child_database 直接塞进当前 page。
8. 回到上一页：
   - 当前页不是合适写入点，并且没有明确、未禁入、值得继续探索的目标，也不应在当前页创建或复制目标时，返回：
     shouldWriteToCurrent=false, goBackFlag=true, exploreTarget=null, createChildType=null, createTitle=null, createDatabaseTitleProperty=null, duplicateServiceType=null, duplicateSourceId=null, duplicateSourceViewId=null, duplicateTitle=null。

创建/复制约束：
- 优先使用当前页中已经存在且未禁入的合适 page/database ID；不要重复创建或复制同类容器。
- 只有在“应该存在一个容器但当前页确实没有合适容器”的情况下创建。
- 只有在“当前页有可复用模板，且经验记忆/页面指令/用户意图明确要求复制模板”的情况下复制。
- 创建或复制动作完成后，Agent 会自动进入新建 child_page/child_database/复制结果继续判断，不需要你提供新 ID。

返回字段：
- shouldWriteToCurrent: boolean
- goBackFlag: boolean
- exploreTarget: string | null
- createChildType: "child_page" | "child_database" | null
- createTitle: string | null
- createDatabaseTitleProperty: string | null
- duplicateServiceType: "duplicate" | "duplicate_with_content" | "duplicate_without_content" | null
- duplicateSourceId: string | null
- duplicateSourceViewId: string | null
- duplicateTitle: string | null
- reason: string
"""


def _NormalizeNullableString(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.lower() in {"null", "none"}:
        return None

    return text


def _NormalizeExploreTarget(value: Any) -> str | None:
    return _NormalizeNullableString(value)


def _NormalizeCreateChildType(value: Any) -> str | None:
    text = _NormalizeNullableString(value)
    if text is None:
        return None

    normalized = text.lower().strip()
    if normalized in {"page", "child page", "child-page", _CREATE_CHILD_PAGE}:
        return _CREATE_CHILD_PAGE

    if normalized in {"database", "child database", "child-database", _CREATE_CHILD_DATABASE}:
        return _CREATE_CHILD_DATABASE

    return None


def _NormalizeDuplicateServiceType(value: Any) -> str | None:
    text = _NormalizeNullableString(value)
    if text is None:
        return None

    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if normalized in {"duplicate", "non_database", "non_database_duplicate", "duplicate_service"}:
        return _DUPLICATE_NON_DATABASE
    if normalized in {"duplicate_with_content", "with_content", "database_with_content", "duplicate_database_with_content"}:
        return _DUPLICATE_DATABASE_WITH_CONTENT
    if normalized in {"duplicate_without_content", "without_content", "database_without_content", "duplicate_database_without_content"}:
        return _DUPLICATE_DATABASE_WITHOUT_CONTENT
    return None


def BuildAction(data: dict[str, Any]) -> TaskWritePointNavigationAction:
    shouldWriteToCurrent = bool(data.get("shouldWriteToCurrent", False))
    goBackFlag = bool(data.get("goBackFlag", False))
    exploreTarget = _NormalizeExploreTarget(data.get("exploreTarget"))
    createChildType = _NormalizeCreateChildType(data.get("createChildType"))
    createTitle = _NormalizeNullableString(data.get("createTitle"))
    createDatabaseTitleProperty = _NormalizeNullableString(data.get("createDatabaseTitleProperty"))
    duplicateServiceType = _NormalizeDuplicateServiceType(data.get("duplicateServiceType"))
    duplicateSourceId = _NormalizeNullableString(data.get("duplicateSourceId"))
    duplicateSourceViewId = _NormalizeNullableString(data.get("duplicateSourceViewId"))
    duplicateTitle = _NormalizeNullableString(data.get("duplicateTitle"))
    reason = str(data.get("reason", "")).strip()

    # 兜底保证八种行动互斥，避免 LLM 虽然满足 JSON schema 但给出冲突字段。
    # 优先级：写当前页 > 探索已有目标 > 复制模板 > 创建新目标 > 回退。
    # 这样可以最大限度避免重复创建已有结构；复制优先于新建是因为复制通常表示页面有明确模板。
    if shouldWriteToCurrent:
        return TaskWritePointNavigationAction(
            shouldWriteToCurrent=True,
            goBackFlag=False,
            exploreTarget=None,
            createChildType=None,
            createTitle=None,
            createDatabaseTitleProperty=None,
            duplicateServiceType=None,
            duplicateSourceId=None,
            duplicateSourceViewId=None,
            duplicateTitle=None,
            reason=reason,
        )

    if exploreTarget is not None:
        return TaskWritePointNavigationAction(
            shouldWriteToCurrent=False,
            goBackFlag=False,
            exploreTarget=exploreTarget,
            createChildType=None,
            createTitle=None,
            createDatabaseTitleProperty=None,
            duplicateServiceType=None,
            duplicateSourceId=None,
            duplicateSourceViewId=None,
            duplicateTitle=None,
            reason=reason,
        )

    if duplicateServiceType is not None and duplicateSourceId is not None:
        return TaskWritePointNavigationAction(
            shouldWriteToCurrent=False,
            goBackFlag=False,
            exploreTarget=None,
            createChildType=None,
            createTitle=None,
            createDatabaseTitleProperty=None,
            duplicateServiceType=duplicateServiceType,
            duplicateSourceId=duplicateSourceId,
            duplicateSourceViewId=duplicateSourceViewId if duplicateServiceType in {_DUPLICATE_DATABASE_WITH_CONTENT, _DUPLICATE_DATABASE_WITHOUT_CONTENT} else None,
            duplicateTitle=duplicateTitle,
            reason=reason,
        )

    if createChildType is not None and createTitle is not None:
        if createChildType == _CREATE_CHILD_PAGE:
            createDatabaseTitleProperty = None
        elif createDatabaseTitleProperty is None:
            createDatabaseTitleProperty = "Name"

        return TaskWritePointNavigationAction(
            shouldWriteToCurrent=False,
            goBackFlag=False,
            exploreTarget=None,
            createChildType=createChildType,
            createTitle=createTitle,
            createDatabaseTitleProperty=createDatabaseTitleProperty,
            duplicateServiceType=None,
            duplicateSourceId=None,
            duplicateSourceViewId=None,
            duplicateTitle=None,
            reason=reason,
        )

    return TaskWritePointNavigationAction(
        shouldWriteToCurrent=False,
        goBackFlag=goBackFlag,
        exploreTarget=None,
        createChildType=None,
        createTitle=None,
        createDatabaseTitleProperty=None,
        duplicateServiceType=None,
        duplicateSourceId=None,
        duplicateSourceViewId=None,
        duplicateTitle=None,
        reason=reason,
    )


def RunRaw(
    currentContent: str,
    memoriesText: str,
    commitPreview: str,
    forbiddenIds: list[str] | set[str] | tuple[str, ...] | None,
    openAiApiKey: str,
) -> dict[str, Any]:
    prompt = GetPrompt(
        currentContent=currentContent,
        memoriesText=memoriesText,
        commitPreview=commitPreview,
        forbiddenIds=forbiddenIds,
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
    currentContent: str,
    memoriesText: str,
    commitPreview: str,
    forbiddenIds: list[str] | set[str] | tuple[str, ...] | None,
    openAiApiKey: str,
) -> TaskWritePointNavigationAction:
    data = RunRaw(
        currentContent=currentContent,
        memoriesText=memoriesText,
        commitPreview=commitPreview,
        forbiddenIds=forbiddenIds,
        openAiApiKey=openAiApiKey,
    )
    action = BuildAction(data)
    return action


__all__ = [
    "BuildAction",
    "GetPrompt",
    "JsonSchema",
    "Run",
    "RunRaw",
    "SystemPrompt",
    "TaskWritePointNavigationAction",
]
