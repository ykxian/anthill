"""节点身份：可以在运行期**被设定一次**。

以前 `layout` / `config` / `peers` 是在 `create_app` 时captured 进闭包的常量，
因为「一个 serve 进程 = 一个节点 = 一个工作区」在启动那一刻就定死了。

现在多了一种开局：`anthill serve` 撞上一个还没有工作区的目录时，
不再替人做主就地建一个，而是进入**未配置**状态 ——
面板给一个目录浏览器，人挑好地方再建。在那之前磁盘一个字都不写。

所以身份要能从「没有」变成「有」。刻意**只支持这一次转变**，不支持来回切换：
peers 与密钥是跟着工作区走的，运行中途换工作区等于换身份，
已经跑着的 agentd、已经建立的信任关系全都对不上，
那是一堆很难查的问题，换来的方便却很有限。想换？重启一次 serve 就好。
"""

from __future__ import annotations

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.workspace import ConfigRef, create_workspace, default_node_name
from anthill.discovery.registry import PeerRegistry

NOT_READY = "本节点还没配好工作区 —— 打开面板挑一个目录（或者用 anthill init 建一个）"


class NodeContext:
    """身份的持有者。没配好之前，问它要 layout 就抛。"""

    def __init__(
        self,
        layout: NodeLayout | None = None,
        config: Config | None = None,
        peers: PeerRegistry | None = None,
    ) -> None:
        self._layout = layout
        self._ref = ConfigRef(layout, config) if layout is not None else None
        self._peers = peers

    @property
    def ready(self) -> bool:
        return self._layout is not None

    @property
    def layout(self) -> NodeLayout:
        if self._layout is None:
            raise AntHillError(NOT_READY)
        return self._layout

    @property
    def config(self) -> Config:
        if self._ref is None:
            raise AntHillError(NOT_READY)
        return self._ref.current

    @property
    def peers(self) -> PeerRegistry:
        if self._peers is None:
            raise AntHillError(NOT_READY)
        return self._peers

    @property
    def node_name(self) -> str:
        """给日志和错误信息用，没配好也能安全地叫一声。"""
        return self.config.node.name if self.ready else "(未配置)"

    def adopt(self, layout: NodeLayout, *, node_name: str = "") -> Config:
        """认下一个工作区：已经有配置就直接用，没有就建出来。

        只能从「未配置」走到「已配置」—— 见模块开头关于不支持切换的说明。
        """
        if self.ready:
            raise AntHillError(
                f"本节点已经在用 {self.layout.workspace} 了；"
                "要换工作区就重启一次 serve（peers 与密钥是跟着工作区走的）"
            )
        config = (
            Config.load_from(layout)
            if layout.node_toml.is_file()
            else create_workspace(layout, node_name=node_name or default_node_name())
        )
        self._layout = layout
        self._ref = ConfigRef(layout, config)
        self._peers = PeerRegistry(layout.root)
        return config
