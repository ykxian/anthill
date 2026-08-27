"""Codex 会话桥接：交互式 TUI 照常聊，AntHill 来信自动开新 turn。

Claude Code 桥接借用了它的「后台 Bash 结束会通知模型」语义。Codex TUI
没有这个语义，但 app-server 把同一会话的 thread / turn 做成了双向
JSON-RPC 协议。这个适配器因此不在 TUI 里挂一条永远不退的 shell，而是：

有两条接入路径，但都严格保持一条 thread 只有一个 writer：

* Anthill 启动 TUI 时，TUI 和桥接器连同一个私有 app-server，桥接器用
  ``turn/start`` 注入来信；
* 已经运行的普通 Codex TUI 持有 writer 时，桥接器用 Codex 原生
  ``queue`` 命令把来信交给现有 writer，再用独立 app-server 的只读
  ``thread/read`` 取得最终回答。这样来信和处理过程显示在原来的前台，
  也不会触发 ``already has an active writer``。

信箱、信封、路由与 Claude 桥接都不改；这里只补 Codex 宿主的「最后一步
唤醒」。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import websockets

from anthill import __version__
from anthill.adapters.bridge import BRIDGE_DIR
from anthill.adapters.bridge_connect import codex_session_instructions, role_card_prompt
from anthill.adapters.interactive_agent import (
    HostTurn,
    InboxMessage,
    InteractiveAgentBridge,
    wait_or_stop,
)
from anthill.agent.context import untrusted_wrap
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.security.secrets import sanitized_child_env

APP_SERVER_START_TIMEOUT = 15.0
RPC_TIMEOUT = 30.0
POLL_INTERVAL = 0.5
SESSION_FILE = "codex-session.json"
QUEUE_STATE_FILE = "codex-queue-state.json"
ENDPOINT_RE = re.compile(r"listening on:\s+(ws://\S+)")
QUEUE_ID_RE = re.compile(r"\bQueued message\s+(\S+)")


class CodexAppServerError(AntHillError):
    """app-server 进程或协议不可用。"""


class CodexRpcError(CodexAppServerError):
    """app-server 返回了 JSON-RPC error。"""

    def __init__(self, method: str, error: Any) -> None:
        super().__init__(f"Codex app-server {method} 失败：{error}")
        self.method = method
        self.error = error


class CodexRpcClient:
    """app-server WebSocket 的最小 JSON-RPC 客户端。

    app-server 还会反向向客户端请求审批。来信触发的 turn 没有人坐在
    这条 RPC 连接前，所以绝不能悬空等审批：普通命令/改文件明确 decline，
    额外权限给空授权，交互问题返回空答案。这和 agentd --unattended 的
    「没人能批就拒绝」是同一安全边界。
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(
                self.endpoint,
                open_timeout=RPC_TIMEOUT,
                close_timeout=3,
                # thread/read(includeTurns=true) 会返回整段长会话；默认/过小的
                # WebSocket 上限会让正常的大 thread 被误判为连接断开。
                max_size=64 * 1024 * 1024,
            )
        except Exception as exc:
            raise CodexAppServerError(f"连不上 Codex app-server {self.endpoint}：{exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "anthill",
                    "title": "AntHill Codex bridge",
                    "version": __version__,
                }
            },
        )
        await self.notify("initialized", {})

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        self._reader = None
        self._ws = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._ws is None:
            raise CodexAppServerError("Codex app-server RPC 还没连接")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            await self._ws.send(json.dumps(message, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
        except TimeoutError as exc:
            raise CodexAppServerError(f"Codex app-server {method} {RPC_TIMEOUT:g}s 没回应") from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise CodexRpcError(method, response["error"])
        return response.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._ws is None:
            raise CodexAppServerError("Codex app-server RPC 还没连接")
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._ws.send(json.dumps(message, ensure_ascii=False))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif request_id is not None and message.get("method"):
                    await self._answer_server_request(message)
                elif message.get("method"):
                    await self.notifications.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = CodexAppServerError(f"Codex app-server 连接断开：{exc}")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
        finally:
            error = CodexAppServerError("Codex app-server 连接已关闭")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _answer_server_request(self, message: dict[str, Any]) -> None:
        assert self._ws is not None
        method = str(message.get("method", ""))
        request_id = message.get("id")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result: dict[str, Any] = {"decision": "decline"}
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            result = {
                "decision": {
                    "denied": {"rejection": "AntHill 后台 turn 无人交互审批；请在 TUI 中手动继续"}
                }
            }
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            await self._ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "AntHill background turn cannot handle interactive request",
                        },
                    }
                )
            )
            return
        await self._ws.send(json.dumps({"id": request_id, "result": result}, ensure_ascii=False))


