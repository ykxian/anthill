"""编排状态的不可变更新、落盘恢复，以及 BOARD.md 渲染。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.ids import new_id, new_thread_id
from anthill.orchestrator.board import (
    BOARD_FILE,
    MAX_BOARD_LINES,
    STATE_MARK,
    Blackboard,
)
from anthill.orchestrator.plan import Plan
from anthill.orchestrator.state import RunState, RunStore, StepState

PLAN = Plan.model_validate(
    {
        "goal": "为 date.py 补单测并通过审查",
        "steps": [
            {"id": "s1", "assignee": "coder", "task": "写测试", "depends_on": []},
            {"id": "s2", "assignee": "role:reviewer", "task": "审查", "depends_on": ["s1"]},
        ],
        "done_when": "reviewer approve",
    }
)
TASK_ID = new_id()
THREAD = new_thread_id()
SUB_THREAD = new_thread_id()


def make_state() -> RunState:
    return RunState.start(
        task_id=TASK_ID,
        plan=PLAN,
        requester="testnode:cli",
        root_thread=THREAD,
        root_msg_id=new_id(),
    )


# ---------- 不可变更新 ----------


def test_dispatching_a_step_does_not_mutate_the_previous_state() -> None:
    # Arrange
    before = make_state()

    # Act
    after = before.dispatch("s1", thread=SUB_THREAD, msg_id="01J000000000000000M1")

    # Assert
    assert before.step("s1").state is StepState.PENDING
    assert after.step("s1").state is StepState.RUNNING
    assert after.step("s1").thread == SUB_THREAD


def test_completing_a_step_records_summary_and_artifacts() -> None:
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1")

    done = state.complete("s1", summary="写了 12 个用例", artifacts=("tests/test_date.py",))

    assert done.step("s1").state is StepState.DONE
    assert done.step("s1").artifacts == ("tests/test_date.py",)
    assert done.done_ids == {"s1"}


def test_failing_a_step_records_the_error() -> None:
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1").fail("s1", error="模型炸了")

    assert state.step("s1").state is StepState.FAILED
    assert "炸了" in (state.step("s1").error or "")


def test_step_lookup_by_thread_is_how_replies_get_matched() -> None:
    # 子任务用独立 thread 派发，worker 的回信按 thread 认领步骤
    state = make_state().dispatch("s1", thread=SUB_THREAD, msg_id="m1")

    matched = state.step_for_thread(SUB_THREAD)
    assert matched is not None and matched.id == "s1"
    assert state.step_for_thread(new_thread_id()) is None


def test_busy_and_done_sets_drive_the_scheduler() -> None:
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1")

    assert state.busy_ids == {"s1"}
    assert state.done_ids == set()
    assert state.plan.ready(done=state.done_ids, taken=state.busy_ids) == ()


def test_run_is_complete_only_when_every_step_settled() -> None:
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1").complete("s1", summary="ok")

    assert not state.all_settled
    settled = state.dispatch("s2", thread=THREAD, msg_id="m2").complete("s2", summary="ok")
    assert settled.all_settled


def test_unknown_step_id_fails_loudly() -> None:
    with pytest.raises(KeyError, match="s9"):
        make_state().dispatch("s9", thread=THREAD, msg_id="m1")


# ---------- 落盘与恢复 ----------


def test_state_survives_a_save_load_roundtrip(tmp_path: Path) -> None:
    # Arrange
    store = RunStore(tmp_path)
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1").complete("s1", summary="好了")

    # Act
    store.save(state)
    loaded = store.load(TASK_ID)

    # Assert：coordinator 崩溃重启后能接着调度，靠的就是这个
    assert loaded is not None
    assert loaded.step("s1").state is StepState.DONE
    assert loaded.step("s1").summary == "好了"
    assert loaded.plan.done_when == PLAN.done_when
    assert loaded.requester == "testnode:cli"


def test_loading_an_unknown_task_returns_none(tmp_path: Path) -> None:
    assert RunStore(tmp_path).load(TASK_ID) is None


def test_corrupt_state_file_is_reported_not_silently_ignored(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.path(TASK_ID).parent.mkdir(parents=True, exist_ok=True)
    store.path(TASK_ID).write_text("{坏", encoding="utf-8")

    with pytest.raises(ValueError, match="损坏"):
        store.load(TASK_ID)


def test_active_runs_are_listed_for_the_reminder_scan(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.save(make_state())
    store.save(make_state().finish(summary="收工").model_copy(update={"task_id": new_id()}))

    active = [s.task_id for s in store.active()]

    assert active == [TASK_ID]


# ---------- BOARD.md ----------


def test_board_shows_goal_steps_and_status(tmp_path: Path) -> None:
    # Arrange
    board = Blackboard(tmp_path)
    state = make_state().dispatch("s1", thread=THREAD, msg_id="m1")

    # Act
    board.write([state])

    # Assert
    text = (tmp_path / BOARD_FILE).read_text(encoding="utf-8")
    assert PLAN.goal in text
    assert "s1" in text and "coder" in text
    assert "running" in text


def test_board_stays_within_the_line_budget(tmp_path: Path) -> None:
    # 黑板要注进每个 Agent 的上下文，长了就是纯烧 token
    board = Blackboard(tmp_path)
    states = [make_state().model_copy(update={"task_id": new_id()}) for _ in range(40)]

    board.write(states)

    lines = (tmp_path / BOARD_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) <= MAX_BOARD_LINES


def test_board_summary_is_empty_when_nothing_is_running(tmp_path: Path) -> None:
    assert Blackboard(tmp_path).summary() == ""


def test_board_summary_reads_back_what_was_written(tmp_path: Path) -> None:
    board = Blackboard(tmp_path)
    board.write([make_state()])

    assert PLAN.goal in board.summary()


def test_task_dir_is_created_under_tasks(tmp_path: Path) -> None:
    path = Blackboard(tmp_path).task_dir(TASK_ID)

    assert path.is_dir()
    assert path.parent.name == "tasks"


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_task_dir_rejects_ids_that_are_not_ulids(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="task"):
        Blackboard(tmp_path).task_dir(bad)


def test_every_step_state_has_a_mark(tmp_path: Path) -> None:
    """渲染是下标取值 —— 少一格就是 KeyError。加了新状态忘填，这里先红。"""
    assert set(STATE_MARK) == set(StepState)


def test_the_board_survives_one_branch_failing_while_another_still_runs(tmp_path: Path) -> None:
    """真出过的崩：并行 DAG 一支失败、另一支还在跑的那一刻，STATE_MARK 里没有
    SKIPPED，`render_board` 直接 KeyError —— BOARD.md 从此停更、tick.failed 刷屏。

    以前测不出来，是因为用例里 skipped 和收尾发生在同一次调度，落盘时 run 已经结束，
    而 BOARD 只对**未完成**的 run 展开步骤明细。所以这里手工摆出那个中间态。
    """
    board = Blackboard(tmp_path)
    state = make_state()
    steps = [
        state.steps[0].model_copy(update={"state": StepState.FAILED, "error": "炸了"}),
        state.steps[1].model_copy(update={"state": StepState.SKIPPED}),
    ]
    mid_flight = state.model_copy(update={"steps": steps})

    board.write([mid_flight])  # 不能抛

    text = (tmp_path / BOARD_FILE).read_text(encoding="utf-8")
    assert "skipped" in text and "failed" in text


# ---------- fork：从某一步重跑 ----------


def _three_step_state() -> RunState:
    """s1 → s2 → s3 一条链，s1 成了、s2 失败、s3 被跳过，run 已结束。"""
    plan = Plan.model_validate(
        {
            "goal": "g",
            "steps": [
                {"id": "s1", "assignee": "a", "task": "一", "depends_on": []},
                {"id": "s2", "assignee": "b", "task": "二", "depends_on": ["s1"]},
                {"id": "s3", "assignee": "c", "task": "三", "depends_on": ["s2"]},
            ],
            "done_when": "",
        }
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="n:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    state = state.dispatch("s1", thread=new_thread_id(), msg_id=new_id())
    state = state.complete("s1", summary="一号交付", artifacts=("a.py",))
    state = state.dispatch("s2", thread=new_thread_id(), msg_id=new_id())
    state = state.fail("s2", error="炸了")
    state = state.block_unreachable()
    return state.finish(summary="步骤 s2 失败")


def test_fork_resets_the_step_and_its_transitive_downstream() -> None:
    source = _three_step_state()

    forked = source.fork("s2", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())

    assert forked.step("s1").state is StepState.DONE
    assert forked.step("s1").summary == "一号交付"  # 保留的交付连内容一起带走
    assert forked.step("s1").artifacts == ("a.py",)
    assert forked.step("s2").state is StepState.PENDING
    assert forked.step("s2").attempts == 0 and forked.step("s2").error == ""
    assert forked.step("s3").state is StepState.PENDING
    assert not forked.finished
    assert forked.task_id != source.task_id
    assert forked.root_thread != source.root_thread
    assert forked.ready_steps() == ("s2",)


def test_fork_also_resets_non_done_steps_outside_the_closure() -> None:
    """闭包外但没成过的步骤（失败/跳过/在跑）一律重置 —— fork 出来的 run
    不该带着一身旧伤起跑。"""
    source = _three_step_state()

    forked = source.fork("s3", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())

    # s2 在 s3 的闭包之外，但它是 FAILED —— 也要重置
    assert forked.step("s2").state is StepState.PENDING
    assert forked.step("s1").state is StepState.DONE


def test_fork_treats_fanout_steps_as_plain_nodes() -> None:
    """for_each 展开出来的 s2__1/s2__2 是普通节点，闭包按 depends_on 走。"""
    plan = Plan.model_validate(
        {
            "goal": "g",
            "steps": [
                {"id": "s1", "assignee": "a", "task": "一", "depends_on": []},
                {
                    "id": "s2",
                    "assignee": "b",
                    "task": "处理 {item}",
                    "depends_on": ["s1"],
                    "for_each": ["x", "y"],
                },
            ],
            "done_when": "",
        }
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="n:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    state = state.dispatch("s1", thread=new_thread_id(), msg_id=new_id())
    state = state.complete("s1", summary="好了")
    for sid in ("s2__1", "s2__2"):
        state = state.dispatch(sid, thread=new_thread_id(), msg_id=new_id())
        state = state.complete(sid, summary="done")
    state = state.finish(summary="ok")

    forked = state.fork("s1", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())

    assert forked.step("s2__1").state is StepState.PENDING
    assert forked.step("s2__2").state is StepState.PENDING


def test_fork_lets_run_if_recompute_naturally() -> None:
    """run_if 的三态在重算就绪时自然生效：上游成了，upstream_failed 的
    兜底步不就绪 —— fork 不需要为它写一行特判。"""
    plan = Plan.model_validate(
        {
            "goal": "g",
            "steps": [
                {"id": "s1", "assignee": "a", "task": "一", "depends_on": []},
                {
                    "id": "clean",
                    "assignee": "b",
                    "task": "兜底",
                    "depends_on": ["s1"],
                    "run_if": "upstream_failed",
                },
                {"id": "s3", "assignee": "c", "task": "三", "depends_on": ["s1"]},
            ],
            "done_when": "",
        }
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="n:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    state = state.dispatch("s1", thread=new_thread_id(), msg_id=new_id())
    state = state.complete("s1", summary="好了")
    state = state.dispatch("s3", thread=new_thread_id(), msg_id=new_id())
    state = state.fail("s3", error="炸了")
    state = state.block_unreachable()
    state = state.finish(summary="s3 失败")

    forked = state.fork("s3", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())

    assert forked.ready_steps() == ("s3",)  # 兜底步不就绪：它等的是上游失败


def test_fork_does_not_inherit_approvals() -> None:
    """批准不跨 fork 继承：审批 id 从 task_id 派生，新 task 一定重新走审批。"""
    from anthill.orchestrator.coordinator import _approval_id

    source = _three_step_state()
    forked = source.fork("s2", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())

    step = source.plan.step("s2")
    assert _approval_id(source, step) != _approval_id(forked, step)


def test_fork_of_an_unknown_step_raises() -> None:
    source = _three_step_state()

    with pytest.raises(KeyError):
        source.fork("ghost", task_id=new_id(), root_thread=new_thread_id(), root_msg_id=new_id())
