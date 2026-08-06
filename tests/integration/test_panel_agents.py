"""装好就能开面板，以及在面板上管 Agent。

这两件事合起来才让**单机不开终端也能用**：
以前第一步要 `anthill init`，之后每个 Agent 还要一个终端窗口跑 `agent start`。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace, load_or_create
from anthill.discovery.registry import PeerRegistry
from anthill.web.agents import (
    AgentSpec,
    add_agent,
    remove_agent,
    running_pid,
    start_agent,
    stop_agent,
)
from anthill.web.app import create_app

Bundle = tuple[NodeLayout, Config, PeerRegistry]


@pytest.fixture
def node(tmp_path: Path) -> Bundle:
    layout = NodeLayout(tmp_path / "ws")
    config = create_workspace(layout, node_name="box")
    return layout, config, PeerRegistry(layout.root)


def client_for(node: Bundle, *, host: str = "127.0.0.1") -> httpx.AsyncClient:
    layout, config, peers = node
    app = create_app(
        layout=layout,
        config=config,
        peers=peers,
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),  # type: ignore[arg-type]
        base_url="http://panel.test",
    )


# ---------- 装好就能用 ----------


def test_a_workspace_is_created_when_there_is_none(tmp_path: Path) -> None:
    """`serve` 撞上空目录时该把工作区建出来，而不是让人先去跑 init。"""
    layout = NodeLayout(tmp_path / "brand-new")

    config, created = load_or_create(layout)

    assert created is True
    assert layout.node_toml.is_file()
    assert config.node.name  # 默认取主机名
    assert "cli" in config.agents


def test_an_existing_workspace_is_reused_not_clobbered(tmp_path: Path) -> None:
    """已经有配置就绝不能重建 —— 配置被无声覆盖是最难查的事故之一。"""
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="mine")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8") + "\n# 我加的注释\n", encoding="utf-8"
    )

    config, created = load_or_create(layout)

    assert created is False
    assert config.node.name == "mine"
    assert "我加的注释" in layout.node_toml.read_text(encoding="utf-8")


# ---------- 加 / 删 Agent ----------


async def test_adding_an_agent_from_the_panel(node: Bundle) -> None:
    # Arrange
    layout, _, _ = node

    # Act
    async with client_for(node) as client:
        response = await client.post(
            "/panel/api/agents",
            json={"name": "coder", "role": "worker", "brain": "bridge"},
        )

    # Assert
    assert response.status_code == 201, response.text
    fresh = Config.load_from(layout)
    assert fresh.agents["coder"].bridge is True
    assert (layout.root / "node.toml.bak").is_file()  # 上一版留着


async def test_the_template_comments_survive_an_edit(node: Bundle) -> None:
    """那一大堆注释是这个模板最有用的部分，加个 Agent 不该把它们洗掉。"""
    layout, _, _ = node

    async with client_for(node) as client:
        await client.post("/panel/api/agents", json={"name": "coder", "brain": "echo"})

    assert "# AntHill 节点配置" in layout.node_toml.read_text(encoding="utf-8")


async def test_removing_an_agent_keeps_the_rest_of_the_file(node: Bundle) -> None:
    layout, _, _ = node

    async with client_for(node) as client:
        await client.post("/panel/api/agents", json={"name": "coder", "brain": "echo"})
        response = await client.delete("/panel/api/agents/coder")

    assert response.status_code == 200, response.text
    fresh = Config.load_from(layout)
    assert "coder" not in fresh.agents
    assert {"cli", "coordinator", "echo"} <= set(fresh.agents)
    assert "# AntHill 节点配置" in layout.node_toml.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ({"name": "cli", "brain": "echo"}, "重名"),
        ({"name": "Bad Name", "brain": "echo"}, "非法名字"),
        ({"name": "x", "brain": "provider", "provider": "nope"}, "provider 不存在"),
        ({"name": "x", "brain": "command", "command": ""}, "command 是空的"),
    ],
)
async def test_bad_agent_specs_never_touch_the_disk(
    node: Bundle, payload: dict[str, str], because: str
) -> None:
    layout, _, _ = node
    before = layout.node_toml.read_text(encoding="utf-8")

    async with client_for(node) as client:
        response = await client.post("/panel/api/agents", json=payload)

    assert response.status_code in (400, 422), because
    assert layout.node_toml.read_text(encoding="utf-8") == before


def test_a_command_agent_is_written_as_a_toml_array(node: Bundle) -> None:
    layout, config, _ = node

    result = add_agent(layout, config, AgentSpec(name="cc", brain="command", command="claude -p"))

    assert 'command = ["claude", "-p"]' in result["text"]


def test_quotes_in_a_persona_cannot_break_out_of_the_string(node: Bundle) -> None:
    """这段文本会被当配置解析 —— 引号没转义就是一次配置注入。"""
    layout, config, _ = node

    result = add_agent(
        layout,
        config,
        AgentSpec(name="tricky", persona='他说"你好"\n[agents.evil]\nrole = "worker"'),
    )

    import tomllib

    parsed = tomllib.loads(result["text"])
    assert "evil" not in parsed["agents"]


def test_the_last_agent_cannot_be_removed(node: Bundle) -> None:
    layout, config, _ = node
    for name in ("coordinator", "echo"):
        text = remove_agent(layout, config, name)["text"]
        layout.node_toml.write_text(text, encoding="utf-8")
        config = Config.load_from(layout)

    with pytest.raises(AntHillError, match="最后一个"):
        remove_agent(layout, config, "cli")


# ---------- 启 / 停 agentd ----------


async def test_an_agent_can_be_started_and_stopped_from_the_panel(node: Bundle) -> None:
    """单机场景下这是最后一处非用终端不可的事。"""
    # Arrange
    layout, _, _ = node

    # Act
    async with client_for(node) as client:
        started = await client.post("/panel/api/agents/echo/start")
        await _wait_until(lambda: running_pid(layout, "echo") is not None)
        stopped = await client.post("/panel/api/agents/echo/stop")
        await _wait_until(lambda: running_pid(layout, "echo") is None)

    # Assert
    assert started.status_code == 202, started.text
    assert stopped.status_code == 202
    assert running_pid(layout, "echo") is None


async def test_starting_twice_is_harmless(node: Bundle) -> None:
    layout, config, _ = node
    try:
        first = start_agent(layout, config, "echo")
        await _wait_until(lambda: running_pid(layout, "echo") is not None)
        second = start_agent(layout, config, "echo")
        assert second["already"] is True
        assert second["pid"] == running_pid(layout, "echo")
    finally:
        stop_agent(layout, "echo")
        await _wait_until(lambda: running_pid(layout, "echo") is None)
    assert first["already"] is False


def test_stopping_something_that_is_not_running_is_not_an_error(node: Bundle) -> None:
    layout, _, _ = node

    assert stop_agent(layout, "echo")["already"] is True


def test_a_stale_pid_is_not_mistaken_for_a_running_agent(node: Bundle) -> None:
    """机器重启后 runtime.json 还在，pid 却早就是别人的了（或者没人用）。"""
    layout, _, _ = node
    path = layout.agent_dir("echo") / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": "echo", "pid": 2**22}), encoding="utf-8")

    assert running_pid(layout, "echo") is None


async def test_starting_an_unknown_agent_is_refused(node: Bundle) -> None:
    async with client_for(node) as client:
        assert (await client.post("/panel/api/agents/ghost/start")).status_code == 400


async def test_the_network_cannot_start_processes(node: Bundle) -> None:
    """启动进程比改配置还直接 —— 只允许本机。"""
    async with client_for(node, host="10.0.8.9") as client:
        assert (await client.post("/panel/api/agents/echo/start")).status_code == 403
        assert (await client.post("/panel/api/agents", json={"name": "x"})).status_code == 403


async def _wait_until(check: object, timeout: float = 15.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():  # type: ignore[operator]
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等超时了")


# ---------- 加完之后，别的地方要立刻看得见 ----------


async def test_an_agent_added_from_the_panel_shows_up_immediately(node: Bundle) -> None:
    """真出过的 bug：面板加完 Agent，配置文件确实改了，
    可 serve 手里还捧着启动那一刻读到的那份 —— 新 Agent 在面板上根本不出现。"""
    async with client_for(node) as client:
        await client.post("/panel/api/agents", json={"name": "coder", "brain": "echo"})
        body = (await client.get("/panel/api/state")).json()

    assert "coder" in [a["name"] for a in body["agents"]]


async def test_an_agent_added_from_the_panel_can_receive_lan_messages(node: Bundle) -> None:
    """比上面那条更糟的一面：`/deliver` 也拿旧 config 判收件人，
    于是新 Agent **收不了跨机消息**（对方拿到 404「本节点没有这个 Agent」）。"""
    # Arrange
    from anthill.core.envelope import Address, Envelope
    from anthill.core.ids import now
    from anthill.core.payloads import ChatPayload, MessageType
    from anthill.security.keys import PairingToken, new_key
    from anthill.security.signing import sign_envelope

    layout, _, peers = node
    key = new_key()
    peers.trust(PairingToken(node="lab", endpoint="", key=key))

    async with client_for(node) as client:
        await client.post("/panel/api/agents", json={"name": "coder", "brain": "echo"})

        envelope = sign_envelope(
            Envelope(
                from_=Address(node="lab", agent="cli"),
                to=Address(node="box", agent="coder"),
                type=MessageType.CHAT,
                thread="01J0000000000000000000000A",
                ts=now(),
                payload=ChatPayload(body="给刚加的那个 Agent"),
            ),
            key,
        )
        # Act
        response = await client.post("/deliver", json=envelope.model_dump(mode="json"))

    # Assert
    assert response.status_code == 202, response.text
    from anthill.core.mailbox import Mailbox

    assert len(Mailbox(layout.mailbox_dir("coder")).list_new()) == 1


def test_a_broken_config_does_not_take_the_node_down(node: Bundle) -> None:
    """改坏了就继续用上一份好的 —— 别让一次手滑弄停整个节点。"""
    from anthill.core.workspace import ConfigRef

    layout, config, _ = node
    ref = ConfigRef(layout, config)
    assert "echo" in ref.current.agents

    layout.node_toml.write_text("这不是 TOML [", encoding="utf-8")

    assert "echo" in ref.current.agents  # 还是上一份好的


def test_the_panel_does_not_call_a_bridge_agent_echo(node: Bundle) -> None:
    """背后是个人却显示成 echo，会让人以为它不干活。

    这个 bug 在 CLI 的 `agent list` 上修过一次，面板又犯了一遍 ——
    因为判定逻辑有两份。现在共用 `core/config.brain_of`。
    """
    from anthill.web.panel import build_snapshot

    layout, _, peers = node
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.cc]\nrole = "worker"\nbridge = true\n'
        + '\n[agents.term]\nrole = "worker"\ncommand = ["claude", "-p"]\n',
        encoding="utf-8",
    )
    fresh = Config.load_from(layout)

    brains = {a["name"]: a["provider"] for a in build_snapshot(layout, fresh, peers)["agents"]}

    assert brains["cc"] == "bridge"
    assert brains["term"] == "claude"
    assert brains["echo"] == "echo"


# ---------- 在面板上当那个「人」 ----------


def bridge_node(tmp_path: Path) -> Bundle:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="box")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.cc]\nrole = "worker"\nbridge = true\n',
        encoding="utf-8",
    )
    return layout, Config.load_from(layout), PeerRegistry(layout.root)


def waiting_note(layout: NodeLayout, msg_id: str = "01KZ000000000000000000000A") -> str:
    """伪造一条「在等人回」的消息 —— agentd 收到消息时写的就是这种文件。"""
    inbox = layout.agent_dir("cc") / "bridge" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{msg_id}.md").write_text(
        f"---\nfrom: box:cli\nto: box:cc\ntype: chat\nthread: T1\nmsg_id: {msg_id}\n---\n"
        "这块接口我想改成异步的，你那边有依赖吗\n",
        encoding="utf-8",
    )
    return msg_id


async def test_the_panel_shows_what_the_bridge_agent_is_waiting_on(tmp_path: Path) -> None:
    """加它的地方和用它的地方该是同一个地方。

    以前在网页上加完 bridge Agent 之后，页面上什么都看不到 ——
    还得去终端交代一遍「盯着那个目录」。
    """
    node = bridge_node(tmp_path)
    waiting_note(node[0])

    async with client_for(node) as client:
        body = (await client.get("/panel/api/bridge/cc")).json()

    assert len(body["waiting"]) == 1
    assert "异步" in body["waiting"][0]["body"]
    assert body["waiting"][0]["frm"] == "box:cli"
    assert body["dir"].endswith("agents/cc/bridge")
    # 给会话粘的那句话现在是个**真的监控循环**（一条会阻塞的命令），
    # 而不是「盯着这个目录」—— 后者会话根本没理由主动去看
    assert "--wait" in body["prompt"]
    assert "--reply" in body["prompt"]


async def test_replying_from_the_panel_writes_the_same_file_a_human_would(
    tmp_path: Path,
) -> None:
    """页面上回的那句，落成的还是 outbox 里那个文件 ——
    所以和盯着目录的 Claude Code 会话完全并存，不是另起一套。"""
    node = bridge_node(tmp_path)
    layout = node[0]
    msg_id = waiting_note(layout)

    async with client_for(node) as client:
        response = await client.post(
            f"/panel/api/bridge/cc/reply/{msg_id}", json={"text": "有依赖，scheduler 里同步调的"}
        )

    assert response.status_code == 201, response.text
    draft = layout.agent_dir("cc") / "bridge" / "outbox" / f"{msg_id}.md"
    assert draft.is_file()
    assert "scheduler" in draft.read_text(encoding="utf-8")


async def test_speaking_up_from_the_panel_needs_a_recipient(tmp_path: Path) -> None:
    node = bridge_node(tmp_path)

    async with client_for(node) as client:
        blank = await client.post("/panel/api/bridge/cc/speak", json={"text": "喂"})
        good = await client.post(
            "/panel/api/bridge/cc/speak", json={"to": "coder", "text": "这块我来改，你别动"}
        )

    assert blank.status_code == 400
    assert good.status_code == 201
    drafts = list((node[0].agent_dir("cc") / "bridge" / "outbox").glob("*.md"))
    assert len(drafts) == 1
    assert "to: coder" in drafts[0].read_text(encoding="utf-8")


async def test_a_non_bridge_agent_has_no_bridge_inbox(tmp_path: Path) -> None:
    node = bridge_node(tmp_path)

    async with client_for(node) as client:
        response = await client.get("/panel/api/bridge/echo")

    assert response.status_code == 404
    assert "bridge = true" in response.json()["detail"]


async def test_replying_to_something_nobody_asked_is_refused(tmp_path: Path) -> None:
    """回一条不存在的消息 = 往 outbox 里塞一个没人认领的文件，直接拦掉。"""
    node = bridge_node(tmp_path)

    async with client_for(node) as client:
        response = await client.post(
            "/panel/api/bridge/cc/reply/01KZ000000000000000000000Z", json={"text": "?"}
        )

    assert response.status_code == 400


async def test_a_reply_cannot_be_steered_out_of_the_bridge_directory(tmp_path: Path) -> None:
    """`msg_id` 从 URL 来，下一步就是个文件名 —— 一个 `../` 就能写到目录外面去。

    能走到这个接口的人本来就有写权限（等价于能在这台机器上执行命令），
    所以这不是提权；但边界上的东西就该在边界上校验，不靠调用方老实。
    """
    node = bridge_node(tmp_path)
    outside = tmp_path / "偷偷写到这儿.md"
    outside.write_text("原文", encoding="utf-8")
    escape = f"../../../../../{outside.stem}"

    async with client_for(node) as client:
        response = await client.post(
            f"/panel/api/bridge/cc/reply/{escape}", json={"text": "覆盖掉"}
        )

    assert response.status_code in (400, 404)
    assert outside.read_text(encoding="utf-8") == "原文"
