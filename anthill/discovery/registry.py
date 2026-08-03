"""peers 列表与 TOFU 信任（01-architecture §5.3）。

**发现 ≠ 可通信**：广播里看到一个节点，只是把它记进列表（`discovered`），
必须显式 `anthill peers trust` 交换密钥之后才能互投消息。

TOFU（Trust On First Use，抄 SSH known_hosts 的作业）：首次信任记下密钥指纹；
之后同名节点拿着不同的钥匙来，一律拒绝并告警 —— 可能是对方重装了，
也可能是有人在冒充，这个判断必须由人来做，不能自动放行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.atomic import atomic_write
from anthill.core.errors import PeerError
from anthill.core.ids import now
from anthill.security.keys import PairingToken, decode_key, encode_key, fingerprint

PEERS_FILE = "peers.json"
FILE_MODE = 0o600


class PeerRecord(BaseModel):
    """对端的**非密**信息。密钥单独存，不进这个模型，免得随手打印就泄了。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    endpoint: str = ""
    agents: tuple[str, ...] = ()
    trusted: bool = False
    fingerprint: str = ""
    first_seen: str = Field(default="")
    last_seen: str = Field(default="")

    @property
    def status(self) -> str:
        return "trusted" if self.trusted else "discovered"


class PeerRegistry:
    """`.anthill/peers.json` 的读写。文件权限 0600 —— 里面有共享密钥明文。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._peers: dict[str, PeerRecord] = {}
        self._keys: dict[str, bytes] = {}
        self._mtime: float | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._root / PEERS_FILE

    # ---------- 查询 ----------

    def get(self, node: str) -> PeerRecord | None:
        self._refresh()
        return self._peers.get(node)

    def all(self) -> list[PeerRecord]:
        self._refresh()
        return [self._peers[name] for name in sorted(self._peers)]

    def key_for(self, node: str) -> bytes | None:
        self._refresh()
        return self._keys.get(node)

    def require_trusted(self, node: str) -> tuple[PeerRecord, bytes]:
        """取出「可以真的往那儿发消息」所需的一切。不满足条件就抛，不返回 None。"""
        self._refresh()
        peer = self._peers.get(node)
        if peer is None:
            raise PeerError(
                f"未知节点 {node!r}；先让对方跑 `anthill peers invite <你的节点名>`，"
                f"再在这里 `anthill peers trust --token <令牌>`"
            )
        key = self._keys.get(node)
        if not peer.trusted or key is None:
            raise PeerError(
                f"节点 {node!r} 只是被发现过，尚未信任 —— 发现 ≠ 可通信。"
                f"核对指纹后执行 `anthill peers trust --token <令牌>`"
            )
        return peer, key

    # ---------- 变更 ----------

    def observe(self, *, node: str, endpoint: str, agents: tuple[str, ...]) -> PeerRecord:
        """收到一条 announce。只更新「见过」的事实，**绝不影响信任状态**。"""
        stamp = now().isoformat()
        existing = self._peers.get(node)
        record = PeerRecord(
            node=node,
            endpoint=endpoint or (existing.endpoint if existing else ""),
            agents=agents or (existing.agents if existing else ()),
            trusted=existing.trusted if existing else False,
            fingerprint=existing.fingerprint if existing else "",
            first_seen=existing.first_seen if existing else stamp,
            last_seen=stamp,
        )
        self._peers = {**self._peers, node: record}
        self._save()
        return record

    def trust(self, token: PairingToken, *, replace: bool = False) -> PeerRecord:
        """用配对令牌信任一个节点。指纹与上次不一致时拒绝，除非显式 replace。"""
        print_ = fingerprint(token.key)
        existing = self._peers.get(token.node)
        if existing and existing.trusted and existing.fingerprint != print_ and not replace:
            raise PeerError(
                f"节点 {token.node!r} 的密钥指纹变了：\n"
                f"  之前 {existing.fingerprint}\n"
                f"  现在 {print_}\n"
                "可能是对方重装了，也可能有人在冒充。确认无误后加 --replace 覆盖。"
            )

        stamp = now().isoformat()
        record = PeerRecord(
            node=token.node,
            endpoint=token.endpoint or (existing.endpoint if existing else ""),
            agents=existing.agents if existing else (),
            trusted=True,
            fingerprint=print_,
            first_seen=existing.first_seen if existing else stamp,
            last_seen=stamp,
        )
        self._peers = {**self._peers, token.node: record}
        self._keys = {**self._keys, token.node: token.key}
        self._save()
        return record

    def forget(self, node: str) -> bool:
        if node not in self._peers:
            return False
        self._peers = {k: v for k, v in self._peers.items() if k != node}
        self._keys = {k: v for k, v in self._keys.items() if k != node}
        self._save()
        return True

    # ---------- 落盘 ----------

    def _refresh(self) -> None:
        """文件被别的进程改过就重新读。

        一个节点上会同时跑好几个进程（每个 agentd 一个，加一个 serve），
        `serve` 学到的对端地址必须让 agentd 看得见，否则回信路由是断的 ——
        这是联调时真的踩到的坑。文件是唯一真相，内存只是缓存。
        """
        if self._mtime is not None and self._current_mtime() == self._mtime:
            return
        self._peers = {}
        self._keys = {}
        self._load()

    def _current_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime_ns / 1e9
        except OSError:
            return None

    def _load(self) -> None:
        self._mtime = self._current_mtime()
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data["peers"] if isinstance(data, dict) else data
            for item in entries:
                record = PeerRecord.model_validate({k: v for k, v in item.items() if k != "key"})
                self._peers[record.node] = record
                if item.get("key"):
                    self._keys[record.node] = decode_key(str(item["key"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PeerError(f"peers 列表 {self.path} 已损坏：{exc}") from exc

    def _save(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        for name in sorted(self._peers):
            item = self._peers[name].model_dump(mode="json")
            key = self._keys.get(name)
            if key is not None:
                item["key"] = encode_key(key)
            entries.append(item)
        payload = json.dumps({"peers": entries}, ensure_ascii=False, indent=2).encode()
        atomic_write(self._root, self._root, PEERS_FILE, payload)
        self._mtime = self._current_mtime()  # 自己写的不必再重载
        # 先写后收权限会有一瞬间的宽窗口，但 tmp 文件也在同一个 0700 的 .anthill 下，
        # 且这一步紧跟 rename，实际风险可接受；换成 fchmod 需要改动原子写协议。
        os.chmod(self.path, FILE_MODE)
