"""全项目异常层级。

原则（CLAUDE 规则「错误处理」）：不静默吞错；每个异常都带足够定位问题的上下文。
"""

from __future__ import annotations


class AntHillError(Exception):
    """所有本项目异常的根。调用方可以只 catch 这一个。"""


class ConfigError(AntHillError):
    """node.toml 缺失、字段非法、引用了不存在的 provider 等。启动期 fail fast。"""


class ProtocolError(AntHillError):
    """信封/邮箱协议被违反。"""


class EnvelopeTooLarge(ProtocolError):
    """payload 超过 64KB。大文件应放 blackboard，信封里只传路径引用。"""


class HopLimitExceeded(ProtocolError):
    """hops 超过 ttl_hops —— @mention 死循环的熔断点（02-protocol §1）。"""


class UnknownRecipient(ProtocolError):
    """收件人解析不到任何具体 Agent。"""


class MailboxError(AntHillError):
    """邮箱目录不可用、tmp 与 new 不在同一文件系统等。"""


class DeliveryError(AntHillError):
    """一次投递尝试失败。可重试与否由 retryable 标记。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class PolicyDenied(AntHillError):
    """策略引擎拒绝了一次工具调用或一条消息。"""


class ProviderError(AntHillError):
    """LLM 调用失败：上游报错、返回不可解析、录制带用完等。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolError(AntHillError):
    """工具执行失败。区别于 ToolResult(ok=False)：这里指工具本身崩了。"""


class BudgetExceeded(AntHillError):
    """单次任务的步数或 token 预算耗尽（03-tech-design §4 双熔断）。"""


class PlanError(AntHillError):
    """计划生成失败：模型不给 JSON、schema 不合法、把活派给不存在的 Agent。"""


class OrchestrationError(AntHillError):
    """编排过程本身出错（状态损坏、找不到对应步骤等）。"""


class SignatureError(ProtocolError):
    """签名缺失/不匹配，或时间戳超出防重放窗口（02-protocol §6）。"""


class PeerError(AntHillError):
    """对端不存在、未信任，或指纹与首次信任时不一致（TOFU 告警）。"""
