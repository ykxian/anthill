"""各消息类型的 payload schema（02-protocol §1「消息类型」表）。

每个 payload 都是 frozen 模型且 extra="forbid"：
- frozen：贯彻不可变原则，信封一旦构造就不再被就地修改
- forbid：拼错字段立刻报错，而不是被 LLM 悄悄吞掉
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_CONTENT_CHARS = 16 * 1024 * 1024


class MessageType(StrEnum):
    TASK_REQUEST = "task.request"
    TASK_RESULT = "task.result"
    TASK_ERROR = "task.error"
    CHAT = "chat"
    RECEIPT_DELIVERED = "receipt.delivered"
    RECEIPT_ACCEPTED = "receipt.accepted"
    RECEIPT_REJECTED = "receipt.rejected"
    RECEIPT_EXPIRED = "receipt.expired"
    EVENT = "event"
    HEARTBEAT = "heartbeat"

    @property
    def is_receipt(self) -> bool:
        return self.value.startswith("receipt.")


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class RiskLevel(StrEnum):
    """工具/任务风险分级，策略引擎的输入之一（03-tech-design §6）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceLevel(StrEnum):
    MESSAGE_BODY = "message_body"
    TOOL_RESULT = "tool_result"
    AGENT_RESULT = "agent_result"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(_Payload):
    """A model-safe pointer to complete, content-addressed evidence."""

    status: Literal["TRUNCATED_WITH_EVIDENCE"] = "TRUNCATED_WITH_EVIDENCE"
    path: str = Field(min_length=1, max_length=500)
    sha256: str
    bytes: int = Field(ge=0)
    lines: int = Field(ge=0)
    owner: str = Field(min_length=1, max_length=200)
    evidence_level: EvidenceLevel
    needs_reply: bool = False

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        if value.startswith(("/", "~")) or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("evidence path must stay relative to the workspace/blackboard")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("evidence sha256 must be 64 lowercase hexadecimal characters")
        return value


class TaskRequestPayload(_Payload):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=MAX_CONTENT_CHARS)
    artifacts: tuple[str, ...] = ()
    priority: Priority = Priority.NORMAL
    risk: RiskLevel = RiskLevel.LOW
    details: EvidenceRef | None = None


class TaskResultPayload(_Payload):
    summary: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    artifacts: tuple[str, ...] = ()
    status: Literal["ok", "partial"] = "ok"
    details: EvidenceRef | None = None


class TaskErrorPayload(_Payload):
    error: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    retryable: bool = True
    details: EvidenceRef | None = None


class ChatPayload(_Payload):
    body: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    mentions: tuple[str, ...] = ()
    details: EvidenceRef | None = None


class ReceiptPayload(_Payload):
    """三级回执共用。`ref` 指向被确认的那条消息 ID。"""

    ref: str
    reason: str | None = Field(default=None, max_length=2_000)


class EventPayload(_Payload):
    kind: str = Field(min_length=1, max_length=64)
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class HeartbeatPayload(_Payload):
    agent: str
    status: Literal["idle", "busy", "draining"] = "idle"
    queue_depth: int = Field(default=0, ge=0)


Payload = (
    TaskRequestPayload
    | TaskResultPayload
    | TaskErrorPayload
    | ChatPayload
    | ReceiptPayload
    | EventPayload
    | HeartbeatPayload
)

PAYLOAD_MODELS: dict[MessageType, type[_Payload]] = {
    MessageType.TASK_REQUEST: TaskRequestPayload,
    MessageType.TASK_RESULT: TaskResultPayload,
    MessageType.TASK_ERROR: TaskErrorPayload,
    MessageType.CHAT: ChatPayload,
    MessageType.RECEIPT_DELIVERED: ReceiptPayload,
    MessageType.RECEIPT_ACCEPTED: ReceiptPayload,
    MessageType.RECEIPT_REJECTED: ReceiptPayload,
    MessageType.RECEIPT_EXPIRED: ReceiptPayload,
    MessageType.EVENT: EventPayload,
    MessageType.HEARTBEAT: HeartbeatPayload,
}
