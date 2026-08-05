"""thread 历史落盘与超长摘要压缩。

一个 thread 一个 jsonl 文件，追加写。这样做的好处是：agentd 崩了重启，
同一 thread 的后续消息仍然带着之前的上下文；而且这份文件本身就是可读的排查材料。

历史超过阈值时用模型把「老的部分」压成一段摘要，只保留最近几轮原文。
摘要失败时保持原样 —— 上下文长一点没关系，把历史弄丢就麻烦了。
"""

from __future__ import annotations

import json
from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.errors import ProviderError
from anthill.core.ids import is_valid_id
from anthill.providers.base import (
    ChatProvider,
    Msg,
    drop_orphan_tool_results,
    estimate_tokens,
)

THREADS_DIR = "threads"
DEFAULT_COMPACT_THRESHOLD = 24_000
DEFAULT_KEEP_TAIL = 6
SUMMARY_PREFIX = "【历史摘要】"

SUMMARY_PROMPT = """\
下面是一段 Agent 对话历史。请压缩成一段不超过 400 字的摘要，
保留：任务目标、已完成的动作、产出的文件路径、尚未解决的问题。
不要评论，直接给摘要正文。

{history}\
"""


class ThreadMemory:
    def __init__(
        self,
        path: Path,
        *,
        compact_threshold: int = DEFAULT_COMPACT_THRESHOLD,
        keep_tail: int = DEFAULT_KEEP_TAIL,
    ) -> None:
        self._path = path
        self._compact_threshold = compact_threshold
        self._keep_tail = keep_tail

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def path_for(agent_dir: Path, thread: str) -> Path:
        """thread id 直接当文件名，所以必须先确认它是 ULID —— 否则就是路径注入。"""
        if not is_valid_id(thread):
            raise ValueError(f"非法 thread id {thread!r}：只接受 ULID")
        return agent_dir / THREADS_DIR / f"{thread}.jsonl"

    # ---------- 读写 ----------

    def append(self, msg: Msg) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

    def extend(self, messages: list[Msg]) -> None:
        for msg in messages:
            self.append(msg)

    def load(self) -> list[Msg]:
        """坏行跳过而不是抛错：一条日志损坏不该让整个 thread 不可用。"""
        if not self._path.is_file():
            return []
        out: list[Msg] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(Msg.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
        return out

    def approx_tokens(self) -> int:
        return sum(m.approx_tokens for m in self.load())

    # ---------- 压缩 ----------

    async def compact(self, provider: ChatProvider) -> bool:
        """超阈值时把老历史换成摘要。返回是否真的压缩了。"""
        messages = self.load()
        if sum(m.approx_tokens for m in messages) <= self._compact_threshold:
            return False
        if len(messages) <= self._keep_tail:
            return False

        head, tail = messages[: -self._keep_tail], messages[-self._keep_tail :]
        try:
            turn = await provider.complete(
                [Msg.user(SUMMARY_PROMPT.format(history=_render(head)))], []
            )
        except ProviderError:
            return False  # 压缩是优化不是必需，失败就带着长历史继续跑
        summary = turn.text.strip()
        if not summary:
            return False

        self._rewrite([Msg.system(f"{SUMMARY_PREFIX}{summary}"), *drop_orphan_tool_results(tail)])
        return True

    def _rewrite(self, messages: list[Msg]) -> None:
        """整体重写走原子写，避免压缩中途崩溃留下半个历史文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(
            json.dumps(m.to_dict(), ensure_ascii=False) + "\n" for m in messages
        ).encode()
        atomic_write(self._path.parent, self._path.parent, self._path.name, data)


def _render(messages: list[Msg]) -> str:
    return "\n".join(f"[{m.role}] {m.content}" for m in messages if m.content)


def estimate_history_tokens(messages: list[Msg]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)
