"""state.update 的线协议、完整快照 replica 与 runtime pre-handler 快路径。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from anthill.agent.handlers import HandlerContext
from anthill.agent.runtime import AgentRuntime
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import MailboxError, ProtocolError
from anthill.core.logging import EventLog, read_log
from anthill.core.mailbox import Mailbox
from anthill.core.payloads import ChatPayload, MessageType, StateUpdatePayload, TaskRequestPayload
from anthill.core.state_sync import StateApplyStatus, StateStore, snapshot_digest

SOURCE = "testnode:cli"
OTHER_SOURCE = "testnode:alpha"


def update(
    revision: int,
    snapshot: dict[str, object],
    *,
    key: str = "project.board",
    summary: str | None = None,
) -> StateUpdatePayload:
    return StateUpdatePayload.from_snapshot(
        key=key,
        revision=revision,
        summary=summary or f"board revision {revision}",
        snapshot=snapshot,  # type: ignore[arg-type]
    )


def test_digest_is_canonical_and_state_key_is_strict() -> None:
    assert snapshot_digest({"b": 2, "a": {"z": 1}}) == snapshot_digest({"a": {"z": 1}, "b": 2})

    with pytest.raises(ValidationError, match="state key"):
        update(1, {"ok": True}, key="../BOARD")
    with pytest.raises(ValidationError, match="SHA-256"):
        StateUpdatePayload(
            key="project.board",
            revision=1,
            digest="not-a-digest",
            summary="bad",
            snapshot={},
        )


def test_store_applies_newer_complete_snapshots_and_classifies_noops(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = update(1, {"status": "starting"})

    assert store.apply(first, source=SOURCE).status is StateApplyStatus.APPLIED
    assert store.apply(first, source=SOURCE).status is StateApplyStatus.DUPLICATE
    assert (
        store.apply(update(1, {"status": "other"}), source=SOURCE).status
        is StateApplyStatus.CONFLICT
    )
    fifth = update(5, {"status": "running"})
    result = store.apply(fifth, source=SOURCE)
    assert result.status is StateApplyStatus.APPLIED
    assert result.skipped_revisions == 3
    assert store.apply(update(4, {"status": "old"}), source=SOURCE).status is StateApplyStatus.STALE

    saved = store.load("project.board")
    assert saved is not None
    assert saved.source == SOURCE
    assert saved.revision == 5
    assert saved.snapshot == {"status": "running"}


def test_fresh_snapshot_replica_can_start_at_revision_37(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    result = store.apply(update(37, {"status": "current"}), source=SOURCE)

    assert result.status is StateApplyStatus.APPLIED
    saved = store.load("project.board")
    assert saved is not None and saved.revision == 37


def test_disconnected_replica_recovers_from_latest_snapshot_only(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")

    assert store.apply(update(5, {"complete": True}), source=SOURCE).applied

    saved = store.load("project.board")
    assert saved is not None
    assert saved.revision == 5
    assert saved.snapshot == {"complete": True}


def test_declared_digest_is_recomputed_and_mismatch_never_writes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    good = update(1, {"status": "safe"})
    forged = good.model_copy(update={"digest": "0" * 64})

    result = store.apply(forged, source=SOURCE)

    assert result.status is StateApplyStatus.CONFLICT
    assert "digest mismatch" in result.reason
    assert store.load(good.key) is None


def test_atomic_write_failure_preserves_last_complete_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    assert store.apply(update(1, {"value": "old"}), source=SOURCE).applied
    path = store.path_for("project.board")
    before = path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("anthill.core.atomic.os.replace", fail_replace)

    with pytest.raises(MailboxError, match="simulated crash"):
        store.apply(update(2, {"value": "new"}), source=SOURCE)

    assert path.read_bytes() == before
    assert store.load("project.board").revision == 1  # type: ignore[union-attr]


def test_state_key_is_owned_by_its_first_envelope_source(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = update(1, {"value": "owner"})
    assert store.apply(first, source=SOURCE).applied
    path = store.path_for(first.key)
    before = path.read_bytes()

    same_revision = store.apply(first, source=OTHER_SOURCE)
    higher_revision = store.apply(update(2, {"value": "hijack"}), source=OTHER_SOURCE)

    assert same_revision.status is StateApplyStatus.CONFLICT
    assert higher_revision.status is StateApplyStatus.CONFLICT
    assert "authority mismatch" in same_revision.reason
    assert path.read_bytes() == before
    assert store.load(first.key).source == SOURCE  # type: ignore[union-attr]


def test_replica_without_publisher_authority_fails_closed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = update(1, {"value": "old"})
    assert store.apply(first, source=SOURCE).applied
    path = store.path_for(first.key)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("source")
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()

    result = store.apply(update(2, {"value": "new"}), source=SOURCE)

    assert result.status is StateApplyStatus.CONFLICT
    assert "local state invalid" in result.reason
    assert path.read_bytes() == before
    with pytest.raises(ProtocolError, match="source"):
        store.load(first.key)


class CountingHandler:
    name = "counting"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
        self.calls.append(env.id)


def state_envelope(payload: StateUpdatePayload, *, source_agent: str = "cli") -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent=source_agent),
        recipient=Address(node="testnode", agent="beta"),
        type=MessageType.STATE_UPDATE,
        payload=payload,
    )


async def test_runtime_consumes_every_state_outcome_without_handler_or_thread_history(
    layout, config, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = CountingHandler()
    log_path = layout.log_file("beta")
    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name="beta",
        handler=handler,
        log=EventLog(log_path, agent="beta", echo=False),
    )
    box = Mailbox(layout.mailbox_dir("beta"))
    receipts: list[MessageType] = []

    async def record_receipt(
        _source: Envelope, kind: MessageType, *, reason: str | None = None
    ) -> None:
        receipts.append(kind)

    # 本用例只验 runtime 分流；Sender/Maildir 的投递语义已有集成测试覆盖。
    monkeypatch.setattr(runtime.sender, "send_receipt", record_receipt)
    conflict = state_envelope(update(37, {"value": "conflict"}))
    envelopes_to_process = [
        state_envelope(update(37, {"value": "current"})),
        state_envelope(update(37, {"value": "current"})),  # fresh id, same snapshot
        conflict,
        conflict,  # same rejected envelope id must remain rejected on redelivery
        state_envelope(update(38, {"value": "hijack"}), source_agent="alpha"),
        state_envelope(update(40, {"value": "latest"})),
        state_envelope(update(39, {"value": "old"})),
    ]
    ordinary = [
        Envelope.new(
            sender=Address(node="testnode", agent="cli"),
            recipient=Address(node="testnode", agent="beta"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="still handled", body="task body"),
        ),
        Envelope.new(
            sender=Address(node="testnode", agent="cli"),
            recipient=Address(node="testnode", agent="beta"),
            type=MessageType.CHAT,
            payload=ChatPayload(body="chat body"),
        ),
    ]

    try:
        for env in envelopes_to_process:
            await runtime._process(box.deposit(env))
        for env in ordinary:
            await runtime._process(box.deposit(env))
    finally:
        await runtime.aclose()

    assert handler.calls == [env.id for env in ordinary]
    assert not (layout.agent_dir("beta") / "threads").exists()
    saved = StateStore(layout.state_dir("beta")).load("project.board")
    assert saved is not None
    assert saved.source == SOURCE and saved.revision == 40
    assert saved.snapshot == {"value": "latest"}
    archived_ids = {env.id for env in (*envelopes_to_process, *ordinary)}
    assert len(list(box.done.rglob("*.json"))) == len(archived_ids)

    statuses = {
        record["status"] for record in read_log(log_path) if record.get("event") == "state.update"
    }
    assert statuses == {status.value for status in StateApplyStatus}
    state_logs = [record for record in read_log(log_path) if record.get("event") == "state.update"]
    assert {record["source"] for record in state_logs} == {SOURCE, OTHER_SOURCE}

    assert receipts.count(MessageType.RECEIPT_ACCEPTED) == 6
    assert receipts.count(MessageType.RECEIPT_REJECTED) == 3

    on_disk = json.loads(StateStore(layout.state_dir("beta")).path_for("project.board").read_text())
    assert on_disk["source"] == SOURCE
    assert on_disk["digest"] == snapshot_digest(on_disk["snapshot"])
    applied_logs = [record for record in state_logs if record["status"] == "applied"]
    assert {record["skipped_revisions"] for record in applied_logs} == {0, 2}
