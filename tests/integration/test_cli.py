"""CLI 冒烟测试：命令能跑通、参数错误有明确提示、退出码正确。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.config import Config
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "clinode"])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_init_creates_workspace_and_mailboxes(workspace: Path):
    layout = NodeLayout(workspace)
    config = Config.load_from(layout)

    assert config.node.name == "clinode"
    for name in config.agents:
        assert Mailbox(layout.mailbox_dir(name)).exists
    assert (layout.blackboard / "BOARD.md").is_file()


def test_init_refuses_to_clobber_without_force(workspace: Path):
    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code == 1
    assert "--force" in result.output


def test_init_force_rewrites(workspace: Path):
    result = runner.invoke(app, ["init", str(workspace), "--force", "-n", "renamed"])

    assert result.exit_code == 0
    assert Config.load_from(NodeLayout(workspace)).node.name == "renamed"


def test_status_reports_discovery_is_off(workspace: Path):
    result = runner.invoke(app, ["status", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "clinode" in result.output
    assert "disabled" in result.output  # 默认静默是核心需求，冒烟测试盯住它


def test_status_shows_peers_established_by_trust_not_only_configured_ones(workspace: Path):
    """`peers trust` 建立的对端不写进 node.toml —— 只看配置就会报「未配置」。

    这条命令是「为什么收不到消息」的第一站，报错了比不报还糟。
    """
    # Arrange
    from anthill.discovery.registry import PeerRegistry
    from anthill.security.keys import PairingToken, new_key

    PeerRegistry(NodeLayout(workspace).root).trust(
        PairingToken(node="lab", endpoint="http://10.0.8.21:45778", key=new_key())
    )

    # Act
    result = runner.invoke(app, ["status", "-w", str(workspace)])

    # Assert
    assert result.exit_code == 0
    assert "lab" in result.output
    assert "已信任" in result.output
    assert "未配置" not in result.output


def test_agent_list_shows_configured_agents(workspace: Path):
    result = runner.invoke(app, ["agent", "list", "-w", str(workspace)])

    assert result.exit_code == 0
    for name in ("cli", "coordinator", "echo"):
        assert name in result.output


def test_send_delivers_into_the_target_mailbox(workspace: Path):
    result = runner.invoke(app, ["send", "echo", "你好", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    inbox = Mailbox(NodeLayout(workspace).mailbox_dir("echo")).list_new()
    assert len(inbox) == 1


def test_send_to_unknown_agent_fails_with_hint(workspace: Path):
    result = runner.invoke(app, ["send", "ghost", "在吗", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "ghost" in result.output


def test_send_rejects_receipt_types(workspace: Path):
    result = runner.invoke(
        app, ["send", "echo", "x", "--type", "receipt.accepted", "-w", str(workspace)]
    )

    assert result.exit_code == 1
    assert "task.request" in result.output


def test_send_rejects_unknown_type(workspace: Path):
    result = runner.invoke(
        app, ["send", "echo", "x", "--type", "task.teleport", "-w", str(workspace)]
    )

    assert result.exit_code == 1


def test_log_lists_available_logs(workspace: Path):
    runner.invoke(app, ["send", "echo", "留个日志", "-w", str(workspace)])

    result = runner.invoke(app, ["log", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "cli" in result.output


def test_log_for_unknown_agent_fails(workspace: Path):
    result = runner.invoke(app, ["log", "nobody", "-w", str(workspace)])

    assert result.exit_code == 1


def test_commands_outside_a_workspace_say_how_to_fix(tmp_path: Path):
    result = runner.invoke(app, ["status", "-w", str(tmp_path / "nowhere")])

    assert result.exit_code == 1
    assert "anthill init" in result.output


def test_version(workspace: Path):
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "anthill" in result.output


def test_agent_list_shows_what_each_brain_actually_is(workspace: Path):
    """桥接 Agent 显示成 echo 会让人以为它不干活 —— 背后其实是一个人。"""
    toml = NodeLayout(workspace).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + '\n[agents.cc]\nrole = "worker"\nbridge = true\n'
        + '\n[agents.term]\nrole = "worker"\ncommand = ["claude", "-p"]\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["agent", "list", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "bridge" in result.output
    assert "claude" in result.output
