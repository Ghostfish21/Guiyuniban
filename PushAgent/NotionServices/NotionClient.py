from __future__ import annotations

from typing import Any
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .NotionText import BlockToMarkdown, DatabaseTitleFromDatabaseData, PageTitleFromPageData


class NotionClient:
    def __init__(
        self,
        token: str | None = None,
        notionVersion: str | None = None,
        baseUrl: str = "https://api.notion.com/v1",
        timeoutSeconds: int = 45,
    ) -> None:
        self.token = token or os.getenv("NOTION_TOKEN") or os.getenv("NOTION_API_KEY") or ""
        self.notionVersion = notionVersion or os.getenv("NOTION_VERSION") or "2022-06-28"
        self.baseUrl = baseUrl.rstrip("/")
        self.timeoutSeconds = timeoutSeconds
        # Views API requires a newer Notion-Version than the legacy database API.
        # Keep the existing default for older read/write paths, but allow view-aware
        # duplicate services to opt into the version that exposes /views.
        self.viewsNotionVersion = os.getenv("NOTION_VIEWS_VERSION") or "2026-03-11"
        self._pageTitleCache: dict[str, str] = {}
        self._databaseTitleCache: dict[str, str] = {}
        self._databaseViewIdCache: dict[str, str] = {}

    def RequireToken(self) -> str:
        if not self.token:
            raise RuntimeError("缺少 Notion token。请设置 NOTION_TOKEN / NOTION_API_KEY。")
        return self.token

    def Request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        notionVersion: str | None = None,
    ) -> dict[str, Any]:
        token = self.RequireToken()
        url = f"{self.baseUrl}{path}"
        bodyData = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=bodyData,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Notion-Version": notionVersion or self.notionVersion,
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeoutSeconds) as response:
                rawText = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            errorBody = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Notion API 请求失败 {exc.code}: {errorBody}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Notion API 请求失败: {exc}") from exc

        if not rawText.strip():
            return {}
        try:
            return json.loads(rawText)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Notion API 响应不是合法 JSON: {exc}") from exc

    def NormalizeId(self, value: str) -> str:
        compactValue = re.sub(r"[^0-9a-fA-F]", "", value or "")
        if len(compactValue) == 32:
            return "-".join(
                [compactValue[:8], compactValue[8:12], compactValue[12:16], compactValue[16:20], compactValue[20:]]
            )
        return value

    def SearchPage(self, title: str, pageSize: int = 10) -> str:
        payload = {
            "query": title,
            "filter": {"property": "object", "value": "page"},
            "page_size": pageSize,
        }
        data = self.Request("POST", "/search", payload)
        for item in data.get("results", []):
            if item.get("object") == "page" and item.get("id"):
                return str(item["id"])
        return ""

    def Search(self, query: str, filterValue: str | None = None, pageSize: int = 10) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "page_size": pageSize}
        if filterValue:
            payload["filter"] = {"property": "object", "value": filterValue}
        return self.Request("POST", "/search", payload)

    def GetPage(self, pageId: str) -> dict[str, Any]:
        return self.Request("GET", f"/pages/{self.NormalizeId(pageId)}")

    def GetBlock(self, blockId: str) -> dict[str, Any]:
        return self.Request("GET", f"/blocks/{self.NormalizeId(blockId)}")

    def GetDatabase(self, databaseId: str) -> dict[str, Any]:
        return self.Request("GET", f"/databases/{self.NormalizeId(databaseId)}")

    def QueryDatabase(
        self,
        databaseId: str,
        filterPayload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        pageSize: int = 100,
        startCursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"page_size": pageSize}
        if filterPayload:
            payload["filter"] = filterPayload
        if sorts:
            payload["sorts"] = sorts
        if startCursor:
            payload["start_cursor"] = startCursor
        return self.Request("POST", f"/databases/{self.NormalizeId(databaseId)}/query", payload)

    def GetBlockChildren(self, blockId: str, pageSize: int = 100, startCursor: str | None = None) -> dict[str, Any]:
        query = f"?page_size={pageSize}"
        if startCursor:
            query += "&start_cursor=" + urllib.parse.quote(startCursor)
        return self.Request("GET", f"/blocks/{self.NormalizeId(blockId)}/children{query}")

    def FetchTopLevelBlocks(self, blockId: str, maxBlocks: int = 100) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor = ""
        while len(blocks) < maxBlocks:
            data = self.GetBlockChildren(blockId, pageSize=min(100, maxBlocks - len(blocks)), startCursor=cursor or None)
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor") or ""
            if not cursor:
                break
        return blocks

    def EnrichBlockForMarkdown(self, block: dict[str, Any]) -> None:
        """Attach extra metadata needed by the Markdown renderer."""
        blockType = str(block.get("type") or "")
        blockValue = block.get(blockType) or {}
        if not isinstance(blockValue, dict):
            return

        if blockType == "link_to_page":
            pageId = str(blockValue.get("page_id") or "")
            databaseId = str(blockValue.get("database_id") or "")
            if pageId:
                block["_notion_link_title"] = self.ResolvePageTitle(pageId)
            elif databaseId:
                block["_notion_link_title"] = self.ResolveDatabaseTitle(databaseId)
                block["_notion_database_view_id"] = self.ResolveDatabaseViewId(databaseId)
            return

        if blockType == "child_database":
            databaseId = str(block.get("id") or blockValue.get("database_id") or "")
            if databaseId:
                block["_notion_database_view_id"] = self.ResolveDatabaseViewId(databaseId)

    def ResolvePageTitle(self, pageId: str) -> str:
        normalizedPageId = self.NormalizeId(pageId)
        if not normalizedPageId:
            return ""
        if normalizedPageId in self._pageTitleCache:
            return self._pageTitleCache[normalizedPageId]

        try:
            title = PageTitleFromPageData(self.GetPage(normalizedPageId))
        except Exception:
            title = ""
        self._pageTitleCache[normalizedPageId] = title
        return title

    def ResolveDatabaseTitle(self, databaseId: str) -> str:
        normalizedDatabaseId = self.NormalizeId(databaseId)
        if not normalizedDatabaseId:
            return ""
        if normalizedDatabaseId in self._databaseTitleCache:
            return self._databaseTitleCache[normalizedDatabaseId]

        try:
            title = DatabaseTitleFromDatabaseData(self.GetDatabase(normalizedDatabaseId))
        except Exception:
            title = ""
        self._databaseTitleCache[normalizedDatabaseId] = title
        return title

    def ResolveDatabaseViewId(self, databaseId: str) -> str:
        """Return the first accessible view id for a database, if the Views API can see one."""
        normalizedDatabaseId = self.NormalizeId(databaseId)
        if not normalizedDatabaseId:
            return ""
        if normalizedDatabaseId in self._databaseViewIdCache:
            return self._databaseViewIdCache[normalizedDatabaseId]

        viewId = ""
        try:
            viewId = self.FirstDatabaseViewId(normalizedDatabaseId)
        except Exception:
            viewId = ""
        self._databaseViewIdCache[normalizedDatabaseId] = viewId
        return viewId

    def ListDatabaseViews(self, databaseId: str) -> dict[str, Any]:
        query = "?database_id=" + urllib.parse.quote(self.NormalizeId(databaseId))
        return self.Request("GET", f"/views{query}", notionVersion=self.viewsNotionVersion)

    def GetView(self, viewId: str) -> dict[str, Any]:
        return self.Request("GET", f"/views/{self.NormalizeId(viewId)}", notionVersion=self.viewsNotionVersion)

    def CreateView(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.Request("POST", "/views", payload, notionVersion=self.viewsNotionVersion)

    def UpdateView(self, viewId: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.Request("PATCH", f"/views/{self.NormalizeId(viewId)}", payload, notionVersion=self.viewsNotionVersion)

    def DeleteView(self, viewId: str) -> dict[str, Any]:
        return self.Request("DELETE", f"/views/{self.NormalizeId(viewId)}", notionVersion=self.viewsNotionVersion)

    def FirstDatabaseViewId(self, databaseId: str) -> str:
        data = self.ListDatabaseViews(databaseId)
        for item in data.get("results", []):
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
        return ""

    def GetDatabaseWithViewsVersion(self, databaseId: str) -> dict[str, Any]:
        return self.Request("GET", f"/databases/{self.NormalizeId(databaseId)}", notionVersion=self.viewsNotionVersion)

    def CreateDatabase(
        self,
        parentPageId: str,
        title: str,
        properties: dict[str, Any] | None = None,
        isInline: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": self.NormalizeId(parentPageId)},
            "title": self._TitleRichText(title),
            "is_inline": isInline,
        }
        if properties:
            # Legacy database API accepts the schema directly as `properties`.
            # This path intentionally creates a full-page database by default,
            # unlike CreateChildDatabase which creates an inline child_database.
            payload["properties"] = properties
        return self.Request("POST", "/databases", payload)

    def PrimaryDatabaseDataSourceId(self, databaseId: str) -> str:
        databaseData: dict[str, Any] = {}
        try:
            databaseData = self.GetDatabaseWithViewsVersion(databaseId)
        except Exception:
            databaseData = {}

        for key in ("data_sources", "dataSources"):
            dataSources = databaseData.get(key)
            if isinstance(dataSources, list):
                for item in dataSources:
                    if isinstance(item, dict) and item.get("id"):
                        return str(item["id"])

        for key in ("data_source", "dataSource", "initial_data_source", "initialDataSource"):
            dataSource = databaseData.get(key)
            if isinstance(dataSource, dict) and dataSource.get("id"):
                return str(dataSource["id"])

        # Last resort: the default view carries the new database's data_source_id.
        viewId = self.FirstDatabaseViewId(databaseId)
        if viewId:
            viewData = self.GetView(viewId)
            dataSourceId = str(viewData.get("data_source_id") or "")
            if dataSourceId:
                return dataSourceId

        return ""

    def FetchBlockChildrenMarkdown(
        self,
        blockId: str,
        depth: int = 0,
        maxDepth: int = 3,
        expandDirectPage: bool = True,
    ) -> list[str]:
        """Fetch block children and preserve common Notion formatting as Markdown-like text."""
        if depth > maxDepth:
            return []

        lines: list[str] = []
        cursor = ""
        numberedListCounter = 1
        while True:
            data = self.GetBlockChildren(blockId, startCursor=cursor or None)
            for block in data.get("results", []):
                blockType = str(block.get("type") or "")
                listNumber = numberedListCounter if blockType == "numbered_list_item" else 1
                self.EnrichBlockForMarkdown(block)
                markdown = BlockToMarkdown(
                    block,
                    depth=depth,
                    listNumber=listNumber,
                    expandDirectPage=expandDirectPage,
                )
                if markdown:
                    lines.extend(markdown.splitlines())

                if blockType == "numbered_list_item":
                    numberedListCounter += 1
                else:
                    numberedListCounter = 1

                shouldFetchChildren = bool(block.get("has_children") and block.get("id"))
                if blockType == "child_page" and not expandDirectPage:
                    shouldFetchChildren = False

                if shouldFetchChildren:
                    childLines = self.FetchBlockChildrenMarkdown(
                        str(block["id"]),
                        depth + 1,
                        maxDepth,
                        expandDirectPage=expandDirectPage,
                    )
                    if childLines:
                        lines.extend(childLines)

                if blockType == "toggle":
                    lines.append(("  " * depth) + "</details>")

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor") or ""
            if not cursor:
                break
        return lines

    def FetchPageFormatSample(
        self,
        pageId: str,
        maxTopLevelBlocks: int = 40,
        maxChildBlocks: int = 10,
    ) -> dict[str, Any]:
        pageData = self.GetPage(pageId)
        rawBlocks = self.FetchTopLevelBlocks(pageId, maxBlocks=maxTopLevelBlocks)
        sampleBlocks: list[dict[str, Any]] = []
        for block in rawBlocks:
            blockType = str(block.get("type") or "")
            blockValue = block.get(blockType) if blockType else {}
            if not isinstance(blockValue, dict):
                blockValue = {}
            sampleBlock: dict[str, Any] = {
                "id": block.get("id"),
                "type": blockType,
                "markdown": BlockToMarkdown(block),
                "has_children": bool(block.get("has_children")),
            }
            if blockType in {"child_page", "child_database"}:
                sampleBlock["title"] = blockValue.get("title")
            if block.get("has_children") and block.get("id"):
                childBlocks = self.FetchTopLevelBlocks(str(block["id"]), maxBlocks=maxChildBlocks)
                sampleBlock["children"] = [
                    {
                        "id": child.get("id"),
                        "type": child.get("type"),
                        "markdown": BlockToMarkdown(child, depth=1),
                    }
                    for child in childBlocks
                ]
            sampleBlocks.append(sampleBlock)
        return {
            "pageTitle": PageTitleFromPageData(pageData),
            "sampledTopLevelBlockCount": len(sampleBlocks),
            "format": "markdown",
            "formatBlocks": sampleBlocks,
            "markdownOutline": "\n".join(BlockToMarkdown(block) for block in rawBlocks if BlockToMarkdown(block)),
        }

    def _NormalizeAppendPosition(self, position: dict[str, Any] | None) -> dict[str, Any] | None:
        if not position:
            return None

        normalizedPosition = json.loads(json.dumps(position))
        if normalizedPosition.get("type") == "after_block":
            afterBlock = normalizedPosition.get("after_block")
            if isinstance(afterBlock, dict) and afterBlock.get("id"):
                afterBlock["id"] = self.NormalizeId(str(afterBlock["id"]))
        return normalizedPosition

    def AppendBlocks(
        self,
        blockId: str,
        blocks: list[dict[str, Any]],
        position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not blocks:
            return {"results": []}

        results: list[dict[str, Any]] = []
        currentPosition = self._NormalizeAppendPosition(position)
        for index in range(0, len(blocks), 100):
            payload: dict[str, Any] = {"children": blocks[index : index + 100]}
            if currentPosition:
                payload["position"] = currentPosition
            data = self.Request("PATCH", f"/blocks/{self.NormalizeId(blockId)}/children", payload)
            createdBlocks = data.get("results", [])
            results.extend(createdBlocks)

            # When inserting in batches, anchor the next batch after the last
            # block returned by Notion so the caller's original block order is
            # preserved for start/after_block/end positions.
            if currentPosition and createdBlocks:
                lastBlockId = str(createdBlocks[-1].get("id") or "")
                if lastBlockId:
                    currentPosition = {
                        "type": "after_block",
                        "after_block": {"id": self.NormalizeId(lastBlockId)},
                    }
        return {"results": results}

    @staticmethod
    def _TitleRichText(content: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": {"content": content}}]

    def CreateChildPage(
        self,
        parentPageId: str,
        title: str,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": self.NormalizeId(parentPageId)},
            "properties": {"title": self._TitleRichText(title)},
        }
        if children:
            payload["children"] = children[:100]
        return self.Request("POST", "/pages", payload)

    def CreateChildDatabase(
        self,
        parentPageId: str,
        title: str,
        titleProperty: str = "Name",
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalizedProperties = dict(properties or {})
        normalizedProperties.setdefault(titleProperty, {"title": {}})

        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": self.NormalizeId(parentPageId)},
            "title": self._TitleRichText(title),
            "is_inline": True,
            "properties": normalizedProperties,
        }
        return self.Request("POST", "/databases", payload)

    def CreateDatabasePage(
        self,
        databaseId: str,
        properties: dict[str, Any],
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "database_id", "database_id": self.NormalizeId(databaseId)},
            "properties": properties,
        }
        if children:
            payload["children"] = children[:100]
        return self.Request("POST", "/pages", payload)
