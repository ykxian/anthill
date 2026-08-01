"""编排状态的不可变更新、落盘恢复，以及 BOARD.md 渲染。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.ids import new_id, new_thread_id
from anthill.orchestrator.board import BOARD_FILE, MAX_BOARD_LINES, Blackboard
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
