"""在面板上跟任意一台机器上的任意 Agent 对话，以及远端管理。

远端管理是这个项目里权限最大的一处，所以测试重点在**它默认不存在**、
打开之后仍然要签名、以及每一次改动都留痕。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import new_thread_id, now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType
from anthill.discovery.registry import PeerRegistry
from anthill.security.keys import PairingToken, new_key
from anthill.security.signing import sign_request
from anthill.web.app import create_app
from anthill.web.chat import messages, record_outgoing, threads
from anthill.web.endpoints import CONFIG_PATH

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


def make_node(root: Path, name: str, extra: str = "") -> Bundle:
    layout = NodeLayout(root).ensure_base()
    layout.node_toml.write_text(NODE_TOML.format(name=name) + extra, encoding="utf-8")
    for agent in ("cli", "coder"):
        Mailbox(layout.mailbox_dir(agent)).ensure()
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


@pytest.fixture
def node(tmp_path: Path) -> Bundle:
    return make_node(tmp_path / "laptop", "laptop")


def quiet() -> EventLog:
    return EventLog(None, agent="test", echo=False)


def client_for(
    bundle: Bundle, *, host: str = "127.0.0.1", writable: bool = True, admin: bool = False
) -> httpx.AsyncClient:
    layout, config, peers = bundle
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=quiet(),
        panel=True,
        panel_writable=writable,
        remote_admin=admin,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),  # type: ignore[arg-type]
        base_url="http://laptop.test",
    )


def incoming(thread: str, body: str, *, frm: str = "laptop:coder") -> Envelope:
    node_name, agent = frm.split(":")
    return Envelope(
        from_=Address(node=node_name, agent=agent),
        to=Address(node="laptop", agent="cli"),
        type=MessageType.CHAT,
        thread=thread,
        ts=now(),
        payload=ChatPayload(body=body),
    )


# ---------- 一个会话要看得到一来一回 ----------


def test_a_conversation_shows_both_directions(node: Bundle) -> None:
    """收到的信在邮箱里，**发出去的信不在** —— 它被投到对方邮箱去了。

    所以发的时候必须自己记一笔，否则页面上只能看到半边对话。
    """
    # Arrange
    layout, _, _ = node
    thread = new_thread_id()
    sent = incoming(thread, "我问的", frm="laptop:cli")
    record_outgoing(layout, sent, "这块接口改成异步的行吗")
    Mailbox(layout.mailbox_dir("cli")).deposit(incoming(thread, "行，我这边跟一版"))

    # Act
    history = messages(layout, thread)

    # Assert
    assert [m["body"] for m in history] == ["这块接口改成异步的行吗", "行，我这边跟一版"]
    assert [m["mine"] for m in history] == [True, False]


def test_threads_are_listed_with_who_and_what(node: Bundle) -> None:
    layout, _, _ = node
    thread = new_thread_id()
    record_outgoing(layout, incoming(thread, "x", frm="laptop:cli"), "在吗")

    listed = threads(layout)

    assert len(listed) == 1
    assert listed[0]["thread"] == thread
    assert listed[0]["last"] == "在吗"


def test_a_damaged_line_does_not_destroy_the_conversation(node: Bundle) -> None:
    """会话记录是追加写的 —— 断电写半行不该让整段对话读不出来。"""
    layout, _, _ = node
    thread = new_thread_id()
    record_outgoing(layout, incoming(thread, "x", frm="laptop:cli"), "第一句")
    path = layout.root / "chats" / f"{thread}.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + '{"半行\n', encoding="utf-8")
    record_outgoing(layout, incoming(thread, "y", frm="laptop:cli"), "第三句")

    assert [m["body"] for m in messages(layout, thread)] == ["第一句", "第三句"]


# ---------- 面板的对话接口 ----------


async def test_the_panel_serves_a_conversation(node: Bundle) -> None:
    layout, _, _ = node
    thread = new_thread_id()
    record_outgoing(layout, incoming(thread, "x", frm="laptop:cli"), "你好")

    async with client_for(node) as client:
        body = (await client.get(f"/panel/api/chat/{thread}")).json()

    assert [m["body"] for m in body["messages"]] == ["你好"]


async def test_a_reply_continues_the_same_thread(node: Bundle) -> None:
    """接着上一句往下说，对方的 thread 记忆才接得上。"""
    thread = new_thread_id()

    async with client_for(node) as client:
        body = (
            await client.post(
                "/panel/api/send",
                json={"to": "coder", "body": "接着上一句", "thread": thread},
            )
        ).json()

    assert body["thread"] == thread


async def test_conversations_are_not_exposed_to_the_network(node: Bundle) -> None:
    """面板绑 0.0.0.0 时（跨机投递需要），对话内容不该跟着摊出去。"""
    async with client_for(node, host="10.0.8.99") as client:
        assert (await client.get("/panel/api/chats")).status_code == 403


async def test_a_bogus_thread_id_is_refused(node: Bundle) -> None:
    """thread 会被拼成文件名 —— 只认 ULID，别让它变成一次路径遍历。"""
    async with client_for(node) as client:
        assert (await client.get("/panel/api/chat/....")).status_code == 400
        assert (await client.get("/panel/api/chat/notaulid")).status_code == 400


# ---------- 远端管理：默认根本不存在 ----------


def signed(node_name: str, key: bytes) -> dict[str, str]:
    stamp = now().isoformat()
    return {
        "X-AntHill-Node": node_name,
        "X-AntHill-Ts": stamp,
        "X-AntHill-Sig": sign_request(key, node=node_name, path=CONFIG_PATH, ts=stamp),
    }


@pytest.fixture
def paired(tmp_path: Path) -> tuple[Bundle, bytes]:
    bundle = make_node(tmp_path / "laptop", "laptop")
    key = new_key()
    bundle[2].trust(PairingToken(node="lab", endpoint="", key=key))
    return bundle, key


async def test_remote_admin_is_absent_until_switched_on(paired: tuple[Bundle, bytes]) -> None:
    """默认是 404「这个接口不存在」，不是 403「存在但拒绝你」。

    别给人留一个可以试探的门把手 —— 和面板写入口同一条原则。
    """
    bundle, key = paired

    async with client_for(bundle, admin=False) as client:
        response = await client.get(CONFIG_PATH, headers=signed("lab", key))

    assert response.status_code == 404
    assert "remote_admin" in response.json()["detail"]


async def test_a_trusted_peer_can_read_the_config_once_open(
    paired: tuple[Bundle, bytes],
) -> None:
    bundle, key = paired

    async with client_for(bundle, admin=True) as client:
        response = await client.get(CONFIG_PATH, headers=signed("lab", key))

    assert response.status_code == 200
    assert "[node]" in response.json()["text"]


async def test_a_trusted_peer_can_rewrite_the_config(paired: tuple[Bundle, bytes]) -> None:
    # Arrange
    bundle, key = paired
    layout = bundle[0]
    fresh = NODE_TOML.format(name="laptop") + '\n[agents.newbie]\nrole = "worker"\n'

    # Act
    async with client_for(bundle, admin=True) as client:
        response = await client.put(CONFIG_PATH, headers=signed("lab", key), json={"text": fresh})

    # Assert
    assert response.status_code == 200
    assert "newbie" in layout.node_toml.read_text(encoding="utf-8")
    assert (layout.root / "node.toml.bak").is_file()  # 上一版留着


async def test_an_invalid_config_leaves_the_disk_untouched(
    paired: tuple[Bundle, bytes],
) -> None:
    bundle, key = paired
    before = bundle[0].node_toml.read_text(encoding="utf-8")

    async with client_for(bundle, admin=True) as client:
        response = await client.put(
            CONFIG_PATH, headers=signed("lab", key), json={"text": "这不是 TOML ["}
        )

    assert response.status_code == 400
    assert bundle[0].node_toml.read_text(encoding="utf-8") == before


async def test_an_untrusted_node_cannot_touch_the_config(paired: tuple[Bundle, bytes]) -> None:
    bundle, _ = paired

    async with client_for(bundle, admin=True) as client:
        response = await client.put(
            CONFIG_PATH, headers=signed("stranger", new_key()), json={"text": "x = 1"}
        )

    assert response.status_code == 403


async def test_a_forged_signature_cannot_touch_the_config(paired: tuple[Bundle, bytes]) -> None:
    bundle, _ = paired

    async with client_for(bundle, admin=True) as client:
        response = await client.put(
            CONFIG_PATH, headers=signed("lab", new_key()), json={"text": "x = 1"}
        )

    assert response.status_code == 401


async def test_every_config_change_is_audited(tmp_path: Path) -> None:
    """改坏了配置的话，下一次 agentd 起不来；那时唯一能回答
    「谁在什么时候改的」的就是这条日志。"""
    # Arrange
    bundle = make_node(tmp_path / "laptop", "laptop")
    key = new_key()
    bundle[2].trust(PairingToken(node="lab", endpoint="", key=key))
    layout, config, peers = bundle
    log_file = layout.log_file("serve")
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(log_file, agent="serve:laptop", echo=False),
        remote_admin=True,
    )

    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.9", 1)),  # type: ignore[arg-type]
        base_url="http://laptop.test",
    ) as client:
        await client.put(
            CONFIG_PATH,
            headers=signed("lab", key),
            json={"text": NODE_TOML.format(name="laptop")},
        )

    # Assert
    written = log_file.read_text(encoding="utf-8")
    assert "admin.config_write_attempt" in written
    assert "admin.config_written" in written
    assert '"by": "lab"' in written


# ---------- 客户端一侧：把对端的拒绝翻译成人能看懂的话 ----------


async def remote_against(server: Bundle, caller: Bundle, key: bytes, action: str) -> str:
    """让 caller 通过真的 HTTP 栈去操作 server，返回失败信息。"""
    import anthill.web.remote as remote_mod

    _, config, peers = caller
    app = create_app(
        layout=server[0], config=server[1], peers=server[2], log=quiet(), remote_admin=False
    )
    original = httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=app)  # type: ignore[assignment]
        return original(*args, **kwargs)  # type: ignore[arg-type]

    remote_mod.httpx.AsyncClient = patched  # type: ignore[assignment]
    try:
        if action == "read":
            await remote_mod.read_config(config, peers, "lab")
        else:
            await remote_mod.write_config(config, peers, "lab", "x = 1")
    except AntHillError as exc:
        return str(exc)
    finally:
        remote_mod.httpx.AsyncClient = original  # type: ignore[assignment]
    return ""


@pytest.fixture
def two_nodes(tmp_path: Path) -> tuple[Bundle, Bundle, bytes]:
    """caller 信任 lab；lab 也信任 caller（配对是对称的）。"""
    server = make_node(tmp_path / "lab", "lab")
    caller = make_node(tmp_path / "laptop", "laptop")
    key = new_key()
    server[2].trust(PairingToken(node="laptop", endpoint="", key=key))
    caller[2].trust(PairingToken(node="lab", endpoint="http://lab.test", key=key))
    return server, caller, key


async def test_a_closed_node_explains_exactly_what_to_switch_on(
    two_nodes: tuple[Bundle, Bundle, bytes],
) -> None:
    """「对方没开」是用户最可能撞上的情况，报错必须直接给出下一步。"""
    server, caller, key = two_nodes

    message = await remote_against(server, caller, key, "read")

    assert "remote_admin" in message
    assert "执行命令" in message  # 顺带把代价讲清楚


async def test_an_unpaired_target_says_go_pair_first(tmp_path: Path) -> None:
    import anthill.web.remote as remote_mod

    caller = make_node(tmp_path / "laptop", "laptop")

    with pytest.raises(AntHillError, match=r"配对|不认识|未知"):
        await remote_mod.read_config(caller[1], caller[2], "nobody")


async def test_an_ssh_peer_is_not_administered_through_the_panel(tmp_path: Path) -> None:
    """SSH 那侧的约定是不开任何新端口 —— 不为了面板破例。"""
    import anthill.web.remote as remote_mod

    caller = make_node(tmp_path / "laptop", "laptop")
    caller[2].trust(PairingToken(node="server", endpoint="", key=new_key()))

    with pytest.raises(AntHillError, match="SSH"):
        await remote_mod.read_config(caller[1], caller[2], "server")
