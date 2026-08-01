"""node.toml 解析与 fail-fast 校验（03-tech-design §8）。

铁律：**配置文件里永远只存环境变量名，不存密钥本身**。
启动期把能查的都查掉（provider 是否存在、env 是否设置、邮箱是否可写），
查不过就拒绝启动并给出可执行的修复提示。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anthill.core.envelope import AGENT_NAME_RE, NODE_NAME_RE, TransportKind
from anthill.core.errors import ConfigError
from anthill.core.paths import NodeLayout

DEFAULT_MULTICAST_GROUP = "239.77.77.7"
DEFAULT_DISCOVERY_PORT = 45777
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TASK_TIMEOUT = 600.0


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeSection(_Section):
    name: str
    workspace: str = "."

    @model_validator(mode="after")
    def _check_name(self) -> Self:
        if not NODE_NAME_RE.match(self.name):
            raise ValueError(f"非法节点名 {self.name!r}")
        return self


class DiscoverySection(_Section):
    """默认全关 —— 「不开启时节点完全静默」是核心需求（01-architecture §5.3）。"""

    enabled: bool = False
    multicast_group: str = DEFAULT_MULTICAST_GROUP
    port: int = Field(default=DEFAULT_DISCOVERY_PORT, ge=1, le=65535)


class PeerSection(_Section):
    """显式配置的对端节点。LAN/SSH 阶段才会真正用到。"""

    transport: TransportKind
    host: str | None = None
    user: str | None = None
    remote_workspace: str | None = None
    endpoint: str | None = None
    key_env: str | None = None

    @model_validator(mode="after")
    def _check_transport_fields(self) -> Self:
        if self.transport is TransportKind.SSH and not (self.host and self.remote_workspace):
            raise ValueError("ssh peer 必须配置 host 与 remote_workspace")
        if self.transport is TransportKind.LAN and not self.endpoint:
            raise ValueError("lan peer 必须配置 endpoint")
        return self


class ProviderSection(_Section):
    kind: Literal["openai_compat", "anthropic"]
    api_key_env: str
    model: str
    base_url: str | None = None


class AgentSection(_Section):
    """`provider` 留空表示 echo agent（M1 用：只回显，不调 LLM）。"""

    name: str = ""
    role: str = "worker"
    provider: str | None = None
    persona: str = ""
    tools: tuple[str, ...] = ()


class RuntimeSection(_Section):
    poll_interval: float = Field(default=DEFAULT_POLL_INTERVAL, gt=0)
    task_timeout: float = Field(default=DEFAULT_TASK_TIMEOUT, gt=0)
    watch_mode: Literal["auto", "inotify", "poll"] = "auto"


class Config(_Section):
    node: NodeSection
    discovery: DiscoverySection = DiscoverySection()
    runtime: RuntimeSection = RuntimeSection()
    peers: dict[str, PeerSection] = Field(default_factory=dict)
    providers: dict[str, ProviderSection] = Field(default_factory=dict)
    agents: dict[str, AgentSection] = Field(default_factory=dict)
    source: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def _inject_agent_names(cls, data: Any) -> Any:
        """toml 的 `[agents.coder]` 里没有 name 字段，把表键补进去。"""
        if not isinstance(data, dict):
            return data
        agents = data.get("agents")
        if not isinstance(agents, dict):
            return data
        patched = {
            key: ({**value, "name": key} if isinstance(value, dict) else value)
            for key, value in agents.items()
        }
        return {**data, "agents": patched}

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        for name, agent in self.agents.items():
            if not AGENT_NAME_RE.match(name):
                raise ValueError(f"非法 Agent 名 {name!r}：只允许小写字母开头的 name")
            if agent.provider and agent.provider not in self.providers:
                known = ", ".join(sorted(self.providers)) or "（未配置任何 provider）"
                raise ValueError(
                    f"Agent {name} 引用了不存在的 provider {agent.provider!r}；已有：{known}"
                )
        return self

    # ---------- 加载 ----------

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.is_file():
            raise ConfigError(f"配置文件不存在：{path}；先跑一次 `anthill init`")
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"解析 {path} 失败：{exc}") from exc
        try:
            return cls.model_validate({**raw, "source": path})
        except ValueError as exc:
            raise ConfigError(f"配置 {path} 非法：{exc}") from exc

    @classmethod
    def load_from(cls, layout: NodeLayout) -> Config:
        return cls.load(layout.node_toml)

    # ---------- 查询 ----------

    def agent(self, name: str) -> AgentSection:
        try:
            return self.agents[name]
        except KeyError:
            known = ", ".join(sorted(self.agents)) or "（node.toml 里没有配置任何 agent）"
            raise ConfigError(f"未知 Agent {name!r}；已配置：{known}") from None

    def agents_with_role(self, role: str) -> list[AgentSection]:
        return [a for a in self.agents.values() if a.role == role]

    def provider_for(self, agent_name: str) -> ProviderSection | None:
        provider = self.agent(agent_name).provider
        return self.providers[provider] if provider else None


def check_runtime(config: Config, layout: NodeLayout, agent_name: str) -> None:
    """agentd 启动前的 fail-fast 体检。任一项不过直接拒绝启动。"""
    agent = config.agent(agent_name)
    problems: list[str] = []

    if agent.provider:
        provider = config.providers[agent.provider]
        if not os.environ.get(provider.api_key_env):
            problems.append(
                f"环境变量 {provider.api_key_env} 未设置"
                f"（provider {agent.provider} 需要它）→ export {provider.api_key_env}=..."
            )

    mailbox = layout.mailbox_dir(agent_name)
    try:
        mailbox.mkdir(parents=True, exist_ok=True)
        probe = mailbox / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(f"邮箱目录不可写：{mailbox}（{exc}）")

    if problems:
        raise ConfigError("启动前检查未通过：\n  - " + "\n  - ".join(problems))


def default_node_toml(node_name: str) -> str:
    """`anthill init` 生成的模板。注释即文档，让人一眼知道能改什么。"""
    return f"""# AntHill 节点配置
[node]
name = "{node_name}"
workspace = "."

[discovery]
enabled = false            # 默认不广播：不发包、不监听，同网段其他 Agent 与你互不可见
multicast_group = "239.77.77.7"
port = 45777

[runtime]
poll_interval = 2.0        # watcher 降级为轮询时的扫描间隔（秒）
task_timeout = 600.0
watch_mode = "auto"        # auto | inotify | poll（NFS 上会自动降级为 poll）

# ---- 模型 provider：只写环境变量名，不写密钥本身 ----
# [providers.deepseek]
# kind = "openai_compat"
# base_url = "https://api.deepseek.com"
# api_key_env = "DEEPSEEK_API_KEY"
# model = "deepseek-chat"

# ---- Agent：provider 留空 = echo agent（不调 LLM，只回显，用于跑通链路）----
[agents.cli]
role = "user"              # `anthill send` 的收件箱：回执与 result 会回到这里

[agents.coordinator]
role = "coordinator"

[agents.echo]
role = "worker"

# ---- 跨机 peer（M5 SSH 阶段启用）----
# [peers.lab-server]
# transport = "ssh"
# host = "10.0.8.21"
# user = "yekaixian"
# remote_workspace = "~/work/proj"
"""
