"""run 的执行流水：`blackboard/tasks/<task_id>/trace.jsonl`。

state.json 回答「现在什么样」，这个文件回答「怎么走到这一步的」——
哪一步先派、催没催过、重试发生在哪个环节，终态快照里全都看不见。
借鉴 DeepSeek Harness 的 append-only 会话日志，但刻意收窄：

- **观察者，不是事实源。** coordinator 在既有状态迁移点旁追加事件，
  写失败只当没记，绝不打断调度；恢复调度仍然只认 state.json。
- **单写者纪律。** 只有 coordinator 写；一行一个 JSON，事件必带
  seq/ts/kind，读端容忍撕裂的末行，写端重启后 seq 接着编。
- **关联键。** step.dispatched / step.done 事件带 msg + thread，
  和 `agent start --record` 的模型级录音行用同一对键就能 join。
- **敏感面。** 面板只拿 `event_count`（「N 条事件」）；全文回放走
  `anthill runs <id> --trace`，不进任何快照。
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from anthill.core.ids import now

TRACE_FILE = "trace.jsonl"

_RESERVED = ("seq", "ts", "kind", "step")
"""协议字段。detail 里同名的键会被这四个顶掉，而不是反过来。"""


class RunTrace:
    """一次 run 的追加式写者。构造时扫一遍尾部，崩溃重启后 seq 不回卷。"""

    def __init__(self, task_dir: Path) -> None:
        self._path = task_dir / TRACE_FILE
        self._seq = _last_seq(self._path)
        _heal_torn_tail(self._path)

    def emit(self, kind: str, *, step: str = "", **detail: object) -> None:
        """追加一条事件。记不上只能沉默 —— 观察者不许把调度拖下水。"""
        record: dict[str, object] = dict(detail)
        record["seq"] = self._seq + 1
        record["ts"] = now().isoformat()
        record["kind"] = kind
        if step:
            record["step"] = step
        with suppress(OSError):
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._seq += 1


def read_trace(task_dir: Path) -> list[dict[str, object]]:
    """按序读回事件。撕裂/损坏的行直接跳过 —— 半条事件不如没有。"""
    path = task_dir / TRACE_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        with suppress(json.JSONDecodeError):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def event_count(task_dir: Path) -> int:
    """给面板的「N 条事件」。只数完整行，不带任何内容。"""
    return len(read_trace(task_dir))


def _last_seq(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            value = json.loads(line).get("seq", 0)
            return max(0, int(value))
    return 0


def _heal_torn_tail(path: Path) -> None:
    """上一个写者死在半行上：补一个换行，新事件才不会黏在残行后面。"""
    with suppress(OSError), path.open("rb+") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            return
        handle.seek(-1, 2)
        if handle.read(1) != b"\n":
            handle.write(b"\n")
