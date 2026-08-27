"""交互式 Agent 通用信箱驱动的宿主无关契约。"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest

from anthill.adapters.bridge import BridgeHandler
from anthill.adapters.bridge_connect import NO_REPLY_SENTINEL
from anthill.adapters.interactive_agent import (
    HostTurn,
    InboxMessage,
    InteractiveAgentBridge,
)
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout

AGENT = "test-agent"


class FakeHostError(AntHillError):
    """Fake 宿主交付失败。"""


class FakeHostBridge(InteractiveAgentBridge):
    """可分别控制空闲、交付完成和失败的最小宿主。"""

    def __init__(
        self,
        layout: NodeLayout,
        *,
        answers: dict[str, str] | None = None,
        fail_ids: set[str] | None = None,
        blocked_ids: set[str] | None = None,
    ) -> None:
        super().__init__(
            layout=layout,
            agent=AGENT,
            log=EventLog(None, echo=False),
            event_prefix="fake.bridge",
            host_name="Fake Host",
            poll_interval=0.005,
        )
        self.answers = answers or {}
        self.fail_ids = fail_ids or set()
        self.blocked_ids = blocked_ids or set()
        self.available = asyncio.Event()
        self.available.set()
        self.release_delivery = asyncio.Event()
        self.delivery_started = asyncio.Event()
        self.delivery_finished = asyncio.Event()
        self.wait_calls = 0
        self.attempts: Counter[str] = Counter()
        self.delivered: list[str] = []
        self.completed: list[str] = []

    async def wait_until_available(self, stop: asyncio.Event) -> None:
        self.wait_calls += 1
        while not stop.is_set() and not self.available.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self.available.wait(), timeout=0.01)

    async def deliver(self, message: InboxMessage, stop: asyncio.Event) -> HostTurn | None:
        self.attempts[message.id] += 1
        self.delivered.append(message.id)
        self.delivery_started.set()
        if message.id in self.fail_ids:
            raise FakeHostError(f"{message.id} failed")
        while (
            message.id in self.blocked_ids
            and not self.release_delivery.is_set()
            and not stop.is_set()
        ):
            with suppress(TimeoutError):
                await asyncio.wait_for(self.release_delivery.wait(), timeout=0.01)
        if stop.is_set():
            return None
        return HostTurn(id=f"turn-{message.id}", answer=self.answers.get(message.id, ""))

    def after_delivery(self, message: InboxMessage, turn: HostTurn) -> None:
        self.completed.append(message.id)
        self.delivery_finished.set()


def seed_message(
    layout: NodeLayout,
    message_id: str,
    *,
    kind: str = "chat",
    needs_reply: bool,
    body: str = "请处理",
) -> tuple[BridgeHandler, Path]:
    handler = BridgeHandler(root=layout.agent_dir(AGENT), agent_name=AGENT)
    path = handler.dir("inbox") / f"{message_id}.md"
    path.write_text(
        "---\n"
        "from: node:sender\n"
        f"to: node:{AGENT}\n"
        f"type: {kind}\n"
        f"needs_reply: {str(needs_reply).lower()}\n"
        "thread: thread-1\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    if needs_reply:
        (handler.dir("pending") / f"{message_id}.json").write_text("{}", encoding="utf-8")
    return handler, path


@asynccontextmanager
async def running(bridge: InteractiveAgentBridge) -> AsyncIterator[None]:
    stop = asyncio.Event()
    task = asyncio.create_task(bridge.run(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_inbox_messages_are_delivered_in_filename_order(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    later = "01KZ000000000000000000AAAB"
    earlier = "01KZ000000000000000000AAAA"
    seed_message(layout, later, kind="task.result", needs_reply=False)
    seed_message(layout, earlier, kind="task.result", needs_reply=False)
    bridge = FakeHostBridge(layout)

    async with running(bridge):
        await wait_until(lambda: len(bridge.completed) == 2)

    assert bridge.delivered == [earlier, later]


@pytest.mark.asyncio
async def test_delivery_waits_for_the_host_to_become_available(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    seed_message(layout, message_id, kind="task.result", needs_reply=False)
    bridge = FakeHostBridge(layout)
    bridge.available.clear()

    async with running(bridge):
        await wait_until(lambda: bridge.wait_calls == 1)
        assert bridge.delivered == []
        bridge.available.set()
        await wait_until(lambda: bridge.completed == [message_id])


@pytest.mark.asyncio
async def test_reply_required_writes_the_final_answer_to_the_matching_outbox(
    tmp_path: Path,
) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    handler, source = seed_message(layout, message_id, needs_reply=True)
    bridge = FakeHostBridge(layout, answers={message_id: "  已经处理完成。\n"})
    reply = handler.dir("outbox") / source.name

    async with running(bridge):
        await wait_until(reply.is_file)

    assert reply.read_text(encoding="utf-8") == "已经处理完成。"


@pytest.mark.asyncio
async def test_notification_is_delivered_then_acked_without_an_outbox(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    handler, source = seed_message(
        layout, message_id, kind="task.result", needs_reply=False, body="任务已完成"
    )
    bridge = FakeHostBridge(layout)

    async with running(bridge):
        await wait_until(lambda: (handler.dir("done") / source.name).is_file())

    assert bridge.delivered == [message_id]
    assert not (handler.dir("outbox") / source.name).exists()
    assert not source.exists()


@pytest.mark.asyncio
async def test_no_reply_sentinel_silently_acks_a_chat(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    handler, source = seed_message(layout, message_id, kind="chat", needs_reply=True)
    bridge = FakeHostBridge(layout, answers={message_id: NO_REPLY_SENTINEL})

    async with running(bridge):
        await wait_until(lambda: (handler.dir("done") / source.name).is_file())

    assert not (handler.dir("outbox") / source.name).exists()
    assert (handler.dir("done") / f"{source.stem}.json").is_file()


@pytest.mark.asyncio
async def test_no_reply_sentinel_does_not_suppress_a_task_result(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    handler, source = seed_message(layout, message_id, kind="task.request", needs_reply=True)
    bridge = FakeHostBridge(layout, answers={message_id: NO_REPLY_SENTINEL})
    reply = handler.dir("outbox") / source.name

    async with running(bridge):
        await wait_until(reply.is_file)

    assert reply.read_text(encoding="utf-8") == NO_REPLY_SENTINEL
    assert source.is_file()


@pytest.mark.asyncio
async def test_failed_message_is_attempted_once_and_does_not_block_later_mail(
    tmp_path: Path,
) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    failed = "01KZ000000000000000000AAAA"
    succeeds = "01KZ000000000000000000AAAB"
    failed_handler, failed_source = seed_message(
        layout, failed, kind="task.result", needs_reply=False
    )
    _, success_source = seed_message(layout, succeeds, kind="task.result", needs_reply=False)
    bridge = FakeHostBridge(layout, fail_ids={failed})

    async with running(bridge):
        await wait_until(lambda: (failed_handler.dir("done") / success_source.name).is_file())
        await asyncio.sleep(0.03)

    assert bridge.attempts == Counter({failed: 1, succeeds: 1})
    assert bridge.delivered == [failed, succeeds]
    assert failed_source.is_file()


@pytest.mark.asyncio
async def test_existing_manual_outbox_skips_delivery_and_does_not_block_later_mail(
    tmp_path: Path,
) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    manual = "01KZ000000000000000000AAAA"
    later = "01KZ000000000000000000AAAB"
    handler, manual_source = seed_message(layout, manual, needs_reply=True)
    _, later_source = seed_message(layout, later, kind="task.result", needs_reply=False)
    manual_reply = handler.dir("outbox") / manual_source.name
    manual_reply.write_text("人工回复", encoding="utf-8")
    bridge = FakeHostBridge(layout, answers={manual: "自动回复"})

    async with running(bridge):
        await wait_until(lambda: (handler.dir("done") / later_source.name).is_file())

    assert bridge.delivered == [later]
    assert manual_reply.read_text(encoding="utf-8") == "人工回复"


@pytest.mark.asyncio
async def test_manual_outbox_created_during_delivery_wins_the_race(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    message_id = "01KZ000000000000000000AAAA"
    handler, source = seed_message(layout, message_id, needs_reply=True)
    bridge = FakeHostBridge(
        layout,
        answers={message_id: "自动回复"},
        blocked_ids={message_id},
    )
    manual_reply = handler.dir("outbox") / source.name

    async with running(bridge):
        await wait_until(bridge.delivery_started.is_set)
        manual_reply.write_text("人工回复", encoding="utf-8")
        bridge.release_delivery.set()
        await wait_until(bridge.delivery_finished.is_set)

    assert bridge.attempts[message_id] == 1
    assert manual_reply.read_text(encoding="utf-8") == "人工回复"
