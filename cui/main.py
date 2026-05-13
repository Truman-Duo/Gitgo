"""CUI 终端界面 — 薄入口，委托给 cui/ 子包"""
from rich.console import Console
from cui.main_flow import entry as _entry

_console = Console()

# 注入 console 到各模块
from cui import projects, display, workflow
projects.console = _console
display.console = _console
workflow.console = _console


def entry():
    _entry(_console)
