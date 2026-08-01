"""文件工具：read_file / write_file / list_dir。

路径解析全部走 `ctx.resolve()`，所以逃逸防护只有一处实现、一处测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.payloads import RiskLevel

MAX_LIST_ENTRIES = 200


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取 workspace 或 blackboard 内的文本文件。路径用相对形式，如 src/a.py。"
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": string_param("相对 workspace 的文件路径")},
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve(str(args.get("path", "")))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        if not path.is_file():
            return ToolResult.failed(f"文件不存在：{ctx.relative(path)}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.failed(f"读取 {ctx.relative(path)} 失败：{exc}")
        return ToolResult.ok_result(text).truncated(ctx.security.max_output_bytes)


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "写入文本文件（覆盖）。父目录会自动创建。写公共黑板用 blackboard://relative/path 形式。"
    )
    risk = RiskLevel.MEDIUM
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": string_param("目标路径"),
            "content": string_param("完整文件内容"),
        },
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve(str(args.get("path", "")))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        content = str(args.get("content", ""))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.failed(f"写入 {ctx.relative(path)} 失败：{exc}")
        rel = ctx.relative(path)
        return ToolResult.ok_result(f"已写入 {rel}（{len(content)} 字符）", artifacts=(rel,))


class ListDirTool(BaseTool):
    name = "list_dir"
    description = "列出目录内容。目录以 / 结尾。"
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": string_param("目录路径，默认当前 workspace 根", default=".")},
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve(str(args.get("path") or "."))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        if not path.is_dir():
            return ToolResult.failed(f"目录不存在：{ctx.relative(path)}")
        try:
            entries = sorted(path.iterdir(), key=_sort_key)
        except OSError as exc:
            return ToolResult.failed(f"列出 {ctx.relative(path)} 失败：{exc}")

        shown = entries[:MAX_LIST_ENTRIES]
        lines = [f"{e.name}/" if e.is_dir() else e.name for e in shown]
        if len(entries) > len(shown):
            lines.append(f"…（共 {len(entries)} 项，只显示前 {MAX_LIST_ENTRIES} 项）")
        return ToolResult.ok_result("\n".join(lines) or "（空目录）")


def _sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name)
