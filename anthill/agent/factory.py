"""按配置组装一个 Agent 的大脑。

只有一处地方决定「这个 Agent 是 echo、worker 还是 coordinator」：
role + 有没有 provider。CLI 与测试都走这一个入口，避免两条装配路径长期漂移。
"""

from __future__ import annotations

from pathlib import Path

from anthill.adapters.bridge import BridgeHandler
from anthill.adapters.cli_agent import CliAgentHandler, CliSpec
from anthill.agent.context import ContextBuilder
from anthill.agent.handlers import EchoHandler, MessageHandler
from anthill.agent.llm_handler import LlmHandler
from anthill.agent.tools.base import Confirmer
from anthill.agent.tools.registry import build_toolset
from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.core.payloads import RiskLevel
from anthill.orchestrator.board import Blackboard
from anthill.orchestrator.coordinator import CoordinatorHandler, CoordinatorSettings
from anthill.providers.registry import TapeMode, provider_for_agent
from anthill.security.policy import PolicyEngine

COORDINATOR_ROLE = "coordinator"
DEFAULT_CONTEXT_WINDOW = 128_000


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
    if agent.bridge:
        # 人（或常驻的交互式会话）在回路里：收到的消息写成文件，回复从 outbox 捡
        return BridgeHandler(
            root=layout.agent_dir(agent_name),
            agent_name=agent_name,
            chat_turns=agent.chat_turns,
        )

    if agent.command:
        # 外来终端 Agent（Claude Code / Codex / aider…）：对 runtime 只是又一个 handler
        return CliAgentHandler(
            spec=CliSpec(
                command=agent.command,
                cwd=Path(agent.command_cwd) if agent.command_cwd else layout.workspace,
                timeout=agent.command_timeout,
                prompt_via=agent.prompt_via,
            ),
            agent_name=agent_name,
            role=agent.role,
            persona=agent.persona,
            chat_turns=agent.chat_turns,
        )

    provider = provider_for_agent(config, agent_name, mode=mode, tape=tape)
    if provider is None:
        return EchoHandler()

    blackboard = Blackboard(layout.blackboard)
    if agent.role == COORDINATOR_ROLE:
        return CoordinatorHandler(
            provider=provider,
            blackboard=blackboard,
            settings=CoordinatorSettings(step_timeout=config.runtime.task_timeout),
        )

    section = config.provider_for(agent_name)
    tools = build_toolset(agent.tools)
    # 只给它声明过的那几台 —— 不默认全给，最小权限比省事重要。
    # 「声明了不存在的 server」在 Config 校验时就拦掉了（那是配置有效性问题，
    # 放这儿会漏掉不走这条分支的 Agent，比如 command 适配器）。
    servers = {name: config.mcp[name] for name in agent.mcp}
    builder = ContextBuilder(
        agent=agent,
        node=config.node.name,
        tools=tools,
        context_window=section.context_window if section else DEFAULT_CONTEXT_WINDOW,
        board_summary=blackboard.summary,
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
        chat_turns=agent.chat_turns,
        mcp_servers=servers,
        max_risk=RiskLevel(agent.max_tool_risk),
    )
