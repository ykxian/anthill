"""一个终端会话怎么和一个桥接 Agent 一一对应，以及「一直盯着」。

用户点出的三条缺陷：

1. 粘那句话「还不如之前的 monitor 方法，那个还能一直监控」；
2. hook / MCP「只能一个目录下一个终端」；
3. 「MCP 之后不能自动监控」。

根子是两个：Claude Code 的配置粒度是**目录**（表达不出「同目录下谁对应谁」），
而 MCP 与 hook 都是**拉取式**的（会话闲着时没人叫醒它）。
所以补两样：能穿透到单个会话的绑定，和一个会阻塞的等待。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from anthill.adapters.bridge_session import (
    AGENT_ENV,
    claim,
    pick_agent,
    read_claim,
    release,
    wait_for_message,
)
from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace

TWO_BRIDGES = """
[agents.cc1]
role = "worker"
bridge = true

[agents.cc2]
role = "worker"
bridge = true
"""


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="box")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8") + TWO_BRIDGES, encoding="utf-8"
    )
    return layout, Config.load_from(layout)


# ---------- 一一对应 ----------


def test_two_sessions_claim_different_agents(node: tuple[NodeLayout, Config]) -> None:
    """**同一个目录、同一份配置**，两个会话必须各拿一个。

    这正是「只能一个目录下一个终端」那条缺陷：配置文件里写死 Agent 名的话，
    那个目录下开几个会话就有几个抢同一个。
    """
    layout, config = node

    first = pick_agent(layout, config)
    claim(layout, first)
    second = pick_agent(layout, config)

    assert first != second, "两个会话认领了同一个 Agent"
    assert {first, second} == {"cc1", "cc2"}


def test_an_environment_variable_pins_a_specific_agent(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """能穿透到单个会话的只有环境变量（子进程继承）—— 想钉死就用它。"""
    layout, config = node
    monkeypatch.setenv(AGENT_ENV, "cc2")

    assert pick_agent(layout, config) == "cc2"


def test_pinning_something_that_is_not_a_bridge_is_refused(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config = node
    monkeypatch.setenv(AGENT_ENV, "echo")

    with pytest.raises(Exception, match="不是桥接"):
        pick_agent(layout, config)


def test_a_dead_session_releases_its_agent(node: tuple[NodeLayout, Config]) -> None:
    """认领跟着**进程**走：会话关了就自动空出来，不用手动释放、不用超时。"""
    layout, config = node
    path = layout.agent_dir("cc1") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 一个几乎不可能存在的 pid —— 装成「上一个会话已经没了」
    path.write_text('{"pid": 2147483646, "cwd": "/gone", "since": ""}', encoding="utf-8")

    assert read_claim(layout, "cc1") is None
    assert pick_agent(layout, config) == "cc1", "死掉的会话该把 Agent 让出来"


def test_claiming_something_another_live_session_holds_is_refused(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, _ = node
    path = layout.agent_dir("cc1") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid": {os.getppid()}, "cwd": "/x", "since": ""}}', encoding="utf-8")

    with pytest.raises(Exception, match="占着"):
        claim(layout, "cc1")


def test_releasing_only_touches_your_own_claim(node: tuple[NodeLayout, Config]) -> None:
    """别把别人的会话踢下线。"""
    layout, _ = node
    path = layout.agent_dir("cc1") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid": {os.getppid()}, "cwd": "/x", "since": ""}}', encoding="utf-8")

    assert release(layout, "cc1") is False
    assert path.is_file()


def test_running_out_of_agents_says_what_to_do(node: tuple[NodeLayout, Config]) -> None:
    layout, config = node
    for name in ("cc1", "cc2"):
        claim(layout, name)

    with pytest.raises(Exception, match="再建一个"):
        pick_agent(layout, config)


# ---------- 一直盯着 ----------


def test_waiting_blocks_until_a_message_lands(tmp_path: Path) -> None:
    """MCP 与 hook 都是拉取式的，会话闲着时没有任何东西会把它叫醒。
    这个阻塞调用就是「一直盯着」缺的那一块。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def drop_later() -> None:
        time.sleep(0.6)
        (inbox / "01AAA.md").write_text("有人找你", encoding="utf-8")

    threading.Thread(target=drop_later, daemon=True).start()
    started = time.monotonic()
    found = wait_for_message(inbox, timeout=10)
    waited = time.monotonic() - started

    assert [p.name for p in found] == ["01AAA.md"]
    assert 0.4 < waited < 5, f"该等到消息才返回，实际等了 {waited:.1f}s"


def test_waiting_times_out_instead_of_hanging_forever(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    started = time.monotonic()
    found = wait_for_message(inbox, timeout=1.0)

    assert found == []
    assert time.monotonic() - started < 5


def test_already_seen_messages_do_not_end_the_wait(tmp_path: Path) -> None:
    """「等新消息」不能被收件箱里早就躺着的那些立刻满足 —— 那样循环会空转。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "01OLD.md").write_text("旧的", encoding="utf-8")

    found = wait_for_message(inbox, timeout=1.0, known={"01OLD.md"})

    assert found == []


def test_a_missing_inbox_does_not_crash_the_loop(tmp_path: Path) -> None:
    assert wait_for_message(tmp_path / "从来没有过", timeout=1.0) == []
