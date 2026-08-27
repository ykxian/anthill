"""`anthill mcp serve` —— 以 stdio 起一个 MCP server，供外部 Agent 客户端拉起。"""

from __future__ import annotations

from pathlib import Path

import typer

from anthill.cli.common import console, fail, load
from anthill.core.errors import AntHillError
from anthill.mcp.server import build_server

mcp_app = typer.Typer(help="MCP：把 AntHill 暴露给 Claude Code、Codex 这类客户端")


@mcp_app.command("serve")
def serve(
    agent: str = typer.Argument(
        "", help="代表哪个桥接 Agent；留空 = $ANTHILL_AGENT，再没有就自动认领一个没人占的"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """以 stdio 起一个 MCP server。

    **由客户端拉起，不是你手动跑。** Claude Code、Codex 等支持 stdio MCP 的
    客户端都可以使用；例如配置内容是：

        {"mcpServers": {"anthill": {
            "command": "anthill",
            "args": ["mcp", "serve", "cc", "-w", "/path/to/workspace"]
        }}}

    走 stdio 所以不开端口、不加鉴权面 —— 能起这个进程的人本来就有这台机器的账号，
    和 `anthill bridge` 同一个权限模型。
    """
    layout, config = load(workspace)
    try:
        server = build_server(layout, config, agent)
    except AntHillError as exc:
        fail(str(exc))
    server.run()


@mcp_app.command("tools")
def tools(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    # r-string：`\[` 是给 rich 的转义（不然 [mcp.*] 被当富文本标记吃掉），
    # 普通字符串里它是无效转义 —— Windows 新版 Python 一启动就报 SyntaxWarning
    r"""列出配置里那些外部 MCP server（`\[mcp.*]` 那几节），以及谁在用它们。"""
    _, config = load(workspace)
    if not config.mcp:
        console.print(
            "[dim]还没有配置外部 MCP server。在 node.toml 里写：\n"
            '  [mcp.files]\n  command = ["npx", "-y", '
            '"@modelcontextprotocol/server-filesystem", "/srv/data"]\n'
            '然后给 Agent 加一行 mcp = ["files"]。[/dim]'
        )
        return
    from rich.table import Table

    table = Table(title="外部 MCP server", header_style="bold cyan")
    for column in ("名字", "风险", "命令", "谁在用"):
        table.add_column(column, overflow="fold")
    for name, section in sorted(config.mcp.items()):
        users = [a for a, sec in sorted(config.agents.items()) if name in sec.mcp]
        table.add_row(name, section.risk, " ".join(section.command), ", ".join(users) or "-")
    console.print(table)
    console.print(
        "[dim]风险默认 high —— 外部工具能干什么我们不知道，"
        "无人值守时策略引擎会拒绝。要用就显式降级，由你来做这个判断。[/dim]"
    )
