"""本机密钥库 —— 让「在面板上配好一个能干活的 Agent」不必掉回终端。

## 为什么需要它，以及它没有打破哪条规矩

这个项目有一条从 M2 起就守着的规矩：**配置文件只存环境变量的名字，绝不存密钥**。
那条规矩针对的是 `node.toml` —— 它会进 git、会被 `anthill fetch` 拉走、
会在面板的配置页上原样显示给任何能打开面板的人。这条规矩不动。

但它带来一个后果：面板上就算能配 provider，也仍然配不出一个**能干活**的 Agent ——
因为环境变量得在终端里 export，而且得在 agentd 启动**之前**。
于是 M10 那句「装好就能用、单机不必开终端」在第一步就破功了。

所以密钥单独存一个地方，和 `peers.json` 同一个待遇：

- 路径 `~/.anthill/secrets.env`，权限 0600，**不在工作区里**，不会进 git，
  不会被 `fetch` 拉走，不会出现在配置页上；
- `serve` 与 `agent start` 启动时读进 `os.environ`，所以下游一行代码都不用改 ——
  `registry.py` 还是照常 `os.environ.get(section.api_key_env)`；
- 任何读接口**只回「设没设」，永远不回值**；
- 真正的环境变量优先级更高 —— 已经在终端里 export 过的不会被这里覆盖。

代价要说清楚：密钥落到了磁盘上（0600）。这和「只写变量名」是两种权衡，
所以两条路都留着：不想落盘就照旧 export，面板会显示「已从环境变量读到」。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from anthill.core.errors import AntHillError

SECRETS_FILE = "secrets.env"
FILE_MODE = 0o600
MAX_NAME = 128
MAX_VALUE = 8192
PANEL_TOKEN_ENV = "ANTHILL_PANEL_TOKEN"


def secrets_path() -> Path:
    return Path.home() / ".anthill" / SECRETS_FILE


NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""环境变量名的常规形状。

**必须是 ASCII 严格匹配**，不能用 `str.isalnum()` —— 那个对中文返回 True，
于是「有中文」会被当成合法变量名放进去。名字会被写进 `NAME=value` 一行，
带 `=` 或换行的名字能把这个文件写成别的东西。
"""


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME and bool(NAME_RE.match(name))


def read_all() -> dict[str, str]:
    """读出全部键值。**只给 `load_into_env` 和 `set_secret` 用** ——
    任何面向网络的接口都只该用 `names()`。"""
    path = secrets_path()
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        if sep and _valid_name(name.strip()):
            out[name.strip()] = value
    return out


def names() -> list[str]:
    """存了哪些变量名。值不出这个模块。"""
    return sorted(read_all())


def load_into_env(env: dict[str, str] | None = None) -> int:
    """把密钥库读进环境。返回注入了几个。

    **真正的环境变量优先** —— 已经 export 过的不覆盖，
    否则「我明明在终端里换了 key 却不生效」会是个极难查的问题。
    """
    target = os.environ if env is None else env
    injected = 0
    for name, value in read_all().items():
        if not target.get(name):
            target[name] = value
            injected += 1
    return injected


def sanitized_child_env(
    *, blocked: Iterable[str] = (), extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """给外部 Agent/CLI 子进程的环境：保留正常 shell 配置，剥掉 AntHill 凭据。

    `secrets.env` 里的所有名字、面板 bearer token、全部 ``ANTHILL_*`` 内部变量，
    以及调用方从 node.toml provider 配置得到的变量名都不会默认传给项目内 Agent。
    调用方显式给的 `extra` 最后合并，便于传递无密钥的内部会话变量。
    """
    env = dict(os.environ)
    sensitive = {
        *read_all(),
        *blocked,
        PANEL_TOKEN_ENV,
        *(name for name in env if name.startswith("ANTHILL_")),
    }
    for name in sensitive:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def set_secret(name: str, value: str) -> None:
    if not _valid_name(name):
        raise AntHillError(f"不是合法的环境变量名：{name!r}（只能是字母、数字、下划线）")
    if not value:
        raise AntHillError("密钥不能为空；要删掉请用删除")
    if len(value) > MAX_VALUE or "\n" in value or "\r" in value:
        raise AntHillError("密钥太长或者含换行 —— 多半是粘错了东西")
    _write({**read_all(), name: value})
    os.environ[name] = value  # 当前进程立刻生效，不用重启 serve


def unset_secret(name: str) -> bool:
    current = read_all()
    if name not in current:
        return False
    del current[name]
    _write(current)
    os.environ.pop(name, None)
    return True


def _write(values: dict[str, str]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{name}={value}\n" for name, value in sorted(values.items()))
    # 先按 0600 建出来再写内容 —— 反过来的话，中间那一瞬间是 0644
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.write(handle, body.encode("utf-8"))
    finally:
        os.close(handle)
    path.chmod(FILE_MODE)  # 文件已存在时 os.open 不会改权限，补一刀
