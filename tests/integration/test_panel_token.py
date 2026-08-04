"""面板令牌 —— 给**没有显示器的机器**用。

原来面板只认「连接来自回环」。那条判据的真实含义是「你是这台机器的主人」，
在笔记本上成立，在机柜里的服务器上直接崩掉：你没法在它上面开浏览器。
而全新的无头机器还没配过对，连节点间那条签名通道也用不上 —— 完全够不着。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace
from anthill.security.panel_token import load_or_create, token_path
from anthill.web.app import create_app
from anthill.web.context import NodeContext, NodeRegistry

TOKEN = "test-token-1234567890"


@pytest.fixture(autouse=True)
def home_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("ANTHILL_PANEL_TOKEN", raising=False)


def node_app(tmp_path: Path, *, token: str = TOKEN) -> object:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="headless")
    return create_app(
        registry=NodeRegistry([NodeContext(layout)]),
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
        panel_token=token,
    )


def client(app: object, *, host: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),  # type: ignore[arg-type]
        base_url="http://box.test",
    )


LAN = "10.15.3.99"


# ---------- 令牌本身 ----------


def test_a_token_is_generated_once_and_kept_private(tmp_path: Path) -> None:
    """它等价于「能在这台机器上执行命令」—— 分量和一把 SSH 私钥同档。"""
    first = load_or_create()
    second = load_or_create()

    assert first == second  # 重启不换
    assert len(first) > 30
    assert token_path().stat().st_mode & 0o077 == 0


def test_an_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_PANEL_TOKEN", "从环境变量来的")

    assert load_or_create() == "从环境变量来的"


def test_the_token_is_never_taken_from_the_command_line() -> None:
    """`ps` 是所有人都看得见的 —— 命令行参数只是个开关，不承载令牌本身。"""
    import inspect

    from anthill.cli.serve_cmd import serve_command

    option = inspect.signature(serve_command).parameters["panel_token"]
    assert option.annotation in (bool, "bool")  # from __future__ import annotations 会变成字符串


# ---------- 拿它能从别的机器操作 ----------


async def test_without_a_token_the_network_still_cannot_touch_the_panel(
    tmp_path: Path,
) -> None:
    async with client(node_app(tmp_path, token=""), host=LAN) as api:
        assert (await api.get("/panel/api/cluster")).status_code == 403
        assert (await api.post("/panel/api/agents", json={"name": "x"})).status_code == 403


async def test_a_valid_token_unlocks_the_panel_from_another_machine(
    tmp_path: Path,
) -> None:
    """这就是整件事的意义：61 没有显示器，也得能被操作。"""
    async with client(node_app(tmp_path), host=LAN) as api:
        headers = {"X-AntHill-Panel": TOKEN}
        state = await api.get("/panel/api/state", headers=headers)
        added = await api.post(
            "/panel/api/agents", json={"name": "coder", "brain": "echo"}, headers=headers
        )

    assert state.status_code == 200
    assert added.status_code == 201, added.text


async def test_a_wrong_token_is_refused(tmp_path: Path) -> None:
    async with client(node_app(tmp_path), host=LAN) as api:
        response = await api.get("/panel/api/state", headers={"X-AntHill-Panel": "wrong-token"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("Authorization: Bearer", {"headers": {"Authorization": f"Bearer {TOKEN}"}}),
        ("自定义头", {"headers": {"X-AntHill-Panel": TOKEN}}),
        ("cookie", {"cookies": {"anthill_panel": TOKEN}}),
        ("查询参数（第一次落地用）", {"params": {"token": TOKEN}}),
    ],
)
async def test_the_token_can_arrive_any_of_the_usual_ways(
    tmp_path: Path, label: str, kwargs: dict[str, object]
) -> None:
    async with client(node_app(tmp_path), host=LAN) as api:
        response = await api.get("/panel/api/state", **kwargs)  # type: ignore[arg-type]

    assert response.status_code == 200, label


async def test_loopback_never_needs_a_token(tmp_path: Path) -> None:
    """本机操作不该被自己的令牌挡住。"""
    async with client(node_app(tmp_path), host="127.0.0.1") as api:
        assert (await api.get("/panel/api/state")).status_code == 200


async def test_a_cross_site_page_is_refused_even_with_the_token(tmp_path: Path) -> None:
    """令牌可能被别的页面偷去用 —— 同源那道检查不因为有令牌就放松。"""
    async with client(node_app(tmp_path), host=LAN) as api:
        response = await api.get(
            "/panel/api/state",
            headers={"X-AntHill-Panel": TOKEN, "Origin": "http://evil.example"},
        )

    assert response.status_code == 403


async def test_a_non_ascii_token_is_refused_not_crashed(tmp_path: Path) -> None:
    async with client(node_app(tmp_path), host=LAN) as api:
        # HTTP 头按 latin-1 解出来，一个 0xFF 字节就能让定时安全比较抛 TypeError
        response = await api.get("/panel/api/state", headers={"X-AntHill-Panel": b"\xff\xfe token"})

    assert response.status_code == 401


async def test_the_setup_screen_is_reachable_on_a_brand_new_headless_box(
    tmp_path: Path,
) -> None:
    """全新的无头机器还没配过对，节点之间那条签名通道用不上 —— 只能靠令牌。"""
    app = create_app(
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
        panel_token=TOKEN,
    )
    headers = {"X-AntHill-Panel": TOKEN}

    async with client(app, host=LAN) as api:
        setup = await api.get("/panel/api/setup", headers=headers)
        browse = await api.get(f"/panel/api/setup/browse?path={tmp_path}", headers=headers)
        adopt = await api.post(
            "/panel/api/setup/adopt",
            json={"path": str(tmp_path / "远程建的"), "node_name": "remote"},
            headers=headers,
        )

    assert setup.status_code == 200
    assert setup.json()["ready"] is False
    assert browse.status_code == 200
    assert adopt.status_code == 201, adopt.text
