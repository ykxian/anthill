"""发送侧：路由 → 熔断检查 → 落 outbox → 投递 → 重试 → 死信上报。

「先落盘再发送」是这里的关键：进程随时可能被 kill -9，
只要信封已经在 outbox/pending，重启后重试循环会把它捡起来继续发。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from anthill.core.envelope import Address, Envelope, ReplyVia, TransportKind
from anthill.core.errors import (
    DeliveryError,
    HopLimitExceeded,
    MailboxError,
    UnknownRecipient,
    UnroutableNode,
)
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox, OutboxEntry
from anthill.core.payloads import EventPayload, MessageType, Payload, ReceiptPayload
from anthill.core.router import Router
from anthill.core.spool import Spool
from anthill.core.states import DeliveryState, DeliveryTracker
from anthill.transport.base import DeliveryResult, Destination
from anthill.transport.registry import TransportRegistry

DEAD_LETTER_EVENT = "delivery.dead_letter"
RETRY_TICK = 1.0


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    result: DeliveryResult
    dead_report: Envelope | None = None
    dead_msg_id: str | None = None


class Sender:
    def __init__(
        self,
        *,
        identity: Address,
        mailbox: Mailbox,
        router: Router,
        transports: TransportRegistry,
        tracker: DeliveryTracker,
        log: EventLog,
        coordinator: str | None = None,
        spool: Spool | None = None,
    ) -> None:
        self.identity = identity
        self._outbox = Outbox(mailbox)
        self._router = router
        self._transports = transports
        self._tracker = tracker
        self._log = log
        self._coordinator = coordinator
        self._spool = spool
        # 正在投递中的消息 id。outbox/pending 是「待发」的持久化真相，
        # 但首次投递期间信封也还躺在 pending 里 —— 不挡一下的话重试循环会抢着重发一遍。
        self._inflight: set[str] = set()

    # ---------- 对外 ----------

    async def send(self, env: Envelope) -> tuple[DeliveryResult, ...]:
        """发一条消息。角色/广播地址会在这里展开成多条具体投递。"""
        Router.check_hops(env)
        try:
            resolved = self._router.resolve(env)
        except UnknownRecipient as exc:
            self._log.error("route.failed", to=str(env.to), msg=env.id, error=str(exc))
            raise
        return tuple([await self._deliver_once(item) for item in resolved])

    async def send_new(
        self,
        *,
        to: Address,
        type: MessageType,
        payload: Payload,
        thread: str | None = None,
        reply_to: str | None = None,
        hops: int = 1,
    ) -> Envelope:
        env = self.prepare_new(
            to=to,
            type=type,
            payload=payload,
            thread=thread,
            reply_to=reply_to,
            hops=hops,
        )
        await self.send(env)
        return env

    def prepare_new(
        self,
        *,
        to: Address,
        type: MessageType,
        payload: Payload,
        thread: str | None = None,
        reply_to: str | None = None,
        hops: int = 1,
    ) -> Envelope:
        """只构造信封，不投递。

        bridge 需要先把这份信封落盘，再尝试发送；否则 send 成功后的任意异常会
        让整份草稿重跑，并生成一个接收端无法按 ID 去重的新信封。
        """
        return Envelope.new(
            sender=self.identity,
            recipient=to,
            type=type,
            payload=payload,
            thread=thread,
            reply_to=reply_to,
            hops=hops,
            reply_via=ReplyVia(transport=TransportKind.LOCAL),
        )

    async def send_receipt(
        self, source: Envelope, kind: MessageType, *, reason: str | None = None
    ) -> Envelope | None:
        """回执沿原路返回。回执本身不再产生回执，否则会无限套娃。"""
        if not kind.is_receipt:
            raise ValueError(f"{kind} 不是回执类型")
        if source.type.is_receipt:
            return None
        try:
            env = source.reply(
                type=kind,
                payload=ReceiptPayload(ref=source.id, reason=reason),
                sender=self.identity,
                recipient=source.from_,
            )
        except HopLimitExceeded:
            # 回执被跳数卡住说明链路已经异常长了，记一笔但不让它冒泡打断主流程
            self._log.warn("receipt.hop_limit", msg=source.id, thread=source.thread)
            return None
        await self.send(env)
        return env

    async def retry_due(self) -> None:
        """把到点该重试的 pending 条目再发一次。由 runtime 的定时任务调用。"""
        for entry in self._outbox.due():
            if entry.msg_id in self._inflight:
                continue  # 首次投递还在路上，别抢
            await self._retry(entry)

    async def run_retry_loop(self, stop: asyncio.Event, *, tick: float = RETRY_TICK) -> None:
        while not stop.is_set():
            try:
                await self.retry_due()
            except Exception as exc:  # 重试循环绝不能因为单条消息挂掉
                self._log.error("retry.loop_error", error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except TimeoutError:
                continue

    # ---------- 内部 ----------

    async def _deliver_once(self, env: Envelope) -> DeliveryResult:
        lease = self._outbox.delivery_lock(env.id)
        while not lease.try_acquire():
            # 另一个进程正在投同一 Envelope。检查状态和创建 pending 也必须
            # 等它释放；否则 sent 检查与 enqueue 之间会重新造出同 ID pending。
            await asyncio.sleep(0.02)
        self._inflight.add(env.id)
        outcome: _AttemptOutcome | None = None
        try:
            if self._outbox.sent_envelope(env) is not None:
                return self._already_sent(env)
            entry = self._outbox.enqueue(env)
            if self._tracker.get(env.id) is None:
                self._tracker.register(env)
            if entry.attempts:
                # 活跃投递者刚失败并写好了退避。动作重入只能观察这个结果，
                # 不能抢在 retry scheduler 前再次调用 transport、快速耗尽 attempts。
                return self._pending_failure(entry)
            outcome = await self._attempt_inner(entry)
        finally:
            self._inflight.discard(env.id)
            lease.release()
        return await self._finish_attempt(outcome)

    async def _retry(self, entry: OutboxEntry) -> DeliveryResult | None:
        lease = self._outbox.delivery_lock(entry.msg_id)
        if not lease.try_acquire():
            return None
        self._inflight.add(entry.msg_id)
        outcome: _AttemptOutcome | None = None
        try:
            env = entry.envelope
            if self._outbox.sent_envelope(env) is not None:
                return self._already_sent(env)
            current = self._outbox.pending_entry(env)
            if current is None:
                return None  # 另一进程已经把它移到 dead；这份 due 快照作废
            if not current.is_due():
                return None  # 竞争者刚写下失败退避，必须等下一轮 scheduler
            self._log.warn(
                "delivery.retry",
                msg=current.msg_id,
                attempts=current.attempts,
                to=str(current.envelope.to),
            )
            outcome = await self._attempt_inner(current)
        finally:
            self._inflight.discard(entry.msg_id)
            lease.release()
        return await self._finish_attempt(outcome)

    @staticmethod
    def _pending_failure(entry: OutboxEntry) -> DeliveryResult:
        return DeliveryResult.failure(
            TransportKind.LOCAL,
            Destination(node=entry.envelope.to.node, agent=entry.envelope.to.agent),
            entry.last_error or "消息已在等待重试",
        )

    def _already_sent(self, env: Envelope) -> DeliveryResult:
        detail = str(self._outbox.sent_path(env.id))
        record = self._tracker.get(env.id)
        if record is None:
            self._tracker.register(env)
            record = self._tracker.get(env.id)
        if record is not None and record.state is DeliveryState.PENDING:
            self._tracker.mark(env.id, DeliveryState.DELIVERED, detail=detail)
        self._log.info("delivery.already_sent", msg=env.id, to=str(env.to), thread=env.thread)
        return DeliveryResult.success(
            TransportKind.LOCAL,
            Destination(node=env.to.node, agent=env.to.agent),
            detail,
        )

    async def _attempt_inner(self, entry: OutboxEntry) -> _AttemptOutcome:
        env = entry.envelope
        try:
            result = await self._transports.deliver(env)
        except UnroutableNode as exc:
            spooled = self._try_spool(env, exc)
            if spooled is not None:
                self._outbox.mark_sent(entry)
                self._tracker.mark(env.id, DeliveryState.DELIVERED, detail=str(spooled))
                return _AttemptOutcome(
                    DeliveryResult.success(
                        TransportKind.LOCAL,
                        Destination(node=env.to.node, agent=env.to.agent),
                        str(spooled),
                    )
                )
            abandoned = self._outbox.abandon(entry, str(exc))
            report = self._give_up(abandoned, str(exc))
            result = DeliveryResult(
                ok=False, transport=TransportKind.LOCAL, destination=str(env.to), detail=str(exc)
            )
            return _AttemptOutcome(result, report, env.id)
        except DeliveryError as exc:
            result = DeliveryResult(
                ok=False, transport=TransportKind.LOCAL, destination=str(env.to), detail=str(exc)
            )
            if not exc.retryable:
                # 先移出 pending 再上报，否则重试循环会一直捡起它反复报死信
                abandoned = self._outbox.abandon(entry, str(exc))
                report = self._give_up(abandoned, str(exc))
                return _AttemptOutcome(result, report, env.id)

        if result.ok:
            self._outbox.mark_sent(entry)
            # 本地传输 rename 成功即视为 delivered，不再单发一条 receipt.delivered
            self._tracker.mark(env.id, DeliveryState.DELIVERED, detail=result.path)
            self._log.info(
                "delivery.ok",
                msg=env.id,
                type=str(env.type),
                to=str(env.to),
                thread=env.thread,
                transport=str(result.transport),
            )
            return _AttemptOutcome(result)

        updated = self._outbox.mark_failed(entry, result.detail or "未知错误")
        self._log.warn(
            "delivery.failed",
            msg=env.id,
            to=str(env.to),
            attempts=updated.attempts,
            error=result.detail,
        )
        report = None
        if updated.is_dead:
            report = self._give_up(updated, result.detail or "重试耗尽")
        return _AttemptOutcome(result, report, env.id if report is not None else None)

    def _try_spool(self, env: Envelope, exc: UnroutableNode) -> object | None:
        """路由不到就暂存，等对方来拉（SSH 场景：服务器连不回笔记本）。

        没开暂存就返回 None，走原来的死信流程 —— 默认行为不变。
        """
        if self._spool is None:
            return None
        try:
            path = self._spool.deposit(env)
        except (MailboxError, OSError) as spool_exc:
            self._log.error("spool.failed", msg=env.id, error=str(spool_exc))
            return None
        self._log.info(
            "delivery.spooled",
            msg=env.id,
            to=str(env.to),
            type=str(env.type),
            reason=str(exc),
            path=str(path),
        )
        return path

    def _give_up(self, entry: OutboxEntry, error: str) -> Envelope | None:
        """锁内完成死信状态并准备报告；真正发送必须等原消息锁释放。"""
        env = entry.envelope
        self._tracker.mark(env.id, DeliveryState.DEAD, detail=error)
        self._log.error(
            "delivery.dead", msg=env.id, to=str(env.to), attempts=entry.attempts, error=error
        )
        if self._coordinator is None or self._is_dead_letter_report(env):
            return None
        return Envelope.new(
            sender=self.identity,
            recipient=Address(node=self.identity.node, agent=self._coordinator),
            type=MessageType.EVENT,
            payload=EventPayload(
                kind=DEAD_LETTER_EVENT,
                data={
                    "msg": env.id,
                    "to": str(env.to),
                    "type": str(env.type),
                    "thread": env.thread,
                    "error": error[:500],
                },
            ),
            thread=env.thread,
        )

    async def _finish_attempt(self, outcome: _AttemptOutcome) -> DeliveryResult:
        """原 delivery lease 已释放；此时递归发送报告不会发生同桶自锁/ABBA。"""
        report = outcome.dead_report
        if report is None:
            return outcome.result
        try:
            await self.send(report)
        except (DeliveryError, UnknownRecipient, HopLimitExceeded) as exc:
            self._log.error(
                "delivery.dead_report_failed", msg=outcome.dead_msg_id or "", error=str(exc)
            )
        return outcome.result

    @staticmethod
    def _is_dead_letter_report(env: Envelope) -> bool:
        return env.type is MessageType.EVENT and getattr(env.payload, "kind", "") == (
            DEAD_LETTER_EVENT
        )
