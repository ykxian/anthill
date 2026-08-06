"""共用测试夹具：一个临时工作区 + 三个 Agent（alpha / beta / cli）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskRequestPayload

NODE_TOML = """
[node]
name = "testnode"
workspace = "."

[runtime]
poll_interval = 0.05
watch_mode = "poll"

[agents.cli]
role = "user"

[agents.alpha]
role = "coordinator"

[agents.beta]
role = "worker"

[agents.gamma]
role = "worker"
"""


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**没有一条测试该写进开发者真正的 `~/.anthill/`。**

    那个目录里有机器级工作区清单、面板令牌、密钥库。这个坑踩过两次了：
    先是浏览器测试（起子进程，`Path.home` 的 monkeypatch 拦不住），
    后来 `anthill init` 改成会登记工作区，于是整个测试套件把用户的清单刷成了
    二十几条 `/tmp/pytest-of-.../test_xxx0`。

    两样都换掉：`Path.home()` 管本进程，`HOME` 管它 spawn 出去的子进程。
    autouse，所以**新写的测试不用记得这件事**——这才是真的防住。
    """
    # **放在 tmp_path 外面**：不少测试会列 tmp_path 的内容
    # （比如目录浏览器那几条），家目录混在里面会把断言弄脏。
    home = tmp_path.parent / "fake-homes" / tmp_path.name
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def layout(tmp_path: Path) -> NodeLayout:
    node = NodeLayout(tmp_path).ensure_base()
    node.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "alpha", "beta", "gamma"):
        Mailbox(node.mailbox_dir(name)).ensure()
    return node


@pytest.fixture
def config(layout: NodeLayout) -> Config:
    return Config.load_from(layout)


@pytest.fixture
def mailbox(layout: NodeLayout) -> Mailbox:
    return Mailbox(layout.mailbox_dir("beta"))


@pytest.fixture
def addr() -> Callable[[str], Address]:
    def make(agent: str, node: str = "testnode") -> Address:
        return Address(node=node, agent=agent)

    return make


@pytest.fixture
def make_task(addr: Callable[[str], Address]) -> Callable[..., Envelope]:
    def make(sender: str = "alpha", recipient: str = "beta", **kwargs: object) -> Envelope:
        return Envelope.new(
            sender=addr(sender),
            recipient=addr(recipient),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="测试任务", body="正文"),
            **kwargs,  # type: ignore[arg-type]
        )

    return make
