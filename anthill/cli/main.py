"""AntHill CLI 入口。

anthill init            初始化工作区
anthill agent start/list 守护进程
anthill run             把任务交给 coordinator，多 Agent 协同完成
anthill send            投递消息
anthill serve           节点接收端：LAN 投递端点 + 可选组播信标
anthill peers           对端节点与信任关系
anthill approve         批准远端 agentd 停下来等确认的危险操作
anthill fetch           按需拉取远端产物
anthill pull            取回远端替我们暂存的回信
anthill status          节点总览
anthill log             结构化日志
"""

from __future__ import annotations

import typer

from anthill import __version__
from anthill.cli.agent_cmd import agent_app
from anthill.cli.common import console
from anthill.cli.init_cmd import init_command
from anthill.cli.log_cmd import log_command
from anthill.cli.msg_cmd import send_command
from anthill.cli.peers_cmd import peers_app
from anthill.cli.remote_cmd import approve_command, fetch_command, pull_command
from anthill.cli.run_cmd import run_command
from anthill.cli.serve_cmd import serve_command
from anthill.cli.status_cmd import status_command

app = typer.Typer(
    name="anthill",
    help="AntHill — 基于文件邮箱的分布式多 Agent 协同框架",
    no_args_is_help=True,
    add_completion=False,
)

app.command("init")(init_command)
app.command("run")(run_command)
app.command("send")(send_command)
app.command("serve")(serve_command)
app.command("approve")(approve_command)
app.command("fetch")(fetch_command)
app.command("pull")(pull_command)
app.command("status")(status_command)
app.command("log")(log_command)
app.add_typer(agent_app, name="agent")
app.add_typer(peers_app, name="peers")


@app.command("version")
def version() -> None:
    """打印版本号。"""
    console.print(f"anthill {__version__}")


if __name__ == "__main__":
    app()
