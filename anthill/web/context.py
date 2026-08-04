"""这台机器上、由本进程照看的那些节点。

一个工作区 = 一个节点：peers、密钥、邮箱全都挂在它上面，工作区之间彼此隔离。
但**隔离不等于要各起一个进程** —— 路由键 `to.node` 本来就写在信封里，
所以一个 serve 拿到信一看就知道该往哪个工作区的邮箱放。
于是「一台机器一个端口、一个面板管全部工作区」是查表的事，不需要多进程。

这里就是那张表：

- `NodeContext` —— 一个节点的身份（工作区路径、配置、peers）
- `NodeRegistry` —— 本进程照看的全部节点，外加一个「主节点」（命令行指的那个）

`NodeRegistry` 可以是**空的**：`serve` 撞上一个还没有工作区的目录时不替人做主
建一个，而是空着等你在面板上挑。那时节点端点一律 503，面板只出设置界面。
"""

from __future__ import annotations

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.workspace import ConfigRef, create_workspace, default_node_name
from anthill.discovery.registry import PeerRegistry

NOT_READY = "本节点还没配好工作区 —— 打开面板挑一个目录（或者用 anthill init 建一个）"


class NodeContext:
    """一个节点。构造出来就是可用的 —— 「还没配好」由 `NodeRegistry` 为空表示。"""

    def __init__(self, layout: NodeLayout, config: Config | None = None) -> None:
        self.layout = layout
        self._ref = ConfigRef(layout, config)
        self.peers = PeerRegistry(layout.root)

    @property
    def config(self) -> Config:
        return self._ref.current

    @property
    def name(self) -> str:
        return self.config.node.name

    @classmethod
    def open(cls, layout: NodeLayout, *, node_name: str = "") -> NodeContext:
        """已经是工作区就直接用，不是就建出来。"""
        if not layout.node_toml.is_file():
            create_workspace(layout, node_name=node_name or default_node_name())
        return cls(layout)


class NodeRegistry:
    """本进程照看的所有节点，按名字查。

    刻意**不支持把一个已在册的节点换成别的工作区**：peers 与密钥跟着工作区走，
    换一次等于换身份，已经跑着的 agentd 与已建立的信任关系全都对不上。
    加、减可以；原地掉包不行。
    """

    def __init__(self, contexts: list[NodeContext] | None = None) -> None:
        self._nodes: dict[str, NodeContext] = {}
        self._primary = ""
        for ctx in contexts or []:
            self.add(ctx)

    # ---------- 查询 ----------

    @property
    def ready(self) -> bool:
        return bool(self._nodes)

    @property
    def primary(self) -> NodeContext:
        """命令行指的那个节点。面板不带 `?node=` 时说的就是它。"""
        if not self._primary:
            raise AntHillError(NOT_READY)
        return self._nodes[self._primary]

    @property
    def primary_name(self) -> str:
        return self._primary or "（未配置）"

    def all(self) -> list[NodeContext]:
        """主节点排最前，其余按名字。面板上的顺序也照这个来。"""
        rest = sorted(n for n in self._nodes if n != self._primary)
        return [self._nodes[n] for n in ([self._primary, *rest] if self._primary else rest)]

    def names(self) -> list[str]:
        return [ctx.name for ctx in self.all()]

    def get(self, name: str) -> NodeContext | None:
        return self._nodes.get(name)

    def require(self, name: str) -> NodeContext:
        ctx = self._nodes.get(name)
        if ctx is None:
            raise AntHillError(f"本进程没有照看名为 {name!r} 的节点")
        return ctx

    def speaker_for(self, remote: str) -> NodeContext:
        """要跟某个**远端**节点说话时，该用本机哪个身份去说。

        密钥是跟着工作区走的：和 lab 配过对的可能是节点 B 而不是主节点 A。
        找那个真的信任它的；都不认识就退回主节点，让它去报一个像样的错。
        """
        for ctx in self.all():
            if ctx.peers.get(remote) is not None:
                return ctx
        return self.primary

    def holds(self, layout: NodeLayout) -> bool:
        return any(ctx.layout.workspace == layout.workspace for ctx in self._nodes.values())

    # ---------- 增减 ----------

    def add(self, ctx: NodeContext, *, primary: bool = False) -> NodeContext:
        existing = self._nodes.get(ctx.name)
        if existing is not None and existing.layout.workspace != ctx.layout.workspace:
            raise AntHillError(
                f"已经有一个叫 {ctx.name} 的节点了（{existing.layout.workspace}）——"
                "两个工作区不能同名，否则信封上的收件人指谁就说不清了"
            )
        self._nodes[ctx.name] = ctx
        if primary or not self._primary:
            self._primary = ctx.name
        return ctx

    def attach(self, layout: NodeLayout, *, node_name: str = "") -> NodeContext:
        """认下一个工作区（没有就建）。已经在照看的直接返回，不重复加。"""
        for ctx in self._nodes.values():
            if ctx.layout.workspace == layout.workspace:
                return ctx
        return self.add(NodeContext.open(layout, node_name=node_name))

    def detach(self, name: str) -> None:
        ctx = self._nodes.pop(name, None)
        if ctx is None:
            return
        if self._primary == name:
            # 主节点被摘掉就顺位下一个，别让 primary 指向一个不存在的名字
            self._primary = next(iter(sorted(self._nodes)), "")
