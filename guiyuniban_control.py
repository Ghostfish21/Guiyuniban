from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from rich.align import Align
    from rich.console import Console, Group, RenderableType
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
except ModuleNotFoundError as exc:  # pragma: no cover - only happens before dependencies are installed
    missing = exc.name or "rich"
    print(f"缺少终端 UI 依赖：{missing}", file=sys.stderr)
    print(f"当前运行的 Python：{sys.executable}", file=sys.stderr)
    print("请用同一个 Python 安装依赖：", file=sys.stderr)
    print(f"  {sys.executable} -m pip install rich", file=sys.stderr)
    raise SystemExit(1) from exc
except ImportError as exc:  # pragma: no cover - broken or shadowed dependency
    print("终端 UI 依赖导入失败，可能是安装损坏或当前目录下有同名文件/目录遮蔽了 rich。", file=sys.stderr)
    print(f"当前运行的 Python：{sys.executable}", file=sys.stderr)
    print(f"原始错误：{exc}", file=sys.stderr)
    print("可尝试：", file=sys.stderr)
    print(f"  {sys.executable} -m pip install --upgrade --force-reinstall rich", file=sys.stderr)
    raise SystemExit(1) from exc

APP_NAME = "guiyuniban_log"

# 所有运行时文件统一放在用户目录下，避免污染项目目录
DATA_DIR = Path.home() / f".{APP_NAME}"

# 当前还没有 commit 的任务记录
UNCOMMIT_FILE = DATA_DIR / "uncommit_tasks.txt"

# commit 预览结果，log commit 生成，log push 使用
COMMIT_PREVIEW_FILE = DATA_DIR / "commit_preview.txt"

# 任务排序编号的下一个可用值。log indexreset 会把它重置为 10000。
TASK_INDEX_FILE = DATA_DIR / "task_index.txt"

# 配置文件，后续可以放 Notion page id、OpenAI key、活跃日边界等
CONFIG_FILE = DATA_DIR / "config.txt"


THEME = Theme(
    {
        "app.title": "bold bright_cyan",
        "app.subtitle": "dim",
        "command": "bold bright_cyan",
        "argument": "bright_magenta",
        "muted": "dim",
        "success": "bold bright_green",
        "warning": "bold yellow",
        "error": "bold bright_red",
        "info": "bold bright_blue",
        "path": "bright_black",
    }
)

console = Console(theme=THEME, highlight=False)
err_console = Console(stderr=True, theme=THEME, highlight=False)

HELP_TEXT = """
用法:

  log start 任务名
      开始一个新任务

  log cont [任务名]
      写任务名时寻找最相似的未 commit 任务；不写任务名时继续上一个 log end 的任务

  log end
      结束当前正在进行的任务，结束时间为当前系统时间

  log end 时间
      使用自然语言时间结束当前任务
      例如:
        log end 中午十二点
        log end 下午五点
        log end 开始之后三个小时五十三分后

  log commit
      整理所有未 commit 任务，输出预览

  log chat 中文指令
      让 LLM 按中文指令修改 commit 预览，逐条 y/n 审核后覆盖 commit_preview.txt
      例如: log chat 把周三的写代码任务的类别改成 Work

  log push
      将 commit 结果同步至 Notion

  log check
      检查即将 push 的 committed 任务是否有时间重叠 / 持续时间不一致；
      有问题时自动打开图形编辑面板

  log edit
      直接打开图形编辑面板，编辑 committed 任务（时间轴 + 逐条修正）

  log indexreset
      将新任务编号重置为 10000
"""


def _badge(label: str, style: str) -> Text:
    """生成一个不依赖手写字符画的彩色状态标签。"""
    return Text(f" {label} ", style=f"bold white on {style}")


def _print_section_title(title: str, icon: str = "") -> None:
    label = f"{icon} {title}".strip()
    console.print(Rule(Text(label, style="app.title"), style="bright_black"))


