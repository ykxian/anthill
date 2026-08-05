"""检索工具：search_text（按内容找）/ find_files（按文件名找）。

没有这两个之前，Agent 想在一个陌生代码库里定位一处实现，只能一层层 `list_dir`
（还封顶 200 项）—— 这不是「慢」，是**根本做不到**。

`run_shell` 本可以顶上（`grep -rn` / `find`），但白名单只有七条验证类命令，
白名单外一律 HIGH → 无人值守时策略引擎判 DENY。也就是说框架主打的
「无人值守多机协作」场景下，Agent 实际只能读文件、写文件、跑 pytest。

白名单的设计理由是站得住的（能跑任意命令的 Agent 等于能在这台机器上做任何事），
但结论应该是**把只读检索做成受控工具**，而不是留着 run_shell 形同虚设。
所以这两个工具：只读、限定在 workspace 内、结果有上限、风险 LOW。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.payloads import RiskLevel

MAX_HITS = 100
MAX_FILES_SCANNED = 5_000
MAX_LINE_CHARS = 200
MAX_FILE_BYTES = 2 * 1024 * 1024

SKIP_DIRS = frozenset(
    {
        ".git",
        ".anthill",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
"""不进这些目录。不是为了快 —— 是为了**结果有用**：
`.venv` 里几万个匹配会把真正想找的那几行淹掉，也会把上下文预算烧光。"""


class SearchTextTool(BaseTool):
    name = "search_text"
    description = (
        "在 workspace 里按内容搜索（正则），返回 文件:行号:该行。"
        "找某个函数在哪被调用、某段配置写在哪里，用这个，不要一层层 list_dir。"
    )
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": string_param("正则表达式，如 def send_receipt"),
            "path": string_param("在哪个子目录里找，默认整个 workspace", default="."),
            "glob": string_param("只看匹配这个通配符的文件，如 *.py", default=""),
        },
        "required": ["pattern"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(args.get("pattern", "")).strip()
        if not raw:
            return ToolResult.failed("pattern 不能为空")
        try:
            needle = re.compile(raw)
        except re.error as exc:
            return ToolResult.failed(f"正则写错了：{exc}")
        try:
            root = ctx.resolve(str(args.get("path") or "."))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        if not root.is_dir():
            return ToolResult.failed(f"目录不存在：{ctx.relative(root)}")

        pattern = str(args.get("glob") or "").strip()
        hits: list[str] = []
        scanned = 0
        for path in _walk(root):
            if scanned >= MAX_FILES_SCANNED:
                break
            if pattern and not path.match(pattern):
                continue
            scanned += 1
            hits.extend(_hits_in(path, needle, ctx))
            if len(hits) >= MAX_HITS:
                hits = hits[:MAX_HITS]
                hits.append(f"…（结果超过 {MAX_HITS} 条，把 pattern 写得更具体一些）")
                break

        if not hits:
            return ToolResult.ok_result(f"没有匹配 {raw!r} 的内容（找了 {scanned} 个文件）")
        return ToolResult.ok_result("\n".join(hits)).truncated(ctx.security.max_output_bytes)


class FindFilesTool(BaseTool):
    name = "find_files"
    description = (
        "在 workspace 里按文件名通配符找文件，如 **/*.py 或 test_*.py。"
        "只知道文件名、不知道它在哪层目录时用这个。"
    )
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "glob": string_param("通配符，如 **/*.py"),
            "path": string_param("从哪个子目录开始找，默认整个 workspace", default="."),
        },
        "required": ["glob"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("glob", "")).strip()
        if not pattern:
            return ToolResult.failed("glob 不能为空")
        if pattern.startswith("/") or ".." in pattern:
            return ToolResult.failed("glob 只能是相对形式，不能用 / 开头或包含 ..")
        try:
            root = ctx.resolve(str(args.get("path") or "."))
        except ValueError as exc:
            return ToolResult.failed(str(exc))
        if not root.is_dir():
            return ToolResult.failed(f"目录不存在：{ctx.relative(root)}")

        try:
            found = [
                ctx.relative(p)
                for p in sorted(root.glob(pattern))
                if p.is_file() and not _skipped(p, root)
            ]
        except (OSError, ValueError) as exc:
            return ToolResult.failed(f"查找失败：{exc}")

        if not found:
            return ToolResult.ok_result(f"没有匹配 {pattern!r} 的文件")
        shown = found[:MAX_HITS]
        if len(found) > len(shown):
            shown.append(f"…（共 {len(found)} 个，只显示前 {MAX_HITS} 个）")
        return ToolResult.ok_result("\n".join(shown))


def _walk(root: Path) -> Iterator[Path]:
    """深度优先遍历，跳过那些一进去就淹没结果的目录。"""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue  # 软链可能绕出 workspace，也可能成环
        if entry.is_dir():
            if entry.name not in SKIP_DIRS:
                yield from _walk(entry)
        elif entry.is_file():
            yield entry


def _skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in parts)


def _hits_in(path: Path, needle: re.Pattern[str], ctx: ToolContext) -> list[str]:
    """一个文件里的匹配行。读不动就跳过 —— 二进制文件不是错误，只是不相关。"""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rel = ctx.relative(path)
    return [
        f"{rel}:{number}:{line[:MAX_LINE_CHARS]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if needle.search(line)
    ]
