"""
`log check` 与 `log edit` 的命令入口。

- check_task：读 commit_preview.txt，跑 重叠 + 持续时间不一致 两项分析，
  按 JetBrains 风格在终端打印；只要有问题，就自动打开 `log edit` 的 GUI。
- edit_task：直接打开 GUI 编辑 committed 任务。

GUI（PySide6）只在需要时才 import，避免无图形环境时强依赖 Qt。
"""

from __future__ import annotations

from typing import Optional

from summary import _render_error, _render_info

from .analysis import Problem, Severity, analyze
from .store import CommitData, CommitLoadError, load_commit_data

try:
    from rich.console import Console

    _console: Optional[Console] = Console()
except ImportError:  # pragma: no cover
    _console = None


def _print_problems_report(problems: list[Problem]) -> None:
    """终端里按 JetBrains 风格罗列问题：❌ 红=重叠，⚠ 黄=持续不一致。"""
    if not problems:
        if _console is not None:
            _console.print("[bold green]✓ 未发现时间重叠或持续时间不一致问题[/bold green]")
        else:
            print("✓ 未发现时间重叠或持续时间不一致问题")
        return

    errors = sum(1 for p in problems if p.severity is Severity.ERROR)
    warnings = len(problems) - errors
    header = f"发现 {len(problems)} 个问题（重叠 {errors}，持续不一致 {warnings}）"
    if _console is not None:
        _console.print(f"[bold]{header}[/bold]")
        for problem in problems:
            if problem.severity is Severity.ERROR:
                _console.print(f"  [bold red]❌ {problem.message}[/bold red]")
            else:
                _console.print(f"  [yellow]⚠ {problem.message}[/yellow]")
    else:
        print(header)
        for problem in problems:
            mark = "❌" if problem.severity is Severity.ERROR else "⚠"
            print(f"  {mark} {problem.message}")


def _launch_editor(commit_data: CommitData, context: dict[str, str]) -> int:
    """打开 GUI；成功打开并返回即视为完成。Qt 缺失时给出可执行的提示。"""
    try:
        from .ui import run_editor
    except ImportError as exc:  # pragma: no cover - 仅在未装 PySide6 时
        _render_error(
            "无法打开编辑器 GUI",
            "缺少 PySide6。请安装后重试：\n"
            "  python -m pip install PySide6-Essentials\n"
            f"原始错误：{exc}",
        )
        return 1

    applied = run_editor(commit_data, context)
    if applied:
        _render_info("log edit 完成", "改动已写回 commit_preview.txt（committed 数据）。")
    else:
        _render_info("log edit 已取消", "未对 committed 数据做任何改动。")
    return 0


def check_task(context: dict[str, str]) -> int:
    """
    log check —— 回顾即将 push 的 committed 内容，做两项分析；有问题则自动进入 log edit。
    """
    try:
        commit_data = load_commit_data(context)
    except CommitLoadError as exc:
        _render_error("log check 无法执行", str(exc))
        return 1

    problems = analyze(commit_data.items)
    _print_problems_report(problems)

    if not problems:
        return 0

    # 有问题：自动输入 log edit 指令，打开编辑面板
    _render_info("检测到问题，正在打开 log edit 编辑面板", "可在面板中修正后点击“应用”。")
    return _launch_editor(commit_data, context)


def edit_task(context: dict[str, str]) -> int:
    """log edit —— 直接打开编辑面板编辑 committed 任务。"""
    try:
        commit_data = load_commit_data(context)
    except CommitLoadError as exc:
        _render_error("log edit 无法执行", str(exc))
        return 1

    return _launch_editor(commit_data, context)