@dataclass(slots=True)
class TurnResult:
    status: str
    answer: str


class CodexInboxBridge(InteractiveAgentBridge):
    """bridge/inbox ↔ 一条 Codex app-server thread。"""

    def __init__(
        self,
        *,
        client: CodexRpcClient,
        layout: NodeLayout,
        agent: str,
        thread_id: str,
        log: EventLog,
    ) -> None:
        super().__init__(
            layout=layout,
            agent=agent,
            log=log,
            event_prefix="codex.bridge",
            host_name="Codex",
            poll_interval=POLL_INTERVAL,
        )
        self.client = client
        self.thread_id = thread_id
        self._answers: dict[str, list[tuple[str | None, str]]] = {}
        self._turns: dict[str, TurnResult] = {}
        self._turn_events: dict[str, asyncio.Event] = {}
        self._events_task: asyncio.Task[None] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        self._events_task = asyncio.create_task(self._consume_events())
        try:
            await super().run(stop)
        finally:
            if self._events_task is not None:
                self._events_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._events_task

    async def wait_until_available(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and await self._is_active():
            await wait_or_stop(stop, POLL_INTERVAL)

    async def _is_active(self) -> bool:
        result = await self.client.request(
            "thread/read", {"threadId": self.thread_id, "includeTurns": False}
        )
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        status = thread.get("status", {}) if isinstance(thread, dict) else {}
        return isinstance(status, dict) and status.get("type") == "active"

    async def retry_after_error(self, error: AntHillError) -> bool:
        # 用户在「查空闲→start」之间抢先发起 turn 是正常竞态。只要 thread
        # 确实 active 就重新排队；其他协议错误由基类隔离并保留来信。
        return isinstance(error, CodexRpcError) and await self._is_active()

    async def deliver(self, message: InboxMessage, stop: asyncio.Event) -> HostTurn | None:
        result = await self.client.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": render_incoming_prompt(
                            agent=self.agent,
                            message_id=message.id,
                            headers=message.headers,
                            body=message.body,
                            needs_reply=message.needs_reply,
                            session_instructions=role_card_prompt(self.layout, self.agent),
                        ),
                    }
                ],
                "clientUserMessageId": f"anthill-{message.id}",
            },
        )
        turn = result.get("turn", {}) if isinstance(result, dict) else {}
        turn_id = str(turn.get("id", "")) if isinstance(turn, dict) else ""
        if not turn_id:
            raise CodexAppServerError("Codex turn/start 没返回 turn.id")
        self.log.info(
            "codex.bridge.turn_started",
            file=message.path.name,
            turn=turn_id,
            thread=self.thread_id,
            needs_reply=message.needs_reply,
        )
        event = self._turn_events.setdefault(turn_id, asyncio.Event())
        if turn_id not in self._turns:
            await event.wait()
        outcome = self._turns.pop(turn_id)
        self._turn_events.pop(turn_id, None)
        self._answers.pop(turn_id, None)
        if outcome.status != "completed":
            raise CodexAppServerError(f"Codex turn {turn_id} 以 {outcome.status} 结束")
        return HostTurn(id=turn_id, answer=outcome.answer)

    async def _consume_events(self) -> None:
        while True:
            message = await self.client.notifications.get()
            method = str(message.get("method", ""))
            params = message.get("params", {})
            if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
                continue
            if method == "item/completed":
                item = params.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    turn_id = str(params.get("turnId", ""))
                    text = str(item.get("text", ""))
                    phase = item.get("phase")
                    if turn_id and text:
                        self._answers.setdefault(turn_id, []).append(
                            (str(phase) if phase is not None else None, text)
                        )
            elif method == "turn/completed":
                turn = params.get("turn", {})
                if not isinstance(turn, dict):
                    continue
                turn_id = str(turn.get("id", ""))
                if not turn_id:
                    continue
                messages = self._answers.get(turn_id, [])
                finals = [text for phase, text in messages if phase == "final_answer"]
                answer = finals[-1] if finals else (messages[-1][1] if messages else "")
                self._turns[turn_id] = TurnResult(
                    status=str(turn.get("status", "unknown")), answer=answer
                )
                self._turn_events.setdefault(turn_id, asyncio.Event()).set()
                self._trim_completed_turns()

    def _trim_completed_turns(self) -> None:
        """TUI 里的人类 turn 也会经过这条订阅，别让它们无限堆着。"""
        while len(self._turns) > 64:
            oldest = next(iter(self._turns))
            self._turns.pop(oldest, None)
            self._turn_events.pop(oldest, None)
            self._answers.pop(oldest, None)


