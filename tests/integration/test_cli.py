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


# ---------- bridge --text-file：一条不经 shell 的正文入口 ----------
#
# `--text` 的正文要穿过 shell：反引号是命令替换、`${…}` 是变量替换，
# 内容被吃掉一截**还不报错，消息照发**。已经咬过两次（一次把审查回复里的
# JS 代码片段截断，只好补发更正）。文件这条路上，正文一个字节都不过 shell。

TRICKY = "改这行：`const x = ${y}` —— 还有 $(whoami) 和 \"引号\" 和 'single'"
"""专挑会被 shell 吃掉的东西：反引号、${}、$()、两种引号。"""


def test_bridge_reply_from_a_file_keeps_the_body_byte_for_byte(tmp_path: Path):
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    note = tmp_path / "reply.md"
    note.write_text(TRICKY, encoding="utf-8")

    result = runner.invoke(
        app, ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(note), "-w", str(workspace)]
    )

    assert result.exit_code == 0, result.output
    draft = bridge / "outbox" / "01KZ000000000000000000AAAA.md"
    assert draft.read_text(encoding="utf-8") == TRICKY, "正文没有原样落到草稿里"


def test_bridge_send_from_a_file_keeps_the_body_byte_for_byte(tmp_path: Path):
    """`--to` 那条路和 `--reply` 一样要能用 —— 两边都在拼 `_draft` 的正文。"""
    workspace = bridge_workspace(tmp_path)
    note = tmp_path / "say.md"
    note.write_text(TRICKY, encoding="utf-8")

    result = runner.invoke(
        app, ["bridge", "cc", "--to", "box:cli", "--text-file", str(note), "-w", str(workspace)]
    )

    assert result.exit_code == 0, result.output
    drafts = list((NodeLayout(workspace).agent_dir("cc") / "bridge" / "outbox").glob("*.md"))
    assert len(drafts) == 1
    assert TRICKY in drafts[0].read_text(encoding="utf-8")


def test_bridge_reply_reads_the_body_from_stdin(tmp_path: Path):
    """`-` 读 stdin，走 Unix 老规矩 —— 人在终端里配引号定界的 heredoc 用。"""
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)

    result = runner.invoke(
        app,
        ["bridge", "cc", "--reply", "00AAAA", "--text-file", "-", "-w", str(workspace)],
        input=TRICKY,
    )

    assert result.exit_code == 0, result.output
    draft = bridge / "outbox" / "01KZ000000000000000000AAAA.md"
    assert draft.read_text(encoding="utf-8").rstrip("\n") == TRICKY


def test_bridge_refuses_both_text_and_text_file(tmp_path: Path):
    """同时给两个正文来源 —— 不猜哪个优先，直接报错。"""
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    note = tmp_path / "reply.md"
    note.write_text("从文件来的", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "bridge",
            "cc",
            "--reply",
            "00AAAA",
            "--text",
            "从命令行来的",
            "--text-file",
            str(note),
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert not list((bridge / "outbox").glob("*.md")), "报错时不该留下半条草稿"


def test_bridge_text_file_that_is_missing_says_which_path(tmp_path: Path):
    """读不到就得说清是哪个路径 —— 空正文静默发出去比报错难查得多。"""
    workspace = bridge_workspace(tmp_path)
    seed_pending(workspace)
    missing = tmp_path / "nope.md"

    result = runner.invoke(
        app,
        ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(missing), "-w", str(workspace)],
    )

    assert result.exit_code != 0
    assert "nope.md" in result.output


def test_bridge_text_file_that_is_not_utf8_says_so_instead_of_a_traceback(tmp_path: Path):
    """非 UTF-8 的文件要和其他错误路径一样干净地报错。

    `UnicodeDecodeError` 是 `ValueError` 的子类**不是 `OSError`** —— 只 catch
    OSError 的话它会溜过去，用户看到的是裸 traceback、`result.output` 是空的。
    偏偏「传日志片段、二进制样本」正是这个选项存在的理由之一，那类文件最
    可能不是 UTF-8，撞上的就是最难看的那条路。
    """
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    note = tmp_path / "binary.bin"
    note.write_bytes(b"\xff\xfe\x00\x01\x80\x81")

    result = runner.invoke(
        app, ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(note), "-w", str(workspace)]
    )

    assert result.exit_code != 0
    assert "binary.bin" in result.output, "没说是哪个文件"
    assert not isinstance(result.exception, UnicodeDecodeError), "漏出了裸 traceback"
    assert not list((bridge / "outbox").glob("*.md"))


def test_bridge_text_file_that_is_a_directory_fails(tmp_path: Path):
    workspace = bridge_workspace(tmp_path)
    seed_pending(workspace)

    result = runner.invoke(
        app,
        ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(tmp_path), "-w", str(workspace)],
    )

    assert result.exit_code != 0
    assert "目录" in result.output


def test_bridge_text_file_does_not_scrub_control_characters(tmp_path: Path):
    """**协议层对控制字符保持透明。**

    实测裸 NUL 能从草稿一路原样抵达对话日志（信封、JSON 往返、渲染全保真），
    全仓送件路径也没有任何控制字符过滤。这条钉子防的是「以后有人顺手加清洗」——
    真要清洗该在显示层做（面板的 md 渲染已经在剥 NUL），不是在这儿。
    """
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    raw = "前\x00后\x07还有\ttab"
    note = tmp_path / "raw.md"
    note.write_text(raw, encoding="utf-8")

    result = runner.invoke(
        app, ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(note), "-w", str(workspace)]
    )

    assert result.exit_code == 0, result.output
    draft = bridge / "outbox" / "01KZ000000000000000000AAAA.md"
    assert draft.read_text(encoding="utf-8") == raw, "控制字符被清洗了 —— 协议层该透明"


def test_bridge_text_file_that_is_empty_is_refused(tmp_path: Path):
    """空文件跟空 --text 一个待遇：不发。"""
    workspace = bridge_workspace(tmp_path)
    bridge = seed_pending(workspace)
    note = tmp_path / "empty.md"
    note.write_text("   \n", encoding="utf-8")

    result = runner.invoke(
        app, ["bridge", "cc", "--reply", "00AAAA", "--text-file", str(note), "-w", str(workspace)]
    )

    assert result.exit_code != 0
    assert not list((bridge / "outbox").glob("*.md"))


def test_cli_send_shows_up_in_the_chat_records(workspace: Path):
    """`anthill send` 发的消息也得进对话记录 —— 以前只有面板发的才记，
    于是对话页上只见对方的回音、不见你发出去的那半句，
    跨机器联调时看着就像「消息没显示」。"""
    result = runner.invoke(app, ["send", "echo", "CLI 发的这句要看得见", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    chats = list((workspace / ".anthill" / "chats").glob("*.jsonl"))
    assert chats, "CLI 发件没有进 chats 记录"
    assert any("CLI 发的这句要看得见" in p.read_text(encoding="utf-8") for p in chats)
