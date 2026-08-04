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
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.2
DEFAULT_LLM_TIMEOUT = 120.0
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_STEPS = 20
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_SHELL_TIMEOUT = 120.0
DEFAULT_CLI_TIMEOUT = 900.0
DEFAULT_CHAT_TURNS = 6


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeSection(_Section):
    name: str
    workspace: str = "."
    endpoint: str = ""
    """本机对外地址（如 http://10.0.8.9:45778）。投递时随请求带给对方，让对方知道回信往哪发。"""

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
    port: int = Field(default=22, ge=1, le=65535)
    identity_file: str | None = None
    """SSH 私钥路径。留空则用 ssh-agent 与 ~/.ssh 下的默认密钥。"""

    known_hosts: str | None = None
    """known_hosts 路径。留空用 ~/.ssh/known_hosts —— 主机指纹校验绝不会被跳过。"""

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
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, gt=0)
    temperature: float = Field(default=DEFAULT_TEMPERATURE, ge=0, le=2)
    timeout: float = Field(default=DEFAULT_LLM_TIMEOUT, gt=0)
    context_window: int = Field(default=DEFAULT_CONTEXT_WINDOW, gt=0)
    """模型上下文窗口，context.py 按它的 70% 做预算（03-tech-design §4）。"""


class AgentSection(_Section):
    """一个 Agent 的大脑由哪来，看两个字段：

    - `command` 非空 → **外来终端 Agent**（Claude Code / Codex / aider…），见 adapters/cli_agent.py
    - 否则 `provider` 非空 → 本项目自研的 ReAct 循环
    - 两个都没有 → echo agent（只回显，不调模型，用来跑通链路）
    """

    name: str = ""
    role: str = "worker"
    provider: str | None = None
    persona: str = ""
    tools: tuple[str, ...] = ()
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, gt=0)
    token_budget: int = Field(default=DEFAULT_TOKEN_BUDGET, gt=0)
    chat_turns: int = Field(default=DEFAULT_CHAT_TURNS, ge=0)
    """同一话题里最多接几轮对话。

    两个 Agent 互相回信如果没有别的刹车，只能等 hops 熔断 —— 那是协议层的兜底，
    不该拿来当对话的正常终止方式。这个预算按 thread 计，是**确定性**的：
    不依赖模型自觉说「我说完了」。0 表示不限（仍受 hops 约束）。
    """

    command: tuple[str, ...] = ()
    """外来终端 Agent 的启动命令，如 ["claude", "-p"]。"""

    command_cwd: str = ""
    """命令的工作目录，默认 workspace。"""

    command_timeout: float = Field(default=DEFAULT_CLI_TIMEOUT, gt=0)
    prompt_via: Literal["arg", "stdin"] = "arg"
    """prompt 怎么交给它：作为最后一个参数，还是从标准输入喂。"""

    @model_validator(mode="after")
    def _check_brain(self) -> Self:
        if self.command and self.provider:
            raise ValueError(
                f"Agent {self.name or '?'} 同时配了 command 与 provider；"
                "一个 Agent 只能有一个大脑，去掉其中一个"
            )
        return self


DEFAULT_SHELL_ALLOWLIST = (
    "pytest",
    "python -m pytest",
    "ruff",
    "mypy",
    "git status",
    "git diff",
    "git log",
)
"""白名单只放「验证类」命令。

刻意**不放** `cat`/`ls`/`head` 这类看似人畜无害的读命令：shell 的 cwd 虽然锁在
workspace，但参数可以写绝对路径，`cat /etc/passwd` 一样读得到。Agent 想读文件有
`read_file`/`list_dir` 工具，那两个是做过路径前缀校验的 —— 没必要为了省事在这里开个后门。
"""


class SecuritySection(_Section):
    """工具策略（03-tech-design §6）。默认保守：危险操作宁可停下来问人。"""

    shell_allowlist: tuple[str, ...] = DEFAULT_SHELL_ALLOWLIST
    shell_timeout: float = Field(default=DEFAULT_SHELL_TIMEOUT, gt=0)
    confirm_high_risk: bool = True
    """False 表示无人值守模式：high 风险直接拒绝而不是等人确认。"""

    max_output_bytes: int = Field(default=64_000, gt=0)


