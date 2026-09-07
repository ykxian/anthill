"""Phase-1 Context/token hard-gate acceptance tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anthill.agent.context import ContextBuilder
from anthill.agent.handlers import HandlerContext
from anthill.agent.llm_handler import LlmHandler
from anthill.agent.loop import AgentLoop, LoopOutcome, _hard_query_violation
from anthill.agent.runtime import AgentRuntime
from anthill.agent.tools.base import ToolContext
from anthill.agent.tools.registry import build_toolset
from anthill.core.config import SecuritySection
from anthill.core.envelope import Address, Envelope
from anthill.core.evidence import (
    MAX_MODEL_MESSAGE_BYTES,
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    TRUNCATED_WITH_EVIDENCE,
    EvidenceStore,
    restore_evidence_for_signature,
)
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.payloads import ChatPayload, MessageType, TaskRequestPayload
from anthill.providers.base import Msg, Role, ToolCall, Turn
from anthill.providers.fake import FakeProvider
from anthill.security.policy import PolicyEngine, TrustLevel
from anthill.security.signing import sign_envelope, verify_envelope


def large_task(body: str, *, suffix: str = "") -> Envelope:
    return Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="beta"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title=f"100 KiB report{suffix}", body=body),
    )


def test_100_kib_message_is_complete_on_disk_and_only_summary_reaches_model(
    layout, config, mailbox: Mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = "failure_case=test_large_report\n" + "evidence line\n" * 7400
    assert len(report.encode("utf-8")) >= 100 * 1024

    delivered = mailbox.deposit(large_task(report))
    bounded = Mailbox.read_envelope(delivered)
    ref = bounded.payload.details

    assert ref is not None
    assert ref.status == TRUNCATED_WITH_EVIDENCE
    assert len(bounded.payload.body.encode("utf-8")) <= 768
    details_path = layout.workspace / ref.path
    assert details_path.read_text(encoding="utf-8") == report
    record = json.loads(details_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert ref.sha256 == hashlib.sha256(report.encode("utf-8")).hexdigest()
    assert record["sha256"] == ref.sha256
    assert record["owner"] == "testnode:cli"
    assert record["evidence_level"] == "message_body"

    def forbidden_load(self, details):  # type: ignore[no-untyped-def]
        raise AssertionError("model path must not auto-read details")

    monkeypatch.setattr(EvidenceStore, "load", forbidden_load)
    builder = ContextBuilder(
        agent=config.agent("beta"),
        node=config.node.name,
        tools=[],
        evidence_root=layout.details_dir,
        evidence_owner="testnode:beta",
    )
    incoming = builder.incoming(bounded)
    assert len(incoming.content.encode("utf-8")) <= MAX_MODEL_MESSAGE_BYTES
    assert TRUNCATED_WITH_EVIDENCE in incoming.content
    assert ref.path in incoming.content and ref.sha256 in incoming.content
    assert incoming.content.count("evidence line") <= 6


def test_same_100_kib_digest_has_one_details_file_and_one_agent_consumption(
    layout, config, mailbox: Mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = "same digest\n" * 9000
    first = Mailbox.read_envelope(mailbox.deposit(large_task(report, suffix=" one")))
    second = Mailbox.read_envelope(mailbox.deposit(large_task(report, suffix=" two")))

    assert first.id != second.id
    assert first.payload.details is not None and second.payload.details is not None
    assert first.payload.details.sha256 == second.payload.details.sha256
    assert len(list(layout.details_dir.glob("*.txt"))) == 1

    handled: list[str] = []

    class RecordingHandler:
        name = "recording"

        async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
            handled.append(env.id)

    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name="beta",
        handler=RecordingHandler(),  # type: ignore[arg-type]
        log=EventLog(None, agent="beta", echo=False),
    )

    async def no_receipt(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(runtime.sender, "send_receipt", no_receipt)

    async def dispatch() -> None:
        await asyncio.wait_for(runtime._dispatch(first), timeout=2)
        await asyncio.wait_for(runtime._dispatch(second), timeout=2)
        await runtime.aclose()

    asyncio.run(dispatch())
    assert handled == [first.id]

    # A clean runtime/session still sees the persistent digest claim and does
    # not inject the same report again.
    third = Mailbox.read_envelope(mailbox.deposit(large_task(report, suffix=" three")))
    restarted = AgentRuntime(
        layout=layout,
        config=config,
        agent_name="beta",
        handler=RecordingHandler(),  # type: ignore[arg-type]
        log=EventLog(None, agent="beta", echo=False),
    )
    monkeypatch.setattr(restarted.sender, "send_receipt", no_receipt)

    async def dispatch_after_restart() -> None:
        await asyncio.wait_for(restarted._dispatch(third), timeout=2)
        await restarted.aclose()

    asyncio.run(dispatch_after_restart())
    assert handled == [first.id]


@pytest.mark.asyncio
async def test_20_kib_1000_line_tool_result_is_externalized_before_second_model_call(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    blackboard = workspace / ".anthill" / "blackboard"
    workspace.mkdir()
    blackboard.mkdir(parents=True)
    original = "".join(f"failure line {index:04d} payload payload\n" for index in range(1000))
    assert len(original.encode("utf-8")) > 20 * 1024
    (workspace / "tool.log").write_text(original, encoding="utf-8")
    context = ToolContext(
        workspace=workspace,
        blackboard=blackboard,
        security=SecuritySection(),
        thread="01J00000000000000000000000",
    )
    provider = FakeProvider(
        [
            Turn(
                tool_calls=(
                    ToolCall(
                        id="read-1",
                        name="read_file",
                        arguments={"path": "tool.log", "limit": 1000},
                    ),
                )
            ),
            Turn(
                tool_calls=(ToolCall(id="finish-1", name="finish", arguments={"summary": "done"}),)
            ),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=build_toolset(("read_file", "finish")),
        policy=PolicyEngine(context.security),
        tool_ctx=context,
        log=EventLog(None, agent="beta", echo=False),
        trust=TrustLevel.USER,
        max_steps=3,
        token_budget=100_000,
        evidence_owner="testnode:beta",
    )

    await loop.run([Msg.user("read the log")])

    tool_message = next(
        message for message in provider.calls[1].messages if message.role is Role.TOOL
    )
    assert TRUNCATED_WITH_EVIDENCE in tool_message.content
    assert len(tool_message.content.encode("utf-8")) <= MAX_TOOL_RESULT_BYTES
    assert len(tool_message.content.splitlines()) <= MAX_TOOL_RESULT_LINES
    ref_path = next(
        line.split(": ", 1)[1]
        for line in tool_message.content.splitlines()
        if line.startswith("details:")
    )
    details_path = workspace / ref_path
    assert details_path.read_text(encoding="utf-8") == original.rstrip("\n")
    record = json.loads(details_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert record["sha256"] == hashlib.sha256(original.rstrip("\n").encode()).hexdigest()


def test_secret_bearing_large_message_is_rejected_without_plaintext_details(
    layout, mailbox: Mailbox
) -> None:
    secret = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    env = Envelope.new(
        sender=Address(node="testnode", agent="cli"),
        recipient=Address(node="testnode", agent="beta"),
        type=MessageType.CHAT,
        payload=ChatPayload(body=(secret + "\n") * 200),
    )

    with pytest.raises(Exception, match="secret/token/cookie"):
        mailbox.deposit(env)
    assert not list(layout.details_dir.glob("*.txt"))
    assert mailbox.list_new() == []


def test_signed_large_envelope_is_reconstructed_only_for_hmac_verification(
    mailbox: Mailbox,
) -> None:
    key = b"test-only-signing-key"
    original = large_task("signed evidence\n" * 9000)
    signed = sign_envelope(original, key)
    bounded = Mailbox.read_envelope(mailbox.deposit(signed))

    assert bounded.payload.details is not None
    restored = restore_evidence_for_signature(bounded, mailbox.evidence_store)
    assert restored.payload.body == original.payload.body
    assert restored.payload.details is None
    verify_envelope(restored, key, max_skew=None)


@pytest.mark.asyncio
async def test_agent_result_is_at_most_40_lines_with_complete_evidence(layout, config) -> None:
    sent: list[Envelope] = []

    class CapturingSender:
        async def send(self, env: Envelope):  # type: ignore[no-untyped-def]
            sent.append(env)
            return ()

    handler = LlmHandler(
        provider=FakeProvider([Turn(text="unused")]),
        tools=[],
        policy=PolicyEngine(config.security),
        builder=ContextBuilder(agent=config.agent("beta"), node=config.node.name, tools=[]),
        max_steps=1,
        token_budget=1000,
    )
    source = large_task("small request")
    ctx = HandlerContext(
        identity=Address(node="testnode", agent="beta"),
        agent=config.agent("beta"),
        sender=CapturingSender(),  # type: ignore[arg-type]
        layout=layout,
        config=config,
        log=EventLog(None, agent="beta", echo=False),
    )
    original = "".join(f"result line {line}\n" for line in range(100))
    await handler._reply_result(
        source,
        ctx,
        LoopOutcome(summary=original, finished=True),
    )

    assert len(sent) == 1
    result = sent[0].payload
    assert len(result.summary.splitlines()) <= 40
    assert result.details is not None
    assert TRUNCATED_WITH_EVIDENCE in result.summary
    assert (layout.workspace / result.details.path).read_text(encoding="utf-8") == original


def test_terminal_mailbox_entries_do_not_return_to_new_after_recovery(
    mailbox: Mailbox, make_task
) -> None:
    done_env = make_task()
    superseded_env = make_task()
    expired_env = make_task().model_copy(
        update={"expires_at": datetime.now(tz=UTC) - timedelta(seconds=1)}
    )

    done_claim = mailbox.claim(mailbox.deposit(done_env))
    mailbox.archive(done_claim)
    superseded_claim = mailbox.claim(mailbox.deposit(superseded_env))
    mailbox.archive_terminal(superseded_claim, "superseded")
    mailbox.claim(mailbox.deposit(expired_env))

    assert mailbox.recover_stale() == []
    assert mailbox.list_new() == []
    assert any(path.stem == done_env.id for path in mailbox.done.rglob("*.json"))
    assert (mailbox.superseded / f"{superseded_env.id}.json").is_file()
    assert (mailbox.expired / f"{expired_env.id}.json").is_file()


@pytest.mark.asyncio
async def test_empty_queue_for_ten_simulated_minutes_causes_zero_model_calls(
    layout, config
) -> None:
    model_calls: list[str] = []

    class CountingModelHandler:
        name = "counting-model"

        async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
            model_calls.append(env.id)

    runtime = AgentRuntime(
        layout=layout,
        config=config,
        agent_name="beta",
        handler=CountingModelHandler(),  # type: ignore[arg-type]
        log=EventLog(None, agent="beta", echo=False),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    await asyncio.wait_for(runtime.ready.wait(), timeout=2)
    for _minute in range(10):
        # Advance every maintenance boundary without depositing a queue item.
        runtime._sweep()
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert model_calls == []


def test_representative_duplicate_report_reduces_injected_bytes_and_wakes(
    layout, config, mailbox: Mailbox
) -> None:
    report = "representative report\n" * 5000
    envs = [
        Mailbox.read_envelope(mailbox.deposit(large_task(report, suffix=str(index))))
        for index in range(5)
    ]
    builder = ContextBuilder(
        agent=config.agent("beta"),
        node=config.node.name,
        tools=[],
        evidence_root=layout.details_dir,
        evidence_owner="testnode:beta",
    )
    after_bytes = len(builder.incoming(envs[0]).content.encode("utf-8"))
    before_bytes = len(report.encode("utf-8")) * len(envs)
    after_wakes = sum(mailbox.claim_evidence_digest(env) for env in envs)

    assert after_wakes == 1
    assert after_bytes < before_bytes * 0.01
    assert len(list(layout.details_dir.glob("*.txt"))) == 1


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff HEAD",
        "docker ps",
        "docker compose ps",
        "docker logs service",
        "docker compose logs --tail 20 service",
        "journalctl -n 20",
    ],
)
def test_context_amplifying_shell_queries_are_rejected_before_execution(command: str) -> None:
    call = ToolCall(id="broad-query", name="run_shell", arguments={"command": command})

    violation = _hard_query_violation(call)

    assert violation is not None


@pytest.mark.parametrize(
    "command",
    [
        "git status -- anthill/core/mailbox.py",
        "git diff HEAD -- anthill/core/mailbox.py",
        "docker ps --filter name=anthill",
        "docker compose ps bridge",
        "docker logs --since 10m --tail 100 bridge",
        "docker compose logs --since=10m --tail=100 bridge",
        "journalctl --since 10m -n 100",
    ],
)
def test_scoped_shell_queries_pass_the_hard_shape_gate(command: str) -> None:
    call = ToolCall(id="scoped-query", name="run_shell", arguments={"command": command})

    assert _hard_query_violation(call) is None


@pytest.mark.asyncio
async def test_agent_loop_returns_scope_error_without_running_broad_shell_query(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    blackboard = workspace / ".anthill" / "blackboard"
    workspace.mkdir()
    blackboard.mkdir(parents=True)
    context = ToolContext(
        workspace=workspace,
        blackboard=blackboard,
        security=SecuritySection(),
        thread="01J00000000000000000000000",
    )
    provider = FakeProvider(
        [
            Turn(
                tool_calls=(
                    ToolCall(
                        id="broad-query",
                        name="run_shell",
                        arguments={"command": "git status"},
                    ),
                )
            ),
            Turn(
                tool_calls=(
                    ToolCall(id="finish-after-reject", name="finish", arguments={"summary": "ok"}),
                )
            ),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        tools=build_toolset(("run_shell", "finish")),
        policy=PolicyEngine(context.security),
        tool_ctx=context,
        log=EventLog(None, agent="beta", echo=False),
        trust=TrustLevel.USER,
        max_steps=3,
        token_budget=100_000,
    )

    await loop.run([Msg.user("inspect")])

    tool_message = next(
        message for message in provider.calls[1].messages if message.role is Role.TOOL
    )
    assert "HARD_QUERY_SCOPE_REQUIRED" in tool_message.content
    assert not (workspace / ".git").exists()
