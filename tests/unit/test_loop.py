"""ReAct 循环：工具调用、策略拦截、步数与 token 双熔断。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.agent.loop import AgentLoop, LoopOutcome
from anthill.agent.tools.base import ToolContext
from anthill.agent.tools.registry import build_toolset
from anthill.core.config import SecuritySection
from anthill.core.errors import BudgetExceeded, ProviderError
from anthill.core.logging import EventLog
from anthill.providers.base import Msg, Role, ToolCall, Turn, Usage
from anthill.providers.fake import FakeProvider
from anthill.security.policy import PolicyEngine, TrustLevel

THREAD = "01J00000000000000000000000"


@pytest.fixture
def tool_ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "ws").mkdir()
    (tmp_path / "bb").mkdir()
    return ToolContext(
        workspace=tmp_path / "ws",
        blackboard=tmp_path / "bb",
        security=SecuritySection(),
        thread=THREAD,
    )


def make_loop(
    provider: FakeProvider,
    tool_ctx: ToolContext,
    *,
    tools: tuple[str, ...] = (),
    trust: TrustLevel = TrustLevel.USER,
    max_steps: int = 5,
    token_budget: int = 100_000,
    confirm: object = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        tools=build_toolset(tools),
        policy=PolicyEngine(tool_ctx.security),
        tool_ctx=tool_ctx,
        log=EventLog(None, agent="coder", echo=False),
        trust=trust,
        max_steps=max_steps,
        token_budget=token_budget,
        confirm=confirm,  # type: ignore[arg-type]
    )


def finish_turn(summary: str = "搞定", **extra: object) -> Turn:
    return Turn(
        tool_calls=(ToolCall(id="c-fin", name="finish", arguments={"summary": summary, **extra}),),
        usage=Usage(input_tokens=10, output_tokens=2),
    )


# ---------- 正常路径 ----------


async def test_loop_executes_tool_then_finishes(tool_ctx: ToolContext) -> None:
    # Arrange
    (tool_ctx.workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    provider = FakeProvider(
        [
            Turn(tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),)),
            finish_turn("读完了", artifacts=["a.py"]),
        ]
    )
    loop = make_loop(provider, tool_ctx)

    # Act
    outcome = await loop.run([Msg.user("看看 a.py")])

    # Assert
    assert isinstance(outcome, LoopOutcome)
    assert outcome.finished
    assert outcome.summary == "读完了"
    assert outcome.artifacts == ("a.py",)
    assert outcome.steps == 2
    assert outcome.usage.total > 0


async def test_loop_feeds_tool_output_back_to_model(tool_ctx: ToolContext) -> None:
    # Arrange
    (tool_ctx.workspace / "a.py").write_text("答案是 42\n", encoding="utf-8")
    provider = FakeProvider(
        [
            Turn(tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),)),
            finish_turn(),
        ]
    )

    # Act
    await make_loop(provider, tool_ctx).run([Msg.user("看看")])

    # Assert：第二次调用时，模型应该看到工具结果
    second_call_messages = provider.calls[1].messages
    tool_msgs = [m for m in second_call_messages if m.role is Role.TOOL]
    assert tool_msgs and "42" in tool_msgs[0].content


async def test_plain_text_answer_without_tools_is_accepted_as_result(
    tool_ctx: ToolContext,
) -> None:
    provider = FakeProvider([Turn(text="不需要动手，结论是 A")])

    outcome = await make_loop(provider, tool_ctx).run([Msg.user("问个问题")])

    assert not outcome.finished
    assert "结论是 A" in outcome.summary
    assert outcome.status == "ok"


async def test_loop_records_transcript_for_persistence(tool_ctx: ToolContext) -> None:
    provider = FakeProvider([finish_turn()])

    outcome = await make_loop(provider, tool_ctx).run([Msg.user("干活")])

    assert [m.role for m in outcome.transcript] == [Role.ASSISTANT, Role.TOOL]


# ---------- 错误与拦截 ----------


async def test_unknown_tool_is_reported_back_to_model_not_crashing(
    tool_ctx: ToolContext,
) -> None:
    provider = FakeProvider(
        [
            Turn(tool_calls=(ToolCall(id="c1", name="launch_missiles", arguments={}),)),
            finish_turn("改用别的办法"),
        ]
    )

    outcome = await make_loop(provider, tool_ctx).run([Msg.user("干活")])

    assert outcome.finished
    tool_msgs = [m for m in provider.calls[1].messages if m.role is Role.TOOL]
    assert "未知工具" in tool_msgs[0].content


async def test_tool_not_granted_to_agent_is_refused(tool_ctx: ToolContext) -> None:
    # reviewer 只有 read_file 与 finish，却想跑 shell
    provider = FakeProvider(
        [
            Turn(tool_calls=(ToolCall(id="c1", name="run_shell", arguments={"command": "ls"}),)),
            finish_turn("那就不跑了"),
        ]
    )

    await make_loop(provider, tool_ctx, tools=("read_file", "finish")).run([Msg.user("干活")])

    tool_msgs = [m for m in provider.calls[1].messages if m.role is Role.TOOL]
    assert "未知工具" in tool_msgs[0].content


async def test_high_risk_tool_is_denied_without_confirmer(tool_ctx: ToolContext) -> None:
    # Arrange：run_shell 非白名单命令 = high 风险，无人可确认
    provider = FakeProvider(
        [
            Turn(
                tool_calls=(
                    ToolCall(id="c1", name="run_shell", arguments={"command": "rm -rf build"}),
                )
            ),
            finish_turn("被拦了，改用安全方案"),
        ]
    )

    # Act
    outcome = await make_loop(provider, tool_ctx, confirm=None).run([Msg.user("清理")])

    # Assert
    tool_msgs = [m for m in provider.calls[1].messages if m.role is Role.TOOL]
    assert "确认" in tool_msgs[0].content
    assert outcome.finished


async def test_high_risk_tool_runs_after_confirmation(tool_ctx: ToolContext) -> None:
    # Arrange
    asked: list[str] = []

    async def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    provider = FakeProvider(
        [
            Turn(
                tool_calls=(
                    ToolCall(id="c1", name="run_shell", arguments={"command": "echo hi > out.txt"}),
                )
            ),
            finish_turn(),
        ]
    )

    # Act
    await make_loop(provider, tool_ctx, confirm=confirm).run([Msg.user("写文件")])

    # Assert
    assert asked
    assert (tool_ctx.workspace / "out.txt").is_file()


async def test_provider_error_propagates(tool_ctx: ToolContext) -> None:
    provider = FakeProvider([ProviderError("上游 500", retryable=True)])

    with pytest.raises(ProviderError):
        await make_loop(provider, tool_ctx).run([Msg.user("干活")])


# ---------- 双熔断 ----------


async def test_step_limit_breaks_the_loop(tool_ctx: ToolContext) -> None:
    # 模型永远只读文件、从不收尾
    (tool_ctx.workspace / "a.py").write_text("x", encoding="utf-8")
    provider = FakeProvider(
        [Turn(tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),))]
    )

    with pytest.raises(BudgetExceeded, match="步"):
        await make_loop(provider, tool_ctx, max_steps=3).run([Msg.user("干活")])

    assert len(provider.calls) == 3


async def test_token_budget_breaks_the_loop(tool_ctx: ToolContext) -> None:
    (tool_ctx.workspace / "a.py").write_text("x", encoding="utf-8")
    provider = FakeProvider(
        [
            Turn(
                tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),),
                usage=Usage(input_tokens=400, output_tokens=100),
            )
        ]
    )

    with pytest.raises(BudgetExceeded, match="token"):
        await make_loop(provider, tool_ctx, max_steps=50, token_budget=600).run([Msg.user("干活")])

    assert len(provider.calls) == 2


# ---------- 边跑边落盘 ----------


async def test_messages_are_emitted_to_sink_as_they_happen(tool_ctx: ToolContext) -> None:
    # Arrange
    (tool_ctx.workspace / "a.py").write_text("x", encoding="utf-8")
    seen: list[Msg] = []
    provider = FakeProvider(
        [
            Turn(tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),)),
            finish_turn(),
        ]
    )

    # Act
    await make_loop(provider, tool_ctx).run([Msg.user("干活")], sink=seen.append)

    # Assert
    assert [m.role for m in seen] == [Role.ASSISTANT, Role.TOOL, Role.ASSISTANT, Role.TOOL]


async def test_sink_still_receives_work_done_before_a_budget_break(tool_ctx: ToolContext) -> None:
    # 熔断会抛异常，靠返回值持久化的话已做的工作就丢了
    (tool_ctx.workspace / "a.py").write_text("x", encoding="utf-8")
    seen: list[Msg] = []
    provider = FakeProvider(
        [Turn(tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),))]
    )

    with pytest.raises(BudgetExceeded):
        await make_loop(provider, tool_ctx, max_steps=2).run([Msg.user("干活")], sink=seen.append)

    assert len(seen) == 4  # 两轮各 1 条 assistant + 1 条 tool，都落下来了


async def test_sink_failure_does_not_fail_the_task(tool_ctx: ToolContext) -> None:
    def broken(msg: Msg) -> None:
        raise OSError("磁盘满了")

    outcome = await make_loop(FakeProvider([finish_turn()]), tool_ctx).run(
        [Msg.user("干活")], sink=broken
    )

    assert outcome.finished
