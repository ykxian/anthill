"""`anthill run` 的观察者逻辑与 CLI 行为。

编排逻辑不在这里 —— RunWatcher 是只读的，它只做三件事：
认领属于自己 thread 的回信、渲染步骤表、判断该不该收工。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.cli.run_cmd import RunWatcher, _steps_table
from anthill.core.config import Config, find_coordinator
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import ConfigError
from anthill.core.ids import new_id, new_thread_id
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import (
    MessageType,
    ReceiptPayload,
    TaskErrorPayload,
    TaskResultPayload,
)
from anthill.orchestrator.plan import Plan
from anthill.orchestrator.state import RunState, RunStore

runner = CliRunner()

PLAN = Plan.model_validate(
    {
        "goal": "补单测",
        "steps": [{"id": "s1", "assignee": "coder", "task": "写测试", "depends_on": []}],
        "done_when": "",
    }
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "clinode"])
    assert result.exit_code == 0, result.output
    return tmp_path


def make_watcher(layout: NodeLayout, thread: str, *, timeout: float = 5.0) -> RunWatcher:
    return RunWatcher(
        layout=layout,
        mailbox=Mailbox(layout.mailbox_dir("cli")).ensure(),
        thread=thread,
        task="补单测",
        timeout=timeout,
    )


def deliver(layout: NodeLayout, env: Envelope) -> None:
    Mailbox(layout.mailbox_dir("cli")).ensure().deposit(env)


def reply(thread: str, kind: MessageType, payload: object) -> Envelope:
    return Envelope.new(
        sender=Address(node="clinode", agent="boss"),
        recipient=Address(node="clinode", agent="cli"),
        type=kind,
        payload=payload,  # type: ignore[arg-type]
        thread=thread,
    )


# ---------- 找 coordinator ----------


def test_coordinator_is_found_by_role() -> None:
    config = Config.model_validate(
        {
            "node": {"name": "n"},
            "agents": {
                "cli": {"role": "user"},
                "boss": {"role": "coordinator", "command": ["claude", "-p"]},
            },
        }
    )

    assert find_coordinator(config) == "boss"


def test_a_brainless_coordinator_is_refused_instead_of_pretending_to_succeed() -> None:
    """本项目最恶劣的一次首跑体验。

    `init` 生成的默认模板里 `[agents.coordinator]` 只写了 role，没有 provider ——
    按项目自己的规则，没有 provider 就是 echo agent。而 `_find_coordinator`
    以前只按 role 找名字，不看它有没有大脑。于是 `anthill run` 把任务派给一个
    只会回显的 Agent，拿回一句复读，然后打印「完成（ok）」、**退出码 0**。

    这比卡住 600 秒糟糕得多：卡住至少能让人意识到不对劲，
    而新用户看到的是一次「成功」的运行 —— 拆解、派活、汇总一样没发生。
    """
    config = Config.model_validate(
        {"node": {"name": "n"}, "agents": {"coordinator": {"role": "coordinator"}}}
    )

    with pytest.raises(ConfigError, match="没有大脑"):
        find_coordinator(config)


def test_missing_coordinator_gives_an_actionable_error() -> None:
    config = Config.model_validate({"node": {"name": "n"}, "agents": {"cli": {"role": "user"}}})

    with pytest.raises(ConfigError, match="coordinator"):
        find_coordinator(config)


def test_run_without_a_coordinator_exits_with_a_hint(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path), "--node-name", "solo"])
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('[agents.coordinator]\nrole = "coordinator"', ""),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["run", "干点活", "--workspace", str(tmp_path)])

    assert result.exit_code == 1
    assert "coordinator" in result.output


# ---------- 观察者 ----------


async def test_watcher_stops_on_the_final_result(workspace: Path) -> None:
    # Arrange
    layout = NodeLayout(workspace)
    thread = new_thread_id()
    watcher = make_watcher(layout, thread)
    deliver(layout, reply(thread, MessageType.TASK_RESULT, TaskResultPayload(summary="搞定")))

    # Act
    keep_going = await watcher._step()

    # Assert
    assert not keep_going
    assert watcher._final is not None
    assert watcher._final.type is MessageType.TASK_RESULT


async def test_watcher_ignores_messages_from_other_threads(workspace: Path) -> None:
    # 同一个 cli 邮箱可能同时挂着别的任务，串台会让画面完全错乱
    layout = NodeLayout(workspace)
    watcher = make_watcher(layout, new_thread_id())
    deliver(
        layout, reply(new_thread_id(), MessageType.TASK_RESULT, TaskResultPayload(summary="别人的"))
    )

    assert await watcher._step()
    assert watcher._final is None


async def test_watcher_keeps_going_after_a_receipt(workspace: Path) -> None:
    layout = NodeLayout(workspace)
    thread = new_thread_id()
    watcher = make_watcher(layout, thread)
    deliver(layout, reply(thread, MessageType.RECEIPT_ACCEPTED, ReceiptPayload(ref=new_id())))

    assert await watcher._step()
    assert watcher._stream  # 回执进了消息流，但不算收工


async def test_watcher_gives_up_after_the_timeout(workspace: Path) -> None:
    watcher = make_watcher(NodeLayout(workspace), new_thread_id(), timeout=-1.0)

    assert not await watcher._step()
    assert any("超时" in line for line in watcher._stream)


def test_watcher_picks_the_run_matching_its_own_thread(workspace: Path) -> None:
    # Arrange：黑板上同时有两次运行，只有一个属于本次
    layout = NodeLayout(workspace)
    thread = new_thread_id()
    store = RunStore(layout.blackboard)
    for root in (new_thread_id(), thread):
        store.save(
            RunState.start(
                task_id=new_id(),
                plan=PLAN,
                requester="clinode:cli",
                root_thread=root,
                root_msg_id=new_id(),
            )
        )

    # Act
    state = make_watcher(layout, thread)._state()

    # Assert
    assert state is not None
    assert state.root_thread == thread


def test_steps_table_renders_every_step(workspace: Path) -> None:
    state = RunState.start(
        task_id=new_id(),
        plan=PLAN,
        requester="clinode:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )

    assert _steps_table(state).row_count == 1


async def test_error_result_is_surfaced_as_a_nonzero_exit(workspace: Path) -> None:
    import typer

    layout = NodeLayout(workspace)
    thread = new_thread_id()
    watcher = make_watcher(layout, thread)
    deliver(layout, reply(thread, MessageType.TASK_ERROR, TaskErrorPayload(error="步骤 s1 失败")))
    await watcher._step()

    with pytest.raises(typer.Exit) as exc:
        watcher._print_outcome()
    assert exc.value.exit_code == 1
