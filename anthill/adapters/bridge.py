"""文件夹桥接：让一个**常驻的交互式会话**（Claude Code、Cursor，或者就是你本人）
以 Agent 的身份参与协作。

这就是本项目起点那个土办法的正式版本 —— 只不过现在它有信封、有回执、有 thread、
有跨机传输。目录长这样：

    bridge/
    ├── inbox/<信封id>.md    ← AntHill 写：收到的消息，你或你的 Claude Code 读
    ├── outbox/<信封id>.md   ← 你写：回复；AntHill 读走、发出、归档
    ├── pending/<信封id>.json  原始信封（内部用，用来构造回信时保住 thread 与 hops）
    ├── prepared/<草稿名>.json 已构造的回信（失败重试时复用同一个消息 ID）
    └── done/                已处理归档

和 cli_agent 适配器的**根本区别是它不阻塞**：

- `handle()` 只把消息写成文件就返回 —— 人可以想十分钟，期间 Agent 照常收新消息，
  几条消息会一起躺在 inbox/ 里等你；
- 回复由 runtime 的定时 tick 扫 outbox/ 发出去。

由此白捡一个能力：**outbox 里放一个带 `to:` 的文件，就是你主动发起的一条消息**，
不必是对谁的回复。人因此可以随时插进正在进行的对话里说一句。

给你的 Claude Code 会话的提示词大致是：

    盯着 .anthill/agents/cc/bridge/inbox/，出现新 .md 就读，
    把回复写进 ../outbox/ 下同名的 .md 文件。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthill.agent.conversation import chat_payload, message_expects_reply, plan_reply
from anthill.agent.handlers import HandlerContext
from anthill.agent.memory import ThreadMemory
from anthill.core.atomic import atomic_write
from anthill.core.chat_log import record_outgoing
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError, HopLimitExceeded
from anthill.core.ids import is_valid_id, now
from anthill.core.payloads import ChatPayload, MessageType, TaskResultPayload
from anthill.core.router import parse_address
from anthill.providers.base import Msg, Role

BRIDGE_DIR = "bridge"
INBOX, OUTBOX, PENDING, PREPARED, DONE = "inbox", "outbox", "pending", "prepared", "done"
STABLE_SECONDS = 1.0
MAX_BODY_CHARS = 30_000
PREPARED_VERSION = 1
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)

AWAITS_REPLY = frozenset({MessageType.TASK_REQUEST, MessageType.CHAT})
"""类型级兼容判定：旧 note 没有 needs_reply 字段时用它兜底。

新信封必须走 message_expects_reply：task.request 一定等回复，chat 用
reply_to + mentions 区分普通回答和 talk。等待回复的消息才建 pending
（回信要用原信封接住 thread/hops）。
"""

SURFACED = AWAITS_REPLY | frozenset({MessageType.TASK_RESULT, MessageType.TASK_ERROR})
"""**写进 inbox 给人看**的全部类型 —— 比「等你回」的多出两种。

### 为什么必须比 AWAITS_REPLY 大

以前这两个集合是同一个，于是 `task.result` / `task.error` 走到 handler 就被
`msg.ignored` 静默丢掉、直接归档。后果不是「少显示一条」，是**漏消息**：

桥接 Agent 只有一个醒来的入口 —— 值守会话盯着 `bridge/inbox/`。而 AntHill 有
两条信道：聊天走 bridge，`anthill send` 的任务结果走 mailbox。任务结果本该由
handler 从 mailbox 搬进 bridge/inbox，可它在这儿被扔了，于是**这个人永远不会
被唤醒**，日志里只留一行谁也不会去看的 `msg.ignored`。

实测过：投一条 task.result 给桥接 Agent，`msg.received` → `msg.ignored` →
归档进 `done/`，bridge/inbox 里什么都没多。

最容易中招的正是「桥接 Agent 自己派了活出去」——它用 outbox 里带 `to:` 的
文件发出一条 task，对方干完回 `task.result`，而**发起人看不见回音**。

