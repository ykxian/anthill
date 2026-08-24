"""把一个常驻会话（Claude Code / Codex / 就是你本人）当成 coordinator 的大脑。

## 为什么这层值得存在

编排本来要求 coordinator 配一个 `provider` —— 也就是要一把 API key。而这个
项目最常见的用法恰恰是「几个 Claude Code 会话互相协作」，那些会话本身就是
很强的模型，却只能当 worker：拆解、派活、汇总非得再花钱调一次 API。

## 接合点选在哪

**不改编排状态机一行。** `CoordinatorHandler` 只依赖 `ChatProvider.complete()`，
而且拆解与判定都是 `complete([Msg.user(prompt)], [])` —— 不用工具。所以让
「问人」实现 `ChatProvider` 就够了，`generate_plan` / `_judge` / 重试反馈
全部原样复用。换句话说：桥接不是编排的一个特例，它只是又一种 provider。

## 走的是同一批文件

问题写进 `bridge/inbox/<id>.md`，回答从 `bridge/outbox/<id>.md` 读 —— 和普通
消息一模一样的约定。这不是偷懒：值守会话本来就挂着
`anthill bridge <名字> --wait -1` 盯着 inbox，用同一个位置意味着**它不用学
任何新东西**，`anthill bridge <名字> --reply <id> --text-file …` 也照常能用。

**不写 `pending/<id>.json`** —— 那个目录放的是待回复的信封，而这里问的不是
一条消息。少了它，`--reply` 依然能匹配（它按 inbox 里的文件名匹配），而
BridgeHandler 的投递逻辑不会把回答当成一条要发出去的消息。

## 谁也不会跟谁抢

桥接 coordinator 的 handler 是 `CoordinatorHandler`（见 agent/factory.py），
**不是** `BridgeHandler` —— 所以没有第二个 tick 循环在扫这个 outbox，回答只会
被这里读走。普通桥接 worker 那条路完全不受影响。

## 阻塞是有意的，但必须有界

`complete()` 会一直等到人回答。agentd 的消费循环是串行的，所以等待期间这只
coordinator 处理不了别的消息 —— 这可以接受，因为两次调用的时机都很安全：
拆解在派活之前（此时没有别的消息要处理），判定在所有步骤都完成之后。
而 `tick()` 跑在另一个 task 上，超时与催办照常。

真正不能接受的是**无界**等待：人走开了就把 coordinator 永远焊死。所以有
`timeout`，超时抛 `ProviderError` —— `generate_plan` 会把它变成一条
「拆解任务失败」的 task.error 回给发起方，而不是让人对着空白看板等下去。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from anthill.adapters.bridge import BRIDGE_DIR, DONE, INBOX, OUTBOX, STABLE_SECONDS, parse_note
from anthill.core.errors import ProviderError
from anthill.core.ids import new_id, now
from anthill.providers.base import ChatProvider, Msg, Role, ToolSpec, Turn

DEFAULT_ASK_TIMEOUT = 1800.0
"""默认等半小时。比模型超时长得多是故意的 —— 另一头是人，去泡杯咖啡很正常。"""

POLL_INTERVAL = 0.5

ASK_TEMPLATE = """\
---
from: {agent}（编排大脑）
to: {agent}
type: ask
id: {ask_id}
---

# 请你充当 coordinator 的大脑

下面是编排要问你的问题。**按它要求的格式回答**（通常是「只输出一个 JSON」），
你的回答会被直接解析，所以别加寒暄、别加代码块以外的解释。

{body}

<!-- 回答：把内容写进 ../outbox/{ask_id}.md，或者跑
     anthill bridge {agent} --reply {ask_id} --text-file <文件>
     （正文含反引号或 ${{…}} 时务必用 --text-file，别用 --text）。 -->
