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


def test_status_says_visible_but_not_yet_reachable(workspace: Path):
    """默认可见，但状态里必须写清「还要配对」—— 否则人会以为已经能互投消息了。"""
    result = runner.invoke(app, ["status", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "clinode" in result.output
    assert "enabled" in result.output
    assert "配对" in result.output


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


# ---------- bridge --ack：协议内的无声确认 ----------
#
# 纯回执消息（「收到」「无需回复」）以前只能靠手动 mv 文件清掉 ——
# 回一句会在对方队列里生成新待办，对方再回执，循环没有终点。


def bridge_workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"])
    assert result.exit_code == 0, result.output
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[agents.cc]\nrole = "worker"\nbridge = true\n',
        encoding="utf-8",
    )
    return tmp_path


def seed_pending(workspace: Path, msg_id: str = "01KZ000000000000000000AAAA") -> Path:
    bridge = NodeLayout(workspace).agent_dir("cc") / "bridge"
    (bridge / "inbox").mkdir(parents=True, exist_ok=True)
    (bridge / "pending").mkdir(parents=True, exist_ok=True)
    (bridge / "inbox" / f"{msg_id}.md").write_text(
        f"---\nfrom: box:cli\nid: {msg_id}\n---\n收到，无需回复\n", encoding="utf-8"
    )
    (bridge / "pending" / f"{msg_id}.json").write_text("{}", encoding="utf-8")
    return bridge


def test_bridge_ack_clears_a_message_without_replying(tmp_path: Path):
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)

    result = runner.invoke(app, ["bridge", "cc", "--ack", "00AAAA", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    assert not list((bridge / "inbox").glob("*.md")), "确认后 inbox 该清空"
    assert not list((bridge / "pending").glob("*.json")), "确认后 pending 该清空"
    assert not list((bridge / "outbox").glob("*.md")), "无声确认不该发任何东西"
    assert (bridge / "done" / "01KZ000000000000000000AAAA.md").is_file(), "确认的消息该归档"


def test_bridge_ack_refuses_when_a_reply_draft_exists(tmp_path: Path):
    """又写了回复草稿又要无声确认 —— 意图矛盾，拒绝并让人自己拿主意。"""
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    (bridge / "outbox").mkdir(parents=True, exist_ok=True)
    (bridge / "outbox" / "01KZ000000000000000000AAAA.md").write_text("想回的话", encoding="utf-8")

    result = runner.invoke(app, ["bridge", "cc", "--ack", "00AAAA", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "草稿" in result.output
    assert (bridge / "inbox" / "01KZ000000000000000000AAAA.md").is_file(), "拒绝时什么都不该动"
    assert (bridge / "outbox" / "01KZ000000000000000000AAAA.md").is_file()


def test_bridge_ack_with_an_unknown_id_fails(tmp_path: Path):
    workspace = bridge_workspace(tmp_path)
    seed_pending(workspace)

    result = runner.invoke(app, ["bridge", "cc", "--ack", "ZZZZZZ", "-w", str(workspace)])

    assert result.exit_code != 0


def test_cli_send_shows_up_in_the_chat_records(workspace: Path):
    """`anthill send` 发的消息也得进对话记录 —— 以前只有面板发的才记，
    于是对话页上只见对方的回音、不见你发出去的那半句，
    跨机器联调时看着就像「消息没显示」。"""
    result = runner.invoke(app, ["send", "echo", "CLI 发的这句要看得见", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    chats = list((workspace / ".anthill" / "chats").glob("*.jsonl"))
    assert chats, "CLI 发件没有进 chats 记录"
    assert any("CLI 发的这句要看得见" in p.read_text(encoding="utf-8") for p in chats)
