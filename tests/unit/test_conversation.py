"""对话规则：@ 谁回给谁、轮次到顶就不接话。"""

from __future__ import annotations

import pytest

from anthill.agent.conversation import (
    ChatPlan,
    chat_payload,
    count_turns,
    message_expects_reply,
    plan_reply,
)
from anthill.core.envelope import Address, Envelope
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload
from anthill.providers.base import Msg, Role

ME = Address(node="n", agent="coder")


def chat_from(sender: str, *, mentions: tuple[str, ...] = ()) -> Envelope:
    return Envelope.new(
        sender=Address(node="n", agent=sender),
        recipient=ME,
        type=MessageType.CHAT,
        payload=ChatPayload(body="你怎么看", mentions=mentions),
    )


def turns(n: int) -> list[Msg]:
    """n 轮：每轮一条来件 + 一条我的回复。"""
    out: list[Msg] = []
    for i in range(n):
        out.append(Msg.user(f"第{i}问"))
        out.append(Msg(role=Role.ASSISTANT, content=f"第{i}答"))
    return out


# ---------- 回给谁 ----------


def test_a_plain_chat_is_answered_to_the_sender() -> None:
    plan = plan_reply(chat_from("cli"), identity=ME, history=[], budget=6)

    assert plan.should_reply
    assert plan.recipient is not None and plan.recipient.agent == "cli"
    assert not plan.expects_reply, "普通问答的答案不该再向对方索要一轮回复"
    assert chat_payload("答案", plan).mentions == ()


def test_a_mentioned_partner_gets_the_reply_not_the_sender() -> None:
    """人让 coder「跟 reviewer 讨论」，coder 的回信该发给 reviewer，不是发回给人。

    这就是「聊不起来」的根源 —— 回信总是回给发件人，球永远不在两个 Agent 之间。
    """
    plan = plan_reply(chat_from("cli", mentions=("reviewer",)), identity=ME, history=[], budget=6)

    assert plan.recipient is not None and plan.recipient.agent == "reviewer"


def test_the_reply_mentions_me_so_the_ball_comes_back() -> None:
    plan = plan_reply(chat_from("cli", mentions=("reviewer",)), identity=ME, history=[], budget=6)

    assert plan.mentions == ("coder",)
    payload = chat_payload("我的看法", plan)
    assert payload.mentions == ("coder",)
    assert plan.expects_reply, "显式 talk 才把球继续打回去"


def test_a_terminal_chat_answer_is_not_another_request() -> None:
    answer = chat_from("reviewer")
    answer = answer.model_copy(
        update={"payload": ChatPayload(body="检查通过"), "reply_to": "01J00000000000000000000000"}
    )

    assert not message_expects_reply(answer)
    assert message_expects_reply(chat_from("reviewer"))


def test_a_mention_of_myself_is_ignored_when_picking_the_partner() -> None:
    """@ 里带上自己是常事（对方把球打回来），不该因此自己跟自己聊。"""
    plan = plan_reply(chat_from("reviewer", mentions=("coder",)), identity=ME, history=[], budget=6)

    assert plan.recipient is not None and plan.recipient.agent == "reviewer"


# ---------- 什么时候停 ----------


def test_turns_are_counted_from_my_own_replies() -> None:
    assert count_turns([]) == 0
    assert count_turns(turns(3)) == 3


def test_the_conversation_stops_once_the_budget_is_used_up() -> None:
    """两个 Agent 互相回信如果没有别的刹车，只能等 hops 熔断。

    hops 是协议层的兜底，它一响就说明出事了，不该拿来当对话的正常终止方式。
    """
    plan = plan_reply(chat_from("reviewer"), identity=ME, history=turns(6), budget=6)

    assert not plan.should_reply
    assert "6" in plan.reason


def test_one_turn_below_the_budget_still_replies() -> None:
    assert plan_reply(chat_from("reviewer"), identity=ME, history=turns(5), budget=6).should_reply


def test_zero_budget_means_unlimited() -> None:
    """0 = 不限，仍然受 hops 约束。"""
    assert plan_reply(chat_from("reviewer"), identity=ME, history=turns(99), budget=0).should_reply


def test_tasks_are_not_subject_to_the_chat_budget() -> None:
    """预算只管对话。任务有自己的熔断（步数与 token），别把两件事混在一起。"""
    task = Envelope.new(
        sender=Address(node="n", agent="boss"),
        recipient=ME,
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="干活"),
    )

    plan = plan_reply(task, identity=ME, history=turns(99), budget=1)

    assert plan.should_reply
    assert plan.recipient is not None and plan.recipient.agent == "boss"


@pytest.mark.parametrize("budget", [1, 3, 10])
def test_the_budget_is_deterministic(budget: int) -> None:
    """不依赖模型自觉说「我说完了」—— 同样的历史必然得到同样的判定。"""
    assert plan_reply(
        chat_from("x"), identity=ME, history=turns(budget), budget=budget
    ) == ChatPlan(
        should_reply=False,
        reason=f"这个话题已经聊了 {budget} 轮，到上限 {budget}，不再接话",
    )


def test_a_cross_node_sender_mentioning_itself_gets_its_full_address_back() -> None:
    """跨机器的回信惯例是「把自己 @ 回去」—— 对面的 tst1 发来 chat 时
    mentions=("tst1",)。@ 规则把被 @ 的人解析成**本机节点**的地址，
    于是回信发给了不存在的 本机:tst1，route.failed。
    被 @ 的就是发件人自己时，用信封上的完整地址。
    Windows 实机复现：wtst 回 collab-tst:tst1 被路由拒绝，只能手写 outbox。
    """
    sender = Address(node="othermachine", agent="tst1")
    env = Envelope.new(
        sender=sender,
        recipient=ME,
        type=MessageType.CHAT,
        payload=ChatPayload(body="回我这条", mentions=("tst1",)),
    )

    plan = plan_reply(env, identity=ME, history=[], budget=6)

    assert plan.recipient == sender, "被 @ 的就是发件人时，该用信封上的完整地址（含节点）"


def test_a_mention_of_a_third_party_still_resolves_to_my_node() -> None:
    """@ 第三方（既不是我也不是发件人）时，信封上没有它的地址 ——
    维持「同机协作」的旧解析：本机节点。"""
    plan = plan_reply(chat_from("cli", mentions=("reviewer",)), identity=ME, history=[], budget=6)

    assert plan.recipient is not None
    assert plan.recipient.node == "n"
