"""Content-addressed evidence storage and hard pre-model size gates.

Large text is never silently sliced.  The complete UTF-8 source is written once
under ``blackboard/details`` and only a bounded summary plus a verifiable
reference may continue toward an Agent or model.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anthill.core.atomic import atomic_write
from anthill.core.errors import ProtocolError
from anthill.core.payloads import EvidenceLevel, EvidenceRef, MessageType

if TYPE_CHECKING:
    from anthill.core.envelope import Envelope

MAX_DIRECT_BODY_BYTES = 4 * 1024
MAX_MODEL_MESSAGE_BYTES = 2 * 1024
MAX_TOOL_RESULT_BYTES = 8 * 1024
MAX_TOOL_RESULT_LINES = 200
MAX_AGENT_RESULT_LINES = 40
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
SUMMARY_BYTES = 768
TRUNCATED_WITH_EVIDENCE = "TRUNCATED_WITH_EVIDENCE"


@dataclass(frozen=True, slots=True)
class BoundedText:
    text: str
    details: EvidenceRef | None = None

    @property
    def truncated(self) -> bool:
        return self.details is not None


class EvidenceStore:
    """A bounded, content-addressed store; reads are always explicit."""

    def __init__(self, root: Path, *, reference_prefix: str = "details") -> None:
        self.root = root
        self.reference_prefix = reference_prefix.rstrip("/")

    def path_for(self, digest: str) -> Path:
        _validate_digest(digest)
        return self.root / f"{digest}.txt"

    def metadata_path_for(self, digest: str) -> Path:
        _validate_digest(digest)
        return self.root / f"{digest}.meta.json"

    def reference_for(self, digest: str) -> str:
        _validate_digest(digest)
        return f"{self.reference_prefix}/{digest}.txt"

    def put(
        self,
        content: str,
        *,
        owner: str,
        evidence_level: EvidenceLevel,
        needs_reply: bool,
    ) -> EvidenceRef:
        raw = content.encode("utf-8")
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ProtocolError(
                f"evidence {len(raw)} bytes exceeds hard limit {MAX_EVIDENCE_BYTES}; "
                "content was not injected into a model"
            )
        digest = hashlib.sha256(raw).hexdigest()
        path = self.path_for(digest)
        metadata_path = self.metadata_path_for(digest)
        record = {
            "version": 1,
            "sha256": digest,
            "bytes": len(raw),
            "lines": _line_count(content),
            "owner": _bounded_owner(owner),
            "evidence_level": str(evidence_level),
        }
        data = json.dumps(
            record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ).encode("utf-8")
        self._ensure_root()
        self._reject_symlink(path)
        self._reject_symlink(metadata_path)
        if path.is_file():
            existing = self._read_raw(path)
            if existing != raw:
                raise ProtocolError(f"evidence digest collision at {path}")
            try:
                metadata = metadata_path.read_bytes()
            except OSError as exc:
                raise ProtocolError(f"evidence metadata missing for {path}: {exc}") from exc
            self._decode_metadata(metadata, expected_digest=digest)
        else:
            # Metadata first, raw text last.  The .txt file is the commit
            # marker: a crash can leave an ignorable metadata orphan, never a
            # detail path whose ownership/level metadata is missing.
            atomic_write(self.root, self.root, metadata_path.name, data)
            atomic_write(self.root, self.root, path.name, raw)
            try:
                metadata_path.chmod(0o600)
                path.chmod(0o600)
            except OSError as exc:
                raise ProtocolError(f"cannot secure evidence permissions at {path}: {exc}") from exc
        return EvidenceRef(
            path=self.reference_for(digest),
            sha256=digest,
            bytes=len(raw),
            lines=_line_count(content),
            owner=_bounded_owner(owner),
            evidence_level=evidence_level,
            needs_reply=needs_reply,
        )

    def load(self, ref: EvidenceRef) -> str:
        """Explicit detail read.  Injection/rendering code must never call this."""
        expected = self.reference_for(ref.sha256)
        if ref.path != expected:
            raise ProtocolError(f"evidence path mismatch: expected {expected}, got {ref.path}")
        self._ensure_root()
        detail_path = self.path_for(ref.sha256)
        metadata_path = self.metadata_path_for(ref.sha256)
        self._reject_symlink(detail_path)
        self._reject_symlink(metadata_path)
        raw = self._read_raw(detail_path)
        try:
            metadata = metadata_path.read_bytes()
        except OSError as exc:
            raise ProtocolError(f"cannot read evidence metadata for {ref.path}: {exc}") from exc
        record = self._decode_metadata(metadata, expected_digest=ref.sha256)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"evidence {ref.path} is not UTF-8 text") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if actual != ref.sha256:
            raise ProtocolError(f"evidence SHA-256 mismatch for {ref.path}")
        if len(content.encode("utf-8")) != ref.bytes or _line_count(content) != ref.lines:
            raise ProtocolError(f"evidence metadata mismatch for {ref.path}")
        if record["bytes"] != ref.bytes or record["lines"] != ref.lines:
            raise ProtocolError(f"evidence sidecar mismatch for {ref.path}")
        return content

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise ProtocolError("evidence root must not be a symlink")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.root.is_dir() or self.root.is_symlink():
                raise ProtocolError("evidence root is not a safe directory")
            self.root.chmod(0o700)
        except OSError as exc:
            raise ProtocolError(f"cannot prepare evidence root {self.root}: {exc}") from exc

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ProtocolError(f"evidence path must not be a symlink: {path}")

    @staticmethod
    def _read_raw(path: Path) -> bytes:
        try:
            if path.stat().st_size > MAX_EVIDENCE_BYTES:
                raise ProtocolError(f"evidence file exceeds hard limit: {path}")
            return path.read_bytes()
        except OSError as exc:
            raise ProtocolError(f"cannot read evidence {path}: {exc}") from exc

    @staticmethod
    def _decode_metadata(raw: bytes, *, expected_digest: str) -> dict[str, Any]:
        if len(raw) > 4096:
            raise ProtocolError("evidence metadata exceeds hard limit")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"evidence is not valid UTF-8 JSON: {exc}") from exc
        required = {
            "version",
            "sha256",
            "bytes",
            "lines",
            "owner",
            "evidence_level",
        }
        if not isinstance(record, dict) or set(record) != required or record.get("version") != 1:
            raise ProtocolError("evidence schema is invalid")
        if record.get("sha256") != expected_digest:
            raise ProtocolError("evidence metadata SHA-256 mismatch")
        if (
            not isinstance(record.get("bytes"), int)
            or record["bytes"] < 0
            or not isinstance(record.get("lines"), int)
            or record["lines"] < 0
            or not isinstance(record.get("owner"), str)
            or not record["owner"].strip()
        ):
            raise ProtocolError("evidence metadata values are invalid")
        raw_level = record.get("evidence_level")
        try:
            EvidenceLevel(raw_level if isinstance(raw_level, str) else "")
        except (TypeError, ValueError) as exc:
            raise ProtocolError("evidence metadata level is invalid") from exc
        return record


def offload_envelope(env: Envelope, store: EvidenceStore) -> Envelope:
    """Externalize an oversized model-facing payload before mailbox deposit."""
    field = _content_field(env.type)
    if field is None or getattr(env.payload, "details", None) is not None:
        return env
    content = str(getattr(env.payload, field, "") or "")
    raw_size = len(content.encode("utf-8"))
    rendered_size = len(_rendered_inline(env, content).encode("utf-8"))
    line_limited_result = (
        env.type
        in {
            MessageType.TASK_RESULT,
            MessageType.TASK_ERROR,
        }
        and _line_count(content) > MAX_AGENT_RESULT_LINES
    )
    if (
        raw_size <= MAX_DIRECT_BODY_BYTES
        and rendered_size <= MAX_MODEL_MESSAGE_BYTES - 256
        and not line_limited_result
    ):
        return env
    ref = store.put(
        content,
        owner=str(env.from_),
        evidence_level=EvidenceLevel.MESSAGE_BODY,
        needs_reply=_needs_reply(env),
    )
    payload_data = env.payload.model_dump(mode="python")
    payload_data[field] = summarize(content)
    payload_data["details"] = ref
    payload = type(env.payload).model_validate(payload_data)
    return type(env).model_validate(
        {**env.model_dump(mode="python", by_alias=True), "payload": payload}
    )


def restore_evidence_for_signature(env: Envelope, store: EvidenceStore) -> Envelope:
    """Rebuild the originally signed envelope without exposing detail to a model."""
    ref = getattr(env.payload, "details", None)
    field = _content_field(env.type)
    if not isinstance(ref, EvidenceRef) or field is None:
        return env
    payload_data = env.payload.model_dump(mode="python")
    payload_data[field] = store.load(ref)
    payload_data["details"] = None
    payload = type(env.payload).model_validate(payload_data)
    return type(env).model_validate(
        {**env.model_dump(mode="python", by_alias=True), "payload": payload}
    )


def bound_text(
    content: str,
    *,
    store: EvidenceStore,
    owner: str,
    evidence_level: EvidenceLevel,
    needs_reply: bool = False,
    byte_limit: int,
    line_limit: int,
    label: str,
) -> BoundedText:
    if len(content.encode("utf-8")) <= byte_limit and _line_count(content) <= line_limit:
        return BoundedText(content)
    ref = store.put(
        content,
        owner=owner,
        evidence_level=evidence_level,
        needs_reply=needs_reply,
    )
    rendered = render_reference(summary=summarize(content), ref=ref, label=label)
    rendered = clip_utf8(rendered, byte_limit)
    rendered = "\n".join(rendered.splitlines()[:line_limit])
    return BoundedText(rendered, ref)


def render_reference(*, summary: str, ref: EvidenceRef, label: str = "summary") -> str:
    return (
        f"{TRUNCATED_WITH_EVIDENCE}\n"
        f"{label}: {summary}\n"
        f"details: {ref.path}\n"
        f"sha256: {ref.sha256}\n"
        f"bytes: {ref.bytes}\n"
        f"lines: {ref.lines}\n"
        f"owner: {ref.owner}\n"
        f"evidence_level: {ref.evidence_level}\n"
        f"needs_reply: {'yes' if ref.needs_reply else 'no'}"
    )


def summarize(content: str, *, byte_limit: int = SUMMARY_BYTES) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    summary = " | ".join(lines[:6]) or "(empty content)"
    if len(summary.encode("utf-8")) <= byte_limit:
        return summary
    return clip_utf8(summary, max(0, byte_limit - len("…".encode()))) + "…"


def clip_utf8(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    return raw[:limit].decode("utf-8", errors="ignore")


def envelope_evidence_digest(env: Envelope) -> str | None:
    ref = getattr(env.payload, "details", None)
    return ref.sha256 if isinstance(ref, EvidenceRef) else None


def _content_field(kind: MessageType) -> str | None:
    return {
        MessageType.TASK_REQUEST: "body",
        MessageType.TASK_RESULT: "summary",
        MessageType.TASK_ERROR: "error",
        MessageType.CHAT: "body",
    }.get(kind)


def _needs_reply(env: Envelope) -> bool:
    if env.type is MessageType.TASK_REQUEST:
        return True
    if env.type is MessageType.CHAT:
        mentions = tuple(getattr(env.payload, "mentions", ()) or ())
        return env.reply_to is None or bool(mentions)
    return False


def _rendered_inline(env: Envelope, content: str) -> str:
    title = str(getattr(env.payload, "title", "") or "")
    return f"{title}\n{content}" if title else content


def _line_count(content: str) -> int:
    return len(content.splitlines()) if content else 0


def _validate_digest(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("evidence digest must be lowercase SHA-256")


def _bounded_owner(owner: str) -> str:
    tidy = " ".join(owner.split())[:200]
    if not tidy:
        raise ProtocolError("evidence owner must be explicit")
    return tidy
