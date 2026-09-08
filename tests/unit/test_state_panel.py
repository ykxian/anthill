"""共享状态公告 Panel 数据面：有界读取、单一 Core 校验与零 replica 语义。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.core.state_sync import MAX_STATE_FILE_BYTES, StateDocumentUpdate, StateStore
from anthill.web import state_panel
from anthill.web.state_panel import StatePanelError, list_state_metadata, state_detail


def put(
    layout: NodeLayout,
    key: str,
    revision: int,
    snapshot: dict[str, object],
    *,
    publisher: str = "testnode:cli",
    summary: str = "公告摘要",
) -> None:
    update = StateDocumentUpdate.from_snapshot(
        key=key,
        revision=revision,
        summary=summary,
        snapshot=snapshot,  # type: ignore[arg-type]
    )
    assert StateStore(layout.state_dir).publish(update, publisher=publisher).applied


def test_index_contains_metadata_but_snapshot_only_appears_in_detail(
    layout: NodeLayout, config: Config
) -> None:
    put(
        layout,
        "project.board",
        7,
        {"secret_body": "只应在详情出现"},
        summary="给人扫一眼的摘要",
    )

    listing = list_state_metadata(layout, config)
    listed = listing.body["states"]
    assert isinstance(listed, list) and len(listed) == 1
    assert set(listed[0]) == {
        "key",
        "revision",
        "publisher",
        "digest",
        "summary",
        "stored_at",
        "size_bytes",
        "etag",
    }
    assert listing.body["scope"] == "node-local"
    assert "agents" not in listing.body
    assert "只应在详情出现" not in json.dumps(listing.body, ensure_ascii=False)
    assert listing.etag.startswith('"states-')

    detail = state_detail(layout, config, "project.board")
    assert detail.body["state"]["snapshot"] == {"secret_body": "只应在详情出现"}
    assert detail.body["state"]["etag"] == listed[0]["etag"]
    assert detail.etag == listed[0]["etag"]


def test_each_key_has_exactly_one_shared_record(layout: NodeLayout, config: Config) -> None:
    put(layout, "project.board", 1, {"ready": True})
    put(layout, "system.health", 1, {"ok": True})

    result = list_state_metadata(layout, config).body

    assert [item["key"] for item in result["states"]] == ["project.board", "system.health"]
    dumped = json.dumps(result)
    assert "replica_status" not in dumped
    assert "ahead" not in dumped and "behind" not in dumped


def test_bad_files_become_issues_without_hiding_good_ones(
    layout: NodeLayout, config: Config, tmp_path: Path
) -> None:
    put(layout, "good.board", 1, {"ok": True})
    state_dir = layout.state_dir
    (state_dir / "broken.json").write_text("{", encoding="utf-8")
    (state_dir / "BAD.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "outside.json"
    target.write_text('{"secret":"outside"}', encoding="utf-8")
    (state_dir / "linked.json").symlink_to(target)
    (state_dir / "huge.json").write_bytes(b"x" * (MAX_STATE_FILE_BYTES + 1))
    os.mkfifo(state_dir / "pipe.json")

    result = list_state_metadata(layout, config).body

    assert [item["key"] for item in result["states"]] == ["good.board"]
    codes = {item["code"] for item in result["issues"]}
    assert {"invalid_document", "invalid_name", "symlink", "oversize", "not_regular"} <= codes
    assert "outside" not in json.dumps(result)


def test_symlinked_shared_state_directory_is_never_followed(
    layout: NodeLayout, config: Config
) -> None:
    outside = layout.workspace / "outside"
    outside.mkdir()
    (outside / "project.board.json").write_text('{"secret":"outside"}', encoding="utf-8")
    layout.state_dir.symlink_to(outside, target_is_directory=True)

    result = list_state_metadata(layout, config).body

    assert result["states"] == []
    assert result["issues"][0]["code"] == "unsafe_directory"
    with pytest.raises(StatePanelError) as caught:
        state_detail(layout, config, "project.board")
    assert caught.value.status_code == 409


def test_detail_rejects_invalid_keys_and_oversize_files(layout: NodeLayout, config: Config) -> None:
    state_dir = layout.state_dir
    state_dir.mkdir(parents=True)
    (state_dir / "huge.json").write_bytes(b"x" * (MAX_STATE_FILE_BYTES + 1))

    with pytest.raises(StatePanelError) as invalid:
        state_detail(layout, config, "../huge")
    assert invalid.value.status_code == 400
    with pytest.raises(StatePanelError) as huge:
        state_detail(layout, config, "huge")
    assert huge.value.status_code == 413


def test_shared_file_count_is_bounded(
    layout: NodeLayout, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_panel, "MAX_STATE_FILES", 2)
    for number in range(3):
        put(layout, f"key{number}", 1, {"number": number})

    result = list_state_metadata(layout, config).body

    assert len(result["states"]) == 2
    assert result["truncated"] is True
    assert any(issue["code"] == "limit" for issue in result["issues"])


def test_directory_scan_counts_non_json_entries_toward_hard_limit(
    layout: NodeLayout, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_panel, "MAX_STATE_DIRECTORY_ENTRIES", 3)
    state_dir = layout.state_dir
    state_dir.mkdir(parents=True)
    for number in range(4):
        (state_dir / f"junk-{number}.txt").touch()

    result = list_state_metadata(layout, config).body

    assert result["states"] == []
    assert result["truncated"] is True
    assert any(issue["code"] == "limit" for issue in result["issues"])
