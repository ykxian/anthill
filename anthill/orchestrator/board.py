"""共享黑板 BOARD.md（02-protocol §7）。

它是「团队当前在干什么」的一页纸快照，会被注进每个 Agent 的上下文，
所以**必须短**（≤100 行）—— 长了就是每个 Agent 每一轮都在烧同样的 token。

单写者原则：BOARD.md 只有 coordinator 写；任务目录只有该步的 assignee 写。
借鉴 collab-cli 的 SHARD.md，但这里的内容由状态机自动渲染，不靠 Agent 自觉维护。
"""

from __future__ import annotations

from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.ids import is_valid_id
from anthill.orchestrator.state import RunState, StepState

BOARD_FILE = "BOARD.md"
TASKS_DIR = "tasks"
MAX_BOARD_LINES = 100
MAX_TASK_CHARS = 60

STATE_MARK = {
    StepState.PENDING: "…",
    StepState.RUNNING: "▶",
    StepState.DONE: "✓",
    StepState.FAILED: "✗",
    StepState.SKIPPED: "⊘",
    StepState.NOT_NEEDED: "·",
}
"""**每个 StepState 都得在这儿有一格。**（有一条测试逐个枚举，加了新状态忘填会红。）

漏掉 SKIPPED 曾让并行 DAG 一失败就崩：一支失败、另一支还在跑的那一刻，
`_render_run` 下标取值直接 KeyError —— BOARD.md 从此停止更新、tick.failed 刷屏。
以前没被测出来，是因为用例里 skipped 和收尾发生在同一次调度，落盘时 run 已经结束，
而 BOARD 只对**未完成**的 run 展开步骤明细。只要 DAG 真有并行分支就必然撞上。
"""


class Blackboard:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def board_path(self) -> Path:
        return self._root / BOARD_FILE

    def task_dir(self, task_id: str) -> Path:
        """任务产物目录。信封里只传这里的路径引用，大文件不进消息体。"""
        if not is_valid_id(task_id):
            raise ValueError(f"非法 task id {task_id!r}：只接受 ULID")
        path = self._root / TASKS_DIR / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, states: list[RunState]) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return atomic_write(self._root, self._root, BOARD_FILE, render_board(states).encode())

    def summary(self) -> str:
        """注入 Agent 上下文用。读不到就返回空串 —— 黑板不是必需品。"""
        if not self.board_path.is_file():
            return ""
        try:
            return self.board_path.read_text(encoding="utf-8")
        except OSError:
            return ""


def render_board(states: list[RunState], *, max_lines: int = MAX_BOARD_LINES) -> str:
    active = [s for s in states if not s.finished]
    finished = [s for s in states if s.finished]

    lines = [
        "# BOARD —— 当前协作状态",
        "",
        f"进行中 {len(active)} 项，已完成 {len(finished)} 项",
        "",
    ]
    for state in active:
        lines.extend(_render_run(state))
    if finished:
        lines.append("## 最近完成")
        for state in finished[-3:]:
            lines.append(f"- ✓ {_clip(state.plan.goal)}")

    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], f"…（还有 {len(lines) - max_lines + 1} 行未显示）"]
    return "\n".join(lines) + "\n"


def _render_run(state: RunState) -> list[str]:
    head = f"## {_clip(state.plan.goal)}"
    if state.round:
        head += f"（返工第 {state.round} 轮）"
    lines = [head, f"- 发起方 {state.requester} · 任务 {state.task_id[-6:]}"]
    lines.extend(
        f"- {STATE_MARK[record.state]} `{record.id}` @{record.assignee} "
        f"{record.state} —— {_clip(record.summary or record.error or record.task)}"
        for record in state.steps
    )
    lines.append("")
    return lines


def _clip(text: str, limit: int = MAX_TASK_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
