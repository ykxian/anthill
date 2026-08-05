"""在面板上配 provider —— 补上「面板建不出一个能干活的 Agent」那堵墙。

M10 立项目标是「装好就能用，单机不必开终端」，但实测第一步就破功：
面板的「加一个 Agent」表单没有 role 字段（建不出 coordinator），
选 provider 大脑又要求 `[providers.*]` 已经在 node.toml 里配好，
而面板**没有任何配 provider 的界面**。想跑通旗舰功能（多 Agent 编排），
必须先去配置页手写 TOML。

和 `web/agents.py` 一样：改配置走文本追加/按节删行，写完用启动期同一套模型校验，
不合法就原样退回、磁盘一个字不动 —— node.toml 里那一大堆注释是给人看的。

**密钥不写进这里。** `node.toml` 只存环境变量名，那条规矩不动；
密钥落在 `~/.anthill/secrets.env`（0600），见 `security/secrets.py`。
这一层只回答「那个变量设没设」，永远不回值。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.security import secrets

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

PRESETS = {
    "deepseek": {
        "kind": "openai_compat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "anthropic": {
        "kind": "anthropic",
        "base_url": "",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-5",
    },
    "openai": {
        "kind": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "qwen": {
        "kind": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": "qwen-plus",
    },
}
"""常见几家的默认值。面板上选一个就填好，省得人去翻各家文档找 base_url。"""


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=32)
    kind: Literal["openai_compat", "anthropic"] = "openai_compat"
    model: str = Field(min_length=1, max_length=128)
    api_key_env: str = Field(min_length=1, max_length=128)
    base_url: str = Field(default="", max_length=400)


class SecretSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=8192)


def listing(config: Config) -> list[dict[str, Any]]:
    """每个 provider 的公开信息 + 那个环境变量到底有没有值。

    「有没有值」是这一页最要紧的一格：配好了 provider 但没设 key 的话，
    agentd 一启动就 fail fast，而人在面板上看不出为什么。
    """
    stored = set(secrets.names())
    import os

    return [
        {
            "name": name,
            "kind": section.kind,
            "model": section.model,
            "api_key_env": section.api_key_env,
            "key_set": bool(os.environ.get(section.api_key_env)),
            "key_from": ("面板" if section.api_key_env in stored else "环境变量")
            if os.environ.get(section.api_key_env)
            else "",
        }
        for name, section in sorted(config.providers.items())
    ]


def add_provider(layout: NodeLayout, config: Config, spec: ProviderSpec) -> dict[str, Any]:
    if not NAME_RE.match(spec.name):
        raise AntHillError(f"非法 provider 名 {spec.name!r}（小写字母开头，可含数字、_、-）")
    if spec.name in config.providers:
        raise AntHillError(f"[providers.{spec.name}] 已经存在了")
    if spec.kind == "openai_compat" and not spec.base_url.strip():
        raise AntHillError("openai_compat 需要 base_url（比如 https://api.deepseek.com）")
    if spec.base_url and not spec.base_url.startswith(("http://", "https://")):
        raise AntHillError("base_url 得是 http(s):// 开头的完整地址")

    text = layout.node_toml.read_text(encoding="utf-8")
    return {"ok": True, "name": spec.name, "text": text.rstrip("\n") + "\n" + _section(spec)}


def remove_provider(layout: NodeLayout, config: Config, name: str) -> dict[str, Any]:
    if name not in config.providers:
        raise AntHillError(f"没有 [providers.{name}]")
    users = [a for a, section in config.agents.items() if section.provider == name]
    if users:
        # 删掉之后那些 Agent 一启动就报「provider 不存在」—— 先说清楚，别让人后面去猜
        raise AntHillError(f"还有 Agent 在用它：{', '.join(sorted(users))}。先改掉它们的 provider")

    from anthill.web.agents import drop_section

    text = drop_section(layout.node_toml.read_text(encoding="utf-8"), f"providers.{name}")
    return {"ok": True, "name": name, "text": text}


def _section(spec: ProviderSpec) -> str:
    lines = [
        f"\n[providers.{spec.name}]",
        f'kind = "{_esc(spec.kind)}"',
        f'model = "{_esc(spec.model)}"',
        f'api_key_env = "{_esc(spec.api_key_env)}"',
    ]
    if spec.base_url.strip():
        lines.append(f'base_url = "{_esc(spec.base_url.strip())}"')
    return "\n".join(lines) + "\n"


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
