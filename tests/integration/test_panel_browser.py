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

import json
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

REQUIRE = os.environ.get("ANTHILL_REQUIRE_BROWSER") == "1"
"""CI 里置 1：装好了浏览器却还跳过，说明装的那步白做了 —— 直接判失败。

光在 CI 里 `playwright install` 是不够的。这组测试原本会 importorskip 通过、
然后在 `launch()` 那步静默 skip —— **投入最大、抓 bug 最多的一组，CI 里一次都没跑过**。
「跳过」在 CI 里必须是响的，否则 M14 那两个藏了很久的 bug 重现时 CI 照样全绿。
"""


def _unavailable(reason: str) -> None:
    if REQUIRE:
        pytest.fail(f"ANTHILL_REQUIRE_BROWSER=1 但浏览器测试跑不了：{reason}")
    pytest.skip(reason)


try:
    import playwright.sync_api as playwright_api
except ImportError:  # pragma: no cover - 取决于装没装
    playwright_api = None  # type: ignore[assignment]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    if playwright_api is None:
        _unavailable("没装 playwright")
    with playwright_api.sync_playwright() as p:
        try:
            engine = p.chromium.launch()
        except Exception as exc:
            _unavailable(f"chromium 起不来：{exc}")
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


@pytest.fixture
def bridge_panel(tmp_path: Path) -> Iterator[str]:
    """一个装着桥接 Agent、并且已经有人在等它回话的工作区。"""
    workspace = tmp_path / "ws"
    layout = NodeLayout(workspace)
    create_workspace(layout, node_name="bridgebox")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.cc]\nrole = "worker"\nbridge = true\n',
        encoding="utf-8",
    )
    inbox = layout.agent_dir("cc") / "bridge" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "01KZ000000000000000000000A.md").write_text(
        "---\nfrom: bridgebox:cli\nto: bridgebox:cc\ntype: chat\n---\n"
        "这块接口我想改成异步的，你那边有依赖吗\n",
        encoding="utf-8",
    )
    yield from serve(workspace, home=tmp_path / "home")


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


def test_the_bridge_tab_shows_the_queue_and_can_answer_it(
    browser: object, bridge_panel: str, tmp_path: Path
) -> None:
    """「加它的地方和用它的地方是同一个地方」的验收。

    以前在网页上加完 bridge Agent，页面上一点痕迹都没有 ——
    还得去终端交代一遍「盯着那个目录」，而目录里空着，看着像没建成功。
    """
    page, errors = open_panel(browser, bridge_panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)

    page.click('.tab[data-pane="bridge"]')
    page.wait_for_selector("#bridge-body .waiting", timeout=15000)

    assert "异步" in page.text_content("#bridge-body")
    # 「把终端接进来」那三条路也在，而且路径是填好的
    page.click("#bridge-connect > summary")
    page.wait_for_selector("#rcp-mcp", timeout=15000)
    # 1 是个真的监控循环（会阻塞的命令），不是「你想起来看一眼」
    assert "--wait" in page.text_content("#rcp-prompt")
    # 2 的命令里**不写 Agent 名** —— 写死的话同一份配置下的会话会抢同一个
    assert "mcp serve -w" in page.text_content("#rcp-mcp")
    assert "cc" not in page.text_content("#rcp-mcp").split("mcp serve")[1].split("-w")[0]
    assert "ANTHILL_AGENT=cc" in page.text_content("#rcp-pin")
    assert "认领" in page.text_content("#bridge-recipes")
    # 一个会话挂多个工作区：再加一台 server，名字不同即可
    assert "anthill-" in page.text_content("#rcp-multi")
    # 「谁占着哪个」每行都能直接复制一条启动命令 —— 不想依赖自动认领就一律用它
    pin = page.query_selector("[data-copy-text]")
    assert pin is not None
    assert pin.get_attribute("data-copy-text") == "ANTHILL_AGENT=cc claude"

    page.fill("#bridge-body textarea", "有依赖，scheduler 里同步调的")
    page.click("#bridge-body button")
    page.wait_for_selector("#bridge-hint.ok", timeout=15000)

    # 在页面上回的那句，落成的还是 outbox 里那个文件 —— 剩下的路和手写的完全一样，
    # 由 bridge adapter 发出去（那段另有测试，这里不起 agentd，所以队列不会自己清空）。
    draft = (
        NodeLayout(tmp_path / "ws").agent_dir("cc")
        / "bridge"
        / "outbox"
        / "01KZ000000000000000000000A.md"
    )
    assert draft.is_file()
    assert "scheduler" in draft.read_text(encoding="utf-8")
    assert errors == []
    page.close()


def test_a_node_without_a_bridge_agent_hides_the_tab(browser: object, panel: str) -> None:
    """没有桥接 Agent 的节点不该看见一个点开全是空的标签页。"""
    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)
    page.wait_for_timeout(600)

    assert page.is_hidden('.tab[data-pane="bridge"]')
    assert errors == []
    page.close()


