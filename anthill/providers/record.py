"""录制 / 回放（03-tech-design §10）。

真实调一次模型很贵，而集成测试要跑几百次。`--record` 把真实响应写成一条 jsonl 录制带，
`--replay` 用它当假模型跑，既省钱又让测试可重现。录制带同时是排查现场的一手材料。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from anthill.core.errors import ProviderError
from anthill.core.ids import now
from anthill.providers.base import ChatProvider, Msg, ToolSpec, Turn


class RecordingProvider(ChatProvider):
    """透传到真 provider，同时把「请求摘要 + 完整响应」追加进录制带。"""

    def __init__(self, inner: ChatProvider, tape: Path) -> None:
        self._inner = inner
        self.name = f"record:{inner.name}"
        self.model = inner.model
        tape.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = tape.open("a", encoding="utf-8")

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        turn = await self._inner.complete(messages, tools)
        self._write(
            {
                "ts": now().isoformat(),
                "model": self._inner.model,
                "request": {
                    "messages": [m.to_dict() for m in messages],
                    "tools": [t.name for t in tools],
                },
                "turn": turn.to_dict(),
            }
        )
        return turn

    def _write(self, record: dict[str, Any]) -> None:
        if self._fh is None:
            raise ProviderError("录制带已关闭，无法继续写入")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    async def aclose(self) -> None:
        self.close()
        await self._inner.aclose()


class ReplayProvider(ChatProvider):
    """按录制顺序回放。带用完就报错 —— 静默重复会让测试假绿。"""

    name = "replay"

    def __init__(self, turns: list[Turn], *, model: str = "replay-model") -> None:
        self._turns = list(turns)
        self._index = 0
        self.model = model

    @classmethod
    def from_file(cls, tape: Path) -> ReplayProvider:
        if not tape.is_file():
            raise ProviderError(f"录制带不存在：{tape}")
        turns: list[Turn] = []
        model = "replay-model"
        for lineno, line in enumerate(tape.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                turns.append(Turn.from_dict(record["turn"]))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ProviderError(f"录制带 {tape} 第 {lineno} 行损坏：{exc}") from exc
            model = str(record.get("model") or model)
        if not turns:
            raise ProviderError(f"录制带 {tape} 是空的")
        return cls(turns, model=model)

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        if self._index >= len(self._turns):
            raise ProviderError(
                f"录制带只有 {len(self._turns)} 轮，第 {self._index + 1} 次调用无内容可回放；"
                "重新用 --record 录一次"
            )
        turn = self._turns[self._index]
        self._index += 1
        return turn
