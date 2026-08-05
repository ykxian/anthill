"""路由表本身对不对。

起因是一条**从来没有匹配上过任何请求**的路由：

    @app.post("/node/{name}/agents/{{agent}}/{{action}}")   # 少了 f 前缀

少一个 `f`，FastAPI 注册的就是字面量 `{{agent}}`，多节点的远端启停功能因此一直是死的。
而它返回的 404 又被翻译成「对端没开远端管理」，反过来诱导人去打开 remote_admin ——
那是整套里最重的一个开关。这类错误肉眼极难发现，所以在这儿一次性钉住整类。
"""

from __future__ import annotations

import pytest
from starlette.routing import Route, WebSocketRoute

from anthill.core.logging import EventLog
from anthill.web.app import create_app


def paths(app: object) -> list[str]:
    return [
        r.path
        for r in app.routes  # type: ignore[attr-defined]
        if isinstance(r, Route | WebSocketRoute)
    ]


@pytest.fixture
def app() -> object:
    return create_app(
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
        remote_admin=True,
    )


def test_no_route_has_a_literal_double_brace(app: object) -> None:
    """`{{agent}}` 出现在注册路径里 = 有人写 f-string 时漏了 f。"""
    broken = [p for p in paths(app) if "{{" in p or "}}" in p]

    assert broken == []


def test_no_route_repeats_a_path_segment_prefix(app: object) -> None:
    """`/node/{name}/node/agents/...` 这种拼接事故 —— 常量已含前缀又拼了一遍。"""
    doubled = [p for p in paths(app) if "/node/" in p.removeprefix("/node/")]

    assert doubled == []


@pytest.mark.parametrize(
    "url",
    [
        "/node/lab/agents/coder/start",
        "/node/lab/agents/coder/stop",
        "/node/agents/coder/start",
        "/node/lab/config",
        "/node/lab/summary",
    ],
)
def test_the_remote_admin_urls_the_client_builds_actually_match(app: object, url: str) -> None:
    """客户端在 web/remote.py 里拼的就是这些 —— 拼了没人接就是白拼。"""
    matched = any(
        r.path_regex.match(url)  # type: ignore[union-attr]
        for r in app.routes  # type: ignore[attr-defined]
        if isinstance(r, Route)
    )

    assert matched, url
