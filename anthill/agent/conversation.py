"""对话：让「两个 Agent 就一件事聊下去」这件事有确定的规则。

现状的两个问题，这里各给一个答案：

**1. 回信永远发给发件人，所以聊不起来。**
   人让 coder「跟 reviewer 讨论一下」，coder 的回信却回给了人。
   解法用的是协议里本来就有的 `ChatPayload.mentions`：
   带 @ 的对话，回信发给被 @ 的那个人，并把自己 @ 回去 —— 球就在两人之间来回了。

**2. 只有 hops 能让对话停下来。**
   hops 是协议层的**兜底**，不该拿来当对话的正常终止方式（它一响就说明出事了）。
   所以按 thread 数轮次：同一话题里，一个 Agent 最多接 `chat_turns` 轮。
   这是**确定性**的 —— 不依赖模型自觉说「我说完了」。
"""

from __future__ import annotations

from dataclasses import dataclass

from anthill.core.envelope import Address, Envelope
from anthill.core.payloads import ChatPayload, MessageType
from anthill.providers.base import Msg, Role


@dataclass(frozen=True, slots=True)
class ChatPlan:
    """这一轮对话该怎么回：回给谁、@ 回谁；不该回就 `should_reply=False`。"""

    should_reply: bool
    recipient: Address | None = None
    mentions: tuple[str, ...] = ()
    reason: str = ""


def count_turns(history: list[Msg]) -> int:
    """这个 thread 里我已经开口过几次。

    历史是 (来件, 我的回复) 交替追加的，所以数自己说过的话就是轮次。
    """
    return sum(1 for m in history if m.role is Role.ASSISTANT)


def plan_reply(env: Envelope, *, identity: Address, history: list[Msg], budget: int) -> ChatPlan:
    """决定这条 chat 要不要回、回给谁。

    `budget=0` 表示不限轮次（仍然受协议层 hops 约束）。
    """
    if env.type is not MessageType.CHAT:
        return ChatPlan(should_reply=True, recipient=env.from_)

    turns = count_turns(history)
    if budget and turns >= budget:
        return ChatPlan(
            should_reply=False,
            reason=f"这个话题已经聊了 {turns} 轮，到上限 {budget}，不再接话",
        )

    partner = _partner(env, identity)
    return ChatPlan(
        should_reply=True,
        recipient=partner,
        # 把自己 @ 回去，对方才知道该继续跟我聊，而不是回给最初那个人
        mentions=(identity.agent,),
    )


def _partner(env: Envelope, identity: Address) -> Address:
    """带 @ 的对话，回给被 @ 的那个人；否则回给发件人。

    @ 只带裸名字，节点得自己补：**被 @ 的就是发件人自己时用信封上的完整地址**
    —— 跨机器的回信惯例是「把自己 @ 回去」，这时补成本机节点等于把回信发给
    一个不存在的 本机:对方名，route.failed（Windows 实机踩过）。
    @ 第三方时信封上没有它的地址，维持同机协作的旧解析：本机节点。
    """
    mentions = tuple(getattr(env.payload, "mentions", ()) or ())
    for name in mentions:
        if name == identity.agent:
            continue
        if name == env.from_.agent:
            return env.from_
        return Address(node=identity.node, agent=name)
    return env.from_


def chat_payload(body: str, plan: ChatPlan) -> ChatPayload:
    return ChatPayload(body=body, mentions=plan.mentions)