QueueSubmitter = Callable[[str], Awaitable[str]]


class CodexQueueBridge(InteractiveAgentBridge):
    """把 Anthill 来信排进一个已经运行、已经持有 writer 的 Codex thread。

    ``codex queue`` 负责唤醒现有 TUI；本类持有的 app-server 连接只调用
    ``thread/read``，不会 resume thread，也就不会和前台争 writer。
    """

    def __init__(
        self,
        *,
        client: CodexRpcClient,
        layout: NodeLayout,
        agent: str,
        thread_id: str,
        codex: str,
        log: EventLog,
        submit: QueueSubmitter | None = None,
        child_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            layout=layout,
            agent=agent,
            log=log,
            event_prefix="codex.queue",
            host_name="Codex queue",
            poll_interval=POLL_INTERVAL,
        )
        self.client = client
        self.thread_id = thread_id
        self.codex = codex
        self.state_path = self.handler.root / QUEUE_STATE_FILE
        self._submit = submit or self._submit_with_codex
        self._child_env = child_env or sanitized_child_env()

    async def deliver(self, message: InboxMessage, stop: asyncio.Event) -> HostTurn | None:
        outcome = await self._find_turn(message.id)
        state = self._read_state()
        queued = state.get("queued", {})
        if outcome is None and message.id not in queued:
            # attach 到别的前台持有的 thread 时不能改 developer instructions。
            # 兼容规则只随第一封 queue 消息注入一次，随后留在 thread 历史里；
            # 状态持久化后，桥接重启也不会逐封重复整段说明。
            session_instructions = ""
            if not state.get("instructions_injected", False):
                session_instructions = codex_session_instructions(self.layout, self.agent)
            role_card = role_card_prompt(self.layout, self.agent)
            session_instructions = "\n\n".join(
                part for part in (session_instructions, role_card) if part.strip()
            )
            prompt = render_incoming_prompt(
                agent=self.agent,
                message_id=message.id,
                headers=message.headers,
                body=message.body,
                needs_reply=message.needs_reply,
                source="Codex 原生 queue",
                session_instructions=session_instructions,
            )
            queue_id = await self._submit(prompt)
            queued[message.id] = {"queue_id": queue_id, "queued_at": time.time()}
            state = {
                "thread_id": self.thread_id,
                "instructions_injected": True,
                "queued": queued,
            }
            self._write_state(state)
            self.log.info(
                "codex.queue.submitted",
                file=message.path.name,
                queue=queue_id,
                thread=self.thread_id,
            )

        while outcome is None and not stop.is_set():
            await wait_or_stop(stop, POLL_INTERVAL)
            if stop.is_set():
                return None
            outcome = await self._find_turn(message.id)
        if outcome is None:
            return None
        turn_id, result = outcome
        if result.status != "completed":
            raise CodexAppServerError(f"Codex queue turn {turn_id} 以 {result.status} 结束")
        return HostTurn(id=turn_id, answer=result.answer)

    def after_delivery(self, message: InboxMessage, turn: HostTurn) -> None:
        self._forget(message.id)

    async def _find_turn(self, message_id: str) -> tuple[str, TurnResult] | None:
        result = await self.client.request(
            "thread/read", {"threadId": self.thread_id, "includeTurns": True}
        )
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        turns = thread.get("turns", []) if isinstance(thread, dict) else []
        if not isinstance(turns, list):
            return None
        marker = delivery_marker(message_id)
        for turn in reversed(turns):
            if not isinstance(turn, dict) or not _turn_has_marker(turn, marker):
                continue
            turn_id = str(turn.get("id", ""))
            status = str(turn.get("status", "unknown"))
            # 另一个进程持有 writer 时，只读 app-server 是从仍在增长的
            # rollout 重建 thread。读到文件当前 EOF 会暂时把尚未结束的 turn
            # 表示成 ``interrupted``，但此时没有 completedAt；真实的完成、
            # 失败或人为中断都有结束时间。不能把这个瞬时快照当成失败。
            if status in {"inProgress", "pending"} or turn.get("completedAt") is None:
                return None
            return turn_id, TurnResult(status=status, answer=_final_answer(turn))
        return None

    async def _submit_with_codex(self, prompt: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.codex,
                "queue",
                "--thread",
                self.thread_id,
                "--message",
                prompt,
                cwd=str(self.layout.workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env,
            )
        except OSError as exc:
            raise CodexAppServerError(f"启动 codex queue 失败：{exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=RPC_TIMEOUT)
        except TimeoutError as exc:
            await _terminate(process)
            raise CodexAppServerError(f"codex queue {RPC_TIMEOUT:g}s 没结束") from exc
        output = stdout.decode(errors="replace").strip()
        error = stderr.decode(errors="replace").strip()
        if process.returncode:
            detail = error or output or f"exit {process.returncode}"
            raise CodexAppServerError(f"codex queue 失败：{detail}")
        match = QUEUE_ID_RE.search(output)
        return match.group(1) if match else output

    def _read_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"thread_id": self.thread_id, "queued": {}}
        if not isinstance(state, dict) or state.get("thread_id") != self.thread_id:
            return {
                "thread_id": self.thread_id,
                "instructions_injected": False,
                "queued": {},
            }
        if not isinstance(state.get("queued"), dict):
            state["queued"] = {}
        if not isinstance(state.get("instructions_injected"), bool):
            # 旧版已经排队的 prompt 本身带着整段规则；迁移时视为已注入。
            state["instructions_injected"] = bool(state["queued"])
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _forget(self, message_id: str) -> None:
        state = self._read_state()
        queued = state.get("queued", {})
        queued.pop(message_id, None)
        state["queued"] = queued
        if queued or state.get("instructions_injected", False):
            self._write_state(state)
        else:
            self.state_path.unlink(missing_ok=True)


def delivery_marker(message_id: str) -> str:
    return f"ANTHILL_DELIVERY_ID::{message_id}"


def _turn_has_marker(turn: dict[str, Any], marker: str) -> bool:
    items = turn.get("items", [])
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and marker in str(part.get("text", "")):
                return True
    return False


def _final_answer(turn: dict[str, Any]) -> str:
    items = turn.get("items", [])
    if not isinstance(items, list):
        return ""
    messages = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and str(item.get("text", ""))
    ]
    finals = [item for item in messages if item.get("phase") == "final_answer"]
    selected = finals[-1] if finals else (messages[-1] if messages else {})
    return str(selected.get("text", ""))


