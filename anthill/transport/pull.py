"""把远端替我们暂存的回信取回本机邮箱。

SSH 是单向的：服务器连不回你的笔记本（NAT 后面、也没跑 sshd），所以它把回信
暂存在 `.anthill/spool/<你的节点>/`，由这边来拉 —— 和 `git pull` 一个道理。

这段逻辑本来长在 `cli/remote_cmd.py` 里，而且**只能由人敲命令触发** ——
也就是说人不敲命令，SSH 对端的回信就永远不回来。跨机协作的回程靠人肉驱动，
那这条链其实是断的。挪到这里，好让 `serve` 也能定时拉一次（见 `[runtime] auto_pull_seconds`）。

拉取顺序上有两条硬规矩：
- **先落本地再删远端** —— 反过来的话中途断网就把信弄丢了；
- **只收发给自己的信** —— 和 `/deliver` 的 421 一个道理，不当第三方的中转。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncssh

from anthill.core.config import Config, PeerSection
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.ids import is_valid_id
from anthill.core.logging import EventLog
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.spool import SPOOL_DIR
from anthill.transport.ssh import Connector, SshTransport, default_connect


@dataclass(frozen=True, slots=True)
class PullReport:
    """拉回来了什么、跳过了什么。调用方自己决定怎么呈现（CLI 打印 / serve 记日志）。"""

    taken: tuple[str, ...] = ()
    skipped: tuple[str, ...] = field(default=())

    @property
    def count(self) -> int:
        return len(self.taken)


def is_envelope_name(name: str) -> bool:
    """名字来自远端，会被拼进「读」和「删」两个路径。

    正常的 SFTP 服务端不会返回带 `/` 的条目，但对面已经被攻陷时会 ——
    这个校验的成本是三行，代价是别人能指使我们删任意文件。
    """
    return name.endswith(".json") and is_valid_id(name[: -len(".json")])


async def pull_once(
    layout: NodeLayout,
    config: Config,
    node: str,
    peer: PeerSection,
    *,
    log: EventLog | None = None,
    connect: Connector | None = None,
) -> PullReport:
    """拉一轮。**「连不上」会抛出去，不会被当成「没有待取的」**。

    这两件事混成一个「一切正常」的话，用户会以为回信收完了，其实还堆在服务器上。
    """
    remote_dir = f".anthill/{SPOOL_DIR}/{config.node.name}"
    transport = SshTransport(
        node_name=config.node.name,
        log=log or EventLog(None, agent=config.node.name, echo=False),
        # 测试里换成进程内的 SSH 服务端；生产走默认拨号
        connect=connect or default_connect,
    )
    taken: list[str] = []
    skipped: list[str] = []
    try:
        try:
            names = await transport.listdir(node, peer, remote_dir)
        except asyncssh.SFTPError:
            return PullReport()  # 目录还没建 = 没有待取的，不是错误

        for name in names:
            if not is_envelope_name(name):
                skipped.append(f"可疑的文件名 {name!r}")
                continue
            raw = await transport.read_bytes(node, peer, f"{remote_dir}/{name}")
            try:
                env = Envelope.from_json_bytes(raw)
            except AntHillError as exc:
                skipped.append(f"损坏的 {name}：{exc}")
                continue
            if env.to.node != config.node.name:
                skipped.append(f"{name} 是发给 {env.to.node} 的，不代收")
                continue
            mailbox = Mailbox(layout.mailbox_dir(env.to.agent))
            if not mailbox.exists:
                skipped.append(f"{env.to.agent} 的邮箱不存在，跳过 {name}")
                continue
            mailbox.deposit(env)
            # 先落本地再删远端：反过来的话中途断网就把信弄丢了
            await transport.remove(node, peer, f"{remote_dir}/{name}")
            taken.append(f"{env.type} {env.from_} → {env.to}")
    finally:
        await transport.close()
    return PullReport(taken=tuple(taken), skipped=tuple(skipped))


def ssh_peers(config: Config) -> list[tuple[str, PeerSection]]:
    """配置里所有走 SSH 的对端 —— 只有它们需要「拉」，LAN 那侧是推过来的。"""
    return [(name, peer) for name, peer in sorted(config.peers.items()) if peer.transport == "ssh"]
