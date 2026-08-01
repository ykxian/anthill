"""各消息类型的 payload schema（02-protocol §1「消息类型」表）。

每个 payload 都是 frozen 模型且 extra="forbid"：
- frozen：贯彻不可变原则，信封一旦构造就不再被就地修改
- forbid：拼错字段立刻报错，而不是被 LLM 悄悄吞掉
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskRequestPayload(_Payload):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=32_000)
    artifacts: tuple[str, ...] = ()
    priority: Priority = Priority.NORMAL
    risk: RiskLevel = RiskLevel.LOW


class TaskResultPayload(_Payload):
    summary: str = Field(min_length=1, max_length=32_000)
    artifacts: tuple[str, ...] = ()
    status: Literal["ok", "partial"] = "ok"


class TaskErrorPayload(_Payload):
    error: str = Field(min_length=1, max_length=8_000)
    retryable: bool = True


class ChatPayload(_Payload):
    body: str = Field(min_length=1, max_length=32_000)
    mentions: tuple[str, ...] = ()


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
