"""`anthill dead` —— 死信的查看、重投与清理。

以前死信没有任何出路：`dead_letters()` 只被用来在 status 里数个数，
既看不到内容，也没有重投或清理命令，唯一的恢复手段是手动 mv 文件。
而进死信最常见的原因（对端 agentd 晚起了十秒）恰恰是修好之后就该重投的那种。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.envelope import Address, Envelope
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskRequestPayload

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"]).exit_code == 0
    return tmp_path


def bury(workspace: Path, *, title: str = "没送出去的活", error: str = "对端 agentd 没启动") -> str:
    """造一条死信。"""
    outbox = Outbox(Mailbox(NodeLayout(workspace).mailbox_dir("cli")).ensure())
    env = Envelope.new(
        sender=Address(node="box", agent="cli"),
        recipient=Address(node="lab", agent="runner"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title=title),
    )
    outbox.abandon(outbox.enqueue(env), error)
    return env.id


def test_no_dead_letters_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["dead", "list", "cli", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "没有死信" in result.stdout


def test_listing_shows_who_it_was_for_and_why(workspace: Path) -> None:
    """光有个计数没用 —— 得看得见发给谁、为什么死的。"""
    bury(workspace)

    result = runner.invoke(app, ["dead", "list", "cli", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "lab:runner" in result.stdout
    assert "没启动" in result.stdout


def test_retrying_puts_it_back_in_the_outbox(workspace: Path) -> None:
    msg_id = bury(workspace)

    result = runner.invoke(app, ["dead", "retry", "cli", msg_id[-6:], "-w", str(workspace)])

    assert result.exit_code == 0, result.stdout
    outbox = Outbox(Mailbox(NodeLayout(workspace).mailbox_dir("cli")))
    assert [e.msg_id for e in outbox.load_pending()] == [msg_id]
    assert outbox.dead_letters() == []


def test_retrying_everything_at_once(workspace: Path) -> None:
    for index in range(3):
        bury(workspace, title=f"活 {index}")

    result = runner.invoke(app, ["dead", "retry", "cli", "--all", "-w", str(workspace)])

    assert result.exit_code == 0, result.stdout
    assert Outbox(Mailbox(NodeLayout(workspace).mailbox_dir("cli"))).dead_letters() == []


def test_dropping_removes_it_for_good(workspace: Path) -> None:
    msg_id = bury(workspace)

    result = runner.invoke(app, ["dead", "drop", "cli", msg_id[-6:], "-w", str(workspace)])

    assert result.exit_code == 0
    outbox = Outbox(Mailbox(NodeLayout(workspace).mailbox_dir("cli")))
    assert outbox.dead_letters() == []
    assert outbox.load_pending() == []


def test_an_ambiguous_id_is_refused_instead_of_guessed(workspace: Path) -> None:
    """匹配到多条就让人说清楚 —— 重投错一条消息是有副作用的。"""
    bury(workspace)
    bury(workspace)

    result = runner.invoke(app, ["dead", "retry", "cli", "", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "--all" in result.output


def test_an_unknown_agent_is_an_actionable_error(workspace: Path) -> None:
    result = runner.invoke(app, ["dead", "list", "ghost", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "cli" in result.output  # 把有哪些 Agent 摆出来
