"""Anthropic Messages API。

与 OpenAI 的三处结构差异都在本文件里消化掉：
system 是顶层参数不是消息、内容是 block 列表、工具结果作为 user 消息回传。
"""

from __future__ import annotations

from typing import Any

from anthill.core.errors import ProviderError
from anthill.providers.base import (
    ChatProvider,
    Msg,
    Role,
    ToolCall,
    ToolSpec,
    Turn,
    Usage,
    classify_sdk_error,
)

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 120.0


def split_system(messages: list[Msg]) -> tuple[str, list[Msg]]:
    """Anthropic 的 system 是顶层参数：把 system 消息拆出来拼成一段。"""
    system = "\n\n".join(m.content for m in messages if m.role is Role.SYSTEM and m.content)
    rest = [m for m in messages if m.role is not Role.SYSTEM]
    return system, rest


def to_anthropic_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    """连续的工具结果必须合并进同一条 user 消息，否则 API 会拒绝。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role is Role.TOOL:
            block = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id,
                "content": msg.content,
            }
            if out and out[-1]["role"] == "user" and _is_tool_result_batch(out[-1]):
                out[-1] = {"role": "user", "content": [*out[-1]["content"], block]}
            else:
                out.append({"role": "user", "content": [block]})
            continue

        blocks: list[dict[str, Any]] = []
        if msg.content:
            blocks.append({"type": "text", "text": msg.content})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in msg.tool_calls
        )
        if blocks:
            out.append({"role": str(msg.role), "content": blocks})
    return out


def _is_tool_result_batch(item: dict[str, Any]) -> bool:
    content = item.get("content")
    return isinstance(content, list) and all(b.get("type") == "tool_result" for b in content)


def parse_anthropic_response(raw: Any) -> Turn:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in getattr(raw, "content", None) or ():
        kind = getattr(block, "type", None)
        if kind == "text":
            texts.append(str(getattr(block, "text", "")))
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", None) or {}),
                )
            )
    usage = getattr(raw, "usage", None)
    return Turn(
        text="\n".join(texts),
        tool_calls=tuple(calls),
        usage=Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        ),
        stop_reason=str(getattr(raw, "stop_reason", None) or "stop"),
    )


class AnthropicProvider(ChatProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        name: str = "anthropic",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - 取决于安装环境
                raise ProviderError("缺少 anthropic 依赖；执行 `uv sync --extra llm` 安装") from exc
            kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        system, rest = split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_anthropic_messages(rest),
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [t.to_anthropic() for t in tools]
        try:
            raw = await self._get_client().messages.create(**payload)
        except Exception as exc:
            raise classify_sdk_error(exc, provider=self.name) from exc
        return parse_anthropic_response(raw)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
