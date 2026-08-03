"""对端共享密钥、指纹与配对令牌。

HMAC 是对称的，所以两个节点必须**先带外交换一个共享密钥**才能互认。
流程借鉴 SSH 的 known_hosts：

    A 机：anthill peers invite laptop   →  打印一串配对令牌（含密钥）
    B 机：anthill peers trust --token <令牌>

指纹是密钥的哈希，用来在两台机器上肉眼核对「我们说的是同一把钥匙」，
本身不泄露密钥（TOFU：首次信任记下指纹，之后指纹变了立刻拒收并告警）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass

KEY_BYTES = 32
FINGERPRINT_CHARS = 16
TOKEN_VERSION = 1


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def fingerprint(key: bytes) -> str:
    """`ab:cd:ef:…` 形式的短指纹，方便两台机器上肉眼比对。"""
    digest = hashlib.sha256(b"anthill-peer-key\x00" + key).hexdigest()[:FINGERPRINT_CHARS]
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode()


def decode_key(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text.encode())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"密钥不是合法 base64：{exc}") from exc


@dataclass(frozen=True, slots=True)
class PairingToken:
    """一次性配对信息：对方是谁、怎么连、用哪把钥匙。

    **令牌里含密钥明文**，所以只该走你已经信任的通道传递（当面拷、私聊、SSH）。
    这一点在 CLI 的输出里也会明确提示。
    """

    node: str
    endpoint: str
    key: bytes

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.key)

    def encode(self) -> str:
        payload = {
            "v": TOKEN_VERSION,
            "node": self.node,
            "endpoint": self.endpoint,
            "key": encode_key(self.key),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, text: str) -> PairingToken:
        # 令牌是从终端里复制粘贴过来的，中间很可能夹着换行或空格，先全清掉
        compact = "".join(text.split())
        try:
            raw = base64.urlsafe_b64decode(compact.encode())
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"配对令牌无法解析：{exc}") from exc
        if not isinstance(data, dict) or data.get("v") != TOKEN_VERSION:
            raise ValueError(
                f"配对令牌版本不支持：{data if isinstance(data, dict) else type(data)}"
            )
        for field in ("node", "endpoint", "key"):
            if not data.get(field):
                raise ValueError(f"配对令牌缺少字段 {field}")
        return cls(
            node=str(data["node"]),
            endpoint=str(data["endpoint"]),
            key=decode_key(str(data["key"])),
        )
