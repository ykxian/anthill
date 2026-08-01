"""消息信封（Envelope）—— 系统的「宪法」，见 docs/02-protocol.md §1。

一条消息 = 一个 JSON 文件，文件名即 `id`（ULID）。
本模块只做「结构与规则」，不碰任何 IO —— 所以它可以被 100% 单测覆盖。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from anthill.core.errors import EnvelopeTooLarge, HopLimitExceeded, ProtocolError
from anthill.core.ids import is_valid_id, new_id, new_thread_id, now
from anthill.core.payloads import PAYLOAD_MODELS, MessageType, Payload

PROTO_VERSION = "1.0"
MAX_PAYLOAD_BYTES = 64 * 1024
"""大于此值请走 blackboard 产物目录，信封里只放路径引用（02-protocol §7）。"""

DEFAULT_TTL_HOPS = 8
DEFAULT_LIFETIME = timedelta(hours=1)

NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ROLE_PREFIX = "role:"
BROADCAST = "all"


class TransportKind(StrEnum):
    LOCAL = "local"
    LAN = "lan"
    SSH = "ssh"


class Address(BaseModel):
    """`node + agent` 二元寻址。agent 支持具体名 / `role:x` / `all` 三种形式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    agent: str

    @field_validator("node")
    @classmethod
    def _check_node(cls, v: str) -> str:
        if not NODE_NAME_RE.match(v):
            raise ValueError(f"非法节点名 {v!r}：只允许小写字母数字与 . _ -，≤64 字符")
        return v

    @field_validator("agent")
    @classmethod
    def _check_agent(cls, v: str) -> str:
        name = v[len(ROLE_PREFIX) :] if v.startswith(ROLE_PREFIX) else v
        if v == BROADCAST:
            return v
        if not AGENT_NAME_RE.match(name):
            raise ValueError(f"非法收件人 {v!r}：应为 name / role:name / all")
        return v

    @property
    def is_role(self) -> bool:
        return self.agent.startswith(ROLE_PREFIX)

    @property
    def is_broadcast(self) -> bool:
        return self.agent == BROADCAST

    @property
    def role(self) -> str | None:
        return self.agent[len(ROLE_PREFIX) :] if self.is_role else None

    def with_agent(self, agent: str) -> Address:
        """解析角色地址后得到具体地址；返回新对象，不改原对象。"""
        return Address(node=self.node, agent=agent)

    def __str__(self) -> str:
        return f"{self.node}:{self.agent}"


