"""`anthill state publish` 的纯协议 producer 覆盖。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.envelope import Envelope
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, StateUpdatePayload
from anthill.core.state_sync import StateStore, snapshot_digest

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "statebox"])
    assert result.exit_code == 0, result.output
    return tmp_path


def state_messages(workspace: Path, agent: str) -> list[Envelope]:
    return [
        Mailbox.read_envelope(path)
        for path in Mailbox(NodeLayout(workspace).mailbox_dir(agent)).list_new()
    ]


def seed_replica(
    workspace: Path,
    *,
    agent: str = "echo",
    key: str = "project.board",
    revision: int = 37,
    snapshot: dict[str, object] | None = None,
) -> None:
    payload = StateUpdatePayload.from_snapshot(
        key=key,
        revision=revision,
        summary="current board",
        snapshot=snapshot or {"ready": True},  # type: ignore[arg-type]
    )
    result = StateStore(NodeLayout(workspace).state_dir(agent)).apply(
        payload, source="statebox:cli"
    )
    assert result.applied


def test_publish_delivers_a_valid_state_envelope_without_a_model(workspace: Path) -> None:
    snapshot = {"ready": True, "workers": ["a", "b"]}

    result = runner.invoke(
        app,
        [
            "state",
            "publish",
            "project.board",
            json.dumps(snapshot),
            "--revision",
            "1",
            "--summary",
            "workers ready",
            "--to",
            "echo",
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    messages = state_messages(workspace, "echo")
    assert len(messages) == 1
    env = messages[0]
    assert env.type is MessageType.STATE_UPDATE
    assert env.from_.agent == "cli"
    assert isinstance(env.payload, StateUpdatePayload)
    assert env.payload.revision == 1
    assert env.payload.snapshot == snapshot
    assert env.payload.digest == snapshot_digest(snapshot)  # type: ignore[arg-type]
    assert not NodeLayout(workspace).state_dir("echo").exists(), "producer 只投递，不冒充已应用"
    assert "已投递" in result.output
    assert "尚未确认 replica 已应用" in result.output
    assert "已同步" not in result.output


def test_publish_defaults_to_broadcast_and_fans_out(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "state",
            "publish",
            "system.health",
            '{"ok":true}',
            "--revision",
            "1",
            "--summary",
            "healthy",
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    # init 的默认成员是 cli/coordinator/echo；广播排除发布者 cli。
    assert len(state_messages(workspace, "coordinator")) == 1
    assert len(state_messages(workspace, "echo")) == 1
    assert state_messages(workspace, "cli") == []


@pytest.mark.parametrize("snapshot", ["[]", '"text"', "42", "null"])
def test_publish_rejects_non_object_json(workspace: Path, snapshot: str) -> None:
    result = runner.invoke(
        app,
        [
            "state",
            "publish",
            "project.board",
            snapshot,
            "--revision",
            "1",
            "--summary",
            "bad",
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert "JSON object" in result.output


@pytest.mark.parametrize(
    ("options", "missing"),
    [(["--summary", "has summary"], "--revision"), (["--revision", "1"], "--summary")],
)
def test_publish_requires_explicit_revision_and_summary(
    workspace: Path, options: list[str], missing: str
) -> None:
    result = runner.invoke(
        app,
        ["state", "publish", "project.board", "{}", *options, "-w", str(workspace)],
    )

    assert result.exit_code != 0
    assert missing in result.output


def test_publish_rejects_an_unconfigured_source_authority(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "state",
            "publish",
            "project.board",
            "{}",
            "--revision",
            "1",
            "--summary",
            "bad source",
            "--from",
            "ghost",
            "-w",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert "ghost" in result.output


def test_list_and_show_read_only_the_selected_validated_replica(workspace: Path) -> None:
    seed_replica(workspace, snapshot={"ready": True, "workers": ["a", "b"]})

    listed = runner.invoke(app, ["state", "list", "--agent", "echo", "-w", str(workspace)])
    shown = runner.invoke(
        app, ["state", "show", "project.board", "--agent", "echo", "-w", str(workspace)]
    )

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    list_data = json.loads(listed.output)
    show_data = json.loads(shown.output)
    assert list_data["replica"] == {"workspace": str(workspace), "agent": "echo"}
    assert list_data["states"][0]["key"] == "project.board"
    assert list_data["states"][0]["revision"] == 37
    assert list_data["states"][0]["source"] == "statebox:cli"
    assert len(list_data["states"][0]["digest"]) == 64
    assert list_data["states"][0]["summary"] == "current board"
    assert set(list_data["states"][0]) == {"key", "source", "revision", "digest", "summary"}
    assert "snapshot" not in list_data["states"][0]
    assert "snapshot" in show_data["state"]
    assert show_data["state"]["snapshot"] == {"ready": True, "workers": ["a", "b"]}


@pytest.mark.parametrize(
    "command",
    [
        ["state", "list", "--agent", "ghost"],
        ["state", "show", "project.board", "--agent", "ghost"],
        ["state", "show", "missing.key", "--agent", "echo"],
    ],
)
def test_replica_readers_reject_unknown_agent_or_key(workspace: Path, command: list[str]) -> None:
    result = runner.invoke(app, [*command, "-w", str(workspace)])

    assert result.exit_code != 0
    assert "replica" in result.output


def test_show_rejects_a_path_like_key_instead_of_reading_workspace_files(
    workspace: Path,
) -> None:
    result = runner.invoke(
        app, ["state", "show", "../node", "--agent", "echo", "-w", str(workspace)]
    )

    assert result.exit_code != 0
    assert "非法 state key" in result.output


def test_replica_reader_rejects_a_state_directory_outside_the_workspace(
    workspace: Path,
) -> None:
    outside = workspace.parent / "outside-replica"
    outside.mkdir()
    state_dir = NodeLayout(workspace).state_dir("echo")
    state_dir.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(app, ["state", "list", "--agent", "echo", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "离开了当前 workspace" in result.output


@pytest.mark.parametrize("command", [["state", "list"], ["state", "show", "project.board"]])
def test_replica_readers_fail_closed_on_corrupt_local_state(
    workspace: Path, command: list[str]
) -> None:
    seed_replica(workspace)
    path = StateStore(NodeLayout(workspace).state_dir("echo")).path_for("project.board")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["digest"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")

    result = runner.invoke(app, [*command, "--agent", "echo", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "digest" in result.output
