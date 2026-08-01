"""发送方视角的消息状态机（02-protocol §3）。

    pending → delivered → accepted → completed
       ↓          ↓           ↓
      dead     rejected     failed

只做状态与合法迁移，不碰 IO —— 于是可以被穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from anthill.core.envelope import Address, Envelope
from anthill.core.errors import ProtocolError
from anthill.core.ids import now
from anthill.core.payloads import MessageType


class DeliveryState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        DeliveryState.REJECTED,
        DeliveryState.COMPLETED,
        DeliveryState.FAILED,
        DeliveryState.DEAD,
        DeliveryState.EXPIRED,
    }
)

ALLOWED_TRANSITIONS: dict[DeliveryState, frozenset[DeliveryState]] = {
    DeliveryState.PENDING: frozenset(
        {DeliveryState.PENDING, DeliveryState.DELIVERED, DeliveryState.DEAD, DeliveryState.EXPIRED}
    ),
    DeliveryState.DELIVERED: frozenset(
        {
            DeliveryState.ACCEPTED,
            DeliveryState.REJECTED,
            DeliveryState.COMPLETED,  # 对端极快时 result 可能先于 accepted 到
            DeliveryState.FAILED,
            DeliveryState.EXPIRED,
        }
    ),
    DeliveryState.ACCEPTED: frozenset(
        {DeliveryState.COMPLETED, DeliveryState.FAILED, DeliveryState.EXPIRED}
    ),
    **{state: frozenset() for state in _TERMINAL},
}

RECEIPT_TO_STATE: dict[MessageType, DeliveryState] = {
    MessageType.RECEIPT_DELIVERED: DeliveryState.DELIVERED,
    MessageType.RECEIPT_ACCEPTED: DeliveryState.ACCEPTED,
    MessageType.RECEIPT_REJECTED: DeliveryState.REJECTED,
    MessageType.RECEIPT_EXPIRED: DeliveryState.EXPIRED,
    MessageType.TASK_RESULT: DeliveryState.COMPLETED,
    MessageType.TASK_ERROR: DeliveryState.FAILED,
}


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """一条已发出消息的当前状态。frozen —— 迁移返回新记录。"""

    msg_id: str
    thread: str
    to: Address
    type: MessageType
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    updated_at: datetime | None = None
    detail: str | None = None

    def transition(self, target: DeliveryState, *, detail: str | None = None) -> DeliveryRecord:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise ProtocolError(
                f"消息 {self.msg_id} 非法状态迁移：{self.state} → {target}"
                f"（允许：{sorted(ALLOWED_TRANSITIONS[self.state]) or '无，已是终态'}）"
            )
        return replace(self, state=target, updated_at=now(), detail=detail)

    def with_attempt(self, *, detail: str | None = None) -> DeliveryRecord:
        return replace(self, attempts=self.attempts + 1, updated_at=now(), detail=detail)

    @property
    def is_open(self) -> bool:
        """还在等对方后续动作吗？

        回执与 event 是「送到即完事」，不会再有回复；
        只有 task.request / chat 这类消息才需要继续等 accepted / result。
        """
        if self.state.is_terminal:
            return False
        return not (self._is_fire_and_forget and self.state is DeliveryState.DELIVERED)

    @property
    def _is_fire_and_forget(self) -> bool:
        return self.type.is_receipt or self.type in {
            MessageType.EVENT,
            MessageType.HEARTBEAT,
            MessageType.TASK_RESULT,
            MessageType.TASK_ERROR,
        }


class DeliveryTracker:
    """本 agentd 发出去的消息状态表。进程内存，重启即失忆（M1 够用）。"""

    def __init__(self) -> None:
        self._records: dict[str, DeliveryRecord] = {}

    def register(self, env: Envelope) -> DeliveryRecord:
        record = DeliveryRecord(
            msg_id=env.id, thread=env.thread, to=env.to, type=env.type, updated_at=now()
        )
        self._records[env.id] = record
        return record

    def get(self, msg_id: str) -> DeliveryRecord | None:
        return self._records.get(msg_id)

    def mark(
        self, msg_id: str, state: DeliveryState, *, detail: str | None = None
    ) -> DeliveryRecord | None:
        record = self._records.get(msg_id)
        if record is None or record.state.is_terminal or record.state is state:
            # 同态重复标记是幂等的：重试与回执都可能对同一条消息重复上报
            return record
        updated = record.transition(state, detail=detail)
        self._records[msg_id] = updated
        return updated

    def on_incoming(self, env: Envelope) -> DeliveryRecord | None:
        """收到回执/结果时推进对应原消息的状态。认不出的消息返回 None。"""
        state = RECEIPT_TO_STATE.get(env.type)
        if state is None:
            return None
        ref = getattr(env.payload, "ref", None) or env.reply_to
        if not isinstance(ref, str):
            return None
        detail = getattr(env.payload, "reason", None) or getattr(env.payload, "error", None)
        return self.mark(ref, state, detail=detail)

    def open_records(self) -> tuple[DeliveryRecord, ...]:
        """还在等对方回应的消息 —— coordinator 的催办清单就从这里来。"""
        return tuple(r for r in self._records.values() if r.is_open)

    def snapshot(self) -> tuple[DeliveryRecord, ...]:
        return tuple(self._records.values())
