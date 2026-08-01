"""按 node.toml 装配 provider，并挂上录制/回放包装。

密钥只从环境变量取（配置里存的是变量名）；取不到就 fail fast，
不要等到第一次调用模型才在半路报 401。
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from anthill.core.config import Config, ProviderSection
from anthill.core.errors import ProviderError
from anthill.providers.anthropic_p import AnthropicProvider
from anthill.providers.base import ChatProvider
from anthill.providers.openai_compat import OpenAICompatProvider
from anthill.providers.record import RecordingProvider, ReplayProvider


class TapeMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


def build_provider(section: ProviderSection, *, name: str) -> ChatProvider:
    api_key = os.environ.get(section.api_key_env)
    if not api_key:
        raise ProviderError(
            f"provider {name} 需要环境变量 {section.api_key_env}，但它没设置"
            f" → export {section.api_key_env}=..."
        )
    kind: type[AnthropicProvider] | type[OpenAICompatProvider] = (
        AnthropicProvider if section.kind == "anthropic" else OpenAICompatProvider
    )
    return kind(
        model=section.model,
        api_key=api_key,
        base_url=section.base_url,
        name=name,
        max_tokens=section.max_tokens,
        temperature=section.temperature,
        timeout=section.timeout,
    )


def provider_for_agent(
    config: Config,
    agent_name: str,
    *,
    mode: TapeMode = TapeMode.LIVE,
    tape: Path | None = None,
) -> ChatProvider | None:
    """没配 provider 的 Agent 返回 None（= echo agent，不调模型）。"""
    if mode is TapeMode.REPLAY:
        if tape is None:
            raise ProviderError("replay 模式必须给出录制带路径")
        return ReplayProvider.from_file(tape)

    section = config.provider_for(agent_name)
    if section is None:
        return None
    name = config.agent(agent_name).provider or "provider"
    provider = build_provider(section, name=name)
    if mode is TapeMode.RECORD:
        if tape is None:
            raise ProviderError("record 模式必须给出录制带路径")
        return RecordingProvider(provider, tape)
    return provider
