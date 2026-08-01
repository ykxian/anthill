"""02-protocol §8 用例 5（状态机部分）：回执驱动的发送方状态机。"""

from __future__ import annotations

import pytest

from anthill.core.errors import ProtocolError
from anthill.core.payloads import MessageType, ReceiptPayload, TaskErrorPayload, TaskResultPayload
from anthill.core.states import DeliveryState, DeliveryTracker


def test_happy_path_pending_to_completed(make_task, addr):
    env = make_task()
    tracker = DeliveryTracker()
    tracker.register(env)

    accepted = env.reply(
        type=MessageType.RECEIPT_ACCEPTED, payload=ReceiptPayload(ref=env.id), sender=addr("beta")
    )
    result = env.reply(
        type=MessageType.TASK_RESULT, payload=TaskResultPayload(summary="done"), sender=addr("beta")
    )

    tracker.mark(env.id, DeliveryState.DELIVERED)
    assert tracker.on_incoming(accepted).state is DeliveryState.ACCEPTED
    assert tracker.on_incoming(result).state is DeliveryState.COMPLETED
    assert tracker.open_records() == ()


def test_rejected_is_terminal(make_task, addr):
    env = make_task()
    tracker = DeliveryTracker()
    tracker.register(env)
    tracker.mark(env.id, DeliveryState.DELIVERED)

    rejected = env.reply(
        type=MessageType.RECEIPT_REJECTED,
        payload=ReceiptPayload(ref=env.id, reason="策略不允许"),
        sender=addr("beta"),
    )
    record = tracker.on_incoming(rejected)

    assert record.state is DeliveryState.REJECTED
    assert record.detail == "策略不允许"
    assert record.state.is_terminal


def test_task_error_marks_failed(make_task, addr):
    env = make_task()
    tracker = DeliveryTracker()
    tracker.register(env)
    tracker.mark(env.id, DeliveryState.DELIVERED)

    failure = env.reply(
        type=MessageType.TASK_ERROR,
        payload=TaskErrorPayload(error="pytest 挂了"),
        sender=addr("beta"),
    )

    assert tracker.on_incoming(failure).state is DeliveryState.FAILED


def test_illegal_transition_is_refused(make_task):
    env = make_task()
    tracker = DeliveryTracker()
    record = tracker.register(env)

    with pytest.raises(ProtocolError, match="非法状态迁移"):
        record.transition(DeliveryState.COMPLETED)  # pending 不能直接 completed


def test_terminal_state_ignores_late_receipts(make_task, addr):
    env = make_task()
    tracker = DeliveryTracker()
    tracker.register(env)
    tracker.mark(env.id, DeliveryState.DELIVERED)
    tracker.mark(env.id, DeliveryState.COMPLETED)

    late = env.reply(
        type=MessageType.RECEIPT_ACCEPTED, payload=ReceiptPayload(ref=env.id), sender=addr("beta")
    )

    assert tracker.on_incoming(late).state is DeliveryState.COMPLETED


def test_unknown_reference_is_ignored(make_task, addr):
    tracker = DeliveryTracker()
    stray = make_task()

    receipt = stray.reply(
        type=MessageType.RECEIPT_ACCEPTED, payload=ReceiptPayload(ref=stray.id), sender=addr("beta")
    )

    assert tracker.on_incoming(receipt) is None


def test_non_receipt_message_does_not_move_state(make_task):
    tracker = DeliveryTracker()
    env = make_task()
    tracker.register(env)

    assert tracker.on_incoming(env) is None
    assert tracker.get(env.id).state is DeliveryState.PENDING


def test_records_are_immutable(make_task):
    tracker = DeliveryTracker()
    record = tracker.register(make_task())

    moved = record.transition(DeliveryState.DELIVERED)

    assert record.state is DeliveryState.PENDING
    assert moved.state is DeliveryState.DELIVERED
