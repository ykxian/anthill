"""归档、发件记录与日志的保留策略。

「消息就是文件」的代价是**什么都不会自己消失**，而归档量是消息量的两倍以上
（每条业务消息还额外产生一条回执信封）。这几个目录以前一个刹车都没有。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from anthill.core.ids import now
from anthill.core.retention import rotate_log, sweep_archive, sweep_flat

KEEP = 7


def day_dir(root: Path, days_ago: int) -> Path:
    name = (now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "01AAA.json").write_text("{}", encoding="utf-8")
    return directory


def aged_file(directory: Path, name: str, days_ago: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{}", encoding="utf-8")
    stamp = (now() - timedelta(days=days_ago)).timestamp()
    import os

    os.utime(path, (stamp, stamp))
    return path


# ---------- done/ 按天目录 ----------


def test_old_archive_days_are_removed_and_recent_ones_kept(tmp_path: Path) -> None:
    done = tmp_path / "done"
    old, fresh = day_dir(done, 30), day_dir(done, 1)

    removed = sweep_archive(done, keep_days=KEEP)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_zero_keep_days_means_never_clean(tmp_path: Path) -> None:
    """有人就是想留着全部历史 —— 得留一个显式的「别动我的东西」。"""
    done = tmp_path / "done"
    old = day_dir(done, 900)

    assert sweep_archive(done, keep_days=0) == 0
    assert old.exists()


def test_a_directory_that_is_not_a_date_is_left_alone(tmp_path: Path) -> None:
    """判据是目录名。名字不像日期的，一律不碰 —— 宁可留着也别误删。"""
    done = tmp_path / "done"
    stray = done / "别删我"
    stray.mkdir(parents=True)
    day_dir(done, 30)

    sweep_archive(done, keep_days=KEEP)

    assert stray.exists()


def test_sweeping_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert sweep_archive(tmp_path / "从来没有过", keep_days=KEEP) == 0


# ---------- outbox/sent 与 dead ----------


def test_old_sent_records_are_removed_by_mtime(tmp_path: Path) -> None:
    sent = tmp_path / "sent"
    old = aged_file(sent, "01OLD.json", 30)
    fresh = aged_file(sent, "01NEW.json", 1)

    removed = sweep_flat(sent, keep_days=KEEP)

    assert removed == 1
    assert not old.exists() and fresh.exists()


def test_dead_letters_get_their_own_longer_grace(tmp_path: Path) -> None:
    """死信是「需要人处理」的东西，跟着归档一起清等于把问题藏起来。"""
    dead = tmp_path / "dead"
    letter = aged_file(dead, "01DEAD.json", 20)

    assert sweep_flat(dead, keep_days=30) == 0  # 死信那条更长的保留期
    assert letter.exists()
    assert sweep_flat(dead, keep_days=KEEP) == 1  # 用归档的保留期就会被清掉


# ---------- 日志 ----------


def test_a_big_log_is_rotated_not_truncated(tmp_path: Path) -> None:
    """出问题时最近那一段最值钱，直接清空等于把现场一起清了。"""
    path = tmp_path / "coder.jsonl"
    path.write_text("x" * 5000, encoding="utf-8")

    assert rotate_log(path, max_bytes=1000) is True
    assert not path.exists()
    assert (tmp_path / "coder.jsonl.1").read_text(encoding="utf-8") == "x" * 5000


def test_a_small_log_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "coder.jsonl"
    path.write_text("小", encoding="utf-8")

    assert rotate_log(path, max_bytes=1000) is False
    assert path.exists()


def test_rotation_keeps_only_one_generation(tmp_path: Path) -> None:
    path = tmp_path / "coder.jsonl"
    (tmp_path / "coder.jsonl.1").write_text("上一代", encoding="utf-8")
    path.write_text("y" * 5000, encoding="utf-8")

    rotate_log(path, max_bytes=1000)

    assert (tmp_path / "coder.jsonl.1").read_text(encoding="utf-8") == "y" * 5000
    assert not (tmp_path / "coder.jsonl.1.1").exists()
