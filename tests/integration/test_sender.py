"""Sender 的并发与重试行为。

这里的第一个用例来自一次真实的手工联调：agentd 跑起来后日志里出现了
`delivery.retry attempts=0` 和 `非法状态迁移：delivered → delivered` ——
重试循环把还在投递中的消息又抢发了一遍。
"""

from __future__ import annotations

import asyncio
import multiprocessing
from datetime import timedelta
from pathlib import Path

import pytest

from anthill.agent.sender import Sender
from anthill.core import outbox as outbox_module
from anthill.core.config import Config, PeerSection
from anthill.core.envelope import Address, Envelope, TransportKind
from anthill.core.errors import MailboxError, UnknownRecipient, UnroutableNode
from anthill.core.ids import new_id
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, ReceiptPayload, TaskRequestPayload
from anthill.core.router import Router
from anthill.core.states import DeliveryState, DeliveryTracker
from anthill.core.workspace import create_workspace
from anthill.transport.base import DeliveryResult, Destination
from anthill.transport.registry import TransportRegistry
from anthill.web.workspaces import remember


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


class UnroutableTransports(TransportRegistry):
    def __init__(self, config, layout) -> None:
        super().__init__(config, layout)
        self.calls: list[str] = []

    async def deliver(self, env):  # type: ignore[override]
        self.calls.append(env.id)
        raise UnroutableNode("测试不可路由")


def _cross_process_send(workspace: str, raw_env: str, marker: str) -> None:
    """spawn 子进程入口：两边故意没有共享 Python 对象，只共享磁盘邮箱/内核锁。"""

    async def run() -> None:
        layout = NodeLayout(Path(workspace))
        config = Config.load_from(layout)
        env = Envelope.model_validate_json(raw_env)

        class MarkingTransport:
            async def deliver(self, item: Envelope) -> DeliveryResult:
                with Path(marker).open("a", encoding="utf-8") as handle:
                    handle.write(f"{item.id}\n")
                await asyncio.sleep(0.5)
                return DeliveryResult.success(
                    TransportKind.LOCAL,
                    Destination(node=item.to.node, agent=item.to.agent),
                    "/dev/null",
                )

        sender = Sender(
            identity=Address(node=config.node.name, agent="alpha"),
            mailbox=Mailbox(layout.mailbox_dir("alpha")).ensure(),
            router=Router(config, layout),
            transports=MarkingTransport(),  # type: ignore[arg-type]
            tracker=DeliveryTracker(),
            log=EventLog(None, agent="alpha", echo=False),
        )
        await sender.send(env)

    asyncio.run(run())


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


async def test_an_independent_sender_cannot_steal_another_process_delivery_lease(
    layout, config, addr, make_sender
):
    """CLI 和 agentd 各有自己的 Sender/_inflight；真正的互斥必须落到内核锁。"""
    transports = SlowTransports(config, layout)
    cli_sender, _, _ = make_sender(transports)
    agentd_sender, _, _ = make_sender(transports)

    send = asyncio.create_task(
        cli_sender.send_new(
            to=addr("beta"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="跨进程也别抢"),
        )
    )
    await asyncio.sleep(0.05)
    await agentd_sender.retry_due()
    env = await send

    assert transports.calls == [env.id]


async def test_two_independent_senders_of_the_same_envelope_converge_on_one_transport(
    layout, config, addr, make_sender
):
    transports = SlowTransports(config, layout)
    first, _, _ = make_sender(transports)
    second, second_tracker, mailbox = make_sender(transports)
    env = first.prepare_new(
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="同一信封"),
    )

    first_send = asyncio.create_task(first.send(env))
    await asyncio.sleep(0.05)
    first_result, second_result = await asyncio.gather(first_send, second.send(env))

    assert transports.calls == [env.id]
    assert first_result[0].ok
    assert second_result[0].ok
    assert second_tracker.get(env.id).state is DeliveryState.DELIVERED
    assert Outbox(mailbox).load_pending() == []


def test_two_real_processes_share_one_delivery_lease(layout, config, addr, tmp_path):
    mailbox = Mailbox(layout.mailbox_dir("alpha")).ensure()
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="真实跨进程只投一次"),
    )
    marker = tmp_path / "transport-calls.txt"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_cross_process_send,
            args=(str(layout.workspace), env.model_dump_json(by_alias=True), str(marker)),
        )
        for _ in range(2)
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)

        assert [process.exitcode for process in processes] == [0, 0]
        assert marker.read_text(encoding="utf-8").splitlines() == [env.id]
        assert Outbox(mailbox).load_pending() == []
        assert (mailbox.sent / f"{env.id}.json").is_file()
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=2)


async def test_same_id_with_different_content_cannot_race_past_sent_collision_check(
    layout, config, addr, make_sender
):
    transports = SlowTransports(config, layout)
    first, _, mailbox = make_sender(transports)
    second, _, _ = make_sender(transports)
    env = first.prepare_new(
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="原正文"),
    )
    collision = env.model_copy(update={"payload": ChatPayload(body="不同正文")})

    first_send = asyncio.create_task(first.send(env))
    await asyncio.sleep(0.05)
    with pytest.raises(MailboxError, match="另一份不同信封"):
        await second.send(collision)
    await first_send

    assert transports.calls == [env.id]
    assert Outbox(mailbox).load_pending() == []


