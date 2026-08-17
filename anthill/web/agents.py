"""在面板上管 Agent：加、删、启、停。

这一层存在的理由：不加它的话，单机用这个项目仍然离不开终端 ——
每个 Agent 要开一个窗口跑 `anthill agent start`，加一个 Agent 要手改 TOML。
面板能看不能动，就只是个仪表盘。

两个刻意的选择：

1. **改配置走文本，不走「解析成对象再写回」。** 标准库只有 tomllib，能读不能写；
   而且 node.toml 里那一大堆注释是给人看的，往返一次就全没了。
   所以加是追加一段、删是按节删行，**写完再用启动期同一套模型校验**，
   不合法就原样退回、磁盘一个字不动。
2. **启动 agentd 用 `sys.executable -m anthill`**，不用 `anthill` 这个名字。
   它在不在 PATH 上取决于装法，而 sys.executable 一定是当前这套环境。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.config import Config
from anthill.core.envelope import AGENT_NAME_RE
from anthill.core.errors import AntHillError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.procs import detach_kwargs

RUNTIME_FILE = "runtime.json"
STOP_GRACE = signal.SIGTERM
BRAINS = ("echo", "bridge", "command", "provider")


class AgentSpec(BaseModel):
    """面板上「加一个 Agent」的表单。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    role: str = Field(default="worker", max_length=64)
    brain: Literal["echo", "bridge", "command", "provider"] = "echo"
    provider: str = Field(default="", max_length=64)
    command: str = Field(default="", max_length=400)
    """`brain = "command"` 时的命令行，按空格切；比如 `claude -p`。"""

    persona: str = Field(default="", max_length=2000)


def add_agent(layout: NodeLayout, config: Config, spec: AgentSpec) -> dict[str, Any]:
    if not AGENT_NAME_RE.match(spec.name):
        raise AntHillError(f"非法 Agent 名 {spec.name!r}")
    if spec.name in config.agents:
        raise AntHillError(f"{spec.name} 已经存在了")
    if spec.brain == "provider" and not spec.provider:
        raise AntHillError("选了 provider 就得指明用哪个（先在 node.toml 里配好 [providers.*]）")
    if spec.brain == "provider" and spec.provider not in config.providers:
        raise AntHillError(
            f"node.toml 里没有 [providers.{spec.provider}]，先把它配好再加这个 Agent"
        )
    if spec.brain == "command" and not spec.command.strip():
        raise AntHillError("选了 command 就得给出命令行，比如 claude -p")

    text = layout.node_toml.read_text(encoding="utf-8")
    return {"ok": True, "name": spec.name, "text": text.rstrip("\n") + "\n" + _section(spec)}


def remove_agent(layout: NodeLayout, config: Config, name: str) -> dict[str, Any]:
    if name not in config.agents:
        raise AntHillError(f"没有叫 {name} 的 Agent")
    if len(config.agents) <= 1:
        raise AntHillError("这是最后一个 Agent 了，删掉之后这个节点收不了任何消息")
    if is_running(layout, name):
        raise AntHillError(f"{name} 还在跑，先停掉再删")

    text = layout.node_toml.read_text(encoding="utf-8")
    return {"ok": True, "name": name, "text": drop_section(text, f"agents.{name}")}


def _section(spec: AgentSpec) -> str:
    lines = [f"\n[agents.{spec.name}]", f'role = "{_esc(spec.role)}"']
    if spec.brain == "bridge":
        lines.append("bridge = true")
    elif spec.brain == "command":
        parts = ", ".join(f'"{_esc(p)}"' for p in spec.command.split())
        lines.append(f"command = [{parts}]")
    elif spec.brain == "provider":
        lines.append(f'provider = "{_esc(spec.provider)}"')
    if spec.persona.strip():
        lines.append(f'persona = "{_esc(spec.persona)}"')
    return "\n".join(lines) + "\n"


