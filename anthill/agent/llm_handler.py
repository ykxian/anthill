"""带大脑的 handler：把一条来件跑成一次 ReAct 循环，再把结果原路寄回去。

它是 M1 的 EchoHandler 的同位替换 —— runtime 那一层不知道大脑是 echo 还是 LLM，
这正是 M1 把 handler 抽出来的目的。
"""

from __future__ import annotations

from dataclasses import replace

from anthill.agent.context import ContextBuilder
from anthill.agent.conversation import chat_payload, plan_reply
from anthill.agent.handlers import HandlerContext
from anthill.agent.loop import AgentLoop, LoopOutcome
from anthill.agent.memory import ThreadMemory
from anthill.agent.tools.base import Confirmer, Tool, ToolContext
from anthill.agent.tools.mcp_client import McpToolset
from anthill.core.config import McpSection
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import BudgetExceeded, HopLimitExceeded, ProviderError
from anthill.core.payloads import (
    ChatPayload,
    MessageType,
    RiskLevel,
    TaskErrorPayload,
    TaskRequestPayload,
    TaskResultPayload,
)
from anthill.core.router import parse_address
from anthill.providers.base import ChatProvider, Msg
from anthill.security.policy import PolicyEngine, TrustLevel, trust_of

MAX_SUMMARY_CHARS = 32_000
MAX_MENTION_BODY = 30_000


class EnvelopeMessenger:
    """把 send_message 工具的一句话变成一封挂在当前 thread 上的信。

    走 `env.reply()` 而不是 `send_new()`，是为了继承 thread 与 hops ——
    A@B、B@A 的死循环因此止于协议层，工具这边一行熔断逻辑都不用写。
    """

    def __init__(self, source: Envelope, ctx: HandlerContext) -> None:
        self._source = source
        self._ctx = ctx

    async def send(self, *, to: str, body: str, kind: str) -> str:
        recipient = parse_address(to, default_node=self._ctx.identity.node)
        payload = (
            TaskRequestPayload(title=_clip(body), body=body[:MAX_MENTION_BODY])
            if kind == "task"
            else ChatPayload(body=body[:MAX_MENTION_BODY])
        )
        env = self._source.reply(
            type=MessageType.TASK_REQUEST if kind == "task" else MessageType.CHAT,
            payload=payload,
            sender=self._ctx.identity,
            recipient=recipient,
        )
        await self._ctx.sender.send(env)
        self._ctx.log.info(
            "msg.sent_by_agent", msg=env.id, to=str(recipient), kind=kind, thread=env.thread
        )
        return env.id