class ReplyVia(BaseModel):
    """回执/回复该走哪条路回来。跨节点时必须带，否则对方不知道怎么回。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: TransportKind = TransportKind.LOCAL
    endpoint: str | None = None


class Envelope(BaseModel):
    """一条消息的完整表示。frozen —— 任何「修改」都通过 `reply()` 之类返回新对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    proto: str = PROTO_VERSION
    id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=now)
    # 线上字段名是 `from`（Python 关键字），代码里用 from_，靠 alias 两头兼容
    from_: Address = Field(
        validation_alias=AliasChoices("from", "from_"), serialization_alias="from"
    )
    to: Address
    type: MessageType
    thread: str = Field(default_factory=new_thread_id)
    reply_to: str | None = None
    reply_via: ReplyVia = ReplyVia()
    hops: int = Field(default=1, ge=1)
    ttl_hops: int = Field(default=DEFAULT_TTL_HOPS, ge=1, le=64)
    expires_at: datetime | None = None
    payload: Payload
    sig: str | None = None

    # ---------- 校验 ----------

    @field_validator("proto")
    @classmethod
    def _check_proto(cls, v: str) -> str:
        if v.split(".", 1)[0] != PROTO_VERSION.split(".", 1)[0]:
            raise ValueError(f"协议主版本不兼容：本节点 {PROTO_VERSION}，来件 {v}")
        return v

    @field_validator("id", "thread")
    @classmethod
    def _check_ulid(cls, v: str) -> str:
        if not is_valid_id(v):
            raise ValueError(f"{v!r} 不是合法 ULID")
        return v

    @field_validator("reply_to")
    @classmethod
    def _check_reply_to(cls, v: str | None) -> str | None:
        if v is not None and not is_valid_id(v):
            raise ValueError(f"reply_to {v!r} 不是合法 ULID")
        return v

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """按 `type` 把 payload dict 变成对应的 pydantic 模型，并补 expires_at 默认值。"""
        if not isinstance(data, dict):
            return data
        raw = dict(data)  # 不修改调用方传进来的字典

        msg_type = raw.get("type")
        payload = raw.get("payload")
        if isinstance(msg_type, str) and isinstance(payload, dict):
            try:
                kind = MessageType(msg_type)
            except ValueError as exc:
                raise ValueError(f"未知消息类型 {msg_type!r}") from exc
            raw["payload"] = PAYLOAD_MODELS[kind].model_validate(payload)

        if raw.get("expires_at") is None:
            ts = raw.get("ts")
            base = ts if isinstance(ts, datetime) else now()
            raw["expires_at"] = base + DEFAULT_LIFETIME
        return raw

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        expected = PAYLOAD_MODELS[self.type]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"消息类型 {self.type} 需要 {expected.__name__}，"
                f"实际是 {type(self.payload).__name__}"
            )
        if self.hops > self.ttl_hops:
            raise ValueError(f"hops={self.hops} 已超过 ttl_hops={self.ttl_hops}")
        if self.to.is_broadcast and self.type is not MessageType.EVENT:
            raise ValueError("广播地址 all 只允许用于 event 类型")
        size = len(self.payload.model_dump_json().encode())
        if size > MAX_PAYLOAD_BYTES:
            raise EnvelopeTooLarge(
                f"payload {size} 字节超过上限 {MAX_PAYLOAD_BYTES}；请改用 artifacts 传路径"
            )
        return self

    # ---------- 构造 ----------

    @classmethod
    def new(
        cls,
        *,
        sender: Address,
        recipient: Address,
        type: MessageType,
        payload: Payload,
        thread: str | None = None,
        reply_to: str | None = None,
        reply_via: ReplyVia | None = None,
        hops: int = 1,
        ttl_hops: int = DEFAULT_TTL_HOPS,
    ) -> Envelope:
        return cls(
            from_=sender,
            to=recipient,
            type=type,
            payload=payload,
            thread=thread or new_thread_id(),
            reply_to=reply_to,
            reply_via=reply_via or ReplyVia(),
            hops=hops,
            ttl_hops=ttl_hops,
        )

    def reply(
        self,
        *,
        type: MessageType,
        payload: Payload,
        sender: Address | None = None,
        recipient: Address | None = None,
    ) -> Envelope:
        """构造一条「因本消息而产生」的新消息：同 thread、hops+1。

        hops 超限时抛 HopLimitExceeded —— 这是 A@B、B@A 死循环的熔断点。
        """
        next_hops = self.hops + 1
        if next_hops > self.ttl_hops:
            raise HopLimitExceeded(
                f"消息 {self.id} 在 thread {self.thread} 上已达 "
                f"{self.ttl_hops} 跳上限，拒绝继续转发"
            )
        return Envelope(
            from_=sender or self.to,
            to=recipient or self.from_,
            type=type,
            payload=payload,
            thread=self.thread,
            reply_to=self.id,
            reply_via=self.reply_via,
            hops=next_hops,
            ttl_hops=self.ttl_hops,
        )

    def with_recipient(self, recipient: Address) -> Envelope:
        """角色地址解析成具体地址后使用。返回新信封（id 保持不变，仍是同一条消息）。"""
        return self.model_copy(update={"to": recipient})

    # ---------- 序列化 ----------

    def to_json_bytes(self) -> bytes:
        return self.model_dump_json(by_alias=True, indent=2).encode()

    @classmethod
    def from_json_bytes(cls, data: bytes) -> Envelope:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"信封不是合法 JSON：{exc}") from exc
        return cls.model_validate(parsed)

    def canonical_bytes(self) -> bytes:
        """签名用的规范化字节串：去掉 sig、键排序、无多余空白（02-protocol §6）。"""
        data = self.model_dump(mode="json", by_alias=True, exclude={"sig"})
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    # ---------- 查询 ----------

    def is_expired(self, at: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (at or now()) > self.expires_at

    @property
    def short(self) -> str:
        """日志/终端里的一行摘要。"""
        return f"[{self.type}] {self.from_} → {self.to} #{self.id[-6:]} thread={self.thread[-6:]}"
