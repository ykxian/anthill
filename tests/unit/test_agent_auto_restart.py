"""serve 对旧 agentd 的自动、保守重启。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from anthill.cli import serve_cmd
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace
from anthill.web.agents import runtime_path
from anthill.web.context import NodeContext, NodeRegistry


def registry(tmp_path: Path) -> tuple[NodeRegistry, NodeContext]:
    layout = NodeLayout(tmp_path / "ws")
    config = create_workspace(layout, node_name="box")
    ctx = NodeContext(layout, config)
    return NodeRegistry([ctx]), ctx


@pytest.mark.asyncio
async def test_only_running_stale_agents_are_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes, ctx = registry(tmp_path)
    status = runtime_path(ctx.layout, "echo")
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(
        json.dumps({"pid": 41, "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    fresh = runtime_path(ctx.layout, "coordinator")
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_text(
        json.dumps({"pid": 42, "started_at": "2999-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    picked: list[str] = []

    def fake_pid(_layout: NodeLayout, name: str) -> int | None:
        return {"echo": 41, "coordinator": 42}.get(name)

    async def fake_restart(
        _ctx: NodeContext,
        name: str,
        _log: EventLog,
        *,
        exit_poll: float,
    ) -> bool:
        picked.append(name)
        return True

    monkeypatch.setattr(serve_cmd, "running_pid", fake_pid)
    monkeypatch.setattr(serve_cmd, "_restart_stale_agent", fake_restart)

    count = await serve_cmd._restart_stale_agents(
        nodes,
        EventLog(None, echo=False),
        code_mtime=time.time(),
        exit_poll=0,
    )

    assert count == 1
    assert picked == ["echo"]


@pytest.mark.asyncio
async def test_restart_waits_for_current_message_then_stops_and_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ctx = registry(tmp_path)
    current = ctx.layout.mailbox_dir("echo") / "inbox" / "cur"
    current.mkdir(parents=True, exist_ok=True)
    in_flight = current / "message.json"
    in_flight.write_text("{}", encoding="utf-8")
    pid: int | None = 41
    events: list[str] = []

    def fake_pid(_layout: NodeLayout, _name: str) -> int | None:
        return pid

    def fake_stop(_layout: NodeLayout, _name: str) -> dict[str, Any]:
        nonlocal pid
        events.append("stop")
        pid = None
        return {"already": False, "pid": 41}

    def fake_start(_layout: NodeLayout, _config: Any, _name: str) -> dict[str, Any]:
        nonlocal pid
        events.append("start")
        pid = 42
        return {"already": False, "pid": 42}

    real_sleep = asyncio.sleep

    async def finish_message(_seconds: float) -> None:
        events.append("wait")
        in_flight.unlink()
        await real_sleep(0)

    monkeypatch.setattr(serve_cmd, "running_pid", fake_pid)
    monkeypatch.setattr(serve_cmd, "stop_agent", fake_stop)
    monkeypatch.setattr(serve_cmd, "start_agent", fake_start)
    monkeypatch.setattr(asyncio, "sleep", finish_message)

    changed = await serve_cmd._restart_stale_agent(
        ctx,
        "echo",
        EventLog(None, echo=False),
        exit_poll=0,
    )

    assert changed is True
    assert events == ["wait", "stop", "start"]
    assert pid == 42


@pytest.mark.asyncio
async def test_an_external_replacement_is_not_started_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ctx = registry(tmp_path)
    pids = iter((41, 41, 41, 42, 42))
    starts = 0

    def fake_pid(_layout: NodeLayout, _name: str) -> int | None:
        return next(pids)

    def fake_stop(_layout: NodeLayout, _name: str) -> dict[str, Any]:
        return {"already": False, "pid": 41}

    def fake_start(_layout: NodeLayout, _config: Any, _name: str) -> dict[str, Any]:
        nonlocal starts
        starts += 1
        return {"already": False, "pid": 43}

    monkeypatch.setattr(serve_cmd, "running_pid", fake_pid)
    monkeypatch.setattr(serve_cmd, "stop_agent", fake_stop)
    monkeypatch.setattr(serve_cmd, "start_agent", fake_start)

    changed = await serve_cmd._restart_stale_agent(
        ctx,
        "echo",
        EventLog(None, echo=False),
        exit_poll=0,
    )

    assert changed is False
    assert starts == 0
