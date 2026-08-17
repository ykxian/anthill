"""「怎么把一个终端会话接进来」—— 那几段可以直接粘出去的命令。

从 `web/bridge_panel.py` 里搬出来的：CLI 也要用同一段值守提示词
（`anthill bridge <名字> --prompt`），而让 `cli/` 反过来 import `web/` 是错的方向。
这里只生成文本，不碰网络也不碰面板。
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from anthill.adapters.bridge import BRIDGE_DIR
from anthill.core.paths import NodeLayout


def anthill_exe() -> str:
    """这台机器上 `anthill` 的可执行路径。

    **不能直接写 `anthill`** —— 它在不在 PATH 上取决于装法（`uv run` 装出来的
    只在 venv 里）。页面上给的命令要能原样粘出去就跑得通，所以这里给绝对路径。
    """
    candidate = Path(sys.executable).with_name("anthill")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("anthill") or "anthill"


def watch_prompt(layout: NodeLayout, agent: str) -> str:
    """给值守会话的那段话 —— 一个**不占着会话、也不会自己醒**的监控循环。

    走过三版，前两版都不好：

    1. 「盯着这个目录，出现新 .md 就读」—— 那不是监控，是「你想起来了看一眼」。
    2. 前台 `--wait 300` —— 是真的在等了，但**整个会话被占住五分钟**，
       期间你连让它停下来都做不到。
    3. 后台 `--wait 600` —— 会话回来了，但每十分钟必醒一次：没消息也要烧掉
       一轮对话去发现「没事」，然后再挂一条。

    现在是 `--wait -1`：**永不超时**。值守本来就该一直挂着，
    有消息才醒。没有终点，也就没有白醒。
    """
    exe = anthill_exe()
    workspace = str(layout.workspace)
    root = layout.agent_dir(agent) / BRIDGE_DIR
    wait = f"{exe} bridge {agent} --wait -1 --json -w {workspace}"
    return (
        f"你现在是 AntHill 协作网络里的 Agent「{agent}」。\n"
        f"请立刻**在后台**运行这条命令（Bash 工具的 run_in_background，不要在前台等）：\n"
        f"  {wait}\n"
        "它会一直挂着直到有人找你（不会超时），有消息时退出，那时你会被自动唤起。\n"
        "\n"
        "被唤起后：读那条消息 → 回复 → **再往后台放一条同样的命令**，如此循环：\n"
        f'  {exe} bridge {agent} --reply <消息id> --text "你的回复" -w {workspace}\n'
        "纯回执（「收到」「无需回复」这类）**别回** —— 回了对方还得再回执，没有终点；\n"
        f"用 --ack 清掉它再重新挂监听：{exe} bridge {agent} --ack <消息id> -w {workspace}\n"
        "\n"
        "等消息的时候我照常可以让你干别的活，你不用一直卡在等待里。\n"
        f'想主动找别人说话：{exe} bridge {agent} --to <对方> --text "..." -w {workspace}\n'
        f"（这些命令读写的就是 {root} 下的文件，你也可以直接编辑。）"
    )


def launch_command(layout: NodeLayout, agent: str) -> str:
    """一条命令起一个已经在值守的会话。

    **这解决的是「起来之后还要手动发一条它才开始听」。** Claude Code 是回合制的：
    没有用户回合，模型不会动 —— 所以 hook 也好、MCP 也好，都只能等你先说话。
    唯一能在启动那一刻就推动它的，是**把提示词当作首个提问传进去**。

    提示词不内联而是 `$(... --prompt)` 现取：内联的话，这段话每改一次，
    先前粘出去的命令就旧一次；而且它有换行有引号，塞进命令行迟早出事。
    """
    exe = anthill_exe()
    workspace = str(layout.workspace)
    inner = f"{exe} bridge {agent} --prompt -w {workspace}"
    return f'ANTHILL_AGENT={agent} claude "$({inner})"'


def connect_recipes(layout: NodeLayout, agent: str) -> dict[str, Any]:
    """把「怎么让一个终端会话接进来」写成可以直接粘的几段。

    路径全部由服务端填好：`anthill` 在不在 PATH 上、工作区在哪，
    页面猜不出来，而猜错的结果是粘过去跑不通。
    """
    exe = anthill_exe()
    workspace = str(layout.workspace)
    hook = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "command": f"{exe} bridge --json -w {workspace}",
                    "description": "每轮开始前看看 AntHill 那边有没有消息在等这个会话",
                }
            ]
        }
    }
    return {
        "exe": exe,
        "workspace": workspace,
        "agent": agent,
        # 一条命令起一个**已经在值守**的会话 —— 见 launch_command
        "launch": launch_command(layout, agent),
        # **不是 `~/.claude/settings.json`。** 那是全局的：开的每个会话都会去查收件箱，
        # 而通常只有一两个要接进来。写进项目目录才配得上「只对这个目录生效」。
        "hook_path": f"{workspace}/.claude/settings.local.json",
        "hook": json.dumps(hook, ensure_ascii=False, indent=2),
        # **装在当前目录，不装全局。** `--scope local` 是每个项目目录一份，
        # 所以要先 cd 过去。全局装法（`--scope user`）会让你开的**每一个**
        # Claude Code 都挂上这台 server，而通常只有一两个需要接进来。
        #
        # 命令里**不写 Agent 名** —— 让每个会话自己认领。写死名字的话，
        # 同一份配置下开几个会话就有几个抢同一个 Agent。
        "mcp": f"cd {workspace} && claude mcp add anthill -- {exe} mcp serve -w {workspace}",
        "pin": f"ANTHILL_AGENT={agent} claude",
        # 一个会话挂多个工作区：**再加一台 server 就行**，名字不同即可。
        # MCP 客户端按 server 名给工具分命名空间，两套 anthill_* 不会撞；
        # 而每台 server 的自我介绍里都写着自己是哪个节点、哪个工作区。
        "multi": (
            f"claude mcp add anthill-{Path(workspace).name} -- {exe} mcp serve -w {workspace}"
        ),
    }


def quoted(text: str) -> str:
    """给 shell 用的安全引号 —— 值守提示词里有换行、反引号和双引号。"""
    return shlex.quote(text)
