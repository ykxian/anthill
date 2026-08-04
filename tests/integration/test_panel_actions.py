"""面板的写入口：发起任务、发消息、改配置。

写权限 ≈ 在这台机器上执行命令（能改配置就能加一个带 run_shell 的 Agent），
所以这里的重点全在**边界**：默认不开、非本机拒绝、配置不合法一个字都不写。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anthill.cli.main import app as cli_app
from anthill.core.config import Config
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType
from anthill.discovery.registry import PeerRegistry
from anthill.web.actions import CONFIG_BACKUP, is_local_client
from anthill.web.app import create_app

NODE_TOML = """
[node]
name = "laptop"
workspace = "."

[agents.cli]
role = "user"

[agents.boss]
role = "coordinator"

[agents.coder]
role = "worker"
"""


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config, PeerRegistry]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "boss", "coder"):
        Mailbox(layout.mailbox_dir(name)).ensure()
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


def client_for(
    node: tuple[NodeLayout, Config, PeerRegistry], *, writable: bool = True
) -> httpx.AsyncClient:
    layout, config, peers = node
    application = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=writable,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("127.0.0.1", 5555)),
        base_url="http://panel.test",
    )


def inbox(layout: NodeLayout, agent: str) -> list[MessageType]:
    box = Mailbox(layout.mailbox_dir(agent))
    return [Mailbox.read_envelope(p).type for p in box.list_new()]


# ---------- 发起任务 ----------


async def test_starting_a_run_reaches_the_coordinator(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    # Arrange
    layout, _, _ = node

    # Act
    async with client_for(node) as client:
        response = await client.post("/panel/api/run", json={"task": "给 date.py 补单测"})

    # Assert：和 `anthill run` 走同一条路，任务落进 coordinator 的邮箱
    assert response.status_code == 202
    assert response.json()["to"] == "boss"
    assert inbox(layout, "boss") == [MessageType.TASK_REQUEST]


async def test_a_run_can_target_a_specific_agent(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, _, _ = node

    async with client_for(node) as client:
        await client.post("/panel/api/run", json={"task": "直接给 coder", "to": "coder"})

    assert inbox(layout, "coder") == [MessageType.TASK_REQUEST]


async def test_a_run_without_a_coordinator_says_what_to_do(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(
        '[node]\nname = "solo"\nworkspace = "."\n\n[agents.cli]\nrole = "user"\n',
        encoding="utf-8",
    )
    Mailbox(layout.mailbox_dir("cli")).ensure()
    bundle = (layout, Config.load_from(layout), PeerRegistry(layout.root))

    async with client_for(bundle) as client:
        response = await client.post("/panel/api/run", json={"task": "干活"})

    assert response.status_code == 400
    assert "coordinator" in response.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"task": ""}, {"task": "x", "怪字段": 1}])
async def test_malformed_run_requests_are_rejected(
    node: tuple[NodeLayout, Config, PeerRegistry], body: dict[str, object]
) -> None:
    async with client_for(node) as client:
        assert (await client.post("/panel/api/run", json=body)).status_code == 422


# ---------- 发消息 ----------


async def test_sending_a_chat_from_the_panel(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, _, _ = node

    async with client_for(node) as client:
        response = await client.post(
            "/panel/api/send", json={"to": "coder", "body": "你在忙吗", "kind": "chat"}
        )

    assert response.status_code == 202
    assert inbox(layout, "coder") == [MessageType.CHAT]


async def test_sending_can_mention_a_partner_to_start_a_conversation(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """面板上也能发起两个 Agent 之间的对话 —— 就是带上 mentions。"""
    layout, _, _ = node

    async with client_for(node) as client:
        await client.post(
            "/panel/api/send",
            json={"to": "coder", "body": "讨论一下", "mentions": ["boss"]},
        )

    env = Mailbox.read_envelope(Mailbox(layout.mailbox_dir("coder")).list_new()[0])
    assert env.payload.mentions == ("boss",)


async def test_an_unknown_kind_is_refused(node: tuple[NodeLayout, Config, PeerRegistry]) -> None:
    async with client_for(node) as client:
        response = await client.post(
            "/panel/api/send", json={"to": "coder", "body": "x", "kind": "carrier_pigeon"}
        )

    assert response.status_code == 400


async def test_sending_to_an_unknown_agent_is_a_clear_error(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with client_for(node) as client:
        response = await client.post("/panel/api/send", json={"to": "ghost", "body": "x"})

    assert response.status_code == 400
    assert "ghost" in response.json()["detail"]


# ---------- 改配置 ----------


async def test_config_can_be_read_and_written(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    # Arrange
    layout, _, _ = node

    # Act
    async with client_for(node) as client:
        current = (await client.get("/panel/api/config")).json()["text"]
        updated = current + '\n[agents.tester]\nrole = "worker"\n'
        response = await client.put("/panel/api/config", json={"text": updated})

    # Assert
    assert response.status_code == 200
    assert "tester" in Config.load_from(layout).agents
    assert (layout.root / CONFIG_BACKUP).is_file()  # 上一版留着


async def test_invalid_toml_never_touches_the_disk(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """面板上改坏配置的代价是所有 agentd 都起不来，所以先校验再落盘。"""
    layout, _, _ = node
    before = layout.node_toml.read_text(encoding="utf-8")

    async with client_for(node) as client:
        response = await client.put("/panel/api/config", json={"text": "这不是 toml ["})

    assert response.status_code == 400
    assert "TOML" in response.json()["detail"]
    assert layout.node_toml.read_text(encoding="utf-8") == before


async def test_config_that_breaks_the_schema_is_refused(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, _, _ = node
    before = layout.node_toml.read_text(encoding="utf-8")
    broken = '[node]\nname = "laptop"\n\n[agents.coder]\nprovider = "不存在的"\n'

    async with client_for(node) as client:
        response = await client.put("/panel/api/config", json={"text": broken})

    assert response.status_code == 400
    assert layout.node_toml.read_text(encoding="utf-8") == before


# ---------- 边界：默认不开、非本机拒绝 ----------


async def test_write_endpoints_are_absent_by_default(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """写入口默认就不存在 —— 不是「存在但会拒绝」。"""
    async with client_for(node, writable=False) as client:
        assert (await client.post("/panel/api/run", json={"task": "x"})).status_code == 404
        assert (await client.get("/panel/api/config")).status_code == 404
        assert (await client.get("/panel/api/state")).status_code == 200  # 只读的还在


async def test_a_non_local_client_is_refused(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """逐请求校验来源，而不是依赖「我们绑的是回环」。

    反向代理、端口转发、配置写错，任何一种都会让那个假设悄悄失效。
    """
    layout, config, peers = node
    application = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=("10.0.8.9", 5555)),
        base_url="http://panel.test",
    ) as client:
        run = await client.post("/panel/api/run", json={"task": "x"})
        config_write = await client.put("/panel/api/config", json={"text": "x = 1"})
        state = await client.get("/panel/api/state")

    assert run.status_code == 403
    assert config_write.status_code == 403
    assert state.status_code == 200  # 只读的不受影响
    assert inbox(layout, "boss") == []


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_clients_are_recognised(host: str) -> None:
    assert is_local_client(host)


@pytest.mark.parametrize("host", ["10.0.8.9", "example.com", "", None])
def test_everything_else_is_not_local(host: str | None) -> None:
    assert not is_local_client(host)


# ---------- CLI 开关 ----------


def test_panel_write_refuses_a_non_loopback_host(tmp_path: Path) -> None:
    """能改配置 ≈ 能在这台机器上执行命令，不该跟着 --host 0.0.0.0 一起对外。"""
    from typer.testing import CliRunner

    runner = CliRunner()
    assert runner.invoke(cli_app, ["init", str(tmp_path), "--node-name", "n"]).exit_code == 0

    result = runner.invoke(
        cli_app, ["serve", "--host", "0.0.0.0", "--panel-write", "-w", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "回环" in result.output


def test_panel_write_requires_the_panel_to_be_on(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    assert runner.invoke(cli_app, ["init", str(tmp_path), "--node-name", "n"]).exit_code == 0

    result = runner.invoke(cli_app, ["serve", "--no-panel", "--panel-write", "-w", str(tmp_path)])

    assert result.exit_code == 1