def render_incoming_prompt(
    *,
    agent: str,
    message_id: str,
    headers: dict[str, str],
    body: str,
    needs_reply: bool,
    source: str = "app-server",
    session_instructions: str = "",
) -> str:
    """Codex 看到的来信 turn；稳定规则在 thread developer instructions 里。"""
    prefix = f"{session_instructions.strip()}\n\n" if session_instructions.strip() else ""
    reply = "yes" if needs_reply else "no"
    return (
        f"{prefix}"
        f"[AntHill via {source} · agent={agent} · {headers.get('type', 'chat')} · "
        f"from={headers.get('from', '')} · thread={headers.get('thread', '')} · reply={reply}]\n"
        f"{delivery_marker(message_id)}\n"
        f"{untrusted_wrap(body.strip(), source=headers.get('from', '') or source)}"
    )


async def start_app_server(
    *, codex: str, cwd: Path, log_file: TextIO, env: dict[str, str] | None = None
) -> tuple[asyncio.subprocess.Process, str, asyncio.Task[None]]:
    """0 端口让内核选一个无冲突的本机端口，再从启动行读回实际地址。"""
    try:
        process = await asyncio.create_subprocess_exec(
            codex,
            "app-server",
            "--listen",
            "ws://127.0.0.1:0",
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env or sanitized_child_env(),
        )
    except OSError as exc:
        raise CodexAppServerError(f"启动 codex app-server 失败：{exc}") from exc
    assert process.stderr is not None
    endpoint = ""
    deadline = asyncio.get_running_loop().time() + APP_SERVER_START_TIMEOUT
    lines: list[str] = []
    try:
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            raw = await asyncio.wait_for(process.stderr.readline(), timeout=max(0.1, remaining))
            if not raw:
                break
            line = raw.decode(errors="replace")
            log_file.write(line)
            log_file.flush()
            lines.append(line.strip())
            match = ENDPOINT_RE.search(line)
            if match:
                endpoint = match.group(1)
                break
    except TimeoutError:
        pass
    if not endpoint:
        await _terminate(process)
        detail = _startup_failure_detail(lines)
        raise CodexAppServerError(
            f"codex app-server 在 {APP_SERVER_START_TIMEOUT:g}s 内没就绪：{detail}"
        )
    drain = asyncio.create_task(_drain(process.stderr, log_file))
    return process, endpoint, drain


