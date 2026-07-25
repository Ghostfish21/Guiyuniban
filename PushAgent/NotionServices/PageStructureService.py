from __future__ import annotations

from typing import Any

from .NotionClient import NotionClient


class PageStructureService:
    """Notion 页面/数据库结构操作服务。

    只支持本项目的 NotionClient HTTP wrapper。不要在这里兼容官方 SDK
    或其它假设性 client；项目实际入口统一走 NotionClient。
    """

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def CreateChildPage(
        self,
        parentPageId: str,
        title: str,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """在指定 page 下创建 child_page。"""
        return self.notionClient.CreateChildPage(
            parentPageId=parentPageId,
            title=title,
            children=children,
        )

    def CreateChildDatabase(
        self,
        parentPageId: str,
        title: str,
        titleProperty: str = "Name",
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """在指定 page 下创建 inline child_database。"""
        return self.notionClient.CreateChildDatabase(
            parentPageId=parentPageId,
            title=title,
            titleProperty=titleProperty,
            properties=properties,
        )
