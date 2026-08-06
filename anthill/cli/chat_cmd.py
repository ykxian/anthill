"""`anthill chat`（人跟 Agent 多轮聊）与 `anthill talk`（让两个 Agent 就一件事聊）。

`send --type chat` 是单发的：发一句、收一句就结束了。但协作里真正常见的需求是
**接着上一句往下聊** —— 所以这两个命令都把整段对话挂在同一个 thread 上，
Agent 那边的 thread 记忆自然就接上了。

`talk` 靠的是协议里本来就有的 `ChatPayload.mentions`：
带 @ 的对话，回信发给被 @ 的那个人，球就在两个 Agent 之间来回
（规则在 agent/conversation.py，终止靠每个 Agent 的 `chat_turns` 预算）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import typer
from rich.prompt import Prompt
from rich.table import Table

from anthill.adapters.bridge import BridgeHandler, parse_note
from anthill.adapters.bridge_session import pick_agent, wait_for_message
from anthill.agent.sender import Sender
from anthill.cli.common import console, fail, load
from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import new_thread_id, now
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType
from anthill.core.router import Router, parse_address
from anthill.core.states import DeliveryTracker
from anthill.transport.registry import TransportRegistry

DEFAULT_CLI_AGENT = "cli"
POLL_INTERVAL = 0.3
DEFAULT_WAIT = 120.0
DEFAULT_WATCH = 300.0


def _sender(layout: NodeLayout, config: Config, name: str) -> tuple[Sender, Mailbox, EventLog]:
    mailbox = Mailbox(layout.mailbox_dir(name)).ensure()
    log = EventLog(layout.log_file(name), agent=name, echo=False)
    sender = Sender(
        identity=Address(node=config.node.name, agent=name),
        mailbox=mailbox,
        router=Router(config, layout),
        transports=TransportRegistry(config, layout),
        tracker=DeliveryTracker(),
        log=log,
    )
    return sender, mailbox, log


# ---------- chat：人 ↔ Agent ----------


def chat_command(
    agent: str = typer.Argument(..., help="要聊的 Agent（name / role:xxx / node:agent）"),
    first: str = typer.Argument("", help="第一句话；留空则进入交互输入"),
    wait: float = typer.Option(DEFAULT_WAIT, "--wait", help="每轮等回复的秒数"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """跟一个 Agent 多轮对话。整段挂在同一个 thread 上，对方记得前面说过什么。

    Ctrl-C 或空行退出。
    """
    layout, config = load(workspace)
    try:
        asyncio.run(_chat(layout, config, agent, first, wait))
    except AntHillError as exc:
        fail(str(exc))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]结束对话[/dim]")


async def _chat(layout: NodeLayout, config: Config, agent: str, first: str, wait: float) -> None:
    sender, mailbox, log = _sender(layout, config, DEFAULT_CLI_AGENT)
    thread = new_thread_id()
    recipient = parse_address(agent, default_node=config.node.name)
    console.print(f"[dim]与 {recipient} 的对话 · thread={thread[-6:]} · 空行退出[/dim]")

    try:
        line = first
        while True:
            if not line:
                line = Prompt.ask("[bold cyan]你[/bold cyan]").strip()
            if not line:
                return
            await sender.send_new(
                to=recipient,
                type=MessageType.CHAT,
                payload=ChatPayload(body=line),
                thread=thread,
            )
            await _await_reply(mailbox, thread=thread, timeout=wait)
            line = ""
    finally:
        log.close()


async def _await_reply(mailbox: Mailbox, *, thread: str, timeout: float) -> bool:
    """在自己的收件箱上等一句回话。回执不算回话，继续等。"""
    deadline = now() + timedelta(seconds=timeout)
    while now() < deadline:
        for path in mailbox.list_new():
            try:
                env = Mailbox.read_envelope(path)
            except AntHillError:
                continue
            if env.thread != thread:
                continue
            mailbox.archive(mailbox.claim(path))
            if env.type is MessageType.CHAT:
                console.print(f"[bold]{env.from_.agent}[/bold] {env.payload.body}")  # type: ignore[union-attr]
                return True
            if env.type is MessageType.TASK_ERROR:
                console.print(f"[red]{env.from_.agent} 出错[/red] {env.payload.error}")  # type: ignore[union-attr]
                return True
        await asyncio.sleep(POLL_INTERVAL)
    console.print("[yellow]![/yellow] 等回复超时；对方可能还在想，或者没启动")
    return False


# ---------- talk：Agent ↔ Agent ----------


def talk_command(
    first: str = typer.Argument(..., help="参与讨论的 Agent A"),
    second: str = typer.Argument(..., help="参与讨论的 Agent B"),
    topic: str = typer.Argument(..., help="讨论的话题"),
    watch: float = typer.Option(DEFAULT_WATCH, "--watch", help="旁观秒数"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """让两个 Agent 就一件事聊下去，你在旁边看。

    对话终止靠各自的 `chat_turns` 预算（node.toml 里配，默认 6 轮），
    不靠模型自觉说「我说完了」—— 也不靠 hops 熔断，那是协议层的兜底。
    """
    layout, config = load(workspace)
    for name in (first, second):
        if ":" not in name and name not in config.agents:
            fail(f"未知 Agent {name!r}；已配置：{', '.join(sorted(config.agents)) or '无'}")
    try:
        asyncio.run(_talk(layout, config, first, second, topic, watch))
    except AntHillError as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        console.print("\n[dim]停止旁观；他们可能还在聊，用 `anthill log` 继续看[/dim]")


async def _talk(
    layout: NodeLayout,
    config: Config,
    first: str,
    second: str,
    topic: str,
    watch: float,
) -> None:
    sender, _, log = _sender(layout, config, DEFAULT_CLI_AGENT)
    thread = new_thread_id()
    try:
        # 第一句发给 A，并 @ 上 B —— A 的回信就会发给 B，而不是发回给我
        await sender.send_new(
            to=parse_address(first, default_node=config.node.name),
            type=MessageType.CHAT,
            payload=ChatPayload(body=topic, mentions=(second,)),
            thread=thread,
        )
    finally:
        log.close()

    console.print(
        f"[bold green]▶[/bold green] {first} ⇄ {second} [dim]thread={thread[-6:]}[/dim]\n"
        f"[dim]{topic}[/dim]\n"
    )
    await _watch_conversation(layout, config, thread=thread, timeout=watch)


async def _watch_conversation(
    layout: NodeLayout, config: Config, *, thread: str, timeout: float
) -> None:
    """旁观：扫各 Agent 邮箱里属于这个 thread 的 chat。

    只读不取 —— 这些信是他俩的，我们不能把它们从收件箱里拿走。
    """
    deadline = now() + timedelta(seconds=timeout)
    seen: set[str] = set()
    quiet_since = now()
    while now() < deadline:
        fresh = _scan(layout, config, thread=thread, seen=seen)
        for env in fresh:
            body = getattr(env.payload, "body", "")
            console.print(f"[bold cyan]{env.from_.agent}[/bold cyan] → {env.to.agent}  {body}")
        if fresh:
            quiet_since = now()
        elif now() - quiet_since > timedelta(seconds=15):
            console.print("[dim]安静了一会儿，对话应该结束了[/dim]")
            return
        await asyncio.sleep(POLL_INTERVAL)
    console.print("[dim]旁观时间到[/dim]")


def _scan(layout: NodeLayout, config: Config, *, thread: str, seen: set[str]) -> list[Envelope]:
    out: list[Envelope] = []
    for name in sorted(config.agents):
        mailbox = Mailbox(layout.mailbox_dir(name))
        if not mailbox.exists:
            continue
        paths = [*mailbox.list_new(), *sorted(mailbox.done.rglob("*.json"))]
        for path in paths:
            try:
                env = Mailbox.read_envelope(path)
            except AntHillError:
                continue
            if env.thread != thread or env.id in seen or env.type is not MessageType.CHAT:
                continue
            seen.add(env.id)
            out.append(env)
    return sorted(out, key=lambda e: e.id)


# ---------- bridge：看看有什么在等我 ----------


def bridge_command(
    agent: str = typer.Argument(
        "", help="桥接 Agent 名；留空 = 用 $ANTHILL_AGENT，再没有就自动认领一个没人占的"
    ),
    reply: str = typer.Option("", "--reply", help="回复哪一条（信封 id，可只给后 6 位）"),
    text: str = typer.Option("", "--text", help="回复正文；留空则只列出待办"),
    to: str = typer.Option("", "--to", help="主动发一条：收件人"),
    kind: str = typer.Option("chat", "--kind", help="主动发一条时：chat 或 task"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON，给 hook / 脚本用"),
    wait: float = typer.Option(
        0.0, "--wait", help="阻塞到有新消息为止（秒）—— 常驻会话循环跑这条就是「一直盯着」"
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """看看有什么消息在等你，或者直接写一条回复／主动发一条。

    桥接的常规用法是**直接编辑文件**（你的 Claude Code 会话就盯着那个目录）；
    这个命令只是懒得开编辑器时的捷径。
    """
    layout, config = load(workspace)
    # 不给名字就按环境变量 / 自动认领挑一个 —— 同一个目录下开几个会话时，
    # 配置文件是表达不出「谁对应谁」的，只有环境变量能穿透到单个会话
    try:
        picked = pick_agent(layout, config, agent)
    except AntHillError as exc:
        fail(str(exc))
    # 自动挑的时候一定要说一声挑到了谁 —— 不然人不知道这个终端对应哪个 Agent
    if not agent and not as_json:
        console.print(f"[dim]这个会话 = [b]{picked}[/b][/dim]")
    agent = picked
    if not config.agent(agent).bridge:
        fail(f"Agent {agent!r} 不是桥接 Agent；node.toml 里给它加 bridge = true")
    handler = BridgeHandler(root=layout.agent_dir(agent), agent_name=agent)

    if wait > 0 and not (to or reply):
        # 阻塞到有东西为止。**这就是「一直盯着」缺的那块** ——
        # MCP 与 hook 都是拉取式的，会话闲着时没有任何东西会把它叫醒。
        wait_for_message(handler.dir("inbox"), timeout=wait)

    if to:
        _draft(
            handler,
            f"cli-{new_thread_id()[-8:]}.md",
            f"---\nto: {to}\ntype: {kind}\n---\n\n{text}\n",
        )
        console.print(f"[bold green]✓[/bold green] 已写好草稿，发给 {to}（下一轮 tick 发出）")
        return
    if reply:
        pending = _pending(handler)
        match = [p for p in pending if p.stem.endswith(reply) or p.stem == reply]
        if len(match) != 1:
            fail(f"{reply!r} 匹配到 {len(match)} 条待回复；用 `anthill bridge {agent}` 看看列表")
        _draft(handler, match[0].name, text or "")
        console.print(f"[bold green]✓[/bold green] 已写好回复 {match[0].stem[-6:]}")
        return

    if as_json:
        console.print_json(data=_pending_json(handler, agent))
        return
    _list_pending(handler, agent)


def _pending(handler: BridgeHandler) -> list[Path]:
    return sorted(handler.dir("inbox").glob("*.md"))


def _pending_json(handler: BridgeHandler, agent: str) -> dict[str, Any]:
    """给 hook 用的形状。

    **这是「让常驻会话自动收发」最便宜的那条路**：一个每轮开始时跑
    `anthill bridge <名字> --json` 的 hook，就能把「人肉转述」这一步去掉 ——
    不需要等 MCP。MCP 让这次调用更规整（有 schema、不只限 Claude Code），
    但**真正解决「被动」的是 hook**，不是协议。
    """
    waiting = []
    for path in _pending(handler):
        headers, body = parse_note(path.read_text(encoding="utf-8"))
        waiting.append(
            {
                "id": path.stem,
                "short": path.stem[-6:],
                "from": headers.get("from", ""),
                "type": headers.get("type", "chat"),
                "thread": headers.get("thread", ""),
                "body": body.strip(),
            }
        )
    return {
        "agent": agent,
        "count": len(waiting),
        "waiting": waiting,
        "inbox": str(handler.dir("inbox")),
        "outbox": str(handler.dir("outbox")),
        "reply_hint": (
            f"回复：anthill bridge {agent} --reply <id> --text '…'，"
            f"或直接在 {handler.dir('outbox')} 下写同名 .md"
        ),
    }


def _draft(handler: BridgeHandler, name: str, text: str) -> None:
    if not text.strip():
        fail("正文不能为空；用 --text 写点什么")
    (handler.dir("outbox") / name).write_text(text, encoding="utf-8")


def _list_pending(handler: BridgeHandler, agent: str) -> None:
    pending = _pending(handler)
    if not pending:
        console.print(f"[dim]{agent} 这边没有在等你的消息[/dim]")
        console.print(f"[dim]目录：{handler.dir('inbox')}[/dim]")
        return

    table = Table(title=f"{agent} 在等你回复", header_style="bold yellow")
    for column in ("id", "来自", "类型", "内容"):
        table.add_column(column)
    for path in pending:
        headers, body = parse_note(path.read_text(encoding="utf-8"))
        table.add_row(
            path.stem[-6:],
            headers.get("from", "-"),
            headers.get("type", "-"),
            " ".join(body.split())[:56],
        )
    console.print(table)
    console.print(f"[dim]直接编辑 {handler.dir('outbox')} 下的同名文件即可回复[/dim]")
