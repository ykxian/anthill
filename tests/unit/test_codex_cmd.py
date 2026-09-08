"""``anthill codex`` 的 attach 参数与 active-writer 判定。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from anthill.adapters.codex_app_server import CodexAppServerError, CodexRpcError
from anthill.cli import codex_cmd
from anthill.cli.codex_cmd import (
    _reject_attach_tui_options,
    _start_embedded_runtime,
    _stop_when_owner_exits,
    _tui_options,
    is_active_writer_error,
    run_codex_queue_session,
)
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.process_lock import locked_owner
from anthill.core.workspace import create_workspace


def bridge_node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="box")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.codex-t3]\nrole = "worker"\nbridge = true\n',
        encoding="utf-8",
    )
    return layout, Config.load_from(layout)


def test_only_thread_resume_active_writer_error_triggers_queue_fallback() -> None:
    active = CodexRpcError(
        "thread/resume",
        {"code": -32600, "message": "thread abc already has an active writer"},
    )
    other_method = CodexRpcError(
        "thread/read",
        {"code": -32600, "message": "thread abc already has an active writer"},
    )
    other_error = CodexRpcError("thread/resume", {"code": -32602, "message": "unknown thread abc"})

    assert is_active_writer_error(active)
    assert not is_active_writer_error(other_method)
    assert not is_active_writer_error(other_error)


def test_attach_rejects_options_owned_by_the_existing_foreground() -> None:
    options = _tui_options(
        model="gpt-test",
        profile="",
        sandbox="",
        approval="",
        approve_for_me=False,
        yolo=False,
        search=False,
        no_alt_screen=False,
    )

    with pytest.raises(AntHillError, match="现有前台决定"):
        _reject_attach_tui_options(options)

    _reject_attach_tui_options([])


def test_yolo_maps_to_codex_dangerous_bypass_flag() -> None:
    options = _tui_options(
        model="",
        profile="",
        sandbox="",
        approval="",
        approve_for_me=False,
        yolo=True,
        search=False,
        no_alt_screen=False,
    )

    assert options == ["--dangerously-bypass-approvals-and-sandbox"]


@pytest.mark.asyncio
async def test_codex_session_owns_and_releases_its_embedded_agentd(tmp_path: Path) -> None:
    layout, config = bridge_node(tmp_path)
    stop = asyncio.Event()

    runtime = await _start_embedded_runtime(
        layout=layout, config=config, agent="codex-t3", stop=stop
    )
    assert locked_owner(layout.agent_dir("codex-t3") / "agentd.lock") == os.getpid()

    stop.set()
    await asyncio.wait_for(runtime, timeout=2)

    assert locked_owner(layout.agent_dir("codex-t3") / "agentd.lock") is None


@pytest.mark.asyncio
async def test_attach_startup_failure_also_releases_the_embedded_agentd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config = bridge_node(tmp_path)
    lock = layout.agent_dir("codex-t3") / "agentd.lock"

    async def fail_after_runtime_started(*_args: object, **_kwargs: object) -> None:
        assert locked_owner(lock) == os.getpid()
        raise CodexAppServerError("queue probe failed")

    monkeypatch.setattr(codex_cmd, "_require_codex_queue", fail_after_runtime_started)
    with pytest.raises(CodexAppServerError, match="queue probe failed"):
        await run_codex_queue_session(
            layout=layout,
            config=config,
            agent="codex-t3",
            thread_id="thread-1",
            codex=sys.executable,
        )

    assert locked_owner(lock) is None


@pytest.mark.asyncio
async def test_current_attach_stops_when_its_codex_owner_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alive = iter((True, False))
    monkeypatch.setattr(codex_cmd, "process_alive", lambda _pid: next(alive))
    stop = asyncio.Event()

    await _stop_when_owner_exits(4242, stop, interval=0)

    assert stop.is_set()
