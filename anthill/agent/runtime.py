"""agentd：一个 Agent 的守护进程主体（01-architecture §4）。

    watcher → 收件 → 校验 → 幂等去重 → 回执 accepted → handler → 归档

这一层刻意不含任何 LLM 逻辑：换 handler 就能从 echo agent 变成 LLM agent。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from anthill.agent.factory import build_handler
from anthill.agent.handlers import HandlerContext, MessageHandler
from anthill.agent.sender import Sender
from anthill.agent.tools.base import Confirmer
from anthill.agent.watcher import MailboxWatcher, WatchMode
from anthill.core.config import Config, check_runtime
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError, MailboxError, ProtocolError
from anthill.core.ids import now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import MessageType, TaskErrorPayload
from anthill.core.retention import SweepResult, rotate_log, sweep_archive, sweep_flat
from anthill.core.router import Router
from anthill.core.seen import Claim, SeenStore
from anthill.core.spool import Spool
from anthill.core.states import DeliveryTracker
from anthill.discovery.registry import PeerRegistry
from anthill.providers.registry import TapeMode
from anthill.security.signing import verify_envelope
from anthill.transport.registry import TransportRegistry

COORDINATOR_ROLE = "coordinator"
STATUS_FILE = "runtime.json"
DEFAULT_TICK_INTERVAL = 5.0
SWEEP_INTERVAL = 3600.0
"""多久清一次归档。一小时够慢（清理不该占资源），也够快（不会攒到磁盘满）。"""


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
        mode: TapeMode = TapeMode.LIVE,
        tape: Path | None = None,
        confirm: Confirmer | None = None,
        tick_interval: float = DEFAULT_TICK_INTERVAL,
    ) -> None:
        # fail fast，别等收到消息才发现没配好；回放模式不连上游，也就不需要 API key
        check_runtime(
            config, layout, agent_name, require_provider_key=(mode is not TapeMode.REPLAY)
        )

        self.layout = layout
        self.config = config
        self.agent_name = agent_name
        self.tick_interval = tick_interval
        self.agent_config = config.agent(agent_name)
        self.identity = Address(node=config.node.name, agent=agent_name)
        self.mailbox = Mailbox(layout.mailbox_dir(agent_name)).ensure()
        # 配了 provider 就是 LLM 大脑，没配就是 echo —— 判断只在 factory 里做一次
        self.handler: MessageHandler = handler or build_handler(
            layout=layout,
            config=config,
            agent_name=agent_name,
            mode=mode,
            tape=tape,
            confirm=confirm,
        )
        self.log = log or EventLog(layout.log_file(agent_name), agent=agent_name, echo=echo)

        self._seen: SeenStore = self.mailbox.open_seen()
        self._tracker = DeliveryTracker()
        self._router = Router(config, layout)
        self._peers = PeerRegistry(layout.root, self_name=config.node.name)
        self._transports = TransportRegistry(config, layout, peers=self._peers, log=self.log)
        self.sender = Sender(
            identity=self.identity,
            mailbox=self.mailbox,
            router=self._router,
            transports=self._transports,
            tracker=self._tracker,
            log=self.log,
            coordinator=self._find_coordinator(),
            spool=Spool(layout.root) if config.runtime.spool_unroutable else None,
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
        # handler 是在同步的 __init__ 里造的，异步的准备工作（连外部 MCP server）
        # 只能放在这儿。和 `tick` 一样用鸭子类型，免得所有 handler 都得实现一个空方法。
        setup = getattr(self.handler, "setup", None)
        if setup is not None:
            await setup(self._ctx)
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
            asyncio.create_task(self._sweep_loop(stop), name="sweep"),
        ]
        if hasattr(self.handler, "tick"):
            # 只有需要「时间驱动」的 handler（coordinator 的催办与超时）才起这个任务
            workers.append(asyncio.create_task(self._tick_loop(stop), name="tick"))
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

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        """定时唤醒 handler。tick 抛错只记日志 —— 一次扫描失败不该杀掉整个 agentd。"""
        tick = getattr(self.handler, "tick", None)
        if tick is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.tick_interval)
                return  # stop 被 set，正常退出
            except TimeoutError:
                pass
            try:
                await tick(self._ctx)
            except Exception as exc:
                self.log.error("tick.failed", error=f"{type(exc).__name__}: {exc}")

    def _startup_recovery(self) -> None:
        """崩溃恢复：cur 里没处理完的退回 new，tmp 里的半成品清掉，seen.db 滚动清理。"""
        recovered = self.mailbox.recover_stale()
        swept = self.mailbox.sweep_tmp()
        purged = self._seen.purge(timedelta(days=self.config.runtime.keep_days))
        if recovered or swept or purged:
            self.log.info(
                "agentd.recover", requeued=len(recovered), swept_tmp=swept, purged_seen=purged
            )
        self._sweep()

    def _sweep(self) -> None:
        """给所有单调增长的目录踩一次刹车。见 core/retention.py。

        只在启动时清一次是不够的：agentd 一跑就是几天，而归档量是消息量的两倍以上
        （每条业务消息还额外带一条回执信封）。所以启动清一次，之后定时再清。
        """
        keep = self.config.runtime.keep_days
        result = SweepResult(
            done_days=sweep_archive(self.mailbox.done, keep_days=keep),
            sent=sweep_flat(self.mailbox.sent, keep_days=keep),
            # 死信单独一个更长的保留期 —— 它是「需要人处理」的东西，
            # 跟着归档一起清等于把问题藏起来
            dead=sweep_flat(self.mailbox.dead, keep_days=self.config.runtime.dead_keep_days),
            logs_rotated=int(
                rotate_log(
                    self.layout.log_file(self.agent_name),
                    max_bytes=self.config.runtime.log_max_mb * 1024 * 1024,
                )
            ),
        )
        if not result.touched:
            return
        self.log.info(
            "agentd.sweep",
            done_days=result.done_days,
            sent=result.sent,
            dead=result.dead,
            rotated=result.logs_rotated,
        )
        if result.dead:
            # 删死信要吼一声：那是本来该有人看一眼的东西
            self.log.warn("agentd.dead_expired", count=result.dead, kept_days=keep)

    async def _sweep_loop(self, stop: asyncio.Event) -> None:
        """定时清理。抛错只记日志 —— 清不掉不该杀掉整个 agentd。"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self._sweep)
            except OSError as exc:
                self.log.error("agentd.sweep_failed", error=f"{type(exc).__name__}: {exc}")

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
        await self._close_handler()
        await self._transports.close()
        self._seen.close()
        self.status_path.unlink(missing_ok=True)
        self.log.info("agentd.stop")
        self.log.close()

    async def _close_handler(self) -> None:
        """handler 可选地实现 aclose（LLM handler 用它关上游连接）。关不掉也不该影响退出。"""
        closer = getattr(self.handler, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception as exc:
            self.log.warn("handler.close_failed", error=f"{type(exc).__name__}: {exc}")

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
            self._check_signature(env)
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
            # **归档之后**才落 completed。反过来会开一扇窗：落了 completed 却还没归档时
            # 崩溃，recover_stale 把信退回 new/，而 seen.db 说「干完了」—— 这条就丢了处理。
            self._seen.complete(env.id)

    def _check_signature(self, env: Envelope) -> None:
        """跨节点来件必须验签 —— 只要我们持有对方的密钥。

        邮箱就是一个目录：在共用的服务器上，同机器的其他账号也能往里面写文件。
        SSH/LAN 通道本身的加密拦不住这种「本地伪造投递」，签名才拦得住。

        不查时间窗（`max_skew=None`）：邮箱是存储转发队列，agentd 停机几小时
        再启动是正常的，按时间窗判会把一堆合法消息误杀。重放由 seen.db 兜。
        """
        if env.from_.node == self.identity.node:
            return
        key = self._peers.key_for(env.from_.node)
        if key is None:
            return  # 没配密钥就没法验；这种情况下投递本来也进不来
        verify_envelope(env, key, max_skew=None)

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

        claim = self._seen.claim(env.id, env.expires_at)
        if claim is Claim.DUPLICATE:
            # 重复消息：业务不再处理，但仍补发回执，让发送方状态机收敛
            self.log.info("msg.duplicate", msg=env.id, thread=env.thread)
            await self.sender.send_receipt(env, MessageType.RECEIPT_ACCEPTED, reason="重复消息")
            return
        if claim is Claim.RETRY:
            # 上一个进程实例领了没干完（多半 kill -9）。recover_stale 把信退回了 new/，
            # 这里必须真的重跑 —— 「至少一次」就落在这一行上。
            self.log.warn("msg.retry_after_crash", msg=env.id, thread=env.thread)

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
