from __future__ import annotations

import os
import re
from pathlib import Path


DEFAULT_CONFIG_DIR = ".guiyuniban_log"
DEFAULT_CONFIG_FILE = "config.txt"


def read_config(config_file: str | None) -> dict[str, str]:
    """
    参考 summary.py 的配置读取方式：
    从 key=value 格式的 config.txt 中读取配置。
    """
    if not config_file:
        return {}

    path = Path(config_file).expanduser()
    if not path.exists():
        return {}

    config: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")

    return config


def _normalize_notion_id(value: str | None) -> str:
    """
    支持在 config/env 中填写 Notion 原始 ID 或页面 URL。
    """
    raw = (value or "").strip()
    if not raw:
        return ""

    dashed = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    )
    if dashed:
        return dashed.group(0)

    compact_matches = re.findall(r"[0-9a-fA-F]{32}", raw)
    if compact_matches:
        compact = compact_matches[-1]
        return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"

    return raw


def _resolve_config_value(*values: str | None) -> str:
    for value in values:
        normalized = _normalize_notion_id(value)
        if normalized:
            return normalized
    return ""


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _default_config_file() -> str:
    """
    默认读取用户目录下的 guiyuniban 配置文件。

    Windows 示例：
    C:\\Users\\Guiyuuuuuuu_\\.guiyuniban_log\\config.txt
    """
    return str(Path.home() / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILE)


def _default_data_dir(config: dict[str, str]) -> Path:
    """
    summary.py 里很多文件都来自 context/config。
    这里默认跟你的实际目录保持一致：~/.guiyuniban_log。
    """
    return Path(
        _first_non_empty(
            os.getenv("LOG_DATA_DIR"),
            config.get("data_dir"),
            str(Path.home() / DEFAULT_CONFIG_DIR),
        )
    ).expanduser()


def build_push_config(config_file: str | None = None) -> dict[str, str]:
    """
    读取 PushTasks 需要的关键配置。

    配置来源优先级：
    1. 显式传入 config_file
    2. 环境变量 CONFIG_FILE
    3. ~/.guiyuniban_log/config.txt
    4. 当前目录 config.txt 作为兜底

    注意：
    - 不做 argparse / cmd 子命令解析。
    - 只负责整理 config，不直接执行 push。
    """
    candidate_config_files = [
        config_file,
        os.getenv("CONFIG_FILE"),
        _default_config_file(),
        "config.txt",
    ]

    resolved_config_file = ""
    config: dict[str, str] = {}

    for candidate in candidate_config_files:
        if not candidate:
            continue

        candidate_path = Path(candidate).expanduser()
        candidate_config = read_config(str(candidate_path))
        if candidate_config:
            resolved_config_file = str(candidate_path)
            config = candidate_config
            break

    if not resolved_config_file:
        resolved_config_file = str(Path(_default_config_file()).expanduser())

    config["config_file"] = resolved_config_file

    data_dir = _default_data_dir(config)

    config["commit_preview_file"] = _first_non_empty(
        os.getenv("COMMIT_PREVIEW_FILE"),
        os.getenv("COMMIT_PREVIEW_PATH"),
        config.get("commit_preview_file"),
        str(data_dir / "commit_preview.txt"),
    )

    config["openai_api_key"] = _first_non_empty(
        os.getenv("OPENAI_API_KEY"),
        config.get("openai_api_key"),
    )

    config["notion_token"] = _first_non_empty(
        os.getenv("NOTION_TOKEN"),
        os.getenv("NOTION_API_KEY"),
        config.get("notion_token"),
    )

    config["notion_commit_page_id"] = _resolve_config_value(
        os.getenv("NOTION_COMMIT_PAGE_ID"),
        os.getenv("NOTION_LOG_PAGE_ID"),
        os.getenv("NOTION_PUSH_PAGE_ID"),
        config.get("notion_commit_page_id"),
        config.get("notion_log_page_id"),
        config.get("notion_push_page_id"),
    )

    return config


def read_commit_preview(config: dict[str, str]) -> str:
    """
    根据 summary.py 中 commit_preview_file 的思路读取 commit preview 文本。
    """
    commit_preview_file = config.get("commit_preview_file") or ""
    if not commit_preview_file:
        raise RuntimeError("缺少 commit_preview_file 配置。")

    preview_path = Path(commit_preview_file).expanduser()
    if not preview_path.exists():
        raise RuntimeError(f"commit preview 文件不存在: {preview_path}")

    commit_preview = preview_path.read_text(encoding="utf-8").strip()
    if not commit_preview:
        raise RuntimeError(f"commit preview 文件为空: {preview_path}")

    return commit_preview


def validate_push_config(config: dict[str, str]) -> None:
    """
    PushAgent.PushTasks 当前会用到：
    - config["notion_commit_page_id"]
    - config["openai_api_key"]
    """
    missing = [
        key
        for key in ("openai_api_key", "notion_commit_page_id")
        if not config.get(key)
    ]
    if missing:
        raise RuntimeError(
            "缺少必要配置: "
            + ", ".join(missing)
            + "。已读取配置文件: "
            + str(config.get("config_file") or "未找到")
            + "。请检查该文件、环境变量或字段名。"
        )


def main(config_file: str | None = None) -> int:
    """
    独立入口：
    1. 读取 config
    2. 读取 commitPreview
    3. 直接调用 PushAgent.PushTasks(commitPreview, config)

    不写 cmd 指令，也不解析命令行参数。
    """
    # 兼容两种常见项目结构：
    # 1. PushAgent.py 中直接定义 class PushAgent
    # 2. PushAgent/PushAgent.py 中定义 class PushAgent
    try:
        from PushAgent.Agent import Agent
    except ImportError:
        from PushAgent.Agent import Agent  # type: ignore

    config = build_push_config(config_file)
    validate_push_config(config)

    commit_preview = read_commit_preview(config)

    agent = Agent(configFile=config.get("config_file"), overrides=config)
    agent.PushTasks(commit_preview, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
