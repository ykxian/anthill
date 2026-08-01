"""终端确认流：没有 tty 就没有确认者，超时按拒绝处理。"""

from __future__ import annotations

import pytest

from anthill.security import confirm as confirm_module
from anthill.security.confirm import terminal_confirmer


def test_no_confirmer_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        confirm_module.sys, "stdin", type("X", (), {"isatty": lambda self: False})()
    )

    assert terminal_confirmer() is None


async def test_confirmer_returns_user_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setattr(confirm_module.sys, "stdin", type("X", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(confirm_module.Confirm, "ask", staticmethod(lambda *a, **k: True))
    ask = terminal_confirmer()
    assert ask is not None

    # Act / Assert
    assert await ask("要跑 rm -rf build 吗？")


async def test_confirmer_treats_timeout_as_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange：把超时压到极小，模拟「人不在电脑前」
    import time

    monkeypatch.setattr(confirm_module.sys, "stdin", type("X", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(confirm_module, "CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(
        confirm_module.Confirm, "ask", staticmethod(lambda *a, **k: time.sleep(1) or True)
    )
    ask = terminal_confirmer()
    assert ask is not None

    # Act / Assert
    assert not await ask("危险操作")
