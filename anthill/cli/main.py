"""AntHill CLI 入口。命令地图见 `COMMAND_MAP` / `anthill guide`。"""

from __future__ import annotations

import typer

from anthill import __version__
from anthill.cli.agent_cmd import agent_app
from anthill.cli.chat_cmd import bridge_command, chat_command, talk_command
from anthill.cli.common import console
from anthill.cli.cost_cmd import cost_command
from anthill.cli.dead_cmd import dead_app
from anthill.cli.doctor_cmd import doctor_command
from anthill.cli.init_cmd import init_command
from anthill.cli.log_cmd import log_command
from anthill.cli.msg_cmd import send_command
from anthill.cli.peers_cmd import peers_app
from anthill.cli.remote_cmd import approve_command, fetch_command, pull_command
from anthill.cli.run_cmd import run_command
from anthill.cli.runs_cmd import runs_command
from anthill.cli.serve_cmd import serve_command
from anthill.cli.status_cmd import status_command

COMMAND_MAP = """AntHill 命令地图 —— 按「你想干什么」分组

先跑起来
  init      建一个工作区（node.toml + agents/ + blackboard/ + logs/）
  doctor    体检：配置、密钥、邮箱、在跑的进程，一次看完
  serve     起接收端 + Web 面板（单机也用它，面板上什么都能做）

干活
  run       把任务交给 coordinator，看它拆解、派活、汇总
  send      投一条消息给某个 Agent
  chat      跟一个 Agent 多轮对话
  talk      让两个 Agent 就一件事聊下去，你旁观
  bridge    看看桥接 Agent 那边有什么在等你（人就是那个 Agent）

看情况
  status    本工作区总览：谁在跑、积压、死信、对端
  agent ps  这台机器上「所有」工作区里在跑的 agentd
  runs      编排任务与每一步的产物、耗时、重试
            （anthill run 按 Ctrl-C 退出观察之后，从这儿接着看）
  cost      token 用量与花费
  log       结构化日志（JSON Lines）
  dead      死信：看看什么没送出去，修好之后重投

跨机
  peers     对端节点与信任关系（配对用 peers pair，念六位 PIN）
  approve   批准远端 agentd 停下来等的危险操作
  fetch     按需拉取远端产物
  pull      取回远端替我们暂存的回信（serve 会自动拉，这条是手动补一次）

每条都有 --help。想在浏览器里做同样的事：anthill serve --panel-write
"""
"""按场景分组的命令地图。

以前它是这个模块的 docstring —— **`--help` 里看不到**，而它恰恰是新用户最需要的东西。
rich 渲染 group help 时只取第一段，塞进 `help=` 也显示不全，
所以单独做成 `anthill guide`，并从 `--help` 的第一行指过去。
"""

app = typer.Typer(
    name="anthill",
    help="AntHill — 基于文件邮箱的分布式多 Agent 协同框架。第一次用：anthill guide",
    no_args_is_help=True,
    # shell 补全值得有：命令、Agent 名、节点名都是记不住的东西
    add_completion=True,
)

app.command("init")(init_command)
app.command("doctor")(doctor_command)
app.command("run")(run_command)
app.command("send")(send_command)
app.command("chat")(chat_command)
app.command("talk")(talk_command)
app.command("bridge")(bridge_command)
app.command("serve")(serve_command)
app.command("approve")(approve_command)
app.command("fetch")(fetch_command)
app.command("pull")(pull_command)
app.command("status")(status_command)
app.command("cost")(cost_command)
app.command("runs")(runs_command)


def _version(show: bool) -> None:
    """`--version` 是所有人的肌肉记忆 —— 只有 `anthill version` 子命令不够。"""
    if show:
        console.print(f"anthill {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="打印版本号后退出"
    ),
) -> None:
    pass


@app.command("guide")
def guide() -> None:
    """按场景分组的命令地图 —— 第一次用先看这个。"""
    console.print(COMMAND_MAP)


app.command("log")(log_command)
app.add_typer(agent_app, name="agent")
app.add_typer(dead_app, name="dead")
app.add_typer(peers_app, name="peers")


@app.command("version")
def version() -> None:
    """打印版本号。"""
    console.print(f"anthill {__version__}")


if __name__ == "__main__":
    app()
