"""Tool 协议、执行上下文与结果，以及所有工具共用的路径安全解析。

安全底线只有一条：**任何路径参数都必须落在 workspace 或 blackboard 之内**。
解析走 `Path.resolve()`（会展开软链）后做前缀校验，`../` 与软链逃逸都拦得住。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from anthill.core.config import SecuritySection
from anthill.core.payloads import RiskLevel

BLACKBOARD_PREFIX = "blackboard://"
TRUNCATE_NOTE = "\n…（输出过长已截断，共 {total} 字符）"

Confirmer = Callable[[str], Awaitable[bool]]
"""向人索取一次确认。返回 True 表示放行。"""


@runtime_checkable
class Messenger(Protocol):
    """把工具层与 Sender 隔开：工具只知道「往谁发一句话」，不知道信封与传输。"""

    async def send(self, *, to: str, body: str, kind: str) -> str:
        """返回新消息的 id。发不出去抛 AntHillError 的子类。"""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """工具执行时能看到的全部世界。刻意很小 —— 工具不该拿到 sender 或 config 全量。"""

    workspace: Path
    blackboard: Path
    security: SecuritySection
    thread: str
    messenger: Messenger | None = None
    """没接消息通道的 Agent（比如单机跑的工具测试）用 None，send_message 会明确报错。"""
    sensitive_env: frozenset[str] = frozenset()
    """run_shell 等项目子进程绝不能继承的 provider 凭据变量名。"""

    def resolve(self, raw: str) -> Path:
        """把工具参数里的路径解析成绝对路径；越界抛 ValueError。

        支持 `blackboard://x` 前缀显式指向公共黑板，其余一律相对 workspace。
        """
        if not raw or not raw.strip():
            raise ValueError("路径不能为空")
        text = raw.strip()
        if text.startswith(BLACKBOARD_PREFIX):
            root, rel = self.blackboard, text[len(BLACKBOARD_PREFIX) :]
        else:
            root, rel = self.workspace, text
        if rel.startswith("~"):
            raise ValueError(f"路径 {raw!r} 越界：不允许使用 ~ 展开到用户目录")

        candidate = (root / rel).expanduser()
        resolved = candidate.resolve()
        root_resolved = root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise ValueError(f"路径 {raw!r} 越界：只能访问 {root_resolved} 之内")
        return resolved

    def relative(self, path: Path) -> str:
        """回给模型的路径一律用相对形式，避免把绝对路径泄进上下文。"""
        for root, prefix in ((self.workspace, ""), (self.blackboard, BLACKBOARD_PREFIX)):
            try:
                return prefix + str(path.resolve().relative_to(root.resolve()))
            except ValueError:
                continue
        return str(path)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具执行的结果。失败也是正常返回值 —— 让模型看到错误并自我修正。"""

    ok: bool
    content: str
    artifacts: tuple[str, ...] = ()
    is_finish: bool = False
    status: str = "ok"

    @classmethod
    def ok_result(
        cls, content: str, *, artifacts: tuple[str, ...] = (), is_finish: bool = False
    ) -> ToolResult:
        return cls(ok=True, content=content, artifacts=artifacts, is_finish=is_finish)

    @classmethod
    def failed(cls, content: str) -> ToolResult:
        return cls(ok=False, content=content)

    def truncated(self, limit: int) -> ToolResult:
        if len(self.content) <= limit:
            return self
        note = TRUNCATE_NOTE.format(total=len(self.content))
        return replace(self, content=self.content[:limit] + note)


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    risk: RiskLevel

    @property
    def spec(self) -> Any: ...

    def risk_for(self, args: dict[str, Any], ctx: ToolContext) -> RiskLevel: ...

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class BaseTool:
    """默认实现：固定风险等级 + 由子类给出 JSON Schema。"""

    name = "tool"
    description = ""
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    @property
    def spec(self) -> Any:
        from anthill.providers.base import ToolSpec

        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def risk_for(self, args: dict[str, Any], ctx: ToolContext) -> RiskLevel:
        return self.risk

    def describe_call(self, args: dict[str, Any]) -> str:
        """确认提示里给人看的一行说明。"""
        return ", ".join(f"{k}={_shorten(v)}" for k, v in args.items())

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


def _shorten(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def string_param(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}
