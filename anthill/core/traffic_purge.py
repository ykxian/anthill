"""删对话记录。**先算出要删哪些给人看，再动手。**

## 一条必须守住的线：记录 ≠ 邮件

「清空对话记录」听起来是个纯显示层的操作，但这些记录就是邮箱本身，
而邮箱里有两种东西：

    inbox/done/   处理完归档的 —— 这是**记录**，删了只是少了历史
    inbox/new/    送到了还没被处理的 —— 这是**实信**，删了就是丢件

差别不是学术的。`cli` 按设计就没有处理者（它只是 `anthill send` 收回执和结果的
信箱），所以那里的信**永远**停在 `new/` —— 那正是人想清掉的。可换成一个只是
暂时没启动的 Agent，同一个动作就会把它醒来后要干的活删光。

所以默认**只删归档**，`new/` 里的照实报数、原样留着；真要一起删得显式说
（`drop_pending`）。`cur/` 是「此刻正在被处理」的，任何情况下都不碰 ——
删它是在跟 runtime 抢文件。

## 为什么先 `doomed()` 再 `purge()`

批量删除最该有的一道闸不是「你确定吗」，而是**「你确定要删这几条吗」**。
路由先拿 `doomed()` 算出名单发给前端，前端把条数回传；对不上就整个作废 ——
中间新到了一条消息，这一次就不做，不会顺手把它带走。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anthill.adapters.bridge import BRIDGE_DIR, DONE
from anthill.core.envelope import Envelope
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout

CHAT_DIR = "chats"


@dataclass(frozen=True, slots=True)
class Doomed:
    """这次清理会碰到哪些文件。"""

    archived: tuple[Path, ...]
    """`inbox/done/` 里的归档 —— 删了只是少了历史。"""

    notes: tuple[Path, ...]
    """本机记的发件（`chats/*.jsonl`）和桥接那一份往来。"""

    pending: tuple[Path, ...]
    """`inbox/new/` 里**还没被处理**的。默认不删，只报数。"""

    @property
    def count(self) -> int:
        """会删掉的条数 —— **不含 pending**，那些默认留着。"""
        return len(self.archived) + len(self.notes)

    def as_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "archived": len(self.archived),
            "notes": len(self.notes),
            "pending": len(self.pending),
        }


def doomed(layout: NodeLayout, *, thread: str = "") -> Doomed:
    """算出这次会删哪些。`thread` 留空 = 全部。"""
    archived: list[Path] = []
    pending: list[Path] = []
    notes: list[Path] = []

    for agent in layout.known_agents():
        inbox = layout.mailbox_dir(agent) / "inbox"
        for path in _archive_files(inbox / DONE):
            if _in_thread(path, thread):
                archived.append(path)
        for path in _loose(inbox / "new"):
            if _in_thread(path, thread):
                pending.append(path)
        # 桥接那一份往来（`bridge/done/<id>.md` 与 `.json`）说的是同一批消息，
        # 留着它就等于「清空了还看得见」
        notes.extend(_bridge_files(layout.agent_dir(agent) / BRIDGE_DIR / DONE, thread))

    notes.extend(_chat_notes(layout, thread))
    return Doomed(archived=tuple(archived), notes=tuple(notes), pending=tuple(pending))


def purge(
    layout: NodeLayout,
    *,
    thread: str = "",
    drop_pending: bool = False,
    expect: int | None = None,
) -> dict[str, object]:
    """真的删。`expect` 对不上就整个不做 —— 见模块头注。"""
    target = doomed(layout, thread=thread)
    if expect is not None and expect != target.count:
        raise AntHillError(
            f"记录和你看到的对不上了（你确认的是 {expect} 条，现在是 {target.count} 条）——"
            "这一次没有删任何东西，再看一眼再点"
        )
    removed = 0
    for path in (*target.archived, *target.notes):
        removed += _unlink(path)
    dropped = 0
    if drop_pending:
        for path in target.pending:
            dropped += _unlink(path)
    _prune_empty_days(layout)
    return {
        "ok": True,
        "removed": removed,
        "dropped": dropped,
        # 留下来的那些要说出来 —— 「清空了」和「清空了但还剩 8 条」是两回事
        "kept_pending": 0 if drop_pending else len(target.pending),
    }


def _unlink(path: Path) -> int:
    try:
        path.unlink()
    except OSError:
        return 0
    return 1


def _in_thread(path: Path, thread: str) -> bool:
    if not thread:
        return True
    try:
        return Envelope.from_json_bytes(path.read_bytes()).thread == thread
    except (OSError, AntHillError):
        return False


def _archive_files(done: Path) -> list[Path]:
    try:
        days = sorted(done.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for day in days:
        # `invalid/` 里是解析不了的信封，另有隔离的道理，不在「清对话」的范围内
        if not day.is_dir() or day.name == "invalid":
            continue
        try:
            out.extend(p for p in day.iterdir() if p.suffix == ".json")
        except OSError:
            continue
    return out


def _loose(stage: Path) -> list[Path]:
    try:
        return [p for p in stage.iterdir() if p.suffix == ".json"]
    except OSError:
        return []


def _bridge_files(done: Path, thread: str) -> list[Path]:
    """桥接归档：`<id>.json` 带 thread，`<id>.md` 是它的回复，成对删。"""
    try:
        entries = list(done.iterdir())
    except OSError:
        return []
    if not thread:
        return [p for p in entries if p.is_file()]
    out: list[Path] = []
    for path in entries:
        if path.suffix != ".json" or not _in_thread(path, thread):
            continue
        out.append(path)
        for mate in (path.with_suffix(".md"), path.with_suffix(".md.failed")):
            if mate.is_file():
                out.append(mate)
    return out


def _chat_notes(layout: NodeLayout, thread: str) -> list[Path]:
    """`chats/<thread>.jsonl` —— 文件名就是 thread，不用打开就能挑。"""
    directory = layout.root / CHAT_DIR
    if thread:
        path = directory / f"{thread}.jsonl"
        return [path] if path.is_file() else []
    try:
        return [p for p in directory.iterdir() if p.suffix == ".jsonl"]
    except OSError:
        return []


def _prune_empty_days(layout: NodeLayout) -> None:
    """删空了的 `done/<日期>/` 顺手收掉 —— 留一地空目录只会让人以为还有东西。"""
    for agent in layout.known_agents():
        done = layout.mailbox_dir(agent) / "inbox" / DONE
        try:
            days = list(done.iterdir())
        except OSError:
            continue
        for day in days:
            if not day.is_dir() or day.name == "invalid":
                continue
            try:
                if not any(day.iterdir()):
                    day.rmdir()
            except OSError:
                continue


def counts(layout: NodeLayout) -> dict[str, int]:
    """页面上「一共多少条」用的，不删任何东西。"""
    target = doomed(layout)
    return {"records": target.count, "pending": len(target.pending)}
