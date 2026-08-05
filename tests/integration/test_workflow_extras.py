"""任务模板、通知、定时触发 —— 三件「跑完之后就没下文」的事。

- 每次都得重新用自然语言描述目标，跑得好的一次没法存下来复用；
- 任务跑完不会告诉你（无通知、无 webhook）；
- 不能定时或按事件触发。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from anthill.cli.main import app
from anthill.core.config import Config, NotifySection
from anthill.core.errors import ConfigError
from anthill.core.ids import new_id, new_thread_id
from anthill.core.logging import EventLog
from anthill.core.paths import NodeLayout
from anthill.orchestrator.notify import notify, payload_for
from anthill.orchestrator.plan import Plan
from anthill.orchestrator.state import RunState

runner = CliRunner()

EXTRAS = """
[templates.review]
goal = "审一遍 {arg} 的改动，重点看边界和错误处理"
describe = "代码审查"

[schedules.nightly]
every = 3600
template = "review"
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"]).exit_code == 0
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(toml.read_text(encoding="utf-8") + EXTRAS, encoding="utf-8")
    return tmp_path


def make_state(*, failed: bool = False) -> RunState:
    plan = Plan.model_validate(
        {
            "goal": "补单测",
            "steps": [{"id": "s1", "assignee": "coder", "task": "写"}],
            "done_when": "",
        }
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="box:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    state = state.fail("s1", error="炸了") if failed else state.complete("s1", summary="好了")
    return state.finish(summary="收工")


# ---------- 模板 ----------


def test_a_template_is_reusable(workspace: Path) -> None:
    config = Config.load_from(NodeLayout(workspace))

    assert config.templates["review"].goal.startswith("审一遍")


def test_an_unknown_template_lists_the_real_ones(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "--template", "nope", "x", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "review" in result.output


def test_running_with_neither_task_nor_template_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "-w", str(workspace)])

    assert result.exit_code != 0
    assert "--template" in result.output


# ---------- 定时 ----------


def test_a_schedule_can_reference_a_template(workspace: Path) -> None:
    config = Config.load_from(NodeLayout(workspace))

    assert config.schedules["nightly"].template == "review"
    assert config.schedules["nightly"].every == 3600


def test_a_schedule_pointing_at_a_missing_template_is_caught_at_load(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"])
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[schedules.x]\nevery = 60\ntemplate = "ghost"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ghost"):
        Config.load_from(NodeLayout(tmp_path))


def test_a_schedule_with_nothing_to_run_is_refused(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path), "--node-name", "box"])
    toml = NodeLayout(tmp_path).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8") + "\n[schedules.x]\nevery = 60\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="task"):
        Config.load_from(NodeLayout(tmp_path))


# ---------- 通知 ----------


async def test_nothing_is_sent_when_no_webhook_is_configured() -> None:
    """默认全关 —— 一个会自己往外发 HTTP 的框架，得是用户明确要的。"""
    sent = await notify(make_state(), NotifySection(), EventLog(None, agent="t", echo=False))

    assert sent is False


async def test_a_webhook_gets_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            seen.append({"url": url, "body": json})
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("anthill.orchestrator.notify.peer_client", lambda t: FakeClient())

    sent = await notify(
        make_state(),
        NotifySection(webhook="https://example.test/hook"),
        EventLog(None, agent="t", echo=False),
    )

    assert sent is True
    assert seen[0]["body"]["ok"] is True  # type: ignore[index]
    assert seen[0]["body"]["goal"] == "补单测"  # type: ignore[index]


async def test_failure_only_skips_the_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("anthill.orchestrator.notify.peer_client", lambda t: pytest.fail("不该发"))

    sent = await notify(
        make_state(),
        NotifySection(webhook="https://example.test/hook", on_failure_only=True),
        EventLog(None, agent="t", echo=False),
    )

    assert sent is False


async def test_a_broken_webhook_does_not_look_like_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通知发不出去不该让一次已经成功的协作看起来像失败了 —— 那是两件事。"""

    class Exploding:
        async def __aenter__(self) -> Exploding:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, object]) -> httpx.Response:
            raise httpx.ConnectError("连不上")

    monkeypatch.setattr("anthill.orchestrator.notify.peer_client", lambda t: Exploding())

    sent = await notify(
        make_state(),
        NotifySection(webhook="https://example.test/hook"),
        EventLog(None, agent="t", echo=False),
    )

    assert sent is False  # 只记日志，不抛


def test_the_payload_says_whether_it_went_well() -> None:
    assert payload_for(make_state())["ok"] is True
    assert payload_for(make_state(failed=True))["ok"] is False


# ---------- --json 补齐 ----------


@pytest.mark.parametrize(
    ("argv", "key"),
    [
        (["status"], "agents"),
        (["agent", "list"], "agents"),
        (["peers", "list"], "peers"),
        (["dead", "list", "cli"], "dead"),
        (["runs"], "runs"),
    ],
)
def test_every_read_command_can_speak_json(workspace: Path, argv: list[str], key: str) -> None:
    """全项目此前没有任何 --json 输出，CI 里想判断结果做不到。"""
    result = runner.invoke(app, [*argv, "--json", "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    assert key in json.loads(result.output)
