"""文件式审批：远端停下来等人点头，超时按拒绝处理。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.core.ids import new_id
from anthill.security.approvals import (
    ApprovalRequest,
    ApprovalStore,
    approval_confirmer,
)

PROMPT = "允许执行 run_shell（风险 high）？\n  rm -rf build"


def make_request(agent: str = "runner") -> ApprovalRequest:
    return ApprovalRequest.create(agent=agent, prompt=PROMPT)


# ---------- 存取 ----------


def test_submitted_request_shows_up_as_pending(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = make_request()

    store.submit(request)

    pending = store.pending()
    assert [r.id for r in pending] == [request.id]
    assert pending[0].prompt == PROMPT


def test_answered_request_is_no_longer_pending(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = make_request()
    store.submit(request)

    store.answer(request.id, approved=True, by="me")

    assert store.pending() == []
    answer = store.answer_of(request.id)
    assert answer is not None and answer.approved and answer.by == "me"


def test_close_removes_both_files(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = make_request()
    store.submit(request)
    store.answer(request.id, approved=False)

    store.close(request.id)

    assert not store.request_path(request.id).exists()
    assert not store.answer_path(request.id).exists()


def test_pending_is_empty_before_anything_happens(tmp_path: Path) -> None:
    assert ApprovalStore(tmp_path).pending() == []


def test_corrupt_request_file_is_skipped(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / f"{new_id()}.json").write_text("{坏", encoding="utf-8")

    assert store.pending() == []  # 坏文件不该让整个列表打不开


def test_half_written_answer_reads_as_not_answered_yet(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    request = make_request()
    store.submit(request)
    store.answer_path(request.id).write_text("{半个", encoding="utf-8")

    assert store.answer_of(request.id) is None
    assert [r.id for r in store.pending()] == [request.id]


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_request_id_must_be_a_ulid(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="审批"):
        ApprovalStore(tmp_path).request_path(bad)


# ---------- 等待与确认 ----------


async def test_confirmer_returns_true_after_someone_approves(tmp_path: Path) -> None:
    # Arrange：agentd 那一侧在等
    store = ApprovalStore(tmp_path)
    confirm = approval_confirmer(store, agent="runner", timeout=5.0, poll=0.02)
    task = asyncio.create_task(confirm(PROMPT))

    # Act：另一边（人）批了
    async def approve() -> None:
        for _ in range(200):
            pending = store.pending()
            if pending:
                store.answer(pending[0].id, approved=True, by="human")
                return
            await asyncio.sleep(0.01)

    await approve()

    # Assert
    assert await asyncio.wait_for(task, timeout=5)


async def test_confirmer_returns_false_when_refused(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    confirm = approval_confirmer(store, agent="runner", timeout=5.0, poll=0.02)
    task = asyncio.create_task(confirm(PROMPT))

    for _ in range(200):
        pending = store.pending()
        if pending:
            store.answer(pending[0].id, approved=False, by="human")
            break
        await asyncio.sleep(0.01)

    assert not await asyncio.wait_for(task, timeout=5)


async def test_timeout_counts_as_refusal(tmp_path: Path) -> None:
    """没人管的危险操作，默认不做。"""
    store = ApprovalStore(tmp_path)
    confirm = approval_confirmer(store, agent="runner", timeout=0.1, poll=0.02)

    assert not await confirm(PROMPT)


async def test_request_is_cleaned_up_even_on_timeout(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    confirm = approval_confirmer(store, agent="runner", timeout=0.1, poll=0.02)

    await confirm(PROMPT)

    assert store.pending() == []  # 不留垃圾在目录里


async def test_the_prompt_reaches_the_human_verbatim(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path)
    confirm = approval_confirmer(store, agent="runner", timeout=1.0, poll=0.02)
    task = asyncio.create_task(confirm(PROMPT))
    await asyncio.sleep(0.05)

    seen = store.pending()

    assert seen and seen[0].prompt == PROMPT
    assert seen[0].agent == "runner"
    await task


# ---------- 审批 id 必须是 ULID（复查时发现）----------


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "a/b", "", "不是ULID"])
def test_request_with_a_non_ulid_id_is_rejected_by_the_model(bad: str) -> None:
    """这个模型是从**远端机器**读来的（`anthill approve --peer`）。

    id 会被拼进文件路径，不校验的话被攻陷的远端就能指使我们往任意路径写文件。
    """
    with pytest.raises(ValueError, match="审批 id"):
        ApprovalRequest.model_validate({"id": bad, "agent": "runner", "prompt": "rm -rf /"})
