"""OpenAI 兼容端点：OpenAI / DeepSeek / Qwen / GLM 共用一份代码。

差异只在 `base_url` 与 `model`，所以不引入 litellm 这类重依赖 ——
一个 SDK + 一个可覆写的 base_url 就够了（03-tech-design §1）。
"""

from __future__ import annotations

import json
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


def to_openai_messages(messages: list[Msg]) -> list[dict[str, Any]]:
    """本项目的 Msg → OpenAI chat.completions 的 messages 数组。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role is Role.TOOL:
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
            continue
        item: dict[str, Any] = {"role": str(msg.role), "content": msg.content}
        if msg.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in msg.tool_calls
            ]
        out.append(item)
    return out


def parse_openai_response(raw: Any) -> Turn:
    """SDK 响应对象 → Turn。参数解析失败不抛错，交给工具层回一条可读的错误结果。"""
    choices = getattr(raw, "choices", None) or []
    if not choices:
        raise ProviderError("上游返回里没有 choices，无法解析")
    choice = choices[0]
    message = choice.message
    calls = tuple(
        ToolCall(
            id=str(call.id),
            name=str(call.function.name),
            arguments=_loads_arguments(call.function.arguments),
        )
        for call in (getattr(message, "tool_calls", None) or ())
    )
    usage = getattr(raw, "usage", None)
    return Turn(
        text=getattr(message, "content", None) or "",
        tool_calls=calls,
        usage=Usage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        ),
        stop_reason=str(getattr(choice, "finish_reason", None) or "stop"),
    )


def _loads_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw__": raw}  # 模型偶尔吐非法 JSON，保留原文好排查
    return parsed if isinstance(parsed, dict) else {"__raw__": raw}


class OpenAICompatProvider(ChatProvider):
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai_compat",
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
        """延迟建客户端：没装 openai 也能 import 本模块（单测不需要 SDK）。"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - 取决于安装环境
                raise ProviderError("缺少 openai 依赖；执行 `uv sync --extra llm` 安装") from exc
            self._client = AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url, timeout=self._timeout
            )
        return self._client

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        try:
            raw = await self._get_client().chat.completions.create(**payload)
        except Exception as exc:
            raise classify_sdk_error(exc, provider=self.name) from exc
        return parse_openai_response(raw)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
