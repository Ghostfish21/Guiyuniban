"""
log 交互窗口（命名空间 REPL）。

这里的“窗口”不是独立的 cmd 进程或 Windows 窗口，而是一个 **log 命名空间**：
进入之后所有输入都当作 log 的子指令，直到输入 exit 才退出。

    C:\\> log
    log ❯ start 写代码
    log ❯ end 中午十二点
    log ❯ exit

任何 `log 子指令` 也会先进入这个窗口、再把该子指令当作第一条输入执行：

    C:\\> log start 写代码      # 等价于进入窗口后输入 start 写代码
    log ❯ ...

窗口内建指令：
    exit / quit        退出窗口
    help / ?           打印主帮助页；`help start` 打印子指令帮助
    clear / cls        清屏

非交互场景（stdin 不是终端，或显式 `--once`）不会进入窗口，只执行一次即返回，
保证管道与脚本调用行为不变。
"""

from __future__ import annotations

import shlex
from typing import Any, Callable, Optional, Sequence

# 内建指令词表
EXIT_WORDS = {"exit", "quit", ":q", ":wq"}
HELP_WORDS = {"help", "?", "h"}
CLEAR_WORDS = {"clear", "cls"}

PROMPT = "[bold bright_cyan]log[/bold bright_cyan] [dim]❯[/dim] "

# _handle_tokens 的返回哨兵：表示“该退出窗口了”
_EXIT = object()


def split_command_line(line: str) -> list[str]:
    """
    把一行输入切成 argv。

    优先用 shlex（支持 log start "写 代码" 这种带引号的写法）；
    引号不配对时 shlex 会抛 ValueError，退回最朴素的空白切分，
    避免用户少打一个引号就得不到任何反馈。
    """
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def strip_log_prefix(tokens: list[str]) -> list[str]:
    """窗口内允许连 log 一起打（log start x），这里把多余的前缀吃掉。"""
    while tokens and tokens[0].lower() == "log":
        tokens = tokens[1:]
    return tokens


class LogShell:
    """
    log 窗口本体。

    execute(argv) -> int 由调用方注入（guiyuniban_control 负责 parse + dispatch），
    这样 shell 不反向依赖主模块，也便于单独测试。
    """

    def __init__(
        self,
        execute: Callable[[list[str]], int],
        *,
        console: Any,
        print_help: Callable[[], None],
    ) -> None:
        self.execute = execute
        self.console = console
        self.print_help = print_help
        self.last_code = 0

    # ------------------------------------------------------------------ 输出
    def _print_banner(self) -> None:
        self.console.print()
        self.console.print(
            "[bold bright_cyan]log 窗口[/bold bright_cyan] "
            "[dim]—— 直接输入子指令即可，无需再打 log[/dim]"
        )
        self.console.print(
            "[dim]例如：[/dim][bright_magenta]start 写代码[/bright_magenta][dim] · [/dim]"
            "[bright_magenta]end 中午十二点[/bright_magenta][dim] · [/dim]"
            "[bright_magenta]commit[/bright_magenta][dim] · [/dim]"
            "[bright_magenta]desc[/bright_magenta]"
        )
        self.console.print(
            "[dim]输入 [/dim][bold]exit[/bold][dim] 退出窗口，[/dim]"
            "[bold]help[/bold][dim] 查看全部指令。[/dim]"
        )
        self.console.print()

    def _echo(self, tokens: list[str]) -> None:
        """把随 `log xxx` 带进来的第一条指令回显成一行输入，视觉上与手打一致。"""
        self.console.print(f"{PROMPT}{' '.join(tokens)}")

    # -------------------------------------------------------------- 指令处理
    def _handle_tokens(self, tokens: list[str]) -> Any:
        """
        处理一条已切分好的输入。返回 _EXIT 表示退出窗口，否则返回退出码。
        """
        tokens = strip_log_prefix(tokens)
        if not tokens:
            return self.last_code

        head = tokens[0].lower()

        if head in EXIT_WORDS:
            return _EXIT

        if head in CLEAR_WORDS:
            self.console.clear()
            return 0

        if head in HELP_WORDS:
            if len(tokens) > 1:
                # help start -> start --help
                return self._run([tokens[1], "--help"])
            self.print_help()
            return 0

        return self._run(tokens)

    def _run(self, argv: list[str]) -> int:
        """
        执行一条真正的 log 子指令。

        窗口必须比一次性进程更耐操：argparse 的 --help / 参数错误会抛 SystemExit，
        子命令自身也可能抛异常，这里全部拦下来，只把退出码带回，不让窗口崩掉。
        """
        try:
            return int(self.execute(argv) or 0)
        except SystemExit as exc:  # argparse 的 --help(0) / 参数错误(2)
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            self.console.print(str(code))
            return 1
        except KeyboardInterrupt:
            self.console.print("[yellow]已中断当前指令[/yellow]")
            return 130
        except Exception as exc:  # noqa: BLE001 - 窗口不能被单条指令拖垮
            self.console.print(f"[bold bright_red]指令执行失败：[/bold bright_red]{exc}")
            return 1

    # ------------------------------------------------------------------ 主循环
    def run(self, initial_tokens: Optional[Sequence[str]] = None) -> int:
        """进入窗口，直到 exit / EOF。返回最后一条指令的退出码。"""
        self._print_banner()

        if initial_tokens:
            tokens = list(initial_tokens)
            self._echo(tokens)
            result = self._handle_tokens(tokens)
            if result is _EXIT:
                return self.last_code
            self.last_code = result

        while True:
            try:
                line = self.console.input(PROMPT)
            except EOFError:
                # Ctrl+Z(Windows) / Ctrl+D 等价于 exit
                self.console.print()
                break
            except KeyboardInterrupt:
                self.console.print("\n[dim]（Ctrl+C）输入 exit 退出 log 窗口[/dim]")
                continue

            if not line.strip():
                continue

            result = self._handle_tokens(split_command_line(line))
            if result is _EXIT:
                break
            self.last_code = result

        self.console.print("[dim]已退出 log 窗口。[/dim]")
        return self.last_code


def run_shell(
    execute: Callable[[list[str]], int],
    *,
    console: Any,
    print_help: Callable[[], None],
    initial_tokens: Optional[Sequence[str]] = None,
) -> int:
    """便捷入口：构造 LogShell 并运行。"""
    return LogShell(execute, console=console, print_help=print_help).run(initial_tokens)