async def test_competing_sender_observes_failure_without_bypassing_backoff(
    layout, config, addr, make_sender
):
    transports = SlowTransports(config, layout, delay=0.1, ok=False)
    first, _, mailbox = make_sender(transports)
    second, _, _ = make_sender(transports)
    env = first.prepare_new(
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="失败后要退避"),
    )

    first_send = asyncio.create_task(first.send(env))
    await asyncio.sleep(0.02)
    first_result, second_result = await asyncio.gather(first_send, second.send(env))

    pending = Outbox(mailbox).load_pending()
    assert transports.calls == [env.id]
    assert not first_result[0].ok
    assert not second_result[0].ok
    assert pending[0].attempts == 1
    assert not pending[0].is_due()

    await second.retry_due()
    assert transports.calls == [env.id]


async def test_dead_report_in_the_same_lock_bucket_is_sent_after_releasing_source_lease(
    layout, config, addr, monkeypatch: pytest.MonkeyPatch
):
    transports = UnroutableTransports(config, layout)
    mailbox = Mailbox(layout.mailbox_dir("alpha")).ensure()
    outbox = Outbox(mailbox)
    sender = Sender(
        identity=addr("alpha"),
        mailbox=mailbox,
        router=Router(config, layout),
        transports=transports,
        tracker=DeliveryTracker(),
        log=EventLog(None, agent="alpha", echo=False),
        coordinator="beta",
    )
    env = sender.prepare_new(
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="触发死信"),
    )
    source_bucket = outbox.delivery_lock(env.id).path
    report_id = next(
        candidate
        for _ in range(4096)
        if outbox.delivery_lock(candidate := new_id()).path == source_bucket
    )
    original_new = Envelope.new

    def force_report_id(cls, **kwargs):
        return original_new(**kwargs).model_copy(update={"id": report_id})

    monkeypatch.setattr(Envelope, "new", classmethod(force_report_id))

    results = await asyncio.wait_for(sender.send(env), timeout=1.0)

    assert not results[0].ok
    assert transports.calls == [env.id, report_id]
    assert {path.stem for path in outbox.dead_letters()} == {env.id, report_id}


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


async def test_sending_a_persisted_envelope_again_does_not_redeliver_it(
    layout, config, addr, make_sender
):
    transports = SlowTransports(config, layout, delay=0.0)
    sender, tracker, _ = make_sender(transports)
    env = sender.prepare_new(
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="同一条"),
    )

    await sender.send(env)
    tracker.mark(env.id, DeliveryState.ACCEPTED)
    await sender.send(env)

    assert transports.calls == [env.id]
    assert tracker.get(env.id).state is DeliveryState.ACCEPTED


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


async def test_registered_workspace_on_this_machine_uses_local_transport(
    tmp_path, layout, config, addr, make_sender
):
    """机器清单里的另一个工作区不是远端 peer，不需要配对。"""
    remote_layout = NodeLayout(tmp_path / "data-system")
    remote_config = create_workspace(remote_layout, node_name="data-system")
    remember(layout.workspace, port=0)
    remember(remote_layout.workspace, port=0)
    transports = TransportRegistry(config, layout)
    sender, tracker, _ = make_sender(transports)

    env = await sender.send_new(
        to=addr("echo", node=remote_config.node.name),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="同机跨工作区"),
    )

    delivered = Mailbox(remote_layout.mailbox_dir("echo")).list_new()
    assert [Mailbox.read_envelope(path).id for path in delivered] == [env.id]
    assert tracker.get(env.id).state is DeliveryState.DELIVERED
    assert transports.destination_for(env).local_workspace == remote_layout.workspace


def test_a_registered_workspace_cannot_silently_shadow_the_current_node(
    tmp_path, layout, config, make_task
) -> None:
    duplicate = NodeLayout(tmp_path / "duplicate")
    create_workspace(duplicate, node_name=config.node.name)
    remember(duplicate.workspace, port=0)

    with pytest.raises(UnroutableNode, match="其它工作区都叫"):
        TransportRegistry(config, layout).destination_for(make_task())


def test_a_local_workspace_cannot_silently_shadow_a_peer(tmp_path, layout, config, addr) -> None:
    local = NodeLayout(tmp_path / "local-lab")
    create_workspace(local, node_name="lab")
    remember(local.workspace, port=0)
    ambiguous = config.model_copy(
        update={"peers": {"lab": PeerSection(transport=TransportKind.LAN, endpoint="http://lab")}}
    )
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("echo", node="lab"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="该投给谁"),
    )

    with pytest.raises(UnroutableNode, match=r"本机工作区.*peer"):
        TransportRegistry(ambiguous, layout).destination_for(env)
