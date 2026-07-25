from __future__ import annotations

from pathlib import Path
from typing import Any
import os


class NotionContext:
    """Loads configuration from environment variables and an optional config file."""

    def __init__(self, configFile: str | None = None, overrides: dict[str, str] | None = None) -> None:
        self.configFile = configFile
        self.config = self.ReadConfig(configFile)
        if overrides:
            self.config.update(overrides)

    def ReadConfig(self, configFile: str | None = None) -> dict[str, str]:
        if not configFile:
            return {}

        configPath = Path(configFile)
        if not configPath.exists():
            return {}

        configValues: dict[str, str] = {}
        for rawLine in configPath.read_text(encoding="utf-8").splitlines():
            line = rawLine.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            configValues[key.strip()] = value.strip().strip('"').strip("'")
        return configValues

    def GetValue(self, envName: str, configName: str | None = None, defaultValue: str = "") -> str:
        configKey = configName or envName.lower()
        return os.getenv(envName) or self.config.get(configKey) or defaultValue

    def GetIntValue(
        self,
        envName: str,
        configName: str | None = None,
        defaultValue: int = 0,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        rawValue = self.GetValue(envName, configName, str(defaultValue))
        try:
            parsedValue = int(rawValue)
        except (TypeError, ValueError):
            parsedValue = defaultValue

        if minimum is not None:
            parsedValue = max(minimum, parsedValue)
        if maximum is not None:
            parsedValue = min(maximum, parsedValue)
        return parsedValue

    def GetNotionToken(self) -> str:
        return (
            os.getenv("NOTION_TOKEN")
            or os.getenv("NOTION_API_KEY")
            or self.config.get("notion_token")
            or self.config.get("notion_api_key")
            or ""
        )

    def GetOpenAiApiKey(self) -> str:
        return os.getenv("OPENAI_API_KEY") or self.config.get("openai_api_key") or ""

    def ToDict(self) -> dict[str, Any]:
        return {
            "configFile": self.configFile,
            "config": dict(self.config),
        }
