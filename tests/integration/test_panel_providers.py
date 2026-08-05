"""在面板上配出一个**能干活**的 coordinator —— M10 那句承诺的最后一米。

M10 立项目标是「装好就能用，单机不必开终端」，实测第一步就破功：

- 「加一个 Agent」表单没有 role 字段，后端默认 worker → **面板永远建不出 coordinator**，
  而发起编排任务恰恰要求有一个 `role="coordinator"` 的 Agent；
- 选 provider 大脑要求 `[providers.*]` 已配好，而面板**没有任何配 provider 的界面**。

于是想在面板上跑通旗舰功能（多 Agent 编排），必须先去配置页手写 TOML。
这个文件把那条路整个走一遍。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from anthill.core.config import Config
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace
from anthill.discovery.registry import PeerRegistry
from anthill.security import secrets
from anthill.web.app import create_app


@pytest.fixture(autouse=True)
def home_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """密钥库落在家目录 —— 测试绝不能写开发者真正的 `~/.anthill/`。

    **还要把 os.environ 复原。** `set_secret` 会顺手写进当前进程的环境
    （那是它该做的：存完立刻生效，不用重启 serve），但在测试里那就是跨用例污染 ——
    上一条用例存的 key 会让下一条用例里本该「缺 key」的分支悄悄变成「有 key」。
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    before = dict(os.environ)
    for name in ("DEEPSEEK_API_KEY", "MY_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def client(tmp_path: Path) -> httpx.AsyncClient:
    layout = NodeLayout(tmp_path / "ws")
    config = create_workspace(layout, node_name="box")
    app = create_app(
        layout=layout,
        config=config,
        peers=PeerRegistry(layout.root),
        log=EventLog(None, agent="serve", echo=False),
        panel=True,
        panel_writable=True,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://panel.test",
    )


DEEPSEEK = {
    "name": "deepseek",
    "kind": "openai_compat",
    "model": "deepseek-chat",
    "api_key_env": "DEEPSEEK_API_KEY",
    "base_url": "https://api.deepseek.com",
}


# ---------- 整条路 ----------


async def test_a_working_coordinator_can_be_built_entirely_from_the_panel(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """验收：配 provider → 存密钥 → 加一个 role=coordinator 的 Agent，全程不碰终端。"""
    async with client as api:
        assert (await api.post("/panel/api/providers", json=DEEPSEEK)).status_code == 201
        assert (
            await api.post(
                "/panel/api/secrets", json={"name": "DEEPSEEK_API_KEY", "value": "sk-test-123"}
            )
        ).status_code == 201
        added = await api.post(
            "/panel/api/agents",
            json={
                "name": "boss",
                "role": "coordinator",
                "brain": "provider",
                "provider": "deepseek",
            },
        )
        listing = (await api.get("/panel/api/providers")).json()

    assert added.status_code == 201, added.text
    fresh = Config.load_from(NodeLayout(tmp_path / "ws"))
    assert fresh.agents["boss"].role == "coordinator", "面板建不出 coordinator = 旗舰功能用不了"
    assert fresh.agents["boss"].provider == "deepseek"
    assert listing["providers"][0]["key_set"] is True


async def test_the_role_actually_comes_from_the_request(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """以前表单不发 role，后端默认 worker —— 建出来的东西看着对，其实不是。"""
    async with client as api:
        await api.post("/panel/api/agents", json={"name": "checker", "role": "reviewer"})

    assert Config.load_from(NodeLayout(tmp_path / "ws")).agents["checker"].role == "reviewer"


# ---------- provider ----------


async def test_a_provider_needs_a_base_url_when_it_is_openai_compatible(
    client: httpx.AsyncClient,
) -> None:
    async with client as api:
        response = await api.post("/panel/api/providers", json={**DEEPSEEK, "base_url": ""})

    assert response.status_code == 400
    assert "base_url" in response.json()["detail"]


async def test_a_provider_in_use_cannot_be_removed_silently(client: httpx.AsyncClient) -> None:
    """删掉之后那些 Agent 一启动就报「provider 不存在」—— 先说清楚，别让人后面去猜。"""
    async with client as api:
        await api.post("/panel/api/providers", json=DEEPSEEK)
        await api.post(
            "/panel/api/agents",
            json={"name": "coder", "brain": "provider", "provider": "deepseek"},
        )
        response = await api.delete("/panel/api/providers/deepseek")

    assert response.status_code == 400
    assert "coder" in response.json()["detail"]


async def test_presets_are_offered_so_nobody_hunts_for_a_base_url(
    client: httpx.AsyncClient,
) -> None:
    async with client as api:
        body = (await api.get("/panel/api/providers")).json()

    assert "deepseek" in body["presets"]
    assert body["presets"]["deepseek"]["base_url"].startswith("https://")


# ---------- 密钥：只进不出 ----------


async def test_a_stored_secret_is_never_readable_through_any_endpoint(
    client: httpx.AsyncClient,
) -> None:
    """这是整件事的安全边界：值只进不出。"""
    async with client as api:
        await api.post("/panel/api/providers", json=DEEPSEEK)
        await api.post(
            "/panel/api/secrets", json={"name": "DEEPSEEK_API_KEY", "value": "sk-super-secret"}
        )
        providers = (await api.get("/panel/api/providers")).text
        config = (await api.get("/panel/api/config")).text

    assert "sk-super-secret" not in providers
    assert "sk-super-secret" not in config


async def test_node_toml_still_only_holds_the_variable_name(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """「配置文件只存环境变量名，绝不存密钥」这条规矩没有因为这一页而松动。"""
    async with client as api:
        await api.post("/panel/api/providers", json=DEEPSEEK)
        await api.post(
            "/panel/api/secrets", json={"name": "DEEPSEEK_API_KEY", "value": "sk-super-secret"}
        )

    text = (NodeLayout(tmp_path / "ws").node_toml).read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" in text
    assert "sk-super-secret" not in text


async def test_the_secrets_file_is_private(client: httpx.AsyncClient) -> None:
    async with client as api:
        await api.post("/panel/api/secrets", json={"name": "MY_KEY", "value": "v"})

    assert secrets.secrets_path().stat().st_mode & 0o077 == 0


async def test_a_secret_takes_effect_without_restarting(client: httpx.AsyncClient) -> None:
    """存完立刻生效 —— 否则「存了却还是说缺密钥」会让人以为没存上。"""
    async with client as api:
        await api.post("/panel/api/secrets", json={"name": "MY_KEY", "value": "v"})

    assert os.environ.get("MY_KEY") == "v"


async def test_a_secret_can_be_removed(client: httpx.AsyncClient) -> None:
    async with client as api:
        await api.post("/panel/api/secrets", json={"name": "MY_KEY", "value": "v"})
        first = await api.delete("/panel/api/secrets/MY_KEY")
        second = await api.delete("/panel/api/secrets/MY_KEY")

    assert first.json()["removed"] is True
    assert second.json()["removed"] is False


async def test_a_real_environment_variable_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经在终端 export 过的不覆盖 —— 否则「我明明换了 key 却不生效」极难查。"""
    secrets.set_secret("MY_KEY", "从面板存的")
    monkeypatch.setenv("MY_KEY", "从终端来的")

    secrets.load_into_env()

    assert os.environ["MY_KEY"] == "从终端来的"


@pytest.mark.parametrize("bad", ["有中文", "WITH SPACE", "A=B", "", "x\ny"])
async def test_a_malformed_variable_name_is_refused(client: httpx.AsyncClient, bad: str) -> None:
    """名字会被写进 `NAME=value` 一行 —— 带 = 或换行的能把这个文件写成别的东西。"""
    async with client as api:
        response = await api.post("/panel/api/secrets", json={"name": bad, "value": "v"})

    assert response.status_code in (400, 422), bad
