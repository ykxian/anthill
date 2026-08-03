"""回信暂存区：给「连不回去的对端」用。

SSH 这条路天生是单向的。你的笔记本能 SSH 到学校服务器，
但服务器**连不回你的笔记本** —— 你在 NAT 后面，也没跑 sshd。
那服务器上的 Agent 干完活，结果怎么送回来？

答案是**回信改成拉取**，和 `git pull` 一个道理：

    服务器：投不出去的信封落进 .anthill/spool/<对方节点>/
    笔记本：anthill pull lab-server   → SFTP 取回来、投进本机邮箱、删掉远端的

暂存的信封还是原来那个信封（id、签名、thread 全都不变），
所以拉回来之后走的是和同机投递完全一样的处理路径。
"""

from __future__ import annotations

from pathlib import Path

from anthill.core.atomic import atomic_write
from anthill.core.envelope import NODE_NAME_RE, Envelope
from anthill.core.errors import MailboxError

SPOOL_DIR = "spool"


class Spool:
    """`.anthill/spool/<node>/<id>.json`。按目标节点分目录，拉取方只看自己那一格。"""

    def __init__(self, root: Path) -> None:
        self._root = root / SPOOL_DIR

    @property
    def root(self) -> Path:
        return self._root

    def dir_for(self, node: str) -> Path:
        if not NODE_NAME_RE.match(node):
            raise MailboxError(f"非法节点名 {node!r}，不能作为暂存目录名")
        return self._root / node

    def deposit(self, env: Envelope) -> Path:
        """暂存一条投不出去的信封。原子写 —— 拉取方可能正在同时扫这个目录。"""
        target = self.dir_for(env.to.node)
        target.mkdir(parents=True, exist_ok=True)
        return atomic_write(target, target, f"{env.id}.json", env.to_json_bytes())

    def nodes(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(p.name for p in self._root.iterdir() if p.is_dir())

    def pending(self, node: str) -> list[Path]:
        target = self.dir_for(node)
        if not target.is_dir():
            return []
        return sorted(p for p in target.iterdir() if p.suffix == ".json")

    def take(self, node: str, name: str) -> Envelope:
        path = self.dir_for(node) / name
        try:
            return Envelope.from_json_bytes(path.read_bytes())
        except OSError as exc:
            raise MailboxError(f"读取暂存信封 {path} 失败：{exc}") from exc

    def drop(self, node: str, name: str) -> None:
        (self.dir_for(node) / name).unlink(missing_ok=True)
