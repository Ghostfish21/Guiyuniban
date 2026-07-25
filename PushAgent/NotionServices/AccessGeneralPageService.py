from __future__ import annotations

from typing import Any, Callable

from .AccessDatabaseService import AccessDatabaseService
from .AccessPageService import AccessPageService
from .NotionClient import NotionClient
from .NotionText import PageTitleFromPageData


class AccessGeneralPageService:
    """
    Access a Notion object in the broad UI sense.

    A normal page/child_page is delegated to AccessPageService.
    A database is read as rows only when the target object itself is a database.
    Other block types are delegated to AccessPageService's generic block renderer.
    """

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient
        self.pageService = AccessPageService(notionClient)
        self.databaseService = AccessDatabaseService(notionClient)

    def AccessPage(
        self,
        pageId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        """Access a normal page, database, or other block as Markdown-like content."""
        resolvedId = self.NormalizeInputId(pageId or "")
        searchObject = self.SearchFirstGeneralPageObject(title) if not resolvedId and title else None
        if searchObject:
            resolvedId = self.NormalizeInputId(str(searchObject.get("id") or ""))
        if not resolvedId:
            raise RuntimeError("请提供 pageId，或提供能被 Notion Search 找到的 title。")
        return (
            self.AccessSearchObject(searchObject, resolvedId, maxDepth, expandDirectPage)
            if searchObject
            else self.AccessResolvedId(resolvedId, maxDepth, expandDirectPage)
        )

    def AccessPageAsMarkdown(
        self,
        pageId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> str:
        return str(self.AccessPage(pageId=pageId, title=title, maxDepth=maxDepth, expandDirectPage=expandDirectPage)["markdown"])

    def AccessPageFormatSample(self, pageId: str, maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        """Return a compact format sample for a page or database id."""
        resolvedId = self.NormalizeInputId(pageId)
        databaseData, _ = self.TryNotionCall(lambda: self.notionClient.GetDatabase(resolvedId))
        if databaseData:
            return self.databaseService.BuildDatabaseFormatSample(resolvedId, databaseData, maxTopLevelBlocks)
        return self.pageService.AccessPageFormatSample(resolvedId, maxTopLevelBlocks=maxTopLevelBlocks)

    # Direct page access helpers, kept here only as delegating compatibility wrappers.
    def AccessNormalPage(self, pageId: str, pageData: dict[str, Any], maxDepth: int, expandDirectPage: bool = False) -> dict[str, Any]:
        return self.pageService.BuildPageAccessResult(pageId, pageData, maxDepth=maxDepth, expandDirectPage=expandDirectPage)

    def AccessOtherBlock(self, blockId: str, blockData: dict[str, Any], maxDepth: int, expandDirectPage: bool = False) -> dict[str, Any]:
        return self.pageService.AccessOtherBlock(blockId, blockData, maxDepth=maxDepth, expandDirectPage=expandDirectPage)

    # Database read helpers used by callers that need direct database access.
    def AccessDatabase(
        self,
        databaseId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        return self.databaseService.AccessDatabase(databaseId=databaseId, title=title, maxDepth=maxDepth, expandDirectPage=expandDirectPage)

    def AccessDatabaseAsMarkdown(
        self,
        databaseId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> str:
        return self.databaseService.AccessDatabaseAsMarkdown(
            databaseId=databaseId,
            title=title,
            maxDepth=maxDepth,
            expandDirectPage=expandDirectPage,
        )

    def AccessDatabaseFormatSample(self, databaseId: str, maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        return self.databaseService.AccessDatabaseFormatSample(databaseId=databaseId, maxTopLevelBlocks=maxTopLevelBlocks)

    def AccessSearchObject(self, searchObject: dict[str, Any], resolvedId: str, maxDepth: int, expandDirectPage: bool = False) -> dict[str, Any]:
        objectType = str(searchObject.get("object") or "")
        objectId = self.NormalizeInputId(str(searchObject.get("id") or resolvedId))
        if objectType == "database":
            return self.databaseService.BuildDatabaseAccessResult(objectId, searchObject, maxDepth, expandDirectPage)
        if objectType == "page":
            return self.pageService.BuildPageAccessResult(objectId, self.notionClient.GetPage(objectId), maxDepth, expandDirectPage)
        return self.AccessResolvedId(resolvedId, maxDepth, expandDirectPage)

    def AccessResolvedId(self, resolvedId: str, maxDepth: int, expandDirectPage: bool = False) -> dict[str, Any]:
        databaseData, databaseError = self.TryNotionCall(lambda: self.notionClient.GetDatabase(resolvedId))
        if databaseData:
            return self.databaseService.BuildDatabaseAccessResult(resolvedId, databaseData, maxDepth, expandDirectPage)

        pageData, pageError = self.TryNotionCall(lambda: self.notionClient.GetPage(resolvedId))
        if pageData:
            return self.pageService.BuildPageAccessResult(resolvedId, pageData, maxDepth, expandDirectPage)

        blockData, blockError = self.TryNotionCall(lambda: self.notionClient.GetBlock(resolvedId))
        if blockData:
            return self.pageService.AccessOtherBlock(resolvedId, blockData, maxDepth, expandDirectPage)

        raise RuntimeError(
            f"无法识别 Notion 对象：{resolvedId}。请确认 ID 是否正确，以及 integration 是否有访问权限。"
            f"{self.databaseService.CombineErrors(databaseError, pageError, blockError)}"
        )

    def SearchFirstGeneralPageObject(self, title: str | None) -> dict[str, Any] | None:
        if not title:
            return None
        candidates = [
            item
            for item in self.notionClient.Search(title, pageSize=10).get("results", [])
            if item.get("object") in {"page", "database"} and item.get("id")
        ]
        normalizedTitle = title.strip().casefold()
        return next((item for item in candidates if self.TitleFromSearchObject(item).strip().casefold() == normalizedTitle), None) or (
            candidates[0] if candidates else None
        )

    def FindTopLevelChildDatabaseBlocks(self, pageId: str) -> list[dict[str, Any]]:
        return self.pageService.FindTopLevelChildDatabaseBlocks(pageId)

    def TitleFromSearchObject(self, searchObject: dict[str, Any]) -> str:
        if searchObject.get("object") == "database":
            return self.databaseService.DatabaseTitleFromData(searchObject)
        if searchObject.get("object") == "page":
            return PageTitleFromPageData(searchObject)
        return str(searchObject.get("id") or "")

    def NormalizeInputId(self, value: str) -> str:
        return self.databaseService.NormalizeInputId(value)

    def TryNotionCall(self, func: Callable[[], Any]) -> tuple[Any | None, str]:
        return self.databaseService.TryNotionCall(func)
