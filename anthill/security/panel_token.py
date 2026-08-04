"""面板的访问令牌 —— 给**没有显示器的机器**用。

原来面板的写操作只认「连接来自回环」。那条判据的真实含义是「你是这台机器的主人」，
在笔记本上成立，在一台放在机柜里的服务器上直接崩掉：**你没法在它上面开浏览器**。
而且一台全新的无头机器还没配过对，连节点之间那条签名通道也用不上 —— 等于完全够不着。

所以需要一个真凭据。三条设计上的取舍：

1. **绝不从命令行参数传令牌。** `ps` 是所有人都看得见的。令牌由本机生成、
   落在 `~/.anthill/panel-token`（0600），启动时打印一次；
   要覆盖就用环境变量 `ANTHILL_PANEL_TOKEN`。
2. **它是 bearer 凭据，等价于「在这台机器上执行命令」** —— 拿到它就能改
   node.toml、就能加一个带 `run_shell` 的 Agent。所以它的分量和一把 SSH 私钥
   是一档的，不是"网页密码"那一档。
3. **明文 HTTP 上它会被嗅探到。** 这一点必须说在明处：局域网不可信时，
   正确做法是 `ssh -L` 把端口转发到本地，而不是开着令牌裸奔。

回环仍然直接放行 —— 本机操作不该被自己的令牌挡住。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

TOKEN_FILE = "panel-token"
TOKEN_MODE = 0o600
TOKEN_BYTES = 32
ENV_VAR = "ANTHILL_PANEL_TOKEN"
HEADER = "x-anthill-panel"
COOKIE = "anthill_panel"


def token_path() -> Path:
    """放在 `~/.anthill/` 而不是工作区里：还没有工作区的时候也得能用。"""
    return Path.home() / ".anthill" / TOKEN_FILE


def load_or_create() -> str:
    """环境变量优先；否则读本机那份，没有就生成一个。"""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return override

    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(TOKEN_MODE)
    return token


def presented(headers: dict[str, str], cookies: dict[str, str], query: str = "") -> str:
    """从请求里把令牌捞出来。

    三个来源：`Authorization: Bearer`、自定义头、cookie。
    URL 上的 `?token=` 只用于第一次落地（页面拿到之后会立刻从地址栏抹掉并存起来）——
    URL 会进浏览器历史和 referer，不该长期待在那儿。
    """
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get(HEADER, "") or cookies.get(COOKIE, "") or query).strip()


def matches(given: str, expected: str) -> bool:
    if not given or not expected:
        return False
    try:
        return hmac.compare_digest(given, expected)
    except TypeError:  # 非 ASCII 的 str 会抛，别让它变成 500
        return False
