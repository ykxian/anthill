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

    @property
    def is_settled(self) -> bool:
        return self.state in (StepState.DONE, StepState.FAILED, StepState.SKIPPED)


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
    def all_settled(self) -> bool:
        return all(r.is_settled for r in self.steps)

    def ready_steps(self) -> tuple[str, ...]:
        """排除所有非 pending 的步骤，否则失败的那步会被当成「还没派」反复重派。"""
        taken = {r.id for r in self.steps if r.state is not StepState.PENDING}
        return tuple(s.id for s in self.plan.ready(done=self.done_ids, taken=taken))

    def block_unreachable(self) -> RunState:
        """上游失败/跳过的步骤永远等不到依赖，标成 skipped，让这次运行能收敛。

        不这么做的话，run 永远 all_settled=False，用户就一直等不到结果。
        """
        state = self
        while True:
            blocked = {
                r.id
                for r in state.steps
                if r.state is StepState.PENDING and any(_dead(state, dep) for dep in r.depends_on)
            }
            if not blocked:
                return state
            for step_id in sorted(blocked):
                failed = ", ".join(d for d in state.step(step_id).depends_on if _dead(state, d))
                state = state._replace_step(
                    step_id,
                    state=StepState.SKIPPED,
                    error=f"上游步骤 {failed} 未完成，本步跳过",
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

    def fail(self, step_id: str, *, error: str) -> RunState:
        return self._replace_step(
            step_id, state=StepState.FAILED, error=error, finished_at=now().isoformat()
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
            nudged=False,  # 新的一次派发，催办计数重来
        )

    def mark_nudged(self, step_id: str) -> RunState:
        return self._replace_step(step_id, nudged=True)

    def finish(self, *, summary: str) -> RunState:
        return self.model_copy(update={"finished": True, "result": summary})

    def with_extra_steps(self, plan: Plan, extra: tuple[StepRecord, ...]) -> RunState:
        return self.model_copy(
            update={"plan": plan, "steps": (*self.steps, *extra), "round": self.round + 1}
        )


def _dead(state: RunState, step_id: str) -> bool:
    return state.step(step_id).state in (StepState.FAILED, StepState.SKIPPED)


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
