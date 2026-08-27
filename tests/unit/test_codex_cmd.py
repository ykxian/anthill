"""``anthill codex`` 的 attach 参数与 active-writer 判定。"""

from __future__ import annotations

import pytest

from anthill.adapters.codex_app_server import CodexRpcError
from anthill.cli.codex_cmd import (
    _reject_attach_tui_options,
    _tui_options,
    is_active_writer_error,
)
from anthill.core.errors import AntHillError


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