"""

_ROLE_LABEL = {
    Role.SYSTEM: "【系统】",
    Role.USER: "【要求】",
    Role.ASSISTANT: "【你上一次的回答】",
    Role.TOOL: "【工具结果】",
}


def render_ask(messages: list[Msg]) -> str:
    """把一串对话渲染成人能读的问题。

    多轮的情形真的会发生：`generate_plan` 在计划不合法时会把「你上次回的」和
    「哪里不对」一起追加进来再问一遍。只显示最后一条的话，人看不到自己刚才
    错在哪 —— 那正是最需要看到的东西。
    """
    if not messages:
        return "（没有内容）"
    if len(messages) == 1:
        return messages[0].content
    return "\n\n".join(
        f"{_ROLE_LABEL.get(m.role, '【' + str(m.role) + '】')}\n{m.content}" for m in messages
    )


class BridgeProvider(ChatProvider):
    """问一个常驻会话，等它把答案写回来。

    对 `CoordinatorHandler` 而言它就是个普通 provider；对值守会话而言这就是
    又一条待办。两边都不需要知道对面是什么。
    """

    def __init__(
        self,
        *,
        root: Path,
        agent_name: str,
        timeout: float = DEFAULT_ASK_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self.name = f"bridge:{agent_name}"
        self.model = "human-in-the-loop"
        self._root = root / BRIDGE_DIR
        self._agent = agent_name
        self._timeout = timeout
        self._poll = poll_interval
        for name in (INBOX, OUTBOX, DONE):
            self._dir(name)

    def _dir(self, name: str) -> Path:
        path = self._root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        if tools:
            # 人没法按工具调用的格式回话。编排本身从不传工具（拆解与判定都是
            # 纯文本问答），所以走到这里说明有人把它接到了 ReAct 循环上 ——
            # 那条路要的是另一套设计，不能假装支持。
            raise ProviderError(
                f"{self.name} 不支持工具调用：桥接大脑只能回文本。"
                "带工具的 Agent 请配一个真的 provider"
            )
        ask_id = new_id()
        ask = self._dir(INBOX) / f"{ask_id}.md"
        ask.write_text(
            ASK_TEMPLATE.format(agent=self._agent, ask_id=ask_id, body=render_ask(messages)),
            encoding="utf-8",
        )
        try:
            answer = await self._await_answer(ask_id)
        except TimeoutError as exc:
            raise ProviderError(
                f"等 {self._agent} 回答等了 {self._timeout:.0f} 秒还没等到"
                f"（问题在 {BRIDGE_DIR}/{INBOX}/{ask_id}.md）"
            ) from exc
        return Turn(text=answer)

    async def _await_answer(self, ask_id: str) -> str:
        """轮询等回答。

        用轮询而不是 inotify：等的是**人**，秒级延迟无所谓，而轮询在 NFS、
        容器挂载这些 watcher 会失灵的地方一样可靠 —— 项目别处也是这个取舍。
        """
        reply = self._dir(OUTBOX) / f"{ask_id}.md"
        deadline = now().timestamp() + self._timeout
        while now().timestamp() < deadline:
            body = self._read_if_settled(reply)
            if body:
                self._archive(ask_id)
                return body
            await asyncio.sleep(self._poll)
        raise TimeoutError(ask_id)

    def _read_if_settled(self, path: Path) -> str:
        """读一份**写完了**的回答，否则返回空串。

        「写完了」同样用 mtime 至少 `STABLE_SECONDS` 前来判断 —— 和
        `BridgeHandler.drafts()` 一个标准：编辑器和 Agent 都是边写边刷盘的，
        抢在中途读走会拿到半句话，而半句话会被当成一份不合法的计划。
        """
        try:
            if path.stat().st_mtime > now().timestamp() - STABLE_SECONDS:
                return ""
            _, body = parse_note(path.read_text(encoding="utf-8"))
        except OSError:
            return ""
        return body.strip()

    def _archive(self, ask_id: str) -> None:
        """问答都挪进 done/，别让 inbox 攒着已经答过的问题。

        挪不动不算失败：答案已经拿到了，为一次归档失败把整次编排搞砸不值得。
        """
        done = self._dir(DONE)
        for folder in (INBOX, OUTBOX):
            path = self._root / folder / f"{ask_id}.md"
            with suppress(OSError):
                path.replace(done / f"{ask_id}.{folder}.md")
