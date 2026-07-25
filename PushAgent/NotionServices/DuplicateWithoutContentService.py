from __future__ import annotations

from copy import deepcopy
from typing import Any

from .NotionClient import NotionClient
from .NotionText import DatabaseTitleFromDatabaseData


class DuplicateWithoutContentService:
    """Duplicate a Notion database page/view without copying rows/content.

    Important: this service intentionally does NOT create an inline child_database
    inside the current page. It creates a full-page database under the target
    parent page and then applies/copies the source database view configuration
    using Notion's Views API when a database view id is available.
    """

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def DuplicateDatabase(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
    ) -> dict[str, Any]:
        sourceDatabase = self.notionClient.GetDatabase(databaseId)
        targetParentPageId = self.ResolveTargetParentPageId(sourceDatabase, parentPageId)
        targetTitle = (newTitle or self.DefaultDuplicatedDatabaseTitle(sourceDatabase)).strip()
        properties = self.CloneDatabaseProperties(sourceDatabase)

        createdDatabase = self.notionClient.CreateDatabase(
            parentPageId=targetParentPageId,
            title=targetTitle,
            properties=properties,
            isInline=False,
        )
        createdDatabaseId = str(createdDatabase.get("id") or "")
        if not createdDatabaseId:
            raise RuntimeError("创建 full-page database 成功但 Notion 未返回新 database id。")

        sourceViewId = (databaseViewId or self.notionClient.ResolveDatabaseViewId(databaseId) or "").strip()
        viewResult: dict[str, Any] = {}
        viewError = ""
        if sourceViewId:
            try:
                viewResult = self.CopyOrApplyView(
                    sourceViewId=sourceViewId,
                    targetDatabaseId=createdDatabaseId,
                    fallbackViewName=targetTitle,
                )
            except Exception as exc:
                # The database has already been created correctly. Surface the
                # view-copy issue for debugging without falling back to inline DB.
                viewError = str(exc)

        return {
            "id": createdDatabaseId,
            "database_id": createdDatabaseId,
            "database": createdDatabase,
            "view": viewResult,
            "viewCopyError": viewError,
            "sourceDatabaseId": self.notionClient.NormalizeId(databaseId),
            "sourceDatabaseViewId": self.notionClient.NormalizeId(sourceViewId) if sourceViewId else "",
            "duplicatedWithoutContent": True,
            "createdAsInlineDatabase": False,
        }

    def DuplicateDatabaseWithoutContent(
        self,
        databaseId: str,
        databaseViewId: str | None = None,
        parentPageId: str | None = None,
        newTitle: str | None = None,
    ) -> dict[str, Any]:
        return self.DuplicateDatabase(
            databaseId=databaseId,
            databaseViewId=databaseViewId,
            parentPageId=parentPageId,
            newTitle=newTitle,
        )

    def CopyOrApplyView(
        self,
        sourceViewId: str,
        targetDatabaseId: str,
        fallbackViewName: str,
    ) -> dict[str, Any]:
        sourceView = self.notionClient.GetView(sourceViewId)
        targetDefaultViewId = self.notionClient.FirstDatabaseViewId(targetDatabaseId)
        viewPayload = self.BuildViewPayload(sourceView, fallbackViewName=fallbackViewName)

        # Newly created databases already have one default table view. When the
        # layout type matches, update that view instead of leaving an extra view.
        if targetDefaultViewId:
            try:
                targetDefaultView = self.notionClient.GetView(targetDefaultViewId)
                if str(targetDefaultView.get("type") or "") == str(sourceView.get("type") or ""):
                    updatePayload = self.BuildViewUpdatePayload(sourceView, fallbackViewName=fallbackViewName)
                    if updatePayload:
                        return self.notionClient.UpdateView(targetDefaultViewId, updatePayload)
            except Exception:
                pass

        targetDataSourceId = self.notionClient.PrimaryDatabaseDataSourceId(targetDatabaseId)
        if not targetDataSourceId:
            raise RuntimeError("已创建 full-page database，但无法解析新 database 的 data_source_id，不能复制 view 配置。")

        createPayload = {
            "database_id": self.notionClient.NormalizeId(targetDatabaseId),
            "data_source_id": self.notionClient.NormalizeId(targetDataSourceId),
            **viewPayload,
        }
        return self.notionClient.CreateView(createPayload)

    def BuildViewPayload(self, sourceView: dict[str, Any], fallbackViewName: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": str(sourceView.get("name") or fallbackViewName or "Default view"),
            "type": str(sourceView.get("type") or "table"),
        }
        for key in ("filter", "sorts", "quick_filters", "configuration"):
            if key in sourceView and sourceView.get(key) is not None:
                payload[key] = deepcopy(sourceView[key])
        return payload

    def BuildViewUpdatePayload(self, sourceView: dict[str, Any], fallbackViewName: str) -> dict[str, Any]:
        payload = self.BuildViewPayload(sourceView, fallbackViewName=fallbackViewName)
        # Notion may not allow changing a view type via update; type equality is
        # checked before calling this, but do not send it on PATCH.
        payload.pop("type", None)
        return payload

    def CloneDatabaseProperties(self, databaseData: dict[str, Any]) -> dict[str, Any]:
        sourceProperties = databaseData.get("properties") if isinstance(databaseData.get("properties"), dict) else {}
        cloned: dict[str, Any] = {}
        for name, schema in sourceProperties.items():
            if not isinstance(schema, dict):
                continue
            propertyType = str(schema.get("type") or "")
            if not propertyType:
                continue
            propertyConfig = deepcopy(schema.get(propertyType) if isinstance(schema.get(propertyType), dict) else {})
            cloned[str(name)] = {propertyType: self.StripReadOnlySchemaFields(propertyConfig)}
        return cloned

    def StripReadOnlySchemaFields(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.StripReadOnlySchemaFields(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self.StripReadOnlySchemaFields(nestedValue)
                for key, nestedValue in value.items()
                if key not in {"id", "created_time", "last_edited_time"}
            }
        return value

    def ResolveTargetParentPageId(self, databaseData: dict[str, Any], parentPageId: str | None = None) -> str:
        if parentPageId:
            return self.notionClient.NormalizeId(parentPageId)

        parent = databaseData.get("parent") if isinstance(databaseData.get("parent"), dict) else {}
        pageId = str(parent.get("page_id") or "")
        if pageId:
            return self.notionClient.NormalizeId(pageId)

        raise ValueError("无法从 source database 推断目标父页面；请显式提供 parentPageId。")

    def DefaultDuplicatedDatabaseTitle(self, databaseData: dict[str, Any]) -> str:
        sourceTitle = DatabaseTitleFromDatabaseData(databaseData) or "Untitled Database"
        return f"{sourceTitle} Copy"
