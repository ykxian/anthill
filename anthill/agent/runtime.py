"""agentd：一个 Agent 的守护进程主体（01-architecture §4）。

    watcher → 收件 → 校验 → 幂等去重 → 回执 accepted → handler → 归档

这一层刻意不含任何 LLM 逻辑：换 handler 就能从 echo agent 变成 LLM agent。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from anthill.agent.handlers import EchoHandler, HandlerContext, MessageHandler
from anthill.agent.sender import Sender
from anthill.agent.watcher import MailboxWatcher, WatchMode
from anthill.core.config import Config, check_runtime
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError, MailboxError, ProtocolError
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskErrorPayload
from anthill.core.router import Router
from anthill.core.seen import SeenStore
from anthill.core.states import DeliveryTracker
from anthill.transport.registry import TransportRegistry

COORDINATOR_ROLE = "coordinator"
STATUS_FILE = "runtime.json"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    agent: str
    pid: int
    watch_mode: str
    watch_reason: str
    started_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


class AgentRuntime:
    def __init__(
        self,
        *,
        layout: NodeLayout,
        config: Config,
        agent_name: str,
        handler: MessageHandler | None = None,
        log: EventLog | None = None,
        echo: bool = True,
    ) -> None:
        check_runtime(config, layout, agent_name)  # fail fast，别等收到消息才发现没配好

        self.layout = layout
        self.config = config
        self.agent_name = agent_name
        self.agent_config = config.agent(agent_name)
        self.identity = Address(node=config.node.name, agent=agent_name)
        self.mailbox = Mailbox(layout.mailbox_dir(agent_name)).ensure()
        self.handler: MessageHandler = handler or EchoHandler()
        self.log = log or EventLog(layout.log_file(agent_name), agent=agent_name, echo=echo)

        self._seen: SeenStore = self.mailbox.open_seen()
        self._tracker = DeliveryTracker()
        self._router = Router(config, layout)
        self._transports = TransportRegistry(config, layout)
        self.sender = Sender(
            identity=self.identity,
            mailbox=self.mailbox,
            router=self._router,
            transports=self._transports,
            tracker=self._tracker,
            log=self.log,
            coordinator=self._find_coordinator(),
        )
        self._watcher = MailboxWatcher(
            self.mailbox.new,
            mode=config.runtime.watch_mode,
            poll_interval=config.runtime.poll_interval,
        )
        self._ctx = HandlerContext(
            identity=self.identity,
            agent=self.agent_config,
            sender=self.sender,
            layout=layout,
            config=config,
            log=self.log,
        )

    @property
    def tracker(self) -> DeliveryTracker:
        return self._tracker

    @property
    def status_path(self) -> Path:
        return self.layout.agent_dir(self.agent_name) / STATUS_FILE

    # ---------- 生命周期 ----------

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        self._startup_recovery()
        await self._watcher.prepare()
        self._write_status()
        self.log.info(
            "agentd.start",
            node=self.config.node.name,
            role=self.agent_config.role,
            mailbox=str(self.mailbox.root),
            handler=self.handler.name,
        )
        queue: asyncio.Queue[Path] = asyncio.Queue()
        workers = [
            asyncio.create_task(self._produce(queue), name="watch"),
            asyncio.create_task(self._consume(queue), name="consume"),
            asyncio.create_task(self.sender.run_retry_loop(stop), name="retry"),
        ]
        stopper = asyncio.create_task(stop.wait(), name="stop")
        try:
            # 任一 worker 意外退出也要唤醒主流程，不能傻等 stop
            done, pending = await asyncio.wait(
                {stopper, *workers}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task is not stopper and (exc := task.exception()) is not None:
                    raise exc
        finally:
            await self.aclose()

    async def _produce(self, queue: asyncio.Queue[Path]) -> None:
        """watcher → 队列。单独一个任务，好让消费慢时事件不丢。"""
        stream = self._watcher.stream()
        try:
            async for path in stream:
                await queue.put(path)
        finally:
            await stream.aclose()

    async def _consume(self, queue: asyncio.Queue[Path]) -> None:
        """串行处理：同一个 Agent 一次只处理一条消息，避免上下文交错。"""
        while True:
            path = await queue.get()
            await self._process(path)

    def _startup_recovery(self) -> None:
        """崩溃恢复：cur 里没处理完的退回 new，tmp 里的半成品清掉，seen.db 滚动清理。"""
        recovered = self.mailbox.recover_stale()
        swept = self.mailbox.sweep_tmp()
        purged = self._seen.purge()
        if recovered or swept or purged:
            self.log.info(
                "agentd.recover", requeued=len(recovered), swept_tmp=swept, purged_seen=purged
            )

    def _write_status(self) -> None:
        mode = self._watcher.mode or WatchMode.POLL
        status = RuntimeStatus(
            agent=self.agent_name,
            pid=os.getpid(),
            watch_mode=str(mode),
            watch_reason=self._watcher.reason,
            started_at=now().isoformat(),
        )
        self.status_path.write_text(status.to_json(), encoding="utf-8")
        self.log.info("watch.mode", mode=str(mode), reason=self._watcher.reason)

    async def aclose(self) -> None:
        await self._watcher.stop()
        await self._transports.close()
        self._seen.close()
        self.status_path.unlink(missing_ok=True)
        self.log.info("agentd.stop")
        self.log.close()

    # ---------- 单条消息的处理 ----------

    async def _process(self, path: Path) -> None:
        if not path.is_file():
            return  # 已被上一轮扫描处理掉了

        try:
            claimed = self.mailbox.claim(path)
        except MailboxError:
            return  # 竞态：文件刚被别的循环拿走

        try:
            env = Mailbox.read_envelope(claimed)
        except (ProtocolError, MailboxError, ValidationError) as exc:
            self.mailbox.quarantine(claimed, str(exc))
            self.log.error("msg.invalid", file=claimed.name, error=str(exc))
            return

        try:
            await self._dispatch(env)
        except Exception as exc:  # handler 抛错不能拖垮 agentd
            self.log.error("msg.handler_error", msg=env.id, error=f"{type(exc).__name__}: {exc}")
            await self._report_failure(env, exc)
        finally:
            self.mailbox.archive(claimed)

    async def _dispatch(self, env: Envelope) -> None:
        self.log.info(
            "msg.received",
            msg=env.id,
            type=str(env.type),
            frm=str(env.from_),
            thread=env.thread,
            hops=env.hops,
        )

        if env.is_expired():
            await self.sender.send_receipt(env, MessageType.RECEIPT_EXPIRED, reason="已过期")
            self.log.warn("msg.expired", msg=env.id, expires_at=str(env.expires_at))
            return

        if not self._seen.mark(env.id, env.expires_at):
            # 重复消息：业务不再处理，但仍补发回执，让发送方状态机收敛
            self.log.info("msg.duplicate", msg=env.id, thread=env.thread)
            await self.sender.send_receipt(env, MessageType.RECEIPT_ACCEPTED, reason="重复消息")
            return

        record = self._tracker.on_incoming(env)
        if record is not None:
            self.log.info("delivery.state", msg=record.msg_id, state=str(record.state))
        if env.type.is_receipt:
            return  # 回执只推进状态机，不再进入 handler，也不再回执

        await self.sender.send_receipt(env, MessageType.RECEIPT_ACCEPTED)
        await self.handler.handle(env, self._ctx)

    async def _report_failure(self, env: Envelope, exc: Exception) -> None:
        if env.type is not MessageType.TASK_REQUEST:
            return
        try:
            error = env.reply(
                type=MessageType.TASK_ERROR,
                payload=TaskErrorPayload(error=f"{type(exc).__name__}: {exc}"[:8000]),
                sender=self.identity,
                recipient=env.from_,
            )
            await self.sender.send(error)
        except AntHillError as send_exc:
            self.log.error("task.error_report_failed", msg=env.id, error=str(send_exc))

    def _find_coordinator(self) -> str | None:
        for name, agent in sorted(self.config.agents.items()):
            if agent.role == COORDINATOR_ROLE and name != self.agent_name:
                return name
        return None
