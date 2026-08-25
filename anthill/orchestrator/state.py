"""一次编排运行的状态：谁在做哪一步、做到哪了、结果是什么。

**状态落在黑板上而不是内存里**，这是 coordinator 敢做成事件驱动的前提：
它处理完一条消息就返回，进程崩了重启，从 state.json 就能接着调度。

所有更新都返回新对象（`model_copy`），没有任何就地修改 ——
调度逻辑因此可以放心地把「旧状态」和「新状态」摆在一起比对。
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.atomic import atomic_write
from anthill.core.ids import is_valid_id, now
from anthill.orchestrator.plan import Plan

STATE_FILE = "state.json"
TASKS_DIR = "tasks"


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    """上游失败导致永远不可能开始。和 failed 分开记，排查时一眼看出「谁真的坏了」。"""

    NOT_NEEDED = "not_needed"
    """条件不成立所以不用跑 —— 兜底步骤的上游全成功了，就是这个状态。

    和 SKIPPED 分开，是因为**这不是坏消息**。合在一起的话，
    「计划里写了 run_if=upstream_failed 的兜底步骤」这件事本身
    就会让每一次顺利的运行都被判成失败（见 coordinator._finalize）。
    """


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    assignee: str
    task: str
    depends_on: tuple[str, ...] = ()
    state: StepState = StepState.PENDING
    thread: str = ""
    """派发时新开的子 thread —— 每步一个，父 thread 只挂计划与汇总，隔离上下文防串扰。"""

    msg_id: str = ""
    summary: str = ""
    artifacts: tuple[str, ...] = ()
    error: str = ""
    attempts: int = 0
    nudged: bool = False
    """是否已经催办过。催过一次还不回就判失败，不无限催。"""

    dispatched_at: str = ""
    finished_at: str = ""

    terminal: bool = False
    """这次失败是不是**定论**，重跑也没有意义。

    人拒绝了这一步就是定论：再派一次只会再问一次审批，人已经答过了。
    机器故障（超时、崩溃、模型抽风）则相反，重跑是有意义的 —— 默认按后者算。
    """

    @property
    def is_settled(self) -> bool:
        return self.state in (
            StepState.DONE,
            StepState.FAILED,
            StepState.SKIPPED,
            StepState.NOT_NEEDED,
        )


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    plan: Plan
    requester: str
    """回信地址，形如 `node:agent`。"""

    root_thread: str
    root_msg_id: str
    steps: tuple[StepRecord, ...]
    round: int = 0
    """返工轮次。done_when 不满足时追加修复步骤，上限见 coordinator.MAX_REWORK_ROUNDS。"""

    finished: bool = False
    result: str = ""
    started_at: str = Field(default="")

    # ---------- 构造 ----------

    @classmethod
    def start(
        cls,
        *,
        task_id: str,
        plan: Plan,
        requester: str,
        root_thread: str,
        root_msg_id: str,
    ) -> RunState:
        return cls(
            task_id=task_id,
            plan=plan,
            requester=requester,
            root_thread=root_thread,
            root_msg_id=root_msg_id,
            steps=tuple(
                StepRecord(id=s.id, assignee=s.assignee, task=s.task, depends_on=s.depends_on)
                for s in plan.steps
            ),
            started_at=now().isoformat(),
        )

    def fork(self, from_step: str, *, task_id: str, root_thread: str, root_msg_id: str) -> RunState:
        """从某一步开始重跑的新 run（纯数据操作，调度由 coordinator 自然接手）。

        重置集 = `from_step` 的**传递闭包下游**（含自己）∪ 所有非 DONE 的步骤；
        只有「与重跑无关且真的成过」的步骤连 summary/artifacts 一起保留 ——
        下游的 _compose_body 直接可用。闭包按 depends_on 算：for_each 展开的
        步骤是普通节点、run_if 在重算就绪时自然生效，都不需要特判。

        安全语义（有意）：新 task_id 意味着 needs_approval 步骤的审批 id
        全部重新派生 —— **批准不跨 fork 继承**，重置的步骤要重新走审批。
        保留步骤里的旧 thread 无害：只有 RUNNING 状态才被回信认领。
        """
        self.step(from_step)  # 不存在就 KeyError
        downstream = {from_step}
        changed = True
        while changed:
            changed = False
            for record in self.steps:
                if record.id not in downstream and any(
                    dep in downstream for dep in record.depends_on
                ):
                    downstream.add(record.id)
                    changed = True
        records = tuple(
            StepRecord(id=r.id, assignee=r.assignee, task=r.task, depends_on=r.depends_on)
            if r.id in downstream or r.state is not StepState.DONE
            else r
            for r in self.steps
        )
        return RunState(
            task_id=task_id,
            plan=self.plan,
            requester=self.requester,
            root_thread=root_thread,
            root_msg_id=root_msg_id,
            steps=records,
            round=self.round,
            started_at=now().isoformat(),
        )

    # ---------- 查询 ----------

    def step(self, step_id: str) -> StepRecord:
        for record in self.steps:
            if record.id == step_id:
                return record
        raise KeyError(f"运行 {self.task_id} 里没有步骤 {step_id!r}")

    def step_for_thread(self, thread: str) -> StepRecord | None:
        """worker 的回信按子 thread 认领步骤 —— 这就是「哪一步回来了」的判定方式。"""
        for record in self.steps:
            if record.thread and record.thread == thread:
                return record
        return None

    @property
    def done_ids(self) -> set[str]:
        return {r.id for r in self.steps if r.state is StepState.DONE}

    @property
    def busy_ids(self) -> set[str]:
        return {r.id for r in self.steps if r.state is StepState.RUNNING}

    @property
    def failed_ids(self) -> set[str]:
        return {r.id for r in self.steps if r.state is StepState.FAILED}

    @property
    def skipped_ids(self) -> set[str]:
        return {r.id for r in self.steps if r.state is StepState.SKIPPED}

    @property
    def not_needed_ids(self) -> set[str]:
        return {r.id for r in self.steps if r.state is StepState.NOT_NEEDED}

    @property
    def broken_ids(self) -> set[str]:
        """真的没做成的步骤 —— 收尾时判成败看这个，别把 not_needed 算进来。"""
        return self.failed_ids | self.skipped_ids

    @property
    def _dead_ids(self) -> set[str]:
        """对下游而言「没成功」的步骤。not_needed 也在内 —— 它同样没产出，
        依赖它的普通步骤等不到东西，得跟着落定。"""
        return self.failed_ids | self.skipped_ids | self.not_needed_ids

    @property
    def all_settled(self) -> bool:
        return all(r.is_settled for r in self.steps)

    def ready_steps(self) -> tuple[str, ...]:
        """排除所有非 pending 的步骤，否则失败的那步会被当成「还没派」反复重派。"""
        taken = {r.id for r in self.steps if r.state is not StepState.PENDING}
        return tuple(
            s.id for s in self.plan.ready(done=self.done_ids, taken=taken, dead=self._dead_ids)
        )

    def block_unreachable(self) -> RunState:
        """把「上游已经全部落定、但条件永远不会成立」的 pending 步骤就地了结。

        判据只有一条，对三种 run_if 一视同仁：**上游全落定了，`deps_satisfied`
        还是假** —— 上游状态此后不会再变，所以这个假是永久的。

        以前的判据是「有上游死了 **且** 自己是 run_if=ok」，漏掉了一整格：
        `run_if=upstream_failed` 的兜底步骤，上游全部成功时它既不就绪
        （等的失败没发生）、也不被标记（没有死上游），于是永远 PENDING、
        `all_settled` 永假、`_finalize` 永不触发 —— **整个 run 静悄悄地永久挂起**，
        看板上只剩一个不动的 pending，worker 那边则是「没人再派活了」。

        两种了结分开记：上游真的死了是 SKIPPED（坏消息），
        上游好端端地成功了、只是用不上这一步是 NOT_NEEDED（不是坏消息）。
        """
        state = self
        while True:
            dead = state._dead_ids
            settled = state.done_ids | dead
            blocked = {
                r.id
                for r in state.steps
                if r.state is StepState.PENDING
                and state.plan.step(r.id).deps_settled(settled)
                and not state.plan.step(r.id).deps_satisfied(state.done_ids, settled)
            }
            if not blocked:
                return state
            for step_id in sorted(blocked):
                dead_deps = [d for d in state.step(step_id).depends_on if d in dead]
                if dead_deps:
                    outcome = StepState.SKIPPED
                    error = f"上游步骤 {', '.join(dead_deps)} 未完成，本步跳过"
                else:
                    outcome = StepState.NOT_NEEDED
                    error = "上游步骤全部成功，本步的触发条件不成立，无需执行"
                state = state._replace_step(
                    step_id,
                    state=outcome,
                    error=error,
                    finished_at=now().isoformat(),
                )

    # ---------- 不可变更新 ----------

    def _replace_step(self, step_id: str, **changes: object) -> RunState:
        target = self.step(step_id)  # 不存在就抛 KeyError，别静默吞掉
        steps = tuple(
            record.model_copy(update=changes) if record.id == target.id else record
            for record in self.steps
        )
        return self.model_copy(update={"steps": steps})

    def dispatch(self, step_id: str, *, thread: str, msg_id: str) -> RunState:
        return self._replace_step(
            step_id,
            state=StepState.RUNNING,
            thread=thread,
            msg_id=msg_id,
            attempts=self.step(step_id).attempts + 1,
            dispatched_at=now().isoformat(),
        )

    def complete(self, step_id: str, *, summary: str, artifacts: tuple[str, ...] = ()) -> RunState:
        return self._replace_step(
            step_id,
            state=StepState.DONE,
            summary=summary,
            artifacts=artifacts,
            finished_at=now().isoformat(),
        )

    def fail(self, step_id: str, *, error: str, terminal: bool = False) -> RunState:
        """`terminal=True` 表示这次失败是定论，返工轮次不该再重跑它。

        见 `StepRecord.terminal`。
        """
        return self._replace_step(
            step_id,
            state=StepState.FAILED,
            error=error,
            terminal=terminal,
            finished_at=now().isoformat(),
        )

    def reset_for_retry(self, step_id: str, *, error: str) -> RunState:
        """把一步退回待派，好再派一次。**不清 `attempts`** —— 那是重试的刹车。

        错误留着：重试成功了它就是一段有用的历史（「第一次超时了」），
        重试也失败的话，最后一次的错误会覆盖上去。
        """
        return self._replace_step(
            step_id,
            state=StepState.PENDING,
            error=error,
            thread="",
            msg_id="",
            dispatched_at="",
            finished_at="",
            nudged=False,  # 新的一次派发，催办计数重来
        )

    def can_retry(self, *, max_attempts: int) -> bool:
        """没做成的那些步骤，还有没有重跑的余地。

        有一个不能重跑就整体不重跑：失败的上游要是重跑不了，
        单独把被它连累跳过的下游放回去，只会让那条支路再被挡一次。
        """
        broken = [r for r in self.steps if r.id in self.broken_ids]
        return bool(broken) and all(not r.terminal and r.attempts < max_attempts for r in broken)

    def retry_broken(self) -> RunState:
        """把所有没做成的步骤退回待派，让下一轮重跑；轮次 +1 作为刹车。

        连**被连累跳过的下游**一起退（它们在 `broken_ids` 里），所以重跑的是
        「失败的那一步 + 它挡住的整条支路」，顺序仍由 DAG 自己保证 ——
        不需要另想一套「先跑谁」的逻辑。

        `attempts` 有意不清零：它是重试的刹车，跨轮次累计才拦得住反复失败的步骤。
        """
        state = self
        for step_id in sorted(state.broken_ids):
            state = state.reset_for_retry(step_id, error=state.step(step_id).error)
        return state.model_copy(update={"round": state.round + 1})

    def mark_nudged(self, step_id: str) -> RunState:
        return self._replace_step(step_id, nudged=True)

    def finish(self, *, summary: str) -> RunState:
        return self.model_copy(update={"finished": True, "result": summary})

    def with_extra_steps(self, plan: Plan, extra: tuple[StepRecord, ...]) -> RunState:
        return self.model_copy(
            update={"plan": plan, "steps": (*self.steps, *extra), "round": self.round + 1}
        )


class RunStore:
    """`blackboard/tasks/<task_id>/state.json` 的读写。原子写，崩溃不会留半个状态。"""

    def __init__(self, blackboard: Path) -> None:
        self._root = blackboard

    def path(self, task_id: str) -> Path:
        if not is_valid_id(task_id):
            raise ValueError(f"非法 task id {task_id!r}：只接受 ULID")
        return self._root / TASKS_DIR / task_id / STATE_FILE

    def save(self, state: RunState) -> Path:
        path = self.path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return atomic_write(
            path.parent, path.parent, path.name, state.model_dump_json(indent=2).encode()
        )

    def load(self, task_id: str) -> RunState | None:
        path = self.path(task_id)
        if not path.is_file():
            return None
        try:
            return RunState.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"编排状态 {path} 已损坏：{exc}") from exc

    def all(self) -> list[RunState]:
        root = self._root / TASKS_DIR
        if not root.is_dir():
            return []
        out: list[RunState] = []
        for state_file in sorted(root.glob(f"*/{STATE_FILE}")):
            try:
                out.append(
                    RunState.model_validate(json.loads(state_file.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, ValueError):
                continue  # 单个任务的状态损坏不该让整个看板打不开
        return out

    def active(self) -> list[RunState]:
        return [s for s in self.all() if not s.finished]
