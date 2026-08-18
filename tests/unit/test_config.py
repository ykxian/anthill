"""03-tech-design §8：配置解析与 fail-fast 校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.config import Config, check_runtime, default_node_toml
from anthill.core.errors import ConfigError
from anthill.core.paths import NodeLayout


def write_config(tmp_path: Path, body: str) -> NodeLayout:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(body, encoding="utf-8")
    return layout


def test_agent_table_key_becomes_agent_name(config):
    assert config.agent("beta").name == "beta"
    assert config.agent("beta").role == "worker"


def test_discovery_is_visible_by_default(config):
    """默认可见 —— 否则同网段两台机器要先手动互相告知地址，太劝退。

    真正要守住的那条线（可见 ≠ 可通信）由 peers/beacon 那一层保证，
    见 test_beacon.py::test_announcement_only_marks_discovered_never_trusted。
    """
    assert config.discovery.enabled is True


def test_discovery_can_still_be_made_completely_silent(tmp_path: Path):
    """想彻底隐身的路一直留着，而且是真的零发包零监听。"""
    layout = write_config(tmp_path, '[node]\nname = "quiet"\n[discovery]\nenabled = false\n')

    assert Config.load_from(layout).discovery.enabled is False


def test_default_template_is_valid(tmp_path: Path):
    layout = write_config(tmp_path, default_node_toml("laptop-ykx"))

    loaded = Config.load_from(layout)

    assert loaded.node.name == "laptop-ykx"
    assert "cli" in loaded.agents


def test_missing_config_says_how_to_fix(tmp_path: Path):
    with pytest.raises(ConfigError, match="anthill init"):
        Config.load(tmp_path / "node.toml")


def test_unknown_provider_reference_is_rejected(tmp_path: Path):
    layout = write_config(
        tmp_path,
        """
[node]
name = "n1"
[agents.coder]
role = "coder"
provider = "ghost"
""",
    )

    with pytest.raises(ConfigError, match="ghost"):
        Config.load_from(layout)


def test_unknown_section_is_rejected(tmp_path: Path):
    layout = write_config(tmp_path, '[node]\nname = "n1"\n[nonsense]\nx = 1\n')

    with pytest.raises(ConfigError):
        Config.load_from(layout)


def test_ssh_peer_requires_host_and_workspace(tmp_path: Path):
    layout = write_config(
        tmp_path,
        """
[node]
name = "n1"
[peers.lab]
transport = "ssh"
""",
    )

    with pytest.raises(ConfigError, match="remote_workspace"):
        Config.load_from(layout)


def test_unknown_agent_lookup_lists_known_ones(config):
    with pytest.raises(ConfigError, match="alpha"):
        config.agent("nobody")


def test_check_runtime_requires_api_key_env(tmp_path: Path, monkeypatch):
    layout = write_config(
        tmp_path,
        """
[node]
name = "n1"
[providers.deepseek]
kind = "openai_compat"
api_key_env = "TEST_MISSING_KEY"
model = "deepseek-chat"
[agents.coder]
role = "coder"
provider = "deepseek"
""",
    )
    config = Config.load_from(layout)
    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)

    with pytest.raises(ConfigError, match="TEST_MISSING_KEY"):
        check_runtime(config, layout, "coder")

    monkeypatch.setenv("TEST_MISSING_KEY", "sk-test")
    check_runtime(config, layout, "coder")  # 设置后即通过


def test_check_runtime_passes_for_echo_agent(config, layout):
    check_runtime(config, layout, "beta")

    assert layout.mailbox_dir("beta").is_dir()


def test_agents_with_role(config):
    assert sorted(a.name for a in config.agents_with_role("worker")) == ["beta", "gamma"]


def test_layout_discovery_walks_up(layout: NodeLayout):
    nested = layout.workspace / "src" / "deep"
    nested.mkdir(parents=True)

    assert NodeLayout.discover(nested).workspace == layout.workspace


def test_layout_discovery_fails_outside_workspace(tmp_path: Path):
    with pytest.raises(ConfigError, match="anthill init"):
        NodeLayout.discover(tmp_path)


def test_unattended_allow_accepts_the_loosenable_tiers(tmp_path: Path):
    layout = write_config(
        tmp_path,
        '[node]\nname = "n1"\n[agents.cli]\nrole = "user"\n'
        '[security]\nunattended_allow = ["low", "medium"]\n',
    )

    assert Config.load_from(layout).security.unattended_allow == ("low", "medium")


def test_unattended_allow_rejects_high(tmp_path: Path):
    """红线：high 永远到不了免确认 —— 配置层就得把这扇门焊死。"""
    layout = write_config(
        tmp_path,
        '[node]\nname = "n1"\n[agents.cli]\nrole = "user"\n'
        '[security]\nunattended_allow = ["high"]\n',
    )

    with pytest.raises(ConfigError, match="high"):
        Config.load_from(layout)


def test_unattended_allow_rejects_typos_instead_of_ignoring_them(tmp_path: Path):
    """拼错的档位静默忽略 = 配置没生效还不报 —— 必须炸在启动期。"""
    layout = write_config(
        tmp_path,
        '[node]\nname = "n1"\n[agents.cli]\nrole = "user"\n'
        '[security]\nunattended_allow = ["hgih"]\n',
    )

    with pytest.raises(ConfigError, match="hgih"):
        Config.load_from(layout)