def _make_key_value_grid(items: Iterable[tuple[str, str]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="muted", no_wrap=True)
    grid.add_column()
    for key, value in items:
        grid.add_row(key, value)
    return grid


def print_status(kind: str, title: str, details: list[str] | None = None, *, stderr: bool = False) -> None:
    """
    打印状态消息。

    kind 可选: success / error / warning / info。
    使用 rich 的颜色和排版能力，而不是手写边框字符。
    """
    target = err_console if stderr else console
    kind_map = {
        "success": ("完成", "green", "success", "✅"),
        "error": ("错误", "red", "error", "❌"),
        "warning": ("注意", "yellow", "warning", "⚠️"),
        "info": ("提示", "blue", "info", "ℹ️"),
    }
    badge_text, badge_color, title_style, icon = kind_map.get(kind, kind_map["info"])

    rows: list[RenderableType] = [
        Group(
            _badge(badge_text, badge_color),
            Padding(Text(f"{icon} {title}", style=title_style), (0, 0, 0, 2)),
        )
    ]

    if details:
        detail_grid = Table.grid(padding=(0, 1))
        detail_grid.add_column(style="muted", no_wrap=True)
        detail_grid.add_column()
        for line in details:
            detail_grid.add_row("•", line)
        rows.append(Padding(detail_grid, (1, 0, 0, 2)))

    target.print(Padding(Group(*rows), (1, 0)))


def print_app_help() -> None:
    """打印更图形化的主帮助页面。"""
    console.print()
    console.print(Align.left(Text("guiyuniban log", style="app.title")))
    console.print(Text("个人任务时间记录命令行工具 · start / cont / end / commit / push", style="app.subtitle"))
    console.print()

    _print_section_title("常用命令", "🚀")

    commands = Table(
        show_header=True,
        header_style="bold bright_cyan",
        show_edge=False,
        box=None,
        pad_edge=False,
        expand=True,
    )
    commands.add_column("命令", style="command", no_wrap=True, ratio=2)
    commands.add_column("输入", style="argument", ratio=2)
    commands.add_column("作用", ratio=5)
    commands.add_row("log start", "任务名", "开始一个新任务")
    commands.add_row("log cont", "[任务名]", "写任务名时按相似度继续；不写时继续最近一次 log end 的任务")
    commands.add_row("log end", "[时间]", "结束当前任务；不写时间时使用当前系统时间")
    commands.add_row("log commit", "", "整理所有未 commit 任务，并生成预览")
    commands.add_row("log chat", "中文指令", "让 LLM 按中文指令修改 commit 预览，逐条 y/n 审核")
    commands.add_row("log push", "", "把 commit 结果同步到 Notion")
    commands.add_row("log check", "", "检查 committed 任务重叠/持续不一致，有问题就开编辑面板")
    commands.add_row("log edit", "", "打开图形面板编辑 committed 任务（时间轴 + 逐条修正）")
    commands.add_row("log indexreset", "", "把下一个新任务编号重置为 10000")
    console.print(commands)

    console.print()
    _print_section_title("自然语言时间示例", "🕒")

    examples = Table.grid(padding=(0, 3))
    examples.add_column(style="command", no_wrap=True)
    examples.add_column(style="muted")
    examples.add_row("log end 中午十二点", "把结束时间解析为中午 12:00")
    examples.add_row("log end 下午五点", "把结束时间解析为 17:00")
    examples.add_row("log end 开始之后三个小时五十三分后", "按开始时间加时长")
    console.print(Padding(examples, (0, 0, 0, 2)))

    console.print()
    _print_section_title("运行时文件", "📁")
    console.print(
        Padding(
            _make_key_value_grid(
                [
                    ("数据目录", f"[path]{DATA_DIR}[/path]"),
                    ("未提交任务", f"[path]{UNCOMMIT_FILE.name}[/path]"),
                    ("commit 预览", f"[path]{COMMIT_PREVIEW_FILE.name}[/path]"),
                    ("任务编号", f"[path]{TASK_INDEX_FILE.name}[/path]"),
                    ("配置文件", f"[path]{CONFIG_FILE.name}[/path]"),
                ]
            ),
            (0, 0, 0, 2),
        )
    )
    console.print()


def print_commit_preview_if_available() -> None:
    """commit 成功后，把预览文件用富文本方式展示出来。"""
    if not COMMIT_PREVIEW_FILE.exists():
        return

    preview = COMMIT_PREVIEW_FILE.read_text(encoding="utf-8").strip()
    if not preview:
        print_status("warning", "commit 预览文件为空", [f"文件位置：{COMMIT_PREVIEW_FILE}"])
        return

    console.print()
    _print_section_title("commit 预览", "🧾")

    # 如果 preview 像 Markdown，就按 Markdown 渲染；否则按普通文本做语法高亮。
    if any(marker in preview for marker in ("# ", "- ", "* ", "|")):
        console.print(Padding(Markdown(preview), (0, 0, 0, 2)))
    else:
        console.print(Padding(Syntax(preview, "text", word_wrap=True), (0, 0, 0, 2)))


class GuiyunibanArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print_status(
            "error",
            "参数错误",
            [message, "运行 log --help 查看完整命令说明。"],
            stderr=True,
        )
        raise SystemExit(2)

    def print_help(self, file: Any | None = None) -> None:
        # 主命令 help 使用定制 UI；子命令 help 保留 argparse / rich-argparse 的结构化输出。
        if self.prog == "log":
            print_app_help()
        else:
            super().print_help(file)


def ensure_runtime_files() -> None:
    """
    确保运行时目录和 txt 文件存在。
    这里不写具体业务数据，只负责初始化文件。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not UNCOMMIT_FILE.exists():
        UNCOMMIT_FILE.write_text("", encoding="utf-8")

    if not COMMIT_PREVIEW_FILE.exists():
        COMMIT_PREVIEW_FILE.write_text("", encoding="utf-8")

    if not TASK_INDEX_FILE.exists():
        TASK_INDEX_FILE.write_text("10000", encoding="utf-8")

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            "\n".join(
                [
                    "# guiyuniban log config",
                    "# day_boundary_hour=5 表示凌晨 0:00-4:59 仍算前一天",
                    "day_boundary_hour=5",
                    "",
                    "# notion_task_category_page_id=",
                    "# notion_database_id=",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def build_context() -> dict[str, str]:
    """
    传给 start.py / end.py / summary.py 的运行上下文。

    不直接让其他模块依赖 guiyuniban_control.py 里的全局变量，
    这样以后更容易测试和重构。
    """
    return {
        "data_dir": str(DATA_DIR),
        "uncommit_file": str(UNCOMMIT_FILE),
        "commit_preview_file": str(COMMIT_PREVIEW_FILE),
        "task_index_file": str(TASK_INDEX_FILE),
        "config_file": str(CONFIG_FILE),
    }


def join_free_text(parts: list[str] | None) -> str:
    """
    把命令行里的剩余参数合并成自然语言文本。

    支持:
      log start 写代码
      log start "写代码"

    两者最终都会变成:
      写代码
    """
    if not parts:
        return ""
    return " ".join(parts).strip()


def _formatter_class() -> type[argparse.HelpFormatter]:
    """
    使用 argparse 原生 formatter，避免 rich-argparse 在部分 Python / Windows 组合下
    因样式解析而影响命令运行。

    主帮助页和运行状态仍然由 Rich 渲染；子命令 help 保持稳定、可读。
    """
    return argparse.RawTextHelpFormatter


def build_parser() -> argparse.ArgumentParser:
    parser = GuiyunibanArgumentParser(
        prog="log",
        description="个人任务时间记录命令行工具",
        add_help=True,
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
    )

    # log start 任务名
    parser_start = subparsers.add_parser(
        "start",
        help="开始一个新任务",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )
    parser_start.add_argument(
        "task_name_parts",
        nargs="+",
        metavar="任务名",
        help="任务名，例如: log start 写项目代码",
    )

    # log cont [任务名]
    parser_cont = subparsers.add_parser(
        "cont",
        help="继续任务；不写任务名时继续最近一次 log end 的任务",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )
    parser_cont.add_argument(
        "task_name_parts",
        nargs="*",
        metavar="任务名",
        help="可选任务名，例如: log cont 写日志工具；不写则继续最近一次 log end 的任务",
    )

    # log end [时间]
    parser_end = subparsers.add_parser(
        "end",
        help="结束当前任务，可选自然语言时间",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )
    parser_end.add_argument(
        "time_parts",
        nargs="*",
        metavar="时间",
        help="可选自然语言时间，例如: 中午十二点 / 开始之后三个小时五十三分后",
    )

    # log commit
    subparsers.add_parser(
        "commit",
        help="整理所有未 commit 任务，输出预览",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log chat 中文指令
    parser_chat = subparsers.add_parser(
        "chat",
        help="让 LLM 按中文指令修改 commit 预览，逐条 y/n 审核",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )
    parser_chat.add_argument(
        "instruction_parts",
        nargs="+",
        metavar="指令",
        help="中文修改指令，例如: log chat 把周三的写代码任务的类别改成 Work",
    )

    # log push
    subparsers.add_parser(
        "push",
        help="将 commit 结果同步到 Notion",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log check
    subparsers.add_parser(
        "check",
        help="检查即将 push 的 committed 任务是否有时间重叠 / 持续时间不一致；有问题则打开编辑面板",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log edit
    subparsers.add_parser(
        "edit",
        help="打开图形编辑面板，编辑 committed 任务（时间轴 + 逐条修正）",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log indexreset
    subparsers.add_parser(
        "indexreset",
        help="将新任务编号重置为 10000",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    return parser


def dispatch(args: argparse.Namespace, context: dict[str, str]) -> int:
    """
    根据子指令分发到对应模块。

    注意:
    - start 和 cont 在 start.py
    - end 在 end.py
    - commit 和 push 在 summary.py
    """

    if args.command == "start":
        from start import start_task

        task_name = join_free_text(args.task_name_parts)
        if not task_name:
            print_status("error", "start 指令需要任务名", stderr=True)
            return 2

        code = start_task(task_name=task_name, context=context)
        if code == 0:
            print_status("success", "任务已开始", [f"任务：{task_name}"])
        return code

    if args.command == "cont":
        from start import cont_task

        task_name = join_free_text(args.task_name_parts)
        code = cont_task(task_name=task_name, context=context)
        if code == 0:
            detail = f"输入任务名：{task_name}" if task_name else "未提供任务名：已自动继续最近一次 log end 的任务"
            print_status("success", "任务已继续", [detail])
        return code

    if args.command == "end":
        from end import end_task

        raw_time = join_free_text(args.time_parts)
        code = end_task(raw_time=raw_time, context=context)
        if code == 0:
            detail = f"结束时间：{raw_time}" if raw_time else "结束时间：当前系统时间"
            print_status("success", "任务已结束", [detail])
        return code

    if args.command == "commit":
        from summary import commit_tasks

        code = commit_tasks(context=context)
        if code == 0:
            print_status("success", "commit 预览已生成", [f"文件：{COMMIT_PREVIEW_FILE}"])
            print_commit_preview_if_available()
        return code

    if args.command == "chat":
        from chat import chat_task

        instruction = join_free_text(args.instruction_parts)
        if not instruction:
            print_status("error", "chat 指令需要中文指令", stderr=True)
            return 2
        return chat_task(instruction=instruction, context=context)

    if args.command == "push":
        from summary import push_tasks

        code = push_tasks(context=context)
        if code == 0:
            print_status("success", "已同步到 Notion")
        return code

    if args.command == "check":
        from taskeditor import check_task

        return check_task(context=context)

    if args.command == "edit":
        from taskeditor import edit_task

        return edit_task(context=context)

    if args.command == "indexreset":
        from summary import reset_task_index

        code = reset_task_index(context=context)
        if code == 0:
            print_status("success", "任务编号已重置", [f"下一个新任务编号：10000", f"文件：{TASK_INDEX_FILE}"])
        return code

    print_app_help()
    return 2


def main(argv: list[str] | None = None) -> int:
    ensure_runtime_files()

    parser = build_parser()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print_app_help()
        return 0

    try:
        args = parser.parse_args(argv)
        context = build_context()
        return dispatch(args, context)

    except KeyboardInterrupt:
        print_status("warning", "操作已取消", stderr=True)
        return 130

    except ModuleNotFoundError as exc:
        print_status(
            "error",
            "模块导入失败",
            [
                str(exc),
                "请确认 start.py、end.py、summary.py 与 guiyuniban_control.py 在同一目录下。",
            ],
            stderr=True,
        )
        return 1

    except Exception as exc:
        print_status("error", "运行失败", [str(exc)], stderr=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
