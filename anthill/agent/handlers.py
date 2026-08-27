"""消息处理器接口 + M1 的 echo 实现。

Agent 的「大脑」（M2 的 ReAct loop）就是换一个 handler 插进来，
runtime 那一层完全不需要知道大脑是 LLM 还是 echo。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from anthill.agent.conversation import message_expects_reply
from anthill.agent.sender import Sender
from anthill.core.config import AgentSection, Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import HopLimitExceeded
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, TaskResultPayload

MAX_ECHO_CHARS = 2000


@dataclass(frozen=True, slots=True)
class HandlerContext:
    identity: Address
    agent: AgentSection
    sender: Sender
    layout: NodeLayout
    config: Config
    log: EventLog


@runtime_checkable
class MessageHandler(Protocol):
    name: str

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None: ...


class EchoHandler:
    """没有大脑的 Agent：原样回显。

    价值不在功能，而在于它让 M1 可以独立演示 ——
    收件 → 回执 → 结果 这条链路是真的通的，且不花一分钱 API 费。
    """

    name = "echo"

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
        if env.type is MessageType.TASK_REQUEST:
            await self._reply_task(env, ctx)
        elif env.type is MessageType.CHAT:
            if message_expects_reply(env):
                await self._reply_chat(env, ctx)
            else:
                ctx.log.info("chat.ended", msg=env.id, thread=env.thread, reason="对方不等回复")
        else:
            ctx.log.info("msg.ignored", msg=env.id, type=str(env.type))

    async def _reply_task(self, env: Envelope, ctx: HandlerContext) -> None:
        title = getattr(env.payload, "title", "")
        body = getattr(env.payload, "body", "")
        summary = f"echo[{ctx.identity.agent}] 收到任务「{title}」：{body}"[:MAX_ECHO_CHARS]
        await self._reply(env, ctx, MessageType.TASK_RESULT, TaskResultPayload(summary=summary))

    async def _reply_chat(self, env: Envelope, ctx: HandlerContext) -> None:
        body = getattr(env.payload, "body", "")
        text = f"echo[{ctx.identity.agent}]: {body}"[:MAX_ECHO_CHARS]
        await self._reply(
            env,
            ctx,
            MessageType.CHAT,
            ChatPayload(body=text),
        )

    @staticmethod
    async def _reply(
        env: Envelope,
        ctx: HandlerContext,
        msg_type: MessageType,
        payload: TaskResultPayload | ChatPayload,
    ) -> None:
        try:
            reply = env.reply(
                type=msg_type, payload=payload, sender=ctx.identity, recipient=env.from_
            )
        except HopLimitExceeded as exc:
            # 熔断：两个 echo agent 互相回信是最容易构造出来的消息风暴
            ctx.log.warn("hop.limit", msg=env.id, thread=env.thread, error=str(exc))
            return
        await ctx.sender.send(reply)
