"""人工确认流：agentd 暂停这一步，在自己的终端上问一句。

没有 tty（后台跑、CI、被 systemd 拉起）时返回 None —— 上层策略引擎会把
「需要确认但没人能确认」判成拒绝。宁可拒绝，也不要无人值守地放行危险操作。
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.prompt import Confirm

from anthill.agent.tools.base import Confirmer

CONFIRM_TIMEOUT = 300.0


def terminal_confirmer(console: Console | None = None) -> Confirmer | None:
    if not sys.stdin.isatty():
        return None
    out = console or Console()

    async def ask(prompt: str) -> bool:
        out.print(f"\n[bold yellow]需要你确认[/bold yellow]\n{prompt}")
        try:
            # 阻塞式输入放线程里，别把整个 agentd 的事件循环卡死
            return await asyncio.wait_for(
                asyncio.to_thread(Confirm.ask, "允许执行？", default=False),
                timeout=CONFIRM_TIMEOUT,
            )
        except TimeoutError:
            out.print("[dim]确认超时，按拒绝处理[/dim]")
            return False

    return ask
