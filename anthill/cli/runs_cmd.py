"""`anthill runs` —— 历史任务在哪儿，跑到哪一步了。

两个缺口合成一个坑：`anthill run` 按 Ctrl-C 之后没有任何重连机制
（协调器在磁盘上继续跑，但你看不到了），而且**没有任何命令能列出历史任务** ——
`RunStore.all()` 一直存在，CLI 一处都没用过。于是 Ctrl-C 之后那次协作就
从界面上消失了；面板也只保留最近 3 条已完成任务，第 4 条起永久看不见。

Ctrl-C 时提示的「用 anthill status 查看」还是条死路 —— `status` 的输出里
根本没有任务这一节。这个文件把那条路补上。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from anthill.cli.common import console, fail, load
from anthill.orchestrator.state import RunState, RunStore, StepState

MARK = {
    StepState.PENDING: "…",
    StepState.RUNNING: "▶",
    StepState.DONE: "✓",
    StepState.FAILED: "✗",
    StepState.SKIPPED: "⊘",
}
STYLE = {
    StepState.PENDING: "dim",
    StepState.RUNNING: "yellow",
    StepState.DONE: "green",
    StepState.FAILED: "bold red",
    StepState.SKIPPED: "dim red",
}


def runs_command(
    task_id: str = typer.Argument("", help="只看这一条（可只给末尾几位）；留空则列出全部"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    active: bool = typer.Option(False, "--active", help="只看还没跑完的"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON，便于接进脚本"),
) -> None:
    """列出编排任务，或看某一条的每一步。"""
    layout, _ = load(workspace)
    store = RunStore(layout.blackboard)
    states = store.active() if active else store.all()

    if task_id:
        matched = [s for s in states if s.task_id == task_id or s.task_id.endswith(task_id)]
        if not matched:
            fail(f"没有匹配 {task_id!r} 的任务；先跑 `anthill runs` 看看有哪些")
        if len(matched) > 1:
            fail(f"{task_id!r} 匹配到 {len(matched)} 条，多给几位")
        _detail(matched[0], as_json=as_json)
        return

    if as_json:
        console.print_json(data={"runs": [_summary(s) for s in states]})
        return
    if not states:
        console.print('[dim]还没有任何编排任务。用 `anthill run "..."` 起一个。[/dim]')
        return

    table = Table(title="编排任务", header_style="bold cyan")
    for column in ("任务", "目标", "进度", "状态", "发起方"):
        table.add_column(column, overflow="fold")
    for state in states:
        table.add_row(
            state.task_id[-6:],
            _clip(state.plan.goal),
            f"{sum(1 for s in state.steps if s.state is StepState.DONE)}/{len(state.steps)}",
            "已结束" if state.finished else "进行中",
            state.requester,
        )
    console.print(table)
    console.print("[dim]看某一条：anthill runs <任务号>[/dim]")


def _detail(state: RunState, *, as_json: bool) -> None:
    """一条任务的全部：每一步的产物、耗时、试了几次 —— 面板上这些数据算好了却没显示。"""
    if as_json:
        console.print_json(data=_summary(state) | {"steps": [_step(s) for s in state.steps]})
        return

    console.print(f"[bold]{state.plan.goal}[/bold]")
    console.print(
        f"[dim]任务 {state.task_id[-6:]} · 发起方 {state.requester} · "
        f"{'已结束' if state.finished else '进行中'}"
        f"{f' · 返工 {state.round} 轮' if state.round else ''}[/dim]\n"
    )
    table = Table(box=None, header_style="dim", pad_edge=False)
    for column in ("", "步骤", "执行者", "用时", "试了", "结果"):
        table.add_column(column, overflow="fold")
    for record in state.steps:
        table.add_row(
            f"[{STYLE[record.state]}]{MARK[record.state]}[/{STYLE[record.state]}]",
            record.id,
            record.assignee,
            _elapsed(record.dispatched_at, record.finished_at),
            str(record.attempts) if record.attempts > 1 else "",
            _clip(record.summary or record.error or record.task, 70),
        )
    console.print(table)
    artifacts = [a for record in state.steps for a in record.artifacts]
    if artifacts:
        console.print("\n[dim]产物：[/dim]" + ", ".join(artifacts))


def _summary(state: RunState) -> dict[str, object]:
    return {
        "task_id": state.task_id,
        "goal": state.plan.goal,
        "requester": state.requester,
        "finished": state.finished,
        "round": state.round,
        "done": sum(1 for s in state.steps if s.state is StepState.DONE),
        "total": len(state.steps),
    }


def _step(record: object) -> dict[str, object]:
    return {
        "id": getattr(record, "id", ""),
        "assignee": getattr(record, "assignee", ""),
        "state": str(getattr(record, "state", "")),
        "attempts": getattr(record, "attempts", 0),
        "summary": getattr(record, "summary", ""),
        "error": getattr(record, "error", ""),
        "artifacts": list(getattr(record, "artifacts", ())),
        "dispatched_at": getattr(record, "dispatched_at", ""),
        "finished_at": getattr(record, "finished_at", ""),
    }


def _elapsed(start: str, end: str) -> str:
    """「这一步跑了多久」—— 两个时间戳都记了，以前一处没显示过。"""
    if not start or not end:
        return ""
    from datetime import datetime

    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return ""
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds / 60:.1f}m"


def _clip(text: str, limit: int = 46) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
