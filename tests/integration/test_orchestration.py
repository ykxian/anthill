"""集成：多 Agent 编排（M3 验收，演示场景 A）。

三个 agentd 同时在跑：coordinator 拆计划派活，coder 写，reviewer 审。
全程用 FakeProvider，所以这条链路能在 CI 里天天跑而不花一分钱。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

from anthill.agent.runtime import AgentRuntime
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import ProviderError
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload
from anthill.orchestrator.board import BOARD_FILE
from anthill.orchestrator.coordinator import CoordinatorHandler, CoordinatorSettings
from anthill.orchestrator.state import RunStore, StepState
from anthill.providers.base import ToolCall, Turn
from anthill.providers.fake import FakeProvider
from anthill.security.approvals import ApprovalStore

TIMEOUT = 10.0

NODE_TOML = """
[node]
name = "testnode"
workspace = "."

[runtime]
poll_interval = 0.05
watch_mode = "poll"

[security]
confirm_high_risk = false

[providers.fakeprov]
kind = "openai_compat"
api_key_env = "ANTHILL_TEST_KEY"
model = "fake-model"

[agents.cli]
role = "user"

[agents.boss]
role = "coordinator"
provider = "fakeprov"

[agents.coder]
role = "worker"
provider = "fakeprov"
tools = ["read_file", "write_file", "send_message", "finish"]

