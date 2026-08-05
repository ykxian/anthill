"""MCP 两个方向。

- **server**：把 AntHill 暴露给 Claude Code 这类客户端（`anthill mcp serve`）；
- **client**：让自己的 Agent 用上外部 MCP server 的工具（`[mcp.*]`）。

两件事解决的是不同问题，别搞混：server 让别人能调 AntHill，
client 让 AntHill 的 Agent 能用别人的工具。后者顺带 retire 了
「工具集偏薄」和「TOOL_FACTORIES 是硬编码字典」两条评审意见 ——
自己造插件发现，是重新发明一个已经有事实标准的东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.config import Config, McpSection
from anthill.core.errors import AntHillError, ConfigError
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace

pytest.importorskip("mcp", reason="没装 mcp extra")

from anthill.mcp.server import build_server

BRIDGE = '\n[agents.cc]\nrole = "worker"\nbridge = true\n'


@pytest.fixture
def node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="box")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8") + BRIDGE, encoding="utf-8"
    )
    return layout, Config.load_from(layout)


def tool_map(server: object) -> dict[str, object]:
    """走公开 API（`list_tools`），不摸内部字段 —— 否则升个版本测试就红。"""
    import asyncio

    return {t.name: t for t in asyncio.run(server.list_tools())}  # type: ignore[attr-defined]


def call(server: object, name: str, args: dict[str, object]) -> object:
    import asyncio

    return asyncio.run(server.call_tool(name, args))  # type: ignore[attr-defined]


def waiting_note(layout: NodeLayout, msg_id: str = "01KZ000000000000000000000A") -> str:
    inbox = layout.agent_dir("cc") / "bridge" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{msg_id}.md").write_text(
        f"---\nfrom: box:coder\nto: box:cc\ntype: chat\nmsg_id: {msg_id}\n---\n"
        "这块接口我想改成异步的，你那边有依赖吗\n",
        encoding="utf-8",
    )
    return msg_id


# ---------- server：Claude Code 能调 ----------


def test_the_server_exposes_the_bridge_operations(node: tuple[NodeLayout, Config]) -> None:
    server = build_server(*node, "cc")

    names = set(tool_map(server))

    assert {"anthill_inbox", "anthill_reply", "anthill_send", "anthill_runs"} <= names


def test_only_bridge_agents_can_be_represented(node: tuple[NodeLayout, Config]) -> None:
    """桥接 Agent 背后是「一个人」（或这个会话），所以它不自己想。
    别的 Agent 有自己的大脑，不该由这个会话代答。"""
    with pytest.raises(AntHillError, match="bridge = true"):
        build_server(*node, "echo")


def test_an_unknown_agent_lists_the_real_ones(node: tuple[NodeLayout, Config]) -> None:
    with pytest.raises(AntHillError, match="cc"):
        build_server(*node, "ghost")


def test_the_inbox_tool_lists_what_is_waiting(node: tuple[NodeLayout, Config]) -> None:
    layout, _ = node
    waiting_note(layout)
    server = build_server(*node, "cc")

    result = call(server, "anthill_inbox", {})

    assert "异步" in str(result)
    assert "box:coder" in str(result)


def test_replying_through_mcp_writes_the_same_file_a_human_would(
    node: tuple[NodeLayout, Config],
) -> None:
    """和网页上回、和手写，落的是同一个文件 —— 三条路完全并存。"""
    layout, _ = node
    msg_id = waiting_note(layout)
    server = build_server(*node, "cc")

    call(server, "anthill_reply", {"message_id": msg_id[-6:], "text": "有依赖，scheduler 里"})

    draft = layout.agent_dir("cc") / "bridge" / "outbox" / f"{msg_id}.md"
    assert draft.is_file()
    assert "scheduler" in draft.read_text(encoding="utf-8")


def test_replying_to_nothing_is_a_clear_message_not_a_crash(
    node: tuple[NodeLayout, Config],
) -> None:
    server = build_server(*node, "cc")

    result = call(server, "anthill_reply", {"message_id": "ZZZZZZ", "text": "?"})

    assert "anthill_inbox" in str(result)  # 告诉模型下一步该调什么


def test_sending_needs_a_recipient_and_a_valid_kind(node: tuple[NodeLayout, Config]) -> None:
    server = build_server(*node, "cc")

    blank = call(server, "anthill_send", {"to": "", "text": "喂"})
    bad_kind = call(server, "anthill_send", {"to": "coder", "text": "在", "kind": "urgent"})
    good = call(server, "anthill_send", {"to": "coder", "text": "这块我来改"})

    assert "不能为空" in str(blank)
    assert "chat 或 task" in str(bad_kind)
    assert "ok" in str(good)


def test_the_server_does_not_expose_dangerous_operations(node: tuple[NodeLayout, Config]) -> None:
    """改配置、启停 agentd、审批的分量是「能在这台机器上执行命令」——
    留在 CLI 和面板的写入口里，不从这条路出去。"""
    names = set(tool_map(build_server(*node, "cc")))

    for forbidden in ("config", "approve", "start", "stop", "secret"):
        assert not any(forbidden in n for n in names), forbidden


# ---------- client：Agent 能用外部工具 ----------


def test_an_agent_can_declare_which_servers_it_may_use(tmp_path: Path) -> None:
    """不默认全给 —— 最小权限比省事重要。"""
    config = Config.model_validate(
        {
            "node": {"name": "n"},
            "mcp": {"files": {"command": ["echo", "hi"]}},
            "agents": {"coder": {"role": "worker", "mcp": ["files"]}, "cli": {"role": "user"}},
        }
    )

    assert config.agents["coder"].mcp == ("files",)
    assert config.agents["cli"].mcp == ()


def test_external_tools_are_high_risk_by_default() -> None:
    """外部工具能干什么我们不知道 —— 策略引擎照常管着它，
    无人值守时 high 直接拒绝。要用就显式降级，由人做这个判断。"""
    assert McpSection(command=["x"]).risk == "high"


def test_declaring_a_server_that_does_not_exist_is_caught_at_load(tmp_path: Path) -> None:
    """「引用了不存在的东西」是配置有效性问题，所以校验在 Config 上 ——
    放在 factory 里会漏掉不走那条分支的 Agent（比如 command 适配器）。"""
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="box")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.coder]\nrole = "worker"\ncommand = ["echo"]\nmcp = ["nope"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="nope"):
        Config.load_from(layout)


async def test_a_server_that_will_not_start_does_not_kill_the_agent(tmp_path: Path) -> None:
    """一个外部依赖挂了，不该让整个 agentd 起不来。"""
    from anthill.agent.tools.mcp_client import McpToolset
    from anthill.core.logging import EventLog

    async with McpToolset(log=EventLog(None, agent="t", echo=False)) as toolset:
        got = await toolset.connect("broken", McpSection(command=["definitely-not-a-command"]))

    assert got == 0
    assert toolset.tools == []


# ---------- 真的连一次外部 server ----------

TINY_SERVER = '''
from mcp.server import MCPServer

server = MCPServer("tiny")


@server.tool()
def add(a: int, b: int) -> int:
    """把两个数加起来。"""
    return a + b


if __name__ == "__main__":
    server.run()
'''


async def test_an_external_server_really_gets_mounted(tmp_path: Path) -> None:
    """**必须真连一次。**

    第一版用假对象测是绿的，真连上才发现入参 schema 的字段名在 mcp 2.0 改了
    （`inputSchema` → `input_schema`）—— 那种改名只有真跑才会现形。
    """
    import sys

    from anthill.agent.tools.base import ToolContext
    from anthill.agent.tools.mcp_client import McpToolset
    from anthill.core.config import SecuritySection
    from anthill.core.logging import EventLog

    script = tmp_path / "tiny_server.py"
    script.write_text(TINY_SERVER, encoding="utf-8")

    async with McpToolset(log=EventLog(None, agent="t", echo=False)) as toolset:
        got = await toolset.connect(
            "tiny", McpSection(command=[sys.executable, str(script)], risk="low")
        )
        assert got == 1
        tool = toolset.tools[0]
        # 名字加了前缀：不和内置工具撞名，日志里也一眼看出这次调用出了本进程
        assert tool.name == "tiny__add"
        assert "a" in tool.parameters.get("properties", {}), "入参 schema 没取到"

        result = await tool.run(
            {"a": 2, "b": 40},
            ToolContext(
                workspace=tmp_path,
                blackboard=tmp_path,
                security=SecuritySection(),
                thread="01J000000000000000000THRD",
            ),
        )

    assert result.ok
    assert "42" in result.content
