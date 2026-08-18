"""`anthill doctor` —— 一次把「为什么跑不起来」全查完。

`check_runtime()` 早就能一次性查 provider key、邮箱可写这些东西，
但它**只在 `agent start` 时触发** —— 也就是说你得先把 agentd 启起来、
让它失败一次，才知道哪儿没配好。而这个项目最常见的两种卡壳
（coordinator 没大脑、provider 没设 key）恰恰都是配置问题。

这条命令把那些检查提到前面，并且**一次报全部**，不是报第一个就停 ——
一个个试出来太慢了。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import typer

from anthill.cli.common import console, load
from anthill.core.config import Config, brain_of
from anthill.core.mailbox import Mailbox
from anthill.core.outbox import Outbox
from anthill.core.paths import NodeLayout
from anthill.security import secrets
from anthill.web.agents import running_pid

OK = "[green]✓[/green]"
WARN = "[yellow]![/yellow]"
BAD = "[red]✗[/red]"


@dataclass(frozen=True, slots=True)
class Finding:
    level: str
    text: str
    fix: str = ""

    @property
    def is_bad(self) -> bool:
        return self.level == BAD


def doctor_command(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """体检：配置、密钥、邮箱、在跑的进程 —— 一次看完。"""
    layout, config = load(workspace)
    secrets.load_into_env()  # 面板上存的密钥也算数
    findings = [
        *_check_agents(layout, config),
        *_check_providers(config),
        *_check_mailboxes(layout, config),
        *_check_backlog(layout, config),
        *_check_freshness(layout, config),
        *_check_security_posture(config),
    ]

    console.print(f"[bold]{config.node.name}[/bold] [dim]{layout.workspace}[/dim]\n")
    for finding in findings:
        console.print(f"{finding.level} {finding.text}")
        if finding.fix:
            console.print(f"   [dim]{finding.fix}[/dim]")
    if not any(f.is_bad for f in findings):
        console.print("\n[green]没有阻断性问题。[/green]")
        return
    console.print("\n[red]有阻断性问题 —— 上面标 ✗ 的那些会让对应功能直接用不了。[/red]")
    raise typer.Exit(code=1)


def _check_security_posture(config: Config) -> list[Finding]:
    """无人值守放宽是这台机器的安全姿态事实 —— 巡检必须一眼看到谁开了口子。"""
    allow = config.security.unattended_allow
    if not allow:
        return []
    return [
        Finding(
            WARN,
            f"无人值守放宽已开启：{'、'.join(allow)} 风险免确认（unattended_allow）",
            fix="确认这是有意的；收回：删掉 node.toml [security] 里的 unattended_allow",
        )
    ]


def _check_agents(layout: NodeLayout, config: Config) -> list[Finding]:
    out: list[Finding] = []
    coordinators = [a for a in config.agents.values() if a.role == "coordinator"]
    if not coordinators:
        out.append(
            Finding(
                WARN,
                '没有 role = "coordinator" 的 Agent —— `anthill run` 用不了',
                "在面板的「加一个 Agent」里把角色选成 coordinator，"
                '或在 node.toml 里写 role = "coordinator"',
            )
        )
    usable = [a for a in coordinators if brain_of(a) != "echo"]
    for agent in coordinators:
        if brain_of(agent) != "echo":
            out.append(Finding(OK, f"coordinator「{agent.name}」大脑是 {brain_of(agent)}"))
            continue
        # 这就是那个「假装成功」的根源：没大脑的 coordinator 只会复读，
        # 而 run 以前照样打印「完成（ok）」、退出码 0。
        # 但只要还有**别的**能用的 coordinator，`run` 会挑那个 —— 那就只是提醒，不是阻断。
        out.append(
            Finding(
                WARN if usable else BAD,
                f"coordinator「{agent.name}」没有大脑（没配 provider / command）"
                " —— 它只会把你的话原样回显" + ("；anthill run 会绕开它" if usable else ""),
                "给它配一个 provider，或者从 node.toml 里删掉它",
            )
        )

    running = [name for name in sorted(config.agents) if running_pid(layout, name)]
    out.append(
        Finding(OK, f"在跑的 agentd：{', '.join(running)}")
        if running
        else Finding(
            WARN,
            "一个 agentd 都没在跑 —— 发消息会一直等到超时",
            "启动：anthill agent start <名字>；看全机器：anthill agent ps",
        )
    )
    return out


def _check_providers(config: Config) -> list[Finding]:
    out: list[Finding] = []
    used = {a.provider for a in config.agents.values() if a.provider}
    for name in sorted(used):
        section = config.providers.get(name)
        if section is None:
            out.append(Finding(BAD, f"有 Agent 用了 provider「{name}」，但 node.toml 里没有它"))
            continue
        if os.environ.get(section.api_key_env):
            out.append(Finding(OK, f"provider「{name}」的 {section.api_key_env} 已就绪"))
        else:
            out.append(
                Finding(
                    BAD,
                    f"provider「{name}」需要 {section.api_key_env}，但它没有值",
                    f"export {section.api_key_env}=... ，"
                    "或者在面板的「模型」页上存一个（落 ~/.anthill/secrets.env，0600）",
                )
            )
    return out


def _check_mailboxes(layout: NodeLayout, config: Config) -> list[Finding]:
    """配置里有它，就该能收它的信 —— 邮箱不该等到 agentd 第一次启动才建。"""
    missing = [n for n in sorted(config.agents) if not Mailbox(layout.mailbox_dir(n)).exists]
    if missing:
        return [
            Finding(
                BAD,
                f"这些 Agent 还没有邮箱：{', '.join(missing)} —— 跨机投递会被拒收",
                "在面板上加/改一次 Agent 会自动补建，或直接 anthill agent start 一次",
            )
        ]
    return [Finding(OK, f"{len(config.agents)} 个 Agent 的邮箱都在")]


def _check_backlog(layout: NodeLayout, config: Config) -> list[Finding]:
    out: list[Finding] = []
    for name in sorted(config.agents):
        box = Mailbox(layout.mailbox_dir(name))
        if not box.exists:
            continue
        backlog = len(box.list_new())
        dead = len(Outbox(box).dead_letters())
        if backlog > 20:
            out.append(
                Finding(
                    WARN,
                    f"{name} 积压了 {backlog} 条没消费的消息",
                    f"它的 agentd 在跑吗？anthill agent start {name}",
                )
            )
        if dead:
            out.append(
                Finding(
                    WARN,
                    f"{name} 有 {dead} 条死信",
                    f"看看：anthill dead list {name}；修好之后 anthill dead retry {name} --all",
                )
            )
    return out


def _check_freshness(layout: NodeLayout, config: Config) -> list[Finding]:
    """磁盘新代码、进程旧版本 —— 「明明修了怎么还坏」的头号来源。

    agentd 的启动时刻在它自己写的 runtime.json 里；比一比包里最新的
    .py 改动时刻就知道谁在带病工作。serve 的对应提示在面板顶栏。
    """
    import json

    from anthill.core.freshness import stale_since
    from anthill.web.agents import running_pid, runtime_path

    stale: list[str] = []
    for name in sorted(config.agents):
        if running_pid(layout, name) is None:
            continue
        try:
            data = json.loads(runtime_path(layout, name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if stale_since(str(data.get("started_at", ""))):
            stale.append(name)
    if not stale:
        return [Finding(OK, "在跑的 agentd 都是最新代码")]
    return [
        Finding(
            WARN,
            f"这些 agentd 启动之后代码更新过，跑的是旧版：{', '.join(stale)}",
            "重启它们：面板上停→启，或 anthill agent stop/start <名字>",
        )
    ]
