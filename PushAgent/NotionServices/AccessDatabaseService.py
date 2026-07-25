from __future__ import annotations

from typing import Any, Callable
import re
import urllib.parse

from .NotionClient import NotionClient
from .NotionText import (
    EscapeMarkdownText,
    EscapeMarkdownUrl,
    MarkdownFromRichText,
    PageTitleFromPageData,
    VisibleTextFromRichText,
)


class AccessDatabaseService:
    DATABASE_PLACEMENT_FULL_PAGE = "full_page_database"
    DATABASE_PLACEMENT_INLINE = "inline_database"
    DATABASE_PLACEMENT_UNKNOWN = "unknown_database_placement"

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def AccessDatabase(
        self,
        databaseId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        databaseId, databaseData = self.ResolveDatabase(databaseId, title)
        return self.BuildDatabaseAccessResult(databaseId, databaseData, maxDepth, expandDirectPage)

    def AccessDatabaseAsMarkdown(
        self,
        databaseId: str | None = None,
        title: str | None = None,
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> str:
        return str(
            self.AccessDatabase(
                databaseId=databaseId,
                title=title,
                maxDepth=maxDepth,
                expandDirectPage=expandDirectPage,
            )["markdown"]
        )

    def AccessDatabaseFormatSample(self, databaseId: str, maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        databaseId = self.NormalizeInputId(databaseId)
        return self.BuildDatabaseFormatSample(databaseId, self.notionClient.GetDatabase(databaseId), maxTopLevelBlocks)

    def ResolveDatabase(self, databaseId: str | None = None, title: str | None = None) -> tuple[str, dict[str, Any]]:
        resolvedId = self.NormalizeInputId(databaseId or "")
        databaseData = self.SearchFirstDatabase(title) if not resolvedId and title else None
        if databaseData:
            resolvedId = self.NormalizeInputId(str(databaseData.get("id") or ""))
        if not resolvedId:
            raise RuntimeError("请提供 databaseId，或提供能被 Notion Search 找到的 title。")
        if databaseData is None or not databaseData.get("properties"):
            databaseData = self.notionClient.GetDatabase(resolvedId)
        return resolvedId, databaseData

    def BuildDatabaseAccessResult(
        self,
        databaseId: str,
        databaseData: dict[str, Any],
        maxDepth: int = 3,
        expandDirectPage: bool = False,
    ) -> dict[str, Any]:
        if not databaseData.get("properties"):
            databaseData = self.notionClient.GetDatabase(databaseId)
        rows = self.FetchAllDatabaseRows(databaseId)
        databaseViewId = self.notionClient.ResolveDatabaseViewId(databaseId)
        markdownLines = self.BuildDatabaseMarkdownLines(databaseData, rows, maxDepth, expandDirectPage, databaseId=databaseId, databaseViewId=databaseViewId)
        return {
            "pageId": databaseId,
            "databaseId": databaseId,
            "databaseViewId": databaseViewId,
            "objectType": "database",
            "objectTypeLabel": "child_database",
            "databasePlacement": self.DatabasePlacement(databaseData),
            "title": self.DatabaseTitleFromData(databaseData),
            "rowCount": len(rows),
            "format": "markdown",
            "expandDirectPage": expandDirectPage,
            "markdownLines": markdownLines,
            "markdown": "\n".join(markdownLines),
        }

    def BuildDatabaseFormatSample(self, databaseId: str, databaseData: dict[str, Any], maxTopLevelBlocks: int = 40) -> dict[str, Any]:
        rows = self.FetchAllDatabaseRows(databaseId)
        databaseViewId = self.notionClient.ResolveDatabaseViewId(databaseId)
        return {
            "pageId": databaseId,
            "databaseId": databaseId,
            "databaseViewId": databaseViewId,
            "objectType": "database",
            "objectTypeLabel": "child_database",
            "databasePlacement": self.DatabasePlacement(databaseData),
            "title": self.DatabaseTitleFromData(databaseData),
            "sampledRowCount": min(len(rows), maxTopLevelBlocks),
            "format": "markdown",
            "formatBlocks": [self.SimplifyDatabaseRow(row) for row in rows[:maxTopLevelBlocks]],
        }

    def SearchFirstDatabase(self, title: str | None) -> dict[str, Any] | None:
        if not title:
            return None
        candidates = [
            item for item in self.notionClient.Search(title, pageSize=10).get("results", [])
            if item.get("object") == "database" and item.get("id")
        ]
        normalizedTitle = title.strip().casefold()
        return next((item for item in candidates if self.DatabaseTitleFromData(item).strip().casefold() == normalizedTitle), None) or (
            candidates[0] if candidates else None
        )

    def FetchAllDatabaseRows(self, databaseId: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self.notionClient.QueryDatabase(databaseId, startCursor=cursor)
            rows.extend(data.get("results", []))
            cursor = str(data.get("next_cursor") or "") if data.get("has_more") else ""
            if not cursor:
                return rows

    def BuildDatabaseMarkdownLines(
        self,
        databaseData: dict[str, Any],
        rows: list[dict[str, Any]],
        maxDepth: int,
        expandDirectPage: bool = False,
        databaseId: str | None = None,
        databaseViewId: str | None = None,
    ) -> list[str]:
        idParts: list[str] = []
        resolvedDatabaseId = str(databaseId or databaseData.get("id") or "")
        if resolvedDatabaseId:
            idParts.append(f"[Notion database_id: {resolvedDatabaseId}]")
        if databaseViewId:
            idParts.append(f"[Notion database_view_id: {databaseViewId}]")
        titleSuffix = " " + " ".join(idParts) if idParts else ""
        lines = [
            "> 注释：Notion 页类型 - child_database",
            "",
            f"# {EscapeMarkdownText(self.DatabaseTitleFromData(databaseData))}{titleSuffix}",
            "",
            *self.BuildDatabaseSchemaLines(databaseData),
            "",
            "## Rows",
        ]
        if not rows:
            return [*lines, "_数据库暂无记录。_"]
        for index, row in enumerate(rows, 1):
            lines += self.BuildDatabaseRowLines(index, row, maxDepth, expandDirectPage)
        return lines

    def BuildDatabaseSchemaLines(self, databaseData: dict[str, Any]) -> list[str]:
        properties = databaseData.get("properties") if isinstance(databaseData.get("properties"), dict) else {}
        if not properties:
            return ["## Properties", "_未读取到 database properties。_"]
        return ["## Properties", *[f"- {EscapeMarkdownText(str(name))}: `{self.PropertyType(schema)}`" for name, schema in properties.items()]]

    def BuildDatabaseRowLines(self, index: int, row: dict[str, Any], maxDepth: int, expandDirectPage: bool = False) -> list[str]:
        lines = [f"### {index}. {EscapeMarkdownText(PageTitleFromPageData(row) or f'Row {index}')}"]
        lines += self.BuildDatabaseRowPropertyLines(row) or ["- _无可读 properties。_"]
        rowId = str(row.get("id") or "")
        if rowId and maxDepth > 0:
            childLines = self.notionClient.FetchBlockChildrenMarkdown(rowId, maxDepth=maxDepth - 1, expandDirectPage=expandDirectPage)
            if childLines:
                lines += ["#### Page Content", *childLines]
        return [*lines, ""]

    def BuildDatabaseRowPropertyLines(self, row: dict[str, Any]) -> list[str]:
        properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        return [
            f"- **{EscapeMarkdownText(str(name))}**: {rendered}"
            for name, value in properties.items()
            if (rendered := self.PropertyValueToMarkdown(value))
        ]

    def PropertyValueToMarkdown(self, propertyValue: Any) -> str:
        if not isinstance(propertyValue, dict):
            return ""
        propertyType = str(propertyValue.get("type") or "")
        value = propertyValue.get(propertyType)

        if propertyType in {"title", "rich_text"}:
            return MarkdownFromRichText(value if isinstance(value, list) else [])
        if propertyType == "number":
            return "" if value is None else str(value)
        if propertyType == "checkbox":
            return "true" if value else "false"
        if propertyType in {"select", "status"}:
            return self.NameToMarkdown(value)
        if propertyType == "multi_select":
            return self.JoinNames(value)
        if propertyType == "date":
            return self.DateValueToMarkdown(value)
        if propertyType in {"url", "email", "phone_number", "created_time", "last_edited_time"}:
            return EscapeMarkdownText(str(value or ""))
        if propertyType == "files":
            return self.FilesToMarkdown(value)
        if propertyType == "people":
            return self.JoinNames(value, fallbackKey="id")
        if propertyType in {"created_by", "last_edited_by"}:
            return self.NameToMarkdown(value, fallbackKey="id")
        if propertyType == "relation":
            return ", ".join(str(item.get("id") or "") for item in value or [] if isinstance(item, dict) and item.get("id"))
        if propertyType in {"formula", "rollup"}:
            return self.ComputedPropertyToMarkdown(value)
        if propertyType == "unique_id":
            return self.UniqueIdToMarkdown(value)
        if propertyType == "verification":
            return self.NameToMarkdown(value, nameKey="state")
        return EscapeMarkdownText(str(value or "")) if propertyType else ""

    def ComputedPropertyToMarkdown(self, value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        valueType = str(value.get("type") or "")
        nestedValue = value.get(valueType)
        if valueType == "date":
            return self.DateValueToMarkdown(nestedValue)
        if valueType in {"string", "number", "boolean"}:
            return EscapeMarkdownText(str(nestedValue)) if nestedValue is not None else ""
        if valueType == "array":
            return ", ".join(filter(None, (self.PropertyValueToMarkdown(item) for item in nestedValue or [] if isinstance(item, dict))))
        return EscapeMarkdownText(str(nestedValue or "")) if valueType else ""

    def DateValueToMarkdown(self, dateValue: Any) -> str:
        if not isinstance(dateValue, dict):
            return ""
        start, end = str(dateValue.get("start") or ""), str(dateValue.get("end") or "")
        return f"{start} → {end}" if end else start

    def FilesToMarkdown(self, files: Any) -> str:
        rendered: list[str] = []
        for fileValue in files or []:
            if not isinstance(fileValue, dict):
                continue
            name, url = str(fileValue.get("name") or "file"), self.FileUrlFromNotionFile(fileValue)
            rendered.append(f"[{EscapeMarkdownText(name)}]({EscapeMarkdownUrl(url)})" if url else EscapeMarkdownText(name))
        return ", ".join(rendered)

    def JoinNames(self, items: Any, fallbackKey: str = "") -> str:
        return ", ".join(
            self.NameToMarkdown(item, fallbackKey=fallbackKey)
            for item in items or []
            if isinstance(item, dict) and self.NameToMarkdown(item, fallbackKey=fallbackKey)
        )

    def NameToMarkdown(self, value: Any, nameKey: str = "name", fallbackKey: str = "") -> str:
        return EscapeMarkdownText(str(value.get(nameKey) or value.get(fallbackKey) or "")) if isinstance(value, dict) else ""

    def UniqueIdToMarkdown(self, uniqueId: Any) -> str:
        if not isinstance(uniqueId, dict) or uniqueId.get("number") is None:
            return ""
        prefix = str(uniqueId.get("prefix") or "")
        return f"{prefix}-{uniqueId['number']}" if prefix else str(uniqueId["number"])

    def DatabasePlacement(self, databaseData: dict[str, Any]) -> str:
        return {
            True: self.DATABASE_PLACEMENT_INLINE,
            False: self.DATABASE_PLACEMENT_FULL_PAGE,
        }.get(databaseData.get("is_inline"), self.DATABASE_PLACEMENT_UNKNOWN)

    def DatabaseTitleFromData(self, databaseData: dict[str, Any]) -> str:
        titleParts = databaseData.get("title") if isinstance(databaseData.get("title"), list) else []
        return VisibleTextFromRichText(titleParts) or str(databaseData.get("id") or "Database")

    def FileUrlFromNotionFile(self, fileValue: dict[str, Any]) -> str:
        for key in ("external", "file"):
            nested = fileValue.get(key)
            if isinstance(nested, dict) and nested.get("url"):
                return str(nested["url"])
        return ""

    def SimplifyDatabaseRow(self, row: dict[str, Any]) -> dict[str, Any]:
        properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        return {
            "id": row.get("id"),
            "title": PageTitleFromPageData(row),
            "properties": {name: self.PropertyValueToMarkdown(value) for name, value in properties.items()},
        }

    def PropertyType(self, schema: Any) -> str:
        return str(schema.get("type") or "unknown") if isinstance(schema, dict) else "unknown"

    def NormalizeInputId(self, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = urllib.parse.urlsplit(raw)
        target = parsed.path if parsed.scheme and parsed.netloc else raw
        matches = [match for pattern in self.IdPatterns() for match in re.findall(pattern, target)]
        return self.notionClient.NormalizeId(matches[-1] if matches else raw)

    @staticmethod
    def IdPatterns() -> tuple[str, str]:
        return (
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            r"[0-9a-fA-F]{32}",
        )

    @staticmethod
    def TryNotionCall(func: Callable[[], Any]) -> tuple[Any | None, str]:
        try:
            return func(), ""
        except RuntimeError as exc:
            return None, str(exc)

    @staticmethod
    def CombineErrors(*errors: str) -> str:
        messages = [error for error in errors if error]
        return "" if not messages else " 最近的 Notion API 错误：" + " | ".join(messages[-2:])
