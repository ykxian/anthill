"""节点共享状态文档的 revision、publisher、原子写与跨进程锁。"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from anthill.core.errors import AntHillError, ProtocolError
from anthill.core.process_lock import ProcessLock
from anthill.core.state_sync import (
    MAX_STATE_FILE_BYTES,
    STATE_FILE_VERSION,
    STATE_SCOPE,
    StateDocumentUpdate,
    StatePublishStatus,
    StateStore,
    canonical_snapshot_bytes,
    snapshot_digest,
)

PUBLISHER = "lab:cli"


def update(
    revision: int, snapshot: dict[str, Any], *, summary: str = "状态"
) -> StateDocumentUpdate:
    return StateDocumentUpdate.from_snapshot(
        key="project.board", revision=revision, summary=summary, snapshot=snapshot
    )


def _hold_process_lock(path: str, ready: Any, release: Any) -> None:
    with ProcessLock(Path(path), label="test state publisher"):
        ready.set()
        release.wait(10)


def test_snapshot_digest_is_canonical_and_rejects_non_json_numbers() -> None:
    left = {"中文": [3, {"b": False, "a": None}], "z": 1}
    right = {"z": 1, "中文": [3, {"a": None, "b": False}]}
    assert canonical_snapshot_bytes(left) == canonical_snapshot_bytes(right)
    assert snapshot_digest(left) == snapshot_digest(right)
    with pytest.raises(ProtocolError, match="合法 JSON"):
        snapshot_digest({"bad": float("nan")})


def test_document_model_is_exact_and_digest_is_lowercase_sha256() -> None:
    with pytest.raises(ValidationError):
        StateDocumentUpdate.model_validate(
            {
                "key": "project.board",
                "revision": 1,
                "digest": "A" * 64,
                "summary": "x",
                "snapshot": {},
                "recipient": "all",
            }
        )


@pytest.mark.parametrize(
    "publisher",
    ["lab", "lab:role:worker", "lab:all", "Lab:cli", "lab:CLI", "lab:cli:extra"],
)
def test_publish_rejects_noncanonical_or_nonconcrete_publisher(
    tmp_path: Path, publisher: str
) -> None:
    with pytest.raises(ValueError, match="publisher"):
        StateStore(tmp_path / "state").publish(update(1, {}), publisher=publisher)


def test_publish_applied_duplicate_stale_and_conflict(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = update(3, {"status": "ready"}, summary="ready")

    assert store.publish(first, publisher=PUBLISHER).status is StatePublishStatus.APPLIED
    assert store.publish(first, publisher=PUBLISHER).status is StatePublishStatus.DUPLICATE
    assert (
        store.publish(update(2, {"status": "old"}), publisher=PUBLISHER).status
        is StatePublishStatus.STALE
    )
    assert (
        store.publish(update(3, {"status": "different"}), publisher=PUBLISHER).status
        is StatePublishStatus.CONFLICT
    )
    assert (
        store.publish(first.model_copy(update={"summary": "different"}), publisher=PUBLISHER).status
        is StatePublishStatus.CONFLICT
    )
    assert (
        store.publish(update(4, {"status": "next"}), publisher="lab:other").status
        is StatePublishStatus.CONFLICT
    )

    applied = store.publish(update(8, {"status": "current"}), publisher=PUBLISHER)
    assert applied.status is StatePublishStatus.APPLIED
    assert applied.skipped_revisions == 4
    record = store.load("project.board")
    assert record is not None
    assert record.publisher == PUBLISHER
    assert record.revision == 8
    assert record.snapshot == {"status": "current"}


def test_disk_schema_is_v2_node_local_and_has_no_delivery_fields(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.publish(update(1, {"ready": True}), publisher=PUBLISHER)
    raw = json.loads(store.path_for("project.board").read_text(encoding="utf-8"))

    assert raw["version"] == STATE_FILE_VERSION == 2
    assert raw["scope"] == STATE_SCOPE == "node-local"
    assert raw["publisher"] == PUBLISHER
    assert set(raw) == {
        "version",
        "scope",
        "key",
        "publisher",
        "revision",
        "digest",
        "summary",
        "snapshot",
    }
    assert not ({"recipient", "envelope", "receipt", "message_id"} & set(raw))


def test_corrupt_current_document_fails_closed_without_overwrite(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.publish(update(1, {"ready": True}), publisher=PUBLISHER)
    path = store.path_for("project.board")
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()

    result = store.publish(update(2, {"ready": False}), publisher=PUBLISHER)

    assert result.status is StatePublishStatus.CONFLICT
    assert path.read_bytes() == before


def test_shared_store_ignores_legacy_agent_replicas(tmp_path: Path) -> None:
    legacy = tmp_path / ".anthill" / "agents" / "alpha" / "state"
    legacy.mkdir(parents=True)
    (legacy / "project.board.json").write_text("{}", encoding="utf-8")
    store = StateStore(tmp_path / ".anthill" / "blackboard" / "state")

    assert store.list() == ()
    assert store.load("project.board") is None


def test_state_file_limit_is_enforced_before_replacing_current(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.publish(update(1, {"value": "small"}), publisher=PUBLISHER)
    path = store.path_for("project.board")
    before = path.read_bytes()

    with pytest.raises(ProtocolError, match="超过"):
        store.publish(update(2, {"value": "x" * MAX_STATE_FILE_BYTES}), publisher=PUBLISHER)
    assert path.read_bytes() == before


def test_cross_process_lock_is_fail_fast_and_preserves_store(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    store = StateStore(root)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_process_lock,
        args=(str(store.lock_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(AntHillError, match="已有一个实例"):
            store.publish(update(1, {"ready": True}), publisher=PUBLISHER)
        assert not store.path_for("project.board").exists()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert store.publish(update(1, {"ready": True}), publisher=PUBLISHER).applied


def test_state_directory_and_entry_symlinks_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProtocolError, match="符号链接"):
        StateStore(linked_root).publish(update(1, {}), publisher=PUBLISHER)

    root = tmp_path / "state"
    root.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    (root / "project.board.json").symlink_to(target)
    with pytest.raises(ProtocolError, match="符号链接"):
        StateStore(root).load("project.board")
