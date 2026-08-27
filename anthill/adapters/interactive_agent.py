"""交互式 Agent 宿主的通用 inbox 驱动。

Claude Code、Codex 以及后续接入的终端 Agent，区别在于怎样唤醒宿主、怎样判断
宿主空闲，以及怎样拿到最终回答。其余文件信箱语义必须完全一致：逐封取 inbox、
保护人工 outbox、通知只归档、需要回复时写同名草稿、失败后不要在热循环里重试。

宿主适配器继承 :class:`InteractiveAgentBridge`，只需实现 :meth:`deliver`；若宿主
不能在忙碌时接收新 turn，再覆盖 :meth:`wait_until_available`。``deliver`` 必须等
一次宿主 turn 成功结束后返回；宿主失败应抛 :class:`AntHillError` 的子类。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anthill.adapters.bridge import (
    DONE,
    INBOX,
    OUTBOX,
    PENDING,
    BridgeHandler,
    note_needs_reply,
    parse_note,
)
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout

DEFAULT_POLL_INTERVAL = 0.5
MAX_REPLY_CHARS = 30_000
NO_REPLY_SENTINEL = "ANTHILL_NO_REPLY"


class InteractiveAgentBridgeError(AntHillError):
    """通用信箱驱动无法读取来信或收齐宿主回答。"""


@dataclass(frozen=True, slots=True)
class InboxMessage:
    """已经解析、即将交给宿主的一封 bridge/inbox 来信。"""

    path: Path
    headers: dict[str, str]
    body: str
    needs_reply: bool

    @property
    def id(self) -> str:
        return self.path.stem


@dataclass(frozen=True, slots=True)
class HostTurn:
    """宿主成功完成的一轮；非成功状态应由宿主适配器转换成异常。"""

    id: str
    answer: str = ""


class InteractiveAgentBridge(ABC):
    """把 AntHill 文件信箱交付给一个交互式 Agent 宿主。

    子类拥有宿主协议，基类拥有信箱状态机。这条边界保证新增宿主不会各自重新实现
    回复、ack、静默和失败语义。
    """

    def __init__(
        self,
        *,
        layout: NodeLayout,
        agent: str,
        log: EventLog,
        event_prefix: str,
        host_name: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.layout = layout
        self.agent = agent
        self.log = log
        self.handler = BridgeHandler(root=layout.agent_dir(agent), agent_name=agent)
        self.event_prefix = event_prefix
        self.host_name = host_name
        self.poll_interval = poll_interval
        self._failed: set[str] = set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            note = self._next_message()
            if note is None:
                await wait_or_stop(stop, self.poll_interval)
                continue
            try:
                await self.wait_until_available(stop)
                if stop.is_set():
                    return
                await self._process(note, stop)
            except asyncio.CancelledError:
                raise
            except AntHillError as exc:
                if await self.retry_after_error(exc):
                    self.log.info(f"{self.event_prefix}.queued", file=note.name, reason=str(exc))
                    await wait_or_stop(stop, self.poll_interval)
                    continue
                self._failed.add(note.name)
                self.log.error(f"{self.event_prefix}.failed", file=note.name, error=str(exc))

    def _next_message(self) -> Path | None:
        """稳定排序；失败项和已有人工草稿的项不阻塞后续来信。"""
        for path in sorted(self.handler.dir(INBOX).glob("*.md")):
            if path.name in self._failed:
                continue
            if (self.handler.dir(OUTBOX) / path.name).is_file():
                continue
            return path
        return None

    async def wait_until_available(self, stop: asyncio.Event) -> None:
        """等宿主可接收新 turn；支持原生排队的宿主无需覆盖。"""
        return None

    @abstractmethod
    async def deliver(self, message: InboxMessage, stop: asyncio.Event) -> HostTurn | None:
        """提交来信并等待成功结果；因 stop 提前退出时返回 ``None``。"""

    async def retry_after_error(self, error: AntHillError) -> bool:
        """仅瞬时竞态返回真；其余失败在本进程生命周期内隔离。"""
        return False

    def after_delivery(self, message: InboxMessage, turn: HostTurn) -> None:
        """信箱处理成功后的宿主清理钩子。"""
        return None

    async def _process(self, path: Path, stop: asyncio.Event) -> None:
        try:
            headers, body = parse_note(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise InteractiveAgentBridgeError(f"读不了来信 {path}：{exc}") from exc
        message = InboxMessage(
            path=path,
            headers=headers,
            body=body,
            needs_reply=note_needs_reply(headers),
        )
        turn = await self.deliver(message, stop)
        if turn is None:
            return
        if message.needs_reply:
            if not turn.answer.strip():
                raise InteractiveAgentBridgeError(f"{self.host_name} turn {turn.id} 没有最终回答")
            if _suppresses_chat_reply(message.headers, turn.answer):
                self._ack(path)
                self.log.info(f"{self.event_prefix}.suppressed", file=path.name, turn=turn.id)
            else:
                self._write_reply(path, turn.answer)
                self.log.info(
                    f"{self.event_prefix}.replied",
                    file=path.name,
                    turn=turn.id,
                    chars=len(turn.answer),
                )
        else:
            self._ack(path)
            self.log.info(f"{self.event_prefix}.acked", file=path.name, turn=turn.id)
        self.after_delivery(message, turn)

    def _write_reply(self, source: Path, text: str) -> None:
        target = self.handler.dir(OUTBOX) / source.name
        if target.is_file():
            return  # 人在宿主 turn 期间手动回了：人的草稿优先，绝不覆盖。
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text.strip()[:MAX_REPLY_CHARS], encoding="utf-8")
        temporary.replace(target)

    def _ack(self, source: Path) -> None:
        done = self.handler.dir(DONE)
        envelope = self.handler.dir(PENDING) / f"{source.stem}.json"
        if envelope.is_file():
            envelope.replace(done / envelope.name)
        if source.is_file():
            source.replace(done / source.name)


def _suppresses_chat_reply(headers: dict[str, str], answer: str) -> bool:
    """纯 chat 才允许静默收口；任务必须始终交付结果。"""
    return headers.get("type", "chat") == "chat" and answer.strip() == NO_REPLY_SENTINEL


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """等待轮询间隔，但 stop 一到就立即返回。"""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
