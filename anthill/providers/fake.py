"""按脚本出牌的假 provider —— 集成测试的主力，不花一分钱 API 费。

脚本元素可以是 `Turn`（正常返回）或异常实例（模拟上游报错）。
脚本用完后重复最后一条，方便写「模型一直不肯收工」这类熔断测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from anthill.core.errors import ProviderError
from anthill.providers.base import ChatProvider, Msg, ToolSpec, Turn

Scripted = Turn | Exception


@dataclass(frozen=True, slots=True)
class FakeCall:
    """录下每次调用，测试里断言「模型确实看到了这些消息」。"""

    messages: tuple[Msg, ...]
    tools: tuple[ToolSpec, ...]


class FakeProvider(ChatProvider):
    name = "fake"
    model = "fake-model"

    def __init__(self, script: list[Scripted] | None = None) -> None:
        if not script:
            raise ProviderError("FakeProvider 需要至少一条脚本")
        self._script = list(script)
        self._calls: list[FakeCall] = []
        self._index = 0

    @property
    def calls(self) -> list[FakeCall]:
        return list(self._calls)

    async def complete(self, messages: list[Msg], tools: list[ToolSpec]) -> Turn:
        self._calls.append(FakeCall(messages=tuple(messages), tools=tuple(tools)))
        item = self._script[min(self._index, len(self._script) - 1)]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item
