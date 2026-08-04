"""对端之间的 HTTP 客户端。

**不走系统代理**（`trust_env=False`）。代理是用来出网的，而这里的目标是
你自己局域网里的另一台机器 —— 把它塞给代理，轻则绕一圈，重则直接失败：
`no_proxy` 里写 `10.0.0.0/8` 这种网段，httpx 是不认的（它按主机名匹配），
于是 `10.15.3.61` 被判成要走代理，换回来一个空的 502，
错误信息还完全看不出根因。这个坑值得在代码里堵死，而不是让每个人自己去踩。
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 10.0


def peer_client(timeout: float = DEFAULT_TIMEOUT, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, trust_env=False, **kwargs)  # type: ignore[arg-type]
