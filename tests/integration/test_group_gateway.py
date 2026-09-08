"""项目组单一外部邮箱，以及与消息平面分离的组间状态读取。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from anthill.cli.main import app as cli_app
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, EventPayload, MessageType
from anthill.discovery.registry import PeerRegistry
from anthill.security.keys import PairingToken, new_key
from anthill.security.signing import sign_envelope, sign_request
from anthill.web.app import STATE_PATH, create_app

NODE_TOML = """
[node]
name = "data-system"
workspace = "."
external_gateway_agent = "gateway"

[agents.gateway]
role = "coordinator"

[agents.worker]
role = "worker"
"""


@pytest.fixture
def group_node(tmp_path: Path) -> tuple[NodeLayout, Config, PeerRegistry, bytes]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for agent in ("gateway", "worker"):
        Mailbox(layout.mailbox_dir(agent)).ensure()
    config = Config.load_from(layout)
    peers = PeerRegistry(layout.root)
    key = new_key()
    peers.trust(PairingToken(node="other-team", endpoint="", key=key))
    return layout, config, peers, key


def _client(layout: NodeLayout, config: Config, peers: PeerRegistry) -> httpx.AsyncClient:
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="test", echo=False),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://data-system.test"
    )


def _incoming(agent: str) -> Envelope:
    message_type = MessageType.EVENT if agent == "all" else MessageType.CHAT
    payload = EventPayload(kind="test") if agent == "all" else ChatPayload(body="hello")
    return Envelope.new(
        sender=Address(node="other-team", agent="gateway"),
        recipient=Address(node="data-system", agent=agent),
        type=message_type,
        payload=payload,
    )


def _request_headers(key: bytes, path: str) -> dict[str, str]:
    stamp = now().isoformat()
    return {
        "X-AntHill-Node": "other-team",
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node="other-team", path=path, ts=stamp),
    }


async def test_remote_ingress_accepts_only_the_exact_gateway_without_member_disclosure(
    group_node: tuple[NodeLayout, Config, PeerRegistry, bytes],
) -> None:
    layout, config, peers, key = group_node

    async with _client(layout, config, peers) as client:
        accepted = await client.post(
            "/deliver",
            json=sign_envelope(_incoming("gateway"), key).model_dump(mode="json", by_alias=True),
        )
        refusals = []
        for recipient in ("worker", "role:worker", "all"):
            response = await client.post(
                "/deliver",
                json=sign_envelope(_incoming(recipient), key).model_dump(
                    mode="json", by_alias=True
                ),
            )
            refusals.append((response.status_code, response.json()["detail"]))
        health = (await client.get("/health")).json()

    assert accepted.status_code == 202
    assert len(Mailbox(layout.mailbox_dir("gateway")).list_new()) == 1
    assert Mailbox(layout.mailbox_dir("worker")).list_new() == []
    assert refusals == [(404, "本节点没有开放这个外部收件地址")] * 3
    assert health["agents"] == ["gateway"]
    assert health["nodes"] == [{"node": "data-system", "agents": ["gateway"]}]


async def test_gateway_gate_runs_only_after_signature_verification(
    group_node: tuple[NodeLayout, Config, PeerRegistry, bytes],
) -> None:
    layout, config, peers, _ = group_node
    forged = sign_envelope(_incoming("worker"), new_key())

    async with _client(layout, config, peers) as client:
        response = await client.post("/deliver", json=forged.model_dump(mode="json", by_alias=True))

    assert response.status_code == 401
    assert Mailbox(layout.mailbox_dir("worker")).list_new() == []


def _non_state_files(layout: NodeLayout) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in layout.root.rglob("*"):
        if path.is_file() and not path.is_relative_to(layout.state_dir):
            files[str(path.relative_to(layout.root))] = path.read_bytes()
    return files


async def test_group_state_publish_show_and_trusted_pull_bypass_all_message_queues(
    group_node: tuple[NodeLayout, Config, PeerRegistry, bytes],
) -> None:
    layout, config, peers, key = group_node
    runner = CliRunner()
    before = _non_state_files(layout)

    with (
        patch("anthill.core.mailbox.Mailbox.deposit") as deposit,
        patch("anthill.agent.runtime.AgentRuntime.run") as wake,
        patch("anthill.providers.fake.FakeProvider.complete") as model,
    ):
        published = runner.invoke(
            cli_app,
            [
                "state",
                "publish",
                "project.status",
                '{"phase":"verified","blockers":[]}',
                "--revision",
                "7",
                "--summary",
                "gateway candidate",
                "--from",
                "gateway",
                "-w",
                str(layout.workspace),
            ],
        )
        shown = runner.invoke(
            cli_app, ["state", "show", "project.status", "-w", str(layout.workspace)]
        )

    assert published.exit_code == 0, published.output
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["state"]["snapshot"]["phase"] == "verified"
    assert deposit.call_count == wake.call_count == model.call_count == 0
    assert _non_state_files(layout) == before
    assert (layout.state_dir / "project.status.json").is_file()
    for agent in ("gateway", "worker"):
        assert Mailbox(layout.mailbox_dir(agent)).list_new() == []
        assert not list(Mailbox(layout.mailbox_dir(agent)).pending.glob("*.json"))

    detail_path = f"{STATE_PATH}/project.status"
    async with _client(layout, config, peers) as client:
        unsigned = await client.get(STATE_PATH, headers={"X-AntHill-Node": "other-team"})
        listed = await client.get(STATE_PATH, headers=_request_headers(key, STATE_PATH))
        detail = await client.get(detail_path, headers=_request_headers(key, detail_path))

    assert unsigned.status_code == 401
    assert listed.status_code == 200
    assert detail.status_code == 200
    metadata = listed.json()["states"][0]
    assert set(metadata) == {
        "key",
        "revision",
        "digest",
        "publisher",
        "updated_at",
        "summary",
    }
    assert "snapshot" not in metadata
    assert metadata["publisher"] == "data-system:gateway"
    assert detail.json()["state"]["snapshot"] == {"phase": "verified", "blockers": []}
