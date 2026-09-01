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

from anthill.adapters.bridge import BridgeHandler, note_needs_reply, parse_note, render_request
from anthill.agent.conversation import message_expects_reply
from anthill.agent.factory import build_handler
from anthill.agent.memory import ThreadMemory
from anthill.agent.runtime import AgentRuntime
from anthill.agent.sender import Sender
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import ConfigError
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import (
    ChatPayload,
    MessageType,
    TaskRequestPayload,
    TaskResultPayload,
)

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


def chat_to_cc(
    body: str = "你怎么看",
    mentions: tuple[str, ...] = (),
    *,
    expects_reply: bool = True,
) -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.CHAT,
        payload=ChatPayload(body=body, mentions=mentions),
        reply_to=None if expects_reply else "01J00000000000000000000000",
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
    assert "needs_reply: true" in text


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


def result_to_cc(summary: str = "排期谈定了：9 月 3 日交付") -> Envelope:
    """别人干完你派的活，回给你的那种消息。"""
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="cc"),
        type=MessageType.TASK_RESULT,
        payload=TaskResultPayload(summary=summary),
    )


async def test_a_task_result_reaches_the_person_instead_of_being_dropped(
    node: tuple[NodeLayout, Config],
) -> None:
    """**桥接 Agent 只有一个醒来的入口。**

    值守会话盯的是 `bridge/inbox/`，而 AntHill 有两条信道：聊天走 bridge，
    `anthill send` 的任务结果走 mailbox。任务结果本该由 handler 从 mailbox
    搬进 bridge/inbox —— 以前这里只放行 task.request 与 chat，别的一律
    `msg.ignored` 直接归档，于是**这个人永远不会被唤醒**，日志里只留一行
    谁也不会去看的 ignored。

    最容易中招的是「桥接 Agent 自己派了活出去」：对方干完回 task.result，
    而发起人看不见回音。
    """
    layout, config = node
    handler = handler_for(layout)
    env = result_to_cc()

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())

    note = (handler.dir("inbox") / f"{env.id}.md").read_text(encoding="utf-8")
    assert "排期谈定了" in note, "正文没带上，人看见了也不知道发生了什么"


async def test_a_task_result_is_not_marked_as_awaiting_a_reply(
    node: tuple[NodeLayout, Config],
) -> None:
    """看得见，但**不该要人回**。

    task.result 是别人给你的答复，回它只会在对方队列里生成一条新待办、对方
    再回执，没有终点。所以它不进 `pending/` —— 那里放的是构造回信要用的原始
    信封，只有「在等你回」的才需要。
    """
    layout, config = node
    handler = handler_for(layout)
    answer, ask = result_to_cc(), task_to_cc("这件事交给你")

    async with running(layout, config, handler):
        box = Mailbox(layout.mailbox_dir("cc"))
        box.deposit(answer)
        box.deposit(ask)
        await wait_until(lambda: len(list(handler.dir("inbox").glob("*.md"))) == 2)

    assert not (handler.dir("pending") / f"{answer.id}.json").is_file(), "通知不该占「待回复」"
    assert (handler.dir("pending") / f"{ask.id}.json").is_file(), "派活仍然要能回"


