"""在真的浏览器里点一遍面板。

为什么非要这一层：这个项目的面板此前**在浏览器里根本没跑通过**，而所有测试都绿的。
两个 bug 一起藏了很久，只有真正打开页面才会现形：

1. 页面挂在 `/panel`（没有尾斜杠），所以 `fetch("api/cluster")` 解析成
   `/api/cluster` —— 404。拓扑一直是空的。
2. `uvicorn` 没装 ws 实现时，WebSocket 升级请求被当成 404，实时推送从来没通过。

curl 测的是接口，ASGI 传输测的是路由，**都测不到「浏览器怎么解析相对路径」**。
所以这里用 playwright 真开一个 chromium。没装就跳过，不挡住别人跑测试。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="没装 playwright，跳过浏览器测试"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with playwright_api.sync_playwright() as p:
        try:
            engine = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium 起不来：{exc}")
        yield engine
        engine.close()


def serve(
    workspace: Path | None, *, cwd: Path | None = None, home: Path | None = None
) -> Iterator[str]:
    """`workspace=None` = 不给 `-w`，让它在 cwd 里找 —— 找不到就是「未配置」。

    给了 `-w` 就是「没有也给我建一个」，那样永远到不了未配置状态。

    **HOME 必须换掉。** 工作区清单和面板令牌都落在 `~/.anthill/`，
    子进程没法用 monkeypatch 拦 —— 不换的话测试会往开发者的家目录里写东西，
    而且下一次「全新机器」会把上一次建的工作区认回来，于是根本不新。
    """
    port = free_port()
    command = [
        sys.executable,
        "-m",
        "anthill",
        "serve",
        "--port",
        str(port),
        "--panel-write",
        "--quiet",
    ]
    if workspace is not None:
        command[4:4] = ["--workspace", str(workspace)]
    sandbox = home or (cwd or Path.cwd()) / "fake-home"
    (sandbox / "projects").mkdir(parents=True, exist_ok=True)  # 目录浏览器得有东西可列
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, "HOME": str(sandbox)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/panel"
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("serve 没起来")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[str]:
    """一个已经配好工作区的 serve。"""
    workspace = tmp_path / "ws"
    create_workspace(NodeLayout(workspace), node_name="browserbox")
    yield from serve(workspace, home=tmp_path / "home")


@pytest.fixture
def fresh_panel(tmp_path: Path) -> Iterator[str]:
    """一台**全新**机器：空目录，没给 -w，所以它不会擅自建 —— 就是「未配置」。"""
    empty = tmp_path / "brand-new"
    empty.mkdir()
    yield from serve(None, cwd=empty, home=tmp_path / "home")


def open_panel(browser: object, url: str) -> tuple[object, list[str]]:
    page = browser.new_page(viewport={"width": 1440, "height": 900})  # type: ignore[attr-defined]
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None,
    )
    page.goto(url, wait_until="networkidle")
    return page, errors


def test_the_panel_loads_its_data_and_opens_the_websocket(browser: object, panel: str) -> None:
    """一次覆盖开头说的那两个 bug：拓扑得有内容，连接状态得是「实时」。"""
    page, errors = open_panel(browser, panel)

    page.wait_for_selector("#topo-body .card", timeout=15000)
    page.wait_for_function(
        "() => document.getElementById('conn').className === 'live'", timeout=15000
    )

    assert page.text_content("#node") == "browserbox"
    assert "实时" in page.text_content("#conn")  # WS 通了；只有轮询的话这里是「轮询」
    assert errors == []
    page.close()


def test_adding_starting_and_stopping_an_agent_from_the_page(browser: object, panel: str) -> None:
    """单机不开终端也能用 —— 这条路径全在页面上。"""
    card = '#topo-body .card:has-text("frombrowser")'
    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)

    # 加
    page.fill("#agent-name", "frombrowser")
    page.select_option("#agent-brain", "echo")
    page.click('#add-agent button[type="submit"]')
    page.wait_for_selector(card, timeout=15000)

    # 启
    page.hover(card)
    page.click(f'{card} [data-op="start"]')
    page.wait_for_selector(f'{card} [data-op="stop"]', timeout=20000)

    # 停
    page.hover(card)
    page.click(f'{card} [data-op="stop"]')
    page.wait_for_selector(f'{card} [data-op="start"]', timeout=20000)

    assert errors == []
    page.close()


def test_talking_to_an_agent_from_the_page(browser: object, panel: str) -> None:
    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)

    page.click('.tab[data-pane="chat"]')
    page.select_option("#chat-to", "echo")
    page.fill("#chat-input", "从浏览器发的一条")
    page.click('#chat-form button[type="submit"]')

    page.wait_for_selector("#chat-body .msg.mine", timeout=15000)
    assert "从浏览器发的一条" in page.text_content("#chat-body")
    assert errors == []
    page.close()


def test_every_tab_renders_without_error(browser: object, panel: str) -> None:
    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)

    for pane in ("chat", "events", "setup", "config"):
        page.click(f'.tab[data-pane="{pane}"]')
        page.wait_for_timeout(400)
        assert not page.is_hidden(f"#pane-{pane}"), pane

    # 配置页要真的把 node.toml 读出来，不能是空的
    assert "[node]" in page.input_value("#config-text")
    assert errors == []
    page.close()


def test_the_sidebar_form_does_not_overflow(browser: object, panel: str) -> None:
    """侧栏是固定宽度的 —— 表单撑破它的话，控件会被裁掉一半，点都点不着。"""
    page, _ = open_panel(browser, panel)
    page.wait_for_selector("#add-agent", timeout=15000)

    overflow = page.evaluate(
        "() => { const a = document.querySelector('aside'); return a.scrollWidth - a.clientWidth; }"
    )

    assert overflow <= 1, f"侧栏横向溢出 {overflow}px"
    page.close()


def test_a_brand_new_machine_can_reach_the_setup_screen(browser: object, fresh_panel: str) -> None:
    """真出过的死结：探写权限时拿 `api/config` 探，而它在「还没配工作区」时回 409 ——
    于是全新机器被判成「不能写」，那个**专门给「还没有工作区」准备的**设置界面
    反而永远出不来，没有任何办法把工作区配起来。
    """
    page, errors = open_panel(browser, fresh_panel)
    page.wait_for_timeout(1500)

    tab = page.query_selector('.tab[data-pane="setup"]')
    assert tab is not None and not tab.is_hidden(), "工作区标签页没出来 —— 全新机器就没救了"

    tab.click()
    page.wait_for_selector("#dir-list .dir", timeout=15000)
    assert not page.is_hidden("#picker")  # 没配好时目录浏览器该直接摊开
    assert errors == []
    page.close()


def test_a_brand_new_machine_can_create_its_workspace_from_the_page(
    browser: object, fresh_panel: str, tmp_path: Path
) -> None:
    page, errors = open_panel(browser, fresh_panel)
    page.wait_for_timeout(1500)
    page.click('.tab[data-pane="setup"]')
    page.wait_for_selector("#ws-form", timeout=15000)

    page.fill("#ws-name", "made-in-browser")
    page.fill("#ws-node", "newbox")
    page.click('#ws-form button[type="submit"]')

    # 认下工作区之后页面会整个重来，节点名应当变成刚填的那个
    page.wait_for_function(
        "() => document.getElementById('node').textContent === 'newbox'", timeout=20000
    )
    assert errors == []
    page.close()
