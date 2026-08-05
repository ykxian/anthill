"""文件工具：read_file / write_file / edit_file / list_dir。

路径解析全部走 `ctx.resolve()`，所以逃逸防护只有一处实现、一处测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.payloads import RiskLevel

MAX_LIST_ENTRIES = 200
MAX_READ_LINES = 800
"""一次最多读多少行。读不完会明确告诉模型「还有，从第几行接着读」——
以前是一次性读、超了直接截断，模型无从知道自己只看到了半个文件。"""


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "读取 workspace 或 blackboard 内的文本文件。路径用相对形式，如 src/a.py。"
        "文件很大时用 offset/limit 按行翻页读。"
    )
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": string_param("相对 workspace 的文件路径"),
            "offset": {"type": "integer", "description": "从第几行开始读（1 起），默认 1"},
            "limit": {"type": "integer", "description": f"最多读多少行，默认 {MAX_READ_LINES}"},
        },
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

        offset = max(1, _int_arg(args, "offset", 1))
        limit = max(1, _int_arg(args, "limit", MAX_READ_LINES))
        lines = text.splitlines()
        window = lines[offset - 1 : offset - 1 + limit]
        if not window and offset > 1:
            return ToolResult.failed(
                f"{ctx.relative(path)} 只有 {len(lines)} 行，读不到第 {offset} 行"
            )

        body = "\n".join(window)
        end = offset - 1 + len(window)
        if end < len(lines):
            # **必须告诉它后面还有。** 以前是一次性读、超了直接截断，
            # 模型无从知道自己只看到了半个文件 —— 文件后半部分等于永远读不到。
            body += f"\n…（第 {offset}–{end} 行，共 {len(lines)} 行；继续读：offset={end + 1}）"
        return ToolResult.ok_result(body).truncated(ctx.security.max_output_bytes)


class EditFileTool(BaseTool):
    name = "edit_file"
    description = (
        "把文件里的一段原文替换成新内容（精确匹配）。"
        "改几行不必用 write_file 重写整个文件 —— 那样既费 token 又容易把别的地方写坏。"
        "old 必须在文件里唯一出现；不唯一就多带几行上下文。"
    )
    risk = RiskLevel.MEDIUM
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": string_param("目标路径"),
            "old": string_param("要被替换掉的原文，必须和文件里一模一样"),
            "new": string_param("替换成什么"),
        },
        "required": ["path", "old", "new"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = ctx.resolve(str(args.get("path", "")))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        if not path.is_file():
            return ToolResult.failed(f"文件不存在：{ctx.relative(path)}")
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        if not old:
            return ToolResult.failed("old 不能为空 —— 要新建文件请用 write_file")
        if old == new:
            return ToolResult.failed("old 和 new 一样，这次编辑没有意义")

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.failed(f"读取 {ctx.relative(path)} 失败：{exc}")

        hits = text.count(old)
        if hits == 0:
            return ToolResult.failed(f"在 {ctx.relative(path)} 里找不到那段原文（注意空格与缩进）")
        if hits > 1:
            # 不猜是哪一处 —— 改错地方比改不动更难查
            return ToolResult.failed(
                f"那段原文在 {ctx.relative(path)} 里出现了 {hits} 次，多带几行上下文"
            )

        try:
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult.failed(f"写入 {ctx.relative(path)} 失败：{exc}")
        rel = ctx.relative(path)
        return ToolResult.ok_result(f"已修改 {rel}", artifacts=(rel,))


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


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    """模型经常把整数写成字符串，别为这个报错。"""
    try:
        return int(args.get(key) or default)
    except (TypeError, ValueError):
        return default


def _sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name)
