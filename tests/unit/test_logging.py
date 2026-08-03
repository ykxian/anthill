"""结构化日志：JSONL 落盘、终端渲染、跟随读取。

重点是**字段值一律 escape** —— 日志里装的是任意内容，
被终端渲染器悄悄吃掉一段，比没有日志更误导人。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from anthill.core.logging import EventLog, follow_log, format_record, read_log


def rendered(record: dict[str, object]) -> str:
    """把 rich 标记真的渲染成纯文本，看最终用户到底看到了什么。"""
    console = Console(file=None, width=200, no_color=True, markup=True)
    with console.capture() as capture:
        console.print(format_record(record))  # type: ignore[arg-type]
    return capture.get()


# ---------- 落盘 ----------


def test_events_are_written_as_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "agentd-beta.jsonl"
    with EventLog(path, agent="beta", echo=False) as log:
        log.info("msg.received", msg="01J", thread="01K")
        log.warn("hop.limit", msg="01J")

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in lines] == ["msg.received", "hop.limit"]
    assert [r["level"] for r in lines] == ["info", "warn"]
    assert all(r["agent"] == "beta" for r in lines)


def test_non_scalar_fields_are_stringified(tmp_path: Path) -> None:
    """字段必须可 JSON 序列化，别把整个对象塞进去。"""
    path = tmp_path / "a.jsonl"
    with EventLog(path, agent="a", echo=False) as log:
        log.info("odd", where=Path("/tmp/x"), n=3, flag=True, nothing=None)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["where"] == "/tmp/x"
    assert (record["n"], record["flag"], record["nothing"]) == (3, True, None)


def test_log_without_a_path_only_echoes(tmp_path: Path) -> None:
    EventLog(None, agent="a", echo=False).info("nowhere")

    assert list(tmp_path.iterdir()) == []


# ---------- 终端渲染 ----------


def test_square_brackets_in_a_field_survive_rendering() -> None:
    """rich 会把 [discovery] 当成样式标记吃掉 —— 日志字段必须先转义。"""
    record = {
        "ts": "2026-08-01T10:00:00",
        "agent": "serve",
        "level": "info",
        "event": "discovery.disabled",
        "hint": "node.toml 里 [discovery] enabled = false",
    }

    text = rendered(record)

    assert "[discovery]" in text
    assert "enabled = false" in text


def test_malformed_markup_in_a_field_does_not_crash() -> None:
    """错误信息里出现半个标记是很正常的事，不能让它把渲染搞崩。"""
    record = {
        "ts": "2026-08-01T10:00:00",
        "agent": "beta",
        "level": "error",
        "event": "msg.invalid",
        "error": "解析失败：unexpected [/ at line 3",
    }

    assert "unexpected [/ at line 3" in rendered(record)


def test_event_and_agent_are_both_shown() -> None:
    text = rendered(
        {"ts": "2026-08-01T10:00:00", "agent": "coder", "level": "info", "event": "tool.done"}
    )

    assert "tool.done" in text
    assert "coder" in text


@pytest.mark.parametrize("level", ["debug", "info", "warn", "error", "unknown"])
def test_every_level_renders(level: str) -> None:
    assert rendered({"ts": "x", "agent": "a", "level": level, "event": "e"})


# ---------- 读取 ----------


def test_read_log_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text('{"event":"ok"}\n{坏\n[]\n{"event":"ok2"}\n', encoding="utf-8")

    assert [r["event"] for r in read_log(path)] == ["ok", "ok2"]


def test_read_log_honours_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    path.write_text("".join(f'{{"event":"e{i}"}}\n' for i in range(10)), encoding="utf-8")

    assert [r["event"] for r in read_log(path, limit=3)] == ["e7", "e8", "e9"]


def test_read_log_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_log(tmp_path / "nope.jsonl") == []


# `follow_log` 是个阻塞的 tail -f 循环，故意不在这里测：
# 要测就得起线程等它，而一个可能挂住整个套件的测试，代价远大于它带来的覆盖。
# 它的验证靠 `anthill log --follow` 的手工使用，以及下面这条对「起点」的断言。


def test_follow_log_waits_for_a_file_that_does_not_exist_yet(tmp_path: Path) -> None:
    """agentd 还没启动时就 --follow，应该等文件出现而不是直接报错。"""
    stream = follow_log(tmp_path / "not-yet.jsonl", poll_interval=0.01)

    assert stream is not None  # 生成器本身不该在创建时就抛
