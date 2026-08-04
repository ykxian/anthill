"""PIN 码配对：用 PAKE 换密钥，**密钥本身从不出现在网线上**。

原来的做法是把含密钥明文的令牌带外拷给对方 —— 安全，但一百多个字符，
而且"带外"意味着你得先有一条已经信任的通道。PIN 码的诱惑在于短。

可短口令有个致命前提：**直接把 PIN 加密的密钥发过去等于没加密**。
抓包的人拿到密文，六位数字离线暴破是秒级的事。想用短口令，就必须用
PAKE —— 双方各拿 PIN 算一条消息交换，密钥是**各自推导**出来的：

- 被动抓包的人一无所获（线路上没有密钥，也没有可离线暴破的密文）
- 主动猜 PIN 的人，每开一次窗口只有**一次**机会，猜错窗口就关

这里用 spake2（magic-wormhole 那套）。PIN 对不上时 SPAKE2 不会报错，
只是两边各得到一把不同的钥匙 —— 所以还要一轮**密钥确认**，
否则会配成"看起来成功了、之后每条消息都验签失败"这种最难查的状态。

    A 机  anthill peers pair              → 显示 PIN，开一个 2 分钟的窗口
    B 机  anthill peers pair --to A --pin  → 一次往返换完，两边各自落库
"""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.errors import PeerError
from anthill.core.ids import now

PAIRING_FILE = "pairing.json"
PAIRING_MODE = 0o600
PIN_DIGITS = 6
WINDOW_SECONDS = 120.0

IDENTITY = b"anthill-pairing-v1"
CONFIRM_HOST = b"anthill-pair-confirm/host"
CONFIRM_JOINER = b"anthill-pair-confirm/joiner"


def new_pin() -> str:
    """六位数字。够短到能念给对方听，而 PAKE 让它不需要更长。"""
    return f"{secrets.randbelow(10**PIN_DIGITS):0{PIN_DIGITS}d}"


def confirm_tag(key: bytes, label: bytes) -> str:
    """密钥确认标签。PIN 对不上时两边的 key 不同，标签自然对不上。"""
    return hmac.new(key, label, sha256).hexdigest()


def tags_match(given: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(given, expected)
    except TypeError:  # 非 ASCII 的 str 会抛，别让它变成 500
        return False


def exchange(pin: str) -> tuple[object, bytes]:
    """开始一次 SPAKE2 交换，返回 (状态, 要发给对方的消息)。"""
    from spake2 import SPAKE2_Symmetric

    state = SPAKE2_Symmetric(pin.encode(), idSymmetric=IDENTITY)
    return state, bytes(state.start())


def derive(state: object, inbound: bytes) -> bytes:
    """收到对方的消息后推导共享密钥。**这一步没有网络往来**。"""
    from spake2 import SPAKEError

    try:
        return bytes(state.finish(inbound))  # type: ignore[attr-defined]
    except (SPAKEError, ValueError) as exc:
        raise PeerError(f"配对消息不合法：{exc}") from exc


@dataclass(frozen=True, slots=True)
class PairingWindow:
    """一个开着的配对窗口。**用过一次就作废**，这是限制在线猜测的关键。"""

    pin: str
    opened_at: str
    used: bool = False
    peer_node: str = ""
    peer_endpoint: str = ""
    peer_key: str = ""

    @property
    def expired(self) -> bool:
        try:
            age = (now() - now().fromisoformat(self.opened_at)).total_seconds()
        except (ValueError, TypeError):
            return True
        return not 0 <= age <= WINDOW_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "pin": self.pin,
            "opened_at": self.opened_at,
            "used": self.used,
            "peer_node": self.peer_node,
            "peer_endpoint": self.peer_endpoint,
            "peer_key": self.peer_key,
        }


class PairingStore:
    """配对窗口落在文件里，因为**开窗口的和处理请求的不是同一个进程** ——
    `anthill peers pair` 是个 CLI，而 `/pair` 由 serve 处理。

    和 peers.json 一样是 0600：窗口里存着 PIN，短时间内它等价于密钥。
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / PAIRING_FILE

    @property
    def path(self) -> Path:
        return self._path

    def open(self, pin: str) -> PairingWindow:
        window = PairingWindow(pin=pin, opened_at=now().isoformat())
        self._write(window)
        return window

    def current(self) -> PairingWindow | None:
        """当前有效窗口；过期或读不出来一律当作没有。"""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            window = PairingWindow(
                pin=str(data["pin"]),
                opened_at=str(data.get("opened_at", "")),
                used=bool(data.get("used", False)),
                peer_node=str(data.get("peer_node", "")),
                peer_endpoint=str(data.get("peer_endpoint", "")),
                peer_key=str(data.get("peer_key", "")),
            )
        except (KeyError, TypeError):
            return None
        if window.expired:
            self.close()
            return None
        return window

    def hold(self, window: PairingWindow, *, node: str, endpoint: str, key: bytes) -> None:
        """记下这一轮的结果，等对方确认。**同时把窗口标成用过** ——
        一个窗口只允许一次尝试，否则六位 PIN 会被在线穷举。"""
        self._write(
            PairingWindow(
                pin=window.pin,
                opened_at=window.opened_at,
                used=True,
                peer_node=node,
                peer_endpoint=endpoint,
                peer_key=key.hex(),
            )
        )

    def close(self) -> None:
        self._path.unlink(missing_ok=True)

    def _write(self, window: PairingWindow) -> None:
        path = atomic_write(
            self._root, self._root, PAIRING_FILE, json.dumps(window.as_dict()).encode()
        )
        path.chmod(PAIRING_MODE)
