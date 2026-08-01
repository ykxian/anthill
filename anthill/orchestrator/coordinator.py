"""Coordinator：接用户任务 → 拆计划 → 拓扑派活 → 催办 → 判定 done_when → 汇总。

**这是一个事件驱动的状态机，不是一段从头跑到尾的流程。**
原因很实在：agentd 的消费循环是串行的，如果 coordinator 在 handler 里 await
下游 worker 的结果，而那个结果又要经同一个循环投递进来，就是死锁。
所以它处理完一条消息立刻返回，状态全部落在黑板上（state.json）；
崩溃重启后从磁盘恢复，接着调度。

三处防跑飞：
- 计划是有限步的 DAG，拓扑调度天然终止；
- `done_when` 不满足时最多返工 MAX_REWORK_ROUNDS 轮；
- 派发的信封继承 hops，长链路会被协议层的 ttl_hops 熔断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from anthill.agent.handlers import HandlerContext
from anthill.core.envelope import Envelope
from anthill.core.errors import HopLimitExceeded, PlanError, UnknownRecipient
from anthill.core.ids import new_id, new_thread_id, now
from anthill.core.payloads import (
    ChatPayload,
    MessageType,
    TaskErrorPayload,
    TaskRequestPayload,
    TaskResultPayload,
)
from anthill.core.router import parse_address
from anthill.orchestrator.board import Blackboard
from anthill.orchestrator.plan import Plan, PlanStep, RosterEntry, generate_plan
from anthill.orchestrator.state import RunState, RunStore, StepRecord, StepState
from anthill.providers.base import ChatProvider, Msg
from anthill.security.policy import USER_ROLE

MAX_REWORK_ROUNDS = 2
DEFAULT_STEP_TIMEOUT = 600.0
DEFAULT_NUDGE_AFTER = 180.0
MAX_SUMMARY_CHARS = 4_000

STEP_BODY = """\
{task}

## 本步所属目标
{goal}

## 你的产物放这里
{task_dir}

{context}\
"""

DONE_WHEN_PROMPT = """\
判断下面这次协作是否达成了完成标准。

目标：{goal}
完成标准：{done_when}

各步骤的交付：
{deliverables}