def test_a_working_coordinator_can_be_built_without_touching_a_terminal(
    browser: object, panel: str, tmp_path: Path
) -> None:
    """M10 那句「装好就能用，单机不必开终端」的真验收。

    以前这条路在浏览器里是断的：「加一个 Agent」表单没有 role（建不出 coordinator），
    而选 provider 大脑又要求 [providers.*] 已配好，面板却没有任何地方能配它。
    """
    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)

    # 1) 配一个 provider（选预设，省得手填 base_url）
    page.click('.tab[data-pane="models"]')
    page.wait_for_selector("#provider-form", timeout=15000)
    page.select_option("#provider-preset", "deepseek")
    page.click('#provider-form button[type="submit"]')
    page.wait_for_selector("#models-body .waiting", timeout=15000)
    assert "缺密钥" in page.text_content("#models-body")

    # 2) 存密钥
    page.fill("#secret-name", "DEEPSEEK_API_KEY")
    page.fill("#secret-value", "sk-from-the-browser")
    page.click('#secret-form button[type="submit"]')
    page.wait_for_function(
        "() => document.getElementById('models-body').textContent.includes('密钥已就绪')",
        timeout=15000,
    )

    # 3) 建一个真正的 coordinator
    page.fill("#agent-name", "boss")
    page.select_option("#agent-role", "coordinator")
    page.select_option("#agent-brain", "provider")
    page.fill("#agent-extra", "deepseek")
    page.click('#add-agent button[type="submit"]')
    page.wait_for_selector('#topo-body .card:has-text("boss")', timeout=15000)

    text = (tmp_path / "ws" / ".anthill" / "node.toml").read_text(encoding="utf-8")
    assert 'role = "coordinator"' in text
    assert "sk-from-the-browser" not in text, "密钥绝不能写进 node.toml"
    assert errors == []
    page.close()


def test_clearing_the_workspace_list_from_the_page(
    browser: object, panel: str, tmp_path: Path
) -> None:
    """清单里攒垃圾是常态（跑过的临时目录、试着建了又不要的、别处删掉了的），
    一条条点太蠢。但**只清清单，不删文件** —— 网页上的一次误点没有 undo。
    """
    junk = tmp_path / "junk"
    create_workspace(NodeLayout(junk), node_name="junkbox")
    registry = tmp_path / "home" / ".anthill" / "workspaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps([{"path": str(junk), "port": 45778}], ensure_ascii=False), encoding="utf-8"
    )

    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)
    page.on("dialog", lambda d: d.accept())

    page.click('.tab[data-pane="setup"]')
    page.wait_for_selector("#ws-clear-all", timeout=15000)
    page.click("#ws-clear-all")
    page.wait_for_selector("#ws-clear-hint.ok", timeout=15000)

    assert "移除" in page.text_content("#ws-clear-hint")
    assert (junk / ".anthill" / "node.toml").is_file(), "只清清单的那个按钮不该删文件"
    # 本进程正照看的那个必须还在 —— 把自己踢掉，面板下一秒就找不着自己了
    assert (tmp_path / "ws" / ".anthill" / "node.toml").is_file()
    page.wait_for_selector("#ws-list .ws", timeout=15000)
    assert errors == []
    page.close()


def test_purging_workspaces_from_the_page_spares_the_current_one(
    browser: object, panel: str, tmp_path: Path
) -> None:
    """「连目录一起删」那个按钮真的会删文件，所以在真浏览器里走一遍。

    验的是两条不能破的：**本进程正照看的那个必须活下来**（删掉它面板下一秒
    就找不着自己了），以及**只删 .anthill/**，人自己放在那儿的东西不动。
    """
    # 造一个「别的工作区」，并让 serve 那边的清单认得它
    junk = tmp_path / "junk"
    create_workspace(NodeLayout(junk), node_name="junkbox")
    keepsake = junk / "我自己的笔记.md"
    keepsake.write_text("别删我", encoding="utf-8")
    registry = tmp_path / "home" / ".anthill" / "workspaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps([{"path": str(junk), "port": 45778}], ensure_ascii=False), encoding="utf-8"
    )

    page, errors = open_panel(browser, panel)
    page.wait_for_selector("#topo-body .card", timeout=15000)
    page.on("dialog", lambda d: d.accept())  # 两道确认都点是

    page.click('.tab[data-pane="setup"]')
    page.wait_for_selector("#ws-purge-all", timeout=15000)
    page.click("#ws-purge-all")
    page.wait_for_selector("#ws-clear-hint.ok", timeout=15000)

    assert not (junk / ".anthill").exists(), "该删的没删掉"
    assert keepsake.read_text(encoding="utf-8") == "别删我", "只该删 .anthill/"
    assert (tmp_path / "ws" / ".anthill" / "node.toml").is_file(), "把自己删了"
    assert errors == []
    page.close()


