"""M10 之后新能力只进了面板，CLI 停在最初那批命令。

远端启停、加删 Agent、后台起进程、多工作区管理 —— 面板全有，CLI 全无。
对一个日常在 tmux 里干活的人来说，停 agent、看历史任务、看花销这几件事
只能靠 `ls`、`jq` 或者去开浏览器。这个文件覆盖补回来的那几条。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.config import Config
from anthill.core.paths import NodeLayout
from anthill.core.workspace import ensure_mailboxes

runner = CliRunner()

WITH_BRAIN = """
[providers.fake]
kind = "openai_compat"
base_url = "https://example.test"
api_key_env = "FAKE_KEY"
model = "fake-1"
price_in = 1.0
price_out = 2.0

[agents.boss]
role = "coordinator"
provider = "fake"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"]).exit_code == 0
    layout = NodeLayout(tmp_path)
    layout.node_toml.write_text(
        layout.node_toml.read_text(encoding="utf-8") + WITH_BRAIN, encoding="utf-8"
    )
    # 手改 node.toml 不会建邮箱 —— 这正是 doctor 会点名的那件事，
    # 这里补上，好让那几条用例测的是别的东西
    ensure_mailboxes(layout, Config.load_from(layout))
    return tmp_path


# ---------- 停进程 ----------


def test_stopping_something_that_is_not_running_is_not_an_error(workspace: Path) -> None:
    result = runner.invoke(app, ["agent", "stop", "echo", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "没在跑" in result.output


def test_stopping_an_unknown_agent_lists_the_real_ones(workspace: Path) -> None:
    result = runner.invoke(app, ["agent", "stop", "ghost", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "echo" in result.output


def test_ps_works_even_outside_a_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """它问的是「这台机器上」，所以当前目录没有工作区也不该报错。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["agent", "ps"])

    assert result.exit_code == 0


# ---------- 历史任务 ----------


def test_runs_says_so_when_there_is_nothing(workspace: Path) -> None:
    result = runner.invoke(app, ["runs", "-w", str(workspace)])

    assert result.exit_code == 0
    assert "还没有任何编排任务" in result.output


def test_runs_emits_machine_readable_json(workspace: Path) -> None:
    """全项目此前没有任何 --json 输出，CI 里没法判断结果。"""
    result = runner.invoke(app, ["runs", "-w", str(workspace), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"runs": []}


def test_an_unknown_run_is_an_actionable_error(workspace: Path) -> None:
    result = runner.invoke(app, ["runs", "ZZZZZZ", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "anthill runs" in result.output


# ---------- 花销 ----------


def test_cost_reads_the_numbers_that_were_already_being_logged(workspace: Path) -> None:
    """token 用量一直有人算、有人写日志，就是没人汇总 —— 数据在最后一米被丢掉。"""
    log = NodeLayout(workspace).log_file("boss")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in (
                {
                    "event": "task.done",
                    "agent": "boss",
                    "in_tokens": 1000,
                    "out_tokens": 500,
                    "model": "fake-1",
                },
                {
                    "event": "task.done",
                    "agent": "boss",
                    "in_tokens": 2000,
                    "out_tokens": 250,
                    "model": "fake-1",
                },
                {"event": "msg.received", "agent": "boss"},  # 不是用量，别算进去
            )
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["cost", "-w", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agents"]["boss"]["tasks"] == 2
    assert data["total_tokens"] == 3750
    # 单价来自 [providers.*]：1.0/百万 输入 + 2.0/百万 输出
    assert data["agents"]["boss"]["cost"] == pytest.approx((3000 * 1.0 + 750 * 2.0) / 1_000_000)


def test_cost_does_not_invent_a_price(workspace: Path) -> None:
    """没标价就只报 token —— 写死在代码里的价格迟早过期，而过期的价格比没有更糟。"""
    toml = NodeLayout(workspace).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8")
        .replace("price_in = 1.0", "price_in = 0.0")
        .replace("price_out = 2.0", "price_out = 0.0"),
        encoding="utf-8",
    )
    log = NodeLayout(workspace).log_file("boss")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"event": "task.done", "agent": "boss", "in_tokens": 10, "model": "fake-1"}),
        encoding="utf-8",
    )

    data = json.loads(runner.invoke(app, ["cost", "-w", str(workspace), "--json"]).output)

    assert data["agents"]["boss"]["cost"] is None


def test_cost_survives_a_corrupt_log_line(workspace: Path) -> None:
    log = NodeLayout(workspace).log_file("boss")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "这不是 json\n" + json.dumps({"event": "task.done", "in_tokens": 5}), encoding="utf-8"
    )

    assert runner.invoke(app, ["cost", "-w", str(workspace)]).exit_code == 0


# ---------- 体检 ----------


def test_doctor_flags_a_missing_api_key(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: workspace / "home"))

    result = runner.invoke(app, ["doctor", "-w", str(workspace)])

    assert result.exit_code == 1
    assert "FAKE_KEY" in result.output


def test_doctor_flags_a_brainless_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认模板里的 coordinator 就是这样 —— 这正是「假装成功」的根源。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    runner.invoke(app, ["init", str(tmp_path), "--node-name", "plain"])

    result = runner.invoke(app, ["doctor", "-w", str(tmp_path)])

    assert result.exit_code == 1
    assert "没有大脑" in result.output


def test_doctor_is_happy_when_everything_is_configured(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_KEY", "sk-x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: workspace / "home"))

    result = runner.invoke(app, ["doctor", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "没有阻断性问题" in result.output


# ---------- 命令地图 ----------


def test_the_command_map_is_reachable(workspace: Path) -> None:
    """它以前是模块 docstring —— `--help` 里看不到，而它正是新用户最需要的。"""
    result = runner.invoke(app, ["guide"])

    assert result.exit_code == 0
    assert "agent ps" in result.output and "dead" in result.output


def test_help_points_at_the_map() -> None:
    result = runner.invoke(app, ["--help"])

    assert "anthill guide" in result.output
