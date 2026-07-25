from __future__ import annotations

from copy import deepcopy
from typing import Any

from .AccessDatabaseService import AccessDatabaseService
from .DuplicateService import DuplicateService
from .DuplicateWithoutContentService import DuplicateWithoutContentService
from .NotionClient import NotionClient


class DuplicateWithContentService:
    """Duplicate a Notion database block/schema and copy its rows/page content."""

    SETTABLE_PROPERTY_TYPES = {
        "title",
        "rich_text",
        "number",
        "select",
        "multi_select",
        "date",
        "people",
        "files",
        "checkbox",
        "url",
        "email",
        "phone_number",
        "status",
        "relation",
    }

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient
        self.databaseAccessService = AccessDatabaseService(notionClient)
        self.duplicateService = DuplicateService(notionClient)
        self.duplicateWithoutContentService = DuplicateWithoutContentService(notionClient)

    def DuplicateDatabase(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
        includePageContent: bool = True,
    ) -> dict[str, Any]:
        sourceDatabase = self.notionClient.GetDatabase(databaseId)
        createdDatabase = self.duplicateWithoutContentService.DuplicateDatabase(
            databaseId=databaseId,
            databaseViewId=databaseViewId,
            parentPageId=parentPageId,
            newTitle=newTitle,
        )
        createdDatabaseObject = createdDatabase.get("database") if isinstance(createdDatabase.get("database"), dict) else createdDatabase
        createdDatabaseId = str(createdDatabase.get("id") or createdDatabaseObject.get("id") or "")
        if not createdDatabaseId:
            raise RuntimeError("复制 database schema 成功但 Notion 未返回新 database id。")

        sourceRows = self.databaseAccessService.FetchAllDatabaseRows(databaseId)
        targetProperties = createdDatabaseObject.get("properties") if isinstance(createdDatabaseObject.get("properties"), dict) else {}
        createdRows: list[dict[str, Any]] = []
        for row in sourceRows:
            rowProperties = self.CloneDatabaseRowProperties(row, targetProperties)
            createdRow = self.notionClient.CreateDatabasePage(createdDatabaseId, rowProperties)
            createdRowId = str(createdRow.get("id") or "")
            if includePageContent and createdRowId:
                self.duplicateService.DuplicateChildren(
                    sourceBlockId=str(row.get("id") or ""),
                    targetParentId=createdRowId,
                )
            createdRows.append(createdRow)

        return {
            "database": createdDatabase,
            "createdRows": createdRows,
            "sourceRowCount": len(sourceRows),
            "createdRowCount": len(createdRows),
        }

    def DuplicateDatabaseWithContent(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
        includePageContent: bool = True,
    ) -> dict[str, Any]:
        return self.DuplicateDatabase(
            databaseId=databaseId,
            databaseViewId=databaseViewId,
            parentPageId=parentPageId,
            newTitle=newTitle,
            includePageContent=includePageContent,
        )

    def CloneDatabaseRowProperties(self, row: dict[str, Any], targetSchema: dict[str, Any]) -> dict[str, Any]:
        sourceProperties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        cloned: dict[str, Any] = {}
        for name, value in sourceProperties.items():
            if name not in targetSchema or not isinstance(value, dict):
                continue
            propertyType = str(value.get("type") or "")
            if propertyType not in self.SETTABLE_PROPERTY_TYPES:
                continue
            if not self.TargetSchemaAcceptsType(targetSchema.get(name), propertyType):
                continue
            clonedValue = self.CloneDatabaseRowPropertyValue(propertyType, value.get(propertyType))
            cloned[name] = {propertyType: clonedValue}
        return cloned

    def CloneDatabaseRowPropertyValue(self, propertyType: str, value: Any) -> Any:
        if propertyType in {"select", "status"}:
            if not isinstance(value, dict) or not value.get("name"):
                return None
            return {"name": str(value["name"])}

        if propertyType == "multi_select":
            return [
                {"name": str(item["name"])}
                for item in value or []
                if isinstance(item, dict) and item.get("name")
            ]

        if propertyType == "files":
            # Notion-hosted file URLs can expire; keep the original structure and
            # let Notion validate it, while preserving external files cleanly.
            return deepcopy(value or [])

        return deepcopy(value)

    def TargetSchemaAcceptsType(self, targetPropertySchema: Any, propertyType: str) -> bool:
        return isinstance(targetPropertySchema, dict) and str(targetPropertySchema.get("type") or "") == propertyType
