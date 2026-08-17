"""收件人解析与 @mention（02-protocol §5）。

三种寻址形式：
- 具体名 `coder`
- 角色 `role:reviewer` —— 节点内有多个同角色时选「负载最低」的那个
- 广播 `all` —— 仅 event 类型允许

跨节点地址不在这里解析，交给传输层按 peers 配置处理。
"""

from __future__ import annotations

import re

from anthill.core.config import Config
from anthill.core.envelope import BROADCAST, ROLE_PREFIX, Address, Envelope
from anthill.core.errors import HopLimitExceeded, UnknownRecipient
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout

MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,31})\b")


def extract_mentions(text: str) -> tuple[str, ...]:
    """从 Agent 的自然语言输出里抓 @mention，去重且保持出现顺序。"""
    seen: dict[str, None] = {}
    for name in MENTION_RE.findall(text):
        seen.setdefault(name, None)
    return tuple(seen)


def parse_address(raw: str, *, default_node: str) -> Address:
    """把人写的收件人字符串解析成 Address。

    支持 `coder` / `role:reviewer` / `all` / `node:agent` / `node:role:x` 五种写法。
    `role:` 里也带冒号，所以不能简单按第一个冒号切 —— 先认已知前缀。
    """
    text = raw.strip()
    if not text:
        raise UnknownRecipient("收件人不能为空")
    if text.startswith(ROLE_PREFIX) or text == BROADCAST or ":" not in text:
        return Address(node=default_node, agent=text)
    node, _, agent = text.partition(":")
    return Address(node=node, agent=agent)


class Router:
    """无状态解析器（负载信息实时读磁盘），可以随便新建。"""

    def __init__(self, config: Config, layout: NodeLayout) -> None:
        self._config = config
        self._layout = layout

    @property
    def node_name(self) -> str:
        return self._config.node.name

    def is_local(self, address: Address) -> bool:
        return address.node == self.node_name

    def resolve(self, env: Envelope) -> tuple[Envelope, ...]:
        """把一条信封展开成「收件人已确定」的若干条信封。

        广播会产生多条（每个收件人一条独立信封，各自有 id，便于分别追踪回执）。
        """
        target = env.to
        if not self.is_local(target):
            return (env,)  # 跨节点：由传输层按 peer 配置投递，不在本地展开

        names = self._resolve_names(env)
        if not names:
            raise UnknownRecipient(f"无法解析收件人 {target}：本节点没有匹配的 Agent")
        if len(names) == 1 and names[0] == target.agent:
            return (env,)
        return tuple(
            env.with_recipient(target.with_agent(name))
            if i == 0
            else env.model_copy(update={"to": target.with_agent(name), "id": _fresh_id()})
            for i, name in enumerate(names)
        )

    def _resolve_names(self, env: Envelope) -> list[str]:
        target = env.to
        if target.is_broadcast:
            return [name for name in sorted(self._config.agents) if name != env.from_.agent]
        if target.is_role:
            role = target.role or ""
            candidates = [a.name for a in self._config.agents_with_role(role)]
            if not candidates:
                raise UnknownRecipient(
                    f"本节点没有角色为 {role!r} 的 Agent；已配置："
                    + (
                        ", ".join(f"{a.name}({a.role})" for a in self._config.agents.values())
                        or "无"
                    )
                )
            return [min(candidates, key=lambda name: (self._queue_depth(name), name))]
        if target.agent not in self._config.agents:
            # 裸名字默认补成本节点 —— 想发到别的机器/工作区的人最容易在这儿栽：
            # 报错里把正确写法给出来，别让人以为是链路断了
            raise UnknownRecipient(
                f"本节点没有名为 {target.agent!r} 的 Agent；已配置："
                + (", ".join(sorted(self._config.agents)) or "无")
                + f"。要发给别的节点上的 Agent，写完整地址：<节点名>:{target.agent}"
            )
        return [target.agent]

    def _queue_depth(self, name: str) -> int:
        """用 inbox/new 里的积压条数近似「负载」。够用且零额外状态。"""
        return len(Mailbox(self._layout.mailbox_dir(name)).list_new())

    @staticmethod
    def check_hops(env: Envelope) -> None:
        """路由层的最后一道熔断：拒发已超跳数的消息。"""
        if env.hops > env.ttl_hops:
            raise HopLimitExceeded(
                f"消息 {env.id} hops={env.hops} 超过 ttl_hops={env.ttl_hops}，路由层拒发"
            )


def _fresh_id() -> str:
    from anthill.core.ids import new_id

    return new_id()
