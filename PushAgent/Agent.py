from __future__ import annotations

from typing import Any, Callable
from datetime import datetime
from pathlib import Path
import json
import os
import sys

from .NotionServices import (
    AccessPageService,
    DuplicateService,
    DuplicateWithContentService,
    DuplicateWithoutContentService,
    NotionClient,
    NotionContext,
    PageStructureService,
    PushTasksService,
    ReadTaskCategoriesService,
    WriteDatabaseService,
    WritePageService,
)
from .OpenAiServices import ChatCompletionRequest
from . import ConceptMemories
from .ConceptMemories import ConceptMemories, ConceptMemory
from .NotionServices.AccessGeneralPageService import AccessGeneralPageService
from .Prompts import MemoriesChecker, MemoriesBuilder, PageTypeRecognizer, TaskWritePointNavigator, TaskWriteExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()
    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - 只在未安装 rich 的环境中触发
    console = None
    RICH_AVAILABLE = False


class Agent:
    """Facade class that wires NotionServices and OpenAiServices together."""

    _LogRunTimestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _LogFile = Path(f"logs/push_agent_{_LogRunTimestamp}.log")

    @classmethod
    def _TimestampedLogFile(cls, raw: str | None = None) -> Path:
        """Return a log file path whose file name contains this Agent run timestamp."""
        timestamp = cls._LogRunTimestamp
        path = Path(raw or "logs/push_agent.log")

        # If the configured value looks like a directory, put the timestamped log inside it.
        if not path.suffix:
            return path / f"push_agent_{timestamp}.log"

        return path.with_name(f"{path.stem}_{timestamp}{path.suffix}")

    @classmethod
    def _SetLogFile(cls, configFile: str | None = None, overrides: dict[str, str] | None = None) -> None:
        """Resolve the local verbose log destination without printing anything to console."""
        raw = os.getenv("PUSH_AGENT_LOG_FILE") or os.getenv("AGENT_LOG_FILE")

        if not raw and overrides:
            raw = (
                overrides.get("push_agent_log_file")
                or overrides.get("agent_log_file")
                or overrides.get("log_file")
            )

        if not raw and configFile:
            try:
                path = Path(configFile)
                if path.exists():
                    for rawLine in path.read_text(encoding="utf-8").splitlines():
                        line = rawLine.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        if key in {"push_agent_log_file", "agent_log_file", "log_file"}:
                            raw = value.strip().strip('"').strip("'")
                            break
            except OSError:
                raw = None

        cls._LogRunTimestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls._LogFile = cls._TimestampedLogFile(raw)

    @staticmethod
    def _PreviewText(value: Any, limit: int = 900) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except TypeError:
                text = str(value)
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _SafeShortId(value: Any) -> str:
        text = str(value or "")
        return text[:8] + "…" if len(text) > 8 else text

    @classmethod
    def _AppendLog(cls, title: str, payload: Any | None = None) -> None:
        try:
            cls._LogFile.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().isoformat(timespec="seconds")
            with cls._LogFile.open("a", encoding="utf-8") as f:
                f.write(f"\n[{timestamp}] {title}\n")
                if payload is not None:
                    if isinstance(payload, str):
                        f.write(payload.rstrip() + "\n")
                    else:
                        f.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        except OSError:
            # 日志失败不能影响主流程，也不要把日志异常打到 console。
            pass

    @classmethod
    def _DebugPrint(cls, message: str, **kwargs: Any) -> None:
        cls._AppendLog(f"[Agent] {message}", kwargs or None)

    @classmethod
    def _LogText(cls, title: str, content: str) -> None:
        cls._AppendLog(title, content)

    @classmethod
    def _RenderEvent(cls, title: str, rows: dict[str, Any], *, borderStyle: str = "cyan") -> None:
        cls._AppendLog(title, rows)
        displayRows = [
            (str(key), cls._PreviewText(value))
            for key, value in rows.items()
            if value is not None and cls._PreviewText(value) != ""
        ]
        if not displayRows:
            displayRows = [("状态", "-")]

        if not RICH_AVAILABLE:
            print(title)
            for key, value in displayRows:
                print(f"{key}: {value}")
            return

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="white", overflow="fold", no_wrap=False, ratio=1)
        for key, value in displayRows:
            table.add_row(key, Text(str(value), style="white", overflow="fold", no_wrap=False))

        console.print(
            Panel(
                table,
                title=Text(title, style=f"bold {borderStyle}"),
                border_style=borderStyle,
                box=box.ROUNDED,
                expand=False,
            )
        )

    @classmethod
    def _RenderNotionEvent(cls, stage: str, action: str, **kwargs: Any) -> None:
        style = {"调用": "cyan", "结果": "green", "失败": "red"}.get(stage, "cyan")
        cls._RenderEvent(f"Notion API {stage}", {"动作": action, **kwargs}, borderStyle=style)

    @classmethod
    def _RenderChatGptEvent(cls, stage: str, promptName: str, **kwargs: Any) -> None:
        style = {"调用": "cyan", "结果": "green", "失败": "red"}.get(stage, "cyan")
        cls._RenderEvent(f"ChatGPT {stage}", {"Prompt": promptName, **kwargs}, borderStyle=style)

    @classmethod
    def _SummarizeNotionResult(cls, result: Any) -> dict[str, Any]:
        if isinstance(result, str):
            return {"文本长度": len(result or ""), "预览": result}
        if isinstance(result, list):
            return {"结果数量": len(result), "预览": result[:3]}
        if isinstance(result, dict):
            summary: dict[str, Any] = {}
            for key in ("id", "page_id", "database_id", "object", "objectType"):
                value = result.get(key)
                if value:
                    summary[key] = cls._SafeShortId(value) if key.endswith("id") or key == "id" else value
            if isinstance(result.get("markdown"), str):
                summary["markdown长度"] = len(result.get("markdown") or "")
                summary["markdown预览"] = result.get("markdown")
            if isinstance(result.get("results"), list):
                summary["结果数量"] = len(result.get("results") or [])
                summary["has_more"] = result.get("has_more")
            if isinstance(result.get("database"), dict):
                summary["database_id"] = cls._SafeShortId(result["database"].get("id"))
            if isinstance(result.get("page"), dict):
                summary["page_id"] = cls._SafeShortId(result["page"].get("id"))
            if not summary:
                summary["结果预览"] = result
            return summary
        return {"结果": result}

    def _CallNotion(
        self,
        action: str,
        params: dict[str, Any],
        runner: Callable[[], Any],
        resultSummary: Callable[[Any], dict[str, Any]] | None = None,
    ) -> Any:
        self._RenderNotionEvent("调用", action, **params)
        try:
            result = runner()
        except Exception as exc:
            self._RenderNotionEvent("失败", action, error=f"{type(exc).__name__}: {exc}")
            raise
        self._AppendLog(f"Notion API 原始结果: {action}", result)
        summary = resultSummary(result) if resultSummary else self._SummarizeNotionResult(result)
        self._RenderNotionEvent("结果", action, **summary)
        return result

    def _CallChatGpt(
        self,
        promptName: str,
        promptSummary: dict[str, Any],
        runner: Callable[[], Any],
        resultSummary: Callable[[Any], dict[str, Any]] | None = None,
    ) -> Any:
        self._RenderChatGptEvent("调用", promptName, **promptSummary)
        try:
            result = runner()
        except Exception as exc:
            self._RenderChatGptEvent("失败", promptName, error=f"{type(exc).__name__}: {exc}")
            raise
        self._AppendLog(f"ChatGPT 原始结果: {promptName}", result)
        summary = resultSummary(result) if resultSummary else {"结果": result}
        self._RenderChatGptEvent("结果", promptName, **summary)
        return result

    @staticmethod
    def _MemoryText(memory: Any) -> str:
        if hasattr(memory, "ToText"):
            return str(memory.ToText())
        return str(memory or "")

    def _RunMemoriesCheckerWithReason(
        self,
        memory: str,
        notionMarkdown: str,
        commitPreview: str,
        openAiApiKey: str,
    ) -> dict[str, Any]:
        """Run MemoriesChecker and keep the JSON reason for console UI output."""
        if hasattr(MemoriesChecker, "RunRaw"):
            data = MemoriesChecker.RunRaw(memory, notionMarkdown, commitPreview, openAiApiKey)
            return {
                "consistent": bool(data.get("consistent")),
                "reason": str(data.get("reason") or ""),
            }

        if all(hasattr(MemoriesChecker, name) for name in ("GetPrompt", "SystemPrompt", "JsonSchema")):
            prompt = MemoriesChecker.GetPrompt(memory, notionMarkdown, commitPreview)
            request = ChatCompletionRequest(apiKey=openAiApiKey, model="gpt-5.5")
            request.SetSystem(MemoriesChecker.SystemPrompt)
            request.SetPrompt(prompt)
            request.SetTemperature(1)
            request.SetResponseFormat(MemoriesChecker.JsonSchema)

            result = request.SendAndWait()
            data: dict[str, Any] = json.loads(result.text)
            return {
                "consistent": bool(data.get("consistent")),
                "reason": str(data.get("reason") or ""),
            }

        consistent = MemoriesChecker.Run(memory, notionMarkdown, commitPreview, openAiApiKey)
        return {"consistent": bool(consistent), "reason": ""}

    def _RunPageTypeRecognizerWithReason(
        self,
        parentMemory: Any,
        notionMarkdown: str,
        openAiApiKey: str,
    ) -> dict[str, Any]:
        """Run PageTypeRecognizer and keep pageType + reason for console UI output."""
        parentMemoryText = self._MemoryText(parentMemory)

        if hasattr(PageTypeRecognizer, "RunRaw"):
            data = PageTypeRecognizer.RunRaw(
                parentMemory=parentMemoryText,
                notionMarkdown=notionMarkdown,
                openAiApiKey=openAiApiKey,
            )
            pageType = str(data.get("pageType") or "").replace("$", "").strip()
            if not pageType:
                pageType = getattr(PageTypeRecognizer, "UNKNOWN_PAGE_TYPE", "UnknownChildPageType")
            return {
                "pageType": pageType,
                "matchedExistingType": bool(data.get("matchedExistingType")),
                "reason": str(data.get("reason") or ""),
            }

        pageType = PageTypeRecognizer.Run(
            parentMemory=parentMemory,
            notionMarkdown=notionMarkdown,
            openAiApiKey=openAiApiKey,
        )
        pageTypeText = str(pageType or "").replace("$", "").strip()
        if not pageTypeText:
            pageTypeText = "UnknownChildPageType"
        return {"pageType": pageTypeText, "matchedExistingType": None, "reason": ""}

    @staticmethod
    def _ExtractCreatedNotionId(result: dict[str, Any], *, objectName: str | None = None) -> str:
        """Extract the id from common Notion SDK / wrapper response shapes."""
        if not isinstance(result, dict):
            raise RuntimeError(f"Notion 创建结果不是 dict，无法提取 id: {result!r}")

        for key in ("id", "page_id", "database_id"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        nestedKeys: tuple[str, ...]
        if objectName == "page":
            nestedKeys = ("page", "result", "data")
        elif objectName == "database":
            nestedKeys = ("database", "result", "data")
        else:
            nestedKeys = ("page", "database", "result", "data")

        for key in nestedKeys:
            nested = result.get(key)
            if isinstance(nested, dict):
                for idKey in ("id", "page_id", "database_id"):
                    value = nested.get(idKey)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

        raise RuntimeError(f"Notion 创建结果中没有可识别的 id: {result!r}")

    def _CreateChildFromNavigatorAction(
        self,
        parentPageId: str,
        action: TaskWritePointNavigator.TaskWritePointNavigationAction,
    ) -> str:
        if not action.shouldCreateChild:
            raise ValueError("当前 action 不是创建 child_page/child_database 动作。")

        title = (action.createTitle or "").strip()
        if not title:
            raise ValueError("创建 child_page/child_database 时 createTitle 不能为空。")

        if action.shouldCreateChildPage:
            self._DebugPrint("根据导航器动作创建 child_page", parentPageId=parentPageId, title=title)
            result = self.CreateChildNotionPage(parentPageId=parentPageId, title=title)
            return self._ExtractCreatedNotionId(result, objectName="page")

        if action.shouldCreateChildDatabase:
            titleProperty = (action.createDatabaseTitleProperty or "Name").strip() or "Name"
            self._DebugPrint(
                "根据导航器动作创建 child_database",
                parentPageId=parentPageId,
                title=title,
                titleProperty=titleProperty,
            )
            result = self.CreateChildNotionDatabase(
                parentPageId=parentPageId,
                title=title,
                titleProperty=titleProperty,
            )
            return self._ExtractCreatedNotionId(result, objectName="database")

        raise ValueError(f"未知 createChildType: {action.createChildType!r}")

    def _DuplicateFromNavigatorAction(
        self,
        parentPageId: str,
        action: TaskWritePointNavigator.TaskWritePointNavigationAction,
    ) -> str:
        if not action.shouldDuplicate:
            raise ValueError("当前 action 不是复制动作。")

        sourceId = (action.duplicateSourceId or "").strip()
        if not sourceId:
            raise ValueError("复制动作 duplicateSourceId 不能为空。")

        duplicateTitle = (action.duplicateTitle or "").strip() or None
        duplicateSourceViewId = (action.duplicateSourceViewId or "").strip() or None

        if action.shouldDuplicateNonDatabase:
            self._DebugPrint(
                "根据导航器动作复制非 database 块",
                parentPageId=parentPageId,
                duplicateSourceId=sourceId,
                duplicateTitle=duplicateTitle,
            )
            result = self.DuplicateNotionBlock(
                blockId=sourceId,
                targetParentId=parentPageId,
                newTitle=duplicateTitle,
            )
            return self._ExtractCreatedNotionId(result)

        if action.shouldDuplicateDatabaseWithContent:
            self._DebugPrint(
                "根据导航器动作复制 database，包含内容",
                parentPageId=parentPageId,
                duplicateSourceId=sourceId,
                duplicateSourceViewId=duplicateSourceViewId,
                duplicateTitle=duplicateTitle,
            )
            result = self.DuplicateNotionDatabaseWithContent(
                databaseId=sourceId,
                databaseViewId=duplicateSourceViewId,
                parentPageId=parentPageId,
                newTitle=duplicateTitle,
            )
            database = result.get("database") if isinstance(result, dict) else None
            if isinstance(database, dict):
                return self._ExtractCreatedNotionId(database, objectName="database")
            return self._ExtractCreatedNotionId(result, objectName="database")

        if action.shouldDuplicateDatabaseWithoutContent:
            self._DebugPrint(
                "根据导航器动作复制 database，不包含内容",
                parentPageId=parentPageId,
                duplicateSourceId=sourceId,
                duplicateSourceViewId=duplicateSourceViewId,
                duplicateTitle=duplicateTitle,
            )
            result = self.DuplicateNotionDatabaseWithoutContent(
                databaseId=sourceId,
                databaseViewId=duplicateSourceViewId,
                parentPageId=parentPageId,
                newTitle=duplicateTitle,
            )
            return self._ExtractCreatedNotionId(result, objectName="database")

        raise ValueError(f"未知 duplicateServiceType: {action.duplicateServiceType!r}")

    def __init__(self, configFile: str | None = None, overrides: dict[str, str] | None = None) -> None:
        self._SetLogFile(configFile=configFile, overrides=overrides)
        self._DebugPrint("初始化 Agent", configFile=configFile, hasOverrides=overrides is not None, logFile=str(self._LogFile))
        self.notionContext = NotionContext(configFile=configFile, overrides=overrides)
        self.notionClient = NotionClient(token=self.notionContext.GetNotionToken())
        self.accessGeneralPageService = AccessGeneralPageService(self.notionClient)
        # Backward-compatible broad access alias used by the navigation loop.
        self.accessPageService = self.accessGeneralPageService
        # Direct child_page/page access service for AccessNotionPage and page-only callers.
        self.directAccessPageService = AccessPageService(self.notionClient)
        self.readTaskCategoriesService = ReadTaskCategoriesService(self.notionClient, self.notionContext)
        self.writePageService = WritePageService(self.notionClient)
        self.writeDatabaseService = WriteDatabaseService(self.notionClient)
        self.pageStructureService = PageStructureService(self.notionClient)
        self.duplicateService = DuplicateService(self.notionClient)
        self.duplicateWithContentService = DuplicateWithContentService(self.notionClient)
        self.duplicateWithoutContentService = DuplicateWithoutContentService(self.notionClient)
        self.pushTasksService = PushTasksService(self.writePageService, self.writeDatabaseService)
        self._DebugPrint("Agent 初始化完成")

    def PushTasks(self, commitPreview: str, config: dict[str, str]):
        notionRootId: str = config["notion_commit_page_id"]
        openaiApiKey: str = config["openai_api_key"]
        self._DebugPrint(
            "开始 PushTasks",
            notionRootId=notionRootId,
            commitPreviewLength=len(commitPreview or ""),
            configKeys=sorted(config.keys()),
        )

        # 已访问过的 Notion page/database/block id，避免循环探索
        visitedIds: set[str] = set()
        # 父 id -> 当前父页面下禁止再次进入的子 id
        forbiddenChildIdsByParent: dict[str, set[str]] = {}
        def addForbiddenChild(parentId: str, childId: str):
            self._DebugPrint("加入 forbidden child", parentId=parentId, childId=childId)
            if parentId not in forbiddenChildIdsByParent:
                forbiddenChildIdsByParent[parentId] = set()
            forbiddenChildIdsByParent[parentId].add(childId)
        def getForbiddenChildren(parentId: str) -> set[str]:
            return forbiddenChildIdsByParent.get(parentId, set())
        def markVisited(pageId: str):
            self._DebugPrint("标记已访问", pageId=pageId)
            visitedIds.add(pageId)
        def hasVisited(pageId: str) -> bool:
            return pageId in visitedIds

        # 读取 NOTION 页 和 本地 NotionRoot 经验记忆
        self._DebugPrint("读取 NotionRoot 页面", pageId=notionRootId)
        notionContent: str = self._CallNotion(
            "AccessPageAsMarkdown",
            {"用途": "读取 NotionRoot", "pageId": self._SafeShortId(notionRootId)},
            lambda: self.accessPageService.AccessPageAsMarkdown(pageId=notionRootId),
            lambda result: {"markdown长度": len(result or ""), "markdown预览": result},
        )
        self._DebugPrint("NotionRoot 页面读取完成", contentLength=len(notionContent or ""))
        self._LogText("[Agent] NotionRoot markdown", notionContent)
        rootPageMemories: ConceptMemory = ConceptMemories.Get("NotionRoot")
        self._DebugPrint("读取 NotionRoot 本地经验记忆", exists=rootPageMemories.exists)

        # 检查是否需要总结 NotionRoot 经验记忆
        summarizeMemFlag: bool = False
        if rootPageMemories.exists:
            self._DebugPrint("检查 NotionRoot 经验记忆一致性")
            memoryCheckResult = self._CallChatGpt(
                "MemoriesChecker",
                {
                    "用途": "检查 NotionRoot 经验记忆一致性",
                    "memory长度": len(rootPageMemories.ToText() or ""),
                    "notionMarkdown长度": len(notionContent or ""),
                    "commitPreview长度": len(commitPreview or ""),
                },
                lambda: self._RunMemoriesCheckerWithReason(
                    rootPageMemories.ToText(),
                    notionContent,
                    commitPreview,
                    openaiApiKey,
                ),
                lambda result: {
                    "isConsistent": bool(result.get("consistent")),
                    "reason": result.get("reason"),
                },
            )
            isConsistent = bool(memoryCheckResult.get("consistent"))
            self._DebugPrint(
                "NotionRoot 经验记忆检查完成",
                isConsistent=isConsistent,
                reason=memoryCheckResult.get("reason"),
            )
            summarizeMemFlag = not isConsistent
        else:
            self._DebugPrint("NotionRoot 不存在本地经验记忆，需要总结")
            summarizeMemFlag = True

        targetWriteId: str | None = None

        # 总结 NotionRoot 经验记忆
        if summarizeMemFlag:
            self._DebugPrint("总结 NotionRoot 经验记忆")
            memoriesBuildResult, shouldWriteToCurrent = self._CallChatGpt(
                "MemoriesBuilder",
                {
                    "用途": "总结 NotionRoot 经验记忆",
                    "pageType": "NotionRoot",
                    "notionMarkdown长度": len(notionContent or ""),
                    "commitPreview长度": len(commitPreview or ""),
                },
                lambda: MemoriesBuilder.Run(notionContent, commitPreview, openaiApiKey, "NotionRoot"),
                lambda result: {
                    "shouldWriteToCurrent": result[1],
                    "memory预览": result[0].ToText() if hasattr(result[0], "ToText") else result[0],
                },
            )
            rootPageMemories = memoriesBuildResult
            ConceptMemories.Save("NotionRoot", rootPageMemories)
            self._DebugPrint("保存 NotionRoot 经验记忆", shouldWriteToCurrent=shouldWriteToCurrent)
            self._LogText("[Agent] NotionRoot memory", rootPageMemories.ToText())
            if shouldWriteToCurrent:
                targetWriteId = notionRootId
                self._DebugPrint("NotionRoot 被判定为写入点", targetWriteId=targetWriteId)

        # 寻找 Tasks 写入点
        self._DebugPrint("开始寻找 Tasks 写入点")
        currentScope: list[str] = [notionRootId]
        memories: list[ConceptMemory] = [rootPageMemories]
        markVisited(notionRootId)

        if targetWriteId is None:
            shouldWriteToCurrent = False

            while not shouldWriteToCurrent:
                if len(currentScope) == 0:
                    raise RuntimeError("没有找到合适的 Tasks 写入点，并且已经回退到根之前。")

                currentId: str = currentScope[-1]
                self._DebugPrint("导航循环", depth=len(currentScope), currentId=currentId, scope=" -> ".join(currentScope))
                isNotionRoot: bool = currentId == notionRootId
                pageType: str
                pageTypeRaw: str | None = None

                if not isNotionRoot:
                    parentMemory: ConceptMemory = memories[-2]
                    self._DebugPrint("识别子页类型", currentId=currentId, parentConcept=parentMemory.conceptName)
                    currentContentForType: str = self._CallNotion(
                        "AccessPageAsMarkdown",
                        {"用途": "识别子页类型", "pageId": self._SafeShortId(currentId)},
                        lambda: self.accessPageService.AccessPageAsMarkdown(pageId=currentId),
                        lambda result: {"markdown长度": len(result or ""), "markdown预览": result},
                    )
                    pageTypeDecision = self._CallChatGpt(
                        "PageTypeRecognizer",
                        {
                            "用途": "识别子页类型",
                            "parentConcept": parentMemory.conceptName,
                            "notionMarkdown长度": len(currentContentForType or ""),
                        },
                        lambda: self._RunPageTypeRecognizerWithReason(
                            parentMemory=parentMemory,
                            notionMarkdown=currentContentForType,
                            openAiApiKey=openaiApiKey,
                        ),
                        lambda result: {
                            "pageTypeRaw": result.get("pageType"),
                            "matchedExistingType": result.get("matchedExistingType"),
                            "reason": result.get("reason"),
                        },
                    )
                    pageTypeRaw = str(pageTypeDecision.get("pageType") or "").strip()
                    if not pageTypeRaw:
                        raise NotImplementedError("需要接入 LLM 逻辑以识别当前子页类型 pageTypeRaw。")
                    pageType = parentMemory.conceptName + "$" + pageTypeRaw
                    self._DebugPrint(
                        "子页类型识别完成",
                        pageTypeRaw=pageTypeRaw,
                        pageType=pageType,
                        matchedExistingType=pageTypeDecision.get("matchedExistingType"),
                        reason=pageTypeDecision.get("reason"),
                    )
                else:
                    pageType = "NotionRoot"
                    pageTypeRaw = "NotionRoot"
                    self._DebugPrint("当前页是 NotionRoot", pageType=pageType)

                conceptMemory: ConceptMemory = ConceptMemories.Get(pageType)
                self._DebugPrint("读取当前页经验记忆", pageType=pageType, exists=conceptMemory.exists)
                currentContent: str = self._CallNotion(
                    "AccessPageAsMarkdown",
                    {"用途": "读取当前页", "pageId": self._SafeShortId(currentId)},
                    lambda: self.accessPageService.AccessPageAsMarkdown(pageId=currentId),
                    lambda result: {"markdown长度": len(result or ""), "markdown预览": result},
                )
                self._DebugPrint("当前页内容读取完成", currentId=currentId, contentLength=len(currentContent or ""))

                summarizeMemFlag = False
                if not isNotionRoot:
                    if conceptMemory.exists:
                        self._DebugPrint("检查当前页经验记忆一致性", pageType=pageType)
                        memoryCheckResult = self._CallChatGpt(
                            "MemoriesChecker",
                            {
                                "用途": "检查当前页经验记忆一致性",
                                "pageType": pageType,
                                "memory长度": len(conceptMemory.ToText() or ""),
                                "notionMarkdown长度": len(currentContent or ""),
                                "commitPreview长度": len(commitPreview or ""),
                            },
                            lambda: self._RunMemoriesCheckerWithReason(
                                conceptMemory.ToText(),
                                currentContent,
                                commitPreview,
                                openaiApiKey,
                            ),
                            lambda result: {
                                "isConsistent": bool(result.get("consistent")),
                                "reason": result.get("reason"),
                            },
                        )
                        isConsistent = bool(memoryCheckResult.get("consistent"))
                        self._DebugPrint(
                            "当前页经验记忆检查完成",
                            pageType=pageType,
                            isConsistent=isConsistent,
                            reason=memoryCheckResult.get("reason"),
                        )
                        summarizeMemFlag = not isConsistent
                    else:
                        self._DebugPrint("当前页不存在本地经验记忆，需要总结", pageType=pageType)
                        summarizeMemFlag = True

                if summarizeMemFlag:
                    self._DebugPrint("总结当前页经验记忆", pageType=pageType, pageTypeRaw=pageTypeRaw)
                    memoriesBuildResult, shouldWriteToCurrent1 = self._CallChatGpt(
                        "MemoriesBuilder",
                        {
                            "用途": "总结当前页经验记忆",
                            "pageType": pageType,
                            "pageTypeRaw": pageTypeRaw,
                            "notionMarkdown长度": len(currentContent or ""),
                            "commitPreview长度": len(commitPreview or ""),
                        },
                        lambda: MemoriesBuilder.Run(currentContent, commitPreview, openaiApiKey, pageTypeRaw),
                        lambda result: {
                            "shouldWriteToCurrent": result[1],
                            "memory预览": result[0].ToText() if hasattr(result[0], "ToText") else result[0],
                        },
                    )
                    currentMemory: ConceptMemory = memoriesBuildResult
                    ConceptMemories.Save(pageType, currentMemory)
                    self._DebugPrint("保存当前页经验记忆", pageType=pageType, shouldWriteToCurrent=shouldWriteToCurrent1)
                    if len(memories) == len(currentScope):
                        memories[-1] = currentMemory
                    else: memories.append(currentMemory)
                    self._LogText(f"[Agent] current memory: {pageType}", currentMemory.ToText())
                    if shouldWriteToCurrent1:
                        targetWriteId = currentId
                        shouldWriteToCurrent = True
                        self._DebugPrint("经验总结直接判定当前页为写入点", targetWriteId=targetWriteId, pageType=pageType)
                        break
                else:
                    self._DebugPrint("复用当前页经验记忆", pageType=pageType)
                    if len(memories) == len(currentScope):
                        memories[-1] = conceptMemory
                    else: memories.append(conceptMemory)

                memoriesText: str = "\n".join([m.ToText() for m in memories])
                forbiddenIds: set[str] = getForbiddenChildren(currentId)
                self._DebugPrint("准备调用写入点导航器", currentId=currentId, memoriesCount=len(memories), forbiddenIds=list(forbiddenIds))

                shouldWriteToCurrent2: bool = False
                goBackFlag: bool = False
                exploreTarget: str | None = None

                action = self._CallChatGpt(
                    "TaskWritePointNavigator",
                    {
                        "用途": "选择 Tasks 写入点/下一步动作",
                        "currentId": self._SafeShortId(currentId),
                        "memories数量": len(memories),
                        "forbiddenIds": [self._SafeShortId(item) for item in forbiddenIds],
                        "currentContent长度": len(currentContent or ""),
                        "commitPreview长度": len(commitPreview or ""),
                    },
                    lambda: TaskWritePointNavigator.Run(
                        currentContent=currentContent,
                        memoriesText=memoriesText,
                        commitPreview=commitPreview,
                        forbiddenIds=list(forbiddenIds),
                        openAiApiKey=openaiApiKey,
                    ),
                    lambda result: {
                        "shouldWriteToCurrent": result.shouldWriteToCurrent,
                        "goBackFlag": result.goBackFlag,
                        "exploreTarget": self._SafeShortId(result.exploreTarget),
                        "createChildType": result.createChildType,
                        "createTitle": result.createTitle,
                        "createDatabaseTitleProperty": result.createDatabaseTitleProperty,
                        "duplicateServiceType": result.duplicateServiceType,
                        "duplicateSourceId": self._SafeShortId(result.duplicateSourceId),
                        "duplicateSourceViewId": self._SafeShortId(result.duplicateSourceViewId),
                        "duplicateTitle": result.duplicateTitle,
                        "reason": result.reason,
                    },
                )

                shouldWriteToCurrent2: bool = action.shouldWriteToCurrent
                goBackFlag: bool = action.goBackFlag
                exploreTarget: str | None = action.exploreTarget
                self._DebugPrint(
                    "写入点导航器返回",
                    shouldWriteToCurrent=shouldWriteToCurrent2,
                    goBackFlag=goBackFlag,
                    exploreTarget=exploreTarget,
                    createChildType=action.createChildType,
                    createTitle=action.createTitle,
                    createDatabaseTitleProperty=action.createDatabaseTitleProperty,
                    duplicateServiceType=action.duplicateServiceType,
                    duplicateSourceId=action.duplicateSourceId,
                    duplicateSourceViewId=action.duplicateSourceViewId,
                    duplicateTitle=action.duplicateTitle,
                    reason=action.reason,
                )

                if shouldWriteToCurrent2:
                    targetWriteId = currentId
                    shouldWriteToCurrent = True
                    self._DebugPrint("导航器判定当前页为写入点", targetWriteId=targetWriteId)
                    break

                if goBackFlag:
                    if isNotionRoot:
                        raise RuntimeError("LLM 要求从 NotionRoot 回退，但根页面已经没有父级。")

                    self._DebugPrint("导航器要求回退", currentId=currentId)
                    poppedId: str = currentScope.pop()
                    memories.pop()
                    parentId: str = currentScope[-1]
                    addForbiddenChild(parentId, poppedId)
                    continue

                if exploreTarget is not None:
                    if exploreTarget in forbiddenIds:
                        # AI 选择了当前页面下已经禁入的子页，不进入，继续让下一轮重新判断
                        self._DebugPrint("导航器返回了 forbidden target，跳过", currentId=currentId, exploreTarget=exploreTarget)
                        continue

                    if hasVisited(exploreTarget):
                        # 如果该 ID 已经访问过，则不做任何改动，并将其加入当前页禁入 ID 列表
                        self._DebugPrint("导航器返回了已访问 target，加入 forbidden", currentId=currentId, exploreTarget=exploreTarget)
                        addForbiddenChild(currentId, exploreTarget)
                        continue

                    self._DebugPrint("进入下一层探索", fromId=currentId, exploreTarget=exploreTarget)
                    currentScope.append(exploreTarget)
                    markVisited(exploreTarget)

                    # 子页 memory 类型还没识别，下一轮会根据父 memory 和子页内容识别
                    memories.append(ConceptMemories.Get("NotExist"))

                    continue

                if action.shouldDuplicate:
                    duplicateSourceId = action.duplicateSourceId or ""
                    if duplicateSourceId in forbiddenIds:
                        self._DebugPrint("导航器返回了 forbidden duplicate source，跳过", currentId=currentId, duplicateSourceId=duplicateSourceId)
                        continue

                    createdDuplicateId = self._DuplicateFromNavigatorAction(
                        parentPageId=currentId,
                        action=action,
                    )

                    if hasVisited(createdDuplicateId):
                        self._DebugPrint("复制结果 id 已访问，加入 forbidden", currentId=currentId, createdDuplicateId=createdDuplicateId)
                        addForbiddenChild(currentId, createdDuplicateId)
                        continue

                    self._DebugPrint(
                        "复制后进入下一层探索",
                        fromId=currentId,
                        createdDuplicateId=createdDuplicateId,
                        duplicateServiceType=action.duplicateServiceType,
                        duplicateSourceId=action.duplicateSourceId,
                        duplicateSourceViewId=action.duplicateSourceViewId,
                        duplicateTitle=action.duplicateTitle,
                    )
                    currentScope.append(createdDuplicateId)
                    markVisited(createdDuplicateId)

                    # 复制结果的 memory 类型还没识别，下一轮会根据父 memory 和复制结果内容识别
                    memories.append(ConceptMemories.Get("NotExist"))

                    continue

                if action.shouldCreateChild:
                    createdChildId = self._CreateChildFromNavigatorAction(
                        parentPageId=currentId,
                        action=action,
                    )

                    if hasVisited(createdChildId):
                        self._DebugPrint("新建 child id 已访问，加入 forbidden", currentId=currentId, createdChildId=createdChildId)
                        addForbiddenChild(currentId, createdChildId)
                        continue

                    self._DebugPrint(
                        "新建 child 后进入下一层探索",
                        fromId=currentId,
                        createdChildId=createdChildId,
                        createChildType=action.createChildType,
                        createTitle=action.createTitle,
                    )
                    currentScope.append(createdChildId)
                    markVisited(createdChildId)

                    # 新建 child 的 memory 类型还没识别，下一轮会根据父 memory 和新 child 内容识别
                    memories.append(ConceptMemories.Get("NotExist"))

                    continue

                # 如果 LLM 没有给出任何有效行动，避免死循环
                if isNotionRoot:
                    raise RuntimeError("没有找到合适的 Tasks 写入点，且当前已经位于 NotionRoot。")

                self._DebugPrint("无有效行动，执行兜底回退", currentId=currentId)
                poppedId = currentScope.pop()
                memories.pop()

                parentId = currentScope[-1]
                addForbiddenChild(parentId, poppedId)

        if targetWriteId is None:
            raise RuntimeError("没有找到合适的 Tasks 写入点。")

        self._DebugPrint("PushTasks 找到最终写入点", targetWriteId=targetWriteId)

        # LLM Prompt 3: 根据最终写入点类型生成 WritePageService / WriteDatabaseService 参数，并执行写入。
        self._DebugPrint("读取最终写入点内容，准备 Prompt3", targetWriteId=targetWriteId)
        targetAccessResult = self._CallNotion(
            "AccessPage",
            {"用途": "读取最终写入点", "pageId": self._SafeShortId(targetWriteId)},
            lambda: self.accessPageService.AccessPage(pageId=targetWriteId),
            lambda result: {
                "objectType": result.get("objectType") if isinstance(result, dict) else "",
                "markdown长度": len(str(result.get("markdown") or "")) if isinstance(result, dict) else 0,
                "markdown预览": str(result.get("markdown") or "") if isinstance(result, dict) else result,
            },
        )
        targetObjectTypeRaw = str(targetAccessResult.get("objectType") or "page")
        targetObjectType = TaskWriteExecutor.NormalizeTargetObjectType(targetObjectTypeRaw)
        targetContent = str(targetAccessResult.get("markdown") or "")
        finalMemoriesText = "\n".join([m.ToText() for m in memories])

        self._DebugPrint(
            "调用 Prompt3 生成最终写入参数",
            targetWriteId=targetWriteId,
            targetObjectType=targetObjectType,
            targetContentLength=len(targetContent or ""),
        )
        writeInstruction = self._CallChatGpt(
            "TaskWriteExecutor",
            {
                "用途": "生成最终 Notion 写入参数",
                "targetWriteId": self._SafeShortId(targetWriteId),
                "targetObjectType": targetObjectType,
                "targetContent长度": len(targetContent or ""),
                "memoriesText长度": len(finalMemoriesText or ""),
                "commitPreview长度": len(commitPreview or ""),
            },
            lambda: TaskWriteExecutor.Run(
                targetContent=targetContent,
                targetObjectType=targetObjectType,
                memoriesText=finalMemoriesText,
                commitPreview=commitPreview,
                openAiApiKey=openaiApiKey,
            ),
            lambda result: {
                "targetType": result.targetType,
                "pageHeading": result.pageHeading,
                "pagePositionType": result.pagePositionType,
                "databaseTitleProperty": result.databaseTitleProperty,
                "databaseContentProperty": result.databaseContentProperty,
                "databaseRowCount": len(result.databaseRows),
                "reason": result.reason,
            },
        )
        self._DebugPrint(
            "Prompt3 返回最终写入参数",
            targetType=writeInstruction.targetType,
            pageHeading=writeInstruction.pageHeading,
            pageAppendDivider=writeInstruction.pageAppendDivider,
            pagePositionType=writeInstruction.pagePositionType,
            databaseTitleProperty=writeInstruction.databaseTitleProperty,
            databaseContentProperty=writeInstruction.databaseContentProperty,
            databaseRowCount=len(writeInstruction.databaseRows),
            reason=writeInstruction.reason,
        )

        if writeInstruction.shouldWriteDatabase:
            rows = writeInstruction.ToDatabaseRows()
            writeResult = self._CallNotion(
                "WriteRowsToDatabase",
                {
                    "databaseId": self._SafeShortId(targetWriteId),
                    "rowCount": len(rows),
                    "titleProperty": (writeInstruction.databaseTitleProperty or "Name").strip() or "Name",
                    "contentProperty": (writeInstruction.databaseContentProperty or "").strip() or None,
                },
                lambda: self.writeDatabaseService.WriteRowsToDatabase(
                    databaseId=targetWriteId,
                    rows=rows,
                    titleProperty=(writeInstruction.databaseTitleProperty or "Name").strip() or "Name",
                    contentProperty=(writeInstruction.databaseContentProperty or "").strip() or None,
                ),
                lambda result: {"写入行数": len(rows), "结果数量": len(result) if isinstance(result, list) else "", "结果预览": result},
            )
            self._DebugPrint("已写入 database", targetWriteId=targetWriteId, rowCount=len(rows))
            return {
                "targetWriteId": targetWriteId,
                "targetType": "database",
                "writeInstruction": writeInstruction.ToDict(),
                "writeResult": writeResult,
            }

        pagePosition = None
        if writeInstruction.pagePositionType == "start":
            pagePosition = self.writePageService.StartPosition()
        elif writeInstruction.pagePositionType == "after_block":
            pagePosition = self.writePageService.AfterBlockPosition(writeInstruction.pageAfterBlockId or "")
        # pagePositionType="end" 使用 Notion append 默认行为，避免向旧版 API 发送额外 position 字段。

        writeResult = self._CallNotion(
            "WriteContentToPage",
            {
                "pageId": self._SafeShortId(targetWriteId),
                "content长度": len(writeInstruction.pageContent or ""),
                "heading": writeInstruction.pageHeading,
                "appendDivider": writeInstruction.pageAppendDivider,
                "positionType": writeInstruction.pagePositionType,
            },
            lambda: self.writePageService.WriteContentToPage(
                pageId=targetWriteId,
                content=writeInstruction.pageContent or "",
                heading=writeInstruction.pageHeading,
                appendDivider=writeInstruction.pageAppendDivider,
                position=pagePosition,
            ),
        )
        self._DebugPrint("已写入 page", targetWriteId=targetWriteId)
        return {
            "targetWriteId": targetWriteId,
            "targetType": "page",
            "writeInstruction": writeInstruction.ToDict(),
            "writeResult": writeResult,
        }

    def NewChatCompletionRequest(self, model: str | None = None) -> ChatCompletionRequest:
        return ChatCompletionRequest(apiKey=self.notionContext.GetOpenAiApiKey(), model=model)

    def AccessNotionPage(
        self,
        pageId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "AccessPage",
            {"pageId": self._SafeShortId(pageId), "title": title, "maxDepth": maxDepth},
            lambda: self.directAccessPageService.AccessPage(pageId=pageId, title=title, maxDepth=maxDepth),
        )

    def AccessNotionPageFormatSample(self, pageId: str, maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        return self._CallNotion(
            "AccessPageFormatSample",
            {"pageId": self._SafeShortId(pageId), "maxTopLevelBlocks": maxTopLevelBlocks},
            lambda: self.accessPageService.AccessPageFormatSample(pageId, maxTopLevelBlocks=maxTopLevelBlocks),
        )

    def ReadTaskCategoriesFromNotion(self, pageTitle: str = "任务分类", maxDepth: int = 3) -> str:
        return self._CallNotion(
            "ReadTaskCategories",
            {"pageTitle": pageTitle, "maxDepth": maxDepth},
            lambda: self.readTaskCategoriesService.ReadTaskCategories(pageTitle=pageTitle, maxDepth=maxDepth),
            lambda result: {"文本长度": len(result or ""), "预览": result},
        )

    def WriteContentToNotionPage(
        self,
        pageId: str,
        content: str,
        heading: str | None = None,
        appendDivider: bool = False,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "WriteContentToPage",
            {
                "pageId": self._SafeShortId(pageId),
                "content长度": len(content or ""),
                "heading": heading,
                "appendDivider": appendDivider,
            },
            lambda: self.writePageService.WriteContentToPage(
                pageId=pageId,
                content=content,
                heading=heading,
                appendDivider=appendDivider,
            ),
        )

    def WriteRowsToNotionDatabase(
        self,
        databaseId: str,
        rows: list[dict[str, Any]],
        titleProperty: str = "Name",
        contentProperty: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._CallNotion(
            "WriteRowsToDatabase",
            {
                "databaseId": self._SafeShortId(databaseId),
                "rowCount": len(rows),
                "titleProperty": titleProperty,
                "contentProperty": contentProperty,
            },
            lambda: self.writeDatabaseService.WriteRowsToDatabase(
                databaseId=databaseId,
                rows=rows,
                titleProperty=titleProperty,
                contentProperty=contentProperty,
            ),
            lambda result: {"写入行数": len(rows), "结果数量": len(result) if isinstance(result, list) else "", "结果预览": result},
        )

    def CreateChildNotionPage(
        self,
        parentPageId: str,
        title: str,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "CreateChildPage",
            {
                "parentPageId": self._SafeShortId(parentPageId),
                "title": title,
                "childrenCount": len(children or []),
            },
            lambda: self.pageStructureService.CreateChildPage(parentPageId, title, children=children),
        )

    def CreateChildNotionDatabase(
        self,
        parentPageId: str,
        title: str,
        titleProperty: str = "Name",
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "CreateChildDatabase",
            {
                "parentPageId": self._SafeShortId(parentPageId),
                "title": title,
                "titleProperty": titleProperty,
                "propertyCount": len(properties or {}),
            },
            lambda: self.pageStructureService.CreateChildDatabase(
                parentPageId=parentPageId,
                title=title,
                titleProperty=titleProperty,
                properties=properties,
            ),
        )

    def DuplicateNotionBlock(
        self,
        blockId: str,
        targetParentId: str | None = None,
        newTitle: str | None = None,
        includeChildren: bool = True,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "DuplicateBlock",
            {
                "blockId": self._SafeShortId(blockId),
                "targetParentId": self._SafeShortId(targetParentId),
                "newTitle": newTitle,
                "includeChildren": includeChildren,
            },
            lambda: self.duplicateService.DuplicateBlock(
                blockId=blockId,
                targetParentId=targetParentId,
                newTitle=newTitle,
                includeChildren=includeChildren,
            ),
        )

    def DuplicateNotionDatabaseWithContent(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
        includePageContent: bool = True,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "DuplicateDatabaseWithContent",
            {
                "databaseId": self._SafeShortId(databaseId),
                "databaseViewId": self._SafeShortId(databaseViewId),
                "parentPageId": self._SafeShortId(parentPageId),
                "newTitle": newTitle,
                "includePageContent": includePageContent,
            },
            lambda: self.duplicateWithContentService.DuplicateDatabaseWithContent(
                databaseId=databaseId,
                databaseViewId=databaseViewId,
                parentPageId=parentPageId,
                newTitle=newTitle,
                includePageContent=includePageContent,
            ),
        )

    def DuplicateNotionDatabaseWithoutContent(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "DuplicateDatabaseWithoutContent",
            {
                "databaseId": self._SafeShortId(databaseId),
                "databaseViewId": self._SafeShortId(databaseViewId),
                "parentPageId": self._SafeShortId(parentPageId),
                "newTitle": newTitle,
            },
            lambda: self.duplicateWithoutContentService.DuplicateDatabaseWithoutContent(
                databaseId=databaseId,
                databaseViewId=databaseViewId,
                parentPageId=parentPageId,
                newTitle=newTitle,
            ),
        )

    def PushCommitPreviewToNotion(
        self,
        commitPreviewFile: str,
        targetPageId: str | None = None,
        targetDatabaseId: str | None = None,
        uncommitFile: str | None = None,
        archiveDir: str | None = None,
    ) -> dict[str, Any]:
        return self._CallNotion(
            "PushCommitPreview",
            {
                "commitPreviewFile": commitPreviewFile,
                "targetPageId": self._SafeShortId(targetPageId),
                "targetDatabaseId": self._SafeShortId(targetDatabaseId),
                "uncommitFile": uncommitFile,
                "archiveDir": archiveDir,
            },
            lambda: self.pushTasksService.PushCommitPreview(
                commitPreviewFile=commitPreviewFile,
                targetPageId=targetPageId,
                targetDatabaseId=targetDatabaseId,
                uncommitFile=uncommitFile,
                archiveDir=archiveDir,
            ),
        )
