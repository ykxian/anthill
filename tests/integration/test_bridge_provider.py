"""让一个常驻会话当 coordinator 的大脑：不用 API key 也能跑真编排。

盯住四件事：
- 装配走对了岔路 —— 桥接 **coordinator** 要拿到编排状态机，不是 BridgeHandler；
- 问答走的是普通消息那套 inbox/outbox 约定，所以值守会话不用学新东西；
- 半写完的回答不能被读走（和 `BridgeHandler.drafts()` 一个标准）；
- 等待必须有界 —— 人走开了不能把 coordinator 永远焊死。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.adapters.bridge import BRIDGE_DIR, DONE, INBOX, OUTBOX
from anthill.adapters.bridge_provider import BridgeProvider, render_ask
from anthill.agent.factory import build_handler
from anthill.core.config import Config
from anthill.core.errors import ProviderError
from anthill.core.paths import NodeLayout
from anthill.providers.base import Msg, Role, ToolSpec

NODE_TOML = """
[node]
name = "n"
workspace = "."

[agents.cli]
role = "user"

[agents.boss]
role = "coordinator"
bridge = true

[agents.helper]
role = "worker"
bridge = true
"""


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    return layout, Config.load_from(layout)


def provider_for(layout: NodeLayout, **kw: float) -> BridgeProvider:
    return BridgeProvider(
        root=layout.agent_dir("boss"),
        agent_name="boss",
        timeout=kw.get("timeout", 5.0),
        poll_interval=kw.get("poll_interval", 0.05),
    )


def bridge_dir(layout: NodeLayout, folder: str) -> Path:
    return layout.agent_dir("boss") / BRIDGE_DIR / folder


async def answer_when_asked(layout: NodeLayout, text: str, *, settle: float = 0.0) -> None:
    """扮演值守会话：等问题出现，把答案写进 outbox 里同名的文件。"""
    inbox = bridge_dir(layout, INBOX)
    for _ in range(200):
        asks = sorted(inbox.glob("*.md"))
        if asks:
            reply = bridge_dir(layout, OUTBOX) / asks[0].name
            reply.write_text(text, encoding="utf-8")
            if settle:
                await asyncio.sleep(settle)
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等了很久也没等到问题写进 inbox")


# ---------- 装配：桥接 coordinator 要走编排，不是 BridgeHandler ----------


def test_a_bridge_coordinator_gets_the_orchestrator_not_the_mailbox(
    node: tuple[NodeLayout, Config],
) -> None:
    """**这是这次改动的全部要点。**

    以前 factory 只看 `agent.bridge` 就返回 BridgeHandler，于是桥接 coordinator
    收到任务只是把它写成一个文件躺在收件箱里 —— 拆解、派活、汇总一样不会发生，
    而两道闸都放行（`brain_of` 对桥接返回 "bridge" ≠ "echo"），人只能对着空白
    看板等到超时。现在它拿到的是编排状态机，只是问模型那一步改成问人。
    """
    layout, config = node

    handler = build_handler(layout=layout, config=config, agent_name="boss")

    assert handler.name != "bridge", "桥接 coordinator 又被当成普通桥接了"
    assert hasattr(handler, "tick"), "编排要靠 tick 做催办与超时"


def test_a_bridge_worker_is_still_a_plain_bridge(node: tuple[NodeLayout, Config]) -> None:
    """只有 coordinator 走岔路 —— 普通桥接 worker 那条路一个字都不该变。"""
    layout, config = node

    assert build_handler(layout=layout, config=config, agent_name="helper").name == "bridge"


# ---------- 问答：走的是普通消息那套约定 ----------


async def test_the_question_lands_in_the_inbox_and_the_answer_is_read_back(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, _ = node
    provider = provider_for(layout)

    asker = asyncio.create_task(provider.complete([Msg.user("拆一下这个目标")], []))
    await answer_when_asked(layout, '{"goal": "x"}')
    turn = await asker

    assert turn.text == '{"goal": "x"}'


async def test_the_question_is_readable_and_says_how_to_answer(
    node: tuple[NodeLayout, Config],
) -> None:
    """另一头是人，问题得能读、得写清楚怎么回 —— 否则这条路没人用得起来。"""
    layout, _ = node
    provider = provider_for(layout)

    asker = asyncio.create_task(provider.complete([Msg.user("拆一下这个目标")], []))
    inbox = bridge_dir(layout, INBOX)
    for _ in range(200):
        if sorted(inbox.glob("*.md")):
            break
        await asyncio.sleep(0.02)
    text = sorted(inbox.glob("*.md"))[0].read_text(encoding="utf-8")

    assert "拆一下这个目标" in text, "问题正文没带上"
    assert "outbox" in text, "没告诉人答案往哪写"
    assert "--text-file" in text, "含 JSON 的答案该走 --text-file，得写在提示里"

    await answer_when_asked(layout, "x")
    await asker


async def test_both_sides_of_the_exchange_are_archived(node: tuple[NodeLayout, Config]) -> None:
    """答过的问题不能留在 inbox 里 —— 值守会话会把它当成一条新待办反复看见。"""
    layout, _ = node
    provider = provider_for(layout)

    asker = asyncio.create_task(provider.complete([Msg.user("问题")], []))
    await answer_when_asked(layout, "答案")
    await asker

    assert sorted(bridge_dir(layout, INBOX).glob("*.md")) == []
    assert sorted(bridge_dir(layout, OUTBOX).glob("*.md")) == []
    assert len(sorted(bridge_dir(layout, DONE).glob("*.md"))) == 2, "问与答都该归档"


async def test_a_multi_turn_ask_shows_what_went_wrong_last_time(
    node: tuple[NodeLayout, Config],
) -> None:
    """计划不合法时 `generate_plan` 会带着「你上次回的」和「哪里不对」再问一遍。

    只渲染最后一条的话，人看不到自己错在哪 —— 那正是最需要看到的东西。
    """
    layout, _ = node
    provider = provider_for(layout)
    history = [
        Msg.user("第一次的要求"),
        Msg(role=Role.ASSISTANT, content="我上次回的那份烂计划"),
        Msg.user("哪里不对：assignee 不在名单里"),
    ]

    asker = asyncio.create_task(provider.complete(history, []))
    inbox = bridge_dir(layout, INBOX)
    for _ in range(200):
        if sorted(inbox.glob("*.md")):
            break
        await asyncio.sleep(0.02)
    text = sorted(inbox.glob("*.md"))[0].read_text(encoding="utf-8")

    assert "我上次回的那份烂计划" in text
    assert "assignee 不在名单里" in text

    await answer_when_asked(layout, "改好的")
    await asker


def test_render_ask_labels_each_turn() -> None:
    rendered = render_ask([Msg.user("要求"), Msg(role=Role.ASSISTANT, content="回答")])

    assert "【要求】" in rendered and "【你上一次的回答】" in rendered


# ---------- 边界 ----------


async def test_a_half_written_answer_is_not_read(node: tuple[NodeLayout, Config]) -> None:
    """边写边刷盘的文件抢着读走会拿到半句话，而半句话会被当成一份不合法的计划。

    判据和 `BridgeHandler.drafts()` 一样：mtime 至少 STABLE_SECONDS 之前。
    这里把超时压到 0.4 秒 —— 比稳定窗口短，所以「刚写完就读」必然读不到，
    从而证明那道守卫真的在起作用（不设这个上限的话，等一秒它就读到了，
    测试照样绿，但什么也没证明）。
    """
    layout, _ = node
    provider = provider_for(layout, timeout=0.4)

    asker = asyncio.create_task(provider.complete([Msg.user("问题")], []))
    await answer_when_asked(layout, "刚写完的答案")

    with pytest.raises(ProviderError, match="还没等到"):
        await asker


async def test_waiting_is_bounded(node: tuple[NodeLayout, Config]) -> None:
    """人走开了不能把 coordinator 永远焊死 —— 消费循环是串行的。

    超时抛 ProviderError，`generate_plan` 会把它变成一条「拆解任务失败」回给
    发起方，而不是让人对着空白看板等下去。
    """
    layout, _ = node
    provider = provider_for(layout, timeout=0.2)

    with pytest.raises(ProviderError, match="还没等到"):
        await provider.complete([Msg.user("没人会回答的问题")], [])

    assert sorted(bridge_dir(layout, INBOX).glob("*.md")), "超时后问题该留着，让人还能看到"


async def test_tools_are_refused_instead_of_silently_ignored(
    node: tuple[NodeLayout, Config],
) -> None:
    """人没法按工具调用的格式回话。编排从不传工具，所以走到这里说明接错了地方
    —— 假装支持会让调用方拿到一个永远没有 tool_calls 的 Turn，更难查。"""
    layout, _ = node
    provider = provider_for(layout)
    spec = ToolSpec(name="read_file", description="读文件", parameters={})

    with pytest.raises(ProviderError, match="不支持工具调用"):
        await provider.complete([Msg.user("问题")], [spec])
