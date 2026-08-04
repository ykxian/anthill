"""跨机传输：SFTP 直接把信封写进远端邮箱（01-architecture §5.2）。

选 SFTP 直写而不是在服务器上自建一个 TCP 服务，是本项目最重要的取舍之一：

- **服务器不用开任何新端口**，也不用配证书、不用穿透 —— 复用你已经有的 SSH 信任通道；
- 学校/公司的服务器往往只开 22 端口，这条路是唯一能走通的；
- SFTP 原生支持 rename，所以 `tmp→rename` 原子写协议原样适用，跨机与同机语义完全一致。

连接是复用的（一台机器一条），断了自动重连一次 —— SSH 握手比投递本身贵得多。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import asyncssh

from anthill.core.config import PeerSection
from anthill.core.envelope import Envelope, TransportKind
from anthill.core.errors import DeliveryError
from anthill.core.logging import EventLog
from anthill.core.paths import ANTHILL_DIR
from anthill.discovery.registry import PeerRegistry
from anthill.security.signing import sign_envelope
from anthill.transport.base import DeliveryResult, Destination, Transport

DEFAULT_SSH_PORT = 22
CONNECT_TIMEOUT = 20.0
PART_SUFFIX = ".part"


@dataclass(frozen=True, slots=True)
class SshTarget:
    """连一台远端机器需要知道的全部信息。"""

    host: str
    remote_workspace: str
    port: int = DEFAULT_SSH_PORT
    user: str | None = None
    identity_file: str | None = None
    known_hosts: str | None = None

    @classmethod
    def from_peer(cls, node: str, peer: PeerSection | None) -> SshTarget:
        if peer is None or not peer.host or not peer.remote_workspace:
            raise DeliveryError(
                f"节点 {node} 缺少 ssh 配置；node.toml 里需要 host 与 remote_workspace",
                retryable=False,
            )
        return cls(
            host=peer.host,
            remote_workspace=peer.remote_workspace,
            port=peer.port,
            user=peer.user,
            identity_file=peer.identity_file,
            known_hosts=peer.known_hosts,
        )

    def options(self) -> dict[str, Any]:
        """交给 asyncssh 的连接参数。

        只走密钥认证，不传密码；`known_hosts` 留空时 asyncssh 用 ~/.ssh/known_hosts ——
        **主机指纹校验永远不会被跳过**，这是 SSH 这条路的安全前提，不给关掉的开关。
        """
        opts: dict[str, Any] = {"port": self.port}
        if self.user:
            opts["username"] = self.user
        if self.identity_file:
            opts["client_keys"] = [self.identity_file]
        if self.known_hosts:
            opts["known_hosts"] = self.known_hosts
        return opts


Connector = Callable[[SshTarget], Awaitable[Any]]
"""建连函数。抽出来是为了测试能指向一个进程内的 SSH 服务端，跑真的 SFTP。"""


async def default_connect(target: SshTarget) -> Any:
    return await asyncio.wait_for(
        asyncssh.connect(target.host, **target.options()), timeout=CONNECT_TIMEOUT
    )


class SshTransport(Transport):
    kind = TransportKind.SSH

    def __init__(
        self,
        *,
        node_name: str,
        log: EventLog,
        peers: PeerRegistry | None = None,
        connect: Connector = default_connect,
    ) -> None:
        self._node = node_name
        self._log = log
        self._peers = peers
        self._connect = connect
        self._conns: dict[str, Any] = {}

    # ---------- 投递 ----------

    async def deliver(self, env: Envelope, dest: Destination) -> DeliveryResult:
        target = SshTarget.from_peer(dest.node, dest.peer)
        signed = self._sign(env, dest.node)
        try:
            path = await self._put(signed, dest, target)
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            # 网络与远端故障都算可重试：交给 outbox 退避，别急着进死信
            self._drop(dest.node)
            return DeliveryResult.failure(self.kind, dest, f"{type(exc).__name__}: {exc}")
        self._log.info("ssh.delivered", msg=signed.id, to=str(dest), path=path)
        return DeliveryResult.success(self.kind, dest, path)

    def _sign(self, env: Envelope, node: str) -> Envelope:
        """有共享密钥就签名。

        SSH 通道本身已经是加密可信的，签名防的是另一件事：
        **远端多用户环境下，同机器上的其他账号往你的邮箱里塞文件**。
        """
        key = self._peers.key_for(node) if self._peers else None
        return sign_envelope(env, key) if key else env

    async def _put(self, env: Envelope, dest: Destination, target: SshTarget) -> str:
        conn = await self._connection(dest.node, target)
        async with conn.start_sftp_client() as sftp:
            inbox = await self._remote_inbox(sftp, target, dest.agent)
            tmp = f"{inbox}/tmp/{env.id}.json{PART_SUFFIX}"
            final = f"{inbox}/new/{env.id}.json"
            for sub in ("tmp", "new"):
                await sftp.makedirs(f"{inbox}/{sub}", exist_ok=True)
            async with sftp.open(tmp, "wb") as handle:
                await handle.write(env.to_json_bytes())
            await _atomic_rename(sftp, tmp, final)
            return final

    @staticmethod
    async def _remote_inbox(sftp: Any, target: SshTarget, agent: str) -> str:
        workspace = target.remote_workspace
        if workspace.startswith("~"):
            # SFTP 不认 ~，得先问远端「我的起始目录在哪」
            home = await sftp.realpath(".")
            workspace = str(PurePosixPath(home) / workspace.lstrip("~/"))
        return str(PurePosixPath(workspace) / ANTHILL_DIR / "agents" / agent / "mailbox" / "inbox")

    # ---------- 拉取远端产物 ----------

    async def fetch(self, node: str, peer: PeerSection | None, remote: str, local: Any) -> int:
        """把远端的一个文件拉到本地，返回字节数。

        result 里只带产物**路径**（消息体上限 64KB），要看内容就按需拉 ——
        这也避免了把几十兆的日志塞进消息链路。
        """
        data = await self.read_bytes(node, peer, remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        self._log.info("ssh.fetched", node=node, remote=remote, bytes=len(data))
        return len(data)

    async def read_bytes(
        self, node: str, peer: PeerSection | None, remote: str, *, max_bytes: int | None = None
    ) -> bytes:
        """读远端小文件。`max_bytes` 是给「文件由对方决定大小」的场景用的上限 ——
        不设上限的话，对面放一个几个 G 的文件就能把这边撑爆。"""
        target = SshTarget.from_peer(node, peer)
        conn = await self._connection(node, target)
        async with conn.start_sftp_client() as sftp:
            path = await self._resolve(sftp, target, remote)
            async with sftp.open(path, "rb") as handle:
                data = await (handle.read() if max_bytes is None else handle.read(max_bytes + 1))
        raw = bytes(data) if isinstance(data, bytes | bytearray) else str(data).encode()
        if max_bytes is not None and len(raw) > max_bytes:
            raise DeliveryError(
                f"{node} 的 {remote} 超过 {max_bytes} 字节上限，拒绝读取", retryable=False
            )
        return raw

    async def write_bytes(
        self, node: str, peer: PeerSection | None, remote: str, data: bytes
    ) -> str:
        """写远端小文件（审批答复用）。同样走 tmp→rename，别让对方读到半个文件。"""
        target = SshTarget.from_peer(node, peer)
        conn = await self._connection(node, target)
        async with conn.start_sftp_client() as sftp:
            path = await self._resolve(sftp, target, remote)
            parent = str(PurePosixPath(path).parent)
            await sftp.makedirs(parent, exist_ok=True)
            tmp = f"{path}{PART_SUFFIX}"
            async with sftp.open(tmp, "wb") as handle:
                await handle.write(data)
            await _atomic_rename(sftp, tmp, path)
        return path

    async def listdir(self, node: str, peer: PeerSection | None, remote: str) -> list[str]:
        target = SshTarget.from_peer(node, peer)
        conn = await self._connection(node, target)
        async with conn.start_sftp_client() as sftp:
            path = await self._resolve(sftp, target, remote)
            return sorted(n for n in await sftp.listdir(path) if n not in (".", ".."))

    async def remove(self, node: str, peer: PeerSection | None, remote: str) -> None:
        target = SshTarget.from_peer(node, peer)
        conn = await self._connection(node, target)
        async with conn.start_sftp_client() as sftp:
            await sftp.remove(await self._resolve(sftp, target, remote))

    async def _resolve(self, sftp: Any, target: SshTarget, remote: str) -> str:
        """相对路径一律相对 remote_workspace 解析，绝对路径原样用。"""
        if remote.startswith("/"):
            return remote
        workspace = target.remote_workspace
        if workspace.startswith("~"):
            home = await sftp.realpath(".")
            workspace = str(PurePosixPath(home) / workspace.lstrip("~/"))
        return str(PurePosixPath(workspace) / remote)

    # ---------- 连接复用 ----------

    async def _connection(self, node: str, target: SshTarget) -> Any:
        conn = self._conns.get(node)
        if conn is not None and not _is_closed(conn):
            return conn
        self._conns.pop(node, None)
        try:
            conn = await self._connect(target)
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise DeliveryError(
                f"连不上 {target.user or ''}@{target.host}:{target.port}："
                f"{type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        self._conns[node] = conn
        self._log.info("ssh.connected", node=node, host=target.host, port=target.port)
        return conn

    def _drop(self, node: str) -> None:
        """连接可能已经废了，扔掉让下次重连 —— 半死不活的连接比没有连接更糟。"""
        conn = self._conns.pop(node, None)
        if conn is not None:
            conn.close()

    async def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()


async def _atomic_rename(sftp: Any, src: str, dst: str) -> None:
    """优先用 posix-rename 扩展（目标已存在也能覆盖），退化到普通 rename。

    重复投递同一条消息时目标是存在的（文件名就是 ULID），
    普通 rename 在部分服务端会直接失败 —— 那会让重试永远成功不了。
    """
    posix_rename = getattr(sftp, "posix_rename", None)
    if posix_rename is not None:
        try:
            await posix_rename(src, dst)
            return
        except (asyncssh.SFTPOpUnsupported, asyncssh.SFTPFailure):
            pass
    with contextlib.suppress(OSError, asyncssh.Error):
        await sftp.remove(dst)
    await sftp.rename(src, dst)


def _is_closed(conn: Any) -> bool:
    checker = getattr(conn, "is_closed", None)
    return bool(checker()) if callable(checker) else False
