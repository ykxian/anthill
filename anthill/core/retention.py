"""给所有单调增长的目录装刹车。

这套架构里「消息就是文件」很好用，代价是**什么都不会自己消失**：

- `mailbox/done/<日期>/` —— 每条处理过的消息一个文件；
- `outbox/sent/` —— 每条发出去的消息一个文件；
- `logs/*.jsonl` —— 只追加；
- `seen.db` —— 只在启动时清一次。

而且归档量是消息量的**两倍以上**：每条业务消息还额外产生一条回执信封。
一个跑长任务的节点，这几个目录会一直涨到磁盘满为止。

三条原则：

1. **`done/` 按天目录整个删**，不逐个文件 stat —— 目录名就是日期，这是最便宜的判据。
2. **死信不按这个节奏清。** 它是「需要人处理」的东西，删掉等于把问题藏起来。
   单独一个更长的保留期，而且清了要吼一声（`anthill dead list` 才是正经出路）。
3. **日志滚动而不是截断。** 出问题时最近那一段最值钱，掐掉尾巴等于掐掉现场。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from anthill.core.ids import now

DAY_FORMAT = "%Y-%m-%d"


@dataclass(frozen=True, slots=True)
class SweepResult:
    done_days: int = 0
    sent: int = 0
    dead: int = 0
    logs_rotated: int = 0

    @property
    def touched(self) -> bool:
        return bool(self.done_days or self.sent or self.dead or self.logs_rotated)

    def merged(self, other: SweepResult) -> SweepResult:
        return SweepResult(
            done_days=self.done_days + other.done_days,
            sent=self.sent + other.sent,
            dead=self.dead + other.dead,
            logs_rotated=self.logs_rotated + other.logs_rotated,
        )


def sweep_archive(done_dir: Path, *, keep_days: int) -> int:
    """删掉过期的按天归档目录。返回删了几天。

    比对的是**目录名**而不是 mtime：`done/2026-07-02/` 这个名字本身就是日期，
    读目录名比 stat 一堆文件便宜得多，而且不会被「碰过文件」影响判断。
    """
    if keep_days <= 0 or not done_dir.is_dir():
        return 0
    cutoff = (now() - timedelta(days=keep_days)).strftime(DAY_FORMAT)
    removed = 0
    for day in sorted(done_dir.iterdir()):
        if not day.is_dir() or not _looks_like_a_day(day.name) or day.name >= cutoff:
            continue
        _remove_tree(day)
        removed += 1
    return removed


def sweep_flat(directory: Path, *, keep_days: int, suffix: str = ".json") -> int:
    """删掉目录里过期的文件（没有按天分层的那些）。返回删了几个。"""
    if keep_days <= 0 or not directory.is_dir():
        return 0
    cutoff = (now() - timedelta(days=keep_days)).timestamp()
    removed = 0
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(suffix):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            continue  # 正在被写、或者刚被别人删了 —— 下一轮再说
        removed += 1
    return removed


def rotate_log(path: Path, *, max_bytes: int) -> bool:
    """日志超过上限就滚动一次：`x.jsonl` → `x.jsonl.1`（旧的 .1 被覆盖）。

    只留一代。**滚动而不是截断** —— 出问题时最近那一段最值钱，
    直接清空等于把现场一起清了。
    """
    if max_bytes <= 0 or not path.is_file():
        return False
    try:
        if path.stat().st_size < max_bytes:
            return False
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        return False
    return True


def _looks_like_a_day(name: str) -> bool:
    return len(name) == 10 and name[4] == "-" and name[7] == "-" and name.replace("-", "").isdigit()


def _remove_tree(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink(missing_ok=True)
    directory.rmdir()
