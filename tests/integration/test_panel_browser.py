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


@pytest.fixture
def panel(tmp_path: Path) -> Iterator[str]:
    """起一个真的 serve，返回面板地址。"""
    workspace = tmp_path / "ws"
    create_workspace(NodeLayout(workspace), node_name="browserbox")
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "anthill",
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--panel-write",
            "--quiet",
        ],
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
