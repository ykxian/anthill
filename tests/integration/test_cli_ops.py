"""M10 之后新能力只进了面板，CLI 停在最初那批命令。

远端启停、加删 Agent、后台起进程、多工作区管理 —— 面板全有，CLI 全无。
对一个日常在 tmux 里干活的人来说，停 agent、看历史任务、看花销这几件事
只能靠 `ls`、`jq` 或者去开浏览器。这个文件覆盖补回来的那几条。
"""

from __future__ import annotations

import io
import json
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anthill.cli.common import read_body
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


def _seed_run_with_trace(workspace: Path) -> str:
    """落一条已完成的 run + 三条流水事件，给回放用例当数据。"""
    from anthill.core.ids import new_id, new_thread_id
    from anthill.orchestrator.plan import Plan
    from anthill.orchestrator.state import RunState, RunStore
    from anthill.orchestrator.trace import RunTrace

    layout = NodeLayout(workspace)
    plan = Plan.model_validate(
        {
            "goal": "修好日期解析",
            "steps": [{"id": "s1", "assignee": "boss", "task": "写用例"}],
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
    RunStore(layout.blackboard).save(state)
    trace = RunTrace(layout.blackboard / "tasks" / state.task_id)
    trace.emit("run.started", goal="修好日期解析")
    trace.emit("step.dispatched", step="s1", to="coder", thread="T1", msg="M1")
    trace.emit("run.finished", status="ok")
    return state.task_id


def test_runs_trace_replays_the_event_stream(workspace: Path) -> None:
    """`anthill runs <id> --trace`：全文回放只走 CLI（敏感面纪律的另一半）。"""
    task_id = _seed_run_with_trace(workspace)

    result = runner.invoke(app, ["runs", task_id[-6:], "-w", str(workspace), "--trace"])

    assert result.exit_code == 0
    for kind in ("run.started", "step.dispatched", "run.finished"):
        assert kind in result.output
    assert "s1" in result.output


def test_runs_trace_as_json_emits_the_raw_events(workspace: Path) -> None:
    task_id = _seed_run_with_trace(workspace)

    result = runner.invoke(app, ["runs", task_id, "-w", str(workspace), "--trace", "--json"])

    assert result.exit_code == 0
    events = json.loads(result.output)["events"]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[1]["step"] == "s1"


def test_runs_trace_says_so_when_there_is_no_trace(workspace: Path) -> None:
    """老任务没有流水文件 —— 说清楚，不是报错也不是空输出装哑巴。"""
    from anthill.core.ids import new_id, new_thread_id
    from anthill.orchestrator.plan import Plan
    from anthill.orchestrator.state import RunState, RunStore

    layout = NodeLayout(workspace)
    plan = Plan.model_validate(
        {"goal": "g", "steps": [{"id": "s1", "assignee": "boss", "task": "t"}], "done_when": ""}
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="box:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    RunStore(layout.blackboard).save(state)

    result = runner.invoke(app, ["runs", state.task_id, "-w", str(workspace), "--trace"])

    assert result.exit_code == 0
    assert "没有执行流水" in result.output


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


# ---------- 长正文不必塞进命令行 ----------


def test_a_task_can_come_from_a_file(workspace: Path, tmp_path: Path) -> None:
    """正文以前只能当位置参数传 —— 稍长一点的 prompt 要么被 shell 的引号规则折磨，
    要么根本没法带换行。"""
    brief = tmp_path / "brief.md"
    brief.write_text("第一行\n第二行：给 date.py 补单测", encoding="utf-8")

    assert read_body(f"@{brief}") == "第一行\n第二行：给 date.py 补单测"


def test_a_task_can_come_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("从管道来的任务\n"))

    assert read_body("-") == "从管道来的任务"


def test_a_literal_at_sign_is_escapable() -> None:
    """真正以 @ 开头的正文别被当成文件名。"""
    assert read_body("@@coder 看一下") == "@coder 看一下"


def test_a_missing_file_is_an_actionable_error(tmp_path: Path) -> None:
    import typer

    with pytest.raises(typer.Exit):
        read_body(f"@{tmp_path / '不存在.md'}")


def test_ordinary_text_is_untouched() -> None:
    assert read_body("就是一句话") == "就是一句话"


# ---------- --version ----------


def test_the_long_version_flag_works() -> None:
    """`--version` 是所有人的肌肉记忆，只有 `anthill version` 子命令不够。"""
    for flag in ("--version", "-V"):
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0, flag
        assert "anthill" in result.output


# ---------- help 别被 rich 吃掉 ----------


def test_the_unattended_flag_explains_that_it_is_not_approve_all() -> None:
    """这是个安全开关 —— 中文没有空格，rich 断不了行就直接截断成「…」，
    那句最关键的语义澄清在终端里无论如何都读不到。"""
    result = runner.invoke(app, ["agent", "start", "--help"])

    assert "全部同意" in result.output


def test_square_brackets_survive_in_help() -> None:
    """rich 把中括号当样式标记吃掉，`[node] name` 显示成「里的  name」。"""
    result = runner.invoke(app, ["peers", "invite", "--help"])

    assert "[node]" in result.output


# ---------- 广播哪个地址 ----------


def test_serve_owns_the_signals_so_ctrl_c_is_quiet() -> None:
    """uvicorn 默认会换掉 SIGINT/SIGTERM 的处理器。那样 Ctrl-C 会同时触发
    两条关闭路径（它自己的、和我们的 stop 事件），互相打断，终端上糊一段
    `asyncio.exceptions.CancelledError` —— 看着像崩了，其实是正常退出。

    这个进程还管着信标、状态、拉取、定时四个循环，本来就该由我们统一收尾。
    """
    import uvicorn

    from anthill.cli.serve_cmd import _OwnedServer

    assert issubclass(_OwnedServer, uvicorn.Server)
    server = _OwnedServer(uvicorn.Config(app=lambda *a: None))
    handler_before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is handler_before, "uvicorn 又把信号抢走了"


def test_the_endpoint_from_config_wins_over_guessing(workspace: Path) -> None:
    """自动识别一定会有猜错的时候（多网卡、隧道、VPN），所以两条覆盖路都得留着。"""
    from anthill.core.config import Config

    toml = NodeLayout(workspace).node_toml
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "[node]", '[node]\nendpoint = "http://10.15.3.61:45778"', 1
        ),
        encoding="utf-8",
    )

    assert Config.load_from(NodeLayout(workspace)).node.endpoint == "http://10.15.3.61:45778"


