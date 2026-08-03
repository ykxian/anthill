"""发送侧：路由 → 熔断检查 → 落 outbox → 投递 → 重试 → 死信上报。

「先落盘再发送」是这里的关键：进程随时可能被 kill -9，
只要信封已经在 outbox/pending，重启后重试循环会把它捡起来继续发。
"""

from __future__ import annotations

import asyncio

from anthill.core.envelope import Address, Envelope, ReplyVia, TransportKind
from anthill.core.errors import DeliveryError, HopLimitExceeded, UnknownRecipient
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox, OutboxEntry
from anthill.core.payloads import EventPayload, MessageType, Payload, ReceiptPayload
from anthill.core.router import Router
from anthill.core.states import DeliveryState, DeliveryTracker
from anthill.transport.base import DeliveryResult
from anthill.transport.registry import TransportRegistry

DEAD_LETTER_EVENT = "delivery.dead_letter"
RETRY_TICK = 1.0


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
    ) -> None:
        self.identity = identity
        self._outbox = Outbox(mailbox)
        self._router = router
        self._transports = transports
        self._tracker = tracker
        self._log = log
        self._coordinator = coordinator
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
        env = Envelope.new(
            sender=self.identity,
            recipient=to,
            type=type,
            payload=payload,
            thread=thread,
            reply_to=reply_to,
            hops=hops,
            reply_via=ReplyVia(transport=TransportKind.LOCAL),
        )
        await self.send(env)
        return env

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
        entry = self._outbox.enqueue(env)
        self._tracker.register(env)
        return await self._attempt(entry)

    async def _retry(self, entry: OutboxEntry) -> DeliveryResult:
        self._log.warn(
            "delivery.retry",
            msg=entry.msg_id,
            attempts=entry.attempts,
            to=str(entry.envelope.to),
        )
        return await self._attempt(entry)

    async def _attempt(self, entry: OutboxEntry) -> DeliveryResult:
        self._inflight.add(entry.msg_id)
        try:
            return await self._attempt_inner(entry)
        finally:
            self._inflight.discard(entry.msg_id)

    async def _attempt_inner(self, entry: OutboxEntry) -> DeliveryResult:
        env = entry.envelope
        try:
            result = await self._transports.deliver(env)
        except DeliveryError as exc:
            result = DeliveryResult(
                ok=False, transport=TransportKind.LOCAL, destination=str(env.to), detail=str(exc)
            )
            if not exc.retryable:
                # 先移出 pending 再上报，否则重试循环会一直捡起它反复报死信
                abandoned = self._outbox.abandon(entry, str(exc))
                await self._give_up(abandoned, str(exc))
                return result

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
            return result

        updated = self._outbox.mark_failed(entry, result.detail or "未知错误")
        self._log.warn(
            "delivery.failed",
            msg=env.id,
            to=str(env.to),
            attempts=updated.attempts,
            error=result.detail,
        )
        if updated.is_dead:
            await self._give_up(updated, result.detail or "重试耗尽")
        return result

    async def _give_up(self, entry: OutboxEntry, error: str) -> None:
        """死信：标记状态 + 上报 coordinator。上报失败只记日志，绝不递归上报。"""
        env = entry.envelope
        self._tracker.mark(env.id, DeliveryState.DEAD, detail=error)
        self._log.error(
            "delivery.dead", msg=env.id, to=str(env.to), attempts=entry.attempts, error=error
        )
        if self._coordinator is None or self._is_dead_letter_report(env):
            return
        report = Envelope.new(
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
        try:
            await self.send(report)
        except (DeliveryError, UnknownRecipient, HopLimitExceeded) as exc:
            self._log.error("delivery.dead_report_failed", msg=env.id, error=str(exc))

    @staticmethod
    def _is_dead_letter_report(env: Envelope) -> bool:
        return env.type is MessageType.EVENT and getattr(env.payload, "kind", "") == (
            DEAD_LETTER_EVENT
        )