@pytest.fixture
def two_workspaces(tmp_path: Path) -> Iterator[str]:
    """一个 serve 照看两个工作区 —— 这是「怎么切换」那些 bug 的现场。"""
    home = tmp_path / "home"
    (home / ".anthill").mkdir(parents=True, exist_ok=True)
    first, second = tmp_path / "collab", tmp_path / "collab-tst"
    create_workspace(NodeLayout(first), node_name="collab")
    create_workspace(NodeLayout(second), node_name="collab-tst")
    (home / ".anthill" / "workspaces.json").write_text(
        json.dumps([{"path": str(second), "port": 45778}]), encoding="utf-8"
    )
    yield from serve(first, home=home)


def test_both_workspaces_show_up_and_can_be_switched(browser: object, two_workspaces: str) -> None:
    """侧栏把本机每个工作区都摆出来，点一下就切过去并展开。

    这里一次盯住三个真出过的问题：

    1. 点了「切到这个」界面纹丝不动 —— 顶栏的名字读的是服务端的**主节点**，
       行上的高亮也是服务端按主节点算的，都不看客户端的焦点；
    2. 第二个本机节点被显示成「连不上的对端」—— 总控视图合并时先到先得，
       而主节点的 peers 里有一条同名记录（同机器的另一个工作区被组播「发现」了）；
    3. WebSocket 每 2 秒把别的本机节点抹掉 —— 那段过滤写的是「留下所有非本机的」。
    """
    page, errors = open_panel(browser, two_workspaces)
    page.wait_for_selector("#topo-body .node-group", timeout=15000)

    heads = page.query_selector_all("[data-focus-node]")
    names = sorted(h.get_attribute("data-focus-node") for h in heads)
    assert names == ["collab", "collab-tst"], "两个工作区都该摆在侧栏里，且都能点"
    assert "连不上" not in page.inner_text("#topo-body"), "本机的另一个工作区被当成对端了"

    assert page.text_content("#node") == "collab"
    next(h for h in heads if h.get_attribute("data-focus-node") == "collab-tst").click()
    page.wait_for_function(
        "() => document.getElementById('node').textContent === 'collab-tst'", timeout=15000
    )

    # 展开的是切过去的那个（它下面才有「加一个 Agent」）
    focused = page.query_selector(".node-group.focus")
    assert focused is not None and "collab-tst" in focused.inner_text()

    # WS 推几轮之后两个都还在 —— 以前第二个会被推送抹掉
    page.wait_for_timeout(5000)
    assert len(page.query_selector_all("[data-focus-node]")) == 2
    assert page.text_content("#node") == "collab-tst", "焦点被推送覆盖了"
    assert errors == []
    page.close()


@pytest.fixture
def bridge_in_second(tmp_path: Path) -> Iterator[str]:
    """两个工作区，bridge Agent 在**第二个**里。"""
    home = tmp_path / "home"
    (home / ".anthill").mkdir(parents=True, exist_ok=True)
    first, second = tmp_path / "collab", tmp_path / "collab-tst"
    create_workspace(NodeLayout(first), node_name="collab")
    layout = NodeLayout(second)
    create_workspace(layout, node_name="collab-tst")
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8")
        + '\n[agents.cc]\nrole = "worker"\nbridge = true\n',
        encoding="utf-8",
    )
    (home / ".anthill" / "workspaces.json").write_text(
        json.dumps([{"path": str(second), "port": 45778}]), encoding="utf-8"
    )
    yield from serve(first, home=home)


def test_the_bridge_tab_follows_the_focused_workspace(
    browser: object, bridge_in_second: str
) -> None:
    """桥接 Agent 配在**第二个**工作区里时，切过去之后那个标签页必须出现。

    以前 `bridgeAgents()` 写的是 `find(n => n.local)` —— 一台机器照看好几个工作区时，
    那句拿到的是「第一个」，不是你切过去的那个。于是标签页一直不出现，
    而人在页面上明明已经切过去了。待审批那一格是同一个写法，一起修的。
    """
    page, errors = open_panel(browser, bridge_in_second)
    page.wait_for_selector("#topo-body .node-group", timeout=15000)

    assert page.is_hidden('.tab[data-pane="bridge"]'), "第一个工作区没有 bridge Agent"

    page.click('[data-focus-node="collab-tst"]')
    page.wait_for_function(
        "() => document.getElementById('node').textContent === 'collab-tst'", timeout=15000
    )
    page.wait_for_selector('.tab[data-pane="bridge"]:not([hidden])', timeout=15000)

    page.click('.tab[data-pane="bridge"]')
    page.click("#bridge-connect > summary")
    page.wait_for_selector("#rcp-mcp", timeout=15000)
    assert "collab-tst" in page.text_content("#rcp-mcp"), "命令指向了别的工作区"
    assert "ANTHILL_AGENT=cc" in page.text_content("#rcp-pin")
    assert errors == []
    page.close()
