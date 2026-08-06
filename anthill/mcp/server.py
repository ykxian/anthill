"""把 AntHill 暴露成 MCP server —— 让 Claude Code 原生调它，而不是靠人转述。

桥接的设计本来就聪明：收消息不阻塞，人可以慢慢想，网页和常驻会话写的是同一批文件。
但集成方式一直是**被动**的 —— 面板给你一句提示词，你粘给会话，让它「盯着这个目录」。
会话不会被通知，也得先理解目录结构。

这一层把同样几件事换成有 schema 的工具：`anthill_inbox` / `anthill_reply` /
`anthill_send` / `anthill_runs` / `anthill_status`。

## 一件要说清楚的事

**MCP 不解决「被动」。** 工具也是拉取式的，模型自己决定什么时候调；
装完之后 Claude Code 依然不会主动知道有消息在等它。真正去掉人肉转述的是
**hook**（见 `examples/claude-code-hook/`）。MCP 让那次调用规整
（有 schema、不用解析 CLI 输出、不只限 Claude Code），两件事是互补的，不是替代。

## 边界

- 走 **stdio**，由客户端拉起：不开端口、不加鉴权面，因为能起这个进程的人
  本来就有这台机器的账号 —— 和 `anthill bridge` 同一个权限模型。
- 只暴露**桥接与只读查询**。不暴露改配置、启停 agentd、审批 ——
  那些的分量是「能在这台机器上执行命令」，留在 CLI 和面板的写入口里。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from anthill.adapters.bridge import BridgeHandler, parse_note
from anthill.adapters.bridge_session import claim, pick_agent, wait_for_message
from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.ids import new_id
from anthill.core.paths import NodeLayout
from anthill.orchestrator.state import RunStore, StepState

PREVIEW = 2000
MAX_WAIT = 600.0
"""单次阻塞等待的上限。太长会让客户端那边看起来像卡死。"""


def build_server(layout: NodeLayout, config: Config, agent: str) -> Any:
    """造一个 FastMCP 实例。`agent` 是**这个会话代表谁**（一个桥接 Agent）。"""
    try:
        # mcp 2.0 起叫 MCPServer（1.x 里是 FastMCP）；两个名字都接一下，
        # 免得用户装了哪个版本就得改代码
        try:
            from mcp.server import MCPServer as _Server
        except ImportError:  # pragma: no cover - 1.x 的路径
            from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover - 取决于装没装
        raise AntHillError("缺少 mcp 依赖；执行 `uv sync --extra mcp` 安装") from exc

    # 名字没给就按「环境变量 > 自动认领一个没人占的」挑 —— 这是「一一对应」的关键：
    # Claude Code 的配置粒度是目录，同一个目录下开几个会话就是几份一样的配置，
    # 配置文件表达不出「谁对应谁」。见 adapters/bridge_session.py。
    agent = pick_agent(layout, config, agent)
    section = config.agents.get(agent)
    if section is None:
        raise AntHillError(
            f"本节点没有 Agent {agent!r}；有的是：{', '.join(sorted(config.agents))}"
        )
    if not section.bridge:
        raise AntHillError(
            f"{agent} 不是桥接 Agent —— node.toml 里给它加 bridge = true。\n"
            "  桥接 Agent 背后是「一个人」（或你这个会话），所以它不自己想；"
            "别的 Agent 有自己的大脑，不该由这个会话代答。"
        )

    server = _Server("anthill")
    handler = BridgeHandler(root=layout.agent_dir(agent), agent_name=agent)
    # 认领它：别的会话再起一个 MCP server 时会自动挑别的，不会两个会话抢同一个。
    # 松开靠 pid —— 这个进程没了，认领自动失效（见 read_claim）。
    claim(layout, agent, force=True)

    @server.tool()
    def anthill_inbox() -> dict[str, Any]:
        """看看有哪些消息在等「我」回复。

        我在 AntHill 这个多 Agent 协作网络里代表 Agent「{agent}」。
        别的 Agent（可能在别的机器上）发给我的消息会在这里排队等我回。
        """
        waiting = []
        for path in sorted(handler.dir("inbox").glob("*.md")):
            headers, body = parse_note(path.read_text(encoding="utf-8"))
            waiting.append(
                {
                    "id": path.stem,
                    "short": path.stem[-6:],
                    "from": headers.get("from", ""),
                    "type": headers.get("type", "chat"),
                    "thread": headers.get("thread", ""),
                    "body": body.strip()[:PREVIEW],
                }
            )
        return {"agent": agent, "count": len(waiting), "waiting": waiting}

    @server.tool()
    def anthill_wait(seconds: float = 300.0) -> dict[str, Any]:
        """**等到有人找我为止**（最多 seconds 秒），然后把消息给我。

        闲着没事干的时候调这个，而不是反复调 anthill_inbox 空转 ——
        它会一直阻塞到有新消息进来。返回的形状和 anthill_inbox 一样。
        超时了就再调一次。
        """
        known = {p.name for p in handler.dir("inbox").glob("*.md")}
        fresh = wait_for_message(
            handler.dir("inbox"), timeout=max(1.0, min(seconds, MAX_WAIT)), known=known
        )
        if not fresh:
            return {"agent": agent, "count": 0, "waiting": [], "timed_out": True}
        return anthill_inbox()

    @server.tool()
    def anthill_reply(message_id: str, text: str) -> dict[str, Any]:
        """回复一条在等我的消息。`message_id` 用 anthill_inbox 给的 id（后 6 位也行）。"""
        pending = sorted(handler.dir("inbox").glob("*.md"))
        matched = [p for p in pending if p.stem == message_id or p.stem.endswith(message_id)]
        if not matched:
            return {"ok": False, "error": f"没有在等回复的消息 {message_id}；先调 anthill_inbox"}
        if len(matched) > 1:
            return {"ok": False, "error": f"{message_id} 匹配到 {len(matched)} 条，多给几位"}
        if not text.strip():
            return {"ok": False, "error": "回复不能是空的"}
        (handler.dir("outbox") / matched[0].name).write_text(text, encoding="utf-8")
        return {"ok": True, "replied": matched[0].stem, "note": "下一轮 tick 就发出去"}

    @server.tool()
    def anthill_send(to: str, text: str, kind: str = "chat") -> dict[str, Any]:
        """主动发一条给别的 Agent（不是回复）。

        `to` 可以是 `coder`（本机）或 `lab:coder`（别的机器上的）。
        `kind` 是 chat 或 task —— task 会让对方当成一件要交付的活。
        """
        if not to.strip() or not text.strip():
            return {"ok": False, "error": "收件人和正文都不能为空"}
        if kind not in ("chat", "task"):
            return {"ok": False, "error": "kind 只能是 chat 或 task"}
        name = f"{new_id()}.md"
        (handler.dir("outbox") / name).write_text(
            f"---\nto: {to.strip()}\nkind: {kind}\n---\n{text}", encoding="utf-8"
        )
        return {"ok": True, "to": to.strip(), "kind": kind, "note": "下一轮 tick 就发出去"}

    @server.tool()
    def anthill_runs(task_id: str = "") -> dict[str, Any]:
        """看编排任务：留空列出全部，给 task_id 看某一条的每一步。"""
        states = RunStore(layout.blackboard).all()
        if task_id:
            matched = [s for s in states if s.task_id == task_id or s.task_id.endswith(task_id)]
            if not matched:
                return {"error": f"没有匹配 {task_id} 的任务"}
            state = matched[0]
            return {
                "task_id": state.task_id,
                "goal": state.plan.goal,
                "finished": state.finished,
                "steps": [
                    {
                        "id": s.id,
                        "assignee": s.assignee,
                        "state": str(s.state),
                        "attempts": s.attempts,
                        "summary": s.summary or s.error,
                        "artifacts": list(s.artifacts),
                    }
                    for s in state.steps
                ],
            }
        return {
            "runs": [
                {
                    "task_id": s.task_id,
                    "short": s.task_id[-6:],
                    "goal": s.plan.goal,
                    "finished": s.finished,
                    "done": sum(1 for x in s.steps if x.state is StepState.DONE),
                    "total": len(s.steps),
                }
                for s in states
            ]
        }

    @server.tool()
    def anthill_status() -> dict[str, Any]:
        """这个节点上有哪些 Agent、哪些对端 —— 想知道「能发给谁」时调它。"""
        from anthill.core.config import brain_of

        return {
            "node": config.node.name,
            "workspace": str(layout.workspace),
            "me": agent,
            "claimed_by_pid": os.getpid(),
            "agents": [
                {"name": name, "role": section.role, "brain": brain_of(section)}
                for name, section in sorted(config.agents.items())
            ],
            "peers": sorted(config.peers),
        }

    return server


def workspace_of(raw: str | None) -> Path:
    return Path(raw).expanduser().resolve() if raw else Path.cwd()