### 为什么不干脆把回执也放进来

`receipt.*` 在 runtime 里就返回了（`env.type.is_receipt`），根本到不了
handler，那是对的：回执是状态机的燃料不是给人读的，一条业务消息配一条回执，
放进来会让收件箱里一半是噪音。`event` / `heartbeat` 同理。

**判据是「人需不需要知道这件事」**，不是「消息是不是发给我的」。
"""

REQUEST_TEMPLATE = """\
---
from: {frm}
to: {to}
type: {kind}
needs_reply: {needs_reply}
thread: {thread}
id: {msg_id}
---

{body}

<!-- {instruction} -->
"""


@dataclass(frozen=True, slots=True)
class BridgeNote:
    """outbox 里的一份草稿：要么是对某条消息的回复，要么是主动发起的新消息。"""

    path: Path
    body: str
    headers: dict[str, str]
    mtime_ns: int

    @property
    def reply_to(self) -> str:
        """文件名就是被回复消息的 id；主动发起的消息文件名随意。"""
        stem = self.path.stem
        return stem if is_valid_id(stem) else ""


class BridgeHandler:
    """人（或常驻会话）在回路里的 Agent。"""

    name = "bridge"

    def __init__(self, *, root: Path, agent_name: str, chat_turns: int = 0) -> None:
        self._root = root / BRIDGE_DIR
        self._agent = agent_name
        self._chat_turns = chat_turns
        # 启动就把目录建出来：人得先能告诉自己的 Claude Code「盯着这个目录」，
        # 而不是等第一条消息到了才发现目录还不存在
        for name in (INBOX, OUTBOX, PENDING, PREPARED, DONE):
            self.dir(name)

    @property
    def root(self) -> Path:
        return self._root

    def dir(self, name: str) -> Path:
        path = self._root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------- 收：写成文件就返回，绝不在这里等人 ----------

    async def handle(self, env: Envelope, ctx: HandlerContext) -> None:
        if env.type not in SURFACED:
            ctx.log.info("msg.ignored", msg=env.id, type=str(env.type))
            return

        self.dir(INBOX).joinpath(f"{env.id}.md").write_text(render_request(env), encoding="utf-8")
        # **只有「在等你回」的才进 pending。** pending 里放的是构造回信要用的
        # 原始信封；task.result / task.error 是**别人给你的答复**，不需要你回，
        # 给它建 pending 只会让 `--ack` 和「待回复」的计数把它算进去。
        if message_expects_reply(env):
            self.dir(PENDING).joinpath(f"{env.id}.json").write_bytes(env.to_json_bytes())
        ctx.log.info(
            "bridge.waiting",
            msg=env.id,
            thread=env.thread,
            frm=str(env.from_),
            type=str(env.type),
            file=f"{BRIDGE_DIR}/{INBOX}/{env.id}.md",
        )

    # ---------- 发：定时扫 outbox ----------

    async def tick(self, ctx: HandlerContext) -> None:
        for note in self.drafts():
            try:
                await self._deliver(note, ctx)
            except AntHillError as exc:
                ctx.log.error("bridge.send_failed", file=note.path.name, error=str(exc))
                self._archive(note.path, suffix=".failed")
                prepared = self._prepared_path(note)
                if prepared.is_file():
                    self._archive(prepared, suffix=".failed")

    def drafts(self) -> list[BridgeNote]:
        """读 outbox 里**写完了**的草稿。

        「写完了」用 mtime 至少 1 秒前来判断：编辑器与 Agent 都是边写边刷盘的，
        抢在中途读走会发出半句话。这比要求人写个结束标记更不容易出错。
        """
        out: list[BridgeNote] = []
        cutoff = now().timestamp() - STABLE_SECONDS
        for path in sorted(self.dir(OUTBOX).glob("*.md")):
            try:
                info = path.stat()
                if info.st_mtime > cutoff:
                    continue  # 还在写，下一轮再看
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            headers, body = parse_note(text)
            if not body.strip():
                continue  # 空草稿：人还没写正文
            out.append(
                BridgeNote(
                    path=path,
                    body=body.strip(),
                    headers=headers,
                    mtime_ns=info.st_mtime_ns,
                )
            )
        return out

    async def _deliver(self, note: BridgeNote, ctx: HandlerContext) -> None:
        source = self._pending(note.reply_to)
        if source is not None:
            await self._send_reply(source, note, ctx)
        elif prepared := self._load_prepared(note, ctx):
            # 上一轮可能已经 send + settle，只在归档草稿前崩了。原始 pending
            # 此时已经不在，但 prepared 信封仍是幂等真相；必须复用它，不能把
            # 同一份 note 当成「主动新消息」再造一个 ID。
            await self._resend_prepared(prepared, ctx)
        else:
            await self._send_new(note, ctx)
        self._archive(note.path)
        self._forget_prepared(note)

    def _pending(self, msg_id: str) -> Envelope | None:
        if not msg_id:
            return None
        path = self.dir(PENDING) / f"{msg_id}.json"
        if not path.is_file():
            return None
        try:
            return Envelope.from_json_bytes(path.read_bytes())
        except AntHillError:
            return None

    async def _send_reply(self, source: Envelope, note: BridgeNote, ctx: HandlerContext) -> None:
        reply = self._load_prepared(note, ctx)
        if reply is None:
            body = note.body[:MAX_BODY_CHARS]
            recipient = source.from_
            payload: ChatPayload | TaskResultPayload
            if source.type is MessageType.CHAT:
                history = self._history(ctx, source.thread)
                plan = plan_reply(
                    source, identity=ctx.identity, history=history, budget=self._chat_turns
                )
                if not plan.should_reply:
                    ctx.log.info("chat.ended", msg=source.id, reason=plan.reason)
                    self._settle(source.id)
                    return
                payload = chat_payload(body, plan)
                recipient = plan.recipient or source.from_
                kind = MessageType.CHAT
            else:
                payload = TaskResultPayload(
                    summary=body, artifacts=_artifacts(note.headers), status="ok"
                )
                kind = MessageType.TASK_RESULT

            try:
                reply = source.reply(
                    type=kind, payload=payload, sender=ctx.identity, recipient=recipient
                )
            except HopLimitExceeded as exc:
                ctx.log.warn("hop.limit", msg=source.id, error=str(exc))
                self._settle(source.id)
                return
            self._store_prepared(note, reply)
        elif reply.reply_to != source.id:
            raise AntHillError(
                f"{note.path.name} 的已准备信封回复 {reply.reply_to}，但当前原信是 {source.id}"
            )

        body = _outgoing_body(reply)
        recipient = reply.to
        await ctx.sender.send(reply)
        # 记进本机发件记录 —— 收件方在**另一台机器**时，对话页上没有任何
        # 别的地方能看到这半句（收件方的归档在对面）。本机投递的靠 id 去重。
        # 补记失败（OSError）只记日志：它排在 send 成功之后、归档之前，
        # 放它抛出去会逃过 tick 的 except AntHillError —— 草稿留在 outbox
        # 下一轮重发，磁盘抖一下变成对方收到重复消息。显示侧的记录
        # 没资格打断投递语义。
        try:
            record_outgoing(ctx.layout, reply, body)
        except OSError as exc:
            ctx.log.warn("chat.record_failed", msg=reply.id, error=str(exc))
        self._remember(ctx, source, reply)
        ctx.log.info("bridge.replied", msg=reply.id, to=str(recipient), thread=reply.thread)
        self._settle(source.id)

    async def _send_new(self, note: BridgeNote, ctx: HandlerContext) -> None:
        """outbox 里放一个带 `to:` 的文件 = 你主动发起一条消息。

        这就是「人手动中途插进对话」的那条路 —— 不必是对谁的回复。
        """
        target = note.headers.get("to", "").strip()
        if not target:
            raise AntHillError(
                f"{note.path.name} 既不是对某条消息的回复（文件名不是信封 id），"
                "又没写 `to:` —— 不知道该发给谁"
            )
        kind = note.headers.get("type", "chat").strip().lower()
        body = note.body[:MAX_BODY_CHARS]
        mentions = _mentions(note.headers)
        env = ctx.sender.prepare_new(
            to=parse_address(target, default_node=ctx.identity.node),
            type=MessageType.TASK_REQUEST if kind == "task" else MessageType.CHAT,
            payload=(
                _task_payload(body) if kind == "task" else ChatPayload(body=body, mentions=mentions)
            ),
            thread=note.headers.get("thread") or None,
        )
        self._store_prepared(note, env)
        await ctx.sender.send(env)
        try:
            record_outgoing(ctx.layout, env, body)
        except OSError as exc:  # 同上：显示侧失败不打断投递
            ctx.log.warn("chat.record_failed", msg=env.id, error=str(exc))
        ctx.log.info("bridge.sent", msg=env.id, to=target, kind=kind, thread=env.thread)

    async def _resend_prepared(self, env: Envelope, ctx: HandlerContext) -> None:
        """重跑 send 之后的失败路径；Envelope ID 和正文都以落盘版本为准。"""
        body = _outgoing_body(env)
        await ctx.sender.send(env)
        try:
            record_outgoing(ctx.layout, env, body)
        except OSError as exc:
            ctx.log.warn("chat.record_failed", msg=env.id, error=str(exc))
        ctx.log.info("bridge.resent", msg=env.id, to=str(env.to), thread=env.thread)

    def _prepared_path(self, note: BridgeNote) -> Path:
        return self.dir(PREPARED) / f"{note.path.name}.json"

    def _load_prepared(self, note: BridgeNote, ctx: HandlerContext) -> Envelope | None:
        path = self._prepared_path(note)
        if not path.is_file():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AntHillError(f"{path.name} 的已准备发送状态损坏：{exc}") from exc
        if not isinstance(state, dict) or state.get("version") != PREPARED_VERSION:
            raise AntHillError(f"{path.name} 不是兼容的已准备发送状态")
        expected = _draft_fingerprint(note)
        if state.get("fingerprint") != expected or state.get("source_id") != (
            note.reply_to or None
        ):
            raise AntHillError(
                f"{note.path.name} 自准备信封后已被修改；拒绝把旧消息发给新草稿的收件人"
            )
        raw_envelope = state.get("envelope")
        if not isinstance(raw_envelope, dict):
            raise AntHillError(f"{path.name} 缺少已准备的信封")
        env = Envelope.from_json_bytes(json.dumps(raw_envelope, ensure_ascii=False).encode("utf-8"))
        if env.from_ != ctx.identity:
            raise AntHillError(f"{path.name} 的发送者是 {env.from_}，不是当前 Agent {ctx.identity}")
        return env

    def _store_prepared(self, note: BridgeNote, env: Envelope) -> None:
        path = self._prepared_path(note)
        state = {
            "version": PREPARED_VERSION,
            "fingerprint": _draft_fingerprint(note),
            "source_id": note.reply_to or None,
            "envelope": env.model_dump(mode="json", by_alias=True),
        }
        atomic_write(
            path.parent,
            path.parent,
            path.name,
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def _forget_prepared(self, note: BridgeNote) -> None:
        self._prepared_path(note).unlink(missing_ok=True)

    # ---------- 归档 ----------

    def _settle(self, msg_id: str) -> None:
        for name, suffix in ((INBOX, ".md"), (PENDING, ".json")):
            path = self.dir(name) / f"{msg_id}{suffix}"
            if path.is_file():
                self._archive(path)

    def _archive(self, path: Path, *, suffix: str = "") -> None:
        target = self.dir(DONE) / f"{path.name}{suffix}"
        try:
            path.replace(target)
        except OSError:
            path.unlink(missing_ok=True)

    # ---------- thread 记忆（对话轮次靠它数）----------

    def _history(self, ctx: HandlerContext, thread: str) -> list[Msg]:
        return ThreadMemory(
            ThreadMemory.path_for(ctx.layout.agent_dir(ctx.identity.agent), thread)
        ).load()

    def _remember(self, ctx: HandlerContext, source: Envelope, reply: Envelope) -> None:
        memory = ThreadMemory(
            ThreadMemory.path_for(ctx.layout.agent_dir(ctx.identity.agent), source.thread)
        )
        memory.extend_once(
            reply.id,
            [
                Msg.user(_incoming_text(source)),
                Msg(role=Role.ASSISTANT, content=_outgoing_body(reply)),
            ],
        )


# ---------- 文件格式 ----------


def render_request(env: Envelope) -> str:
    needs_reply = message_expects_reply(env)
    instruction = (
        f"回复：在 ../outbox/{env.id}.md 写下正文即可（这段注释不用删）。\n"
        "     想主动发一条新消息：在 outbox/ 下新建任意文件名的 .md，\n"
        "     开头写 --- / to: 某个agent / --- 再写正文。"
        if needs_reply
        else "这是答复或通知，不要回信；读完后归档即可。"
    )
    return REQUEST_TEMPLATE.format(
        frm=str(env.from_),
        to=str(env.to),
        kind=str(env.type),
        needs_reply=str(needs_reply).lower(),
        thread=env.thread,
        msg_id=env.id,
        body=_incoming_text(env),
        instruction=instruction,
    )


def _outgoing_body(env: Envelope) -> str:
    payload = env.payload
    if isinstance(payload, ChatPayload):
        return payload.body
    if isinstance(payload, TaskResultPayload):
        return payload.summary
    # bridge 主动派出的 task.request；这里只取线协议已有的正文。
    return str(getattr(payload, "body", ""))


def _draft_fingerprint(note: BridgeNote) -> str:
    normalized = json.dumps(
        {
            "name": note.path.name,
            "mtime_ns": note.mtime_ns,
            "headers": note.headers,
            "body": note.body,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def note_needs_reply(headers: dict[str, str]) -> bool:
    """新 note 读显式字段，旧 note 退回类型判定，保证升级时未处理来信可继续。"""
    value = headers.get("needs_reply", "").strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    return headers.get("type", "chat") in {str(kind) for kind in AWAITS_REPLY}


def parse_note(text: str) -> tuple[dict[str, str], str]:
    """极简 front matter：`--- / key: value / ---`，然后是正文。

    刻意不引 YAML：人要手写它，键值对已经够用，多一分语法就多一分写错的机会。
    """
    match = FRONT_MATTER_RE.match(text)
    if match is None:
        return {}, _strip_comments(text)
    headers: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            headers[key.strip().lower()] = value.strip()
    return headers, _strip_comments(match.group(2))


def _strip_comments(text: str) -> str:
    """把模板里那段 HTML 注释去掉 —— 人多半懒得删，删了反而容易连正文一起删。"""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _incoming_text(env: Envelope) -> str:
    """标题 + 正文，但标题只是正文开头时不重复一遍。

    `anthill send` 默认取正文前 60 字当标题，直接拼起来会把同一句话读两遍。
    """
    payload: Any = env.payload
    title = str(getattr(payload, "title", "") or "").strip()
    body = str(getattr(payload, "body", "") or getattr(payload, "summary", "") or "").strip()
    if not title or (body and body.startswith(title.rstrip("…"))):
        return body or title
    return f"{title}\n\n{body}".strip()


def _artifacts(headers: dict[str, str]) -> tuple[str, ...]:
    raw = headers.get("artifacts", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _mentions(headers: dict[str, str]) -> tuple[str, ...]:
    raw = headers.get("mentions", "")
    return tuple(part.strip().lstrip("@") for part in raw.split(",") if part.strip())


def _task_payload(body: str) -> Any:
    from anthill.core.payloads import TaskRequestPayload

    flat = " ".join(body.split())
    return TaskRequestPayload(title=flat[:60] or "任务", body=body)
