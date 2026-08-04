"""文件夹桥接：常驻的交互式会话（或就是人本人）作为 Agent 参与协作。

这是本项目起点那个土办法的正式版本，重点验证两件此前做不到的事：
**收消息不阻塞**（人可以慢慢想，期间照常收新消息）、**人能主动插话**。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from anthill.adapters.bridge import BridgeHandler, parse_note, render_request
from anthill.agent.factory import build_handler
from anthill.agent.runtime import AgentRuntime
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import ConfigError
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload

TIMEOUT = 10.0

NODE_TOML = """
[node]
name = "testnode"
workspace = "."

[runtime]
poll_interval = 0.05
watch_mode = "poll"

[agents.cli]
role = "user"

[agents.cc]
role = "worker"
bridge = true

[agents.coder]
role = "worker"
"""


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "cc", "coder"):
        Mailbox(layout.mailbox_dir(name)).ensure()
    return layout, Config.load_from(layout)


def handler_for(layout: NodeLayout, chat_turns: int = 0) -> BridgeHandler:
    return BridgeHandler(root=layout.agent_dir("cc"), agent_name="cc", chat_turns=chat_turns)


@asynccontextmanager
async def running(
    layout: NodeLayout, config: Config, handler: BridgeHandler
) -> AsyncIterator[None]:
    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name="cc",
        handler=handler,
        log=EventLog(layout.log_file("cc"), agent="cc", echo=False),
        tick_interval=0.1,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=TIMEOUT)


async def wait_until(predicate: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


def write_draft(handler: BridgeHandler, name: str, text: str) -> Path:
    """写一份草稿，并把 mtime 拨旧 —— 免得测试要真等一秒「写完了」的判定。"""
    path = handler.dir("outbox") / name
    path.write_text(text, encoding="utf-8")
    old = time.time() - 5
    import os

    os.utime(path, (old, old))
    return path


def task_to_cc(body: str = "帮我看看 date.py") -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="看代码", body=body),
    )


def chat_to_cc(body: str = "你怎么看", mentions: tuple[str, ...] = ()) -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.CHAT,
        payload=ChatPayload(body=body, mentions=mentions),
    )


def envelopes(mailbox: Mailbox) -> list[Envelope]:
    return [Mailbox.read_envelope(p) for p in mailbox.list_new()]


# ---------- 文件格式 ----------


def test_the_request_file_tells_a_human_how_to_reply() -> None:
    env = task_to_cc("给 date.py 补单测")

    text = render_request(env)

    assert "给 date.py 补单测" in text
    assert f"outbox/{env.id}.md" in text  # 回复该写到哪，写在纸面上
    assert "testnode:cli" in text


def test_front_matter_is_parsed_without_a_yaml_dependency() -> None:
    headers, body = parse_note("---\nto: coder\ntype: task\n---\n\n正文在这里\n")

    assert headers == {"to": "coder", "type": "task"}
    assert body.strip() == "正文在这里"


def test_a_note_without_front_matter_is_all_body() -> None:
    headers, body = parse_note("就一句话")

    assert headers == {}
    assert body.strip() == "就一句话"


def test_the_template_comment_is_stripped_from_the_reply() -> None:
    """模板里那段说明注释人多半懒得删，不能把它当成正文发出去。"""
    _, body = parse_note("我的回复\n\n<!-- 回复：在 ../outbox/x.md 写下正文即可 -->\n")

    assert "outbox" not in body
    assert body.strip() == "我的回复"


# ---------- 收：不阻塞 ----------


async def test_an_incoming_message_becomes_a_file_and_does_not_block(
    node: tuple[NodeLayout, Config],
) -> None:
    """人可以想十分钟，期间 Agent 照常收新消息 —— 几条一起躺在 inbox 里等。"""
    # Arrange
    layout, config = node
    handler = handler_for(layout)
    first, second = task_to_cc("第一件事"), task_to_cc("第二件事")

    # Act：一条都不回复，直接再投一条
    async with running(layout, config, handler):
        box = Mailbox(layout.mailbox_dir("cc"))
        box.deposit(first)
        box.deposit(second)
        await wait_until(lambda: len(list(handler.dir("inbox").glob("*.md"))) == 2)

    # Assert：两条都在等，谁也没把消费循环堵住
    names = {p.stem for p in handler.dir("inbox").glob("*.md")}
    assert names == {first.id, second.id}
    assert "第一件事" in (handler.dir("inbox") / f"{first.id}.md").read_text(encoding="utf-8")


async def test_the_sender_still_gets_an_accepted_receipt_right_away(
    node: tuple[NodeLayout, Config],
) -> None:
    """回执是 runtime 发的，不等人 —— 否则发起方会以为消息丢了。"""
    layout, config = node
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, handler_for(layout)):
        Mailbox(layout.mailbox_dir("cc")).deposit(task_to_cc())
        await wait_until(lambda: bool(cli_box.list_new()))

    assert envelopes(cli_box)[0].type is MessageType.RECEIPT_ACCEPTED


# ---------- 发：回复 ----------


async def test_a_reply_written_by_hand_is_sent_as_a_task_result(
    node: tuple[NodeLayout, Config],
) -> None:
    # Arrange
    layout, config = node
    handler = handler_for(layout)
    env = task_to_cc()
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    # Act：人（或常驻会话）在 outbox 里写下回复
    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(
            handler,
            f"{env.id}.md",
            "---\nartifacts: notes.md\n---\n\n看过了，边界没问题\n",
        )
        await wait_until(lambda: any(e.type is MessageType.TASK_RESULT for e in envelopes(cli_box)))

    # Assert
    result = next(e for e in envelopes(cli_box) if e.type is MessageType.TASK_RESULT)
    assert result.payload.summary == "看过了，边界没问题"
    assert result.payload.artifacts == ("notes.md",)
    assert result.thread == env.thread


async def test_a_reply_to_a_chat_goes_back_as_a_chat(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, config = node
    handler = handler_for(layout)
    env = chat_to_cc()
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(handler, f"{env.id}.md", "我觉得先加日志")
        await wait_until(lambda: any(e.type is MessageType.CHAT for e in envelopes(cli_box)))

    assert next(e for e in envelopes(cli_box) if e.type is MessageType.CHAT).payload.body == (
        "我觉得先加日志"
    )


async def test_a_chat_reply_follows_the_mention_rule(
    node: tuple[NodeLayout, Config],
) -> None:
    """人在回路里也守同一套对话规则：@ 谁就回给谁。"""
    layout, config = node
    handler = handler_for(layout)
    env = chat_to_cc("讨论一下", mentions=("coder",))
    coder_box = Mailbox(layout.mailbox_dir("coder"))

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(handler, f"{env.id}.md", "我的看法是……")
        await wait_until(lambda: bool(coder_box.list_new()))

    got = envelopes(coder_box)[0]
    assert got.type is MessageType.CHAT
    assert got.payload.mentions == ("cc",)  # 把球打回来


async def test_replying_archives_both_the_request_and_the_draft(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, config = node
    handler = handler_for(layout)
    env = task_to_cc()
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(handler, f"{env.id}.md", "好了")
        await wait_until(lambda: any(e.type is MessageType.TASK_RESULT for e in envelopes(cli_box)))

    assert list(handler.dir("inbox").glob("*.md")) == []
    assert list(handler.dir("outbox").glob("*.md")) == []
    assert len(list(handler.dir("done").iterdir())) >= 2


# ---------- 人主动插话 ----------


async def test_a_human_can_start_a_message_on_their_own(
    node: tuple[NodeLayout, Config],
) -> None:
    """outbox 里放一个带 `to:` 的文件 = 主动发起，不必是对谁的回复。

    这就是「人手动中途插进对话」的那条路。
    """
    # Arrange
    layout, config = node
    handler = handler_for(layout)
    coder_box = Mailbox(layout.mailbox_dir("coder"))

    # Act
    async with running(layout, config, handler):
        write_draft(handler, "随手写的.md", "---\nto: coder\n---\n\n这块我来改，你别动\n")
        await wait_until(lambda: bool(coder_box.list_new()))

    # Assert
    got = envelopes(coder_box)[0]
    assert got.type is MessageType.CHAT
    assert got.payload.body == "这块我来改，你别动"
    assert got.from_.agent == "cc"


async def test_a_human_can_hand_out_a_task_too(node: tuple[NodeLayout, Config]) -> None:
    layout, config = node
    handler = handler_for(layout)
    coder_box = Mailbox(layout.mailbox_dir("coder"))

    async with running(layout, config, handler):
        write_draft(handler, "派活.md", "---\nto: coder\ntype: task\n---\n\n把测试补齐\n")
        await wait_until(lambda: bool(coder_box.list_new()))

    assert envelopes(coder_box)[0].type is MessageType.TASK_REQUEST


async def test_a_draft_that_says_nothing_about_the_recipient_is_reported(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, config = node
    handler = handler_for(layout)

    async with running(layout, config, handler):
        write_draft(handler, "忘了写收件人.md", "随便说点什么")
        await wait_until(lambda: bool(list(handler.dir("done").glob("*.failed"))))

    assert list(handler.dir("outbox").glob("*.md")) == []  # 不会一直重试刷屏


# ---------- 半成品与空稿 ----------


def test_a_draft_still_being_written_is_not_picked_up(node: tuple[NodeLayout, Config]) -> None:
    """编辑器边写边刷盘，抢在中途读走会发出半句话。"""
    layout, _ = node
    handler = handler_for(layout)
    (handler.dir("outbox") / "刚写一半.md").write_text("我正在写……", encoding="utf-8")

    assert handler.drafts() == []


def test_an_empty_draft_is_ignored(node: tuple[NodeLayout, Config]) -> None:
    layout, _ = node
    handler = handler_for(layout)
    write_draft(handler, "空的.md", "---\nto: coder\n---\n\n   \n")

    assert handler.drafts() == []


# ---------- 装配 ----------


def test_factory_picks_the_bridge_when_configured(node: tuple[NodeLayout, Config]) -> None:
    layout, config = node

    assert build_handler(layout=layout, config=config, agent_name="cc").name == "bridge"


def test_an_agent_cannot_be_both_a_bridge_and_something_else(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(
        '[node]\nname = "n"\nworkspace = "."\n\n'
        '[agents.cc]\nrole = "worker"\nbridge = true\ncommand = ["claude", "-p"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="一个大脑"):
        Config.load_from(layout)


def test_a_title_that_is_just_the_start_of_the_body_is_not_repeated() -> None:
    """`anthill send` 默认取正文前 60 字当标题，直接拼起来会把同一句话读两遍。"""
    env = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="帮我看看 date.py", body="帮我看看 date.py 的边界情况"),
    )

    text = render_request(env)

    assert text.count("帮我看看 date.py") == 1


def test_a_real_title_is_kept_alongside_the_body() -> None:
    env = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="补单测", body="给 date.py 写 12 个用例"),
    )

    text = render_request(env)

    assert "补单测" in text and "12 个用例" in text
