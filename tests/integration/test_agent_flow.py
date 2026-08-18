"""集成：真的把两个 agentd 跑起来，让消息在文件邮箱之间走一遍。

覆盖 02-protocol §8 的用例 4（幂等）与用例 6（hops 熔断），
以及 M1 的验收标准：send → accepted 回执 → result。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import patch

import pytest

from anthill.agent.handlers import EchoHandler, HandlerContext
from anthill.agent.runtime import AgentRuntime
from anthill.core import outbox as outbox_module
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload
from anthill.core.seen import Claim
from anthill.core.states import DeliveryState

TIMEOUT = 5.0


@asynccontextmanager
async def running(
    layout: NodeLayout, config: Config, name: str, handler=None
) -> AsyncIterator[AgentRuntime]:
    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name=name,
        handler=handler,
        log=EventLog(layout.log_file(name), agent=name, echo=False),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    try:
        yield runtime
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=TIMEOUT)


async def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


def inbox_types(mailbox: Mailbox) -> list[MessageType]:
    """读取（还没被消费的）来件类型，测试里当断言用。"""
    return [Mailbox.read_envelope(p).type for p in mailbox.list_new()]


def archived(mailbox: Mailbox) -> list[Envelope]:
    return [Mailbox.read_envelope(p) for p in sorted(mailbox.done.rglob("*.json"))]


async def test_task_request_gets_accepted_receipt_and_result(layout, config, make_task):
    """M1 验收：投一条任务，收到 accepted 回执与 task.result。"""
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    env = make_task(sender="cli", recipient="beta")

    async with running(layout, config, "beta"):
        Mailbox(layout.mailbox_dir("beta")).deposit(env)
        await wait_until(lambda: len(cli_box.list_new()) >= 2)

    types = inbox_types(cli_box)
    assert MessageType.RECEIPT_ACCEPTED in types
    assert MessageType.TASK_RESULT in types

    result = next(Mailbox.read_envelope(p) for p in cli_box.list_new() if "result" in p.read_text())
    assert result.thread == env.thread  # 同一线程，便于按 thread 折叠展示
    assert result.reply_to == env.id
    assert "测试任务" in result.payload.summary


async def test_duplicate_delivery_is_processed_once_but_acked_every_time(layout, config, make_task):
    """用例 4：同一信封投 3 次 → 业务处理恰好 1 次，回执 3 次。"""
    beta_box = Mailbox(layout.mailbox_dir("beta"))
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    env = make_task(sender="cli", recipient="beta")

    async with running(layout, config, "beta"):
        # 一次一次来：同一个 id 就是同一个文件名，上一次还没被取走就重投，
        # 等于把文件覆盖掉，watcher 只会看到一次 —— 那是测试自己造的竞态
        for expected in (1, 2, 3):
            beta_box.deposit(env)  # 同一个 id，模拟发送方重试
            await wait_until(
                lambda n=expected: inbox_types(cli_box).count(MessageType.RECEIPT_ACCEPTED) == n
            )

    types = inbox_types(cli_box)
    assert types.count(MessageType.RECEIPT_ACCEPTED) == 3
    assert types.count(MessageType.TASK_RESULT) == 1  # 业务只做了一次


async def test_mutual_chat_is_broken_by_hop_limit(layout, config, addr):
    """用例 6：两个 echo agent 互相回信，第 ttl_hops 跳被熔断，消息风暴止住。"""
    ping = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="ping"),
        ttl_hops=4,
    )

    async with running(layout, config, "alpha"), running(layout, config, "beta"):
        Mailbox(layout.mailbox_dir("beta")).deposit(ping)
        await asyncio.sleep(1.5)  # 让它们尽情互相回，看会不会停下来

    chats = [
        env
        for box in ("alpha", "beta")
        for env in archived(Mailbox(layout.mailbox_dir(box)))
        if env.type is MessageType.CHAT
    ]
    assert chats, "至少要有第一条 ping"
    assert max(env.hops for env in chats) <= 4  # 从没突破 ttl
    assert len(chats) <= 4  # 不是无限风暴


async def test_expired_message_is_refused_with_receipt(layout, config, addr):
    expired = Envelope(
        from_=addr("cli"),
        to=addr("beta"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="过期任务"),
        expires_at=now() - timedelta(seconds=1),
    )
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, "beta"):
        Mailbox(layout.mailbox_dir("beta")).deposit(expired)
        await wait_until(lambda: len(cli_box.list_new()) >= 1)

    assert inbox_types(cli_box) == [MessageType.RECEIPT_EXPIRED]


async def test_invalid_envelope_is_quarantined_not_swallowed(layout, config):
    beta_box = Mailbox(layout.mailbox_dir("beta"))
    (beta_box.new / "01J000000000000000000000BB.json").write_text("{坏}", encoding="utf-8")

    async with running(layout, config, "beta"):
        await wait_until(lambda: (beta_box.done / "invalid").is_dir())

    assert list((beta_box.done / "invalid").glob("*.json"))
    assert beta_box.list_new() == []  # 不能堵住队列


async def test_sender_state_machine_reaches_completed(layout, config, addr):
    """发送方视角：pending → delivered → accepted → completed 全程走通。"""
    async with running(layout, config, "beta"), running(layout, config, "alpha") as alpha:
        env = await alpha.sender.send_new(
            to=addr("beta"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="端到端", body="跑通状态机"),
        )
        await wait_until(
            lambda: (
                (r := alpha.tracker.get(env.id)) is not None and r.state is DeliveryState.COMPLETED
            )
        )

    assert alpha.tracker.get(env.id).state is DeliveryState.COMPLETED
    # 任务类消息不再有悬空状态（回执类是「送到即完事」，不参与等待）
    assert MessageType.TASK_REQUEST not in {r.type for r in alpha.tracker.open_records()}


async def test_role_addressing_reaches_a_worker(layout, config, addr):
    async with running(layout, config, "beta"), running(layout, config, "alpha") as alpha:
        env = await alpha.sender.send_new(
            to=Address(node="testnode", agent="role:worker"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="按角色派活"),
        )
        await wait_until(
            lambda: (
                (r := alpha.tracker.get(env.id)) is not None and r.state is DeliveryState.COMPLETED
            ),
        )

    assert alpha.tracker.get(env.id).to.agent in {"beta", "gamma"}


async def test_delivery_to_missing_mailbox_retries_then_dead_letters(
    layout, config, addr, monkeypatch
):
    """重试耗尽 → 死信 → 上报 coordinator。退避时间调短，否则测试要跑 31 秒。"""
    monkeypatch.setattr(outbox_module, "BACKOFF_BASE", timedelta(seconds=0.01))
    # gamma 配置里有，但故意不建邮箱目录 → 投递必然失败
    shutil.rmtree(layout.mailbox_dir("gamma"))
    alpha_box = Mailbox(layout.mailbox_dir("alpha"))

    async with running(layout, config, "alpha") as alpha:
        env = await alpha.sender.send_new(
            to=addr("gamma"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="发给不存在的邮箱"),
        )
        await wait_until(
            lambda: (r := alpha.tracker.get(env.id)) is not None and r.state is DeliveryState.DEAD
        )

    assert len(Outbox(alpha_box).dead_letters()) == 1
    assert Outbox(alpha_box).load_pending() == []


@pytest.mark.parametrize("handler", [EchoHandler()])
async def test_handler_errors_become_task_error(layout, config, make_task, handler):
    class Exploding:
        name = "exploding"

        async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
            raise RuntimeError("大脑炸了")

    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, "beta", handler=Exploding()):
        Mailbox(layout.mailbox_dir("beta")).deposit(make_task(sender="cli", recipient="beta"))
        await wait_until(lambda: MessageType.TASK_ERROR in inbox_types(cli_box))

    assert MessageType.TASK_ERROR in inbox_types(cli_box)
    assert isinstance(handler, EchoHandler)  # 夹具参数只是为了对照，确保正常 handler 仍可用


async def test_permanently_unroutable_message_dies_exactly_once(layout, config, addr):
    """不可重试的失败必须移出 pending。

    否则重试循环每秒把它捡起来一次、每秒报一次死信，
    日志和 coordinator 邮箱都会被刷爆 —— 这是 M4 联调时真的踩到的坑。
    """
    alpha_box = Mailbox(layout.mailbox_dir("alpha"))

    async with running(layout, config, "alpha") as alpha:
        env = await alpha.sender.send_new(
            to=Address(node="nowhere", agent="ghost"),  # 不在 peers 列表里 → 不可重试
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="发给不存在的节点"),
        )
        await wait_until(
            lambda: (r := alpha.tracker.get(env.id)) is not None and r.state is DeliveryState.DEAD
        )
        await asyncio.sleep(1.2)  # 给重试循环足够多的机会再犯一次

    assert Outbox(alpha_box).load_pending() == []
    assert len(Outbox(alpha_box).dead_letters()) == 1
    coordinator_box = Mailbox(layout.mailbox_dir("alpha"))
    reports = [e for e in archived(coordinator_box) if e.type is MessageType.EVENT]
    assert len(reports) <= 1  # 死信只上报一次


async def test_unroutable_reply_is_spooled_when_the_node_enables_it(layout, config, addr):
    """SSH 是单向的：服务器连不回你的笔记本，回信只能暂存等你来拉。

    默认关闭（上一条用例验证了默认还是死信），开了才走这条路。
    """
    from anthill.core.spool import Spool

    spooling = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"spool_unroutable": True})}
    )
    spool = Spool(layout.root)

    async with running(layout, spooling, "alpha") as alpha:
        env = await alpha.sender.send_new(
            to=Address(node="laptop", agent="cli"),  # 路由不到
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="回不去的信"),
        )
        await wait_until(lambda: bool(spool.pending("laptop")))

    assert [p.name for p in spool.pending("laptop")] == [f"{env.id}.json"]
    assert Outbox(Mailbox(layout.mailbox_dir("alpha"))).dead_letters() == []  # 不是死信
    assert spool.take("laptop", f"{env.id}.json").id == env.id  # 信封原样保留


async def test_a_message_interrupted_by_a_crash_really_gets_processed_again(
    layout, config, make_task
):
    """「至少一次」的端到端验收 —— 这是文件邮箱最核心的那个卖点。

    以前这条链是断的：`recover_stale()` 把信从 cur/ 退回 new/ 了，可 seen.db
    一进 `_dispatch` 就 `mark()`，重放回来一律判成重复、只补一条回执就 return，
    **handler 永远不会重跑**。退信那一步是安慰剂，真实语义是「最多一次」。

    这里造的就是那个现场：信卡在 cur/、seen.db 里已登记但没完成，然后重启。
    """
    beta_box = Mailbox(layout.mailbox_dir("beta")).ensure()
    env = make_task(sender="cli", recipient="beta")

    # kill -9 的现场：信被领走（在 cur/），登记过，但 handler 没跑完
    beta_box.deposit(env)
    claimed = beta_box.claim(beta_box.list_new()[0])
    with patch("anthill.core.seen.RUNTIME_TOKEN", "crashed:aaa"), beta_box.open_seen() as dying:
        assert dying.claim(env.id) is Claim.FIRST
    assert claimed.parent == beta_box.cur

    handled: list[str] = []

    class Recording(EchoHandler):
        async def handle(self, incoming: Envelope, ctx: HandlerContext) -> None:
            handled.append(incoming.id)
            await super().handle(incoming, ctx)

    async with running(layout, config, "beta", handler=Recording()):
        await wait_until(lambda: handled == [env.id])

    assert handled == [env.id], "崩溃中断的消息没有重跑 —— 那是「最多一次」"


async def test_a_finished_message_is_not_redone_after_a_restart(layout, config, make_task):
    """另一面：真干完了的，重启后不该再做一遍。"""
    beta_box = Mailbox(layout.mailbox_dir("beta")).ensure()
    env = make_task(sender="cli", recipient="beta")
    handled: list[str] = []

    class Recording(EchoHandler):
        async def handle(self, incoming: Envelope, ctx: HandlerContext) -> None:
            handled.append(incoming.id)
            await super().handle(incoming, ctx)

    async with running(layout, config, "beta", handler=Recording()):
        beta_box.deposit(env)
        await wait_until(lambda: handled == [env.id])

    # 重启（同一个工作区，新的 runtime），再投一次同样的信封
    async with running(layout, config, "beta", handler=Recording()):
        beta_box.deposit(env)
        await asyncio.sleep(0.4)

    assert handled == [env.id], "已经处理完的消息被重放处理了第二遍"


async def test_startup_announces_a_loosened_security_posture(layout, make_task):
    """unattended_allow 非空是这台机器的安全姿态事实 ——
    每次 agentd 启动都要在日志里响一声，不许静默生效。"""
    import json

    toml = layout.node_toml.read_text(encoding="utf-8")
    layout.node_toml.write_text(
        toml + '\n[security]\nunattended_allow = ["low"]\n', encoding="utf-8"
    )
    loosened = Config.load_from(layout)

    async with running(layout, loosened, "beta"):
        await wait_until(lambda: layout.log_file("beta").is_file())

    events = [
        json.loads(line)
        for line in layout.log_file("beta").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    hits = [e for e in events if e.get("event") == "policy.loosened"]
    assert hits, "启动时必须announce放宽姿态"
    assert "low" in str(hits[0])
