"""send_message 工具与收件人解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.agent.tools.base import ToolContext
from anthill.agent.tools.messaging import SendMessageTool, normalize_recipient
from anthill.core.config import SecuritySection
from anthill.core.errors import HopLimitExceeded, UnknownRecipient
from anthill.core.ids import new_thread_id
from anthill.core.router import extract_mentions, parse_address


class RecordingMessenger:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[dict[str, str]] = []
        self._fail = fail

    async def send(self, *, to: str, body: str, kind: str) -> str:
        if self._fail is not None:
            raise self._fail
        self.sent.append({"to": to, "body": body, "kind": kind})
        return "01J00000000000000000000MSG"


def make_ctx(tmp_path: Path, messenger: object | None) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        blackboard=tmp_path,
        security=SecuritySection(),
        thread=new_thread_id(),
        messenger=messenger,  # type: ignore[arg-type]
    )


# ---------- 收件人写法 ----------


@pytest.mark.parametrize(
    ("raw", "expected"), [("@reviewer", "reviewer"), ("reviewer", "reviewer"), (" @a ", "a")]
)
def test_at_prefix_is_stripped(raw: str, expected: str) -> None:
    """模型习惯像人一样写 @reviewer，路由层不认这个前缀。"""
    assert normalize_recipient(raw) == expected


def test_parse_address_handles_all_supported_forms() -> None:
    assert parse_address("coder", default_node="me") == parse_address("coder", default_node="me")
    assert parse_address("coder", default_node="me").node == "me"
    assert parse_address("role:reviewer", default_node="me").role == "reviewer"
    assert parse_address("lab:runner", default_node="me").node == "lab"
    assert parse_address("lab:role:runner", default_node="me").agent == "role:runner"
    assert parse_address("all", default_node="me").is_broadcast


def test_parse_address_rejects_empty() -> None:
    with pytest.raises(UnknownRecipient):
        parse_address("  ", default_node="me")


def test_extract_mentions_keeps_order_and_dedupes() -> None:
    assert extract_mentions("先 @reviewer 看看，再 @coder 改，@reviewer 复核") == (
        "reviewer",
        "coder",
    )


# ---------- 工具行为 ----------


async def test_send_message_delivers_chat_by_default(tmp_path: Path) -> None:
    messenger = RecordingMessenger()

    result = await SendMessageTool().run(
        {"to": "@reviewer", "body": "帮我看下 tests/test_date.py"}, make_ctx(tmp_path, messenger)
    )

    assert result.ok
    assert messenger.sent == [
        {"to": "reviewer", "body": "帮我看下 tests/test_date.py", "kind": "chat"}
    ]


async def test_send_message_supports_task_kind(tmp_path: Path) -> None:
    messenger = RecordingMessenger()

    await SendMessageTool().run(
        {"to": "role:reviewer", "body": "请审查", "kind": "task"}, make_ctx(tmp_path, messenger)
    )

    assert messenger.sent[0]["kind"] == "task"


async def test_send_message_without_a_channel_says_so(tmp_path: Path) -> None:
    result = await SendMessageTool().run({"to": "x", "body": "hi"}, make_ctx(tmp_path, None))

    assert not result.ok
    assert "消息通道" in result.content


@pytest.mark.parametrize(
    ("args", "hint"),
    [({"to": "", "body": "hi"}, "to"), ({"to": "a", "body": " "}, "body")],
)
async def test_send_message_validates_arguments(
    tmp_path: Path, args: dict[str, str], hint: str
) -> None:
    result = await SendMessageTool().run(args, make_ctx(tmp_path, RecordingMessenger()))

    assert not result.ok
    assert hint in result.content


async def test_send_message_rejects_unknown_kind(tmp_path: Path) -> None:
    result = await SendMessageTool().run(
        {"to": "a", "body": "hi", "kind": "carrier_pigeon"},
        make_ctx(tmp_path, RecordingMessenger()),
    )

    assert not result.ok


async def test_hop_limit_comes_back_as_a_readable_tool_failure(tmp_path: Path) -> None:
    """熔断由协议层做；工具这边只负责把原因讲清楚，让模型换个做法。"""
    messenger = RecordingMessenger(fail=HopLimitExceeded("已达 8 跳上限"))

    result = await SendMessageTool().run(
        {"to": "reviewer", "body": "再看一眼"}, make_ctx(tmp_path, messenger)
    )

    assert not result.ok
    assert "8 跳" in result.content


async def test_unknown_recipient_comes_back_as_a_tool_failure(tmp_path: Path) -> None:
    messenger = RecordingMessenger(fail=UnknownRecipient("本节点没有名为 'devops' 的 Agent"))

    result = await SendMessageTool().run(
        {"to": "devops", "body": "部署一下"}, make_ctx(tmp_path, messenger)
    )

    assert not result.ok
    assert "devops" in result.content
