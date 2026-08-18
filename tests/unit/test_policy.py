"""策略引擎：工具风险 × 来源信任 → allow / require_confirm / deny。"""

from __future__ import annotations

import pytest

from anthill.core.config import SecuritySection
from anthill.core.envelope import Address
from anthill.core.payloads import RiskLevel
from anthill.security.policy import (
    Decision,
    PolicyEngine,
    TrustLevel,
    trust_of,
)


def make_engine(**kwargs: object) -> PolicyEngine:
    return PolicyEngine(SecuritySection(**kwargs))  # type: ignore[arg-type]


# ---------- 信任级判定 ----------


def test_local_user_agent_is_most_trusted() -> None:
    level = trust_of(
        Address(node="me", agent="cli"), local_node="me", roles={"cli": "user"}, trusted_peers=set()
    )

    assert level is TrustLevel.USER


def test_local_worker_agent_is_local_trust() -> None:
    level = trust_of(
        Address(node="me", agent="coder"),
        local_node="me",
        roles={"coder": "worker"},
        trusted_peers=set(),
    )

    assert level is TrustLevel.LOCAL_AGENT


def test_trusted_remote_peer_is_peer_trust() -> None:
    level = trust_of(
        Address(node="lab", agent="runner"),
        local_node="me",
        roles={},
        trusted_peers={"lab"},
    )

    assert level is TrustLevel.TRUSTED_PEER


def test_unknown_remote_node_is_untrusted() -> None:
    level = trust_of(
        Address(node="stranger", agent="x"), local_node="me", roles={}, trusted_peers=set()
    )

    assert level is TrustLevel.UNKNOWN


# ---------- 决策矩阵 ----------


@pytest.mark.parametrize(
    ("risk", "trust", "expected"),
    [
        (RiskLevel.LOW, TrustLevel.USER, Decision.ALLOW),
        (RiskLevel.LOW, TrustLevel.TRUSTED_PEER, Decision.ALLOW),
        (RiskLevel.LOW, TrustLevel.UNKNOWN, Decision.DENY),
        (RiskLevel.MEDIUM, TrustLevel.USER, Decision.ALLOW),
        (RiskLevel.MEDIUM, TrustLevel.LOCAL_AGENT, Decision.ALLOW),
        (RiskLevel.MEDIUM, TrustLevel.TRUSTED_PEER, Decision.CONFIRM),
        (RiskLevel.HIGH, TrustLevel.USER, Decision.CONFIRM),
        (RiskLevel.HIGH, TrustLevel.LOCAL_AGENT, Decision.CONFIRM),
        (RiskLevel.HIGH, TrustLevel.TRUSTED_PEER, Decision.CONFIRM),
        (RiskLevel.HIGH, TrustLevel.UNKNOWN, Decision.DENY),
    ],
)
def test_decision_matrix(risk: RiskLevel, trust: TrustLevel, expected: Decision) -> None:
    assert make_engine().decide(risk, trust) is expected


def test_unattended_mode_turns_high_risk_confirmation_into_denial() -> None:
    engine = make_engine(confirm_high_risk=False)

    assert engine.decide(RiskLevel.HIGH, TrustLevel.USER) is Decision.DENY


# ---------- 确认流 ----------


async def test_authorize_allows_low_risk_without_asking() -> None:
    # Arrange
    asked: list[str] = []
    engine = make_engine()

    async def confirmer(prompt: str) -> bool:
        asked.append(prompt)
        return True

    # Act
    verdict = await engine.authorize(
        tool="read_file",
        risk=RiskLevel.LOW,
        trust=TrustLevel.USER,
        detail="a.py",
        confirm=confirmer,
    )

    # Assert
    assert verdict.allowed
    assert asked == []


