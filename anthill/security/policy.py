"""工具风险 × 来源信任 → allow / require_confirm / deny（03-tech-design §6）。

这套矩阵是本项目安全模型的核心：一个 Agent 能不能删你的文件，
不只取决于它想干什么，还取决于**是谁让它干的**。
默认最保守的一条：来自未知节点的任何操作一律拒收。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from anthill.agent.tools.base import Confirmer
from anthill.core.config import SecuritySection
from anthill.core.envelope import Address
from anthill.core.payloads import RiskLevel

USER_ROLE = "user"


class TrustLevel(IntEnum):
    """数值越大越可信，便于写 `>=` 这样的门限比较。"""

    UNKNOWN = 0
    TRUSTED_PEER = 1
    LOCAL_AGENT = 2
    USER = 3


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "require_confirm"
    DENY = "deny"


def trust_of(
    source: Address,
    *,
    local_node: str,
    roles: dict[str, str],
    trusted_peers: set[str],
) -> TrustLevel:
    """判定一条消息来源的信任级。本机 user 角色 = 你本人。"""
    if source.node != local_node:
        return TrustLevel.TRUSTED_PEER if source.node in trusted_peers else TrustLevel.UNKNOWN
    return TrustLevel.USER if roles.get(source.agent) == USER_ROLE else TrustLevel.LOCAL_AGENT


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str = ""
    decision: Decision = Decision.ALLOW
    auto_allowed: bool = False
    """本要问人、因 unattended_allow 白名单放行 —— 调用方要为它响一声。"""


# 风险 → 「免确认执行所需的最低信任级」。够不到就要确认；来源未知则一律拒绝。
MIN_TRUST_TO_ALLOW: dict[RiskLevel, TrustLevel] = {
    RiskLevel.LOW: TrustLevel.TRUSTED_PEER,
    RiskLevel.MEDIUM: TrustLevel.LOCAL_AGENT,
}

RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
}
"""风险档位的全序。RiskLevel 是 StrEnum 没有大小 —— ②的白名单、③的
每 Agent 上限都要比档，比较一律走这张表，别在各处自造次序。"""

ALWAYS_CONFIRM = frozenset({RiskLevel.HIGH})
"""high 风险无论谁派的活都要人点头 —— 这是「远端 agent 能跑命令但危险命令要本人同意」的落点。"""


class PolicyEngine:
    def __init__(self, security: SecuritySection) -> None:
        self._security = security

    def decide(self, risk: RiskLevel, trust: TrustLevel) -> Decision:
        if trust is TrustLevel.UNKNOWN:
            return Decision.DENY
        if risk in ALWAYS_CONFIRM:
            # 无人值守模式（confirm_high_risk=false）：不等人，直接拒
            return Decision.CONFIRM if self._security.confirm_high_risk else Decision.DENY
        return Decision.ALLOW if trust >= MIN_TRUST_TO_ALLOW[risk] else Decision.CONFIRM

    async def authorize(
        self,
        *,
        tool: str,
        risk: RiskLevel,
        trust: TrustLevel,
        detail: str,
        confirm: Confirmer | None,
    ) -> Verdict:
        decision = self.decide(risk, trust)
        if decision is Decision.ALLOW:
            return Verdict(allowed=True, decision=decision)
        if decision is Decision.DENY:
            return Verdict(
                allowed=False,
                reason=f"策略拒绝：{tool}（风险 {risk}，来源信任 {trust.name}）",
                decision=decision,
            )
        if confirm is None:
            # 无人值守的有界放宽：只把「差个人点头」的档位转成放行。
            # high 到不了这里享受白名单 —— 就算有人绕过配置校验塞进来，
            # 这里再焊一道（红线的第二道保险）。人在场时不查表：问人永远更好。
            loosened = risk is not RiskLevel.HIGH and risk.value in self._security.unattended_allow
            if loosened:
                return Verdict(allowed=True, decision=Decision.ALLOW, auto_allowed=True)
            return Verdict(
                allowed=False,
                reason=(
                    f"{tool} 需要人工确认，但当前没有可确认的终端"
                    f"（风险 {risk}，来源信任 {trust.name}）"
                ),
                decision=Decision.DENY,
            )
        approved = await confirm(f"允许执行 {tool}（风险 {risk}）？\n  {detail}")
        if approved:
            return Verdict(allowed=True, decision=decision)
        return Verdict(allowed=False, reason=f"用户拒绝执行 {tool}", decision=Decision.DENY)