class RuntimeSection(_Section):
    poll_interval: float = Field(default=DEFAULT_POLL_INTERVAL, gt=0)
    task_timeout: float = Field(default=DEFAULT_TASK_TIMEOUT, gt=0)
    watch_mode: Literal["auto", "inotify", "poll"] = "auto"
    spool_unroutable: bool = False
    """路由不到的目标改为暂存，等对方 `anthill pull` 来取。

    服务器上开这个：SSH 是单向的，服务器连不回你的笔记本（NAT 后面、也没跑 sshd），
    结果只能等你来拉。默认关闭 —— 关闭时路由不到就是死信，行为跟以前一样。
    """


class Config(_Section):
    node: NodeSection
    discovery: DiscoverySection = DiscoverySection()
    runtime: RuntimeSection = RuntimeSection()
    security: SecuritySection = SecuritySection()
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


def check_runtime(
    config: Config, layout: NodeLayout, agent_name: str, *, require_provider_key: bool = True
) -> None:
    """agentd 启动前的 fail-fast 体检。任一项不过直接拒绝启动。

    `require_provider_key=False` 用于回放模式：那时根本不连上游，
    不该因为「没导出 API key」而拒绝启动。
    """
    agent = config.agent(agent_name)
    problems: list[str] = []

    if agent.provider and require_provider_key:
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
# endpoint = "http://10.0.8.9:45778"   # 本机对外地址，跨机通信时告诉对方回信往哪发

[discovery]
enabled = false            # 默认不广播：不发包、不监听，同网段其他 Agent 与你互不可见
multicast_group = "239.77.77.7"
port = 45777
# 开启后也只是「能互相看见」。发现 ≠ 可通信：
# 必须核对指纹并 `anthill peers trust --token <令牌>` 交换密钥后才能互投消息。

[runtime]
poll_interval = 2.0        # watcher 降级为轮询时的扫描间隔（秒）
task_timeout = 600.0
watch_mode = "auto"        # auto | inotify | poll（NFS 上会自动降级为 poll）

[security]
# 工具风险 × 来源信任 → 放行 / 要确认 / 拒绝。high 风险（如非白名单 shell 命令）
# 一律要人点头；agentd 不在终端里跑时「没人能确认」就等于拒绝。
confirm_high_risk = true
shell_timeout = 120.0
# shell_allowlist = ["pytest", "ruff", "git status"]   # 名单内的命令降为 medium

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

# ---- 有大脑的 Agent：配上 provider 就会走 ReAct 工具循环 ----
# [agents.coder]
# role = "worker"
# provider = "deepseek"
# persona = "你写最小可用的代码，改动前先读现状。"
# tools = ["read_file", "write_file", "list_dir", "run_shell", "send_message", "finish"]
# max_steps = 20           # 步数熔断
# token_budget = 200000    # 费用熔断：单个任务累计 token 上限
#
# [agents.reviewer]        # 最小权限示范：审查者只读，不能写、不能跑命令
# role = "reviewer"
# provider = "deepseek"
# tools = ["read_file", "list_dir", "send_message", "finish"]

# 给上面的 coordinator 配一个 provider，它就会拆解任务、按依赖派活、汇总结果：
#   anthill run "给 utils/date.py 补单测，并让 reviewer 过一遍"
# 编排用强模型、干活用便宜模型是常见配法。

# ---- 跨机 peer ----
# 局域网内的对端一般不用写在这里：`anthill peers invite/trust` 配好后
# 会记进 .anthill/peers.json（含共享密钥，权限 0600）。
# 只有需要固定地址时才显式配置：
# [peers.lab-server]
# transport = "lan"
# endpoint = "http://10.0.8.21:45778"
#
# [peers.lab-server]       # SSH peer：服务器上只要有 sshd，不用开任何新端口
# transport = "ssh"
# host = "10.0.8.21"
# user = "yekaixian"
# remote_workspace = "~/work/proj"
# identity_file = "~/.ssh/id_ed25519"   # 留空则用 ssh-agent 与默认密钥
#
# 服务器那一侧记得开 [runtime] spool_unroutable = true：
# SSH 是单向的，服务器连不回你的笔记本，回信要先暂存等你 `anthill pull`。
"""
