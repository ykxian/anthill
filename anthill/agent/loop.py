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
import shlex
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from anthill.agent.tools.base import Confirmer, Tool, ToolContext, ToolResult
from anthill.core.errors import BudgetExceeded, ToolError
from anthill.core.evidence import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    EvidenceStore,
    bound_text,
)
from anthill.core.logging import EventLog
from anthill.core.payloads import EvidenceLevel, RiskLevel
from anthill.providers.base import ChatProvider, Msg, ToolCall, Turn, Usage, estimate_tokens
from anthill.security.policy import RISK_ORDER, PolicyEngine, TrustLevel

MAX_TOOL_RESULT_CHARS = MAX_TOOL_RESULT_BYTES

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
        max_risk: RiskLevel = RiskLevel.HIGH,
        evidence_owner: str = "anthill:agent-loop",
    ) -> None:
        self._provider = provider
        self._tools = {tool.name: tool for tool in tools}
        # 静态风险超上限的工具不给模型看，免得它反复撞墙。**有意保守**：
        # 按静态标签遮蔽，所以 run_shell（静态 high）对 cap=medium 的 Agent
        # 整个不可见 —— 哪怕白名单命令按 risk_for 只算 medium、本可合规。
        # 想给这种 Agent 开 shell 的路是抬 cap，不是把遮蔽改成按动态风险。
        # MCP 外部工具同理：McpTool.risk 默认 high、只能由人显式降级，
        # 所以戴帽 Agent 天然看不到未经人降级的外部工具 —— 语义同向。
        # 工具表本身留全：真被点名调用时由 _execute 按 risk_for 执法。
        self._specs = [tool.spec for tool in tools if RISK_ORDER[tool.risk] <= RISK_ORDER[max_risk]]
        self._policy = policy
        self._ctx = tool_ctx
        self._log = log
        self._trust = trust
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._confirm = confirm
        self._max_risk = max_risk
        self._evidence_owner = evidence_owner
        self._evidence_store = EvidenceStore(
            tool_ctx.blackboard / "details",
            reference_prefix=".anthill/blackboard/details",
        )

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
                bounded = bound_text(
                    result.content,
                    store=self._evidence_store,
                    owner=self._evidence_owner,
                    evidence_level=EvidenceLevel.TOOL_RESULT,
                    byte_limit=MAX_TOOL_RESULT_BYTES,
                    line_limit=MAX_TOOL_RESULT_LINES,
                    label=f"tool {call.name}",
                )
                tool_msg = Msg.tool_result(call.id, bounded.text, name=call.name)
                history = [*history, tool_msg]
                transcript = [*transcript, tool_msg]
                _emit(sink, tool_msg)
                if bounded.details is not None:
                    self._log.info(
                        "tool.externalized",
                        tool=call.name,
                        thread=self._ctx.thread,
                        details=bounded.details.path,
                        sha256=bounded.details.sha256,
                        bytes=bounded.details.bytes,
                        lines=bounded.details.lines,
                        status=bounded.details.status,
                    )
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
        if violation := _hard_query_violation(call):
            self._log.warn(
                "tool.query_scope_rejected",
                tool=call.name,
                thread=self._ctx.thread,
                reason=violation,
            )
            return ToolResult.failed(f"HARD_QUERY_SCOPE_REQUIRED: {violation}")
        tool = self._tools.get(call.name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "（无）"
            self._log.warn("tool.unknown", tool=call.name, thread=self._ctx.thread)
            return ToolResult.failed(f"未知工具 {call.name}；你可用的工具是：{known}")

        risk = tool.risk_for(call.arguments, self._ctx)
        if RISK_ORDER[risk] > RISK_ORDER[self._max_risk]:
            # cap 是 Agent 自身的特权边界，先于一切放宽生效 ——
            # 放在 authorize 之前，②的无人值守白名单越不过它
            self._log.warn(
                "policy.capped",
                tool=tool.name,
                risk=str(risk),
                cap=str(self._max_risk),
                thread=self._ctx.thread,
            )
            return ToolResult.failed(
                f"{tool.name} 这次调用的风险是 {risk}，超出你的上限 {self._max_risk}"
                "（max_tool_risk）。请换一个更安全的做法。"
            )
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


def _hard_query_violation(call: ToolCall) -> str | None:
    """Reject known context-amplifying shell queries before execution."""
    if call.name != "run_shell":
        return None
    command = str(call.arguments.get("command", ""))
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return "shell command is not parseable"

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        violation = _segment_scope_violation(segment)
        if violation:
            return violation
    return None


def _segment_scope_violation(tokens: list[str]) -> str | None:
    for index, token in enumerate(tokens):
        executable = token.rsplit("/", 1)[-1]
        tail = tokens[index + 1 :]
        if executable == "git":
            for verb in ("status", "diff"):
                if verb not in tail:
                    continue
                args = tail[tail.index(verb) + 1 :]
                if "--" not in args or not args[args.index("--") + 1 :]:
                    return f"git {verb} must use `-- <claimed-path> ...`"
        if executable == "docker":
            if tail[:1] == ["ps"] and not any(
                arg == "--filter" or arg.startswith("--filter=") for arg in tail[1:]
            ):
                return "docker ps must include a container/service --filter"
            if tail[:2] == ["compose", "ps"] and not _has_positional(tail[2:]):
                return "docker compose ps must name a service"
            if tail[:1] == ["logs"]:
                return _log_scope_violation("docker logs", tail[1:])
            if tail[:2] == ["compose", "logs"]:
                return _log_scope_violation("docker compose logs", tail[2:])
        if executable == "journalctl":
            has_since = any(arg == "--since" or arg.startswith("--since=") for arg in tail)
            has_lines = any(
                arg in {"-n", "--lines"} or arg.startswith(("-n=", "--lines=")) for arg in tail
            )
            if not has_since or not has_lines:
                return "journalctl must include both --since and -n/--lines"
    return None


def _log_scope_violation(command: str, args: list[str]) -> str | None:
    has_since = any(arg == "--since" or arg.startswith("--since=") for arg in args)
    has_tail = any(arg == "--tail" or arg.startswith("--tail=") for arg in args)
    if not has_since or not has_tail or not _has_positional(args):
        return f"{command} must name a target and include --since plus --tail"
    return None


def _has_positional(args: list[str]) -> bool:
    options_with_value = {"--filter", "--since", "--tail", "--lines", "-n"}
    skip = False
    for token in args:
        if skip:
            skip = False
            continue
        if token in options_with_value:
            skip = True
            continue
        if token == "--":
            continue
        if not token.startswith("-"):
            return True
    return False
