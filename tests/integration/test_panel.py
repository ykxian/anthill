"""只读面板：数据取自磁盘、不泄密钥、不提供任何写入口。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anthill.cli.serve_cmd import is_loopback
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.ids import new_id, new_thread_id
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskRequestPayload
from anthill.discovery.registry import PeerRegistry
from anthill.orchestrator.plan import Plan
from anthill.orchestrator.state import RunState, RunStore
from anthill.security.keys import PairingToken, new_key
from anthill.web.app import create_app
from anthill.web.panel import build_snapshot

NODE_TOML = """
[node]
name = "laptop"
workspace = "."

[agents.cli]
role = "user"

[agents.coder]
role = "worker"

[agents.boss]
role = "coordinator"
"""

PLAN = Plan.model_validate(
    {
        "goal": "为 date.py 补单测",
        "steps": [{"id": "s1", "assignee": "coder", "task": "写测试", "depends_on": []}],
        "done_when": "",
    }
)


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config, PeerRegistry]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    for name in ("cli", "coder", "boss"):
        Mailbox(layout.mailbox_dir(name)).ensure()
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


def app_for(node: tuple[NodeLayout, Config, PeerRegistry], *, panel: bool = True) -> object:
    layout, config, peers = node
    return create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="serve", echo=False),
        panel=panel,
    )


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://panel.test",
    )


# ---------- 快照 ----------


def test_snapshot_lists_every_configured_agent(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, config, peers = node

    snapshot = build_snapshot(layout, config, peers)

    assert snapshot["node"] == "laptop"
    assert {a["name"] for a in snapshot["agents"]} == {"cli", "coder", "boss"}
    assert all(not a["running"] for a in snapshot["agents"])  # 没启动就是没在跑


def test_snapshot_reports_queue_depth(node: tuple[NodeLayout, Config, PeerRegistry]) -> None:
    layout, config, peers = node
    Mailbox(layout.mailbox_dir("coder")).deposit(
        Envelope.new(
            sender=Address(node="laptop", agent="cli"),
            recipient=Address(node="laptop", agent="coder"),
            type=MessageType.TASK_REQUEST,
            payload=TaskRequestPayload(title="堆着的活"),
        )
    )

    snapshot = build_snapshot(layout, config, peers)

    coder = next(a for a in snapshot["agents"] if a["name"] == "coder")
    assert coder["queue"] == 1


def test_snapshot_includes_runs_and_their_steps(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, config, peers = node
    RunStore(layout.blackboard).save(
        RunState.start(
            task_id=new_id(),
            plan=PLAN,
            requester="laptop:cli",
            root_thread=new_thread_id(),
            root_msg_id=new_id(),
        )
    )

    snapshot = build_snapshot(layout, config, peers)

    assert len(snapshot["runs"]) == 1
    assert snapshot["runs"][0]["goal"] == PLAN.goal
    assert [s["id"] for s in snapshot["runs"][0]["steps"]] == ["s1"]


def test_run_rows_carry_an_event_count_not_the_trace_itself(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """敏感面纪律（与 tst2 对齐的补充 c）：面板只知道「有 N 条事件」，
    流水全文只走 `anthill runs <id> --trace`。"""
    from anthill.orchestrator.trace import RunTrace

    layout, config, peers = node
    task_id = new_id()
    RunStore(layout.blackboard).save(
        RunState.start(
            task_id=task_id,
            plan=PLAN,
            requester="laptop:cli",
            root_thread=new_thread_id(),
            root_msg_id=new_id(),
        )
    )
    trace = RunTrace(layout.blackboard / "tasks" / task_id)
    trace.emit("run.started", goal="秘密目标全文")
    trace.emit("step.dispatched", step="s1")

    snapshot = build_snapshot(layout, config, peers)

    run = snapshot["runs"][0]
    assert run["events"] == 2
    assert "秘密目标全文" not in str(run.get("trace", ""))


def test_snapshot_merges_events_from_every_agent_log(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, config, peers = node
    for name in ("coder", "boss"):
        log = EventLog(layout.log_file(name), agent=name, echo=False)
        log.info("msg.received", msg=new_id(), thread=new_thread_id())
        log.close()

    events = build_snapshot(layout, config, peers)["events"]

    assert {e["agent"] for e in events} == {"coder", "boss"}
    assert all(e["event"] == "msg.received" for e in events)


def test_snapshot_never_exposes_peer_keys(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """peers.json 里有共享密钥明文，面板绝不能把它带出来。"""
    layout, config, peers = node
    key = new_key()
    peers.trust(PairingToken(node="lab", endpoint="http://lab:45778", key=key))

    body = str(build_snapshot(layout, config, peers))

    assert "lab" in body  # 节点本身要能看到
    assert key.hex() not in body
    assert "key" not in body.lower()


# ---------- HTTP ----------


async def test_panel_page_is_served(node: tuple[NodeLayout, Config, PeerRegistry]) -> None:
    async with client_for(app_for(node)) as client:
        response = await client.get("/panel")

    assert response.status_code == 200
    assert "AntHill" in response.text
    # 判据是「有没有真的去外网取东西」，不是「文本里出现过 http」——
    # 占位符里写一句 https://api.deepseek.com 不构成外部依赖
    head = response.text.split("<script>")[0]
    for loader in ('src="http', "src='http", 'href="http', "href='http", "url(http"):
        assert loader not in head, loader


async def test_panel_page_has_no_external_assets(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """无构建链、无 CDN：面板得在没有外网的服务器上也能打开。"""
    async with client_for(app_for(node)) as client:
        html = (await client.get("/panel")).text

    for pattern in ('src="http', 'href="http', "cdn.", "unpkg", "googleapis"):
        assert pattern not in html


async def test_state_endpoint_returns_the_snapshot(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with client_for(app_for(node)) as client:
        body = (await client.get("/panel/api/state")).json()

    assert body["node"] == "laptop"
    assert len(body["agents"]) == 3


async def test_panel_is_absent_when_not_enabled(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with client_for(app_for(node, panel=False)) as client:
        assert (await client.get("/panel")).status_code == 404
        assert (await client.get("/panel/api/state")).status_code == 404


async def test_panel_offers_no_write_endpoints(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """只读是刻意的：确认与操作留在 CLI，面板就没法成为攻击面。"""
    app = app_for(node)
    panel_routes = [r for r in app.routes if str(getattr(r, "path", "")).startswith("/panel")]  # type: ignore[attr-defined]

    assert panel_routes
    for route in panel_routes:
        methods = getattr(route, "methods", set()) or set()
        assert methods <= {"GET", "HEAD"}


async def test_deliver_endpoint_still_works_with_the_panel_on(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with client_for(app_for(node)) as client:
        response = await client.post("/deliver", json={"垃圾": True})

    assert response.status_code == 400  # 面板没有挤掉投递端点


# ---------- 默认只在回环上开 ----------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_recognised(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.8.9", "example.com"])
def test_non_loopback_hosts_are_not(host: str) -> None:
    """绑 0.0.0.0 时面板会跟着暴露给整个网段，所以默认不开。"""
    assert not is_loopback(host)


def test_events_are_not_duplicated(node: tuple[NodeLayout, Config, PeerRegistry]) -> None:
    """serve 的日志只有一个文件，但它在事件里的 agent 名是 `serve:<节点>`。

    照名字拼路径会把同一个文件读两遍，事件流里每条都成双 —— 面板上一眼就能看出来。
    """
    layout, config, peers = node
    log = EventLog(layout.log_file("serve"), agent=f"serve:{config.node.name}", echo=False)
    log.info("serve.start", node=config.node.name)
    log.close()

    events = build_snapshot(layout, config, peers)["events"]

    assert [e["event"] for e in events] == ["serve.start"]