def _esc(value: str) -> str:
    """TOML 基本字符串里的转义。宁可保守 —— 这段文本会被当配置解析。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def drop_section(text: str, header: str) -> str:
    """删掉 `[header]` 那一节，直到下一个节标题为止。

    按行处理是为了**保住周围的注释** —— 那些注释是这个模板最有用的部分。
    """
    out: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            dropping = stripped[1:-1].strip() == header
        if not dropping:
            out.append(line)
    return "".join(out)


# ---------- agentd 的启停 ----------


def runtime_path(layout: NodeLayout, name: str) -> Path:
    return layout.agent_dir(name) / RUNTIME_FILE


def running_pid(layout: NodeLayout, name: str) -> int | None:
    """agentd 自己写的 runtime.json 里有 pid；进程没了就当没在跑。"""
    from anthill.cli.common import is_running

    try:
        data = json.loads(runtime_path(layout, name).read_text(encoding="utf-8"))
        pid = int(data.get("pid", -1))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return pid if pid > 0 and is_running(pid) else None


def is_running(layout: NodeLayout, name: str) -> bool:
    return running_pid(layout, name) is not None


IN_FLIGHT: set[tuple[str, str]] = set()
"""正在启停/删除中的 (节点, Agent)。同一只 Agent 的操作不许叠加 ——
面板双击、两个操作方同时动手（重启撞车实证）都会互相顶：
一边 stop 的宽限期里另一边 start，得到的是「刚启动就被停」。
只防**本 serve 进程内**的并发；跨进程的手动 CLI 操作管不到（已记录）。"""


@contextmanager
def agent_op(node: str, name: str) -> Iterator[None]:
    key = (node, name)
    if key in IN_FLIGHT:
        raise AntHillError(f"{name} 正在操作中 —— 等上一个动作完成再点")
    IN_FLIGHT.add(key)
    try:
        yield
    finally:
        IN_FLIGHT.discard(key)


def start_agent(layout: NodeLayout, config: Config, name: str) -> dict[str, Any]:
    """把一个 agentd 拉起来，脱离本进程独活。

    `start_new_session=True` 是关键：面板重启、甚至 serve 退出，agentd 都该继续跑 ——
    它是独立的一个消费者，不是面板的子任务。
    """
    if name not in config.agents:
        raise AntHillError(f"没有叫 {name} 的 Agent")
    pid = running_pid(layout, name)
    if pid is not None:
        return {"ok": True, "name": name, "pid": pid, "already": True}

    Mailbox(layout.mailbox_dir(name)).ensure()
    command = [
        sys.executable,
        "-m",
        "anthill",
        "agent",
        "start",
        name,
        "--workspace",
        str(layout.workspace),
        "--quiet",  # 没有终端可回显；它自己写 jsonl 日志
        "--unattended",  # 没人在这个进程前面，需要确认的高危操作一律拒绝
    ]
    try:
        # 参数全是本机配置里的名字（已过 AGENT_NAME_RE），不拼接外部输入。
        # 「脱离本进程独活」的平台差异（含 uv 蹦床小黑框）统一在 detach_kwargs。
        process = subprocess.Popen(
            command,
            cwd=str(layout.workspace),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach_kwargs(),
        )
    except OSError as exc:
        raise AntHillError(f"起不来：{exc}") from exc
    return {"ok": True, "name": name, "pid": process.pid, "already": False}


def stop_agent(layout: NodeLayout, name: str) -> dict[str, Any]:
    """SIGTERM 而不是 SIGKILL —— agentd 收到之后会把手上那条消息处理完再退。

    只发给 agentd 本体而非进程组：让它自己收尾孩子，组杀会把干活中的
    command Agent 一起带走。win32 上 os.kill(SIGTERM) 是 TerminateProcess
    **硬停**（无窗口进程没有更好的信号可用）——优雅的正解是 stop.flag
    文件 + 主循环轮询，记在清单里。"""
    pid = running_pid(layout, name)
    if pid is None:
        return {"ok": True, "name": name, "already": True}
    try:
        os.kill(pid, STOP_GRACE)
    except OSError as exc:
        raise AntHillError(f"停不掉 {name}（pid {pid}）：{exc}") from exc
    return {"ok": True, "name": name, "pid": pid, "already": False}
