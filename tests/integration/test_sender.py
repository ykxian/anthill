"""Sender 的并发与重试行为。

这里的第一个用例来自一次真实的手工联调：agentd 跑起来后日志里出现了
`delivery.retry attempts=0` 和 `非法状态迁移：delivered → delivered` ——
重试循环把还在投递中的消息又抢发了一遍。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from anthill.agent.sender import Sender
from anthill.core import outbox as outbox_module
from anthill.core.envelope import TransportKind
from anthill.core.errors import UnknownRecipient
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.payloads import MessageType, ReceiptPayload, TaskRequestPayload
from anthill.core.router import Router
from anthill.core.states import DeliveryState, DeliveryTracker
from anthill.transport.base import DeliveryResult, Destination
from anthill.transport.registry import TransportRegistry


class SlowTransports(TransportRegistry):
    """投递要花 200ms 的传输，好把「投递中」这个窗口放大到可测。"""

    def __init__(self, config, layout, *, delay: float = 0.2, ok: bool = True) -> None:
        super().__init__(config, layout)
        self.delay = delay
        self.ok = ok
        self.calls: list[str] = []

    async def deliver(self, env):  # type: ignore[override]
        self.calls.append(env.id)
        await asyncio.sleep(self.delay)
        dest = Destination(node=env.to.node, agent=env.to.agent)
        if self.ok:
            return DeliveryResult.success(TransportKind.LOCAL, dest, "/dev/null")
        return DeliveryResult.failure(TransportKind.LOCAL, dest, "对方邮箱不存在")


@pytest.fixture
def make_sender(layout, config, addr):
    def build(transports: TransportRegistry) -> tuple[Sender, DeliveryTracker, Mailbox]:
        mailbox = Mailbox(layout.mailbox_dir("alpha")).ensure()
        tracker = DeliveryTracker()
        sender = Sender(
            identity=addr("alpha"),
            mailbox=mailbox,
            router=Router(config, layout),
            transports=transports,
            tracker=tracker,
            log=EventLog(None, agent="alpha", echo=False),
        )
        return sender, tracker, mailbox

    return build


async def test_retry_loop_does_not_steal_an_inflight_message(layout, config, addr, make_sender):
    transports = SlowTransports(config, layout)
    sender, tracker, _ = make_sender(transports)

    send = asyncio.create_task(
        sender.send_new(
            to=addr("beta"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="别重发我"),
        )
    )
    await asyncio.sleep(0.05)  # 首次投递还在路上
    await sender.retry_due()
    env = await send

    assert transports.calls == [env.id]  # 只投了一次
    assert tracker.get(env.id).state is DeliveryState.DELIVERED


async def test_failed_delivery_is_retried_after_backoff(
    layout, config, addr, make_sender, monkeypatch
):
    monkeypatch.setattr(outbox_module, "BACKOFF_BASE", timedelta(seconds=0.01))
    transports = SlowTransports(config, layout, delay=0.0, ok=False)
    sender, _, mailbox = make_sender(transports)

    env = await sender.send_new(
        to=addr("beta"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="第一次会失败"),
    )
    assert Outbox(mailbox).load_pending()[0].attempts == 1

    await asyncio.sleep(0.05)
    await sender.retry_due()

    assert transports.calls == [env.id, env.id]
    assert Outbox(mailbox).load_pending()[0].attempts == 2


async def test_receipts_never_trigger_receipts(layout, config, addr, make_sender, make_task):
    """回执的回执 = 无限套娃，协议上直接禁止。"""
    transports = SlowTransports(config, layout, delay=0.0)
    sender, _, _ = make_sender(transports)
    incoming_receipt = make_task().reply(
        type=MessageType.RECEIPT_ACCEPTED,
        payload=ReceiptPayload(ref="01J000000000000000000000AA"),
        sender=addr("beta"),
    )

    assert await sender.send_receipt(incoming_receipt, MessageType.RECEIPT_ACCEPTED) is None
    assert transports.calls == []


async def test_unknown_recipient_is_reported_not_swallowed(layout, config, addr, make_sender):
    transports = SlowTransports(config, layout, delay=0.0)
    sender, _, _ = make_sender(transports)

    with pytest.raises(UnknownRecipient):
        await sender.send_new(
            to=addr("ghost"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="查无此人"),
        )
