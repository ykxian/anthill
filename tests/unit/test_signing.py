"""HMAC 签名与防重放（02-protocol §6，一致性清单用例 7）。

三种攻击必须全部被拒：篡改 payload、过期时间戳、重复 id。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from anthill.core.envelope import Address, Envelope
from anthill.core.errors import SignatureError
from anthill.core.ids import now
from anthill.core.payloads import MessageType, TaskRequestPayload
from anthill.security.keys import PairingToken, fingerprint, new_key
from anthill.security.signing import (
    MAX_CLOCK_SKEW,
    SIG_PREFIX,
    sign_envelope,
    verify_envelope,
)

KEY = b"0" * 32
OTHER_KEY = b"1" * 32


def make_env(**kwargs: object) -> Envelope:
    return Envelope(
        from_=Address(node="lab", agent="runner"),
        to=Address(node="laptop", agent="cli"),
        type=MessageType.TASK_REQUEST,
        payload=TaskRequestPayload(title="跑测试"),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------- 签名与校验 ----------


def test_signing_returns_a_new_envelope_and_leaves_the_original_alone() -> None:
    # Arrange
    env = make_env()

    # Act
    signed = sign_envelope(env, KEY)

    # Assert
    assert env.sig is None
    assert signed.sig is not None
    assert signed.sig.startswith(SIG_PREFIX)
    assert signed.id == env.id  # 还是同一条消息


def test_a_correctly_signed_envelope_verifies() -> None:
    signed = sign_envelope(make_env(), KEY)

    verify_envelope(signed, KEY)  # 不抛异常即通过


def test_signature_is_stable_for_the_same_envelope_and_key() -> None:
    env = make_env()

    assert sign_envelope(env, KEY).sig == sign_envelope(env, KEY).sig


# ---------- 用例 7：三种攻击 ----------


def test_tampered_payload_is_rejected() -> None:
    # Arrange：签完名之后偷偷改内容
    signed = sign_envelope(make_env(), KEY)
    tampered = signed.model_copy(update={"payload": TaskRequestPayload(title="rm -rf /")})

    # Act / Assert
    with pytest.raises(SignatureError, match="签名"):
        verify_envelope(tampered, KEY)


def test_tampered_recipient_is_rejected() -> None:
    """收件人也在签名范围内，否则中间人可以把消息改投给别的 Agent。"""
    signed = sign_envelope(make_env(), KEY)
    redirected = signed.model_copy(update={"to": Address(node="laptop", agent="coder")})

    with pytest.raises(SignatureError):
        verify_envelope(redirected, KEY)


def test_wrong_key_is_rejected() -> None:
    signed = sign_envelope(make_env(), KEY)

    with pytest.raises(SignatureError):
        verify_envelope(signed, OTHER_KEY)


def test_stale_timestamp_is_rejected_as_replay() -> None:
    # Arrange：一小时前的消息，即使签名完全正确也不收
    old = make_env(ts=now() - timedelta(hours=1))
    signed = sign_envelope(old, KEY)

    # Act / Assert
    with pytest.raises(SignatureError, match="时间"):
        verify_envelope(signed, KEY)


def test_timestamp_from_the_future_is_rejected_too() -> None:
    """时钟快的机器也要拦：否则攻击者可以拿一条「未来的消息」长期重放。"""
    future = make_env(ts=now() + MAX_CLOCK_SKEW + timedelta(minutes=5))
    signed = sign_envelope(future, KEY)

    with pytest.raises(SignatureError, match="时间"):
        verify_envelope(signed, KEY)


def test_timestamp_within_the_window_is_accepted() -> None:
    recent = make_env(ts=now() - MAX_CLOCK_SKEW + timedelta(seconds=30))
    signed = sign_envelope(recent, KEY)

    verify_envelope(signed, KEY)


def test_unsigned_envelope_is_rejected_when_a_signature_is_required() -> None:
    with pytest.raises(SignatureError, match="未签名"):
        verify_envelope(make_env(), KEY)


def test_unknown_signature_algorithm_is_rejected() -> None:
    forged = make_env().model_copy(update={"sig": "md5:whatever"})

    with pytest.raises(SignatureError, match="算法"):
        verify_envelope(forged, KEY)


# ---------- 密钥与配对 ----------


def test_new_key_is_long_enough_and_random() -> None:
    assert len(new_key()) >= 32
    assert new_key() != new_key()


def test_fingerprint_is_stable_short_and_hides_the_key() -> None:
    printed = fingerprint(KEY)

    assert printed == fingerprint(KEY)
    assert printed != fingerprint(OTHER_KEY)
    assert len(printed) <= 32
    assert "0" * 32 not in printed  # 指纹不能把密钥本身印出来


def test_pairing_token_roundtrips() -> None:
    # Arrange
    token = PairingToken(node="lab", endpoint="http://10.0.8.21:45778", key=KEY)

    # Act
    decoded = PairingToken.decode(token.encode())

    # Assert
    assert decoded.node == "lab"
    assert decoded.endpoint == "http://10.0.8.21:45778"
    assert decoded.key == KEY
    assert decoded.fingerprint == fingerprint(KEY)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj"])
def test_broken_pairing_token_fails_with_a_clear_message(bad: str) -> None:
    with pytest.raises(ValueError, match="配对"):
        PairingToken.decode(bad)


def test_pairing_token_survives_being_copied_out_of_a_wrapped_terminal() -> None:
    """终端会把长令牌折行，用户复制过来就带着换行 —— 解析必须容忍。"""
    token = PairingToken(node="lab", endpoint="http://10.0.8.21:45778", key=KEY)
    encoded = token.encode()
    wrapped = "\n".join(encoded[i : i + 40] for i in range(0, len(encoded), 40))

    assert PairingToken.decode(wrapped).key == KEY
