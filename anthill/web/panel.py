"""只读面板的数据层（03-tech-design §9）。

**数据源全部是磁盘上的文件**，不是内存里的 event bus —— 因为一个节点上跑着好几个
进程（每个 agentd 一个，加一个 serve），内存 bus 根本跨不过进程边界。
好在这个项目里「文件就是唯一真相」，面板照着读就行：

    拓扑    node.toml + 各 agent 的 runtime.json（在跑没跑）+ peers.json
    任务    blackboard/tasks/<id>/state.json
    消息流  logs/agentd-*.jsonl

只读、只绑回环、绝不暴露密钥 —— 确认这类动作留在 CLI，不给面板任何写权限，
免得它变成攻击面。
"""

from __future__ import annotations

from typing import Any

from anthill.core.config import Config
from anthill.core.logging import read_log
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.discovery.registry import PeerRegistry
from anthill.orchestrator.state import RunStore

DEFAULT_EVENT_LIMIT = 80
PER_AGENT_EVENTS = 40
RUNTIME_FILE = "runtime.json"


def build_snapshot(
    layout: NodeLayout,
    config: Config,
    peers: PeerRegistry,
    *,
    event_limit: int = DEFAULT_EVENT_LIMIT,
) -> dict[str, Any]:
    return {
        "node": config.node.name,
        "agents": _agents(layout, config),
        "peers": _peers(peers),
        "runs": _runs(layout),
        "events": _events(layout, config, event_limit),
    }


def _agents(layout: NodeLayout, config: Config) -> list[dict[str, Any]]:
    import json

    from anthill.cli.common import is_running

    out: list[dict[str, Any]] = []
    for name, agent in sorted(config.agents.items()):
        status_file = layout.agent_dir(name) / RUNTIME_FILE
        running, watch_mode = False, "-"
        if status_file.is_file():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                running = is_running(int(data.get("pid", -1)))
                watch_mode = str(data.get("watch_mode", "-"))
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        mailbox = Mailbox(layout.mailbox_dir(name))
        out.append(
            {
                "name": name,
                "role": agent.role,
                "provider": agent.provider or "echo",
                "running": running,
                "watch_mode": watch_mode,
                "queue": len(mailbox.list_new()) if mailbox.exists else 0,
                "dead": len(Outbox(mailbox).dead_letters()) if mailbox.exists else 0,
            }
        )
    return out


def _peers(peers: PeerRegistry) -> list[dict[str, Any]]:
    """只出非密字段。PeerRecord 本来就不含密钥，这里再明确一次不要顺手加。"""
    return [
        {
            "node": peer.node,
            "status": peer.status,
            "endpoint": peer.endpoint,
            "agents": list(peer.agents),
            "fingerprint": peer.fingerprint,
            "last_seen": peer.last_seen,
        }
        for peer in peers.all()
    ]


def _runs(layout: NodeLayout) -> list[dict[str, Any]]:
    store = RunStore(layout.blackboard)
    runs = sorted(store.all(), key=lambda s: (s.finished, s.started_at), reverse=False)
    return [
        {
            "task_id": run.task_id,
            "goal": run.plan.goal,
            "requester": run.requester,
            "round": run.round,
            "finished": run.finished,
            "result": run.result,
            "steps": [
                {
                    "id": step.id,
                    "assignee": step.assignee,
                    "state": str(step.state),
                    "detail": step.summary or step.error or step.task,
                }
                for step in run.steps
            ],
        }
        for run in runs
    ]


def _events(layout: NodeLayout, config: Config, limit: int) -> list[dict[str, Any]]:
    """把各 agent 的日志合并成一条时间线。按 ts 排序，取最近的一段。"""
    merged: list[dict[str, Any]] = []
    # 按路径去重：serve 的日志文件只有一个，但它在事件里的 agent 名是 `serve:<节点>`，
    # 照名字拼路径会把同一个文件读两遍，事件流里每条都成双出现
    paths = {layout.log_file(name) for name in (*config.agents, "serve")}
    for path in sorted(paths):
        merged.extend(read_log(path, limit=PER_AGENT_EVENTS))
    merged.sort(key=lambda record: str(record.get("ts", "")))
    return [_slim(record) for record in merged[-limit:]]


INTERESTING = ("msg", "to", "frm", "thread", "step", "task", "error", "tool", "node", "type")


def _slim(record: dict[str, Any]) -> dict[str, Any]:
    """日志字段很多，面板只要能看懂发生了什么的那几个。"""
    return {
        "ts": str(record.get("ts", ""))[11:19],
        "agent": record.get("agent", "-"),
        "level": record.get("level", "info"),
        "event": record.get("event", ""),
        "detail": " ".join(
            f"{k}={record[k]}" for k in INTERESTING if k in record and record[k] not in ("", None)
        )[:160],
    }
