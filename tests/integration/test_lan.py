"""LAN 投递：签名 → HTTP POST → 校验 → 落对方邮箱。

两个节点在同一个进程里用 httpx 的 ASGI 传输直连，不占端口也不依赖网络，
但走的是和真实部署完全相同的代码路径（签名、信任判定、原子写）。
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import DeliveryError
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskRequestPayload
from anthill.discovery.registry import PeerRegistry
from anthill.security.keys import PairingToken, new_key
from anthill.security.signing import sign_envelope
from anthill.transport.base import Destination
from anthill.transport.lan import FATAL_STATUS, LanTransport
from anthill.web.app import create_app

ENDPOINT = "http://lab.test"

NODE_TOML = """
[node]
name = "{name}"
workspace = "."

[agents.cli]
role = "user"

[agents.runner]
role = "worker"
"""


class Node:
    """一台机器：工作区 + peers 列表 + 一个接收端 app。"""

    def __init__(self, root: Path, name: str) -> None:
        self.name = name
        self.layout = NodeLayout(root).ensure_base()
        self.layout.node_toml.write_text(NODE_TOML.format(name=name), encoding="utf-8")
        for agent in ("cli", "runner"):
            Mailbox(self.layout.mailbox_dir(agent)).ensure()
        self.config = Config.load_from(self.layout)
        self.peers = PeerRegistry(self.layout.root)
        self.log = EventLog(None, agent=name, echo=False)
        self.app = create_app(
            layout=self.layout, config=self.config, peers=self.peers, log=self.log
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ENDPOINT)

    def inbox(self, agent: str = "runner") -> list[Envelope]:
        box = Mailbox(self.layout.mailbox_dir(agent))
        return [Mailbox.read_envelope(p) for p in box.list_new()]


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Node, Node, bytes]:
    """两台互相信任的机器：laptop 与 lab。"""
    laptop = Node(tmp_path / "laptop", "laptop")
    lab = Node(tmp_path / "lab", "lab")
    key = new_key()
    laptop.peers.trust(PairingToken(node="lab", endpoint=ENDPOINT, key=key))
    lab.peers.trust(PairingToken(node="laptop", endpoint="http://laptop.test", key=key))
    return laptop, lab, key


def task_to_lab(sender_node: str = "laptop") -> Envelope:
    return Envelope.new(
        sender=Address(node=sender_node, agent="cli"),
        recipient=Address(node="lab", agent="runner"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="在服务器跑测试"),
    )


def transport_to(laptop: Node, lab: Node) -> LanTransport:
    return LanTransport(
        node_name=laptop.name, peers=laptop.peers, client=lab.client(), log=laptop.log
    )


def dest(node: str = "lab", agent: str = "runner") -> Destination:
    return Destination(node=node, agent=agent)


# ---------- 正常投递 ----------


async def test_signed_envelope_lands_in_the_remote_mailbox(
    pair: tuple[Node, Node, bytes],
) -> None:
    # Arrange
    laptop, lab, _ = pair
    env = task_to_lab()

    # Act
    async with transport_to(laptop, lab) as transport:
        result = await transport.deliver(env, dest())

    # Assert
    assert result.ok
    assert [e.id for e in lab.inbox()] == [env.id]
    assert lab.inbox()[0].sig is not None  # 签名随信封一起落盘，可事后审计


async def test_delivery_is_idempotent_at_the_mailbox_level(
    pair: tuple[Node, Node, bytes],
) -> None:
    """重试会把同一条信封再投一次；文件名是 ULID，所以只会覆盖同一个文件。"""
    laptop, lab, _ = pair
    env = task_to_lab()

    async with transport_to(laptop, lab) as transport:
        await transport.deliver(env, dest())
        await transport.deliver(env, dest())

    assert len(lab.inbox()) == 1


async def test_health_endpoint_exposes_no_secrets(pair: tuple[Node, Node, bytes]) -> None:
    _, lab, key = pair

    async with lab.client() as client:
        body = (await client.get("/health")).json()

    assert body["node"] == "lab"
    assert "runner" in body["agents"]
    assert key.hex() not in str(body)
    assert "key" not in str(body).lower()


# ---------- 拒收 ----------


async def test_untrusted_sender_is_refused(tmp_path: Path) -> None:
    # Arrange：lab 根本不认识 laptop
    laptop = Node(tmp_path / "laptop", "laptop")
    lab = Node(tmp_path / "lab", "lab")
    laptop.peers.trust(PairingToken(node="lab", endpoint=ENDPOINT, key=new_key()))

    # Act / Assert：不可重试 —— 重试一万次也还是不被信任
    async with transport_to(laptop, lab) as transport:
        with pytest.raises(DeliveryError) as exc:
            await transport.deliver(task_to_lab(), dest())

    assert not exc.value.retryable
    assert lab.inbox() == []


async def test_tampered_envelope_is_refused_by_the_receiver(
    pair: tuple[Node, Node, bytes],
) -> None:
    # Arrange：拿正确的签名配一个被改过的 payload
    _, lab, key = pair
    signed = sign_envelope(task_to_lab(), key)
    tampered = signed.model_copy(update={"payload": TaskRequestPayload(title="rm -rf /")})

    # Act
    async with lab.client() as client:
        response = await client.post(
            "/deliver", json=tampered.model_dump(mode="json", by_alias=True)
        )

    # Assert
    assert response.status_code == 401
    assert lab.inbox() == []


async def test_replayed_old_envelope_is_refused(pair: tuple[Node, Node, bytes]) -> None:
    # Arrange：一小时前的消息，签名完全正确
    _, lab, key = pair
    old = Envelope(
        from_=Address(node="laptop", agent="cli"),
        to=Address(node="lab", agent="runner"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="重放攻击"),
        ts=now() - timedelta(hours=1),
        expires_at=now() + timedelta(hours=1),
    )
    signed = sign_envelope(old, key)

    # Act
    async with lab.client() as client:
        response = await client.post("/deliver", json=signed.model_dump(mode="json", by_alias=True))

    # Assert
    assert response.status_code == 401
    assert "时间" in response.json()["detail"]
    assert lab.inbox() == []


async def test_unsigned_envelope_is_refused(pair: tuple[Node, Node, bytes]) -> None:
    _, lab, _ = pair

    async with lab.client() as client:
        response = await client.post(
            "/deliver", json=task_to_lab().model_dump(mode="json", by_alias=True)
        )

    assert response.status_code == 401
    assert lab.inbox() == []


async def test_envelope_addressed_to_another_node_is_refused(
    pair: tuple[Node, Node, bytes],
) -> None:
    """防止把 lab 当成中转跳板往第三方投递。"""
    _, lab, key = pair
    misrouted = Envelope.new(
        sender=Address(node="laptop", agent="cli"),
        recipient=Address(node="elsewhere", agent="runner"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="借道"),
    )

    async with lab.client() as client:
        response = await client.post(
            "/deliver", json=sign_envelope(misrouted, key).model_dump(mode="json", by_alias=True)
        )

    assert response.status_code == 421


async def test_unknown_recipient_agent_is_refused(pair: tuple[Node, Node, bytes]) -> None:
    _, lab, key = pair
    env = Envelope.new(
        sender=Address(node="laptop", agent="cli"),
        recipient=Address(node="lab", agent="ghost"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="发给不存在的人"),
    )

    async with lab.client() as client:
        response = await client.post(
            "/deliver", json=sign_envelope(env, key).model_dump(mode="json", by_alias=True)
        )

    assert response.status_code == 404


async def test_malformed_body_is_a_bad_request_not_a_crash(
    pair: tuple[Node, Node, bytes],
) -> None:
    _, lab, _ = pair

    async with lab.client() as client:
        response = await client.post("/deliver", json={"proto": "1.0", "垃圾": True})

    assert response.status_code == 400


# ---------- 发送端的错误处理 ----------


async def test_sending_to_an_untrusted_peer_never_leaves_the_machine(tmp_path: Path) -> None:
    """未信任的对端连请求都不该发出去 —— 免得把消息内容泄给陌生节点。"""
    laptop = Node(tmp_path / "laptop", "laptop")
    laptop.peers.observe(node="lab", endpoint=ENDPOINT, agents=("runner",))
    calls: list[httpx.Request] = []

    async def spy(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(202, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(spy), base_url=ENDPOINT)
    async with LanTransport(
        node_name="laptop", peers=laptop.peers, client=client, log=laptop.log
    ) as transport:
        with pytest.raises(DeliveryError, match="未信任"):
            await transport.deliver(task_to_lab(), dest())

    assert calls == []


async def test_network_failure_is_retryable(tmp_path: Path) -> None:
    # Arrange：对端不可达
    laptop = Node(tmp_path / "laptop", "laptop")
    laptop.peers.trust(PairingToken(node="lab", endpoint=ENDPOINT, key=new_key()))

    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url=ENDPOINT)

    # Act：网络问题返回可重试的失败，交给 outbox 退避重试，而不是直接死信
    async with LanTransport(
        node_name="laptop", peers=laptop.peers, client=client, log=laptop.log
    ) as transport:
        result = await transport.deliver(task_to_lab(), dest())

    # Assert
    assert not result.ok
    assert "connection refused" in (result.detail or "")


async def test_server_error_is_retryable(tmp_path: Path) -> None:
    laptop = Node(tmp_path / "laptop", "laptop")
    laptop.peers.trust(PairingToken(node="lab", endpoint=ENDPOINT, key=new_key()))

    async def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "重启中"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable), base_url=ENDPOINT)
    async with LanTransport(
        node_name="laptop", peers=laptop.peers, client=client, log=laptop.log
    ) as transport:
        result = await transport.deliver(task_to_lab(), dest())

    assert not result.ok
    assert "503" in (result.detail or "")


# ---------- 回信地址的自动学习 ----------


async def test_receiver_learns_the_sender_return_address(tmp_path: Path) -> None:
    """`invite/trust` 之后被邀请方是没有地址的，回信会发不出去。

    所以投递请求带上自己的地址，收件方校验签名通过后记下来 —— 之后就能双向通信。
    """
    # Arrange
    laptop = Node(tmp_path / "laptop", "laptop")
    lab = Node(tmp_path / "lab", "lab")
    key = new_key()
    laptop.peers.trust(PairingToken(node="lab", endpoint=ENDPOINT, key=key))
    lab.peers.trust(PairingToken(node="laptop", endpoint="", key=key))  # 还不知道往哪回
    assert lab.peers.require_trusted("laptop")[0].endpoint == ""

    # Act
    transport = LanTransport(
        node_name="laptop",
        peers=laptop.peers,
        client=lab.client(),
        log=laptop.log,
        advertise="http://laptop.test:45778",
    )
    async with transport:
        await transport.deliver(task_to_lab(), dest())

    # Assert
    learned = PeerRegistry(lab.layout.root).get("laptop")
    assert learned is not None
    assert learned.endpoint == "http://laptop.test:45778"
    assert learned.trusted  # 学地址不影响信任状态


async def test_a_bogus_return_address_header_is_ignored(pair: tuple[Node, Node, bytes]) -> None:
    _, lab, key = pair
    before = lab.peers.require_trusted("laptop")[0].endpoint

    async with lab.client() as client:
        await client.post(
            "/deliver",
            json=sign_envelope(task_to_lab(), key).model_dump(mode="json", by_alias=True),
            headers={"X-AntHill-Endpoint": "file:///etc/passwd"},
        )

    assert PeerRegistry(lab.layout.root).get("laptop").endpoint == before  # type: ignore[union-attr]


async def test_a_recipient_whose_agentd_has_not_started_is_retryable_not_dead(
    pair: tuple[Node, Node, bytes],
) -> None:
    """「对端晚起 10 秒 = 消息永久进死信」—— 真出过的坑。

    这个 Agent 在对方配置里是**存在**的，只是 agentd 还没把邮箱建出来：
    典型的暂时性故障。以前回 404，客户端判为不可重试直接进死信，
    唯一的恢复手段是手动 mv 文件。而完全一样的情形在 local 传输里一直是可重试的
    —— 两条路不该给出相反的判断。
    """
    _, lab, key = pair
    shutil.rmtree(lab.layout.mailbox_dir("runner"))  # agentd 还没起来过
    env = task_to_lab()

    async with lab.client() as client:
        response = await client.post(
            "/deliver", json=sign_envelope(env, key).model_dump(mode="json", by_alias=True)
        )

    assert response.status_code == 503
    assert response.status_code not in FATAL_STATUS, "客户端会据此判成不可重试，直接死信"


async def test_the_lan_transport_actually_retries_that_case(
    pair: tuple[Node, Node, bytes],
) -> None:
    """端到端：503 落到传输层就该是「可重试的失败」，而不是抛 DeliveryError。"""
    laptop, lab, _ = pair
    shutil.rmtree(lab.layout.mailbox_dir("runner"))
    transport = LanTransport(
        node_name="laptop",
        peers=laptop.peers,
        log=laptop.log,
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=lab.app), base_url=ENDPOINT),
    )

    async with transport:
        result = await transport.deliver(
            task_to_lab(), Destination(node="lab", agent="runner", peer=laptop.peers.get("lab"))
        )

    assert result.ok is False  # 失败，但没抛 —— 也就是会进退避重试