async def test_authorize_asks_and_honours_yes_for_high_risk() -> None:
    engine = make_engine()

    async def confirmer(prompt: str) -> bool:
        assert "run_shell" in prompt
        return True

    verdict = await engine.authorize(
        tool="run_shell",
        risk=RiskLevel.HIGH,
        trust=TrustLevel.USER,
        detail="rm -rf build",
        confirm=confirmer,
    )

    assert verdict.allowed


async def test_authorize_denies_when_user_says_no() -> None:
    engine = make_engine()

    async def confirmer(prompt: str) -> bool:
        return False

    verdict = await engine.authorize(
        tool="run_shell",
        risk=RiskLevel.HIGH,
        trust=TrustLevel.USER,
        detail="rm -rf build",
        confirm=confirmer,
    )

    assert not verdict.allowed
    assert "拒绝" in verdict.reason


async def test_authorize_denies_when_nobody_can_confirm() -> None:
    # 无人值守（agentd 不在终端里跑）时不能默认放行
    engine = make_engine()

    verdict = await engine.authorize(
        tool="run_shell",
        risk=RiskLevel.HIGH,
        trust=TrustLevel.LOCAL_AGENT,
        detail="rm -rf build",
        confirm=None,
    )

    assert not verdict.allowed
    assert "确认" in verdict.reason


# ---------- 无人值守白名单（unattended_allow）----------


async def test_low_risk_is_matrix_allowed_and_takes_no_allowlist_credit() -> None:
    """LOW × TRUSTED_PEER 在矩阵里本来就是放行 —— 白名单没出过力，
    auto_allowed 必须是 False：这个标志只给真实的放宽记账。"""
    engine = make_engine(unattended_allow=("low",))

    verdict = await engine.authorize(
        tool="read_file",
        risk=RiskLevel.LOW,
        trust=TrustLevel.TRUSTED_PEER,
        detail="看一眼 README",
        confirm=None,
    )

    assert verdict.allowed
    assert not verdict.auto_allowed


async def test_unattended_allow_does_not_cover_unlisted_risk() -> None:
    engine = make_engine(unattended_allow=("low",))

    verdict = await engine.authorize(
        tool="write_file",
        risk=RiskLevel.MEDIUM,
        trust=TrustLevel.TRUSTED_PEER,
        detail="改配置",
        confirm=None,
    )

    assert not verdict.allowed


async def test_unattended_allow_medium_covers_the_remote_peer_case() -> None:
    engine = make_engine(unattended_allow=("low", "medium"))

    verdict = await engine.authorize(
        tool="write_file",
        risk=RiskLevel.MEDIUM,
        trust=TrustLevel.TRUSTED_PEER,
        detail="改配置",
        confirm=None,
    )

    assert verdict.allowed
    assert verdict.auto_allowed


async def test_high_risk_never_passes_the_allowlist() -> None:
    """红线的第二道保险：config 校验已经拒收 "high"，但就算有人绕过校验
    把它塞进来（model_construct），策略层自己也要顶住。"""
    from anthill.core.config import SecuritySection

    rigged = SecuritySection.model_construct(
        **{**SecuritySection().model_dump(), "unattended_allow": ("low", "medium", "high")}
    )
    engine = PolicyEngine(rigged)

    verdict = await engine.authorize(
        tool="run_shell",
        risk=RiskLevel.HIGH,
        trust=TrustLevel.USER,
        detail="rm -rf build",
        confirm=None,
    )

    assert not verdict.allowed


async def test_a_person_present_is_still_asked_even_for_listed_risk() -> None:
    """放宽只针对「没人可问」的通道 —— 人在场永远问人。"""
    asked: list[str] = []

    async def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    engine = make_engine(unattended_allow=("medium",))

    verdict = await engine.authorize(
        tool="write_file",
        risk=RiskLevel.MEDIUM,
        trust=TrustLevel.TRUSTED_PEER,
        detail="改配置",
        confirm=confirm,
    )

    assert verdict.allowed
    assert asked, "有确认通道时必须真的问人"
    assert not verdict.auto_allowed
