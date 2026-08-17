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
        assert (await client.get("/panel/api/state")).status_code == 200  # 本机读照旧


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
    # 状态里有编排任务的正文和最近的日志 —— 和 /node/summary 给对端看的是同一批
    # 东西，那边要签名，这边不该对整个网段裸奔。没有令牌就是 403。
    assert state.status_code == 403
    assert inbox(layout, "boss") == []


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_clients_are_recognised(host: str) -> None:
    assert is_local_client(host)


@pytest.mark.parametrize("host", ["10.0.8.9", "example.com", "", None])
def test_everything_else_is_not_local(host: str | None) -> None:
    assert not is_local_client(host)


# ---------- CLI 开关 ----------


def test_panel_write_works_alongside_listening_for_other_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨机投递必须绑对外，写权限却只对本机开放 —— 这两件事以前在启动期是互斥的，
    结果总控那台没法既收别的机器的消息、又在面板上操作。

    现在不互斥了：守门的是**逐请求**的来源校验
    （见 test_a_non_local_client_is_refused），启动期那道额外限制
    挡不住任何真实攻击，只挡住了正常用法。
    """
    # Arrange：把真正起服务那一步换掉，只测参数校验这一关
    from typer.testing import CliRunner

    import anthill.cli.serve_cmd as serve_mod

    started: dict[str, object] = {}

    async def fake_serve(*args: object, **kwargs: object) -> None:
        started.update(kwargs)

    monkeypatch.setattr(serve_mod, "_serve", fake_serve)
    runner = CliRunner()
    assert runner.invoke(cli_app, ["init", str(tmp_path), "--node-name", "n"]).exit_code == 0

    # Act
    # --port 0 让端口预检拿一个空闲端口，测试就不依赖 45778 有没有被别的东西占着
    result = runner.invoke(
        cli_app,
        ["serve", "--host", "0.0.0.0", "--port", "0", "--panel-write", "-w", str(tmp_path)],
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert started["panel_write"] is True
    assert "只对本机开放" in result.output  # 而且要把这件事说出来，别让人猜


def test_panel_write_requires_the_panel_to_be_on(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    assert runner.invoke(cli_app, ["init", str(tmp_path), "--node-name", "n"]).exit_code == 0

    result = runner.invoke(cli_app, ["serve", "--no-panel", "--panel-write", "-w", str(tmp_path)])

    assert result.exit_code == 1


# ---------- 跨站发起的请求 ----------


def lan_or_local(node: tuple[NodeLayout, Config, PeerRegistry], host: str) -> httpx.AsyncClient:
    layout, config, peers = node
    application = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=(host, 5555)),
        base_url="http://panel.test",
    )


async def test_a_page_on_another_site_cannot_drive_the_panel(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """纵深防御：即使请求确实来自本机（浏览器就是在本机跑的），
    发起它的如果是别的站点，也不放行。"""
    async with lan_or_local(node, "127.0.0.1") as client:
        run = await client.post(
            "/panel/api/run", json={"task": "x"}, headers={"Origin": "http://evil.example"}
        )
        cluster = await client.get("/panel/api/cluster", headers={"Origin": "http://evil.example"})

    assert run.status_code == 403
    assert cluster.status_code == 403


async def test_the_panel_page_itself_is_allowed(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """页面自己发的请求是同源的，不能被误伤。"""
    async with lan_or_local(node, "127.0.0.1") as client:
        response = await client.get("/panel/api/config", headers={"Origin": "http://panel.test"})

    assert response.status_code == 200


async def test_a_request_without_an_origin_still_works(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """curl 和脚本不带 Origin —— 它们照样得先过「来自本机」那一关，不是白给。"""
    async with lan_or_local(node, "127.0.0.1") as client:
        assert (await client.get("/panel/api/config")).status_code == 200


async def test_the_lan_is_still_refused_even_with_a_convincing_origin(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """真正守门的是连接来源，头部怎么伪造都没用。"""
    async with lan_or_local(node, "10.0.8.9") as client:
        response = await client.put(
            "/panel/api/config",
            json={"text": "x = 1"},
            headers={"Origin": "http://panel.test"},
        )

    assert response.status_code == 403


# ---------- 移除对端 ----------
#
# 侧栏里那排「连不上 · 配对」的节点多半是早前测试留下的发现记录，
# 以前只能去终端 `anthill peers forget` 一个个清 —— 面板上看得见的垃圾，
# 面板上就该能扫。但**已信任的对端不在此列**：删它等于拒收它今后的一切
# 消息，这种级别的动作留在 CLI。


def see_ghost(peers: PeerRegistry, name: str = "ghost") -> None:
    peers.observe(node=name, endpoint="http://10.0.0.9:1", agents=("echo",))


async def test_a_discovered_peer_can_be_forgotten_from_the_page(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, _, peers = node
    see_ghost(peers)

    async with client_for(node) as client:
        response = await client.delete("/panel/api/peers/ghost")

    assert response.status_code == 200
    assert response.json()["removed"] == ["laptop"]
    assert all(p.node != "ghost" for p in PeerRegistry(layout.root).all()), "对端没被移除"


async def test_forgetting_an_unknown_peer_says_so(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with client_for(node) as client:
        response = await client.delete("/panel/api/peers/nobody")

    assert response.status_code == 404


async def test_a_trusted_peer_cannot_be_forgotten_from_the_page(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """删已信任的对端 = 拒收它今后的一切消息 —— 这一刀留在 CLI。"""
    from anthill.security.keys import PairingToken, new_key

    layout, _, peers = node
    peers.trust(PairingToken(node="friend", endpoint="http://10.0.0.8:1", key=new_key()))

    async with client_for(node) as client:
        response = await client.delete("/panel/api/peers/friend")

    assert response.status_code == 409
    assert any(p.node == "friend" for p in PeerRegistry(layout.root).all()), "已信任的对端被误删"


async def test_forgetting_a_peer_requires_write_access(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    _, _, peers = node
    see_ghost(peers)

    async with client_for(node, writable=False) as client:
        assert (await client.delete("/panel/api/peers/ghost")).status_code in (403, 404, 405)


async def test_browsing_your_own_lan_ip_counts_as_local(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """本机浏览器里打开的是本机自己的局域网 IP —— TCP 源地址是这台机器
    自有的地址，包根本没出过这台机器（伪造它无法完成握手）。以前只认回环：
    serve 自己打印的面板地址，在它自己的机器上打开反而 403、WebSocket 拒绝
    握手，页面永远「已断开，重连中」—— Windows 实机第一步就栽在这。
    判据：这条连接的对端地址 == 这条连接的本端地址。
    """
    async with lan_or_local(node, "panel.test") as client:  # 与 base_url 同名 = 连自己
        response = await client.post("/panel/api/run", json={"task": "自己的 IP 也算本机"})

    assert response.status_code == 202


async def test_other_lan_hosts_are_still_refused(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    async with lan_or_local(node, "10.0.8.9") as client:
        response = await client.post("/panel/api/run", json={"task": "别人的机器"})

    assert response.status_code == 403


# ---------- 配置页的图形视图 ----------


async def test_the_config_comes_with_a_parsed_view(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """页面要画概览卡片和常用字段表单，不能靠前端自己解析 TOML ——
    服务端本来就会解析，顺手把结构给出去。"""
    async with client_for(node) as client:
        body = (await client.get("/panel/api/config")).json()

    assert body["parsed"]["node"]["name"] == "laptop"
    assert "agents" in body["parsed"]


async def test_a_broken_config_still_shows_its_text(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """文件被改坏时更需要打开这一页修 —— 原文必须照常给，图形视图标不可用。"""
    layout, _, _ = node

    async with client_for(node) as client:
        # 先起服务再改坏文件 —— 真实场景就是运行中被改坏
        layout.node_toml.write_text("这不是 toml [", encoding="utf-8")
        body = (await client.get("/panel/api/config")).json()

    assert "这不是 toml" in body["text"]
    assert body["parsed"] is None


# ---------- 保存配置的并发防护 ----------


async def test_a_stale_base_turns_the_save_into_a_409(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    """diff 是对着某一版磁盘看的 —— 点保存前磁盘又变了的话，静默覆盖等于
    把别人刚写的改动扔掉（.bak 只留最后一版）。带上基准文本，不匹配就 409。"""
    layout, _, _ = node
    async with client_for(node) as client:
        current = (await client.get("/panel/api/config")).json()["text"]
        response = await client.put(
            "/panel/api/config",
            json={
                "text": current + "\n# 我的改动\n",
                "base_text": current + "\n# 别人抢先写进去的\n",
            },
        )

    assert response.status_code == 409
    assert layout.node_toml.read_text(encoding="utf-8") == current, "409 时磁盘一个字都不能动"


async def test_a_matching_base_saves_normally(
    node: tuple[NodeLayout, Config, PeerRegistry],
) -> None:
    layout, _, _ = node
    async with client_for(node) as client:
        current = (await client.get("/panel/api/config")).json()["text"]
        response = await client.put(
            "/panel/api/config",
            json={"text": current + "\n# 合法的追加\n", "base_text": current},
        )

    assert response.status_code == 200
    assert "# 合法的追加" in layout.node_toml.read_text(encoding="utf-8")
