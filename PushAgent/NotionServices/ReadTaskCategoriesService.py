from __future__ import annotations

from .NotionClient import NotionClient
from .NotionContext import NotionContext


class ReadTaskCategoriesService:
    """Main feature: read the Notion page named “任务分类”."""

    def __init__(self, notionClient: NotionClient, notionContext: NotionContext | None = None) -> None:
        self.notionClient = notionClient
        self.notionContext = notionContext or NotionContext()

    def ReadTaskCategories(self, pageTitle: str = "任务分类", maxDepth: int = 3) -> str:
        pageId = self.notionContext.GetValue("NOTION_TASK_CATEGORY_PAGE_ID", "notion_task_category_page_id")
        if not pageId:
            pageId = self.notionContext.GetValue("NOTION_CATEGORY_PAGE_ID", "notion_category_page_id")
        if not pageId:
            pageId = self.notionClient.SearchPage(pageTitle)
        if not pageId:
            raise RuntimeError(
                f"未找到 Notion 页面“{pageTitle}”。请设置 NOTION_TASK_CATEGORY_PAGE_ID 或 notion_task_category_page_id。"
            )

        lines = self.notionClient.FetchBlockChildrenMarkdown(pageId, maxDepth=maxDepth)
        categoryText = "\n".join(line for line in lines if line.strip()).strip()
        if not categoryText:
            raise RuntimeError(f"Notion 页面“{pageTitle}”内容为空。")
        return categoryText
