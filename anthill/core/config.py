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
DEFAULT_KEEP_DAYS = 7
DEFAULT_DEAD_KEEP_DAYS = 30
DEFAULT_LOG_MAX_MB = 32
DEFAULT_AUTO_PULL = 60.0
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
    """默认**可见**，但默认**不可通信**。

    最初这里是默认全关的（不发包、不监听、连 socket 都不创建）。
    实际用下来，「同网段的机器要先手动互相告知地址」这一步太劝退，
    而广播包里本来就只有公开信息：节点名、Agent 名单、地址。

    真正需要守住的那条线没有动 —— **发现 ≠ 可通信**：
    看见只是让对方出现在你的列表里（`discovered`），要互投消息仍然必须
    有人在两边各看一眼、核对指纹（`anthill peers pair`）。
    确实不想被看见就 `enabled = false`，那时依旧是零发包零监听。
    """

    enabled: bool = True
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

    price_in: float = Field(default=0.0, ge=0)
    price_out: float = Field(default=0.0, ge=0)
    """每百万 token 的价格（输入 / 输出），用于 `anthill cost` 折算。

    默认 0 = 不知道价格，那时只报 token 数，**不瞎猜钱**。
    单价写死在代码里迟早会过期，而一个过期的价格比没有价格更糟。
    """


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
    mcp: tuple[str, ...] = ()
    """这个 Agent 能用哪几台外部 MCP server 的工具（名字对应 `[mcp.<名字>]`）。

    留空 = 不接外部工具。**不默认全给** —— 最小权限比省事重要。
    """

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

    bridge: bool = False
    """文件夹桥接：让一个**常驻的交互式会话**（Claude Code、Cursor，或就是你本人）
    以这个 Agent 的身份参与协作。收到的消息写成 `bridge/inbox/*.md`，
    你把回复写进 `bridge/outbox/`。见 adapters/bridge.py。
    """

    @model_validator(mode="after")
    def _check_brain(self) -> Self:
        brains = [
            name
            for name, on in (
                ("command", bool(self.command)),
                ("provider", bool(self.provider)),
                ("bridge", self.bridge),
            )
            if on
        ]
        if len(brains) > 1:
            raise ValueError(
                f"Agent {self.name or '?'} 同时配了 {' 与 '.join(brains)}；"
                "一个 Agent 只能有一个大脑，去掉多余的"
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

    remote_admin: bool = False
    """允许**已信任的对端**在它的总控面板上直接改本机的 node.toml。

    默认关，而且这是个大开关，不是小配置：**能改 node.toml ≈ 能在本机执行命令**
    （加一个带 run_shell 的 Agent 就行）。打开它等于把「信任一个对端」的含义
    从「它能给我投消息、我的 Agent 会审」升级成「它能接管这台机器」。

    打开之后就是直连，没有逐次审批 —— 想要逐次点头的话用 M5 那套
    `approvals/` 审批流，两者是并列的两条路。
    """


class TemplateSection(_Section):
    """一件常做的事，存下来复用。

    「每次都得重新用自然语言描述目标，跑得好的一次没法存下来」—— 存在这儿。
    `{arg}` 会被 `anthill run --template <名字> <参数>` 的参数替换掉。
    """

    goal: str = Field(min_length=1, max_length=2_000)
    describe: str = Field(default="", max_length=200)
    to: str = Field(default="", max_length=64)
    """派给哪个 coordinator；留空 = 自动找。"""


class NotifySection(_Section):
    """任务跑完之后告诉谁。

    默认全关 —— 一个会自己往外发 HTTP 的框架，得是用户明确要的。
    """

    webhook: str = Field(default="", max_length=500)
    """POST 一个 JSON 过去（任务号、目标、成败、摘要）。留空 = 不发。"""

    on_failure_only: bool = False
    timeout: float = Field(default=10.0, gt=0)


class ScheduleSection(_Section):
    """定时把一件事交给 coordinator。由 `serve` 驱动。"""

    every: float = Field(gt=0)
    """间隔秒数。没做 cron 表达式 —— 那是一门要解析要测的小语言，
    而「每隔多久」覆盖了绝大多数需要，写错的余地也小得多。"""

    task: str = Field(default="", max_length=2_000)
    template: str = Field(default="", max_length=64)
    to: str = Field(default="", max_length=64)
    enabled: bool = True


class McpSection(_Section):
    """一台外部 MCP server。只支持 stdio 启动方式。

    风险默认 **high**：外部工具能干什么我们不知道，策略引擎照常管着它
    （无人值守时 high 直接拒绝）。要用就显式降级，**由人做这个判断**。
    """

    command: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high"] = "high"
    timeout: float = Field(default=20.0, gt=0)


class RuntimeSection(_Section):
    poll_interval: float = Field(default=DEFAULT_POLL_INTERVAL, gt=0)
    task_timeout: float = Field(default=DEFAULT_TASK_TIMEOUT, gt=0)
    watch_mode: Literal["auto", "inotify", "poll"] = "auto"
    # 定期卫生（core/hygiene.py）：role=user 信箱的保留时长与记录类文件的保留天数
    mailbox_keep_hours: float = Field(default=24.0, gt=0)
    records_keep_days: float = Field(default=30.0, gt=0)
    spool_unroutable: bool = False
    """路由不到的目标改为暂存，等对方 `anthill pull` 来取。

    服务器上开这个：SSH 是单向的，服务器连不回你的笔记本（NAT 后面、也没跑 sshd），
    结果只能等你来拉。默认关闭 —— 关闭时路由不到就是死信，行为跟以前一样。
    """

    keep_days: int = Field(default=DEFAULT_KEEP_DAYS, ge=0)
    """归档（done/、outbox/sent/）与 seen.db 的保留天数。0 = 永不清理。

    「消息就是文件」的代价是什么都不会自己消失，而归档量是消息量的**两倍以上**
    （每条业务消息还额外产生一条回执信封）。跑长任务的节点会一直涨到磁盘满。
    """

    dead_keep_days: int = Field(default=DEFAULT_DEAD_KEEP_DAYS, ge=0)
    """死信单独一个更长的保留期。

    死信是「需要人处理」的东西，跟着归档一起清等于把问题藏起来 ——
    正经出路是 `anthill dead list` 看一眼，然后 retry 或 drop。
    """

    log_max_mb: int = Field(default=DEFAULT_LOG_MAX_MB, ge=0)
    """单个日志文件的上限，超了滚动成 `.1`（只留一代）。0 = 不滚动。"""

    auto_pull_seconds: float = Field(default=DEFAULT_AUTO_PULL, ge=0)
    """`serve` 每隔多久替你去 SSH 对端取一次回信。0 = 只能手动 `anthill pull`。

    SSH 是单向的，回信只能靠这边去拉。以前 `anthill pull` 是纯手工的一次性命令 ——
    **人不敲命令，对端的回信就永远不回来**，跨机协作的回程等于靠人肉驱动。
    默认 60 秒：够勤快（回信不会压太久），也不至于把 SSH 连接打得太频。
    """


class Config(_Section):
    node: NodeSection
    discovery: DiscoverySection = DiscoverySection()
    runtime: RuntimeSection = RuntimeSection()
    security: SecuritySection = SecuritySection()
    peers: dict[str, PeerSection] = Field(default_factory=dict)
    mcp: dict[str, McpSection] = Field(default_factory=dict)
    templates: dict[str, TemplateSection] = Field(default_factory=dict)
    schedules: dict[str, ScheduleSection] = Field(default_factory=dict)
    notify: NotifySection = NotifySection()
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
            unknown = [s for s in agent.mcp if s not in self.mcp]
            if unknown:
                known = ", ".join(sorted(self.mcp)) or "（未配置任何 MCP server）"
                raise ValueError(
                    f"Agent {name} 引用了不存在的 MCP server {', '.join(unknown)}；已有：{known}"
                )
        for name, schedule in self.schedules.items():
            if not schedule.task and not schedule.template:
                raise ValueError(f"定时任务 {name} 既没有 task 也没有 template")
            if schedule.template and schedule.template not in self.templates:
                known = ", ".join(sorted(self.templates)) or "（没有任何模板）"
                raise ValueError(
                    f"定时任务 {name} 引用了不存在的模板 {schedule.template!r}；已有：{known}"
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


def brain_of(agent: AgentSection) -> str:
    """这个 Agent 的「大脑」是什么，给人看的一个词。

    桥接 Agent 显示成 echo 会让人以为它不干活 —— 实际上它背后是一个人
    （或一个常驻会话）。「在跑什么」是排查时第一眼看的东西，不能误导。
    CLI 的 `agent list` 和面板共用这一份，免得再各写一遍、再各错一次。
    """
    if agent.bridge:
        return "bridge"
    if agent.command:
        return agent.command[0]
    return agent.provider or "echo"


def default_node_toml(node_name: str) -> str:
    """`anthill init` 生成的模板。注释即文档，让人一眼知道能改什么。"""
    return f"""# AntHill 节点配置
[node]
name = "{node_name}"
workspace = "."
# endpoint = "http://10.0.8.9:45778"   # 本机对外地址，跨机通信时告诉对方回信往哪发

[discovery]
enabled = true             # 同网段的 anthill 节点能互相看见（广播包里只有节点名/Agent 名单/地址）
multicast_group = "239.77.77.7"
port = 45777
# 看见 ≠ 能通信。要互投消息，仍然必须有人在两边各看一眼、核对指纹：
#   A 机 anthill peers pair          → 显示六位 PIN
#   B 机 anthill peers pair --to A --pin <PIN>
# 不想被看见就改成 false —— 那时不发包、不监听、连 socket 都不创建。

[runtime]
poll_interval = 2.0        # watcher 降级为轮询时的扫描间隔（秒）
task_timeout = 600.0
watch_mode = "auto"        # auto | inotify | poll（NFS 上会自动降级为 poll）

[security]
# 工具风险 × 来源信任 → 放行 / 要确认 / 拒绝。high 风险（如非白名单 shell 命令）
# 一律要人点头；agentd 不在终端里跑时「没人能确认」就等于拒绝。
confirm_high_risk = true
shell_timeout = 120.0
# remote_admin = true      # 允许已信任的对端在它的面板上直接改本机 node.toml。
#                          # 想清楚再开：能改 node.toml ≈ 能在本机执行命令
#                          #（加一个带 run_shell 的 Agent 就行）。
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

# 拆解任务、派活、汇总的那个。**它现在还没有大脑** ——
# 没配 provider 就只是个复读机，`anthill run` 会直接拦下来告诉你。
# 想跑多 Agent 编排：把下面 [providers.*] 那段注释打开，然后给它加一行
#   provider = "deepseek"
[agents.coordinator]
role = "coordinator"

[agents.echo]
role = "worker"

# ---- 有大脑的 Agent：配上 provider 就会走 ReAct 工具循环 ----
# [agents.coder]
# role = "worker"
# provider = "deepseek"
# persona = "你写最小可用的代码，改动前先读现状。"
# tools = ["read_file", "write_file", "edit_file", "list_dir", "search_text", "find_files",
#           "run_shell", "send_message", "finish"]
# max_steps = 20           # 步数熔断
# token_budget = 200000    # 费用熔断：单个任务累计 token 上限
#
# [agents.reviewer]        # 最小权限示范：审查者只读，不能写、不能跑命令
# role = "reviewer"
# provider = "deepseek"
# tools = ["read_file", "list_dir", "search_text", "find_files", "send_message", "finish"]
#
# ---- 把已有的终端 Agent 接进来（Claude Code / Codex / aider…）----
# 有 command 就走适配器，不需要 provider；它自己的权限体系我们不代管。
# [agents.cc]
# role = "worker"
# command = ["claude", "-p"]
# command_timeout = 900.0
# chat_turns = 6         # 同一话题最多接几轮，防止两个 Agent 聊不完
#
# ---- 让一个常驻的交互式会话参与（你一直开着的 Claude Code，或就是你本人）----
# 收到的消息写成 .anthill/agents/<name>/bridge/inbox/*.md，回复写进 ../outbox/。
# 收消息不阻塞，人可以慢慢想；outbox 里放带 `to:` 的文件就是主动发起一条消息。
# [agents.cc]
# role = "worker"
# bridge = true

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
