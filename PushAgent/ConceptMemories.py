from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import re


PAGE_STRUCTURE_SECTION = "页结构总体描述"
WRITE_TARGET_SECTION = "用户可能希望写入到哪里"
CHILD_PAGE_TYPES_SECTION = "子页类型列表JSON"


@dataclass
class ChildPageTypeEntry:
    name: str
    description: str = ""
    available: bool = True

    @classmethod
    def FromDict(cls, value: dict[str, Any]) -> "ChildPageTypeEntry":
        return cls(
            name=str(value.get("name", "")).strip(),
            description=str(value.get("description", "")).strip(),
            available=bool(value.get("available", True)),
        )

    def ToDict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "description": self.description,
            "name": self.name,
        }


@dataclass
class ConceptMemory:
    conceptName: str
    pageStructureSummary: str = ""
    possibleWriteTarget: str = ""
    childPageTypes: list[ChildPageTypeEntry] = field(default_factory=list)
    filePath: Path | None = None
    exists: bool = False
    rawText: str = ""

    def ValidChildPageTypes(self) -> list[ChildPageTypeEntry]:
        return [
            childPageType
            for childPageType in self.childPageTypes
            if childPageType.available and childPageType.name
        ]

    def GetChildPageType(self, childTypeName: str) -> ChildPageTypeEntry | None:
        try:
            childTypeName = ConceptMemories.SanitizeName(childTypeName)
        except ValueError:
            return None

        for childPageType in self.childPageTypes:
            if childPageType.name == childTypeName:
                return childPageType
        return None

    def UpsertChildPageType(
        self,
        childTypeName: str,
        description: str = "",
        available: bool = True,
    ) -> None:
        childTypeName = ConceptMemories.SanitizeName(childTypeName)

        existing = self.GetChildPageType(childTypeName)
        if existing:
            existing.description = description.strip() or existing.description
            existing.available = available
            return

        self.childPageTypes.append(
            ChildPageTypeEntry(
                name=childTypeName,
                description=description.strip(),
                available=available,
            )
        )

    def ToText(self) -> str:
        childPageTypesJson = json.dumps(
            [childPageType.ToDict() for childPageType in self.childPageTypes],
            ensure_ascii=False,
            indent=2,
        )

        return (
            f"--- {PAGE_STRUCTURE_SECTION} ---\n"
            f"{self.pageStructureSummary.strip()}\n\n"
            f"--- {WRITE_TARGET_SECTION} ---\n"
            f"{self.possibleWriteTarget.strip()}\n\n"
            f"--- {CHILD_PAGE_TYPES_SECTION} ---\n"
            f"{childPageTypesJson}\n"
        )


