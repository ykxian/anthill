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
    last_claim,
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

    assert read_claim(layout, "cc1") is None, "死掉的会话该把 Agent 让出来"
    # 让出来 ≠ 下一个会话就该拿它 —— 没人用过的优先（别去碰别人的历史）。
    # 真要接手就显式指定。
    assert pick_agent(layout, config, "cc1") == "cc1"


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


# ---------- 重启之后别认错人 ----------


def bind(layout: NodeLayout, config: Config, cwd: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """装成「某个目录里的一个会话起来了，认领完又退出了」。"""
    monkeypatch.chdir(cwd)
    name = pick_agent(layout, config)
    claim(layout, name)
    release(layout, name)
    return name


def test_a_session_gets_the_same_agent_back_after_a_restart(
    node: tuple[NodeLayout, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这是自动认领最危险的地方。**

    A 本来是 cc1、B 是 cc2；两个都重启一次、顺序反过来，纯粹「挑第一个空的」
    会变成 A→cc2、B→cc1。而上下文是挂在 Agent 上的（邮箱、thread、
    别人对「cc1 说过什么」的记忆）—— 认错人等于串了历史。

    pid 每次都变，靠不住；**工作目录**跨重启是稳的。
    """
    layout, config = node
    a, b = tmp_path / "projA", tmp_path / "projB"
    for path in (a, b):
        path.mkdir()

    first = {"A": bind(layout, config, a, monkeypatch), "B": bind(layout, config, b, monkeypatch)}
    # 反着重启
    again = {"B": bind(layout, config, b, monkeypatch), "A": bind(layout, config, a, monkeypatch)}

    assert first["A"] != first["B"], "两个目录该拿到不同的 Agent"
    assert again == first, f"重启后对应关系变了：{first} -> {again}"


def test_a_second_session_does_not_steal_the_first_ones_agent(
    node: tuple[NodeLayout, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先来的那个用完退出之后，后来的**不该顺手接管它** —— 那样每开一次就洗一次牌，
    而且直接读到了别人的会话历史。没人用过的优先。"""
    layout, config = node
    a, b = tmp_path / "projA", tmp_path / "projB"
    for path in (a, b):
        path.mkdir()

    mine = bind(layout, config, a, monkeypatch)
    theirs = bind(layout, config, b, monkeypatch)

    assert mine != theirs


def test_affinity_survives_even_when_the_agent_is_free(
    node: tuple[NodeLayout, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """松开认领**不能把记录删掉** —— 删了就等于每次重启重新洗牌。"""
    layout, config = node
    a = tmp_path / "projA"
    a.mkdir()

    name = bind(layout, config, a, monkeypatch)

    assert read_claim(layout, name) is None, "退出之后该是空闲的"
    assert last_claim(layout, name) is not None, "但得记得上次是谁"
    assert last_claim(layout, name).cwd == str(a)


def test_running_out_of_virgin_agents_falls_back_to_reuse(
    node: tuple[NodeLayout, Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有没人用过的了，才去接别人用过的 —— 这时候串上下文是不可避免的取舍，
    但至少它是最后一档，不是第一档。"""
    layout, config = node
    for name in ("projA", "projB", "projC"):
        (tmp_path / name).mkdir()
    bind(layout, config, tmp_path / "projA", monkeypatch)
    bind(layout, config, tmp_path / "projB", monkeypatch)

    third = bind(layout, config, tmp_path / "projC", monkeypatch)

    assert third in {"cc1", "cc2"}  # 只能接一个用过的，但不该抛


# ---------- 钉死某一个 ----------


def test_pinning_never_silently_steals_a_live_session(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ANTHILL_AGENT=cc2` 指到一个**还活着**的会话头上时，该报错，不该悄悄顶掉它。

    抢了就是两个会话同时是 cc2：读同一个收件箱、抢同一批消息，
    而各自的上下文完全不同 —— 那正好毁掉「一一对应」这件事本身。
    """
    layout, config = node
    path = layout.agent_dir("cc2") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid": {os.getppid()}, "cwd": "/别人那儿", "since": ""}}', encoding="utf-8")
    monkeypatch.setenv(AGENT_ENV, "cc2")

    # 挑得出来（是个合法的桥接 Agent），但认领这一步会拦下
    assert pick_agent(layout, config) == "cc2"
    with pytest.raises(Exception, match="占着"):
        claim(layout, "cc2")


def test_taking_over_is_possible_but_has_to_be_explicit(
    node: tuple[NodeLayout, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一个会话真卡死了得有条出路 —— 但那必须是个显式动作。"""
    layout, _ = node
    path = layout.agent_dir("cc2") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid": {os.getppid()}, "cwd": "/别人那儿", "since": ""}}', encoding="utf-8")
    monkeypatch.setenv("ANTHILL_TAKEOVER", "1")

    assert claim(layout, "cc2").pid == os.getpid()


def test_the_refusal_says_all_three_ways_out(
    node: tuple[NodeLayout, Config],
) -> None:
    layout, _ = node
    path = layout.agent_dir("cc2") / "bridge" / "claim.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid": {os.getppid()}, "cwd": "/x", "since": ""}}', encoding="utf-8")

    with pytest.raises(Exception) as caught:
        claim(layout, "cc2")

    message = str(caught.value)
    assert "ANTHILL_AGENT" in message  # 换一个
    assert "再建一个" in message  # 加一个
    assert "ANTHILL_TAKEOVER" in message  # 接管


# ---------- 等新消息：别被「已经回过草稿」的待办叫醒 ----------


def test_wait_skips_messages_that_already_have_a_draft(tmp_path: Path) -> None:
    """`--reply` 完立刻挂 `--wait` 的竞态：草稿还没被 tick 送出、待办还在
    inbox 里，wait 一启动就退出 —— 白醒一轮，还得再挂一次。
    有同名回复草稿的待办已经是「处理过的」，不该再当新消息叫醒人。
    """
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (inbox / "01A.md").write_text("找你", encoding="utf-8")
    (outbox / "01A.md").write_text("回过了", encoding="utf-8")

    assert wait_for_message(inbox, timeout=1.0, drafted=outbox) == []


def test_wait_still_wakes_for_a_message_without_a_draft(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (inbox / "01A.md").write_text("回过", encoding="utf-8")
    (outbox / "01A.md").write_text("草稿", encoding="utf-8")
    (inbox / "01B.md").write_text("新的", encoding="utf-8")

    found = wait_for_message(inbox, timeout=1.0, drafted=outbox)

    assert [p.name for p in found] == ["01B.md"]


def test_wait_sees_the_message_again_after_its_draft_fails(tmp_path: Path) -> None:
    """发送失败时 tick 会把草稿改名挪进 done/（.failed）—— outbox 里没了，
    跳过条件自动失效，那条待办重新可见。**这是这个设计安全的前提**：
    任何失败路径若让原名草稿滞留 outbox，待办就被永久跳过、消息静默失踪。
    """
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (inbox / "01A.md").write_text("找你", encoding="utf-8")
    (outbox / "01A.md").write_text("要发的", encoding="utf-8")
    assert wait_for_message(inbox, timeout=1.0, drafted=outbox) == []

    (outbox / "01A.md").unlink()  # tick 失败路径：_archive(suffix=".failed") 挪走原文件

    found = wait_for_message(inbox, timeout=1.0, drafted=outbox)
    assert [p.name for p in found] == ["01A.md"]
