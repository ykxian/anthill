"""02-protocol §8 用例 1：schema 校验 —— 非法/缺字段/超大 payload 全部拒绝。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from anthill.core.envelope import (
    MAX_PAYLOAD_BYTES,
    Address,
    Envelope,
    ReplyVia,
    TransportKind,
)
from anthill.core.errors import EnvelopeTooLarge, HopLimitExceeded, ProtocolError
from anthill.core.ids import new_id, new_thread_id, now
from anthill.core.payloads import (
    ChatPayload,
    EventPayload,
    MessageType,
    ReceiptPayload,
    TaskResultPayload,
)


def test_round_trip_preserves_every_field(make_task):
    original = make_task()

    restored = Envelope.from_json_bytes(original.to_json_bytes())

    assert restored == original
    assert b'"from"' in original.to_json_bytes()  # 序列化用 alias，不是 from_


def test_defaults_are_filled(addr):
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="hi"),
    )

    assert env.proto == "1.0"
    assert env.hops == 1
    assert env.ttl_hops == 8
    assert env.expires_at is not None and env.expires_at > env.ts


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "not-a-ulid"),
        ("thread", "12345"),
        ("proto", "2.0"),
        ("hops", 0),
        ("ttl_hops", 0),
    ],
)
def test_rejects_malformed_fields(make_task, field, value):
    raw = make_task().model_dump(mode="json", by_alias=True)
    raw[field] = value

    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)


def test_rejects_unknown_field(make_task):
    raw = make_task().model_dump(mode="json", by_alias=True)
    raw["surprise"] = "值"

    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)


def test_rejects_missing_required_field(make_task):
    raw = make_task().model_dump(mode="json", by_alias=True)
    del raw["to"]

    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)


def test_rejects_payload_mismatched_with_type(addr):
    with pytest.raises(ValidationError):
        Envelope.new(
            sender=addr("alpha"),
            recipient=addr("beta"),
            type=MessageType.TASK_REQUEST,
            payload=ChatPayload(body="类型不匹配"),
        )


def test_rejects_unknown_message_type(make_task):
    raw = make_task().model_dump(mode="json", by_alias=True)
    raw["type"] = "task.teleport"

    with pytest.raises(ValidationError):
        Envelope.model_validate(raw)


def test_rejects_oversized_payload(addr):
    """大文件应该走 blackboard 产物目录，不进消息体。"""
    with pytest.raises((EnvelopeTooLarge, ValidationError)):
        Envelope.new(
            sender=addr("alpha"),
            recipient=addr("beta"),
            type=MessageType.CHAT,
            payload=ChatPayload(body="x" * (MAX_PAYLOAD_BYTES + 10)),
        )


def test_rejects_broadcast_for_non_event(addr):
    with pytest.raises(ValidationError):
        Envelope.new(
            sender=addr("alpha"),
            recipient=Address(node="testnode", agent="all"),
            type=MessageType.CHAT,
            payload=ChatPayload(body="不许广播聊天"),
        )


def test_allows_broadcast_for_event(addr):
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=Address(node="testnode", agent="all"),
        type=MessageType.EVENT,
        payload=EventPayload(kind="board.updated"),
    )

    assert env.to.is_broadcast


@pytest.mark.parametrize("agent", ["Alpha", "9beta", "role:", "a" * 40, ""])
def test_rejects_illegal_agent_names(agent):
    with pytest.raises(ValidationError):
        Address(node="testnode", agent=agent)


def test_role_address_is_parsed(addr):
    address = Address(node="testnode", agent="role:reviewer")

    assert address.is_role
    assert address.role == "reviewer"
    assert not addr("beta").is_role


def test_envelope_is_immutable(make_task):
    env = make_task()

    with pytest.raises(ValidationError):
        env.hops = 5  # type: ignore[misc]


def test_reply_increments_hops_and_keeps_thread(make_task, addr):
    env = make_task()

    reply = env.reply(
        type=MessageType.TASK_RESULT,
        payload=TaskResultPayload(summary="做完了"),
        sender=addr("beta"),
        recipient=addr("alpha"),
    )

    assert reply.hops == env.hops + 1
    assert reply.thread == env.thread
    assert reply.reply_to == env.id
    assert reply.id != env.id
    assert env.hops == 1  # 原信封没被改动


def test_reply_defaults_swap_sender_and_recipient(make_task):
    env = make_task()

    reply = env.reply(type=MessageType.CHAT, payload=ChatPayload(body="收到"))

    assert reply.from_ == env.to
    assert reply.to == env.from_


def test_reply_breaks_circuit_at_ttl(addr):
    """用例 6 的单元层：跳数达到上限后不许再产生新消息。"""
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="ping"),
        hops=3,
        ttl_hops=3,
    )

    with pytest.raises(HopLimitExceeded):
        env.reply(type=MessageType.CHAT, payload=ChatPayload(body="pong"))


def test_hops_above_ttl_is_invalid(addr):
    with pytest.raises(ValidationError):
        Envelope.new(
            sender=addr("alpha"),
            recipient=addr("beta"),
            type=MessageType.CHAT,
            payload=ChatPayload(body="x"),
            hops=9,
            ttl_hops=8,
        )


def test_expiry_is_evaluated_against_clock(addr):
    env = Envelope(
        from_=addr("alpha"),
        to=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="过期件"),
        thread=new_thread_id(),
        expires_at=now() - timedelta(seconds=1),
    )

    assert env.is_expired()
    assert not env.model_copy(update={"expires_at": now() + timedelta(hours=1)}).is_expired()


def test_canonical_bytes_excludes_signature_and_sorts_keys(make_task):
    env = make_task()
    signed = env.model_copy(update={"sig": "hmac-sha256:deadbeef"})

    assert env.canonical_bytes() == signed.canonical_bytes()
    assert b'"sig"' not in signed.canonical_bytes()


def test_from_json_bytes_rejects_garbage():
    with pytest.raises(ProtocolError):
        Envelope.from_json_bytes(b"{ not json")


def test_receipt_payload_carries_reference(make_task, addr):
    env = make_task()

    receipt = env.reply(
        type=MessageType.RECEIPT_ACCEPTED,
        payload=ReceiptPayload(ref=env.id),
        sender=addr("beta"),
    )

    assert receipt.type.is_receipt
    assert receipt.payload.ref == env.id


def test_reply_via_defaults_to_local(addr):
    env = Envelope.new(
        sender=addr("alpha"),
        recipient=addr("beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body="x"),
        reply_via=ReplyVia(transport=TransportKind.SSH, endpoint="lab-server"),
    )

    assert env.reply_via.transport is TransportKind.SSH
    assert (
        env.reply(type=MessageType.CHAT, payload=ChatPayload(body="y")).reply_via == env.reply_via
    )


def test_ids_are_unique_and_sortable():
    ids = [new_id() for _ in range(200)]

    assert len(set(ids)) == 200
    assert ids == sorted(ids)  # ULID 单调：字典序即时间序
