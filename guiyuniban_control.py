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
    from rich.padding import Padding
    from rich.rule import Rule
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

  log
      进入 log 窗口（命名空间），之后直接输入 start / end / commit 等子指令，
      输入 exit 退出。任何 log 子指令也会先进入窗口再执行该指令。

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

  log costart 任务名
      在正在进行的任务之上再开一个并发任务（没有任务进行时无效）。
      父级是当前路径上最内层仍在进行的任务，可以层层嵌套。
      并发任务在 log end 结算前对 status / commit 等一律不可见。

  log coend
      结束一个并发任务；有多个在进行时弹窗选择。
      结束顺序不影响结果，时间轴统一在 log end 时排定。

  log pause
      暂停当前任务（含所有层的并发任务）：弹出全屏置顶窗口，点 resume 才结束。
      这段时间在 log end 时从任务时长里扣掉，不产出任何任务。

  log resume
      命令行结束暂停。正常用窗口上的 resume 即可，这条是窗口被强杀时的收口手段。

  log commit
      让 LLM 校准所有未 commit 任务，追加进 committed 池（累积，不覆盖池内已有任务）；
      同时把这批任务从未 commit 池排干

  log chat 中文指令
      让 LLM 按中文指令修改 committed 池，逐条 y/n 审核后写回
      例如: log chat 把周三的写代码任务的类别改成 Work

  log push
      把整份 committed 池同步至 Notion，并备份到本地后清空池

  log desc
      commit 前编辑任务描述：打开图形面板，上半部分只读展示任务信息，
      下半部分编辑该任务（session）的详细描述，commit 时随任务带入池中

  log check
      检查即将 push 的 committed 任务是否有时间重叠 / 持续时间不一致；
      有问题时自动打开图形编辑面板

  log edit
      直接打开图形编辑面板，编辑 committed 任务（时间轴 + 逐条修正）

  log status
      统计 committed + 未 commit（已结束）任务的总用时/项数、按类别统计、按天统计，
      以及本天时间（committed 本天 + 未 commit 本天 + 进行中任务的已持续时间）

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
    console.print(
        Text("在 log 窗口里可以省略 log 前缀，直接输入子指令；exit 退出窗口", style="app.subtitle")
    )
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
    commands.add_row("log costart", "任务名", "在进行中的任务之上再开一个并发任务，可层层嵌套")
    commands.add_row("log coend", "", "结束一个并发任务；多个在进行时弹窗选择")
    commands.add_row("log pause", "", "全屏置顶暂停；这段空档在 log end 时从任务时长里扣掉")
    commands.add_row("log resume", "", "命令行结束暂停（窗口被强杀时的收口手段）")
    commands.add_row("log commit", "", "校准未 commit 任务并追加进 committed 池（累积，不覆盖）")
    commands.add_row("log chat", "中文指令", "让 LLM 按中文指令修改 committed 池，逐条 y/n 审核")
    commands.add_row("log desc", "", "commit 前编辑任务描述（上=任务信息，下=描述编辑）")
    commands.add_row("log push", "", "把整份 committed 池同步到 Notion，备份后清空")
    commands.add_row("log check", "", "检查 committed 任务重叠/持续不一致，有问题就开编辑面板")
    commands.add_row("log edit", "", "打开图形面板编辑 committed 任务（时间轴 + 逐条修正）")
    commands.add_row("log status", "", "统计 committed + 未 commit 任务用时：总览 / 类别 / 按天 / 本天（含进行中）")
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

    # log costart 任务名
    parser_costart = subparsers.add_parser(
        "costart",
        help="在正在进行的任务之上再开一个并发任务",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )
    parser_costart.add_argument(
        "task_name_parts",
        nargs="+",
        metavar="任务名",
        help="并发任务名，例如: log costart 写代码",
    )

    # log coend
    subparsers.add_parser(
        "coend",
        help="结束一个并发任务；多个在进行时弹窗选择",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log pause
    subparsers.add_parser(
        "pause",
        help="暂停当前任务，弹出全屏置顶窗口，点 resume 结束",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
    )

    # log resume
    subparsers.add_parser(
        "resume",
        help="命令行结束暂停（窗口被强杀时的收口手段）",
        allow_abbrev=False,
        formatter_class=_formatter_class(),
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

    # log desc
    subparsers.add_parser(
        "desc",
        help="commit 前编辑任务描述（上=任务信息，下=描述编辑）",
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

    # log status
    subparsers.add_parser(
        "status",
        help="统计 committed + 未 commit 任务的总用时/项数、类别与按天统计，以及本天时间（含进行中任务）",
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
            from achievements import notify_start

            notify_start(context)
        return code

    if args.command == "cont":
        from start import cont_task

        task_name = join_free_text(args.task_name_parts)
        code = cont_task(task_name=task_name, context=context)
        if code == 0:
            detail = f"输入任务名：{task_name}" if task_name else "未提供任务名：已自动继续最近一次 log end 的任务"
            print_status("success", "任务已继续", [detail])
            from achievements import notify_start

            notify_start(context)
        return code

    if args.command == "end":
        from end import end_task
        from achievements import active_session_id, notify_end

        # 结束【前】先捕获当前 active session id，便于结束后定位刚结束的任务
        ended_session_id = active_session_id(context)

        raw_time = join_free_text(args.time_parts)
        code = end_task(raw_time=raw_time, context=context)
        if code == 0:
            detail = f"结束时间：{raw_time}" if raw_time else "结束时间：当前系统时间"
            print_status("success", "任务已结束", [detail])
            notify_end(context, ended_session_id)
        return code

    if args.command == "costart":
        from costart import costart_task

        task_name = join_free_text(args.task_name_parts)
        if not task_name:
            print_status("error", "costart 指令需要任务名", stderr=True)
            return 2

        # 并发任务是独立子系统：不触发成就、不进 status，直到 log end 结算。
        code = costart_task(task_name=task_name, context=context)
        if code == 0:
            print_status("success", "并发任务已开始", [f"任务：{task_name}"])
        return code

    if args.command == "coend":
        from costart import coend_task

        return coend_task(context=context)

    if args.command == "pause":
        from pause import pause_task

        # 暂停不是任务：不触发成就、不进 status，只在 log end 结算时扣出一个空档。
        return pause_task(context=context)

    if args.command == "resume":
        from pause import resume_task

        return resume_task(context=context)

    if args.command == "commit":
        from summary import commit_tasks

        code = commit_tasks(context=context)
        if code == 0:
            # 池表格已由 summary._render_commit_preview 打印，这里只补一行结果确认，
            # 不再重复渲染 commit_preview.txt 的全文明细。
            print_status("success", "committed 池已更新", [f"池文件：{COMMIT_PREVIEW_FILE}"])
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

    if args.command == "desc":
        from taskeditor import desc_task

        return desc_task(context=context)

    if args.command == "check":
        from taskeditor import check_task

        return check_task(context=context)

    if args.command == "edit":
        from taskeditor import edit_task

        return edit_task(context=context)

    if args.command == "status":
        from status import status_task

        return status_task(context=context)

    if args.command == "indexreset":
        from summary import reset_task_index

        code = reset_task_index(context=context)
        if code == 0:
            print_status("success", "任务编号已重置", [f"下一个新任务编号：10000", f"文件：{TASK_INDEX_FILE}"])
        return code

    print_app_help()
    return 2


# 只执行一条指令、不进入 log 窗口的开关（脚本/管道里用）
ONCE_FLAGS = {"--once", "--no-shell"}
HELP_FLAGS = {"-h", "--help"}


def run_command(parser: argparse.ArgumentParser, argv: list[str], context: dict[str, str]) -> int:
    """解析并执行一条 log 指令。argparse 的 SystemExit 交给调用方处理。"""
    try:
        args = parser.parse_args(argv)
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


def _stdin_is_interactive() -> bool:
    """管道 / 重定向输入时不进入 log 窗口，保证脚本调用行为不变。"""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    ensure_runtime_files()

    parser = build_parser()

    if argv is None:
        argv = sys.argv[1:]

    tokens = list(argv)

    # --once / --no-shell 是窗口级开关，不是子指令，解析前先摘掉
    once = bool(tokens) and tokens[0] in ONCE_FLAGS
    if once:
        tokens = tokens[1:]

    context = build_context()

    # -h/--help 保持一次性输出，不进窗口
    if tokens and tokens[0] in HELP_FLAGS:
        print_app_help()
        return 0

    if once or not _stdin_is_interactive():
        if not tokens:
            print_app_help()
            return 0
        return run_command(parser, tokens, context)

    # 交互终端：任何输入都先打开 log 窗口，再把这条指令当作第一条输入执行
    from shell import run_shell

    return run_shell(
        lambda command_argv: run_command(parser, command_argv, context),
        console=console,
        print_help=print_app_help,
        initial_tokens=tokens,
    )


if __name__ == "__main__":
    raise SystemExit(main())
