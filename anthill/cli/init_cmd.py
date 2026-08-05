"""`anthill init` —— 在当前目录建出工作区骨架。

注意这不再是使用前的**必经**一步：`anthill serve` 找不到工作区会自己建一个，
所以新机器上装好就能直接开面板。这个命令留着，是因为「我想明确指定建在哪、
叫什么名字」仍然是个合理需求。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import typer

from anthill.cli.common import console, fail, ok
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace, suggest_node_name
from anthill.web.workspaces import remember

DEFAULT_PORT = 45778
"""和 serve 的默认端口一致；这里只是清单里的一个提示值，不影响实际监听。"""


def init_command(
    path: Path = typer.Argument(Path("."), help="工作区目录，默认当前目录"),
    node_name: str = typer.Option("", "--node-name", "-n", help="节点名，默认取主机名"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 node.toml"),
) -> None:
    """初始化 .anthill 工作区（node.toml + agents/ + blackboard/ + logs/）。"""
    layout = NodeLayout(path.resolve())
    name = node_name or suggest_node_name()
    try:
        config = create_workspace(layout, node_name=name, force=force)
    except AntHillError as exc:
        fail(f"{exc}（要重建请加 --force）" if "已存在" in str(exc) else str(exc))

    # 记进机器级清单：serve 才看得见它，下一次 init 的重名检查也才看得见它
    with suppress(AntHillError):
        remember(layout.workspace, port=DEFAULT_PORT)

    ok(f"工作区已就绪：{layout.root}")
    console.print(f"  节点名   [b]{name}[/b]")
    console.print(f"  Agent    {', '.join(config.agents) or '（无）'}")
    console.print("\n下一步：")
    console.print("  [dim]终端 1[/dim] anthill serve --panel-write   [dim]# 然后在面板上操作[/dim]")
    console.print("  [dim]终端 2[/dim] anthill agent start echo")
