"""run 的 append-only 执行流水（trace.jsonl）。

state.json 回答「现在什么样」，trace.jsonl 回答「怎么走到这一步的」。
纪律（与 tst2 对齐的设计决定）：

- 单写者（coordinator）追加，一行一个 JSON，事件必带 seq/ts/kind；
- 读端容忍撕裂的末行 —— 进程在写到一半时被杀是常态，不是异常；
- 写端重启后 seq 接着编，不回卷、不撞号；
- **观察者纪律**：记录失败只能沉默或告警，绝不允许打断调度。
"""

from __future__ import annotations

import json
from pathlib import Path

from anthill.orchestrator.trace import TRACE_FILE, RunTrace, event_count, read_trace


def test_events_append_one_json_per_line(tmp_path: Path) -> None:
    trace = RunTrace(tmp_path)

    trace.emit("run.started", goal="修好日期解析")
    trace.emit("step.dispatched", step="s1", to="coder", thread="T1", msg="M1")
    trace.emit("run.finished", status="ok")

    lines = (tmp_path / TRACE_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    events = [json.loads(line) for line in lines]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert all(e["ts"] for e in events)
    assert [e["kind"] for e in events] == ["run.started", "step.dispatched", "run.finished"]
    assert events[1]["step"] == "s1"
    # dispatch 事件带 msg+thread —— 与 `agent start --record` 的行做 join 的关联键
    assert events[1]["thread"] == "T1"
    assert events[1]["msg"] == "M1"


def test_seq_resumes_after_reopen(tmp_path: Path) -> None:
    """崩溃重启 = 新写者接手同一个文件，seq 必须接着编。"""
    RunTrace(tmp_path).emit("run.started")
    RunTrace(tmp_path).emit("step.dispatched", step="s1")

    events = read_trace(tmp_path)
    assert [e["seq"] for e in events] == [1, 2]


def test_torn_last_line_is_tolerated(tmp_path: Path) -> None:
    """写到一半被杀：末行没有换行、也不是合法 JSON。

    读端跳过它；新写者先把行补断再追加，seq 从最后一个**完整**事件续。
    """
    trace = RunTrace(tmp_path)
    trace.emit("run.started")
    path = tmp_path / TRACE_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "kind": "step.dis')  # 撕裂，无换行

    assert [e["kind"] for e in read_trace(tmp_path)] == ["run.started"]

    RunTrace(tmp_path).emit("step.failed", step="s1")
    events = read_trace(tmp_path)
    assert [e["kind"] for e in events] == ["run.started", "step.failed"]
    assert events[-1]["seq"] == 2


def test_reading_a_missing_trace_is_empty(tmp_path: Path) -> None:
    assert read_trace(tmp_path) == []
    assert event_count(tmp_path) == 0


def test_emit_never_raises_when_the_dir_is_gone(tmp_path: Path) -> None:
    """观察者纪律：trace 记不上不许把 coordinator 拖下水。"""
    gone = tmp_path / "no" / "such" / "dir"
    trace = RunTrace(gone)
    trace.emit("run.started")  # 不该抛
    assert event_count(gone) == 0


def test_event_count_ignores_torn_lines(tmp_path: Path) -> None:
    trace = RunTrace(tmp_path)
    trace.emit("run.started")
    trace.emit("step.dispatched", step="s1")
    with (tmp_path / TRACE_FILE).open("a", encoding="utf-8") as handle:
        handle.write("垃圾不是JSON")

    assert event_count(tmp_path) == 2


def test_detail_cannot_shadow_the_reserved_keys(tmp_path: Path) -> None:
    """seq/ts 是协议字段，调用方的 detail 不许顶掉它们。
    （kind 是位置参数，重复传参在 Python 层直接 TypeError，不用另测。）"""
    RunTrace(tmp_path).emit("run.started", **{"seq": 99, "ts": "假的"})

    event = read_trace(tmp_path)[0]
    assert event["seq"] == 1
    assert event["kind"] == "run.started"
    assert event["ts"] != "假的"