async def test_a_terminal_chat_answer_is_visible_but_not_pending(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, config = node
    handler = handler_for(layout)
    answer = chat_to_cc("检查通过", expects_reply=False)

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(answer)
        note = handler.dir("inbox") / f"{answer.id}.md"
        await wait_until(note.is_file)

    headers, body = parse_note(note.read_text(encoding="utf-8"))
    assert body.strip() == "检查通过"
    assert not note_needs_reply(headers)
    assert not (handler.dir("pending") / f"{answer.id}.json").exists()


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

    reply = next(e for e in envelopes(cli_box) if e.type is MessageType.CHAT)
    assert reply.payload.body == "我觉得先加日志"
    assert not message_expects_reply(reply)


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
    assert message_expects_reply(got)


def _pending(handler) -> bool:
    """收件箱或草稿箱里还有没归档的东西。"""
    return bool(list(handler.dir("inbox").glob("*.md")) or list(handler.dir("outbox").glob("*.md")))


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
        # **等的是这条断言自己的前置条件。** 回信是先发出去、后归档的，
        # 只等「结果到了」就断言「归档完了」，在满负载下会偶发地抢在归档前面。
        await wait_until(lambda: not _pending(handler))

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


def test_the_bridge_directories_exist_as_soon_as_the_agent_starts(
    node: tuple[NodeLayout, Config],
) -> None:
    """人得先能告诉自己的 Claude Code「盯着这个目录」。

    等第一条消息到了才建目录的话，配置的那一刻它还不存在。
    """
    layout, config = node

    build_handler(layout=layout, config=config, agent_name="cc")

    bridge = layout.agent_dir("cc") / "bridge"
    assert (bridge / "inbox").is_dir()
    assert (bridge / "outbox").is_dir()


async def test_a_bridge_reply_is_recorded_for_the_chat_page(
    node: tuple[NodeLayout, Config],
) -> None:
    """桥接 Agent 发出去的话要进本机的发件记录 —— 对话页「只读收件方归档」的
    假设在**跨机**方向塌了半边：收件方的归档在对面机器上，本机不补记的话，
    这半句在面板上就不存在。cli 实机复现：wtst↔tst1 的线程里唯独看不见
    tst1 自己的回复。
    """
    layout, config = node
    handler = handler_for(layout)
    env = chat_to_cc()

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(handler, f"{env.id}.md", "回你的这句要能在对话页看见")
        await wait_until(lambda: (layout.root / "chats" / f"{env.thread}.jsonl").is_file())

    record = (layout.root / "chats" / f"{env.thread}.jsonl").read_text(encoding="utf-8")
    assert "回你的这句要能在对话页看见" in record
    assert '"mine": true' in record


async def test_a_bridge_initiated_message_is_recorded_too(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, config = node
    handler = handler_for(layout)

    async with running(layout, config, handler):
        write_draft(handler, "cli-abc123.md", "---\nto: coder\n---\n\n主动说的也要看得见\n")
        await wait_until(lambda: bool(list((layout.root / "chats").glob("*.jsonl"))))

    texts = [p.read_text(encoding="utf-8") for p in (layout.root / "chats").glob("*.jsonl")]
    assert any("主动说的也要看得见" in t for t in texts)


async def test_a_failing_chat_record_does_not_resend_the_reply(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """补记是显示侧的锦上添花，**没资格打断投递语义**：它排在 send 成功之后、
    归档之前，抛 OSError 的话会逃过 tick 的 except AntHillError —— 草稿留在
    outbox，下一轮重发，跨机方向对方收到重复消息。磁盘抖一下不该变成重发。"""
    import anthill.adapters.bridge as bridge_mod

    def boom(*_args: object) -> None:
        raise OSError("磁盘抖了一下")

    monkeypatch.setattr(bridge_mod, "record_outgoing", boom)

    layout, config = node
    handler = handler_for(layout)
    env = chat_to_cc()
    cli_box = Mailbox(layout.mailbox_dir("cli"))

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(env)
        await wait_until(lambda: (handler.dir("inbox") / f"{env.id}.md").is_file())
        write_draft(handler, f"{env.id}.md", "只该发一次")
        await wait_until(lambda: any(e.type is MessageType.CHAT for e in envelopes(cli_box)))
        # 草稿必须已归档 —— 留在 outbox 就等于排队重发
        await wait_until(lambda: not list(handler.dir("outbox").glob("*.md")))

    sent = [e for e in envelopes(cli_box) if e.type is MessageType.CHAT]
    assert len(sent) == 1, f"补记失败被放大成重发：收到 {len(sent)} 条"


async def test_a_tick_retry_reuses_the_persisted_envelope_id(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """send 已成功、后处理却抛错时，整份草稿会重跑，但不得生成一个新消息 ID。"""
    layout, config = node
    handler = handler_for(layout)
    source = chat_to_cc()
    sent_ids: list[str] = []
    remembers = 0

    original_send = Sender.send

    async def capture_send(self: Sender, env: Envelope):
        if env.type is MessageType.CHAT and env.reply_to == source.id:
            sent_ids.append(env.id)
        return await original_send(self, env)

    original_remember = handler._remember

    def fail_once(ctx, incoming: Envelope, reply: Envelope) -> None:
        nonlocal remembers
        remembers += 1
        original_remember(ctx, incoming, reply)
        if remembers == 1:
            raise RuntimeError("模拟 send 之后 tick 失败")

    monkeypatch.setattr(Sender, "send", capture_send)
    monkeypatch.setattr(handler, "_remember", fail_once)

    async with running(layout, config, handler):
        Mailbox(layout.mailbox_dir("cc")).deposit(source)
        await wait_until(lambda: (handler.dir("inbox") / f"{source.id}.md").is_file())
        write_draft(handler, f"{source.id}.md", "这句话只能有一个消息 ID")
        await wait_until(lambda: remembers >= 2 and not list(handler.dir("outbox").glob("*.md")))

    assert len(sent_ids) == 2, "测试必须真实走过一次失败和一次重试"
    assert len(set(sent_ids)) == 1, f"同一草稿重跑生成了新 ID：{sent_ids}"
    assert list(handler.dir("prepared").glob("*.json")) == []
    chat_log = (layout.root / "chats" / f"{source.thread}.jsonl").read_text(encoding="utf-8")
    assert chat_log.count(sent_ids[0]) == 1, "同 ID 重试不应在面板发件记录里重复出现"
    history = ThreadMemory(ThreadMemory.path_for(layout.agent_dir("cc"), source.thread)).load()
    assert len(history) == 2, "同 ID 重试不应把同一问答重复算成两轮"


async def test_reusing_a_draft_name_cannot_send_a_stale_prepared_envelope(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """草稿归档与 prepared 清理之间崩溃后，同名新稿不能继承旧目标与正文。"""
    layout, config = node
    handler = handler_for(layout)
    original_send = Sender.send
    block_send = True

    async def fail_before_delivery(self: Sender, env: Envelope):
        if block_send and env.from_.agent == "cc" and env.type is MessageType.CHAT:
            raise RuntimeError("模拟 prepared 落盘后崩溃")
        return await original_send(self, env)

    monkeypatch.setattr(Sender, "send", fail_before_delivery)
    draft = write_draft(handler, "固定名字.md", "---\nto: coder\n---\n\n旧正文")

    async with running(layout, config, handler):
        await wait_until(lambda: bool(list(handler.dir("prepared").glob("*.json"))))

    old_mtime = draft.stat().st_mtime_ns
    draft.write_text("---\nto: cli\n---\n\n全新的正文", encoding="utf-8")
    import os

    os.utime(draft, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))
    block_send = False

    async with running(layout, config, handler):
        await wait_until(lambda: bool(list(handler.dir("done").glob("固定名字.md.failed"))))

    for recipient in ("cli", "coder"):
        assert not any(
            env.type is MessageType.CHAT
            for env in envelopes(Mailbox(layout.mailbox_dir(recipient)))
        )
    assert list(handler.dir("prepared").glob("*.json")) == []
    assert list(handler.dir("done").glob("固定名字.md.json.failed"))
