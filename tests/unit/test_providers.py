"""providers 层：消息模型、工具 schema 转换、假 provider、录制回放。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.errors import ProviderError
from anthill.providers.base import (
    Msg,
    Role,
    ToolCall,
    ToolSpec,
    Turn,
    Usage,
    estimate_tokens,
)
from anthill.providers.fake import FakeProvider
from anthill.providers.record import RecordingProvider, ReplayProvider

SPEC = ToolSpec(
    name="read_file",
    description="读取文件",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)


# ---------- 消息与用量 ----------


def test_usage_adds_without_mutating_operands() -> None:
    # Arrange
    a = Usage(input_tokens=10, output_tokens=5)
    b = Usage(input_tokens=1, output_tokens=2)

    # Act
    total = a + b

    # Assert
    assert (total.input_tokens, total.output_tokens, total.total) == (11, 7, 18)
    assert a.input_tokens == 10 and b.output_tokens == 2


def test_estimate_tokens_counts_cjk_heavier_than_ascii() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四个字") == 5
    assert estimate_tokens("abcd") == 1


def test_turn_to_msg_carries_tool_calls() -> None:
    # Arrange
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    turn = Turn(text="我看看", tool_calls=(call,), usage=Usage(), stop_reason="tool_use")

    # Act
    msg = turn.to_msg()

    # Assert
    assert msg.role is Role.ASSISTANT
    assert msg.content == "我看看"
    assert msg.tool_calls == (call,)


def test_tool_result_msg_requires_tool_call_id() -> None:
    with pytest.raises(ValueError, match="tool_call_id"):
        Msg(role=Role.TOOL, content="ok")


# ---------- 工具 schema 转换 ----------


def test_tool_spec_to_openai_format() -> None:
    assert SPEC.to_openai() == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件",
            "parameters": SPEC.parameters,
        },
    }


def test_tool_spec_to_anthropic_format() -> None:
    assert SPEC.to_anthropic() == {
        "name": "read_file",
        "description": "读取文件",
        "input_schema": SPEC.parameters,
    }


# ---------- 假 provider ----------


async def test_fake_provider_returns_scripted_turns_in_order() -> None:
    # Arrange
    first = Turn(text="", tool_calls=(ToolCall(id="c1", name="finish", arguments={}),))
    second = Turn(text="收工")
    provider = FakeProvider([first, second])

    # Act
    turns = [await provider.complete([Msg(role=Role.USER, content="hi")], [SPEC]) for _ in range(2)]

    # Assert
    assert [t.text for t in turns] == ["", "收工"]
    assert len(provider.calls) == 2


async def test_fake_provider_repeats_last_turn_when_script_exhausted() -> None:
    provider = FakeProvider([Turn(text="只有一句")])

    await provider.complete([], [])
    again = await provider.complete([], [])

    assert again.text == "只有一句"


async def test_fake_provider_raises_configured_error() -> None:
    provider = FakeProvider([ProviderError("上游 502")])

    with pytest.raises(ProviderError, match="502"):
        await provider.complete([], [])


# ---------- 录制与回放 ----------


async def test_recording_then_replaying_reproduces_turns(tmp_path: Path) -> None:
    # Arrange
    tape = tmp_path / "tape.jsonl"
    scripted = [
        Turn(
            text="",
            tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),),
            usage=Usage(input_tokens=7, output_tokens=3),
        ),
        Turn(text="done", usage=Usage(input_tokens=9, output_tokens=1)),
    ]
    recorder = RecordingProvider(FakeProvider(list(scripted)), tape)

    # Act
    for _ in scripted:
        await recorder.complete([Msg(role=Role.USER, content="hi")], [SPEC])
    recorder.close()
    replay = ReplayProvider.from_file(tape)
    replayed = [await replay.complete([], []) for _ in scripted]

    # Assert
    assert [t.text for t in replayed] == ["", "done"]
    assert replayed[0].tool_calls[0].arguments == {"path": "a.py"}
    assert replayed[1].usage.input_tokens == 9


async def test_replay_provider_fails_loudly_when_tape_runs_out(tmp_path: Path) -> None:
    # Arrange
    tape = tmp_path / "tape.jsonl"
    recorder = RecordingProvider(FakeProvider([Turn(text="一次")]), tape)
    await recorder.complete([], [])
    recorder.close()
    replay = ReplayProvider.from_file(tape)
    await replay.complete([], [])

    # Act / Assert：录制带用完了要报错，而不是悄悄重复上一条
    with pytest.raises(ProviderError, match="录制"):
        await replay.complete([], [])
