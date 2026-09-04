"""状态公告的数据面：有界读取、逐文件隔离与副本机械比较。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.core.payloads import StateUpdatePayload
from anthill.core.state_sync import StateStore
from anthill.web.state_panel import (
    MAX_STATE_DIRECTORY_ENTRIES,
    MAX_STATE_FILE_BYTES,
    MAX_STATE_FILES_PER_AGENT,
    StatePanelError,
    list_state_metadata,
    state_detail,
)


def put(
    layout: NodeLayout,
    agent: str,
    key: str,
    revision: int,
    snapshot: dict[str, object],
    *,
    source: str = "testnode:cli",
    summary: str = "公告摘要",
) -> None:
    payload = StateUpdatePayload.from_snapshot(
        key=key,
        revision=revision,
        summary=summary,
        snapshot=snapshot,  # type: ignore[arg-type]
    )
    assert StateStore(layout.state_dir(agent)).apply(payload, source=source).applied


def group(body: dict[str, Any], agent: str) -> dict[str, Any]:
    groups = body["agents"]
    assert isinstance(groups, list)
    return next(item for item in groups if isinstance(item, dict) and item["agent"] == agent)


def test_index_contains_metadata_but_snapshot_only_appears_in_detail(
    layout: NodeLayout, config: Config
) -> None:
    put(
        layout,
        "alpha",
        "project.board",
        7,
        {"secret_body": "只应在详情出现"},
        summary="给人扫一眼的摘要",
    )

    listing = list_state_metadata(layout, config)
    listed = group(listing.body, "alpha")["states"]
    assert isinstance(listed, list) and len(listed) == 1
    assert set(listed[0]) == {
        "key",
        "revision",
        "source",
        "digest",
        "summary",
        "stored_at",
        "size_bytes",
        "etag",
        "replica_status",
        "replica_note",
    }
    assert "只应在详情出现" not in json.dumps(listing.body, ensure_ascii=False)
    assert listing.etag.startswith('"states-')

    detail = state_detail(layout, config, "alpha", "project.board")
    assert detail.body["snapshot"] == {"secret_body": "只应在详情出现"}
    assert detail.body["etag"] == listed[0]["etag"]
    assert detail.etag == listed[0]["etag"]


def test_only_configured_agents_are_enumerated(layout: NodeLayout, config: Config) -> None:
    put(layout, "ghost", "project.board", 1, {"must_not_leak": True})

    result = list_state_metadata(layout, config)

    assert "ghost" not in [item["agent"] for item in result.body["agents"]]
    assert "must_not_leak" not in json.dumps(result.body)
    with pytest.raises(StatePanelError) as caught:
        state_detail(layout, config, "ghost", "project.board")
    assert caught.value.status_code == 404


def test_replica_labels_are_only_mechanical_comparisons(
    layout: NodeLayout, config: Config
) -> None:
    put(layout, "alpha", "project.board", 4, {"value": "new"})
    put(layout, "beta", "project.board", 2, {"value": "old"})
    put(layout, "alpha", "sync.board", 3, {"ok": True})
    put(layout, "beta", "sync.board", 3, {"ok": True})
    put(layout, "gamma", "solo.board", 1, {"only": True})

    result = list_state_metadata(layout, config).body
    records = {
        (item["agent"], record["key"]): record
        for item in result["agents"]
        for record in item["states"]
    }

    assert records[("alpha", "project.board")]["replica_status"] == "ahead"
    assert records[("beta", "project.board")]["replica_status"] == "behind"
    assert records[("alpha", "sync.board")]["replica_status"] == "in_sync"
    assert records[("beta", "sync.board")]["replica_status"] == "in_sync"
    assert records[("gamma", "solo.board")]["replica_status"] == "only_copy"
    assert "本机落盘" not in records[("alpha", "project.board")]["replica_note"]


@pytest.mark.parametrize("different_source", [False, True])
def test_currently_visible_divergence_is_marked_as_conflict(
    layout: NodeLayout, config: Config, different_source: bool
) -> None:
    put(layout, "alpha", "conflict.board", 5, {"value": "a"})
    put(
        layout,
        "beta",
        "conflict.board",
        5,
        {"value": "b"},
        source="testnode:beta" if different_source else "testnode:cli",
    )

    result = list_state_metadata(layout, config).body
    records = [
        record
        for item in result["agents"]
        for record in item["states"]
        if record["key"] == "conflict.board"
    ]
    assert {record["replica_status"] for record in records} == {"conflict"}


def test_bad_files_become_per_agent_issues_without_hiding_good_ones(
    layout: NodeLayout, config: Config, tmp_path: Path
) -> None:
    put(layout, "alpha", "good.board", 1, {"ok": True})
    state_dir = layout.state_dir("alpha")
    (state_dir / "broken.json").write_text("{", encoding="utf-8")
    (state_dir / "BAD.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "outside.json"
    target.write_text('{"secret":"outside"}', encoding="utf-8")
    (state_dir / "linked.json").symlink_to(target)
    (state_dir / "huge.json").write_bytes(b"x" * (MAX_STATE_FILE_BYTES + 1))
    os.mkfifo(state_dir / "pipe.json")

    result = list_state_metadata(layout, config).body
    alpha = group(result, "alpha")

    assert [item["key"] for item in alpha["states"]] == ["good.board"]
    codes = {item["code"] for item in alpha["issues"]}
    assert {"invalid_json", "invalid_name", "symlink", "oversize", "not_regular"} <= codes
    assert "outside" not in json.dumps(alpha)


def test_symlinked_state_directory_is_never_followed(layout: NodeLayout, config: Config) -> None:
    outside = layout.workspace / "outside"
    outside.mkdir()
    (outside / "project.board.json").write_text('{"secret":"outside"}', encoding="utf-8")
    layout.state_dir("beta").symlink_to(outside, target_is_directory=True)

    result = list_state_metadata(layout, config).body
    beta = group(result, "beta")

    assert beta["states"] == []
    assert beta["issues"][0]["code"] == "unsafe_directory"
    with pytest.raises(StatePanelError) as caught:
        state_detail(layout, config, "beta", "project.board")
    assert caught.value.status_code == 409


def test_detail_rejects_invalid_keys_and_oversize_files(
    layout: NodeLayout, config: Config
) -> None:
    state_dir = layout.state_dir("alpha")
    state_dir.mkdir()
    (state_dir / "huge.json").write_bytes(b"x" * (MAX_STATE_FILE_BYTES + 1))

    with pytest.raises(StatePanelError) as invalid:
        state_detail(layout, config, "alpha", "../huge")
    assert invalid.value.status_code == 400
    with pytest.raises(StatePanelError) as huge:
        state_detail(layout, config, "alpha", "huge")
    assert huge.value.status_code == 413


def test_per_agent_file_count_is_bounded(layout: NodeLayout, config: Config) -> None:
    for revision in range(1, MAX_STATE_FILES_PER_AGENT + 2):
        put(layout, "alpha", f"key{revision}", revision, {"revision": revision})

    alpha = group(list_state_metadata(layout, config).body, "alpha")

    assert len(alpha["states"]) == MAX_STATE_FILES_PER_AGENT
    assert alpha["truncated"] is True
    assert any(issue["code"] == "limit" for issue in alpha["issues"])


def test_directory_scan_counts_non_json_entries_toward_a_hard_limit(
    layout: NodeLayout, config: Config
) -> None:
    state_dir = layout.state_dir("beta")
    state_dir.mkdir()
    for number in range(MAX_STATE_DIRECTORY_ENTRIES + 1):
        (state_dir / f"junk-{number}.txt").touch()

    beta = group(list_state_metadata(layout, config).body, "beta")

    assert beta["states"] == []
    assert beta["truncated"] is True
    assert any(issue["code"] == "limit" for issue in beta["issues"])