class ConceptMemories:
    memoryDir: Path = Path("ConceptMemories")
    defaultSuffix: str = ".md"

    @classmethod
    def Configure(
        cls,
        memoryDir: str | Path = "ConceptMemories",
        defaultSuffix: str = ".md",
    ) -> None:
        cls.memoryDir = Path(memoryDir)
        cls.defaultSuffix = defaultSuffix if defaultSuffix.startswith(".") else f".{defaultSuffix}"

    @classmethod
    def Get(cls, ConceptName: str) -> ConceptMemory:
        filePath = cls._ResolvePath(ConceptName)

        if not filePath.exists():
            return ConceptMemory(
                conceptName=cls._NormalizeConceptName(ConceptName),
                filePath=filePath,
                exists=False,
            )

        rawText = filePath.read_text(encoding="utf-8")
        memory = cls._ParseText(
            text=rawText,
            conceptName=cls._NormalizeConceptName(ConceptName),
        )
        memory.filePath = filePath
        memory.exists = True
        memory.rawText = rawText
        return memory

    @classmethod
    def Save(cls, ConceptName: str, memory: ConceptMemory) -> ConceptMemory:
        filePath = cls._ResolvePath(ConceptName, forWrite=True)
        filePath.parent.mkdir(parents=True, exist_ok=True)

        savedMemory = ConceptMemory(
            conceptName=cls._NormalizeConceptName(ConceptName),
            pageStructureSummary=memory.pageStructureSummary,
            possibleWriteTarget=memory.possibleWriteTarget,
            childPageTypes=cls._NormalizeChildPageTypes(list(memory.childPageTypes)),
            filePath=filePath,
            exists=True,
        )

        text = savedMemory.ToText()
        filePath.write_text(text, encoding="utf-8")
        savedMemory.rawText = text
        return savedMemory

    @classmethod
    def Set(
        cls,
        ConceptName: str,
        pageStructureSummary: str,
        possibleWriteTarget: str,
        childPageTypes: list[ChildPageTypeEntry | dict[str, Any]] | None = None,
    ) -> ConceptMemory:
        return cls.Save(
            ConceptName,
            ConceptMemory(
                conceptName=cls._NormalizeConceptName(ConceptName),
                pageStructureSummary=pageStructureSummary,
                possibleWriteTarget=possibleWriteTarget,
                childPageTypes=cls._NormalizeChildPageTypes(childPageTypes or []),
            ),
        )

    @classmethod
    def SaveChild(
        cls,
        ParentConceptName: str,
        ChildTypeName: str,
        childMemory: ConceptMemory,
        childTypeDescription: str = "",
        available: bool = True,
    ) -> ConceptMemory:
        savedChildMemory = cls.Save(ChildTypeName, childMemory)

        parentMemory = cls.Get(ParentConceptName)
        parentMemory.UpsertChildPageType(
            childTypeName=ChildTypeName,
            description=childTypeDescription or childMemory.pageStructureSummary,
            available=available,
        )
        cls.Save(ParentConceptName, parentMemory)

        return savedChildMemory

    @classmethod
    def UpsertChildPageType(
        cls,
        ParentConceptName: str,
        ChildTypeName: str,
        description: str = "",
        available: bool = True,
    ) -> ConceptMemory:
        parentMemory = cls.Get(ParentConceptName)
        parentMemory.UpsertChildPageType(
            childTypeName=ChildTypeName,
            description=description,
            available=available,
        )
        return cls.Save(ParentConceptName, parentMemory)

    @classmethod
    def Exists(cls, ConceptName: str) -> bool:
        return cls._ResolvePath(ConceptName).exists()

    @classmethod
    def Delete(cls, ConceptName: str) -> bool:
        filePath = cls._ResolvePath(ConceptName)
        if not filePath.exists():
            return False

        filePath.unlink()
        return True

    @classmethod
    def _ParseText(cls, text: str, conceptName: str) -> ConceptMemory:
        sections: dict[str, list[str]] = {
            PAGE_STRUCTURE_SECTION: [],
            WRITE_TARGET_SECTION: [],
            CHILD_PAGE_TYPES_SECTION: [],
        }

        currentSection: str | None = None

        for line in text.splitlines():
            markerMatch = re.match(r"^\s*---\s*(.*?)\s*---\s*$", line)

            if markerMatch:
                sectionName = markerMatch.group(1).strip()
                currentSection = sectionName if sectionName in sections else None
                continue

            if currentSection:
                sections[currentSection].append(line)

        return ConceptMemory(
            conceptName=conceptName,
            pageStructureSummary="\n".join(sections[PAGE_STRUCTURE_SECTION]).strip(),
            possibleWriteTarget="\n".join(sections[WRITE_TARGET_SECTION]).strip(),
            childPageTypes=cls._LoadChildPageTypesJson(
                "\n".join(sections[CHILD_PAGE_TYPES_SECTION]).strip()
            ),
        )

    @classmethod
    def _LoadChildPageTypesJson(cls, text: str) -> list[ChildPageTypeEntry]:
        if not text:
            return []

        try:
            rawEntries = json.loads(text)
        except json.JSONDecodeError:
            return []

        if not isinstance(rawEntries, list):
            return []

        return cls._NormalizeChildPageTypes(rawEntries)

    @classmethod
    def _NormalizeChildPageTypes(
        cls,
        values: list[ChildPageTypeEntry | dict[str, Any]],
    ) -> list[ChildPageTypeEntry]:
        result: list[ChildPageTypeEntry] = []
        seenNames: set[str] = set()

        for value in values:
            childPageType = value if isinstance(value, ChildPageTypeEntry) else ChildPageTypeEntry.FromDict(value)

            try:
                childPageType.name = cls.SanitizeName(childPageType.name)
            except ValueError:
                continue

            if childPageType.name in seenNames:
                continue

            seenNames.add(childPageType.name)
            result.append(childPageType)

        return result

    @classmethod
    def _ResolvePath(cls, ConceptName: str, forWrite: bool = False) -> Path:
        conceptName = cls._NormalizeConceptName(ConceptName)
        conceptPath = Path(conceptName)

        if conceptPath.is_absolute() or ".." in conceptPath.parts:
            raise ValueError("ConceptName 只能是经验记忆文件名，不能是绝对路径或包含 '..'。")

        if len(conceptPath.parts) != 1:
            raise ValueError("ConceptName 只能是单个经验记忆文件名，不能包含目录。")

        if conceptPath.suffix:
            return cls.memoryDir / conceptPath

        exactPath = cls.memoryDir / conceptPath
        suffixPath = cls.memoryDir / f"{conceptName}{cls.defaultSuffix}"

        if not forWrite and exactPath.exists():
            return exactPath

        return suffixPath

    @classmethod
    def _NormalizeConceptName(cls, ConceptName: str) -> str:
        return cls.SanitizeName(ConceptName)

    @classmethod
    def SanitizeName(cls, name: str) -> str:
        """
        将外部/LLM 传入的概念名或子页类型名转换为安全的单个文件名。

        处理范围：
        - 路径分隔符：/ 和 \\
        - Windows 非法文件名字符：< > : " | ? *
        - ASCII 控制字符、换行、制表符
        - 绝对路径、..、空名称、Windows 设备保留名

        这里优先使用全角替代字符，而不是直接删除字符，避免中文语义被破坏。
        """
        safeName = str(name or "").strip()

        if not safeName:
            raise ValueError("ConceptName 不能为空。")

        replacementMap = str.maketrans({
            "/": "",
            "\\": "",
            ":": "",
            "*": "",
            "?": "",
            '"': "",
            "<": "",
            ">": "",
            "|": "",
        })
        safeName = safeName.translate(replacementMap)

        # 将所有 ASCII 控制字符统一压成空格，再合并多余空白。
        safeName = re.sub(r"[\x00-\x1f\x7f]+", " ", safeName)
        safeName = re.sub(r"\s+", " ", safeName).strip()

        # Windows 不允许文件名以空格或点结尾；'.' 和 '..' 也不能作为文件名。
        safeName = safeName.rstrip(" .")

        if not safeName or safeName in {".", ".."}:
            raise ValueError("ConceptName 不能为空或仅包含非法字符。")

        conceptPath = Path(safeName)
        if conceptPath.is_absolute() or ".." in conceptPath.parts or len(conceptPath.parts) != 1:
            # 理论上路径分隔符已被替换；保留这层保护，避免不同平台上的边界情况。
            safeName = "＄".join(part for part in conceptPath.parts if part not in {"", ".", ".."})
            safeName = safeName.strip().rstrip(" .")

        stem = Path(safeName).stem
        reservedNames = {"CON", "PRN", "AUX", "NUL"}
        reservedNames.update({f"COM{i}" for i in range(1, 10)})
        reservedNames.update({f"LPT{i}" for i in range(1, 10)})
        if stem.upper() in reservedNames:
            safeName = f"_{safeName}"

        if not safeName:
            raise ValueError("ConceptName 不能为空或仅包含非法字符。")

        return safeName


__all__ = [
    "ChildPageTypeEntry",
    "ConceptMemory",
    "ConceptMemories",
]