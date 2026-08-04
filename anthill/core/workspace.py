"""建工作区这件事本身。

原来这段逻辑只长在 `anthill init` 里，于是「先在终端跑一次 init」成了硬前提 ——
新机器上装好、想直接开面板，第一步就撞墙。现在它是一个普通函数，
`init`、`serve`（找不到工作区就自己建一个）、面板都调它，行为完全一致。
"""

from __future__ import annotations

import contextlib
import socket

from anthill.core.config import Config, default_node_toml
from anthill.core.errors import AntHillError, ConfigError
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout

BOARD_SEED = "# BOARD\n\n> 当前协作状态快照，由 coordinator 单写者维护。\n"


def default_node_name() -> str:
    """默认拿主机名 —— 局域网里一眼能对上是哪台机器。"""
    raw = socket.gethostname().split(".")[0].lower()
    cleaned = "".join(c if c.isalnum() or c in "._-" else "-" for c in raw)
    return cleaned or "node"


def create_workspace(layout: NodeLayout, *, node_name: str = "", force: bool = False) -> Config:
    """在 `layout` 指的位置建出工作区骨架，返回加载好的配置。

    已经有 node.toml 时默认拒绝 —— 这个函数会覆盖配置文件，
    而配置被无声覆盖是很难查的那种事故。
    """
    layout.ensure_base()
    if layout.node_toml.is_file() and not force:
        raise ConfigError(f"{layout.node_toml} 已存在；要重建请显式要求覆盖")

    layout.node_toml.write_text(
        default_node_toml(node_name or default_node_name()), encoding="utf-8"
    )
    config = Config.load_from(layout)
    ensure_mailboxes(layout, config)
    (layout.blackboard / "BOARD.md").write_text(BOARD_SEED, encoding="utf-8")
    return config


def ensure_mailboxes(layout: NodeLayout, config: Config) -> list[str]:
    """给配置里每个 Agent 都把邮箱目录备好，返回这次新建的那些。

    **配置里有它，就该能收它的信** —— 不该等到那个 agentd 第一次启动。
    否则在面板上加完 Agent，别的机器投过来会撞上「邮箱还没建」的 404，
    而人看着面板上明明有这个 Agent，完全不知道差在哪。
    幂等，所以任何一次配置写入之后都可以无脑调一遍，顺便自愈。
    """
    created = []
    for name in config.agents:
        mailbox = Mailbox(layout.mailbox_dir(name))
        if not mailbox.exists:
            mailbox.ensure()
            created.append(name)
    return created


def load_or_create(layout: NodeLayout, *, node_name: str = "") -> tuple[Config, bool]:
    """有就加载，没有就建一个。返回 `(配置, 是不是刚建的)`。

    给「装好就能直接开面板」用：`anthill serve` 找不到工作区时不该报错退出，
    它该把工作区建出来然后继续 —— 建在哪、叫什么名字，打印出来告诉人就是了。
    """
    if layout.node_toml.is_file():
        return Config.load_from(layout), False
    return create_workspace(layout, node_name=node_name), True


class ConfigRef:
    """按 mtime 自动重载的 node.toml。

    起因是一个真 bug：面板上加了个 Agent，配置文件确实改了，可 serve 手里捧着的
    还是启动那一刻读到的那份 —— 于是新 Agent **既不出现在面板上，也收不了消息**
    （`/deliver` 拿旧 config 判收件人，直接回 404「本节点没有这个 Agent」）。

    node.toml 现在是运行期可改的（面板加 Agent、远端管理、人手动编辑），
    那就不能再当成启动期常量。和 `peers.json` 一个套路：**文件是唯一真相，
    内存里那份只是缓存**；改坏了就继续用上一份好的，别让一次手滑弄停整个节点。
    """

    def __init__(self, layout: NodeLayout, config: Config | None = None) -> None:
        self._layout = layout
        self._config = config if config is not None else Config.load_from(layout)
        self._mtime = self._stamp()

    @property
    def current(self) -> Config:
        stamp = self._stamp()
        if stamp is not None and stamp != self._mtime:
            # 改坏了就继续用上一份好的 —— 别让一次手滑弄停整个节点
            with contextlib.suppress(AntHillError):
                self._config = Config.load_from(self._layout)
            self._mtime = stamp
        return self._config

    def _stamp(self) -> float | None:
        try:
            return self._layout.node_toml.stat().st_mtime
        except OSError:
            return None
