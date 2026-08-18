"""ReAct 工具循环（03-tech-design §4）。

    模型出牌 → 有工具调用就执行 → 结果回喂 → 再出牌 …… 直到 finish 或熔断。

三条防跑飞的闸门，缺一不可：
- **步数**：模型陷在「读文件 → 再读一遍」的循环里时兜底；
- **token**：调试时最容易失控的是钱，累计超预算立刻停；
- **策略**：每次工具调用都过一遍风险 × 信任判定，危险动作要人点头。

工具失败一律作为 `role=tool` 的结果回喂给模型，而不是抛异常 ——
让模型看见错误自己改，是 ReAct 最有价值的部分。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from anthill.agent.tools.base import Confirmer, Tool, ToolContext, ToolResult
from anthill.core.errors import BudgetExceeded, ToolError
from anthill.core.logging import EventLog
from anthill.providers.base import ChatProvider, Msg, ToolCall, Turn, Usage, estimate_tokens
from anthill.security.policy import PolicyEngine, TrustLevel

MAX_TOOL_RESULT_CHARS = 16_000

MessageSink = Callable[[Msg], None]
"""每产生一条消息就回调一次。落盘失败不该拖垮任务，所以调用点会吞掉它的异常并记日志。"""


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """循环的交付物。`finished=False` 表示模型是用纯文本收尾的，没走 finish 工具。"""

    summary: str
    artifacts: tuple[str, ...] = ()
    status: str = "ok"
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    finished: bool = False
    transcript: tuple[Msg, ...] = ()


class AgentLoop:
    def __init__(
        self,
        *,
        provider: ChatProvider,
        tools: list[Tool],
        policy: PolicyEngine,
        tool_ctx: ToolContext,
        log: EventLog,
        trust: TrustLevel,
        max_steps: int,
        token_budget: int,
        confirm: Confirmer | None = None,
    ) -> None:
        self._provider = provider
        self._tools = {tool.name: tool for tool in tools}
        self._specs = [tool.spec for tool in tools]
        self._policy = policy
        self._ctx = tool_ctx
        self._log = log
        self._trust = trust
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._confirm = confirm

    async def run(self, messages: list[Msg], *, sink: MessageSink | None = None) -> LoopOutcome:
        """跑完一次任务。

        `sink` 每产生一条消息就回调一次（handler 用它把历史即时落盘）——
        熔断时 `run` 会抛异常，靠返回值持久化的话，已经做过的工作就白做了。
        """
        history = list(messages)
        transcript: list[Msg] = []
        usage = Usage()

        for step in range(1, self._max_steps + 1):
            turn = await self._provider.complete(history, self._specs)
            usage = usage + _usage_of(turn)
            self._log.info(
                "llm.turn",
                step=step,
                thread=self._ctx.thread,
                model=self._provider.model,
                tool_calls=len(turn.tool_calls),
                tokens=usage.total,
            )

            if not turn.tool_calls:
                # 纯文本收尾也要落进历史：否则下一轮里 Agent 记得别人说过什么，
                # 却不记得自己说过什么 —— 多轮对话会变成单方面的复读
                answer = turn.to_msg()
                _emit(sink, answer)
                return LoopOutcome(
                    summary=turn.text.strip() or "（模型没有产出内容）",
                    steps=step,
                    usage=usage,
                    transcript=(*transcript, answer),
                )

            assistant_msg = turn.to_msg()
            history = [*history, assistant_msg]
            transcript = [*transcript, assistant_msg]
            _emit(sink, assistant_msg)

            finish: ToolResult | None = None
            for call in turn.tool_calls:
                result = await self._execute(call)
                tool_msg = Msg.tool_result(
                    call.id, result.truncated(MAX_TOOL_RESULT_CHARS).content, name=call.name
                )
                history = [*history, tool_msg]
                transcript = [*transcript, tool_msg]
                _emit(sink, tool_msg)
                if result.is_finish and finish is None:
                    finish = result

            if finish is not None:
                return LoopOutcome(
                    summary=finish.content,
                    artifacts=finish.artifacts,
                    status=finish.status,
                    steps=step,
                    usage=usage,
                    finished=True,
                    transcript=tuple(transcript),
                )

            if usage.total > self._token_budget:
                raise BudgetExceeded(
                    f"累计 token {usage.total} 超过预算 {self._token_budget}，已在第 {step} 步中止"
                )

        raise BudgetExceeded(f"已用满 {self._max_steps} 步仍未调用 finish 收尾，中止以防跑飞")

    # ---------- 单次工具调用 ----------

    async def _execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "（无）"
            self._log.warn("tool.unknown", tool=call.name, thread=self._ctx.thread)
            return ToolResult.failed(f"未知工具 {call.name}；你可用的工具是：{known}")

        risk = tool.risk_for(call.arguments, self._ctx)
        verdict = await self._policy.authorize(
            tool=tool.name,
            risk=risk,
            trust=self._trust,
            detail=_describe(tool, call.arguments),
            confirm=self._confirm,
        )
        if not verdict.allowed:
            self._log.warn(
                "policy.denied",
                tool=tool.name,
                risk=str(risk),
                trust=self._trust.name,
                thread=self._ctx.thread,
                reason=verdict.reason,
            )
            return ToolResult.failed(f"{verdict.reason}。请换一个更安全的做法。")
        if verdict.auto_allowed:
            # 白名单放行不是静默特权：本要问人的操作被放过去了，必须响一声
            self._log.warn(
                "policy.auto_allowed",
                tool=tool.name,
                risk=str(risk),
                trust=self._trust.name,
                thread=self._ctx.thread,
            )

        try:
            result = await tool.run(call.arguments, self._ctx)
        except ToolError as exc:
            self._log.error("tool.error", tool=tool.name, error=str(exc))
            return ToolResult.failed(f"工具 {tool.name} 执行失败：{exc}")
        except Exception as exc:  # 工具是插件，崩了也不能拖垮整个 Agent
            self._log.error("tool.crashed", tool=tool.name, error=f"{type(exc).__name__}: {exc}")
            return ToolResult.failed(f"工具 {tool.name} 内部错误：{type(exc).__name__}: {exc}")

        self._log.info(
            "tool.done",
            tool=tool.name,
            ok=result.ok,
            risk=str(risk),
            thread=self._ctx.thread,
            chars=len(result.content),
        )
        return result


def _emit(sink: MessageSink | None, msg: Msg) -> None:
    if sink is None:
        return
    # 历史落盘失败不该让任务失败：任务本身还在正常推进，丢的只是可复用的上下文
    with suppress(OSError):
        sink(msg)


def _describe(tool: Tool, args: dict[str, Any]) -> str:
    describe = getattr(tool, "describe_call", None)
    if callable(describe):
        return str(describe(args))
    return json.dumps(args, ensure_ascii=False)[:200]


def _usage_of(turn: Turn) -> Usage:
    """有些兼容端点不回 usage，退化成估算，好让费用熔断始终有输入。"""
    if turn.usage.total > 0:
        return turn.usage
    return Usage(output_tokens=estimate_tokens(turn.text))
