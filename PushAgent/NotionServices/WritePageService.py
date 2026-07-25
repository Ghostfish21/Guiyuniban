from __future__ import annotations

from typing import Any

from .NotionClient import NotionClient
from .NotionText import MarkdownToBlocks, TextToRichText


class WritePageService:
    """Main feature: write content into a specific Notion page.

    The optional ``position`` argument is passed through to Notion's
    Append block children API, so callers can write at the start, at the end,
    or after a specific existing block.
    """

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def StartPosition(self) -> dict[str, Any]:
        """Return a Notion position object that inserts blocks at the beginning."""
        return {"type": "start"}

    def EndPosition(self) -> dict[str, Any]:
        """Return a Notion position object that inserts blocks at the end."""
        return {"type": "end"}

    def AfterBlockPosition(self, blockId: str) -> dict[str, Any]:
        """Return a Notion position object that inserts blocks after ``blockId``."""
        return {
            "type": "after_block",
            "after_block": {"id": self.notionClient.NormalizeId(blockId)},
        }

    def WriteContentToPage(
        self,
        pageId: str,
        content: str,
        heading: str | None = None,
        appendDivider: bool = False,
        position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if appendDivider:
            blocks.append({"type": "divider", "divider": {}})
        if heading:
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": TextToRichText(heading)}})
        blocks.extend(MarkdownToBlocks(content))
        return self.notionClient.AppendBlocks(pageId, blocks, position=position)

    def WriteBlocksToPage(
        self,
        pageId: str,
        blocks: list[dict[str, Any]],
        position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.notionClient.AppendBlocks(pageId, blocks, position=position)

    def WriteContentToPageStart(
        self,
        pageId: str,
        content: str,
        heading: str | None = None,
        appendDivider: bool = False,
    ) -> dict[str, Any]:
        return self.WriteContentToPage(
            pageId,
            content,
            heading=heading,
            appendDivider=appendDivider,
            position=self.StartPosition(),
        )

    def WriteContentAfterBlock(
        self,
        pageId: str,
        afterBlockId: str,
        content: str,
        heading: str | None = None,
        appendDivider: bool = False,
    ) -> dict[str, Any]:
        return self.WriteContentToPage(
            pageId,
            content,
            heading=heading,
            appendDivider=appendDivider,
            position=self.AfterBlockPosition(afterBlockId),
        )

    def WriteBlocksToPageStart(self, pageId: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return self.WriteBlocksToPage(pageId, blocks, position=self.StartPosition())

    def WriteBlocksAfterBlock(
        self,
        pageId: str,
        afterBlockId: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.WriteBlocksToPage(pageId, blocks, position=self.AfterBlockPosition(afterBlockId))
