"""远端管理：总控面板直接读写**别台机器**的 node.toml。

先把代价说清楚，因为这是这个项目里最重的一处权衡：

    **能改一台机器的 node.toml ≈ 能在那台机器上执行命令。**
    加一个带 `run_shell` 的 Agent 就行。

所以打开它之后，「信任一个对端」的含义就从「它能给我投消息、我的 Agent 会审」
变成了「它能接管我这台机器」。这不是可以顺手默认打开的东西，
于是接收端要一次性显式打开：

    [security]
    remote_admin = true          # 或 anthill serve --remote-admin

打开之后就是直连，没有逐次审批 —— 这是刻意的取舍：要的就是「在面板上直接配」。
不想给这么大权限、又想远程配的话，M5 那套 `approvals/` 审批流仍然在，
两者是并列的两条路，不是一条路的两档。

认证复用投递那把共享密钥（签 `域标签 + 节点 + 路径 + 时间戳`），
另外每一次改动都写审计日志，并且和本机面板一样留一份 `node.toml.bak`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.web.actions import ConfigRequest, read_config, write_config

MAX_CONFIG_CHARS = 200_000


class RemoteConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_CONFIG_CHARS)


def read_remote_config(layout: NodeLayout, *, by: str, log: EventLog) -> dict[str, Any]:
    log.info("admin.config_read", by=by)
    return read_config(layout)


def write_remote_config(
    layout: NodeLayout, body: RemoteConfigRequest, *, by: str, log: EventLog
) -> dict[str, Any]:
    """校验通过才落盘，并留备份 —— 和本机面板走的是同一条函数。

    审计日志写在**改动之前和之后**：改坏了配置的话，下一次 agentd 起不来，
    那时唯一能回答「谁在什么时候改的」的就是这条记录。
    """
    log.warn("admin.config_write_attempt", by=by, bytes=len(body.text))
    result = write_config(layout, ConfigRequest(text=body.text))
    log.warn("admin.config_written", by=by, backup=str(result.get("backup", "")))
    return result


def admin_enabled(config: Config) -> bool:
    return bool(getattr(config.security, "remote_admin", False))


def refuse_reason(config: Config) -> str:
    del config
    return (
        "本节点没有开放远端管理。"
        "对方要在 node.toml 里写 [security] remote_admin = true，"
        "或者用 anthill serve --remote-admin 启动。"
    )


def guard(config: Config) -> None:
    if not admin_enabled(config):
        raise AntHillError(refuse_reason(config))
