"""`anthill state` 直接共享文档入口；不得触碰消息与模型运行面。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.paths import NodeLayout
from anthill.core.state_sync import STATE_SCOPE, StateStore

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "statebox"])
    assert result.exit_code == 0, result.output
    return tmp_path


def non_state_files(workspace: Path) -> dict[str, bytes]:
    root = workspace / ".anthill"
    answer: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[:2] == ("blackboard", "state"):
            continue
        answer[str(relative)] = path.read_bytes()
    return answer


def publish(workspace: Path, revision: int = 1, snapshot: str = '{"ready":true}'):
    return runner.invoke(
        app,
        [
            "state",
            "publish",
            "project.board",
            snapshot,
            "--revision",
            str(revision),
            "--summary",
            f"revision {revision}",
            "--from",
            "cli",
            "-w",
            str(workspace),
        ],
    )


def test_publish_writes_one_shared_document_and_zero_message_artifacts(workspace: Path) -> None:
    before = non_state_files(workspace)

    result = publish(workspace)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "applied"
    assert data["scope"] == STATE_SCOPE
    layout = NodeLayout(workspace)
    state_files = sorted(path.name for path in layout.state_dir.glob("*.json"))
    assert state_files == ["project.board.json"]
    assert non_state_files(workspace) == before
    for agent in ("cli", "coordinator", "echo"):
        agent_root = layout.agent_dir(agent)
        assert not (agent_root / "state").exists()
        assert not (agent_root / "bridge" / "inbox").exists()
        assert not (agent_root / "bridge" / "pending").exists()
        assert not (agent_root / "threads").exists()
        mailbox = layout.mailbox_dir(agent)
        assert not list((mailbox / "new").glob("*.json"))
        assert not list((mailbox / "cur").glob("*.json"))
    assert not list(layout.root.rglob("outbox/*.json"))
    assert not list(layout.root.rglob("spool/**/*.json"))


def test_100_project_status_publishes_create_zero_mail_queue_wake_or_model_calls(
    workspace: Path,
) -> None:
    layout = NodeLayout(workspace)
    before = non_state_files(workspace)
    queue_before = {
        agent: len(list((layout.mailbox_dir(agent) / "new").glob("*.json")))
        for agent in ("cli", "coordinator", "echo")
    }

    with (
        patch("anthill.core.mailbox.Mailbox.deposit") as deposit,
        patch("anthill.agent.runtime.AgentRuntime.run") as wake,
        patch("anthill.providers.fake.FakeProvider.complete") as model,
    ):
        for revision in range(1, 101):
            result = publish(
                workspace,
                revision=revision,
                snapshot=json.dumps({"revision": revision}),
            )
            assert result.exit_code == 0, result.output

    queue_after = {
        agent: len(list((layout.mailbox_dir(agent) / "new").glob("*.json")))
        for agent in ("cli", "coordinator", "echo")
    }
    assert deposit.call_count == wake.call_count == model.call_count == 0
    assert queue_after == queue_before
    assert non_state_files(workspace) == before
    assert [path.name for path in layout.state_dir.glob("*.json")] == ["project.board.json"]


def test_one_status_update_with_ten_online_mailboxes_still_has_one_copy_and_zero_mail(
    workspace: Path,
) -> None:
    layout = NodeLayout(workspace)
    agents = [f"worker{index}" for index in range(10)]
    for agent in agents:
        from anthill.core.mailbox import Mailbox

        Mailbox(layout.mailbox_dir(agent)).ensure()
    before = {
        agent: len(list((layout.mailbox_dir(agent) / "new").glob("*.json"))) for agent in agents
    }

    assert publish(workspace).exit_code == 0

    after = {
        agent: len(list((layout.mailbox_dir(agent) / "new").glob("*.json"))) for agent in agents
    }
    assert after == before
    assert len(list(layout.state_dir.glob("*.json"))) == 1


def test_publish_does_not_import_the_transport_or_runtime_plane() -> None:
    import anthill.cli.state_cmd as state_cmd

    forbidden = {
        "Sender",
        "Router",
        "TransportRegistry",
        "Mailbox",
        "DeliveryTracker",
        "Envelope",
        "Address",
        "AgentRuntime",
    }
    assert forbidden.isdisjoint(vars(state_cmd))


@pytest.mark.parametrize("snapshot", ["[]", '"text"', "42", "null"])
def test_publish_rejects_non_object_json(workspace: Path, snapshot: str) -> None:
    result = publish(workspace, snapshot=snapshot)
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
        app, ["state", "publish", "project.board", "{}", *options, "-w", str(workspace)]
    )
    assert result.exit_code != 0
    assert missing in result.output


def test_old_recipient_and_replica_flags_are_rejected(workspace: Path) -> None:
    old_publish = runner.invoke(
        app,
        [
            "state",
            "publish",
            "project.board",
            "{}",
            "--revision",
            "1",
            "--summary",
            "old",
            "--to",
            "all",
            "-w",
            str(workspace),
        ],
    )
    old_read = runner.invoke(app, ["state", "list", "--agent", "echo", "-w", str(workspace)])
    assert old_publish.exit_code != 0 and "--to" in old_publish.output
    assert old_read.exit_code != 0 and "--agent" in old_read.output


def test_publish_rejects_unconfigured_publisher(workspace: Path) -> None:
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
            "bad",
            "--from",
            "ghost",
            "-w",
            str(workspace),
        ],
    )
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_list_is_metadata_only_and_show_reads_detail(workspace: Path) -> None:
    assert (
        publish(workspace, revision=37, snapshot='{"ready":true,"workers":["a","b"]}').exit_code
        == 0
    )

    listed = runner.invoke(app, ["state", "list", "-w", str(workspace)])
    shown = runner.invoke(app, ["state", "show", "project.board", "-w", str(workspace)])

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    list_data = json.loads(listed.output)
    show_data = json.loads(shown.output)
    assert list_data["scope"] == "node-local"
    assert list_data["node"] == "statebox"
    assert set(list_data["states"][0]) == {
        "key",
        "publisher",
        "revision",
        "digest",
        "summary",
        "updated_at",
    }
    assert "snapshot" not in list_data["states"][0]
    assert show_data["state"]["publisher"] == "statebox:cli"
    assert show_data["state"]["snapshot"] == {"ready": True, "workers": ["a", "b"]}


def test_stale_and_conflict_are_nonzero_and_do_not_replace_current(workspace: Path) -> None:
    assert publish(workspace, revision=3, snapshot='{"value":"current"}').exit_code == 0
    stale = publish(workspace, revision=2, snapshot='{"value":"old"}')
    conflict = publish(workspace, revision=3, snapshot='{"value":"conflict"}')

    assert stale.exit_code != 0 and '"status": "stale"' in stale.output
    assert conflict.exit_code != 0 and '"status": "conflict"' in conflict.output
    saved = StateStore(NodeLayout(workspace).state_dir).load("project.board")
    assert saved is not None and saved.snapshot == {"value": "current"}


def test_state_is_shared_only_inside_one_workspace(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for workspace, node in ((left, "left-node"), (right, "right-node")):
        result = runner.invoke(app, ["init", str(workspace), "--node-name", node])
        assert result.exit_code == 0, result.output
    assert publish(left).exit_code == 0

    left_list = runner.invoke(app, ["state", "list", "-w", str(left)])
    right_list = runner.invoke(app, ["state", "list", "-w", str(right)])
    assert len(json.loads(left_list.output)["states"]) == 1
    assert json.loads(right_list.output)["states"] == []


def test_show_rejects_path_key_and_missing_key(workspace: Path) -> None:
    invalid = runner.invoke(app, ["state", "show", "../node", "-w", str(workspace)])
    missing = runner.invoke(app, ["state", "show", "missing.key", "-w", str(workspace)])
    assert invalid.exit_code != 0 and "非法 state key" in invalid.output
    assert missing.exit_code != 0 and "没有 state key" in missing.output


def test_reader_rejects_state_directory_outside_workspace(workspace: Path) -> None:
    outside = workspace.parent / "outside-state"
    outside.mkdir()
    state_dir = NodeLayout(workspace).state_dir
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    state_dir.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(app, ["state", "list", "-w", str(workspace)])
    assert result.exit_code != 0
    assert "离开了当前 workspace" in result.output


@pytest.mark.parametrize("command", [["state", "list"], ["state", "show", "project.board"]])
def test_readers_fail_closed_on_corrupt_shared_state(workspace: Path, command: list[str]) -> None:
    assert publish(workspace).exit_code == 0
    path = StateStore(NodeLayout(workspace).state_dir).path_for("project.board")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["digest"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")

    result = runner.invoke(app, [*command, "-w", str(workspace)])
    assert result.exit_code != 0
    assert "digest" in result.output
