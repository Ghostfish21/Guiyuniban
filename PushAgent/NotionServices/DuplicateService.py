from __future__ import annotations

from copy import deepcopy
from typing import Any

from .NotionClient import NotionClient


class DuplicateService:
    """Duplicate non-database Notion blocks.

    This service intentionally rejects child_database/database blocks. Use
    DuplicateWithContentService or DuplicateWithoutContentService for database
    duplication.
    """

    DATABASE_BLOCK_TYPES = {"child_database"}
    TOP_LEVEL_READONLY_KEYS = {
        "object",
        "id",
        "parent",
        "created_time",
        "created_by",
        "last_edited_time",
        "last_edited_by",
        "archived",
        "in_trash",
        "has_children",
    }

    def __init__(self, notionClient: NotionClient) -> None:
        self.notionClient = notionClient

    def DuplicateBlock(
        self,
        blockId: str,
        targetParentId: str | None = None,
        newTitle: str | None = None,
        includeChildren: bool = True,
        position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Duplicate one non-database block into ``targetParentId``.

        If ``targetParentId`` is omitted, the copy is inserted after the source
        block under the source block's current parent.
        """
        sourceBlock = self.notionClient.GetBlock(blockId)
        blockType = self.BlockType(sourceBlock)
        self.EnsureNonDatabaseBlock(blockType, blockId)

        resolvedParentId = self.ResolveTargetParentId(sourceBlock, targetParentId)
        resolvedPosition = position
        if targetParentId is None and position is None:
            resolvedPosition = {"type": "after_block", "after_block": {"id": self.notionClient.NormalizeId(blockId)}}

        payload = self.CloneBlockPayload(sourceBlock, newTitle=newTitle)
        appendResult = self.notionClient.AppendBlocks(resolvedParentId, [payload], position=resolvedPosition)
        createdBlocks = appendResult.get("results", [])
        if not createdBlocks:
            raise RuntimeError(f"复制 block 失败，Notion 未返回新 block：{blockId}")

        createdBlock = createdBlocks[0]
        createdBlockId = str(createdBlock.get("id") or "")
        if includeChildren and createdBlockId and sourceBlock.get("has_children"):
            childrenResult = self.DuplicateChildren(
                sourceBlockId=blockId,
                targetParentId=createdBlockId,
            )
            createdBlock["duplicatedChildren"] = childrenResult.get("results", [])
        return createdBlock

    def DuplicateChildren(
        self,
        sourceBlockId: str,
        targetParentId: str,
    ) -> dict[str, Any]:
        """Duplicate all non-database children from one block/page into another."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self.notionClient.GetBlockChildren(sourceBlockId, startCursor=cursor)
            for child in data.get("results", []):
                childId = str(child.get("id") or "")
                childType = self.BlockType(child)
                self.EnsureNonDatabaseBlock(childType, childId)
                results.append(
                    self.DuplicateBlock(
                        blockId=childId,
                        targetParentId=targetParentId,
                        includeChildren=True,
                    )
                )
            cursor = str(data.get("next_cursor") or "") if data.get("has_more") else ""
            if not cursor:
                break
        return {"results": results}

    def CloneBlockPayload(self, block: dict[str, Any], newTitle: str | None = None) -> dict[str, Any]:
        blockType = self.BlockType(block)
        self.EnsureNonDatabaseBlock(blockType, str(block.get("id") or ""))

        blockValue = deepcopy(block.get(blockType) if isinstance(block.get(blockType), dict) else {})
        if blockType == "child_page" and newTitle:
            blockValue["title"] = newTitle

        return {
            "type": blockType,
            blockType: blockValue,
        }

    def ResolveTargetParentId(self, sourceBlock: dict[str, Any], targetParentId: str | None = None) -> str:
        if targetParentId:
            return self.notionClient.NormalizeId(targetParentId)

        parent = sourceBlock.get("parent") if isinstance(sourceBlock.get("parent"), dict) else {}
        for key in ("block_id", "page_id"):
            value = parent.get(key)
            if value:
                return self.notionClient.NormalizeId(str(value))

        raise ValueError("无法从 source block 推断目标父级；请显式提供 targetParentId。")

    def BlockType(self, block: dict[str, Any]) -> str:
        return str(block.get("type") or "")

    def EnsureNonDatabaseBlock(self, blockType: str, blockId: str = "") -> None:
        if blockType in self.DATABASE_BLOCK_TYPES:
            suffix = f"：{blockId}" if blockId else ""
            raise ValueError(f"DuplicateService 只支持复制非 database 块；database 块请使用 DuplicateWithContentService 或 DuplicateWithoutContentService{suffix}")
        if not blockType:
            raise ValueError(f"无法识别 block 类型，不能复制：{blockId}")
