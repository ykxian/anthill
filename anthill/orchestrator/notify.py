"""任务跑完之后告诉一声。

以前一次协作结束是**完全无声**的：没有浏览器通知、没有标题角标、没有 webhook。
盯着终端的人还好，把任务丢给服务器之后走开的人就只能回来轮询。

只做 webhook 一种：它能接到几乎所有别的地方去（飞书、Slack、企业微信、
自己写的一个小服务），而每加一种原生集成都是一份要维护的鉴权与格式。

**默认全关。** 一个会自己往外发 HTTP 的框架，得是用户明确要的 ——
而且这条请求里带着任务目标与摘要，那是内容，不是元数据。
"""

from __future__ import annotations

from typing import Any

import httpx

from anthill.core.config import NotifySection
from anthill.core.logging import EventLog
from anthill.orchestrator.state import RunState
from anthill.transport.http import peer_client

MAX_SUMMARY = 2000


def payload_for(state: RunState) -> dict[str, Any]:
    ok = not (state.failed_ids or state.skipped_ids)
    return {
        "task_id": state.task_id,
        "goal": state.plan.goal,
        "ok": ok,
        "requester": state.requester,
        "round": state.round,
        "summary": state.result[:MAX_SUMMARY],
        "steps": [{"id": s.id, "assignee": s.assignee, "state": str(s.state)} for s in state.steps],
    }


async def notify(state: RunState, section: NotifySection, log: EventLog) -> bool:
    """发一条。返回有没有真的发出去。

    **发不出去只记日志。** 通知失败不该让一次已经成功的协作看起来像失败了 ——
    那是两件事，混在一起会让人去查根本没坏的东西。
    """
    if not section.webhook:
        return False
    body = payload_for(state)
    if section.on_failure_only and body["ok"]:
        return False
    try:
        async with peer_client(section.timeout) as client:
            response = await client.post(section.webhook, json=body)
    except httpx.HTTPError as exc:
        log.warn("notify.failed", task=state.task_id, error=f"{type(exc).__name__}: {exc}")
        return False
    if response.status_code >= 400:
        log.warn("notify.refused", task=state.task_id, status=response.status_code)
        return False
    log.info("notify.sent", task=state.task_id, ok=body["ok"])
    return True