def test_two_inits_on_one_machine_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一台机器上可以有好几个工作区，而节点名必须唯一（信封上的收件人靠它指人）。

    以前两次 `init` 都叫主机名，第二个工作区起 serve 时被跳过，
    只给一句「已经有一个叫 cs 的节点了」—— 用户既没做错什么，也不知道该怎么办。
    `init` 那条路以前还绕过了去重（自己算名字），所以两条路得走同一个入口。
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("socket.gethostname", lambda: "box")

    # 目录名不同 —— 名字直接跟着目录走，一眼看得懂谁是谁。
    # 主机名派生的 `box` / `box-2` 什么也没说，那是只考虑「一台机器一个工作区」时的选择。
    for name in ("collab", "collab-tst"):
        assert runner.invoke(app, ["init", str(tmp_path / name)]).exit_code == 0
    named = [Config.load_from(NodeLayout(tmp_path / n)).node.name for n in ("collab", "collab-tst")]
    assert named == ["collab", "collab-tst"], named

    # 目录名撞了（不同父目录下的同名目录）—— 本机唯一是硬要求，加序号
    twin = tmp_path / "elsewhere" / "collab"
    assert runner.invoke(app, ["init", str(twin)]).exit_code == 0
    assert Config.load_from(NodeLayout(twin)).node.name == "collab-2"


def test_init_registers_the_workspace_so_serve_can_find_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不登记的话，机器级清单里看不见它 —— 面板上也看不见，重名检查也看不见。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    from anthill.web.workspaces import listing

    runner.invoke(app, ["init", str(tmp_path / "solo")])

    assert str(tmp_path / "solo") in [e["path"] for e in listing()]


def test_doctor_flags_a_loosened_security_posture(workspace: Path) -> None:
    """无人值守放宽是安全姿态，巡检必须一眼看到哪台机器开了口子。"""
    layout = NodeLayout(workspace)
    toml = layout.node_toml.read_text(encoding="utf-8")
    # init 模板里已有 [security] 节，键要插进那一节而不是再声明一次
    layout.node_toml.write_text(
        toml.replace("[security]", '[security]\nunattended_allow = ["low", "medium"]', 1),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "-w", str(workspace)])

    assert "unattended_allow" in result.output
    assert "medium" in result.output


