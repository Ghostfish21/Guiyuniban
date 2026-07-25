from __future__ import annotations

from typing import Any, Callable

from .AccessDatabaseService import AccessDatabaseService
from .NotionClient import NotionClient
from .NotionText import BlockToMarkdown, PageTitleFromPageData, VisibleTextFromRichText


class AccessPageService:
    """Access a concrete Notion child_page/page and read its block content.

    This service owns the child_page/page responsibilities. AccessGeneralPageService
    should stay as a broad object router and delegate page-specific rendering here.
    """

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient
        # Reuse the existing normalization/error helpers so id handling stays
        # consistent with database/general access services.
        self.databaseService = AccessDatabaseService(notionClient)

    def AccessPage(
        self,
        pageId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        pageId, pageData = self.ResolvePage(pageId=pageId, title=title)
        return self.BuildPageAccessResult(pageId, pageData, maxDepth=maxDepth, expandDirectPage=expandDirectPage)

    def AccessPageAsMarkdown(
        self,
        pageId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> str:
        return str(
            self.AccessPage(
                pageId=pageId,
                title=title,
                maxDepth=maxDepth,
                expandDirectPage=expandDirectPage,
            )["markdown"]
        )

    def AccessPageFormatSample(self, pageId: str, maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        return self.notionClient.FetchPageFormatSample(
            self.NormalizeInputId(pageId),
            maxTopLevelBlocks=maxTopLevelBlocks,
        )

    def ResolvePage(self, pageId: str | None = None, title: str | None = None) -> tuple[str, dict[str, Any]]:
        resolvedId = self.NormalizeInputId(pageId or "")
        pageData = self.SearchFirstPage(title) if not resolvedId and title else None
        if pageData:
            resolvedId = self.NormalizeInputId(str(pageData.get("id") or ""))
        if not resolvedId:
            raise RuntimeError("请提供 pageId，或提供能被 Notion Search 找到的 title。")
        if pageData is None or not pageData.get("properties"):
            pageData = self.notionClient.GetPage(resolvedId)
        return resolvedId, pageData

    def BuildPageAccessResult(
        self,
        pageId: str,
        pageData: dict[str, Any],
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        childDatabaseBlocks = self.FindTopLevelChildDatabaseBlocks(pageId)
        markdownLines = [
            "> 注释：Notion 页类型 - child_page",
            "",
            *self.notionClient.FetchBlockChildrenMarkdown(
                pageId,
                maxDepth=maxDepth,
                expandDirectPage=expandDirectPage,
            ),
        ]
        return {
            "pageId": pageId,
            "objectType": "page",
            "objectTypeLabel": "child_page",
            "title": PageTitleFromPageData(pageData),
            "containsChildDatabaseBlocks": bool(childDatabaseBlocks),
            "childDatabaseBlocks": childDatabaseBlocks,
            "format": "markdown",
            "expandDirectPage": expandDirectPage,
            "markdownLines": markdownLines,
            "markdown": "\n".join(markdownLines),
        }

    # Backward-compatible alias for older callers that used AccessGeneralPageService.AccessNormalPage.
    def AccessNormalPage(
        self,
        pageId: str,
        pageData: dict[str, Any],
        maxDepth: int,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        return self.BuildPageAccessResult(pageId, pageData, maxDepth=maxDepth, expandDirectPage=expandDirectPage)

    def AccessOtherBlock(
        self,
        blockId: str,
        blockData: dict[str, Any],
        maxDepth: int,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        blockType = str(blockData.get("type") or "other")
        markdownLines = [f"> 注释：Notion 页类型 - 其他 - {blockType}", ""]
        blockMarkdown = BlockToMarkdown(blockData, expandDirectPage=expandDirectPage)
        if blockMarkdown:
            markdownLines.append(blockMarkdown)
        if blockData.get("has_children"):
            markdownLines += self.notionClient.FetchBlockChildrenMarkdown(
                blockId,
                maxDepth=maxDepth,
                expandDirectPage=expandDirectPage,
            )
        return {
            "pageId": blockId,
            "blockId": blockId,
            "objectType": "other",
            "objectTypeLabel": "others",
            "blockType": blockType,
            "title": self.BlockTitleFromData(blockData),
            "format": "markdown",
            "expandDirectPage": expandDirectPage,
            "markdownLines": markdownLines,
            "markdown": "\n".join(markdownLines),
        }

    def SearchFirstPage(self, title: str | None) -> dict[str, Any] | None:
        if not title:
            return None
        candidates = [
            item
            for item in self.notionClient.Search(title, filterValue="page", pageSize=10).get("results", [])
            if item.get("object") == "page" and item.get("id")
        ]
        normalizedTitle = title.strip().casefold()
        return next((item for item in candidates if PageTitleFromPageData(item).strip().casefold() == normalizedTitle), None) or (
            candidates[0] if candidates else None
        )

    def FindTopLevelChildDatabaseBlocks(self, pageId: str) -> list[dict[str, Any]]:
        blocks, _ = self.TryNotionCall(lambda: self.notionClient.FetchTopLevelBlocks(pageId, maxBlocks=100))
        return [
            {
                "id": block.get("id"),
                "title": (block.get("child_database") or {}).get("title") if isinstance(block.get("child_database"), dict) else None,
                "note": "普通 page 内的 child_database block；不是父 page 自身的 rows。",
            }
            for block in blocks or []
            if block.get("type") == "child_database"
        ]

    def BlockTitleFromData(self, blockData: dict[str, Any]) -> str:
        blockType = str(blockData.get("type") or "")
        blockValue = blockData.get(blockType) if blockType else {}
        if isinstance(blockValue, dict):
            if blockValue.get("title"):
                return str(blockValue["title"])
            richText = blockValue.get("rich_text")
            text = VisibleTextFromRichText(richText) if isinstance(richText, list) else ""
            if text:
                return text
        return str(blockData.get("id") or "Other")

    def NormalizeInputId(self, value: str) -> str:
        return self.databaseService.NormalizeInputId(value)

    def TryNotionCall(self, func: Callable[[], Any]) -> tuple[Any | None, str]:
        return self.databaseService.TryNotionCall(func)
