"""把外部 MCP server 的工具挂进 Agent 的工具集。

## 为什么是 MCP 而不是自己造插件体系

评审点了两条：工具集偏薄，以及 `TOOL_FACTORIES` 是个硬编码字典 ——
加一个工具必须改源码，没有 entry points、没有插件发现。

自己造一套插件发现机制，是重新发明一个**已经有事实标准**的东西。
直接说 MCP，整个生态的工具立刻可用（文件系统、数据库、浏览器、公司内部的
那些），而且 `node.toml` 里声明外部工具这件事有了自然的写法：

    [mcp.files]
    command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]

    [agents.coder]
    provider = "deepseek"
    mcp = ["files"]          # 这个 Agent 能用 files 那台 server 的全部工具

## 安全上的三条

1. **风险等级默认 HIGH。** 外部工具能干什么我们不知道 —— 它可能读你的数据库、
   可能发 HTTP 请求。策略引擎照常管着它：无人值守时 HIGH 直接 DENY，
   要用就显式在 `[mcp.<名字>] risk = "medium"` 里降级，**由人做这个判断**。
2. **名字加前缀**（`files__read_file`），不和内置工具撞名，也让日志里一眼看出
   这次调用出了本进程。
3. **起不来不拖垮 Agent。** 连不上就跳过那台 server 并记一条日志 ——
   一个外部依赖挂了不该让整个 agentd 起不来。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from anthill.agent.tools.base import ToolContext, ToolResult
from anthill.core.config import McpSection
from anthill.core.errors import ToolError
from anthill.core.logging import EventLog
from anthill.core.payloads import RiskLevel

CONNECT_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0
NAME_SEPARATOR = "__"


class McpTool:
    """一个远端 MCP 工具，套上本地 `Tool` 协议的外壳。"""

    def __init__(
        self,
        *,
        server: str,
        remote_name: str,
        description: str,
        parameters: dict[str, Any],
        session: Any,
        risk: RiskLevel,
    ) -> None:
        self.name = f"{server}{NAME_SEPARATOR}{remote_name}"
        self.description = f"[{server}] {description}".strip()
        self.risk = risk
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._server = server
        self._remote_name = remote_name
        self._session = session

    @property
    def spec(self) -> Any:
        from anthill.providers.base import ToolSpec

        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def risk_for(self, args: dict[str, Any], ctx: ToolContext) -> RiskLevel:
        return self.risk

    def describe_call(self, args: dict[str, Any]) -> str:
        return f"{self.name} {args}"[:400]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """调远端。**超时、异常都变成失败的 ToolResult，不往上抛** ——
        外部工具坏掉只该让这一步失败，不该杀掉 Agent 循环。"""
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._remote_name, args), timeout=CALL_TIMEOUT
            )
        except TimeoutError:
            return ToolResult.failed(f"{self.name} 超时（{CALL_TIMEOUT:g}s）")
        except Exception as exc:  # 远端什么都可能抛，包括传输层的
            return ToolResult.failed(f"{self.name} 调用失败：{type(exc).__name__}: {exc}")
        if _is_error(result):
            return ToolResult.failed(f"{self.name}：{_text_of(result)}")
        return ToolResult.ok_result(_text_of(result)).truncated(ctx.security.max_output_bytes)


class McpToolset:
    """一组已连上的 MCP server。**必须当异步上下文管理器用**，退出时统一断开。"""

    def __init__(self, log: EventLog | None = None) -> None:
        self._stack = AsyncExitStack()
        self._log = log
        self.tools: list[McpTool] = []

    async def __aenter__(self) -> McpToolset:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def connect(self, name: str, section: McpSection) -> int:
        """连一台 server，把它的工具收进来。返回收到几个；失败返回 0。"""
        try:
            # wait_for 会在单独的 Task 里运行 coroutine。MCP/AnyIO 的上下文若在
            # 那个 Task 里进入、最后却由调用 connect() 的 Task 退出，Python 3.11
            # 会拒绝跨 Task 清理 cancel scope。asyncio.timeout 保留超时语义，
            # 同时让连接的进入与退出始终发生在同一个 Task。
            async with asyncio.timeout(section.timeout or CONNECT_TIMEOUT):
                session = await self._open(section)
            async with asyncio.timeout(CONNECT_TIMEOUT):
                listed = await session.list_tools()
        except Exception as exc:
            # 一个外部依赖挂了，不该让整个 agentd 起不来
            if self._log is not None:
                self._log.error(
                    "mcp.connect_failed", server=name, error=f"{type(exc).__name__}: {exc}"
                )
            return 0

        risk = RiskLevel(section.risk)
        for tool in listed.tools:
            self.tools.append(
                McpTool(
                    server=name,
                    remote_name=tool.name,
                    description=tool.description or "",
                    parameters=dict(_schema_of(tool)),
                    session=session,
                    risk=risk,
                )
            )
        if self._log is not None:
            self._log.info("mcp.connected", server=name, tools=len(listed.tools), risk=str(risk))
        return len(listed.tools)

    async def _open(self, section: McpSection) -> Any:
        """只支持 stdio —— 那是 MCP 最常见也最省事的一种，不用管端口和鉴权。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - 取决于装没装
            raise ToolError("缺少 mcp 依赖；执行 `uv sync --extra mcp` 安装") from exc

        params = StdioServerParameters(
            command=section.command[0],
            args=list(section.command[1:]),
            env=section.env or None,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session


def _schema_of(tool: Any) -> dict[str, Any]:
    """入参 schema 的字段名在 mcp 1.x 是 `inputSchema`，2.0 起是 `input_schema`。

    两个都认 —— 这类改名只有真连一次外部 server 才会现形，
    用假对象测是测不出来的。
    """
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    return dict(schema or {})


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _text_of(result: Any) -> str:
    """MCP 的返回是一串 content block，取其中的文本。"""
    parts = []
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(p for p in parts if p) or "（没有输出）"
