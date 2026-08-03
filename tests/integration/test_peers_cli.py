"""`anthill peers` 与 `anthill serve` 的 CLI 行为，以及真实回环上的组播/HTTP 联调。

联调用例在不支持组播/绑不上端口的环境里会 skip —— CI 容器里组播常被禁掉，
但这条路径值得在本机跑一次，所以留着而不是删掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.config import Config, DiscoverySection
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.discovery.beacon import Announcement, Beacon
from anthill.discovery.registry import PeerRegistry
from anthill.security.keys import PairingToken, new_key

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["init", str(tmp_path), "--node-name", "laptop"])
    assert result.exit_code == 0, result.output
    return tmp_path


def peers_of(workspace: Path) -> PeerRegistry:
    return PeerRegistry(NodeLayout(workspace).root)


# ---------- peers CLI ----------


def test_listing_an_empty_registry_tells_you_what_to_do(workspace: Path) -> None:
    result = runner.invoke(app, ["peers", "list", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "invite" in result.output


def test_invite_prints_a_token_and_a_warning(workspace: Path) -> None:
    result = runner.invoke(app, ["peers", "invite", "lab", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "配对令牌" in result.output
    assert "明文" in result.output  # 必须提醒令牌里有密钥
    assert peers_of(workspace).key_for("lab") is not None


def test_invite_then_trust_round_trips_between_two_workspaces(tmp_path: Path) -> None:
    # Arrange：两个工作区当作两台机器
    laptop, lab = tmp_path / "laptop", tmp_path / "lab"
    for path, name in ((laptop, "laptop"), (lab, "lab")):
        assert runner.invoke(app, ["init", str(path), "--node-name", name]).exit_code == 0

    # Act：lab 邀请 laptop，laptop 拿令牌 trust
    invite = runner.invoke(app, ["peers", "invite", "laptop", "-w", str(lab)])
    token = _extract_token(invite.output)
    trusted = runner.invoke(app, ["peers", "trust", "--token", token, "-w", str(laptop)])

    # Assert：双方持有同一把钥匙、同一个指纹
    assert trusted.exit_code == 0, trusted.output
    assert peers_of(laptop).key_for("lab") == peers_of(lab).key_for("laptop")
    laptop_view = peers_of(laptop).get("lab")
    lab_view = peers_of(lab).get("laptop")
    assert laptop_view is not None and lab_view is not None
    assert laptop_view.fingerprint == lab_view.fingerprint


def test_trust_rejects_a_garbage_token(workspace: Path) -> None:
    result = runner.invoke(app, ["peers", "trust", "--token", "不是令牌", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "配对" in result.output


def test_trust_refuses_a_changed_fingerprint(workspace: Path) -> None:
    # Arrange：先信任 lab
    registry = peers_of(workspace)
    registry.trust(PairingToken(node="lab", endpoint="http://a", key=new_key()))
    forged = PairingToken(node="lab", endpoint="http://a", key=new_key()).encode()

    # Act：同名节点换了钥匙
    result = runner.invoke(app, ["peers", "trust", "--token", forged, "-w", str(workspace)])

    # Assert
    assert result.exit_code == 1
    assert "指纹" in result.output


def test_list_shows_trusted_and_discovered_differently(workspace: Path) -> None:
    registry = peers_of(workspace)
    registry.trust(PairingToken(node="lab", endpoint="http://a", key=new_key()))
    registry.observe(node="stranger", endpoint="http://b", agents=())

    result = runner.invoke(app, ["peers", "list", "-w", str(workspace)])

    assert "trusted" in result.output
    assert "discovered" in result.output


def test_forget_removes_the_peer(workspace: Path) -> None:
    peers_of(workspace).trust(PairingToken(node="lab", endpoint="http://a", key=new_key()))

    result = runner.invoke(app, ["peers", "forget", "lab", "-w", str(workspace)])

    assert result.exit_code == 0
    assert peers_of(workspace).get("lab") is None


# ---------- 真实回环联调 ----------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_serve_accepts_a_real_http_delivery(workspace: Path) -> None:
    """真的起一个 uvicorn，用真的 TCP 投一封信进去。"""
    # Arrange
    import uvicorn

    from anthill.core.envelope import Address, Envelope
    from anthill.core.mailbox import Mailbox
    from anthill.core.payloads import MessageType, TaskRequestPayload
    from anthill.security.signing import sign_envelope
    from anthill.web.app import create_app

    layout = NodeLayout(workspace)
    config = Config.load_from(layout)
    registry = peers_of(workspace)
    key = new_key()
    registry.trust(PairingToken(node="lab", endpoint="http://x", key=key))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                layout=layout,
                config=config,
                peers=registry,
                log=EventLog(None, agent="serve", echo=False),
            ),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    task = asyncio.create_task(server.serve())
    env = sign_envelope(
        Envelope.new(
            sender=Address(node="lab", agent="runner"),
            recipient=Address(node="laptop", agent="echo"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="跨机任务"),
        ),
        key,
    )

    # Act
    try:
        await _wait_for_port(port)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            response = await client.post(
                "/deliver", json=env.model_dump(mode="json", by_alias=True)
            )
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)

    # Assert
    assert response.status_code == 202
    assert [p.name for p in Mailbox(layout.mailbox_dir("echo")).list_new()] == [f"{env.id}.json"]


async def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while True:
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            return

    await asyncio.wait_for(poll(), timeout=timeout)


async def test_two_beacons_discover_each_other_over_real_multicast(tmp_path: Path) -> None:
    # Arrange：两个信标共用一个组播组
    port = _free_port()
    settings = DiscoverySection(enabled=True, port=port)
    made = []
    for name in ("laptop", "lab"):
        root = tmp_path / name
        root.mkdir()
        made.append(
            Beacon(
                settings=settings,
                announcement=Announcement(
                    node=name, endpoint=f"http://{name}.test", agents=("runner",)
                ),
                peers=PeerRegistry(root),
                log=EventLog(None, agent=name, echo=False),
                interval=0.1,
            )
        )
    laptop, lab = made
    stop = asyncio.Event()

    # Act
    tasks = [asyncio.create_task(b.run(stop)) for b in made]
    try:
        await asyncio.sleep(0.1)
        if laptop._transport is None or lab._transport is None:
            pytest.skip("本环境不支持组播（容器里常见）")
        await asyncio.sleep(0.6)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Assert：互相看见了，但都还没被信任
    seen = PeerRegistry(tmp_path / "laptop").get("lab")
    if seen is None:
        pytest.skip("组播包没有回环到本机（内核/网络配置所致）")
    assert seen.endpoint == "http://lab.test"
    assert not seen.trusted


def _extract_token(output: str) -> str:
    for line in output.splitlines():
        candidate = line.strip()
        if (
            len(candidate) > 60
            and candidate.replace("=", "").replace("-", "").replace("_", "").isalnum()
        ):
            return candidate
    raise AssertionError(f"输出里找不到配对令牌：\n{output}")
