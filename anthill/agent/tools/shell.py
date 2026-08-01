"""run_shell —— 全项目风险最高的工具。

三条硬约束（03-tech-design §10「子进程」）：
1. cwd 锁死在 workspace；
2. stdout 与 stderr **合并**捕获，避免只读一路导致管道写满而死锁；
3. 超时后杀掉整个进程组 —— 只 kill 直接子进程的话，`sh -c` 拉起的孙子进程会活下来。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from contextlib import suppress
from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.payloads import RiskLevel

KILL_GRACE_SECONDS = 2.0


class RunShellTool(BaseTool):
    name = "run_shell"
    description = (
        "在 workspace 目录下执行一条 shell 命令，返回合并后的 stdout+stderr 与退出码。"
        "白名单内的命令（pytest/ruff/git status 等）无需确认，其余命令需要人工确认。"
    )
    risk = RiskLevel.HIGH
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"command": string_param("要执行的完整命令")},
        "required": ["command"],
    }

    def risk_for(self, args: dict[str, Any], ctx: ToolContext) -> RiskLevel:
        command = str(args.get("command", ""))
        return (
            RiskLevel.MEDIUM
            if is_allowlisted(command, ctx.security.shell_allowlist)
            else RiskLevel.HIGH
        )

    def describe_call(self, args: dict[str, Any]) -> str:
        return str(args.get("command", ""))

    async def run(
        self, args: dict[str, Any], ctx: ToolContext, *, timeout: float | None = None
    ) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.failed("command 不能为空")
        limit = timeout if timeout is not None else ctx.security.shell_timeout

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(ctx.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # 合并，避免两路各自阻塞
                start_new_session=True,  # 自成进程组，超时才能整组杀干净
            )
        except OSError as exc:
            return ToolResult.failed(f"无法启动命令：{exc}")

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except TimeoutError:
            await _kill_group(proc)
            return ToolResult.failed(f"命令超时（>{limit:g}s）已终止：{command}")

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        code = proc.returncode or 0
        body = f"$ {command}\n退出码 {code}\n{output}".rstrip()
        result = ToolResult.ok_result(body) if code == 0 else ToolResult.failed(body)
        return result.truncated(ctx.security.max_output_bytes)


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """先 TERM 整组，宽限期后 KILL。孙子进程也在同组里，跑不掉。"""
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    with suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)


def is_allowlisted(command: str, allowlist: tuple[str, ...]) -> bool:
    """只认「命令行首」匹配，且不含 shell 串联符 —— `pytest; rm -rf /` 不算白名单。"""
    text = command.strip()
    if not text or any(ch in text for ch in (";", "|", "&", "`", "$(", ">", "<", "\n")):
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    if not tokens:
        return False
    return any(tokens[: len(entry.split())] == entry.split() for entry in allowlist)
