"""带大脑的 handler：把一条来件跑成一次 ReAct 循环，再把结果原路寄回去。

它是 M1 的 EchoHandler 的同位替换 —— runtime 那一层不知道大脑是 echo 还是 LLM，
这正是 M1 把 handler 抽出来的目的。
"""

from __future__ import annotations

from anthill.agent.context import ContextBuilder
from anthill.agent.handlers import HandlerContext
from anthill.agent.loop import AgentLoop, LoopOutcome
from anthill.agent.memory import ThreadMemory
from anthill.agent.tools.base import Confirmer, Tool, ToolContext
from anthill.core.envelope import Envelope
from anthill.core.errors import BudgetExceeded, HopLimitExceeded, ProviderError
from anthill.core.payloads import (
    ChatPayload,
    MessageType,
    TaskErrorPayload,
    TaskResultPayload,
)
from anthill.providers.base import ChatProvider
from anthill.security.policy import PolicyEngine, TrustLevel, trust_of

MAX_SUMMARY_CHARS = 32_000


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
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._policy = policy
        self._builder = builder
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._trusted_peers = trusted_peers
        self._confirm = confirm

    async def aclose(self) -> None:
        """agentd 退出时释放上游连接。不关的话 httpx 客户端会留下未关闭的 socket。"""
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
        messages = self._builder.build(env, history=memory.load())
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
            ),
            log=ctx.log,
            trust=trust,
            max_steps=self._max_steps,
            token_budget=self._token_budget,
            confirm=self._confirm,
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
            finished=outcome.finished,
            artifacts=len(outcome.artifacts),
        )
        await self._reply_result(env, ctx, outcome)

    # ---------- 回信 ----------

    async def _reply_result(self, env: Envelope, ctx: HandlerContext, outcome: LoopOutcome) -> None:
        summary = outcome.summary[:MAX_SUMMARY_CHARS] or "（无输出）"
        if env.type is MessageType.CHAT:
            await self._send(env, ctx, MessageType.CHAT, ChatPayload(body=summary))
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
    ) -> None:
        try:
            reply = env.reply(
                type=msg_type, payload=payload, sender=ctx.identity, recipient=env.from_
            )
        except HopLimitExceeded as exc:
            ctx.log.warn("hop.limit", msg=env.id, thread=env.thread, error=str(exc))
            return
        await ctx.sender.send(reply)