[agents.reviewer]
role = "reviewer"
provider = "fakeprov"
tools = ["read_file", "send_message", "finish"]
"""

PLAN = {
    "goal": "为 date.py 补单测并通过审查",
    "steps": [
        {"id": "s1", "assignee": "coder", "task": "写测试", "depends_on": []},
        {"id": "s2", "assignee": "role:reviewer", "task": "审查 s1 的产物", "depends_on": ["s1"]},
    ],
    "done_when": "reviewer 认可且测试可运行",
}


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_TEST_KEY", "sk-test-not-used")


@pytest.fixture
def node(layout: NodeLayout) -> Config:
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "boss", "coder", "reviewer"):
        Mailbox(layout.mailbox_dir(name)).ensure()
    # `finish` 现在会校验产物真的存在 —— 脚本里声称交付的文件得先在磁盘上。
    # 这本来就更诚实：worker 说它交付了什么，那东西就该找得到。
    (layout.workspace / "tests").mkdir(parents=True, exist_ok=True)
    (layout.workspace / "tests" / "test_date.py").write_text("# 写好的用例\n", encoding="utf-8")
    return Config.load_from(layout)


def plan_turn(plan: dict[str, object] | None = None) -> Turn:
    return Turn(text=json.dumps(plan or PLAN, ensure_ascii=False))


def verdict_turn(satisfied: bool = True, fix: str = "") -> Turn:
    return Turn(
        text=json.dumps(
            {
                "satisfied": satisfied,
                "reason": "看起来达成了" if satisfied else "还差点",
                "fix": fix,
            },
            ensure_ascii=False,
        )
    )


def finish_turn(summary: str, artifacts: tuple[str, ...] = ()) -> Turn:
    """`finish` 现在会校验产物真的存在，所以脚本里声称的文件得先造出来 ——
    这本来就是更诚实的：worker 声称交付了什么，那东西就该在磁盘上。"""
    return Turn(
        tool_calls=(
            ToolCall(
                id="c1",
                name="finish",
                arguments={"summary": summary, "artifacts": list(artifacts)},
            ),
        )
    )


@asynccontextmanager
async def running(
    layout: NodeLayout, config: Config, name: str, handler: object, tick: float = 0.1
) -> AsyncIterator[AgentRuntime]:
    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name=name,
        handler=handler,  # type: ignore[arg-type]
        log=EventLog(layout.log_file(name), agent=name, echo=False),
        tick_interval=tick,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    try:
        yield runtime
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=TIMEOUT)


def worker_handler(layout: NodeLayout, config: Config, name: str, provider: FakeProvider) -> object:
    from anthill.agent.context import ContextBuilder
    from anthill.agent.llm_handler import LlmHandler
    from anthill.agent.tools.registry import build_toolset
    from anthill.orchestrator.board import Blackboard
    from anthill.security.policy import PolicyEngine

    agent = config.agent(name)
    tools = build_toolset(agent.tools)
    return LlmHandler(
        provider=provider,
        tools=tools,
        policy=PolicyEngine(config.security),
        builder=ContextBuilder(
            agent=agent,
            node=config.node.name,
            tools=tools,
            board_summary=Blackboard(layout.blackboard).summary,
        ),
        max_steps=agent.max_steps,
        token_budget=agent.token_budget,
    )


def coordinator_handler(
    layout: NodeLayout, provider: FakeProvider, **settings: float
) -> CoordinatorHandler:
    from anthill.orchestrator.board import Blackboard

    return CoordinatorHandler(
        provider=provider,
        blackboard=Blackboard(layout.blackboard),
        settings=CoordinatorSettings(**settings),  # type: ignore[arg-type]
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


def user_task(title: str = "补单测") -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="boss"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title=title, body="给 date.py 补单测并让 reviewer 过一遍"),
    )


def finals_in(mailbox: Mailbox) -> list[Envelope]:
    return [
        env
        for path in mailbox.list_new()
        if (env := Mailbox.read_envelope(path)).type
        in (MessageType.TASK_RESULT, MessageType.TASK_ERROR)
    ]


# ---------- 场景 A：全流程 ----------


async def test_scenario_a_runs_end_to_end_without_human_intervention(
    layout: NodeLayout, node: Config
) -> None:
    # Arrange：boss 先出计划、最后判 done_when；coder 与 reviewer 各交付一次
    boss = FakeProvider([plan_turn(), verdict_turn(satisfied=True)])
    coder = FakeProvider([finish_turn("写了 12 个用例", ("tests/test_date.py",))])
    reviewer = FakeProvider([finish_turn("覆盖了边界，approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert：用户拿到一份汇总了两步交付的结果
    final = finals_in(cli_box)[0]
    assert final.type is MessageType.TASK_RESULT
    assert final.payload.status == "ok"
    assert "12 个用例" in final.payload.summary
    assert "approve" in final.payload.summary
    assert final.payload.artifacts == ("tests/test_date.py",)


async def test_dependent_step_receives_upstream_deliverables(
    layout: NodeLayout, node: Config
) -> None:
    """reviewer 必须看得到 coder 交付了什么，否则它根本不知道要审什么。"""
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([finish_turn("写了 12 个用例", ("tests/test_date.py",))])
    reviewer = FakeProvider([finish_turn("approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    prompt = reviewer.calls[0].messages[-1].content
    assert "12 个用例" in prompt
    assert "tests/test_date.py" in prompt


async def test_each_step_gets_its_own_thread(layout: NodeLayout, node: Config) -> None:
    """子 thread 隔离：父 thread 只挂计划与汇总，两步之间不共享上下文。"""
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([finish_turn("done")])
    reviewer = FakeProvider([finish_turn("approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    task = user_task()

    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(task)
        await wait_until(lambda: bool(finals_in(cli_box)))

    state = RunStore(layout.blackboard).all()[0]
    threads = {r.thread for r in state.steps}
    assert len(threads) == 2
    assert task.thread not in threads
    assert finals_in(cli_box)[0].thread == task.thread  # 汇总回到用户原来的线程


async def test_board_shows_live_steps_then_collapses_when_the_run_ends(
    layout: NodeLayout, node: Config
) -> None:
    """黑板是「当前在干什么」的快照：跑的时候列步骤，收工后只留一行结论。

    它会被注进每个 Agent 的上下文，所以已完成的任务不能继续占版面。
    """
    # Arrange：coder 迟迟不回，好让我们看到「进行中」的黑板
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([finish_turn("done")])
    reviewer = FakeProvider([finish_turn("approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    board_path = layout.blackboard / BOARD_FILE
    store = RunStore(layout.blackboard)

    # Act
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(store.all()) and bool(store.all()[0].busy_ids))
        in_flight = board_path.read_text(encoding="utf-8")

        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert
    assert PLAN["goal"] in in_flight  # type: ignore[operator]
    assert "s1" in in_flight and "running" in in_flight
    assert "✓" in board_path.read_text(encoding="utf-8")


# ---------- 并发与失败 ----------


async def test_independent_steps_are_dispatched_concurrently(
    layout: NodeLayout, node: Config
) -> None:
    # Arrange：两步互不依赖，应该一次性都派出去
    parallel_plan = {
        "goal": "两件独立的事",
        "steps": [
            {"id": "a", "assignee": "coder", "task": "做 A", "depends_on": []},
            {"id": "b", "assignee": "role:reviewer", "task": "做 B", "depends_on": []},
        ],
        "done_when": "",
    }
    boss = FakeProvider([plan_turn(parallel_plan)])
    coder = FakeProvider([finish_turn("A 好了")])
    reviewer = FakeProvider([finish_turn("B 好了")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert：done_when 为空时不再多问模型一次，所以 boss 只被调用了一次（出计划）
    assert len(boss.calls) == 1
    assert "A 好了" in finals_in(cli_box)[0].payload.summary
    assert "B 好了" in finals_in(cli_box)[0].payload.summary


async def test_failed_step_stops_the_run_and_reports_to_the_user(
    layout: NodeLayout, node: Config
) -> None:
    # Arrange：coder 的模型一直不收尾 → 步数熔断 → task.error 回到 coordinator
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([Turn(text="", tool_calls=(ToolCall(id="c1", name="list_dir"),))])
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    handler = worker_handler(layout, node, "coder", coder)
    handler._max_steps = 2  # type: ignore[attr-defined]

    # Act
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(running(layout, node, "coder", handler))
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert
    final = finals_in(cli_box)[0]
    assert final.type is MessageType.TASK_ERROR
    assert "s1" in final.payload.error
    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").state is StepState.FAILED
    # s2 依赖 s1，永远等不到了：标 skipped 而不是留在 pending，
    # 否则这次运行永远 all_settled=False，用户就一直等不到任何结果
    assert state.step("s2").state is StepState.SKIPPED
    assert state.step("s2").attempts == 0  # 从没派出去过


async def test_unparseable_plan_becomes_a_task_error_not_silence(
    layout: NodeLayout, node: Config
) -> None:
    boss = FakeProvider([Turn(text="我觉得可以先写测试")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, node, "boss", coordinator_handler(layout, boss)):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    final = finals_in(cli_box)[0]
    assert final.type is MessageType.TASK_ERROR
    assert "拆解任务失败" in final.payload.error


# ---------- 返工与催办 ----------


async def test_unmet_done_when_triggers_one_rework_round(layout: NodeLayout, node: Config) -> None:
    # Arrange：第一次判定不达标 → 追加修复步 → 第二次判定通过
    single = {
        "goal": "把活干完",
        "steps": [{"id": "s1", "assignee": "coder", "task": "做事", "depends_on": []}],
        "done_when": "必须有测试",
    }
    boss = FakeProvider(
        [plan_turn(single), verdict_turn(satisfied=False, fix="补上测试"), verdict_turn(True)]
    )
    coder = FakeProvider([finish_turn("先做了功能"), finish_turn("补上了测试")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running(layout, node, "coder", worker_handler(layout, node, "coder", coder))
        )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert
    state = RunStore(layout.blackboard).all()[0]
    assert state.round == 1
    assert [r.id for r in state.steps] == ["s1", "fix1"]
    assert "补上了测试" in finals_in(cli_box)[0].payload.summary


async def test_silent_worker_is_nudged_then_timed_out(layout: NodeLayout, node: Config) -> None:
    # Arrange：coder 根本不启动，步骤永远收不到回复
    single = {
        "goal": "把活干完",
        "steps": [{"id": "s1", "assignee": "coder", "task": "做事", "depends_on": []}],
        "done_when": "",
    }
    boss = FakeProvider([plan_turn(single)])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act：催办 0.15s、超时 0.5s，让 tick 循环在测试里跑得完
    async with running(
        layout, node, "boss", coordinator_handler(layout, boss, nudge_after=0.15, step_timeout=0.5)
    ):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)), timeout=TIMEOUT)

    # Assert：先催后判失败，且用户拿到明确的失败说明而不是无限等待
    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").nudged
    assert state.step("s1").state is StepState.FAILED
    final = finals_in(cli_box)[0]
    assert final.type is MessageType.TASK_ERROR
    assert "未回复" in final.payload.error
    nudges = [
        Mailbox.read_envelope(p)
        for p in Mailbox(layout.mailbox_dir("coder")).list_new()
        if Mailbox.read_envelope(p).type is MessageType.CHAT
    ]
    assert len(nudges) == 1  # 只催一次，不无限催


# ---------- 崩溃恢复 ----------


async def test_coordinator_resumes_scheduling_after_a_restart(
    layout: NodeLayout, node: Config
) -> None:
    """状态在黑板上，不在内存里 —— 换一个全新的 handler 实例也能接着调度。

    worker 故意在 coordinator 停掉之后才启动：这样「回信堆在 inbox 里等着，
    由一个全新实例接手」才是真的被验证到，而不是撞运气撞出来的。
    """
    # Arrange：第一个 coordinator 只负责派出 s1 然后「下线」
    boss1 = FakeProvider([plan_turn()])
    coder = FakeProvider([finish_turn("写完了")])
    reviewer = FakeProvider([finish_turn("approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    store = RunStore(layout.blackboard)

    async with running(layout, node, "boss", coordinator_handler(layout, boss1)):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        # 等一个**单调**的条件：s1 派出去过。
        # 早先这里等的是「s1 正在跑」，可 worker 可能在第一次轮询前就回完了，
        # 那个条件就永远不会为真 —— 测试自身的竞态，偶发超时。
        await wait_until(lambda: bool(store.all()) and store.all()[0].step("s1").attempts > 0)

    # Act：worker 现在才上线，回信会堆在已停机的 coordinator 邮箱里
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        boss2 = FakeProvider([verdict_turn(satisfied=True)])
        async with running(layout, node, "boss", coordinator_handler(layout, boss2)):
            await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert
    assert finals_in(cli_box)[0].type is MessageType.TASK_RESULT
    assert store.all()[0].finished


# ---------- 点对点 @mention ----------


async def test_worker_can_mention_another_worker_directly(layout: NodeLayout, node: Config) -> None:
    """coder 写完直接 @reviewer，不必经过 coordinator —— 「像个团队」的那一半。"""
    # Arrange：coder 先 send_message 给 reviewer，再 finish
    coder = FakeProvider(
        [
            Turn(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="send_message",
                        arguments={"to": "@reviewer", "body": "帮我看下 test_date.py"},
                    ),
                )
            ),
            finish_turn("写完并已请 reviewer 过目"),
        ]
    )
    reviewer = FakeProvider([finish_turn("看过了，没问题")])
    boss = FakeProvider([plan_turn(), verdict_turn()])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert：reviewer 既收到了 coder 的直接消息，也收到了 coordinator 派的 s2
    seen = [call.messages[-1].content for call in reviewer.calls]
    assert any("帮我看下 test_date.py" in text for text in seen)


async def test_mention_loop_is_broken_by_the_hop_limit(layout: NodeLayout, node: Config) -> None:
    """两个 worker 互相 @ 是最容易构造的消息风暴；熔断在协议层，不靠工具自觉。"""

    # Arrange：两边都只会「收到消息就再 @ 回去」
    def pingpong(target: str) -> FakeProvider:
        return FakeProvider(
            [
                Turn(
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="send_message",
                            arguments={"to": f"@{target}", "body": "轮到你了"},
                        ),
                    )
                ),
                finish_turn("发完了"),
            ]
        )

    coder, reviewer = pingpong("reviewer"), pingpong("coder")
    seed = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="coder"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="开始"),
        ttl_hops=4,
    )

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        Mailbox(layout.mailbox_dir("coder")).deposit(seed)
        await asyncio.sleep(1.5)  # 让它们尽情互相 @，看会不会停下来

    # Assert：链路止于 ttl_hops，没有变成无限风暴
    chats = [
        env
        for name in ("coder", "reviewer")
        for path in (Mailbox(layout.mailbox_dir(name)).done).rglob("*.json")
        if (env := Mailbox.read_envelope(path)).type is MessageType.CHAT
    ]
    assert chats
    assert max(env.hops for env in chats) <= 4
    assert len(chats) <= 6


# ---------- 两个 Agent 真的对话（M7）----------


async def test_two_agents_actually_converse_and_stop_on_their_own(
    layout: NodeLayout, node: Config
) -> None:
    """人起个头并 @ 上第二个人，球就在两个 Agent 之间来回，到轮次上限自己停。

    终止靠的是每个 Agent 的 chat_turns 预算（确定性），
    不是 hops 熔断 —— 那是协议层的兜底，一响就说明出事了。
    """
    # Arrange：两边都只会「接着聊」，不会主动收口
    coder = FakeProvider([Turn(text="我觉得先加日志")])
    reviewer = FakeProvider([Turn(text="同意，再补个测试")])
    seed = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="coder"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="这个 bug 怎么修？", mentions=("reviewer",)),
        ttl_hops=64,  # 故意放开 hops，证明停下来的不是它
    )

    def talker(name: str, provider: FakeProvider) -> object:
        handler = worker_handler(layout, node, name, provider)
        handler._chat_turns = 3  # type: ignore[attr-defined]
        return handler

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(running(layout, node, name, talker(name, provider)))
        Mailbox(layout.mailbox_dir("coder")).deposit(seed)
        await asyncio.sleep(2.5)

    # Assert：确实来回了好几轮，且两边都停在自己的预算上
    chats = [
        env
        for name in ("coder", "reviewer")
        for path in Mailbox(layout.mailbox_dir(name)).done.rglob("*.json")
        if (env := Mailbox.read_envelope(path)).type is MessageType.CHAT
    ]
    assert len(chats) >= 3, "应该真的聊起来了"
    assert max(e.hops for e in chats) < 64, "停下来的不该是 hops"
    # 每人最多开口 chat_turns 次，加上最初那一句
    assert len(chats) <= 3 * 2 + 1


async def test_a_conversation_reply_goes_to_the_mentioned_partner(
    layout: NodeLayout, node: Config
) -> None:
    # Arrange
    coder = FakeProvider([Turn(text="我的看法是……")])
    seed = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="coder"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="讨论一下", mentions=("reviewer",)),
    )
    reviewer_box = Mailbox(layout.mailbox_dir("reviewer"))

    # Act
    async with running(layout, node, "coder", worker_handler(layout, node, "coder", coder)):
        Mailbox(layout.mailbox_dir("coder")).deposit(seed)
        await wait_until(lambda: bool(reviewer_box.list_new()))

    # Assert：回信落在 reviewer 那里，而不是回给发起的人
    got = Mailbox.read_envelope(reviewer_box.list_new()[0])
    assert got.type is MessageType.CHAT
    assert got.from_.agent == "coder"
    assert got.payload.mentions == ("coder",)  # 把球打回去
    # 发起人只会收到回执，不会收到这句回话 —— 球已经交给 reviewer 了
    cli_kinds = {
        Mailbox.read_envelope(p).type for p in Mailbox(layout.mailbox_dir("cli")).list_new()
    }
    assert MessageType.CHAT not in cli_kinds


async def test_the_run_is_marked_finished_before_the_result_is_sent(
    layout: NodeLayout, node: Config
) -> None:
    """先落盘再发送 —— 和 Sender 同一条原则。

    反过来的话，进程正好在「结果已发出、状态还没存」之间被 Ctrl-C，
    重启后这次运行看起来仍未收尾，于是再给用户发一遍最终结果。
    """
    # Arrange
    single = {
        "goal": "一步就完",
        "steps": [{"id": "s1", "assignee": "coder", "task": "做事", "depends_on": []}],
        "done_when": "",
    }
    boss = FakeProvider([plan_turn(single)])
    coder = FakeProvider([finish_turn("好了")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))
    store = RunStore(layout.blackboard)

    # Act
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running(layout, node, "coder", worker_handler(layout, node, "coder", coder))
        )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        # 结果一到手就立刻看状态：那一刻磁盘上必须已经是 finished
        await wait_until(lambda: bool(finals_in(cli_box)))
        finished_at_delivery = store.all()[0].finished

    assert finished_at_delivery


# ---------- 一次抖动不该毁掉整次协作 ----------


async def test_a_retryable_step_failure_is_retried_instead_of_killing_the_run(
    layout: NodeLayout, node: Config
) -> None:
    """worker 明确回传的 `retryable` 以前在整个 orchestrator 里从没被读过：
    任何 task.error 一律直接判死，**一次网络抖动就是整个 run 失败**。
    StepRecord.attempts 也是记了没人用。
    """
    # Arrange：coder 第一次调模型撞上可重试的上游错误，第二次正常交付
    boss = FakeProvider([plan_turn(), verdict_turn(satisfied=True)])
    coder = FakeProvider(
        [
            ProviderError("上游 502", retryable=True),
            finish_turn("重试之后写好了", ("tests/test_date.py",)),
        ]
    )
    reviewer = FakeProvider([finish_turn("看过了，approve")])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act
    async with AsyncExitStack() as stack:
        for name, provider in (("coder", coder), ("reviewer", reviewer)):
            await stack.enter_async_context(
                running(layout, node, name, worker_handler(layout, node, name, provider))
            )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    # Assert：整次协作成功，而那一步确实派了两次
    final = finals_in(cli_box)[0]
    assert final.type is MessageType.TASK_RESULT, final.payload
    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").state is StepState.DONE
    assert state.step("s1").attempts == 2


async def test_a_non_retryable_failure_still_fails_immediately(
    layout: NodeLayout, node: Config
) -> None:
    """`retryable=False` 是 worker 在说「重试没有意义」—— 得听它的，别白试三次。"""
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([ProviderError("API key 不对", retryable=False)])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running(layout, node, "coder", worker_handler(layout, node, "coder", coder))
        )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").state is StepState.FAILED
    assert state.step("s1").attempts == 1  # 只派过一次


async def test_retries_are_bounded(layout: NodeLayout, node: Config) -> None:
    """`retryable` 默认就是 True，光看它挡不住无限重试 —— 次数上限才是刹车。"""
    boss = FakeProvider([plan_turn(), verdict_turn()])
    coder = FakeProvider([ProviderError("一直抖", retryable=True)])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            running(layout, node, "coder", worker_handler(layout, node, "coder", coder))
        )
        await stack.enter_async_context(
            running(layout, node, "boss", coordinator_handler(layout, boss, max_step_attempts=2))
        )
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(finals_in(cli_box)))

    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").state is StepState.FAILED
    assert state.step("s1").attempts == 2


# ---------- 人工审批节点 ----------


def plan_with_approval() -> Turn:
    return Turn(
        text=json.dumps(
            {
                "goal": "上线前先让人点个头",
                "steps": [
                    {
                        "id": "s1",
                        "assignee": "coder",
                        "task": "把改动发上去",
                        "needs_approval": True,
                    }
                ],
                "done_when": "",
            },
            ensure_ascii=False,
        )
    )


async def test_a_step_that_needs_approval_is_not_dispatched_until_someone_says_yes(
    layout: NodeLayout, node: Config
) -> None:
    """审批那套（security/approvals.py）本来就在，编排层却从没用过 ——
    「让人在关键一步之前确认」以前只能靠 Agent 自己触发危险操作时被策略引擎拦下，
    非常间接。现在它是计划里的一等公民。
    """
    boss = FakeProvider([plan_with_approval(), verdict_turn(satisfied=True)])
    coder_box = Mailbox(layout.mailbox_dir("coder"))

    async with running(layout, node, "boss", coordinator_handler(layout, boss)):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(ApprovalStore(layout.root).pending()))
        await asyncio.sleep(0.3)

        # 没批之前，那一步一个字都没发出去
        assert not coder_box.list_new(), "没人点头就把活派出去了"
        request = ApprovalStore(layout.root).pending()[0]
        assert "s1" in request.prompt

        ApprovalStore(layout.root).answer(request.id, approved=True, by="我")
        # 等的是这条断言自己的前置条件 —— 信封落地发生在落盘之前，
        # 只等「消息到了」会抢在 state.json 写完之前
        await wait_until(
            lambda: RunStore(layout.blackboard).all()[0].step("s1").state is StepState.RUNNING
        )

    assert coder_box.list_new(), "批了之后活得真的派出去"


async def test_rejecting_a_step_fails_it_instead_of_hanging(
    layout: NodeLayout, node: Config
) -> None:
    """拒绝是个明确答复，不该表现成「一直等」。"""
    boss = FakeProvider([plan_with_approval(), verdict_turn()])
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, node, "boss", coordinator_handler(layout, boss)):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(ApprovalStore(layout.root).pending()))
        request = ApprovalStore(layout.root).pending()[0]
        ApprovalStore(layout.root).answer(request.id, approved=False, by="我")
        await wait_until(lambda: bool(finals_in(cli_box)))

    state = RunStore(layout.blackboard).all()[0]
    assert state.step("s1").state is StepState.FAILED
    assert "拒绝" in state.step("s1").error
    assert not Mailbox(layout.mailbox_dir("coder")).list_new()


async def test_waiting_does_not_pile_up_duplicate_requests(
    layout: NodeLayout, node: Config
) -> None:
    """每次 tick 都重算一次审批 id —— 不稳定的话会每几秒提交一条新请求，
    把 approvals/ 刷爆，人也不知道该批哪条。"""
    boss = FakeProvider([plan_with_approval(), verdict_turn()])

    async with running(layout, node, "boss", coordinator_handler(layout, boss), tick=0.05):
        Mailbox(layout.mailbox_dir("boss")).deposit(user_task())
        await wait_until(lambda: bool(ApprovalStore(layout.root).pending()))
        await asyncio.sleep(0.5)  # 让 tick 转好几轮

        assert len(ApprovalStore(layout.root).pending()) == 1
