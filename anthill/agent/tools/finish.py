"""finish —— 强制结构化收尾。

没有它，Agent 常常「聊了一堆但没有可机读结果」；有了它，
task.result 的 summary/artifacts/status 一定来自模型的明确交付动作。
"""

from __future__ import annotations

from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.payloads import RiskLevel

VALID_STATUS = ("ok", "partial")


class FinishTool(BaseTool):
    name = "finish"
    description = (
        "完成任务并交付结果。必须在任务做完时调用一次，"
        "summary 写给派活的人看，artifacts 列出你产出/修改的文件路径。"
    )
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "summary": string_param("交付说明：做了什么、结论是什么"),
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "产出文件的相对路径列表",
            },
            "status": {
                "type": "string",
                "enum": list(VALID_STATUS),
                "description": "ok 表示完全达成；partial 表示部分达成，需在 summary 说明缺口",
            },
        },
        "required": ["summary"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        if not summary:
            return ToolResult.failed("summary 不能为空：请说明你完成了什么")

        raw_artifacts = args.get("artifacts") or ()
        if isinstance(raw_artifacts, str):
            raw_artifacts = [raw_artifacts]
        artifacts = tuple(str(a) for a in raw_artifacts if str(a).strip())

        status = str(args.get("status") or "ok")
        if status not in VALID_STATUS:
            return ToolResult.failed(f"status 只能是 {VALID_STATUS}，收到 {status!r}")

        return ToolResult(
            ok=True,
            content=summary,
            artifacts=artifacts,
            is_finish=True,
            status=status,
        )
