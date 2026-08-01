"""send_message —— Agent 之间直接说话（02-protocol §5）。

有了它，coder 写完可以直接 `@reviewer` 请人审，不必事事经过 coordinator：
中心编排负责「有人对用户负责」，点对点对话负责「像个团队」。

熔断不在这里做：消息由 `Envelope.reply()` 构造，hops 自动 +1，
超过 ttl_hops 时构造就会失败 —— A@B、B@A 的死循环止于协议层。
"""

from __future__ import annotations

from typing import Any, ClassVar

from anthill.agent.tools.base import BaseTool, ToolContext, ToolResult, string_param
from anthill.core.errors import AntHillError
from anthill.core.payloads import RiskLevel

VALID_KINDS = ("chat", "task")


class SendMessageTool(BaseTool):
    name = "send_message"
    description = (
        "给另一个 Agent 发消息。to 可以写 Agent 名（coder）、角色（role:reviewer）"
        "或 @名字；kind=chat 是聊天讨论，kind=task 是正式派活（对方会回交付结果）。"
    )
    risk = RiskLevel.LOW
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "to": string_param("收件人：coder / role:reviewer / @reviewer"),
            "body": string_param("消息正文，把对方需要的上下文写清楚"),
            "kind": {
                "type": "string",
                "enum": list(VALID_KINDS),
                "description": "chat=讨论，task=派活",
            },
        },
        "required": ["to", "body"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.messenger is None:
            return ToolResult.failed("当前 Agent 没有接入消息通道，无法发消息")

        to = normalize_recipient(str(args.get("to", "")))
        body = str(args.get("body", "")).strip()
        kind = str(args.get("kind") or "chat")
        if not to:
            return ToolResult.failed("to 不能为空")
        if not body:
            return ToolResult.failed("body 不能为空")
        if kind not in VALID_KINDS:
            return ToolResult.failed(f"kind 只能是 {VALID_KINDS}，收到 {kind!r}")

        try:
            msg_id = await ctx.messenger.send(to=to, body=body, kind=kind)
        except AntHillError as exc:
            # 熔断、收件人不存在等都走这里：让模型看到原因，自己换个做法
            return ToolResult.failed(f"发送给 {to} 失败：{exc}")
        return ToolResult.ok_result(f"已发给 {to}（消息 {msg_id[-6:]}）")


def normalize_recipient(raw: str) -> str:
    """`@reviewer` → `reviewer`。模型习惯像人一样写 @，路由层不认这个前缀。"""
    text = raw.strip()
    return text[1:] if text.startswith("@") else text
