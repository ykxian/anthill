"""HMAC-SHA256 签名与时间窗防重放（02-protocol §6）。

签名覆盖**整个信封去掉 sig 字段后的规范化字节串**（`Envelope.canonical_bytes()`），
所以收件人、类型、payload 任何一处被改动都会验签失败 ——
中间人不能把一条「读文件」改投成「跑 shell」。

防重放是两道：
1. `ts` 偏差超过 5 分钟直接拒（本模块）；
2. `id` 重复直接丢弃（seen.db 那一层，M0 就有了）。
只有第一道的话，攻击者可以在窗口内重放；只有第二道的话，seen.db 得永久保留。
两道合起来，seen.db 只需要保留一个时间窗的量。
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from hashlib import sha256

from anthill.core.envelope import Envelope
from anthill.core.errors import SignatureError
from anthill.core.ids import now

SIG_ALGO = "hmac-sha256"
SIG_PREFIX = f"{SIG_ALGO}:"
MAX_CLOCK_SKEW = timedelta(minutes=5)


def compute_signature(env: Envelope, key: bytes) -> str:
    digest = hmac.new(key, env.canonical_bytes(), sha256).hexdigest()
    return f"{SIG_PREFIX}{digest}"


def sign_envelope(env: Envelope, key: bytes) -> Envelope:
    """返回带签名的新信封。原信封不变（frozen 模型，本来也改不了）。"""
    return env.model_copy(update={"sig": compute_signature(env, key)})


def verify_envelope(
    env: Envelope,
    key: bytes,
    *,
    at: datetime | None = None,
    max_skew: timedelta | None = MAX_CLOCK_SKEW,
) -> None:
    """校验签名与时间窗。任一不过抛 SignatureError，绝不返回布尔值让调用方忘了判。

    `max_skew=None` 表示只验签名、不看时间。用于**已经躺在邮箱里**的信封：
    邮箱是存储转发队列，agentd 停机几小时再启动是正常的，
    那时按时间窗判就会把一堆合法消息误杀。这种场景的重放防护由 seen.db 承担。
    """
    if not env.sig:
        raise SignatureError(f"消息 {env.id} 未签名，但本通道要求签名")
    if not env.sig.startswith(SIG_PREFIX):
        algo = env.sig.split(":", 1)[0]
        raise SignatureError(f"不支持的签名算法 {algo!r}，本节点只认 {SIG_ALGO}")

    expected = compute_signature(env, key)
    if not hmac.compare_digest(env.sig, expected):  # 定时安全比较，别用 ==
        raise SignatureError(f"消息 {env.id} 签名不匹配：内容被改过，或用的不是同一把密钥")

    if max_skew is None:
        return
    skew = abs((at or now()) - env.ts)
    if skew > max_skew:
        raise SignatureError(
            f"消息 {env.id} 的时间戳偏差 {skew.total_seconds():.0f}s "
            f"超过 {max_skew.total_seconds():.0f}s，按重放拒收"
            "（也可能是两台机器时钟没对齐）"
        )
