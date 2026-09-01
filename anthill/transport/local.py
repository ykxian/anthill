"""同机传输：直接把信封 rename 进对方邮箱。

这是整个系统里最简单也最重要的一种传输 —— 它就是「Claude Code 监控文件夹」
那个土办法的正式版本。延迟 ≈ 一次 rename，天然崩溃安全。
"""

from __future__ import annotations

import asyncio

from anthill.core.envelope import Envelope, TransportKind
from anthill.core.errors import MailboxError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.transport.base import DeliveryResult, Destination, Transport


class LocalTransport(Transport):
    kind = TransportKind.LOCAL

    def __init__(self, layout: NodeLayout, *, threaded: bool = True) -> None:
        self._layout = layout
        self._threaded = threaded

    async def deliver(self, env: Envelope, dest: Destination) -> DeliveryResult:
        mailbox = Mailbox(self._layout.mailbox_dir(dest.agent))
        if not mailbox.exists:
            return DeliveryResult.failure(
                self.kind,
                dest,
                f"目标邮箱不存在：{mailbox.root}（对方 agentd 没启动过？）",
            )
        try:
            if self._threaded:
                # 常驻 agentd 丢到线程池，别卡住事件循环里的其他 Agent 任务。
                path = await asyncio.to_thread(mailbox.deposit, env)
            else:
                # 短命 CLI 在部分受限 sandbox 里会卡在默认 executor 退出；一次
                # 小文件原子写成本可控，直接执行才能保证 `send --wait 0` 退场。
                path = mailbox.deposit(env)
        except MailboxError as exc:
            return DeliveryResult.failure(self.kind, dest, str(exc))
        return DeliveryResult.success(self.kind, dest, str(path))
