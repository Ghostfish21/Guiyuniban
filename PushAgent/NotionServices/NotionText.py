from __future__ import annotations

from typing import Any
import re

MAX_RICH_TEXT_CHARS = 1900
MARKDOWN_ESCAPE_PATTERN = re.compile(r"([\\\\`*_{}\\[\\]()#+\\-.!|>])")


def VisibleTextFromRichText(richText: list[dict[str, Any]] | None) -> str:
    """Return the user-visible text without Markdown annotation wrappers."""
    if not richText:
        return ""
    return "".join(str(part.get("plain_text") or part.get("text", {}).get("content") or "") for part in richText)


def EscapeMarkdownText(content: str) -> str:
    if not content:
        return ""
    return MARKDOWN_ESCAPE_PATTERN.sub(r"\\\1", content)


def EscapeMarkdownUrl(url: str) -> str:
    """Keep Markdown links valid when a URL contains parentheses or spaces."""
    return (url or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29")

def LinkUrlFromRichTextPart(part: dict[str, Any]) -> str:
    href = part.get("href")
    if href:
        return str(href)

    textValue = part.get("text") if isinstance(part.get("text"), dict) else {}
    linkValue = textValue.get("link") if isinstance(textValue.get("link"), dict) else {}
    return str(linkValue.get("url") or "")

def MarkdownFromRichText(richText: list[dict[str, Any]] | None, escapeMarkdown: bool = True) -> str:
    """
    Convert Notion rich_text into Markdown-like text.

    Preserved information includes bold, italic, strikethrough, underline, code,
    rich-text links, equations, and visible mention labels.
    """
    if not richText:
        return ""

    markdownParts: list[str] = []
    for part in richText:
        partType = str(part.get("type") or "text")
        visibleText = str(part.get("plain_text") or "")

        if partType == "equation":
            expression = str(part.get("equation", {}).get("expression") or visibleText)
            content = f"${expression}$" if expression else ""
        elif partType == "mention":
            content = EscapeMarkdownText(visibleText) if escapeMarkdown else visibleText
        else:
            textValue = part.get("text") if isinstance(part.get("text"), dict) else {}
            content = str(textValue.get("content") or visibleText)
            content = EscapeMarkdownText(content) if escapeMarkdown else content

        annotations = part.get("annotations") if isinstance(part.get("annotations"), dict) else {}
        if content:
            if annotations.get("code"):
                safeCode = content.replace("`", "\\`")
                content = f"`{safeCode}`"
            else:
                if annotations.get("bold"):
                    content = f"**{content}**"
                if annotations.get("italic"):
                    content = f"*{content}*"
                if annotations.get("strikethrough"):
                    content = f"~~{content}~~"
                if annotations.get("underline"):
                    content = f"<u>{content}</u>"

        href = LinkUrlFromRichTextPart(part)
        if href and content:
            content = f"[{content}]({EscapeMarkdownUrl(href)})"

        markdownParts.append(content)

    return "".join(markdownParts)

def SplitRichTextContent(content: str, chunkSize: int = MAX_RICH_TEXT_CHARS) -> list[str]:
    if not content:
        return [""]
    return [content[index : index + chunkSize] for index in range(0, len(content), chunkSize)]


def TextToRichText(content: str, annotations: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    safeAnnotations = annotations or {}
    return [
        {
            "type": "text",
            "text": {"content": part},
            "annotations": safeAnnotations,
        }
        for part in SplitRichTextContent(content)
    ]


def BlockToMarkdown(
    block: dict[str, Any],
    depth: int = 0,
    listNumber: int = 1,
    expandDirectPage: bool = True,
) -> str:
    blockType = str(block.get("type") or "")
    if not blockType:
        return ""

    blockValue = block.get(blockType) or {}
    if not isinstance(blockValue, dict):
        blockValue = {}

    richText = blockValue.get("rich_text") if isinstance(blockValue.get("rich_text"), list) else []
    text = MarkdownFromRichText(richText)
    indent = "  " * max(depth, 0)

    if blockType == "paragraph":
        return f"{indent}{text}" if text else ""
    if blockType == "heading_1":
        return f"# {text}"
    if blockType == "heading_2":
        return f"## {text}"
    if blockType == "heading_3":
        return f"### {text}"
    if blockType == "bulleted_list_item":
        return f"{indent}- {text}"
    if blockType == "numbered_list_item":
        return f"{indent}{listNumber}. {text}"
    if blockType == "to_do":
        checked = "x" if blockValue.get("checked") else " "
        return f"{indent}- [{checked}] {text}"
    if blockType == "toggle":
        return f"{indent}<details>\n{indent}<summary>{text}</summary>"
    if blockType == "quote":
        return f"{indent}> {text}"
    if blockType == "callout":
        icon = blockValue.get("icon") if isinstance(blockValue.get("icon"), dict) else {}
        emoji = str(icon.get("emoji") or "")
        label = f"{emoji} " if emoji else ""
        return f"{indent}> {label}{text}".rstrip()
    if blockType == "code":
        language = str(blockValue.get("language") or "")
        codeText = VisibleTextFromRichText(richText)
        return f"{indent}```{language}\n{codeText}\n{indent}```"
    if blockType == "equation":
        expression = str(blockValue.get("expression") or "")
        return f"{indent}$$\n{expression}\n{indent}$$" if expression else ""
    if blockType == "divider":
        return f"{indent}---"
    if blockType == "table_of_contents":
        return f"{indent}[TOC]"
    if blockType == "breadcrumb":
        return f"{indent}[Breadcrumb]"
    if blockType == "child_page":
        title = EscapeMarkdownText(str(blockValue.get("title") or "Untitled"))
        if expandDirectPage:
            return f"{indent}# {title}"
        pageId = str(block.get("id") or "")
        notionLink = f" [Notion link: {pageId}]" if pageId else ""
        return f"{indent}# {title}{notionLink}"
    if blockType == "child_database":
        title = EscapeMarkdownText(str(blockValue.get("title") or "Untitled Database"))
        databaseId = str(block.get("id") or blockValue.get("database_id") or "")
        databaseViewId = str(block.get("_notion_database_view_id") or blockValue.get("view_id") or blockValue.get("database_view_id") or "")
        parent = block.get("parent") if isinstance(block.get("parent"), dict) else {}
        pageId = str(parent.get("page_id") or block.get("_notion_parent_page_id") or "")
        idParts = []
        idParts.append(f"[Notion database_id: {databaseId}]" if databaseId else "[Notion database_id: unknown]")
        idParts.append(f"[Notion database_view_id: {databaseViewId}]" if databaseViewId else "[Notion database_view_id: unknown]")
        idParts.append(f"[Notion page_id: {pageId}]" if pageId else "[Notion page_id: unknown]")
        return f"{indent}# {title} " + " ".join(idParts)
    if blockType == "table_row":
        cells = blockValue.get("cells") or []
        markdownCells = [MarkdownFromRichText(cell) for cell in cells if isinstance(cell, list)]
        return f"{indent}| " + " | ".join(markdownCells) + " |" if markdownCells else ""
    if blockType in {"image", "video", "file", "pdf", "audio"}:
        caption = MarkdownFromRichText(blockValue.get("caption") if isinstance(blockValue.get("caption"), list) else [])
        url = ExtractFileUrl(blockValue)
        altText = caption or blockType
        if blockType == "image" and url:
            return f"{indent}![{altText}]({EscapeMarkdownUrl(url)})"
        if url:
            return f"{indent}[{altText}]({EscapeMarkdownUrl(url)})"
        return f"{indent}[{blockType}: {altText}]"
    if blockType in {"bookmark", "embed", "link_preview"}:
        url = str(blockValue.get("url") or "")
        caption = MarkdownFromRichText(blockValue.get("caption") if isinstance(blockValue.get("caption"), list) else [])
        label = caption or url or blockType
        return f"{indent}[{label}]({EscapeMarkdownUrl(url)})" if url else f"{indent}{label}"
    if blockType == "link_to_page":
        pageId = str(blockValue.get("page_id") or "")
        databaseId = str(blockValue.get("database_id") or "")
        targetId = pageId or databaseId
        if not targetId:
            return ""
        fallbackTitle = "Unknown Notion Database" if databaseId else "Unknown Notion Page"
        title = EscapeMarkdownText(str(block.get("_notion_link_title") or fallbackTitle))
        viewId = str(block.get("_notion_database_view_id") or "")
        viewPart = f" [Notion database_view_id: {viewId}]" if databaseId and viewId else ""
        return f"{indent}{title} [Notion link: {targetId}]{viewPart}"

    if text:
        return f"{indent}{text}"
    return ""


def ExtractFileUrl(blockValue: dict[str, Any]) -> str:
    for fieldName in ("external", "file"):
        fieldValue = blockValue.get(fieldName)
        if isinstance(fieldValue, dict) and fieldValue.get("url"):
            return str(fieldValue.get("url"))
    return ""


def PageTitleFromPageData(pageData: dict[str, Any]) -> str:
    properties = pageData.get("properties") or {}
    if isinstance(properties, dict):
        for propertyValue in properties.values():
            if not isinstance(propertyValue, dict):
                continue
            if propertyValue.get("type") == "title":
                title = VisibleTextFromRichText(propertyValue.get("title"))
                if title:
                    return title
    return str(pageData.get("id") or "Untitled")


def DatabaseTitleFromDatabaseData(databaseData: dict[str, Any]) -> str:
    title = VisibleTextFromRichText(databaseData.get("title") if isinstance(databaseData.get("title"), list) else [])
    if title:
        return title
    return str(databaseData.get("id") or "Untitled Database")


def MarkdownLineToBlock(line: str) -> dict[str, Any]:
    strippedLine = line.rstrip()
    if strippedLine.startswith("### "):
        return {"type": "heading_3", "heading_3": {"rich_text": TextToRichText(strippedLine[4:])}}
    if strippedLine.startswith("## "):
        return {"type": "heading_2", "heading_2": {"rich_text": TextToRichText(strippedLine[3:])}}
    if strippedLine.startswith("# "):
        return {"type": "heading_1", "heading_1": {"rich_text": TextToRichText(strippedLine[2:])}}
    if strippedLine.startswith("- [ ] "):
        return {"type": "to_do", "to_do": {"rich_text": TextToRichText(strippedLine[6:]), "checked": False}}
    if strippedLine.startswith("- [x] ") or strippedLine.startswith("- [X] "):
        return {"type": "to_do", "to_do": {"rich_text": TextToRichText(strippedLine[6:]), "checked": True}}
    if strippedLine.startswith("- "):
        return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": TextToRichText(strippedLine[2:])}}
    if strippedLine.startswith("> "):
        return {"type": "quote", "quote": {"rich_text": TextToRichText(strippedLine[2:])}}
    return {"type": "paragraph", "paragraph": {"rich_text": TextToRichText(strippedLine)}}


def MarkdownToBlocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for rawLine in markdown.splitlines():
        if not rawLine.strip():
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": []}})
            continue
        blocks.append(MarkdownLineToBlock(rawLine))
    return blocks or [{"type": "paragraph", "paragraph": {"rich_text": []}}]
