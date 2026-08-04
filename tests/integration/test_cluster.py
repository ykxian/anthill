"""总控面板：把各节点的快照汇到一处。

三个重点，每个都对应一次真出过的问题：
**一个连不上的节点不能把整个面板卡住**、
**对端传来的东西是外部输入、必须校验**、
**读别人的状态同样要认证**。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.envelope import TransportKind
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry
from anthill.security.keys import PairingToken, new_key
from anthill.security.signing import sign_request
from anthill.web.app import create_app
from anthill.web.cluster import (
    STATUS_FILE,
    ClusterCache,
    build_cluster,
    read_status,
    write_status,
)
from anthill.web.endpoints import SUMMARY_PATH

NODE_TOML = """
[node]
name = "{name}"
workspace = "."

[agents.cli]
role = "user"

[agents.coder]
role = "worker"
"""

Bundle = tuple[NodeLayout, Config, PeerRegistry]


def make_node(root: Path, name: str) -> Bundle:
    layout = NodeLayout(root).ensure_base()
    layout.node_toml.write_text(NODE_TOML.format(name=name), encoding="utf-8")
    for agent in ("cli", "coder"):
        Mailbox(layout.mailbox_dir(agent)).ensure()
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


def quiet_log() -> EventLog:
    return EventLog(None, agent="test", echo=False)


@pytest.fixture
def local(tmp_path: Path) -> Bundle:
    return make_node(tmp_path / "laptop", "laptop")


# ---------- 状态文件 ----------


def test_status_is_written_as_one_file_for_others_to_fetch(local: Bundle) -> None:
    """约定就是这一条：每个节点写一个文件，总控只取那一个。"""
    layout, config, peers = local

    write_status(layout, config, peers)

    data = json.loads((layout.root / STATUS_FILE).read_text(encoding="utf-8"))
    assert data["node"] == "laptop"
    assert {a["name"] for a in data["agents"]} == {"cli", "coder"}
    assert data["written_at"]


def test_status_never_contains_a_peer_key_or_the_peer_list(local: Bundle) -> None:
    """这个文件是要发给别人的。

    密钥当然不能跟着走；**peers 列表也不该** —— 对端做总控面板不需要知道
    我还认识谁，而那份列表里带着别人的指纹与地址。
    """
    layout, config, peers = local
    key = new_key()
    peers.trust(PairingToken(node="lab", endpoint="http://lab:45778", key=key))

    write_status(layout, config, peers)

    data = json.loads((layout.root / STATUS_FILE).read_text(encoding="utf-8"))
    assert "peers" not in data
    assert key.hex() not in json.dumps(data)


def test_status_file_is_not_world_readable(local: Bundle) -> None:
    """共用服务器上，同机器的其他账号不该能读改这个文件 —— 它会进别人面板的页面。"""
    layout, config, peers = local

    write_status(layout, config, peers)

    assert (layout.root / STATUS_FILE).stat().st_mode & 0o077 == 0


def test_a_stale_status_file_is_recomputed(local: Bundle) -> None:
    """别把陈旧状态当成真相：写得太久以前就现算一份。"""
    layout, config, peers = local
    (layout.root / STATUS_FILE).write_text(
        json.dumps({"node": "laptop", "written_at": "2020-01-01T00:00:00+00:00", "agents": []}),
        encoding="utf-8",
    )

    assert len(read_status(layout, config, peers)["agents"]) == 2


def test_a_future_dated_status_file_is_also_recomputed(local: Bundle) -> None:
    """时钟往前跳过的文件同样不可信 —— 用绝对值判岁数，否则它会「永远新鲜」。"""
    layout, config, peers = local
    (layout.root / STATUS_FILE).write_text(
        json.dumps({"node": "laptop", "written_at": "2099-01-01T00:00:00+00:00", "agents": []}),
        encoding="utf-8",
    )

    assert len(read_status(layout, config, peers)["agents"]) == 2


def test_a_recent_status_file_is_served_as_is(local: Bundle) -> None:
    layout, config, peers = local
    (layout.root / STATUS_FILE).write_text(
        json.dumps(
            {"node": "laptop", "written_at": now().isoformat(), "agents": [{"name": "缓存的"}]}
        ),
        encoding="utf-8",
    )

    assert read_status(layout, config, peers)["agents"] == [{"name": "缓存的"}]


# ---------- /node/summary 的认证 ----------


def app_for(bundle: Bundle, *, summary: bool = True) -> object:
    layout, config, peers = bundle
    return create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=quiet_log(),
        panel=True,
        summary=summary,
    )


def client_for(
    bundle: Bundle, *, host: str = "127.0.0.1", summary: bool = True
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(  # type: ignore[arg-type]
            app=app_for(bundle, summary=summary), client=(host, 1)
        ),
        base_url="http://node.test",
    )


def signed_headers(node: str, key: bytes, *, ts: str | None = None) -> dict[str, str]:
    stamp = ts or now().isoformat()
    return {
        "X-AntHill-Node": node,
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node=node, path=SUMMARY_PATH, ts=stamp),
    }


@pytest.fixture
def paired(tmp_path: Path) -> tuple[Bundle, bytes]:
    bundle = make_node(tmp_path / "lab", "lab")
    key = new_key()
    bundle[2].trust(PairingToken(node="laptop", endpoint="", key=key))
    return bundle, key


async def test_a_trusted_peer_can_read_the_summary(paired: tuple[Bundle, bytes]) -> None:
    # Arrange
    bundle, key = paired

    # Act
    async with client_for(bundle) as client:
        response = await client.get(SUMMARY_PATH, headers=signed_headers("laptop", key))

    # Assert
    assert response.status_code == 200
    assert response.json()["node"] == "lab"


async def test_an_untrusted_node_cannot_read_the_summary(tmp_path: Path) -> None:
    bundle = make_node(tmp_path / "lab", "lab")

    async with client_for(bundle) as client:
        response = await client.get(SUMMARY_PATH, headers=signed_headers("stranger", new_key()))

    assert response.status_code == 403


async def test_a_bad_signature_is_refused(paired: tuple[Bundle, bytes]) -> None:
    bundle, _ = paired

    async with client_for(bundle) as client:
        response = await client.get(SUMMARY_PATH, headers=signed_headers("laptop", new_key()))

    assert response.status_code == 401


async def test_a_stale_request_is_refused_as_replay(paired: tuple[Bundle, bytes]) -> None:
    bundle, key = paired

    async with client_for(bundle) as client:
        response = await client.get(
            SUMMARY_PATH,
            headers=signed_headers("laptop", key, ts="2020-01-01T00:00:00+00:00"),
        )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        # HTTP 头按 latin-1 解出来，一个 0xFF 字节就能让 compare_digest 抛 TypeError
        ("签名里有非 ASCII 字节", {"X-AntHill-Sig": b"hmac-sha256:\xff\xfe"}),
        ("时间戳没带时区", {"X-AntHill-Ts": b"2026-08-04T00:00:00"}),
        ("时间戳根本不是时间", {"X-AntHill-Ts": b"not-a-time"}),
    ],
)
async def test_malformed_headers_are_refused_not_crashed(
    paired: tuple[Bundle, bytes], label: str, headers: dict[str, bytes]
) -> None:
    """认证这段代码在认证**之前**就要面对陌生人给的字节。

    畸形输入必须是 401，不能是 500 —— 换来一段 traceback 的话，
    连密钥都不用有的人随手就能触发。
    """
    bundle, key = paired

    async with client_for(bundle) as client:
        response = await client.get(
            SUMMARY_PATH, headers={**signed_headers("laptop", key), **headers}
        )

    assert response.status_code == 401, label


async def test_no_summary_closes_the_endpoint(paired: tuple[Bundle, bytes]) -> None:
    """不想共享状态的人要有办法关掉它。"""
    bundle, key = paired

    async with client_for(bundle, summary=False) as client:
        response = await client.get(SUMMARY_PATH, headers=signed_headers("laptop", key))

    assert response.status_code == 404


# ---------- 汇总 ----------


async def test_the_cluster_view_starts_with_the_local_node(local: Bundle) -> None:
    layout, config, peers = local

    cluster = await build_cluster(layout, config, peers, quiet_log())

    assert cluster["node"] == "laptop"
    assert [n["node"] for n in cluster["nodes"]] == ["laptop"]
    assert cluster["nodes"][0]["local"] is True


async def test_an_unreachable_peer_is_marked_not_hidden_and_does_not_block(
    local: Bundle,
) -> None:
    """一个连不上的节点不能把整个面板卡住 —— 它只该显示成「连不上」。"""
    # Arrange：指向一个没人监听的地址
    layout, config, peers = local
    peers.trust(PairingToken(node="lab", endpoint="http://127.0.0.1:1", key=new_key()))

    # Act
    cluster = await build_cluster(layout, config, peers, quiet_log())

    # Assert
    lab = next(n for n in cluster["nodes"] if n["node"] == "lab")
    assert lab["reachable"] is False
    assert lab["reason"]
    assert cluster["nodes"][0]["reachable"] is True  # 本机照常


async def test_untrusted_peers_are_not_fetched(local: Bundle) -> None:
    """发现 ≠ 可通信，也 ≠ 可以去读它的状态。"""
    layout, config, peers = local
    peers.observe(node="stranger", endpoint="http://127.0.0.1:1", agents=())

    cluster = await build_cluster(layout, config, peers, quiet_log())

    assert [n["node"] for n in cluster["nodes"]] == ["laptop"]


async def test_a_second_look_at_a_dead_peer_comes_from_cache(local: Bundle) -> None:
    """浏览器每 5 秒轮询一次；每次都去连一遍每台机器纯属浪费对方的 sshd。"""
    layout, config, peers = local
    peers.trust(PairingToken(node="lab", endpoint="http://127.0.0.1:1", key=new_key()))
    cache = ClusterCache(ttl=600.0)

    first = await build_cluster(layout, config, peers, quiet_log(), cache)
    second = await build_cluster(layout, config, peers, quiet_log(), cache)

    assert first["nodes"][1] is second["nodes"][1]  # 同一个对象 = 没有再取一次
    await cache.aclose()


# ---------- 对端传来的东西是外部输入 ----------


class FakePeerServer:
    """一个假的对端：想返回什么就返回什么，用来喂畸形输入。"""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body, self.status = body, status

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


async def fetch_from(local: Bundle, server: FakePeerServer, monkeypatch) -> dict:  # type: ignore[no-untyped-def]
    layout, config, peers = local
    peers.trust(PairingToken(node="lab", endpoint="http://lab.test", key=new_key()))

    import anthill.web.cluster as cluster_mod

    original = httpx.AsyncClient

    def patched(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.ASGITransport(app=server)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    cluster = await cluster_mod.build_cluster(layout, config, peers, quiet_log())
    return next(n for n in cluster["nodes"] if n["node"] == "lab")


async def test_a_peer_returning_valid_json_that_is_not_an_object_is_marked_down(
    local: Bundle, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """`json.loads('"x"')` 是合法 JSON 但不是对象 —— 早先这里会把整个端点 500 掉。"""
    lab = await fetch_from(local, FakePeerServer(b'"just a string"'), monkeypatch)

    assert lab["reachable"] is False
    assert "不合法" in lab["reason"]


async def test_a_peer_cannot_inject_html_through_a_counter_field(
    local: Bundle, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """这些字段最终会进面板的 HTML。

    `queue` 声明成整数，字符串就该在**进程边界**上被挡掉 ——
    而不是指望前端每一处插值都记得转义。
    """
    payload = json.dumps(
        {
            "node": "lab",
            "written_at": now().isoformat(),
            "agents": [{"name": "coder", "queue": "<img src=x onerror=alert(1)>"}],
        }
    ).encode()

    lab = await fetch_from(local, FakePeerServer(payload), monkeypatch)

    assert lab["reachable"] is False
    assert "onerror" not in json.dumps(lab)


async def test_a_peer_cannot_flood_the_panel_with_events(local: Bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps(
        {
            "node": "lab",
            "written_at": now().isoformat(),
            "events": [{"event": "spam"} for _ in range(10_000)],
        }
    ).encode()

    lab = await fetch_from(local, FakePeerServer(payload), monkeypatch)

    assert lab["reachable"] is False


async def test_an_oversized_status_is_refused_before_it_lands_in_memory(
    local: Bundle, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from anthill.web.cluster import MAX_STATUS_BYTES

    lab = await fetch_from(local, FakePeerServer(b"x" * (MAX_STATUS_BYTES + 1)), monkeypatch)

    assert lab["reachable"] is False
    assert "上限" in lab["reason"]


async def test_a_peer_cannot_claim_to_be_the_local_node(local: Bundle, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """payload 里的 `local` / `reachable` 说了不算 —— 以本机的判断为准。"""
    payload = json.dumps(
        {"node": "我不是 lab", "local": True, "reachable": True, "written_at": now().isoformat()}
    ).encode()

    lab = await fetch_from(local, FakePeerServer(payload), monkeypatch)

    assert lab["node"] == "lab"
    assert lab["local"] is False


async def test_a_node_whose_snapshot_stopped_updating_is_not_shown_as_alive(
    local: Bundle, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """SSH 那条路是直接读对方磁盘上的文件。

    那台机器上的 serve 被 kill 掉之后文件还在，读得到 ——
    不看时间的话，一台死了一周的机器会一直显示成绿灯。
    """
    payload = json.dumps(
        {"node": "lab", "written_at": "2020-01-01T00:00:00+00:00", "agents": []}
    ).encode()

    lab = await fetch_from(local, FakePeerServer(payload), monkeypatch)

    assert lab["reachable"] is False
    assert "停更" in lab["reason"]


# ---------- SSH ----------


async def test_ssh_peers_are_read_over_sftp_not_http(tmp_path: Path) -> None:
    """SSH 节点不开端口，状态只能靠 SFTP 读那个文件。"""
    # Arrange：远端工作区里放好 status.json
    import asyncssh

    remote_root = tmp_path / "lab"
    remote_layout, remote_config, remote_peers = make_node(remote_root, "lab")
    write_status(remote_layout, remote_config, remote_peers)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(
            client_key.export_public_key().decode()
        ),
        sftp_factory=True,
    )
    port = int(server.sockets[0].getsockname()[1])

    layout, _, peers = make_node(tmp_path / "laptop", "laptop")
    layout.node_toml.write_text(
        NODE_TOML.format(name="laptop")
        + f'\n[peers.lab]\ntransport = "ssh"\nhost = "127.0.0.1"\nport = {port}\n'
        f'user = "tester"\nremote_workspace = "{remote_root}"\n'
        f'identity_file = "{tmp_path / "id"}"\nknown_hosts = "{tmp_path / "kh"}"\n',
        encoding="utf-8",
    )
    (tmp_path / "id").write_bytes(client_key.export_private_key())
    (tmp_path / "id").chmod(0o600)
    (tmp_path / "kh").write_text(
        f"[127.0.0.1]:{port} " + host_key.export_public_key().decode().strip() + "\n",
        encoding="utf-8",
    )
    config = Config.load_from(layout)
    peers.trust(PairingToken(node="lab", endpoint="", key=new_key()))

    # Act
    try:
        cluster = await build_cluster(layout, config, peers, quiet_log())
    finally:
        server.close()
        await server.wait_closed()

    # Assert
    lab = next(n for n in cluster["nodes"] if n["node"] == "lab")
    assert lab["reachable"] is True, lab.get("reason")
    assert {a["name"] for a in lab["agents"]} == {"cli", "coder"}
    assert config.peers["lab"].transport is TransportKind.SSH


# ---------- 写状态的那个后台循环 ----------


async def test_the_status_loop_keeps_going_when_a_write_fails(
    local: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不出来只该让别人看不到我的状态，**不该让这台机器停止收消息**。

    早先这里只接 OSError，而 `atomic_write` 抛的是 MailboxError ——
    磁盘一满，写状态的任务就崩，`serve` 跟着整个退出，退出码还是 0。
    """
    # Arrange
    import anthill.cli.serve_cmd as serve_mod
    from anthill.core.errors import MailboxError

    layout, config, peers = local
    attempts = 0

    def explode(*_: object) -> None:
        nonlocal attempts
        attempts += 1
        raise MailboxError("磁盘满了")

    monkeypatch.setattr(serve_mod, "write_status", explode)
    stop = asyncio.Event()

    # Act：跑几轮之后叫停
    async def stop_soon() -> None:
        while attempts < 3:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(  # 循环要是提前退了，别把测试挂死在这儿
        asyncio.gather(
            serve_mod._status_loop(layout, config, peers, quiet_log(), stop, interval=0.001),
            stop_soon(),
        ),
        timeout=10,
    )

    # Assert：一直在重试，循环没被那个异常带走
    assert attempts >= 3


async def test_no_summary_skips_writing_the_status_file(local: Bundle) -> None:
    import anthill.cli.serve_cmd as serve_mod

    layout, config, peers = local
    stop = asyncio.Event()
    stop.set()

    await serve_mod._status_loop(
        layout, config, peers, quiet_log(), stop, interval=0.001, enabled=False
    )

    assert not (layout.root / STATUS_FILE).exists()


# ---------- 面板路由 ----------


async def test_the_panel_exposes_the_cluster_view_to_the_local_machine(local: Bundle) -> None:
    async with client_for(local) as client:
        body = (await client.get("/panel/api/cluster")).json()

    assert body["nodes"][0]["node"] == "laptop"


async def test_the_cluster_view_is_refused_to_anyone_else(local: Bundle) -> None:
    """面板绑 0.0.0.0 时，把**所有**对端的状态摊给整个网段是另一回事。"""
    async with client_for(local, host="10.0.8.99") as client:
        response = await client.get("/panel/api/cluster")

    assert response.status_code == 403
    # 只看本机的那个接口照常 —— 页面会自动退回去
    async with client_for(local, host="10.0.8.99") as client:
        assert (await client.get("/panel/api/state")).status_code == 200