只输出一个 JSON：{{"satisfied": true/false, "reason": "一句话理由", "fix": "若未达成，\
用一句话说明还需要做什么；达成则留空"}}\
"""


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    step_timeout: float = DEFAULT_STEP_TIMEOUT
    nudge_after: float = DEFAULT_NUDGE_AFTER
    max_rework_rounds: int = MAX_REWORK_ROUNDS


class CoordinatorHandler:
    name = "coordinator"

    def __init__(
        self,
        *,
        provider: ChatProvider,
        blackboard: Blackboard,
        settings: CoordinatorSettings | None = None,
    ) -> None:
        self._provider = provider
        self._board = blackboard
        self._store = RunStore(blackboard.root)
        self._settings = settings or CoordinatorSettings()

    async def aclose(self) -> None:
        await self._provider.aclose()

    # ---------- 入口 ----------

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
        if env.type is MessageType.TASK_REQUEST:
            await self._start_run(env, ctx)
        elif env.type in (MessageType.TASK_RESULT, MessageType.TASK_ERROR):
            await self._on_step_reply(env, ctx)
        else:
            ctx.log.info("msg.ignored", msg=env.id, type=str(env.type))

    async def tick(self, ctx: HandlerContext) -> None:
        """周期性扫描：先催办，再判超时。runtime 定时调用，handler 自己不起线程。"""
        for state in self._store.active():
            updated = await self._sweep_timeouts(state, ctx)
            if updated is not state:
                await self._advance(updated, ctx, env=None)

    # ---------- 开一次新运行 ----------

    async def _start_run(self, env: Envelope, ctx: HandlerContext) -> None:
        goal = _render_goal(env)
        roster = build_roster(ctx)
        if not roster:
            await self._reply_error(env, ctx, "本节点没有可派活的 Agent")
            return

        try:
            plan = await generate_plan(self._provider, goal=goal, roster=roster)
        except PlanError as exc:
            ctx.log.error("plan.failed", msg=env.id, error=str(exc))
            await self._reply_error(env, ctx, f"拆解任务失败：{exc}")
            return

        task_id = new_id()
        self._board.task_dir(task_id)
        state = RunState.start(
            task_id=task_id,
            plan=plan,
            requester=str(env.from_),
            root_thread=env.thread,
            root_msg_id=env.id,
        )
        ctx.log.info(
            "plan.created",
            task=task_id,
            thread=env.thread,
            steps=len(plan.steps),
            goal=plan.goal,
        )
        await self._advance(state, ctx, env=env)

    # ---------- 下游回信 ----------

    async def _on_step_reply(self, env: Envelope, ctx: HandlerContext) -> None:
        found = self._find_run(env.thread)
        if found is None:
            ctx.log.warn("step.orphan_reply", msg=env.id, thread=env.thread, frm=str(env.from_))
            return
        state, record = found

        if env.type is MessageType.TASK_RESULT:
            summary = str(getattr(env.payload, "summary", ""))[:MAX_SUMMARY_CHARS]
            artifacts = tuple(getattr(env.payload, "artifacts", ()))
            state = state.complete(record.id, summary=summary, artifacts=artifacts)
            ctx.log.info("step.done", task=state.task_id, step=record.id, frm=str(env.from_))
        else:
            error = str(getattr(env.payload, "error", ""))[:MAX_SUMMARY_CHARS]
            state = state.fail(record.id, error=error)
            ctx.log.warn("step.failed", task=state.task_id, step=record.id, error=error)

        await self._advance(state, ctx, env=env)

    def _find_run(self, thread: str) -> tuple[RunState, StepRecord] | None:
        for state in self._store.active():
            record = state.step_for_thread(thread)
            if record is not None and record.state is StepState.RUNNING:
                return state, record
        return None

    # ---------- 调度主循环（一次事件推进一步）----------

    async def _advance(self, state: RunState, ctx: HandlerContext, *, env: Envelope | None) -> None:
        """派出所有就绪步骤；全部落定后判 done_when。每次调用都以落盘收尾。"""
        for step_id in state.ready_steps():
            state = await self._dispatch(state, step_id, ctx, env=env)

        # 上游失败的分支永远等不到依赖，先标成 skipped，这次运行才可能收敛
        state = state.block_unreachable()

        if state.all_settled and not state.finished:
            state = await self._finalize(state, ctx, env=env)

        self._store.save(state)
        self._board.write(self._store.all())

    async def _dispatch(
        self, state: RunState, step_id: str, ctx: HandlerContext, *, env: Envelope | None
    ) -> RunState:
        step = state.plan.step(step_id)
        sub_thread = new_thread_id()
        body = self._compose_body(state, step)
        hops = (env.hops + 1) if env is not None else 1

        try:
            sent = await ctx.sender.send_new(
                to=parse_address(step.assignee, default_node=ctx.identity.node),
                type=MessageType.TASK_REQUEST,
                payload=TaskRequestPayload(title=_clip_title(step.task), body=body),
                thread=sub_thread,
                hops=hops,
            )
        except (HopLimitExceeded, UnknownRecipient, ValueError) as exc:
            ctx.log.error("step.dispatch_failed", task=state.task_id, step=step_id, error=str(exc))
            return state.fail(step_id, error=f"派发失败：{exc}")

        ctx.log.info(
            "step.dispatched",
            task=state.task_id,
            step=step_id,
            to=step.assignee,
            thread=sub_thread,
        )
        return state.dispatch(step_id, thread=sub_thread, msg_id=sent.id)

    def _compose_body(self, state: RunState, step: PlanStep) -> str:
        """把依赖步骤的产出喂给下一步 —— 否则 reviewer 根本不知道要审什么。"""
        upstream = [state.step(dep) for dep in step.depends_on]
        parts = []
        for record in upstream:
            files = ("，产物：" + ", ".join(record.artifacts)) if record.artifacts else ""
            parts.append(f"- 步骤 {record.id}（{record.assignee}）：{record.summary}{files}")
        context = "## 上游步骤的交付\n" + "\n".join(parts) if parts else ""
        return STEP_BODY.format(
            task=step.task,
            goal=state.plan.goal,
            task_dir=f"blackboard://tasks/{state.task_id}/",
            context=context,
        ).strip()

    # ---------- 收尾 ----------

    async def _finalize(
        self, state: RunState, ctx: HandlerContext, *, env: Envelope | None
    ) -> RunState:
        if state.failed_ids or state.skipped_ids:
            failed = ", ".join(sorted(state.failed_ids | state.skipped_ids))
            await self._send_final(
                state,
                ctx,
                MessageType.TASK_ERROR,
                TaskErrorPayload(error=f"步骤 {failed} 未完成：{_first_error(state)}"),
            )
            return state.finish(summary=f"步骤 {failed} 失败")

        verdict = await self._judge(state, ctx)
        if not verdict.satisfied and state.round < self._settings.max_rework_rounds:
            reworked = self._append_rework(state, verdict.fix or verdict.reason)
            ctx.log.info(
                "plan.rework", task=state.task_id, round=reworked.round, reason=verdict.reason
            )
            for step_id in reworked.ready_steps():
                reworked = await self._dispatch(reworked, step_id, ctx, env=env)
            return reworked

        summary = _aggregate(state, verdict)
        await self._send_final(
            state,
            ctx,
            MessageType.TASK_RESULT,
            TaskResultPayload(
                summary=summary,
                artifacts=_all_artifacts(state),
                status="ok" if verdict.satisfied else "partial",
            ),
        )
        ctx.log.info(
            "run.finished", task=state.task_id, satisfied=verdict.satisfied, rounds=state.round
        )
        return state.finish(summary=summary)

    async def _judge(self, state: RunState, ctx: HandlerContext) -> Verdict:
        """用 done_when 对照各步交付。没写 done_when 就默认达成，不为难模型。"""
        if not state.plan.done_when.strip():
            return Verdict(satisfied=True, reason="计划未给出完成标准，按各步已完成处理")
        prompt = DONE_WHEN_PROMPT.format(
            goal=state.plan.goal,
            done_when=state.plan.done_when,
            deliverables="\n".join(
                f"- {r.id}（{r.assignee}）：{r.summary or '（无交付说明）'}" for r in state.steps
            ),
        )
        try:
            turn = await self._provider.complete([Msg.user(prompt)], [])
        except Exception as exc:  # 判定失败不该让整次协作变成失败
            ctx.log.warn("done_when.judge_failed", task=state.task_id, error=str(exc))
            return Verdict(satisfied=True, reason=f"完成标准判定失败（{exc}），按已完成处理")
        return _parse_verdict(turn.text)

    def _append_rework(self, state: RunState, fix: str) -> RunState:
        """返工只追加一步，派给最后一个有产出的执行者。"""
        last = state.steps[-1]
        step_id = f"fix{state.round + 1}"
        step = PlanStep(
            id=step_id,
            assignee=last.assignee,
            task=f"上一轮未达成完成标准，请修复：{fix}",
            depends_on=(),
        )
        plan = Plan(
            goal=state.plan.goal,
            steps=(*state.plan.steps, step),
            done_when=state.plan.done_when,
        )
        record = StepRecord(
            id=step.id, assignee=step.assignee, task=step.task, depends_on=step.depends_on
        )
        return state.with_extra_steps(plan, (record,))

    async def _send_final(
        self,
        state: RunState,
        ctx: HandlerContext,
        kind: MessageType,
        payload: TaskResultPayload | TaskErrorPayload,
    ) -> None:
        await ctx.sender.send_new(
            to=parse_address(state.requester, default_node=ctx.identity.node),
            type=kind,
            payload=payload,
            thread=state.root_thread,
            reply_to=state.root_msg_id,
        )

    # ---------- 催办与超时 ----------

    async def _sweep_timeouts(self, state: RunState, ctx: HandlerContext) -> RunState:
        current = now()
        updated = state
        for record in state.steps:
            if record.state is not StepState.RUNNING or not record.dispatched_at:
                continue
            elapsed = _elapsed(record.dispatched_at, current)
            if elapsed > self._settings.step_timeout:
                ctx.log.warn(
                    "step.timeout", task=state.task_id, step=record.id, seconds=int(elapsed)
                )
                updated = updated.fail(record.id, error=f"超过 {int(elapsed)}s 未回复，判为失败")
            elif elapsed > self._settings.nudge_after and not record.nudged:
                await self._nudge(state, record, ctx)
                updated = updated.mark_nudged(record.id)
        return updated

    async def _nudge(self, state: RunState, record: StepRecord, ctx: HandlerContext) -> None:
        """催办走 chat，挂在该步的子 thread 上 —— 对方能直接看到自己在做的那件事。"""
        try:
            await ctx.sender.send_new(
                to=parse_address(record.assignee, default_node=ctx.identity.node),
                type=MessageType.CHAT,
                payload=ChatPayload(body=f"步骤 {record.id} 还在进行吗？需要的话说一声。"),
                thread=record.thread,
            )
            ctx.log.info("step.nudged", task=state.task_id, step=record.id)
        except (HopLimitExceeded, UnknownRecipient, ValueError) as exc:
            ctx.log.warn("step.nudge_failed", task=state.task_id, step=record.id, error=str(exc))

    # ---------- 杂项 ----------

    async def _reply_error(self, env: Envelope, ctx: HandlerContext, error: str) -> None:
        try:
            reply = env.reply(
                type=MessageType.TASK_ERROR,
                payload=TaskErrorPayload(error=error[:8000], retryable=False),
                sender=ctx.identity,
                recipient=env.from_,
            )
        except HopLimitExceeded as exc:
            ctx.log.warn("hop.limit", msg=env.id, error=str(exc))
            return
        await ctx.sender.send(reply)


@dataclass(frozen=True, slots=True)
class Verdict:
    satisfied: bool
    reason: str = ""
    fix: str = ""


def _parse_verdict(text: str) -> Verdict:
    """判定结果解析不了就按「已达成」处理：宁可少返工一轮，也不要卡住不给用户结果。"""
    start, end = text.find("{"), text.rfind("}")
    if not 0 <= start < end:
        return Verdict(satisfied=True, reason="判定输出无法解析，按已完成处理")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return Verdict(satisfied=True, reason="判定输出不是合法 JSON，按已完成处理")
    return Verdict(
        satisfied=bool(data.get("satisfied", True)),
        reason=str(data.get("reason", "")),
        fix=str(data.get("fix", "")),
    )


def build_roster(ctx: HandlerContext) -> tuple[RosterEntry, ...]:
    """可派活的对象：本节点上除自己与用户信箱之外的所有 Agent。"""
    return tuple(
        RosterEntry(name=name, role=agent.role, persona=agent.persona)
        for name, agent in sorted(ctx.config.agents.items())
        if name != ctx.identity.agent and agent.role != USER_ROLE
    )


def _render_goal(env: Envelope) -> str:
    title = str(getattr(env.payload, "title", ""))
    body = str(getattr(env.payload, "body", ""))
    return f"{title}\n{body}".strip() if body else title


def _clip_title(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return (flat[:limit] or "子任务") if len(flat) > limit else (flat or "子任务")


def _first_error(state: RunState) -> str:
    for record in state.steps:
        if record.state is StepState.FAILED and record.error:
            return record.error
    return "未知原因"


def _aggregate(state: RunState, verdict: Verdict) -> str:
    lines = [f"目标：{state.plan.goal}", ""]
    lines.extend(f"- {r.id}（{r.assignee}）：{r.summary or '（无交付说明）'}" for r in state.steps)
    if verdict.reason:
        lines.extend(["", f"完成标准判定：{verdict.reason}"])
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]


def _all_artifacts(state: RunState) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for record in state.steps:
        for path in record.artifacts:
            seen.setdefault(path, None)
    return tuple(seen)


def _elapsed(iso: str, current: datetime) -> float:
    try:
        return (current - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return 0.0
