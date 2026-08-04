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


def test_switching_workspace_at_runtime_is_refused(tmp_path: Path) -> None:
    """只支持从「没有」到「有」这一次。

    peers 与密钥是跟着工作区走的，中途换等于换身份 ——
    已经跑着的 agentd、已经建立的信任关系全都对不上。想换就重启一次 serve。
    """
    first = tmp_path / "a"
    config = create_workspace(NodeLayout(first), node_name="a")
    ctx = NodeContext(NodeLayout(first), config, PeerRegistry(NodeLayout(first).root))

    with pytest.raises(AntHillError, match="重启"):
        ctx.adopt(NodeLayout(tmp_path / "b"))


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