def _startup_failure_detail(lines: list[str]) -> str:
    """把 app-server 最常见的沙箱状态目录故障翻译成可执行的下一步。"""
    detail = " | ".join(lines[-5:]) or "未输出监听地址"
    lowered = detail.lower()
    if "read-only file system" in lowered or "failed to initialize sqlite state runtime" in lowered:
        return (
            f"{detail}。Codex 状态目录不可写；如果这是由当前 Codex 会话的 shell 工具发起，"
            "请对整条 `anthill codex ... --attach current` 命令申请一次性沙箱外执行，"
            "不要改成 --yolo"
        )
    return detail


async def create_or_resume_thread(
    client: CodexRpcClient,
    *,
    layout: NodeLayout,
    agent: str,
    node: str,
    resume: str = "",
    developer_instructions: str = "",
) -> str:
    if resume:
        resume_params = {"threadId": resume, "cwd": str(layout.workspace)}
        if developer_instructions:
            resume_params["developerInstructions"] = developer_instructions
        result = await client.request("thread/resume", resume_params)
    else:
        start_params: dict[str, Any] = {
            "cwd": str(layout.workspace),
            "serviceName": "anthill_codex_bridge",
        }
        if developer_instructions:
            start_params["developerInstructions"] = developer_instructions
        try:
            result = await client.request("thread/start", start_params)
        except CodexRpcError:
            # 早期 app-server 没有 serviceName；退化不影响 thread/turn 语义。
            start_params.pop("serviceName")
            result = await client.request("thread/start", start_params)
    thread = result.get("thread", {}) if isinstance(result, dict) else {}
    thread_id = str(thread.get("id", "")) if isinstance(thread, dict) else ""
    if not thread_id:
        raise CodexAppServerError("Codex thread/start 没返回 thread.id")
    with suppress(CodexRpcError):
        await client.request(
            "thread/name/set", {"threadId": thread_id, "name": f"anthill:{node}/{agent}"}
        )
    return thread_id


def write_session(
    layout: NodeLayout,
    agent: str,
    *,
    endpoint: str,
    thread_id: str,
    server_pid: int,
    mode: str = "app-server",
) -> Path:
    root = layout.agent_dir(agent) / BRIDGE_DIR
    root.mkdir(parents=True, exist_ok=True)
    target = root / SESSION_FILE
    temporary = root / f"{SESSION_FILE}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "server_pid": server_pid,
                "endpoint": endpoint,
                "thread_id": thread_id,
                "workspace": str(layout.workspace),
                "mode": mode,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


async def _drain(stream: asyncio.StreamReader, target: TextIO) -> None:
    while raw := await stream.readline():
        target.write(raw.decode(errors="replace"))
        target.flush()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=3)
