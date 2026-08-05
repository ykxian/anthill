"""两家 SDK 的格式转换与响应解析。

这些函数是纯的，所以不用联网也能测 —— 而它们恰恰是最容易出错的地方
（工具调用参数是 JSON 字符串还是对象、tool 结果放哪条消息里）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anthill.core.errors import ProviderError
from anthill.providers.anthropic_p import (
    AnthropicProvider,
    parse_anthropic_response,
    split_system,
    to_anthropic_messages,
)
from anthill.providers.base import Msg, Role, ToolCall, ToolSpec, classify_sdk_error
from anthill.providers.openai_compat import (
    OpenAICompatProvider,
    parse_openai_response,
    to_openai_messages,
)

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


# ---------- 真正发出去的那个请求体 ----------
#
# 这两个 provider 是全仓覆盖率最低的两块（61% / 62%），没盖到的正是
# `complete()` —— 也就是**真正发给上游的请求体**长什么样。
# 录制回放机制虽然写好了，但仓库里唯一走回放的用例是先用 FakeProvider 录一条再回放，
# 测的是机制本身；这两个模块从没被真实模型的输出形态回归过。
#
# 真带子要 API key 和网络（也要花钱），CI 里做不到。但请求体这一半是纯逻辑，
# 塞一个假 SDK 客户端进去就能测 —— 而它恰恰是对着真 API 最容易错的地方。


class FakeSDK:
    """记下收到的 kwargs；可以设定返回什么或抛什么。"""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.chat = SimpleNamespace(completions=self)  # OpenAI 那家的路径
        self.messages = self  # Anthropic 那家的路径

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


def anthropic_reply(text: str = "好") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=3),
    )


def openai_reply(text: str = "好") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=None), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
    )


HISTORY = [Msg.system("你是 coder"), Msg.user("看看 a.py")]
TOOL = ToolSpec(name="read_file", description="读文件", parameters={"type": "object"})


async def test_anthropic_hoists_the_system_prompt_out_of_messages() -> None:
    """Anthropic 的 system 是**顶层字段**，不是一条 role=system 的消息 ——
    混进 messages 里会直接 400。"""
    provider = AnthropicProvider(model="claude-x", api_key="k")
    sdk = FakeSDK(response=anthropic_reply())
    provider._client = sdk  # type: ignore[attr-defined]

    await provider.complete(HISTORY, [TOOL])

    sent = sdk.calls[0]
    assert sent["system"] == "你是 coder"
    assert all(m["role"] != "system" for m in sent["messages"])  # type: ignore[index,union-attr]
    assert sent["tools"]


async def test_openai_keeps_the_system_prompt_as_a_message() -> None:
    """OpenAI 那家正相反 —— system 就是消息列表里的第一条。两家的差别只此一处，
    也正是这一处最容易照着另一家的写法写错。"""
    provider = OpenAICompatProvider(model="deepseek-chat", api_key="k")
    sdk = FakeSDK(response=openai_reply())
    provider._client = sdk  # type: ignore[attr-defined]

    await provider.complete(HISTORY, [TOOL])

    sent = sdk.calls[0]
    assert "system" not in sent
    assert sent["messages"][0]["role"] == "system"  # type: ignore[index]


async def test_no_tools_means_no_tools_key_at_all() -> None:
    """空的 tools 列表有的端点会拒收 —— 没有工具就别带这个字段。"""
    provider = OpenAICompatProvider(model="m", api_key="k")
    sdk = FakeSDK(response=openai_reply())
    provider._client = sdk  # type: ignore[attr-defined]

    await provider.complete(HISTORY, [])

    assert "tools" not in sdk.calls[0]


@pytest.mark.parametrize(
    "factory", [AnthropicProvider, OpenAICompatProvider], ids=["anthropic", "openai"]
)
async def test_an_sdk_error_becomes_a_classified_provider_error(factory: type) -> None:
    """上游抛什么都不能原样冒出去 —— 编排层是靠 ProviderError.retryable
    决定要不要重试的。"""
    provider = factory(model="m", api_key="k")
    provider._client = FakeSDK(error=RuntimeError("connection reset"))

    with pytest.raises(ProviderError):
        await provider.complete(HISTORY, [])


@pytest.mark.parametrize(
    "factory", [AnthropicProvider, OpenAICompatProvider], ids=["anthropic", "openai"]
)
async def test_closing_releases_the_sdk_client(factory: type) -> None:
    provider = factory(model="m", api_key="k")
    sdk = FakeSDK()
    provider._client = sdk

    await provider.aclose()
    await provider.aclose()  # 关两次不该炸

    assert sdk.closed
