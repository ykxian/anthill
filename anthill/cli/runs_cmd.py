"""`anthill runs` —— 历史任务在哪儿，跑到哪一步了。

两个缺口合成一个坑：`anthill run` 按 Ctrl-C 之后没有任何重连机制
（协调器在磁盘上继续跑，但你看不到了），而且**没有任何命令能列出历史任务** ——
`RunStore.all()` 一直存在，CLI 一处都没用过。于是 Ctrl-C 之后那次协作就
从界面上消失了；面板也只保留最近 3 条已完成任务，第 4 条起永久看不见。

Ctrl-C 时提示的「用 anthill status 查看」还是条死路 —— `status` 的输出里
根本没有任务这一节。这个文件把那条路补上。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.table import Table
from rich.text import Text

from anthill.cli.common import console, fail, load
from anthill.core.paths import NodeLayout
from anthill.orchestrator.state import RunState, RunStore, StepState

MARK = {
    StepState.PENDING: "…",
    StepState.RUNNING: "▶",
    StepState.DONE: "✓",
    StepState.FAILED: "✗",
    StepState.SKIPPED: "⊘",
}
POLL_INTERVAL = 0.5

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
    follow: bool = typer.Option(False, "--follow", "-f", help="盯着这条任务，跑完为止"),
    trace: bool = typer.Option(False, "--trace", help="回放这条任务的执行流水（事件级）"),
    fork_from: str = typer.Option(
        "", "--fork-from", help="从这一步开始重跑：复制成一条新任务（仅限已结束的任务）"
    ),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON，便于接进脚本"),
) -> None:
    """列出编排任务，或看某一条的每一步。

    `--follow` 是 `anthill run` 按 Ctrl-C 之后的回程：协调器在磁盘上一直在跑，
    这条命令重新盯上去。**它只读黑板，不参与任何编排** —— 关掉它不影响协作。

    `--trace` 回放 trace.jsonl —— 快照答不了的「怎么走到这一步的」：
    哪一步先派、催没催过、重试发生在哪个环节。流水全文只出现在这里，
    面板只知道「有 N 条事件」。
    """
    layout, _ = load(workspace)
    store = RunStore(layout.blackboard)
    states = store.active() if active else store.all()

    if trace and not task_id:
        fail("--trace 要指定看哪一条：anthill runs <任务号> --trace")

    if task_id:
        matched = [s for s in states if s.task_id == task_id or s.task_id.endswith(task_id)]
        if not matched:
            fail(f"没有匹配 {task_id!r} 的任务；先跑 `anthill runs` 看看有哪些")
        if len(matched) > 1:
            fail(f"{task_id!r} 匹配到 {len(matched)} 条，多给几位")
        if fork_from:
            _fork(layout, matched[0], fork_from)
            return
        if trace:
            _replay(layout, matched[0], as_json=as_json)
            return
        if follow:
            _follow(matched[0].task_id, store)
            return
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


def _fork(layout: NodeLayout, state: RunState, from_step: str) -> None:
    """从某一步重跑。**只写盘不碰调度**：BOARD.md 是 coordinator 的单写者
    地盘，这里不写；在跑的 coordinator 下个周期从 store.active() 自然接手。

    v1 只对已结束的任务开放：源 run 还在跑就 fork，同一批 worker 会同时
    收到两份一样的活 —— 失败重试这个主场景根本用不上活 run。
    """
    from anthill.core.ids import new_id, new_thread_id
    from anthill.orchestrator.trace import RunTrace, read_trace

    if not state.finished:
        fail(
            "这条任务还在跑 —— fork 只对已结束的任务开放（不然同一批 Agent 会收到双份活）。"
            "等它结束，或先看 --follow"
        )
    try:
        state.step(from_step)
    except KeyError:
        fail(f"没有步骤 {from_step!r}；这条任务有：{', '.join(r.id for r in state.steps)}")

    events = read_trace(layout.blackboard / "tasks" / state.task_id)
    last_seq = int(events[-1].get("seq", 0)) if events else 0  # 纯出处记录：源流水落笔处
    forked = state.fork(
        from_step, task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id()
    )
    RunStore(layout.blackboard).save(forked)
    RunTrace(layout.blackboard / "tasks" / forked.task_id).emit(
        # 出处字段不能叫 seq —— 那是流水的协议保留键，会被本事件自己的序号顶掉
        "forked_from",
        task=state.task_id,
        source_seq=last_seq,
        step=from_step,
    )

    kept = sum(1 for r in forked.steps if r.state is StepState.DONE)
    console.print(
        f"已从步骤 [bold]{from_step}[/bold] fork 出新任务 "
        f"[bold]{forked.task_id[-6:]}[/bold]（保留 {kept} 步已完成的交付）"
    )
    console.print(
        "[dim]coordinator 在跑的话下个周期自动接手；"
        f"盯进度：anthill runs {forked.task_id[-6:]} -f[/dim]"
    )


def _replay(layout: NodeLayout, state: RunState, *, as_json: bool) -> None:
    """按 seq 回放一次 run 的执行流水。只读，不碰调度。"""
    from anthill.orchestrator.trace import read_trace

    events = read_trace(layout.blackboard / "tasks" / state.task_id)
    if as_json:
        console.print_json(data={"task_id": state.task_id, "events": events})
        return
    if not events:
        console.print(
            "[dim]这条任务没有执行流水 —— 旧版 coordinator 跑的，或已被定期卫生清掉。[/dim]"
        )
        return
    console.print(f"[bold]{_clip(state.plan.goal, 60)}[/bold] · {len(events)} 条事件\n")
    for event in events:
        console.print(_event_line(event))
    console.print("\n[dim]每一步的终态与产物：anthill runs " + state.task_id[-6:] + "[/dim]")


_KIND_STYLE = {
    "run.started": "bold cyan",
    "plan.created": "cyan",
    "plan.rework": "yellow",
    "run.finished": "bold green",
    "step.done": "green",
    "step.failed": "bold red",
    "step.dispatch_failed": "bold red",
    "step.timeout": "red",
    "step.retrying": "yellow",
    "step.nudged": "yellow",
    "step.rejected": "red",
}


def _event_line(event: dict[str, object]) -> Text:
    """一行一事件：`seq 时:分:秒 kind step 其余字段`。"""
    seq = event.get("seq", "?")
    clock = str(event.get("ts", ""))[11:19]  # 日期在 state 里有，这里只要时分秒
    kind = str(event.get("kind", "?"))
    step = str(event.get("step", ""))
    rest = {k: v for k, v in event.items() if k not in ("seq", "ts", "kind", "step")}
    line = Text()
    line.append(f"{seq:>4} ", style="dim")
    line.append(f"{clock} ", style="dim")
    line.append(f"{kind:<22}", style=_KIND_STYLE.get(kind, ""))
    if step:
        line.append(f" {step}", style="bold")
    for key, value in rest.items():
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        line.append(f" {key}=", style="dim")
        line.append(str(rendered))
    return line


def _follow(task_id: str, store: RunStore) -> None:
    """轮询黑板重画，直到这次运行结束。

    和 `anthill run` 的实时画面同一个原理：**只读观察者**。
    以前 Ctrl-C 之后就没有任何办法再看到进展了 —— 协调器还在跑，你却瞎了。
    """
    from rich.live import Live

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            state = next((s for s in store.all() if s.task_id == task_id), None)
            if state is None:
                live.update(Text(f"任务 {task_id[-6:]} 不见了"))
                return
            live.update(_steps_table(state))
            if state.finished:
                break
            time.sleep(POLL_INTERVAL)
    console.print(f"\n[bold green]已结束[/bold green] {state.result or ''}".rstrip())


def _steps_table(state: RunState) -> Table:
    table = Table(
        title=f"{_clip(state.plan.goal, 60)}",
        box=None,
        header_style="dim",
        pad_edge=False,
    )
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
    return table


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
    console.print(_steps_table(state))
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
