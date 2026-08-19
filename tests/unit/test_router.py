"""02-protocol §5：角色寻址、广播、@mention 与跳数熔断。"""

from __future__ import annotations

import pytest

from anthill.core.envelope import Address, Envelope
from anthill.core.errors import HopLimitExceeded, UnknownRecipient
from anthill.core.mailbox import Mailbox
from anthill.core.payloads import ChatPayload, EventPayload, MessageType
from anthill.core.router import Router, extract_mentions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("写完了 @reviewer 帮我看看", ("reviewer",)),
        ("@coder @reviewer 都来", ("coder", "reviewer")),
        ("@coder 再 @coder 一次", ("coder",)),
        ("邮箱 a@b.com 不是 mention", ()),
        ("没有任何提及", ()),
    ],
)
def test_extract_mentions(text, expected):
    assert extract_mentions(text) == expected


def test_concrete_address_passes_through(config, layout, make_task):
    router = Router(config, layout)
    env = make_task()

    resolved = router.resolve(env)

    assert len(resolved) == 1
    assert resolved[0].to.agent == "beta"


def test_role_address_resolves_to_least_loaded_agent(config, layout, make_task, addr):
    """beta 与 gamma 同为 worker：谁 inbox 里积压少就派给谁。"""
    Mailbox(layout.mailbox_dir("beta")).deposit(make_task())
    router = Router(config, layout)
    env = make_task(recipient="beta").model_copy(
        update={"to": Address(node="testnode", agent="role:worker")}
    )

    resolved = router.resolve(env)

    assert [e.to.agent for e in resolved] == ["gamma"]


def test_unknown_role_is_rejected_with_helpful_message(config, layout, make_task):
    router = Router(config, layout)
    env = make_task().model_copy(update={"to": Address(node="testnode", agent="role:reviewer")})

    with pytest.raises(UnknownRecipient, match="reviewer"):
        router.resolve(env)


def test_unknown_agent_is_rejected(config, layout, make_task):
    router = Router(config, layout)
    env = make_task().model_copy(update={"to": Address(node="testnode", agent="ghost")})

    with pytest.raises(UnknownRecipient):
        router.resolve(env)


def test_broadcast_fans_out_to_everyone_but_sender(config, layout, addr):
    router = Router(config, layout)
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=Address(node="testnode", agent="all"),
        type=MessageType.EVENT,
        payload=EventPayload(kind="board.updated"),
    )

    resolved = router.resolve(env)

    assert sorted(e.to.agent for e in resolved) == ["beta", "cli", "gamma"]
    assert len({e.id for e in resolved}) == 3  # 每个收件人一条独立信封，便于分别追踪


def test_remote_address_is_left_for_transport_layer(config, layout, addr):
    router = Router(config, layout)
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=Address(node="lab-server", agent="runner"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="在服务器跑一下"),
    )

    assert router.resolve(env) == (env,)


def test_check_hops_refuses_overflow(addr):
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="x"),
        hops=8,
        ttl_hops=8,
    )
    Router.check_hops(env)  # 正好等于上限，仍然放行

    with pytest.raises(HopLimitExceeded):
        Router.check_hops(env.model_copy(update={"hops": 9}))


# ---------- 配置热感知：面板刚加的 Agent 立刻能收信 ----------


def test_router_picks_up_agents_added_after_startup(config, layout, make_task):
    """agentd 的成员名单以前是启动时载入死的 —— 面板上刚加的 Agent，
    别人发信给它会被「本节点没有 xxx」拒成死信，直到手动重启 agentd。
    实战里 battle_plan:agent1 → mzq 就是这么被拒了两次。"""
    router = Router(config, layout)
    env = make_task().model_copy(update={"to": Address(node="testnode", agent="mzq")})
    with pytest.raises(UnknownRecipient):
        router.resolve(env)

    toml = layout.node_toml.read_text(encoding="utf-8")
    layout.node_toml.write_text(toml + '\n[agents.mzq]\nrole = "worker"\n', encoding="utf-8")

    resolved = router.resolve(env)  # 同一个 Router 实例，不重建不重启

    assert [e.to.agent for e in resolved] == ["mzq"]


def test_router_sees_new_role_members_too(config, layout, make_task):
    router = Router(config, layout)
    env = make_task().model_copy(update={"to": Address(node="testnode", agent="role:reviewer")})
    with pytest.raises(UnknownRecipient):
        router.resolve(env)

    toml = layout.node_toml.read_text(encoding="utf-8")
    layout.node_toml.write_text(toml + '\n[agents.rev]\nrole = "reviewer"\n', encoding="utf-8")

    resolved = router.resolve(env)

    assert [e.to.agent for e in resolved] == ["rev"]


def test_router_keeps_the_old_config_when_the_edit_is_broken(config, layout, make_task):
    """有人正把 node.toml 改到一半：路由层不许崩、也不许把在跑的投递断掉 ——
    继续用旧配置，等文件改好（mtime 再变）自然接上。"""
    router = Router(config, layout)
    router.resolve(make_task())  # 先热身一次

    layout.node_toml.write_text("[node\n这不是合法 TOML", encoding="utf-8")

    resolved = router.resolve(make_task())  # 老收件人照常可达
    assert resolved[0].to.agent == "beta"
    env = make_task().model_copy(update={"to": Address(node="testnode", agent="ghost")})
    with pytest.raises(UnknownRecipient):  # 未知的还是干净地拒，不是崩
        router.resolve(env)