def _clip(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return (flat[:limit] if len(flat) > limit else flat) or "消息"


class LlmHandler:
    name = "llm"

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tools: list[Tool],
        policy: PolicyEngine,
        builder: ContextBuilder,
        max_steps: int,
        token_budget: int,
        trusted_peers: frozenset[str] = frozenset(),
        confirm: Confirmer | None = None,
        chat_turns: int = 0,
        mcp_servers: dict[str, McpSection] | None = None,
        max_risk: RiskLevel = RiskLevel.HIGH,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._policy = policy
        self._builder = builder
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._trusted_peers = trusted_peers
        self._confirm = confirm
        self._chat_turns = chat_turns
        self._mcp_servers = mcp_servers or {}
        self._mcp: McpToolset | None = None
        self._max_risk = max_risk

    async def setup(self, ctx: HandlerContext) -> None:
        """连外部 MCP server，把它们的工具挂进来。

        为什么在这儿而不是 factory 里：连接是异步的，而 handler 是在 agentd 的
        `__init__`（同步）里造出来的。runtime 起循环之前会调这个钩子。

        **连不上不算致命** —— `McpToolset.connect` 自己吞掉异常并记日志，
        一个外部依赖挂了不该让整个 agentd 起不来。
        """
        if not self._mcp_servers:
            return
        self._mcp = McpToolset(log=ctx.log)
        await self._mcp.__aenter__()
        for name, section in sorted(self._mcp_servers.items()):
            await self._mcp.connect(name, section)
        if not self._mcp.tools:
            return
        self._tools = [*self._tools, *self._mcp.tools]
        # 工具清单会进 system prompt，得让模型知道多了这些
        self._builder = replace(self._builder, tools=self._tools)

    async def aclose(self) -> None:
        """agentd 退出时释放上游连接。不关的话 httpx 客户端会留下未关闭的 socket。"""
        if self._mcp is not None:
            await self._mcp.__aexit__(None, None, None)
            self._mcp = None
        await self._provider.aclose()

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
        if env.type not in (MessageType.TASK_REQUEST, MessageType.CHAT):
            ctx.log.info("msg.ignored", msg=env.id, type=str(env.type))
            return

        trust = trust_of(
            env.from_,
            local_node=ctx.config.node.name,
            roles={name: a.role for name, a in ctx.config.agents.items()},
            trusted_peers=set(self._trusted_peers),
        )
        if trust is TrustLevel.UNKNOWN:
            # 未知节点连上下文都不该进：注入的成本应该停在这里，而不是靠工具层兜底
            ctx.log.warn("msg.untrusted_source", msg=env.id, frm=str(env.from_))
            await self._reply_error(
                env, ctx, f"来源节点 {env.from_.node} 未受信任，拒绝处理", retryable=False
            )
            return

        memory = ThreadMemory(
            ThreadMemory.path_for(ctx.layout.agent_dir(ctx.identity.agent), env.thread)
        )
        # 先按「旧历史」组上下文，再把来件落盘 —— 顺序反了会让来件在上下文里出现两次
        history = memory.load()
        messages = self._builder.build(env, history=history)
        memory.append(self._builder.incoming(env))
        loop = AgentLoop(
            provider=self._provider,
            tools=self._tools,
            policy=self._policy,
            tool_ctx=ToolContext(
                workspace=ctx.layout.workspace,
                blackboard=ctx.layout.blackboard,
                security=ctx.config.security,
                thread=env.thread,
                messenger=EnvelopeMessenger(env, ctx),
            ),
            log=ctx.log,
            trust=trust,
            max_steps=self._max_steps,
            token_budget=self._token_budget,
            confirm=self._confirm,
            max_risk=self._max_risk,
        )

        try:
            # sink 让每一步即时落盘：熔断中止时，已经做过的工作仍留在 thread 历史里
            outcome = await loop.run(messages, sink=memory.append)
        except BudgetExceeded as exc:
            ctx.log.warn("task.budget", msg=env.id, thread=env.thread, error=str(exc))
            await self._reply_error(env, ctx, f"熔断：{exc}", retryable=False)
            return
        except ProviderError as exc:
            ctx.log.error("task.provider_failed", msg=env.id, error=str(exc))
            await self._reply_error(env, ctx, f"模型调用失败：{exc}", retryable=exc.retryable)
            return

        await memory.compact(self._provider)

        ctx.log.info(
            "task.done",
            msg=env.id,
            thread=env.thread,
            steps=outcome.steps,
            tokens=outcome.usage.total,
            # 分开记：两家的输入/输出单价差好几倍，只有总数折算不出钱
            in_tokens=outcome.usage.input_tokens,
            out_tokens=outcome.usage.output_tokens,
            provider=self._provider.name,
            model=self._provider.model,
            finished=outcome.finished,
            artifacts=len(outcome.artifacts),
        )
        await self._reply_result(env, ctx, outcome, history)

    # ---------- 回信 ----------

    async def _reply_result(
        self,
        env: Envelope,
        ctx: HandlerContext,
        outcome: LoopOutcome,
        history: list[Msg] | None = None,
    ) -> None:
        summary = outcome.summary[:MAX_SUMMARY_CHARS] or "（无输出）"
        if env.type is MessageType.CHAT:
            plan = plan_reply(
                env,
                identity=ctx.identity,
                history=history or [],
                budget=self._chat_turns,
            )
            if not plan.should_reply:
                ctx.log.info("chat.ended", msg=env.id, thread=env.thread, reason=plan.reason)
                return
            await self._send(
                env, ctx, MessageType.CHAT, chat_payload(summary, plan), plan.recipient
            )
            return
        status = "ok" if outcome.status == "ok" and outcome.finished else "partial"
        await self._send(
            env,
            ctx,
            MessageType.TASK_RESULT,
            TaskResultPayload(
                summary=summary,
                artifacts=outcome.artifacts,
                status=status,  # type: ignore[arg-type]
            ),
        )

    async def _reply_error(
        self, env: Envelope, ctx: HandlerContext, error: str, *, retryable: bool
    ) -> None:
        if env.type is MessageType.CHAT:
            await self._send(env, ctx, MessageType.CHAT, ChatPayload(body=f"处理失败：{error}"))
            return
        await self._send(
            env,
            ctx,
            MessageType.TASK_ERROR,
            TaskErrorPayload(error=error[:8000], retryable=retryable),
        )

    @staticmethod
    async def _send(
        env: Envelope,
        ctx: HandlerContext,
        msg_type: MessageType,
        payload: TaskResultPayload | TaskErrorPayload | ChatPayload,
        recipient: Address | None = None,
    ) -> None:
        try:
            reply = env.reply(
                type=msg_type,
                payload=payload,
                sender=ctx.identity,
                recipient=recipient or env.from_,
            )
        except HopLimitExceeded as exc:
            ctx.log.warn("hop.limit", msg=env.id, thread=env.thread, error=str(exc))
            return
        await ctx.sender.send(reply)
