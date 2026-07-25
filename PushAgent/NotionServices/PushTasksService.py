from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import re
import uuid

from .WritePageService import WritePageService
from .WriteDatabaseService import WriteDatabaseService


COMMIT_JSON_START = "<!-- guiyuniban_commit_json"
COMMIT_JSON_END = "guiyuniban_commit_json -->"


class PushTasksService:
    """Main feature: push committed task preview data into Notion."""

    def __init__(
        self,
        writePageService: WritePageService,
        writeDatabaseService: WriteDatabaseService | None = None,
    ) -> None:
        self.writePageService = writePageService
        self.writeDatabaseService = writeDatabaseService

    def PushCommitPreview(
        self,
        commitPreviewFile: str,
        targetPageId: str | None = None,
        targetDatabaseId: str | None = None,
        uncommitFile: str | None = None,
        archiveDir: str | None = None,
    ) -> dict[str, Any]:
        previewPath = Path(commitPreviewFile)
        if not previewPath.exists() or not previewPath.read_text(encoding="utf-8").strip():
            raise RuntimeError("没有可 push 的 commit 预览，请先生成 commit_preview.txt。")

        previewText = previewPath.read_text(encoding="utf-8")
        commitPayload = self.ExtractCommitPayload(previewText)
        items = commitPayload.get("items") or []
        if not isinstance(items, list) or not items:
            raise RuntimeError("commit 预览为空，没有可 push 的任务。")

        if targetDatabaseId:
            if not self.writeDatabaseService:
                raise RuntimeError("缺少 WriteDatabaseService，无法写入 database。")
            rows = self.CompactCommitItemsForDatabase(items)
            createdItems = self.writeDatabaseService.WriteRowsToDatabase(targetDatabaseId, rows, titleProperty="任务名")
            targetType = "database"
        elif targetPageId:
            markdown = self.BuildCommitMarkdown(items, str(commitPayload.get("commit_id") or ""))
            createdItems = [self.writePageService.WriteContentToPage(targetPageId, markdown, heading="任务记录", appendDivider=True)]
            targetType = "page"
        else:
            raise RuntimeError("请提供 targetPageId 或 targetDatabaseId。")

        changedRecords = 0
        if uncommitFile:
            changedRecords = self.MarkRecordsCommitted(uncommitFile, commitPayload)

        archiveFile = ""
        if archiveDir:
            archiveFile = self.ArchiveCommitPreview(archiveDir, commitPayload, previewText)
            previewPath.write_text("", encoding="utf-8")

        return {
            "targetType": targetType,
            "pushedItemCount": len(items),
            "changedRecordCount": changedRecords,
            "archiveFile": archiveFile,
            "createdItems": createdItems,
        }

    def ExtractCommitPayload(self, previewText: str) -> dict[str, Any]:
        pattern = re.compile(re.escape(COMMIT_JSON_START) + r"\s*(.*?)\s*" + re.escape(COMMIT_JSON_END), re.DOTALL)
        match = pattern.search(previewText)
        if not match:
            raise RuntimeError("commit 预览格式不完整：未找到机器可读 payload。")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"commit payload 不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("commit payload 应该是 JSON object。")
        return payload

    def BuildCommitMarkdown(self, items: list[dict[str, Any]], commitId: str = "") -> str:
        lines: list[str] = []
        if commitId:
            lines.append(f"> commit_id: {commitId}")
            lines.append("")
        for item in reversed(items):
            taskName = str(item.get("任务名") or item.get("task_name") or item.get("name") or "未命名任务")
            weekday = str(item.get("周几") or item.get("weekday") or "未知")
            hours = str(item.get("持续小时") or item.get("hours") or "")
            category = str(item.get("类别") or item.get("category") or "未分类")
            lines.append(f"- **{taskName}** ｜ {weekday} ｜ {hours}H ｜ {category}")
        return "\n".join(lines)

    def CompactCommitItemsForDatabase(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            rows.append(
                {
                    "任务名": item.get("任务名") or item.get("task_name") or item.get("name") or "未命名任务",
                    "周几": item.get("周几") or item.get("weekday") or "未知",
                    "持续小时": item.get("持续小时") or item.get("hours") or 0,
                    "类别": item.get("类别") or item.get("category") or "未分类",
                }
            )
        return rows

    def MarkRecordsCommitted(self, uncommitFile: str, commitPayload: dict[str, Any]) -> int:
        path = Path(uncommitFile)
        if not path.exists():
            return 0
        sessionIds = {str(item.get("session_id") or "") for item in commitPayload.get("items") or [] if item.get("session_id")}
        if not sessionIds:
            return 0

        records: list[dict[str, Any]] = []
        changed = 0
        committedAt = datetime.now().isoformat(timespec="seconds")
        for rawLine in path.read_text(encoding="utf-8").splitlines():
            if not rawLine.strip():
                continue
            record = json.loads(rawLine)
            if str(record.get("session_id") or "") in sessionIds:
                record["committed"] = True
                record["committed_at"] = committedAt
                record["commit_id"] = commitPayload.get("commit_id") or ""
                record["updated_at"] = committedAt
                changed += 1
            records.append(record)

        path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
        return changed

    def ArchiveCommitPreview(self, archiveDir: str, commitPayload: dict[str, Any], previewText: str) -> str:
        targetDir = Path(archiveDir)
        targetDir.mkdir(parents=True, exist_ok=True)
        commitId = str(commitPayload.get("commit_id") or uuid.uuid4())
        archiveFile = targetDir / f"{commitId}.txt"
        archiveFile.write_text(previewText, encoding="utf-8")
        return str(archiveFile)