def test_doctor_flags_tools_an_agent_cannot_use(workspace: Path) -> None:
    """帽子和工具单打架是配置冲突：静态风险超上限的工具模型根本见不到，
    doctor 要点名，不许静默。"""
    layout = NodeLayout(workspace)
    toml = layout.node_toml.read_text(encoding="utf-8")
    layout.node_toml.write_text(
        toml + '\n[agents.capped]\nrole = "worker"\ntools = ["run_shell", "read_file"]\n'
        'max_tool_risk = "medium"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "-w", str(workspace)])

    assert "capped" in result.output
    assert "run_shell" in result.output


def _seed_finished_failed_run(workspace: Path) -> str:
    """s1 成、s2 败、已收尾的 run —— fork 的主场景数据。"""
    from anthill.core.ids import new_id, new_thread_id
    from anthill.orchestrator.plan import Plan
    from anthill.orchestrator.state import RunState, RunStore
    from anthill.orchestrator.trace import RunTrace

    layout = NodeLayout(workspace)
    plan = Plan.model_validate(
        {
            "goal": "修日期解析",
            "steps": [
                {"id": "s1", "assignee": "boss", "task": "写", "depends_on": []},
                {"id": "s2", "assignee": "boss", "task": "审", "depends_on": ["s1"]},
            ],
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
    state = state.dispatch("s1", thread=new_thread_id(), msg_id=new_id())
    state = state.complete("s1", summary="写完了")
    state = state.dispatch("s2", thread=new_thread_id(), msg_id=new_id())
    state = state.fail("s2", error="炸了")
    state = state.finish(summary="步骤 s2 失败")
    RunStore(layout.blackboard).save(state)
    trace = RunTrace(layout.blackboard / "tasks" / state.task_id)
    trace.emit("run.started")
    trace.emit("run.finished", status="error")
    return state.task_id


def test_fork_creates_a_new_pending_run_with_provenance(workspace: Path) -> None:
    from anthill.orchestrator.state import RunStore, StepState
    from anthill.orchestrator.trace import read_trace

    task_id = _seed_finished_failed_run(workspace)
    layout = NodeLayout(workspace)

    result = runner.invoke(app, ["runs", task_id[-6:], "-w", str(workspace), "--fork-from", "s2"])

    assert result.exit_code == 0, result.output
    states = {s.task_id: s for s in RunStore(layout.blackboard).all()}
    assert len(states) == 2
    forked = next(s for s in states.values() if s.task_id != task_id)
    assert not forked.finished
    assert forked.step("s1").state is StepState.DONE
    assert forked.step("s2").state is StepState.PENDING
    first = read_trace(layout.blackboard / "tasks" / forked.task_id)[0]
    assert first["kind"] == "forked_from"
    assert first["task"] == task_id
    assert first["step"] == "s2"
    assert first["source_seq"] == 2  # 源流水落笔处，纯出处记录
    assert first["seq"] == 1  # 本事件在新流水里的序号，别和出处混了


def test_fork_refuses_a_run_that_is_still_going(workspace: Path) -> None:
    """v1 红线：活 run 上 fork = 同一批 worker 双份活，直接拒。"""
    from anthill.core.ids import new_id, new_thread_id
    from anthill.orchestrator.plan import Plan
    from anthill.orchestrator.state import RunState, RunStore

    layout = NodeLayout(workspace)
    plan = Plan.model_validate(
        {"goal": "g", "steps": [{"id": "s1", "assignee": "boss", "task": "t"}], "done_when": ""}
    )
    state = RunState.start(
        task_id=new_id(),
        plan=plan,
        requester="box:cli",
        root_thread=new_thread_id(),
        root_msg_id=new_id(),
    )
    RunStore(layout.blackboard).save(state)

    result = runner.invoke(app, ["runs", state.task_id, "-w", str(workspace), "--fork-from", "s1"])

    assert result.exit_code != 0
    assert "已结束" in result.output
    assert len(RunStore(layout.blackboard).all()) == 1, "拒绝时不许留下半个 fork"


def test_fork_of_an_unknown_step_names_the_real_ones(workspace: Path) -> None:
    task_id = _seed_finished_failed_run(workspace)

    result = runner.invoke(app, ["runs", task_id, "-w", str(workspace), "--fork-from", "ghost"])

    assert result.exit_code != 0
    assert "s1" in result.output and "s2" in result.output


def test_doctor_hints_when_config_is_newer_than_running_agentds(workspace: Path) -> None:
    """加人已由路由热感知覆盖、无需重启；大脑/工具/persona 变更才需要 ——
    doctor 用 INFO 轻声提示。不 WARN：面板每加一个人都会动 node.toml，
    报警会狼来了，三天就没人看了。"""
    import json as _json
    import os

    layout = NodeLayout(workspace)
    rt = layout.agent_dir("boss") / "runtime.json"
    rt.parent.mkdir(parents=True, exist_ok=True)
    rt.write_text(
        _json.dumps({"pid": os.getpid(), "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "-w", str(workspace)])

    assert "boss" in result.output
    assert "热感知" in result.output
    assert "无需重启" in result.output
