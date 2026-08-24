"""桥接 outbox **只能有一个消费者** —— 这是一条全仓纪律，不是某个模块的内务。

## 为什么值得钉一颗钉子

自从桥接 coordinator 能当编排大脑（adapters/bridge_provider.py），同一个
`bridge/outbox/` 上坐着两种语义完全不同的东西：

- 普通桥接 Agent：草稿 = 一条**要发出去的消息**，由 `BridgeHandler.tick()` 投递；
- 桥接大脑：草稿 = 编排问题的**答案**，由 `BridgeProvider.complete()` 读走。

两者不会同时发生（一个 Agent 只会是其中一种），但**多一个消费者就会串台**：
谁抢先把答案当成消息投出去，`complete()` 那边只会一直等到超时 —— 人看到的
是一次「假装成功」，而现场什么线索都没有。

## 扫 `drafts()` 而不是 `tick()`

今天的链是 `tick() → drafts() → 投递`，所以扫 `tick` 看着也行。但 `tick` 是
**今天的形状**，`drafts()` 才是那个资源的入口：更可能的破坏是有人绕过 tick
直接消费 outbox（面板加一个「立即发送」、某个修复图快直接调 `drafts()`），
那种改动扫 `tick` 一个字都看不见，后果却一模一样。

两条都钉：`drafts()` 是资源本身，`tick` 是目前唯一的驱动方式。

## 断言「集合相等」而不是「在白名单里」

白名单会烂 —— 加调用点的人顺手把自己加进名单，测试就退化成橡皮图章。
写成集合相等，加一个就红，逼他显式决定：要么改设计，要么改这条不变式并
在这里写下为什么。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "anthill"

DRAFTS_CONSUMERS = {"adapters/bridge.py"}
"""唯一可以消费 outbox 草稿的地方（`BridgeHandler.tick` 就在这个文件里）。"""

TICK_DRIVERS = {"agent/runtime.py"}
"""唯一可以驱动 handler tick 的地方 —— agentd 的运行时。"""

WHY = """
桥接 outbox 只能有一个消费者：同一个目录上，普通桥接 Agent 的草稿是「要发出去
的消息」，而桥接 coordinator（编排大脑）的草稿是「编排问题的答案」。多一个消费
者就会把答案当成消息投出去，而 BridgeProvider.complete() 只会一直等到超时 ——
人看到的是一次「假装成功」，现场没有任何线索。

真要加，先想清楚这两种语义怎么区分，再改这条不变式并在 tests/unit/
test_outbox_single_consumer.py 里写下理由。
"""


def _callers_of(method: str) -> set[str]:
    """全仓里调用了 `xxx.<method>()` 的模块（相对 anthill/ 的路径）。

    用 AST 而不是字符串搜索：注释和文档字符串里提到 `drafts()` 的地方不该算
    —— 这个文件自己和 bridge_provider 的模块文档就各提了好几次。
    """
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 两种形状都要认：`handler.drafts()` 是 Attribute，而 runtime 里
            # 先 `tick = getattr(handler, "tick")` 再 `tick(ctx)` 是 Name
            attribute_call = isinstance(func, ast.Attribute) and func.attr == method
            bare_call = isinstance(func, ast.Name) and func.id == method
            if attribute_call or bare_call:
                found.add(path.relative_to(SRC).as_posix())
    return found


def test_only_the_bridge_handler_consumes_outbox_drafts() -> None:
    assert _callers_of("drafts") == DRAFTS_CONSUMERS, (
        f"消费 outbox 草稿的地方变了：{sorted(_callers_of('drafts'))}\n{WHY}"
    )


def test_only_the_runtime_drives_handler_ticks() -> None:
    """tick 是目前唯一驱动投递的路径，多一个驱动方等于多一个消费者。

    `runtime.py` 里是 `tick(self._ctx)`（先 `getattr` 取出来再调），所以
    按 `ast.Name` 也能认出来 —— `_callers_of` 两种形状都收。
    """
    assert _callers_of("tick") == TICK_DRIVERS, (
        f"驱动 handler tick 的地方变了：{sorted(_callers_of('tick'))}\n{WHY}"
    )
