"""按 Agent 配置装配工具集。

node.toml 里 `tools = [...]` 留空表示给默认全集；写了就严格按写的来 ——
「reviewer 只有 read_file 与 finish」这种最小权限配置要能一眼看出来。
"""

from __future__ import annotations

from anthill.agent.tools.base import Tool
from anthill.agent.tools.finish import FinishTool
from anthill.agent.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from anthill.agent.tools.shell import RunShellTool
from anthill.core.errors import ToolError

TOOL_FACTORIES = {
    "read_file": ReadFileTool,
    "write_file": WriteFileTool,
    "list_dir": ListDirTool,
    "run_shell": RunShellTool,
    "finish": FinishTool,
}

DEFAULT_TOOL_NAMES = tuple(TOOL_FACTORIES)


def build_toolset(names: tuple[str, ...]) -> list[Tool]:
    selected = names or DEFAULT_TOOL_NAMES
    unknown = [n for n in selected if n not in TOOL_FACTORIES]
    if unknown:
        known = ", ".join(sorted(TOOL_FACTORIES))
        raise ToolError(f"未知工具 {', '.join(unknown)}；可用工具：{known}")
    return [TOOL_FACTORIES[name]() for name in selected]
