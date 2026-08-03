"""`anthill approve` 与 `anthill fetch` 的 CLI 行为。

远端那半程（SFTP 读写）在 test_ssh.py 里用真的 SSH 服务端测过了，
这里只测 CLI 自己的判断：参数校验、本机审批、错误提示是否可执行。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.cli.remote_cmd import render_pending
from anthill.core.paths import NodeLayout
from anthill.security.approvals import ApprovalRequest, ApprovalStore

runner = CliRunner()

PROMPT = "允许执行 run_shell（风险 high）？\n  rm -rf build"

SSH_PEER = """
[peers.lab]
transport = "ssh"
host = "10.0.8.21"
user = "yekaixian"
remote_workspace = "~/work/proj"

[peers.laptop2]
transport = "lan"
endpoint = "http://10.0.8.9:45778"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path), "--node-name", "laptop"]).exit_code == 0
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(toml.read_text(encoding="utf-8") + SSH_PEER, encoding="utf-8")
    return tmp_path


def store_of(workspace: Path) -> ApprovalStore:
    return ApprovalStore(NodeLayout(workspace).root)


def submit(workspace: Path, agent: str = "runner") -> ApprovalRequest:
    request = ApprovalRequest.create(agent=agent, prompt=PROMPT)
    store_of(workspace).submit(request)
    return request


# ---------- approve ----------


def test_nothing_to_approve_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["approve", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "没有待审批" in result.output


def test_approving_everything_writes_answers(workspace: Path) -> None:
    first, second = submit(workspace), submit(workspace, "coder")

    result = runner.invoke(app, ["approve", "--yes", "-w", str(workspace)])

    assert result.exit_code == 0
    store = store_of(workspace)
    for request in (first, second):
        answer = store.answer_of(request.id)
        assert answer is not None and answer.approved


def test_refusing_everything_writes_negative_answers(workspace: Path) -> None:
    request = submit(workspace)

    result = runner.invoke(app, ["approve", "--no", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "已拒绝" in result.output
    answer = store_of(workspace).answer_of(request.id)
    assert answer is not None and not answer.approved


def test_a_single_request_can_be_targeted(workspace: Path) -> None:
    picked, other = submit(workspace), submit(workspace)

    runner.invoke(app, ["approve", "--id", picked.id, "--yes", "-w", str(workspace)])

    store = store_of(workspace)
    assert store.answer_of(picked.id) is not None
    assert store.answer_of(other.id) is None


def test_yes_and_no_together_is_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["approve", "--yes", "--no", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "不能同时" in result.output


def test_interactive_approval_asks_and_shows_the_command(workspace: Path) -> None:
    submit(workspace)

    result = runner.invoke(app, ["approve", "-w", str(workspace)], input="y\n")

    assert result.exit_code == 0
    assert "rm -rf build" in result.output  # 人必须看得到到底要执行什么
    assert "已批准" in result.output


def test_approving_an_unknown_peer_is_an_actionable_error(workspace: Path) -> None:
    result = runner.invoke(app, ["approve", "--peer", "ghost", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "lab" in result.output  # 把已配置的 peer 列出来


def test_approving_a_non_ssh_peer_is_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["approve", "--peer", "laptop2", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "ssh" in result.output


# ---------- fetch ----------


def test_fetching_from_an_unknown_peer_is_an_actionable_error(workspace: Path) -> None:
    result = runner.invoke(app, ["fetch", "ghost", "reports/pytest.log", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "lab" in result.output


def test_fetching_from_a_non_ssh_peer_is_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["fetch", "laptop2", "a.txt", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "ssh" in result.output


def test_fetch_reports_connection_failure_instead_of_a_traceback(workspace: Path) -> None:
    # lab 指向一个不存在的主机，连不上是常态，不能吐 traceback
    result = runner.invoke(app, ["fetch", "lab", "a.txt", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "✗" in result.output


# ---------- 渲染 ----------


def test_pending_table_lists_every_request() -> None:
    requests = [ApprovalRequest.create(agent="runner", prompt=PROMPT) for _ in range(3)]

    assert render_pending(requests).row_count == 3


# ---------- pull ----------


def test_pulling_from_an_unknown_peer_is_an_actionable_error(workspace: Path) -> None:
    result = runner.invoke(app, ["pull", "ghost", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "lab" in result.output


def test_pulling_from_a_non_ssh_peer_is_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["pull", "laptop2", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "ssh" in result.output


def test_pull_reports_connection_failure_instead_of_a_traceback(workspace: Path) -> None:
    result = runner.invoke(app, ["pull", "lab", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "✗" in result.output


def test_pull_does_not_report_a_connection_failure_as_nothing_to_pull(workspace: Path) -> None:
    """「连不上」与「没有待取的」是两回事。

    混成一个「一切正常」，用户会以为回信收完了，其实还堆在服务器上。
    """
    result = runner.invoke(app, ["pull", "lab", "-w", str(workspace)])

    assert "没有待取" not in result.output


# ---------- 远端输入的边界校验（复查时补上）----------


def test_envelope_names_from_a_remote_must_be_ulids() -> None:
    """暂存文件名会被拼进「读」和「删」两个远端路径。

    正常 SFTP 服务端不会返回带 / 的条目名，但对面被攻陷时会 ——
    校验成本三行，不校验的代价是别人能指使我们删任意文件。
    """
    from anthill.cli.remote_cmd import _is_envelope_name
    from anthill.core.ids import new_id

    assert _is_envelope_name(f"{new_id()}.json")
    for bad in ("../../../etc/passwd", "../x.json", "x.json", "evil", f"{new_id()}.txt"):
        assert not _is_envelope_name(bad), bad
