"""还没配工作区时的那一步，以及这台机器上的工作区清单。

核心约定只有一条：**没被告知之前，磁盘一个字都不写。**
上一版 serve 撞上空目录会就地建一个 —— 装好就能用是对的，
但「就地」这个决定不该由程序替人做。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from anthill.core.errors import AntHillError
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace
from anthill.discovery.registry import PeerRegistry
from anthill.web.app import create_app
from anthill.web.context import NodeContext
from anthill.web.setup import browse, is_workspace
from anthill.web.workspaces import clear, listing, remember


@pytest.fixture
def unconfigured() -> object:
    """一个还没有工作区的 serve。"""
    return create_app(
        log=EventLog(None, agent="serve", echo=False), panel=True, panel_writable=True
    )


@pytest.fixture(autouse=True)
def registry_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清单落在 `~/.anthill/` —— 测试别去碰真的 home。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def client(app: object, *, host: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)),  # type: ignore[arg-type]
        base_url="http://panel.test",
    )


# ---------- 没被告知之前不写盘 ----------


async def test_an_unconfigured_node_serves_the_panel_but_nothing_else(
    unconfigured: object,
) -> None:
    """节点端点一律 503 —— 说清楚是「还没配好」，不是崩了。"""
    async with client(unconfigured) as api:
        assert (await api.get("/panel")).status_code == 200
        assert (await api.get("/health")).status_code == 503
        assert (await api.post("/deliver", json={})).status_code == 503
        assert (await api.get("/panel/api/setup")).json()["ready"] is False


async def test_starting_unconfigured_creates_nothing(tmp_path: Path, unconfigured: object) -> None:
    async with client(unconfigured) as api:
        await api.get("/panel/api/setup")
        await api.get(f"/panel/api/setup/browse?path={tmp_path}")

    assert list(tmp_path.iterdir()) == []


# ---------- 目录浏览器 ----------


def test_browse_lists_directories_and_flags_workspaces(tmp_path: Path) -> None:
    (tmp_path / "空的").mkdir()
    (tmp_path / "已经是工作区").mkdir()
    create_workspace(NodeLayout(tmp_path / "已经是工作区"), node_name="old")

    result = browse(str(tmp_path))

    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["空的"]["is_workspace"] is False
    assert by_name["已经是工作区"]["is_workspace"] is True
    assert result["parent"] == str(tmp_path.parent)


def test_browse_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "一个文件"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(AntHillError, match="不是一个目录"):
        browse(str(target))


def test_browse_hides_dotfiles(tmp_path: Path) -> None:
    """挑工作区放哪，不需要看见一堆 .cache/.git。"""
    (tmp_path / ".隐藏的").mkdir()
    (tmp_path / "看得见").mkdir()

    assert [e["name"] for e in browse(str(tmp_path))["entries"]] == ["看得见"]


# ---------- 认下一个工作区 ----------


async def test_adopting_brings_the_node_to_life_in_place(
    tmp_path: Path, unconfigured: object
) -> None:
    """挑好目录之后，**同一个 serve 进程**就活过来了，不用重启。"""
    target = tmp_path / "新工作区"

    async with client(unconfigured) as api:
        created = await api.post(
            "/panel/api/setup/adopt", json={"path": str(target), "node_name": "box"}
        )
        health = await api.get("/health")
        state = await api.get("/panel/api/state")

    assert created.status_code == 201, created.text
    assert health.status_code == 200
    assert state.json()["node"] == "box"
    assert is_workspace(target)


async def test_adopting_an_existing_workspace_does_not_overwrite_it(
    tmp_path: Path, unconfigured: object
) -> None:
    target = tmp_path / "老的"
    create_workspace(NodeLayout(target), node_name="oldnode")

    async with client(unconfigured) as api:
        await api.post("/panel/api/setup/adopt", json={"path": str(target), "node_name": "newname"})
        state = await api.get("/panel/api/state")

    assert state.json()["node"] == "oldnode"


def test_two_workspaces_cannot_share_a_node_name(tmp_path: Path) -> None:
    """一个进程可以照看好几个节点 —— 但名字必须能区分开。

    收件人写在信封上（`to.node`），两个工作区同名的话，
    一封信该往哪个邮箱放就说不清了。
    """
    from anthill.web.context import NodeRegistry

    for sub in ("a", "b"):
        create_workspace(NodeLayout(tmp_path / sub), node_name="samename")
    registry = NodeRegistry([NodeContext(NodeLayout(tmp_path / "a"))])

    with pytest.raises(AntHillError, match="不能同名"):
        registry.attach(NodeLayout(tmp_path / "b"))


def test_attaching_the_same_workspace_twice_is_a_no_op(tmp_path: Path) -> None:
    from anthill.web.context import NodeRegistry

    create_workspace(NodeLayout(tmp_path / "a"), node_name="a")
    registry = NodeRegistry([NodeContext(NodeLayout(tmp_path / "a"))])

    registry.attach(NodeLayout(tmp_path / "a"))

    assert registry.names() == ["a"]


async def test_setup_is_not_exposed_to_the_network(unconfigured: object) -> None:
    """目录浏览器会列出这台机器的目录结构 —— 只对本机开放。"""
    async with client(unconfigured, host="10.0.8.9") as api:
        assert (await api.get("/panel/api/setup")).status_code == 403
        assert (await api.get("/panel/api/setup/browse")).status_code == 403


# ---------- 工作区清单 ----------


async def test_the_registry_lists_what_this_machine_knows(
    tmp_path: Path, unconfigured: object
) -> None:
    async with client(unconfigured) as api:
        await api.post("/panel/api/setup/adopt", json={"path": str(tmp_path / "一号")})
        await api.post("/panel/api/setup/adopt", json={"path": str(tmp_path / "二号")})
        listed = (await api.get("/panel/api/setup")).json()["workspaces"]

    names = {w["name"] for w in listed}
    assert {"一号", "二号"} <= names
    assert sum(1 for w in listed if w["current"]) == 1  # 只有一个是当前在用的


async def test_removing_from_the_registry_keeps_the_files(
    tmp_path: Path, unconfigured: object
) -> None:
    """默认只是「不再显示」—— 删文件会带走密钥和邮箱，那不该是一次误点的代价。"""
    target = tmp_path / "留着"
    async with client(unconfigured) as api:
        await api.post("/panel/api/setup/adopt", json={"path": str(tmp_path / "当前")})
        await api.post("/panel/api/setup/adopt", json={"path": str(target)})
        response = await api.delete(f"/panel/api/setup/workspace?path={target}")
        listed = (await api.get("/panel/api/setup")).json()["workspaces"]

    assert response.status_code == 200
    assert response.json()["purged"] is False
    assert is_workspace(target)  # 文件还在
    assert target.name not in {w["name"] for w in listed}


async def test_purging_really_deletes_but_only_the_anthill_dir(
    tmp_path: Path, unconfigured: object
) -> None:
    target = tmp_path / "要删的"
    keep = target / "我的代码.py"
    async with client(unconfigured) as api:
        await api.post("/panel/api/setup/adopt", json={"path": str(tmp_path / "当前")})
        await api.post("/panel/api/setup/adopt", json={"path": str(target)})
        keep.write_text("print('别删我')", encoding="utf-8")
        response = await api.delete(f"/panel/api/setup/workspace?path={target}&purge=true")

    assert response.status_code == 200
    assert not is_workspace(target)
    assert keep.is_file()  # 只删 .anthill/，不碰你放在那儿的东西


async def test_the_workspace_in_use_cannot_be_deleted(tmp_path: Path, unconfigured: object) -> None:
    target = tmp_path / "当前"
    async with client(unconfigured) as api:
        await api.post("/panel/api/setup/adopt", json={"path": str(target)})
        response = await api.delete(f"/panel/api/setup/workspace?path={target}&purge=true")

    assert response.status_code == 400
    assert is_workspace(target)


async def test_a_broken_registry_file_is_survivable(tmp_path: Path, unconfigured: object) -> None:
    """清单坏了顶多是「面板上少列几个」，不该让面板打不开。"""
    registry = tmp_path / "home" / ".anthill" / "workspaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("这不是 JSON", encoding="utf-8")

    async with client(unconfigured) as api:
        response = await api.get("/panel/api/setup")

    assert response.status_code == 200
    assert response.json()["workspaces"] == []


def test_the_registry_does_not_live_inside_any_workspace(tmp_path: Path) -> None:
    """它记的是「这台机器上有哪些工作区」—— 放进其中一个就本末倒置了。"""
    from anthill.web.workspaces import registry_path

    assert registry_path() == tmp_path / "home" / ".anthill" / "workspaces.json"
    assert json.loads("[]") == []


async def test_an_illegal_node_name_leaves_no_half_built_workspace(
    tmp_path: Path, unconfigured: object
) -> None:
    """先验名字再动盘 —— 否则会留下一个「看着像工作区、其实起不来」的目录。"""
    target = tmp_path / "会失败的"

    async with client(unconfigured) as api:
        response = await api.post(
            "/panel/api/setup/adopt", json={"path": str(target), "node_name": "中文名字"}
        )

    assert response.status_code == 400
    assert "非法节点名" in response.json()["detail"]
    assert not is_workspace(target)


# ---------- 一个进程照看多个节点 ----------


def two_nodes(tmp_path: Path) -> object:
    """一个 serve、一个端口、两个工作区。"""
    from anthill.web.context import NodeContext, NodeRegistry

    contexts = []
    for name in ("boxa", "boxb"):
        layout = NodeLayout(tmp_path / name)
        create_workspace(layout, node_name=name)
        contexts.append(NodeContext(layout))
    registry = NodeRegistry(contexts)
    return create_app(
        registry=registry,
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
    )


async def test_one_process_announces_every_node_it_looks_after(tmp_path: Path) -> None:
    async with client(two_nodes(tmp_path)) as api:
        health = (await api.get("/health")).json()

    assert health["node"] == "boxa"  # 主节点
    assert [n["node"] for n in health["nodes"]] == ["boxa", "boxb"]


async def test_delivery_is_routed_by_the_recipient_on_the_envelope(tmp_path: Path) -> None:
    """这就是多路复用成立的原因：**路由键本来就写在信封上**。

    一个进程按 `to.node` 分派到对应工作区的邮箱，用**那个节点自己的** peers 验签。
    """
    # Arrange：两个节点各自信任 lab，用的是两把**不同**的钥匙
    from anthill.core.envelope import Address, Envelope
    from anthill.core.ids import now
    from anthill.core.mailbox import Mailbox
    from anthill.core.payloads import ChatPayload, MessageType
    from anthill.security.keys import PairingToken, new_key
    from anthill.security.signing import sign_envelope

    app = two_nodes(tmp_path)
    keys = {}
    for name in ("boxa", "boxb"):
        keys[name] = new_key()
        PeerRegistry(NodeLayout(tmp_path / name).root).trust(
            PairingToken(node="lab", endpoint="", key=keys[name])
        )

    def envelope(to_node: str, key: bytes) -> Envelope:
        return sign_envelope(
            Envelope(
                from_=Address(node="lab", agent="cli"),
                to=Address(node=to_node, agent="echo"),
                type=MessageType.CHAT,
                thread="01J0000000000000000000000B",
                ts=now(),
                payload=ChatPayload(body=f"给 {to_node}"),
            ),
            key,
        )

    # Act
    async with client(app) as api:
        for name in ("boxa", "boxb"):
            sent = await api.post(
                "/deliver", json=envelope(name, keys[name]).model_dump(mode="json")
            )
            assert sent.status_code == 202, sent.text
        stranger = await api.post(
            "/deliver", json=envelope("boxc", keys["boxa"]).model_dump(mode="json")
        )

    # Assert：各进各的邮箱，一封都没串
    for name in ("boxa", "boxb"):
        box = Mailbox(NodeLayout(tmp_path / name).mailbox_dir("echo"))
        assert len(box.list_new()) == 1
    assert stranger.status_code == 421  # 不认识的节点：不代转


async def test_a_key_that_works_for_one_node_does_not_work_for_another(
    tmp_path: Path,
) -> None:
    """密钥跟着工作区走 —— 验签必须用**收件那个节点**的 peers，不能用主节点的。"""
    from anthill.core.envelope import Address, Envelope
    from anthill.core.ids import now
    from anthill.core.payloads import ChatPayload, MessageType
    from anthill.security.keys import PairingToken, new_key
    from anthill.security.signing import sign_envelope

    app = two_nodes(tmp_path)
    boxa_key = new_key()
    PeerRegistry(NodeLayout(tmp_path / "boxa").root).trust(
        PairingToken(node="lab", endpoint="", key=boxa_key)
    )
    PeerRegistry(NodeLayout(tmp_path / "boxb").root).trust(
        PairingToken(node="lab", endpoint="", key=new_key())
    )

    # 用 boxa 的钥匙签，却寄给 boxb
    forged = sign_envelope(
        Envelope(
            from_=Address(node="lab", agent="cli"),
            to=Address(node="boxb", agent="echo"),
            type=MessageType.CHAT,
            thread="01J0000000000000000000000C",
            ts=now(),
            payload=ChatPayload(body="冒充"),
        ),
        boxa_key,
    )

    async with client(app) as api:
        response = await api.post("/deliver", json=forged.model_dump(mode="json"))

    assert response.status_code == 401


async def test_the_panel_can_address_each_local_node(tmp_path: Path) -> None:
    app = two_nodes(tmp_path)

    async with client(app) as api:
        default = (await api.get("/panel/api/state")).json()
        named = (await api.get("/panel/api/state?node=boxb")).json()
        missing = await api.get("/panel/api/state?node=nope")

    assert default["node"] == "boxa"
    assert named["node"] == "boxb"
    assert missing.status_code == 404


async def test_a_named_summary_is_signed_for_that_node_only(tmp_path: Path) -> None:
    """一台机器上有好几个节点，所以签名里必须带上「问的是哪个」。"""
    from anthill.core.ids import now as _now
    from anthill.security.keys import PairingToken, new_key
    from anthill.security.signing import sign_request

    app = two_nodes(tmp_path)
    key = new_key()
    for name in ("boxa", "boxb"):
        PeerRegistry(NodeLayout(tmp_path / name).root).trust(
            PairingToken(node="lab", endpoint="", key=key)
        )

    def headers(path: str) -> dict[str, str]:
        stamp = _now().isoformat()
        return {
            "X-AntHill-Node": "lab",
            "X-AntHill-Ts": stamp,
            "X-AntHill-Sig": sign_request(key, node="lab", path=path, ts=stamp),
        }

    async with client(app) as api:
        good = await api.get("/node/boxb/summary", headers=headers("/node/boxb/summary"))
        # 拿给 boxb 的签名去问 boxa
        borrowed = await api.get("/node/boxa/summary", headers=headers("/node/boxb/summary"))

    assert good.status_code == 200
    assert good.json()["node"] == "boxb"
    assert borrowed.status_code == 401


def test_a_wildcard_bind_is_never_advertised_as_an_address() -> None:
    """`0.0.0.0` 是「监听哪儿」，不是「别人怎么找到我」。

    真出过：serve 用 --host 0.0.0.0 起，把 `http://0.0.0.0:45778` 广播了出去，
    对端老老实实记下来，然后永远连不上 —— 而且报错很难懂
    （机器上配了代理的话，是一个空的 502）。
    """
    from anthill.cli.serve_cmd import WILDCARD_HOSTS
    from anthill.core.workspace import local_ip

    assert "0.0.0.0" in WILDCARD_HOSTS
    assert local_ip() not in WILDCARD_HOSTS


# ---------- 一键清清单 ----------


def registry_with(entries: list[Path]) -> None:
    for path in entries:
        remember(path, port=45778)


async def test_clearing_the_list_leaves_the_files_alone(tmp_path: Path) -> None:
    """**只动清单，一个文件都不删。**

    一次能带走好几个工作区的邮箱、黑板甚至密钥，而网页上的一次误点没有 undo。
    单个删除那条路仍然在，它一次只毁一个，而且要你先看清是哪一个。
    """
    junk = tmp_path / "junk"
    create_workspace(NodeLayout(junk), node_name="junk")
    registry_with([junk])

    result = clear()

    assert result["removed"] == 1
    assert listing() == []
    assert (junk / ".anthill" / "node.toml").is_file(), "文件不该被删"


async def test_the_workspace_this_process_watches_is_kept(tmp_path: Path) -> None:
    """把自己从清单里踢掉，面板下一秒就找不着自己了 —— 那不是用户要的「清理」。"""
    mine, junk = tmp_path / "mine", tmp_path / "junk"
    for path in (mine, junk):
        create_workspace(NodeLayout(path), node_name=path.name)
    registry_with([mine, junk])

    result = clear(keep=[mine])

    assert result["removed"] == 1
    assert [e["path"] for e in listing()] == [str(mine)]


async def test_stale_only_spares_the_ones_that_still_exist(tmp_path: Path) -> None:
    """最常见也最安全的一次清理：只扫路径已经没了的那些。"""
    alive, gone = tmp_path / "alive", tmp_path / "gone"
    create_workspace(NodeLayout(alive), node_name="alive")
    registry_with([alive, gone])  # gone 从来没建过

    result = clear(stale_only=True)

    assert result["removed"] == 1
    assert [e["path"] for e in listing()] == [str(alive)]


async def test_clearing_an_empty_list_is_not_an_error(tmp_path: Path) -> None:
    assert clear()["removed"] == 0
