"""状态公告 Panel API：认证、缓存边界与详情懒加载契约。"""

from __future__ import annotations

import json

import httpx

from anthill.core.config import Config
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.payloads import StateUpdatePayload
from anthill.core.state_sync import StateStore
from anthill.discovery.registry import PeerRegistry
from anthill.web.app import create_app

TOKEN = "panel-state-test-token"


def publish(layout: NodeLayout, *, revision: int = 1) -> None:
    payload = StateUpdatePayload.from_snapshot(
        key="project.board",
        revision=revision,
        summary=f"公告 revision {revision}",
        snapshot={"private_detail": f"only-on-click-{revision}"},
    )
    assert StateStore(layout.state_dir("alpha")).apply(
        payload, source="testnode:cli"
    ).applied


def client_for(
    layout: NodeLayout,
    config: Config,
    *,
    host: str = "127.0.0.1",
    token: str = "",
) -> httpx.AsyncClient:
    app = create_app(
        layout=layout,
        config=config,
        peers=PeerRegistry(layout.root),
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_token=token,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),  # type: ignore[arg-type]
        base_url="http://panel.test",
    )


async def test_index_is_metadata_only_and_detail_is_conditional(
    layout: NodeLayout, config: Config
) -> None:
    publish(layout)

    async with client_for(layout, config) as client:
        listing = await client.get("/panel/api/states")
        list_etag = listing.headers["etag"]
        not_changed = await client.get(
            "/panel/api/states", headers={"If-None-Match": list_etag}
        )
        item = next(
            group for group in listing.json()["agents"] if group["agent"] == "alpha"
        )["states"][0]
        detail = await client.get(
            "/panel/api/states/alpha/project.board",
            headers={"If-Match": item["etag"]},
        )
        stale_click = await client.get(
            "/panel/api/states/alpha/project.board",
            headers={"If-Match": '"state-old"'},
        )

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "private, no-cache"
    assert "snapshot" not in listing.text
    assert "only-on-click" not in listing.text
    assert not_changed.status_code == 304
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.headers["etag"] == item["etag"]
    assert detail.json()["snapshot"] == {"private_detail": "only-on-click-1"}
    assert stale_click.status_code == 412


async def test_existing_snapshot_cluster_and_websocket_payload_sources_stay_small(
    layout: NodeLayout, config: Config
) -> None:
    publish(layout)

    async with client_for(layout, config) as client:
        old_state = await client.get("/panel/api/state")
        cluster = await client.get("/panel/api/cluster")

    assert "private_detail" not in old_state.text
    assert "private_detail" not in cluster.text
    assert "snapshot" not in old_state.text
    assert "snapshot" not in cluster.text


async def test_state_routes_reuse_panel_authorization(layout: NodeLayout, config: Config) -> None:
    publish(layout)
    headers = {"X-AntHill-Panel": TOKEN}

    async with client_for(layout, config, host="10.15.3.99", token=TOKEN) as client:
        refused = await client.get("/panel/api/states")
        wrong = await client.get(
            "/panel/api/states/alpha/project.board",
            headers={"X-AntHill-Panel": "wrong"},
        )
        accepted = await client.get("/panel/api/states", headers=headers)
        cross_site = await client.get(
            "/panel/api/states",
            headers={**headers, "Origin": "http://evil.example"},
        )

    assert refused.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert cross_site.status_code == 403


async def test_node_agent_and_key_scope_cannot_escape(layout: NodeLayout, config: Config) -> None:
    publish(layout)
    outside = layout.workspace / "outside.json"
    outside.write_text(json.dumps({"private_detail": "escape"}), encoding="utf-8")

    async with client_for(layout, config) as client:
        unknown_node = await client.get("/panel/api/states?node=elsewhere")
        unknown_agent = await client.get("/panel/api/states/ghost/project.board")
        invalid_key = await client.get("/panel/api/states/alpha/bad%24key")
        slash_escape = await client.get("/panel/api/states/alpha/%2E%2E%2Foutside")

    assert unknown_node.status_code == 404
    assert unknown_agent.status_code == 404
    assert invalid_key.status_code == 400
    assert slash_escape.status_code in {400, 404}
    assert "escape" not in invalid_key.text + slash_escape.text
