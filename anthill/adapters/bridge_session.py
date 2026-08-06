"""把一个终端会话**认领**一个桥接 Agent，以及阻塞等消息。

## 要解决的两件事

**一、Claude Code 的配置粒度是目录，不是会话。**

把 hook / MCP 写进某个项目目录，那个目录下开几个会话就有几个一模一样的配置 ——
它们会全都绑到同一个桥接 Agent 上，互相抢消息。想让「同一目录下的两个会话
各对应一个 Agent」，靠配置文件是表达不出来的。

能穿透到单个会话的只有**环境变量**（子进程继承）。所以：

    ANTHILL_AGENT=cc-1 claude      # 这个会话是 cc-1
    ANTHILL_AGENT=cc-2 claude      # 那个会话是 cc-2

再往前一步：连环境变量都不想设的时候，**自动认领一个还没人占的**。
在面板上建三个桥接 Agent，开三个会话，它们各自认领一个 —— 一一对应，
不用为每个会话配一次。

认领落成 `bridge/claim.json`（pid + cwd + 时间）。判据是 **pid 还活着**：
会话关了、进程没了，那个 Agent 自动回到「没人认领」，下一个会话就能接手。
不用心跳、不用超时 —— 进程活没活着内核最清楚。

**二、MCP 与 hook 都是拉取式的，给不了「一直盯着」。**

`wait_for_message` 会阻塞到有消息为止。有了它，粘给会话的那句话才是个真正的
监控循环（「反复跑这条命令」），而不是「你想起来的时候看一眼」。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthill.adapters.bridge import BRIDGE_DIR
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.ids import now
from anthill.core.paths import NodeLayout

CLAIM_FILE = "claim.json"
AGENT_ENV = "ANTHILL_AGENT"
WORKSPACE_ENV = "ANTHILL_WORKSPACE"
TAKEOVER_ENV = "ANTHILL_TAKEOVER"
"""置 1 才允许从一个还活着的会话手里把 Agent 抢过来。"""
POLL_INTERVAL = 0.5
DEFAULT_WAIT = 300.0


@dataclass(frozen=True, slots=True)
class Claim:
    agent: str
    pid: int
    cwd: str
    since: str

    @property
    def alive(self) -> bool:
        """那个会话还在吗。**判据就是 pid 活没活着** —— 不用心跳，内核最清楚。"""
        return is_alive(self.pid)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "pid": self.pid,
            "cwd": self.cwd,
            "since": self.since,
            "alive": self.alive,
        }


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 别人的进程，但确实在
    return True


def claim_path(layout: NodeLayout, agent: str) -> Path:
    return layout.agent_dir(agent) / BRIDGE_DIR / CLAIM_FILE


def last_claim(layout: NodeLayout, agent: str) -> Claim | None:
    """**上一次**是谁认领的 —— 不管那个进程还在不在。

    「还在不在」是给「空不空」用的；这个是给**亲和性**用的：
    上次在 `/home/x/projA` 的会话认领了 cc1，那么下次 `/home/x/projA` 里的会话
    还该拿到 cc1。见 `pick_agent`。
    """
    try:
        data = json.loads(claim_path(layout, agent).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return Claim(
        agent=agent,
        pid=int(data.get("pid", 0) or 0),
        cwd=str(data.get("cwd", "")),
        since=str(data.get("since", "")),
    )


def read_claim(layout: NodeLayout, agent: str) -> Claim | None:
    """**现在**谁占着这个 Agent。认领的进程死了就当没人占 —— 那正是自动回收。"""
    claim = last_claim(layout, agent)
    return claim if claim is not None and claim.alive else None


def bridge_agents(config: Config) -> list[str]:
    return sorted(name for name, a in config.agents.items() if a.bridge)


def claim(layout: NodeLayout, agent: str, *, force: bool | None = None) -> Claim:
    """认领一个桥接 Agent。已经被**活着的**别的进程占着就抛。

    **默认绝不抢。** 抢了就是两个会话同时是 `cc2`：它们读同一个收件箱、
    抢同一批消息，而各自的上下文完全不同 —— 那正好毁掉「一一对应」这件事本身。
    上一个会话真卡死了就用 `ANTHILL_TAKEOVER=1`，让它是个显式动作。
    """
    steal = force if force is not None else os.environ.get(TAKEOVER_ENV, "") == "1"
    held = read_claim(layout, agent)
    if held is not None and held.pid != os.getpid() and not steal:
        raise AntHillError(
            f"{agent} 已经被另一个会话占着了（pid {held.pid}，在 {held.cwd}）。\n"
            f"  换一个：ANTHILL_AGENT=<别的名字>；\n"
            "  在面板上再建一个桥接 Agent；\n"
            f"  或者确认那个会话已经不用了，用 {TAKEOVER_ENV}=1 接管它"
        )
    mine = Claim(agent=agent, pid=os.getpid(), cwd=str(Path.cwd()), since=now().isoformat())
    _write_claim(layout, mine)
    return mine


def release(layout: NodeLayout, agent: str) -> bool:
    """松开认领。只松自己的 —— 别把别人的会话踢下线。

    **不删文件，只把 pid 清零。** 记录留着是为了亲和性：下次同一个目录里的会话
    还要靠它认回同一个 Agent（见 `pick_agent`）。删掉就等于每次重启重新洗牌。
    """
    held = read_claim(layout, agent)
    if held is not None and held.pid != os.getpid():
        return False
    previous = last_claim(layout, agent)
    if previous is None:
        return True
    _write_claim(layout, Claim(agent=agent, pid=0, cwd=previous.cwd, since=previous.since))
    return True


def _write_claim(layout: NodeLayout, record: Claim) -> None:
    path = claim_path(layout, record.agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.as_dict(), ensure_ascii=False), encoding="utf-8")


def pick_agent(layout: NodeLayout, config: Config, wanted: str = "") -> str:
    """这个会话该当哪个桥接 Agent。

    三级：**显式指定 > 环境变量 > 自动认领一个没人占的**。

    最后那一级是「一一对应」的关键：面板上建三个，开三个会话，
    它们各自认领一个，不用为每个会话配一次。
    """
    named = wanted or os.environ.get(AGENT_ENV, "").strip()
    candidates = bridge_agents(config)
    if not candidates:
        raise AntHillError(
            "这个工作区里没有桥接 Agent —— 在面板的「加一个 Agent」里选 bridge，"
            "或在 node.toml 里给某个 Agent 写 bridge = true"
        )
    if named:
        if named not in candidates:
            raise AntHillError(
                f"{named} 不是桥接 Agent（node.toml 里要写 bridge = true）；"
                f"这个工作区里的桥接 Agent 有：{', '.join(candidates)}"
            )
        return named

    free = [name for name in candidates if read_claim(layout, name) is None]
    if not free:
        holders = ", ".join(
            f"{n}←pid {c.pid}" for n in candidates if (c := read_claim(layout, n)) is not None
        )
        raise AntHillError(
            f"这个工作区的桥接 Agent 都被别的会话占着了（{holders}）。\n"
            "  在面板上再建一个，或者用 ANTHILL_AGENT=<名字> 指定要抢哪个"
        )

    # **认回上次那个。** 纯粹「挑第一个空的」有个要命的后果：A 本来是 cc1、B 是 cc2，
    # 重启一轮顺序反了就变成 A→cc2、B→cc1 —— 而**上下文是挂在 Agent 上的**
    # （邮箱、thread、别人对「cc1 说过什么」的记忆），认错人等于串了历史。
    #
    # 需要一个跨重启稳定的身份。pid 不行（每次都变），**工作目录行**：
    # 同一个项目里重开的会话，还是那个项目的会话。三档优先级：
    here = str(Path.cwd())
    previous = {n: last_claim(layout, n) for n in free}
    # 1. 上次就是我 —— 认回去，历史接得上
    mine = [n for n in free if (p := previous[n]) is not None and p.cwd == here]
    if mine:
        return mine[0]
    # 2. 谁都没用过的 —— 挑它，别去碰别人的历史
    #    （少了这一档，第二个会话会顺手抢走第一个刚放开的那个，
    #     于是「谁是谁」每开一次就洗一次牌）
    virgin = [n for n in free if (p := previous[n]) is None or not p.cwd]
    if virgin:
        return virgin[0]
    # 3. 实在没有了，才去接别人用过的 —— 这时候串上下文是不可避免的取舍
    return free[0]


def workspace_from_env() -> Path | None:
    raw = os.environ.get(WORKSPACE_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def wait_for_message(
    inbox: Path, *, timeout: float = DEFAULT_WAIT, known: set[str] | None = None
) -> list[Path]:
    """阻塞到收件箱里有东西为止。返回那些 `.md`；超时返回空。

    **这就是「一直盯着」缺的那一块。** MCP 工具和 hook 都是拉取式的：
    模型自己决定什么时候调，会话闲着的时候没有任何东西会把它叫醒。
    有了这个阻塞调用，粘给会话的那句话才是个真正的监控循环
    （「反复跑这条命令，它会一直等到有消息」），而不是「你想起来了看一眼」。

    实现是轮询而不是 inotify：这条路要跨 NFS（学校服务器的 home 就是），
    而远端写入根本不产生本地 inotify 事件 —— watcher 那边为此专门做过降级。
    半秒一次的 `iterdir` 换来「哪儿都能用」，值。
    """
    seen = known if known is not None else set()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fresh = [p for p in sorted(inbox.glob("*.md")) if p.name not in seen]
        except OSError:
            fresh = []
        if fresh:
            return fresh
        if time.monotonic() >= deadline:
            return []
        time.sleep(POLL_INTERVAL)
