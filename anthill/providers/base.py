"""ChatProvider 抽象与它的数据模型。

这一层刻意与厂商无关：`Msg` / `ToolSpec` / `Turn` 是本项目自己的中立表示，
各家 SDK 的格式转换关在各自的 provider 实现里。新增一家模型 = 实现一个 ChatProvider，
loop / tools / orchestrator 一行都不用改。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from anthill.core.errors import ProviderError

TOOL_CALL_ID_PREFIX = "call_"
ASCII_CHARS_PER_TOKEN = 4


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：ASCII 约 4 字符 1 token，CJK 约 1 字 1 token。

    只用于预算熔断，宁可高估也不低估 —— 真实计数以 provider 回报的 usage 为准。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ch.isascii())
    return math.ceil(ascii_chars / ASCII_CHARS_PER_TOKEN) + (len(text) - ascii_chars)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求的一次工具调用。`arguments` 已是解析好的 dict。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass(frozen=True, slots=True)
class Msg:
    """一条对话消息。frozen：上下文只做「追加得到新列表」，不就地修改。"""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role is Role.TOOL and not self.tool_call_id:
            raise ValueError("role=tool 的消息必须带 tool_call_id，否则模型无法对上号")

    @property
    def approx_tokens(self) -> int:
        extra = sum(estimate_tokens(f"{c.name}{c.arguments}") for c in self.tool_calls)
        return estimate_tokens(self.content) + extra

    @classmethod
    def system(cls, content: str) -> Msg:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Msg:
        return cls(role=Role.USER, content=content)

    @classmethod
    def tool_result(cls, call_id: str, content: str, *, name: str | None = None) -> Msg:
        return cls(role=Role.TOOL, content=content, tool_call_id=call_id, name=name)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": str(self.role), "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [c.to_dict() for c in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Msg:
        return cls(
            role=Role(data["role"]),
            content=str(data.get("content") or ""),
            tool_calls=tuple(ToolCall.from_dict(c) for c in data.get("tool_calls") or ()),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )


def drop_orphan_tool_results(messages: list[Msg]) -> list[Msg]:
    """丢掉找不到对应 assistant 调用的 tool 结果。

    **任何裁剪历史的地方都必须过这一道。** 一条 `role=tool` 单独留下来时：
    OpenAI 侧是一条前面没有 tool_calls 的 tool 消息，Anthropic 侧是首条 user 里
    挂着一个不存在的 `tool_use_id` —— 两家都直接 400。

    这类缺陷有个讨厌的性质：**历史越长越容易踩**。短会话永远遇不到，
    跑长任务时预算恰好切在 assistant(tool_calls) 和它的结果之间就炸 ——
    也就是任务越接近成功越容易炸。压缩那条路（agent/memory.py）一直做了这件事，
    预算裁剪那条路（agent/context.py）曾经没做，两条路都通向同一个请求体。
    """
    known: set[str] = set()
    out: list[Msg] = []
    for msg in messages:
        if msg.role is Role.ASSISTANT:
            known.update(c.id for c in msg.tool_calls)
        if msg.role is Role.TOOL and msg.tool_call_id not in known:
            continue
        out.append(msg)
    return out


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """给模型看的工具描述。`parameters` 是标准 JSON Schema。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass(frozen=True, slots=True)
class Usage:
    """token 用量。相加返回新对象 —— 费用熔断靠它累计。"""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        if not data:
            return cls()
        return cls(
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
        )


@dataclass(frozen=True, slots=True)
class Turn:
    """一轮模型输出：可能是纯文本，也可能带若干工具调用。"""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "stop"

    def to_msg(self) -> Msg:
        return Msg(role=Role.ASSISTANT, content=self.text, tool_calls=self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "usage": self.usage.to_dict(),
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(
            text=str(data.get("text") or ""),
            tool_calls=tuple(ToolCall.from_dict(c) for c in data.get("tool_calls") or ()),
            usage=Usage.from_dict(data.get("usage")),
            stop_reason=str(data.get("stop_reason") or "stop"),
        )


RETRYABLE_HINTS = (
    "ratelimit",
    "timeout",
    "connection",
    "internalserver",
    "apistatus",
    "overloaded",
)
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def classify_sdk_error(exc: Exception, *, provider: str) -> ProviderError:
    """把各家 SDK 五花八门的异常收敛成一个带 retryable 标记的 ProviderError。

    判断依据优先取 HTTP 状态码（各家 SDK 都带 `status_code`），
    取不到再退化为看异常类名 —— 这样不必 import 任一家 SDK 就能分类。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        retryable = status in RETRYABLE_STATUS
    else:
        name = type(exc).__name__.lower()
        retryable = any(hint in name for hint in RETRYABLE_HINTS)
    return ProviderError(f"{provider} 调用失败：{type(exc).__name__}: {exc}", retryable=retryable)


class ChatProvider(ABC):
    """一轮 LLM 调用的统一入口。实现必须是无状态的（可被并发调用）。"""

    name: str = "provider"
    model: str = "unknown"

    @abstractmethod
    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        """把消息与工具交给模型，拿回一轮输出。失败抛 ProviderError。"""

    async def aclose(self) -> None:  # noqa: B027 - 故意可选：多数 provider 无连接可释放
        """释放连接。默认无操作。"""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
