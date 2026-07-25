from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .NotionClient import NotionClient
from .NotionText import MarkdownToBlocks


class WriteDatabaseService:
    """Main feature: write rows/pages into a Notion database."""

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def WriteRowsToDatabase(
        self,
        databaseId: str,
        rows: list[dict[str, Any]],
        titleProperty: str = "Name",
        contentProperty: str | None = None,
    ) -> list[dict[str, Any]]:
        createdPages: list[dict[str, Any]] = []
        for row in rows:
            properties = self.SanitizeDatabaseEntryProperties(row, titleProperty, contentProperty)
            children = []
            if contentProperty and row.get(contentProperty):
                children = MarkdownToBlocks(str(row[contentProperty]))
            createdPages.append(self.notionClient.CreateDatabasePage(databaseId, properties, children=children))
        return createdPages

    def SanitizeDatabaseEntryProperties(
        self,
        row: dict[str, Any],
        titleProperty: str = "Name",
        contentProperty: str | None = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for key, value in row.items():
            if key == contentProperty:
                continue
            if key == titleProperty:
                properties[key] = self.NotionPropertyFromSimpleValue(value, forceTitle=True)
            else:
                properties[key] = self.NotionPropertyFromSimpleValue(value)
        if titleProperty not in properties:
            properties[titleProperty] = self.NotionPropertyFromSimpleValue(row.get("title") or "Untitled", forceTitle=True)
        return properties

    def NotionPropertyFromSimpleValue(self, value: Any, forceTitle: bool = False) -> dict[str, Any]:
        if isinstance(value, dict) and self.IsNotionPropertyValue(value):
            if forceTitle and "title" not in value:
                fallbackText = self.ExtractPlainTextFromNotionPropertyValue(value) or "Untitled"
                return {"title": [{"type": "text", "text": {"content": fallbackText}}]}
            return value
        if forceTitle:
            return {"title": [{"type": "text", "text": {"content": str(value or "Untitled")}}]}
        if isinstance(value, bool):
            return {"checkbox": value}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"number": value}
        if isinstance(value, (datetime, date)):
            return {"date": {"start": value.isoformat()}}
        if isinstance(value, list):
            return {"multi_select": [{"name": str(item)} for item in value]}
        if value is None:
            return {"rich_text": []}
        return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}

    @staticmethod
    def IsNotionPropertyValue(value: dict[str, Any]) -> bool:
        writablePropertyKeys = {
            "title",
            "rich_text",
            "number",
            "checkbox",
            "date",
            "select",
            "multi_select",
            "status",
            "url",
            "email",
            "phone_number",
            "files",
            "people",
            "relation",
        }
        return any(key in value for key in writablePropertyKeys)

    @staticmethod
    def ExtractPlainTextFromNotionPropertyValue(value: dict[str, Any]) -> str:
        for key in ("title", "rich_text"):
            richText = value.get(key)
            if isinstance(richText, list):
                return "".join(
                    str((item.get("text") or {}).get("content") or item.get("plain_text") or "")
                    for item in richText
                    if isinstance(item, dict)
                ).strip()
        for key in ("select", "status"):
            option = value.get(key)
            if isinstance(option, dict) and option.get("name"):
                return str(option["name"]).strip()
        for key in ("url", "email", "phone_number", "number"):
            if value.get(key) is not None:
                return str(value[key]).strip()
        return ""
