"""按配置组装一个 Agent 的大脑。

只有一处地方决定「这个 Agent 是 echo 还是 LLM」：配置里有没有 provider。
CLI 与测试都走这一个入口，避免两条装配路径长期漂移。
"""

from __future__ import annotations

from pathlib import Path

from anthill.agent.context import ContextBuilder
from anthill.agent.handlers import EchoHandler, MessageHandler
from anthill.agent.llm_handler import LlmHandler
from anthill.agent.tools.base import Confirmer
from anthill.agent.tools.registry import build_toolset
from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.providers.registry import TapeMode, provider_for_agent
from anthill.security.policy import PolicyEngine


def build_handler(
    *,
    layout: NodeLayout,
    config: Config,
    agent_name: str,
    mode: TapeMode = TapeMode.LIVE,
    tape: Path | None = None,
    confirm: Confirmer | None = None,
) -> MessageHandler:
    agent = config.agent(agent_name)
    provider = provider_for_agent(config, agent_name, mode=mode, tape=tape)
    if provider is None:
        return EchoHandler()

    section = config.provider_for(agent_name)
    tools = build_toolset(agent.tools)
    builder = ContextBuilder(
        agent=agent,
        node=config.node.name,
        tools=tools,
        context_window=section.context_window if section else 128_000,
    )
    return LlmHandler(
        provider=provider,
        tools=tools,
        policy=PolicyEngine(config.security),
        builder=builder,
        max_steps=agent.max_steps,
        token_budget=agent.token_budget,
        trusted_peers=frozenset(config.peers),
        confirm=confirm,
    )
