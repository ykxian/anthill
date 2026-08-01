"""两家 SDK 的格式转换与响应解析。

这些函数是纯的，所以不用联网也能测 —— 而它们恰恰是最容易出错的地方
（工具调用参数是 JSON 字符串还是对象、tool 结果放哪条消息里）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anthill.core.errors import ProviderError
from anthill.providers.anthropic_p import (
    parse_anthropic_response,
    split_system,
    to_anthropic_messages,
)
from anthill.providers.base import Msg, Role, ToolCall, classify_sdk_error
from anthill.providers.openai_compat import parse_openai_response, to_openai_messages

CALL = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
ASSISTANT_WITH_CALL = Msg(role=Role.ASSISTANT, content="我看看", tool_calls=(CALL,))


# ---------- OpenAI ----------


def test_openai_serialises_tool_call_arguments_as_json_string() -> None:
    out = to_openai_messages([ASSISTANT_WITH_CALL])

    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'


def test_openai_tool_result_becomes_tool_role_message() -> None:
    out = to_openai_messages([Msg.tool_result("c1", "文件内容")])

    assert out == [{"role": "tool", "tool_call_id": "c1", "content": "文件内容"}]


def test_openai_response_parses_text_calls_and_usage() -> None:
    # Arrange
    raw = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="好的",
                    tool_calls=[
                        SimpleNamespace(
                            id="c1",
                            function=SimpleNamespace(
                                name="read_file", arguments='{"path": "a.py"}'
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
    )

    # Act
    turn = parse_openai_response(raw)

    # Assert
    assert turn.text == "好的"
    assert turn.tool_calls[0].arguments == {"path": "a.py"}
    assert turn.usage.total == 14
    assert turn.stop_reason == "tool_calls"


def test_openai_response_keeps_unparsable_arguments_for_debugging() -> None:
    raw = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="c1", function=SimpleNamespace(name="x", arguments="{不是 JSON")
                        )
                    ],
                ),
                finish_reason="stop",
            )
        ],
        usage=None,
    )

    turn = parse_openai_response(raw)

    assert turn.tool_calls[0].arguments["__raw__"] == "{不是 JSON"


def test_openai_response_without_choices_is_an_error() -> None:
    with pytest.raises(ProviderError, match="choices"):
        parse_openai_response(SimpleNamespace(choices=[]))


# ---------- Anthropic ----------


def test_anthropic_splits_system_out_of_the_message_list() -> None:
    system, rest = split_system([Msg.system("规则一"), Msg.system("规则二"), Msg.user("干活")])

    assert system == "规则一\n\n规则二"
    assert [m.role for m in rest] == [Role.USER]


def test_anthropic_assistant_message_becomes_text_and_tool_use_blocks() -> None:
    out = to_anthropic_messages([ASSISTANT_WITH_CALL])

    assert out[0]["content"][0] == {"type": "text", "text": "我看看"}
    assert out[0]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }


def test_anthropic_merges_consecutive_tool_results_into_one_user_message() -> None:
    # 并行工具调用时会连着来两条 tool 结果，Anthropic 要求合并成一条 user 消息
    out = to_anthropic_messages(
        [
            ASSISTANT_WITH_CALL,
            Msg.tool_result("c1", "内容一"),
            Msg.tool_result("c2", "内容二"),
        ]
    )

    assert len(out) == 2
    assert [b["tool_use_id"] for b in out[1]["content"]] == ["c1", "c2"]


def test_anthropic_starts_new_user_message_after_an_assistant_turn() -> None:
    out = to_anthropic_messages(
        [Msg.tool_result("c1", "一"), ASSISTANT_WITH_CALL, Msg.tool_result("c2", "二")]
    )

    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_anthropic_response_parses_blocks_and_usage() -> None:
    raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="先读文件"),
            SimpleNamespace(type="tool_use", id="c1", name="read_file", input={"path": "a.py"}),
        ],
        usage=SimpleNamespace(input_tokens=20, output_tokens=5),
        stop_reason="tool_use",
    )

    turn = parse_anthropic_response(raw)

    assert turn.text == "先读文件"
    assert turn.tool_calls[0].name == "read_file"
    assert turn.usage.total == 25


# ---------- 错误分类 ----------


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, True), (500, True), (503, True), (400, False), (401, False)],
)
def test_sdk_errors_are_classified_retryable_by_status_code(status: int, expected: bool) -> None:
    exc = type("APIStatusError", (Exception,), {})("boom")
    exc.status_code = status  # type: ignore[attr-defined]

    assert classify_sdk_error(exc, provider="p").retryable is expected


def test_sdk_errors_without_status_fall_back_to_class_name() -> None:
    connection = type("APIConnectionError", (Exception,), {})("断网")
    other = type("BadRequestish", (Exception,), {})("参数错")

    assert classify_sdk_error(connection, provider="p").retryable
    assert not classify_sdk_error(other, provider="p").retryable
