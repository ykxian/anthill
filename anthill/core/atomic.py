"""原子写协议（02-protocol §2）—— 整个并发安全模型的地基。

    1. 写 tmp/<name>.part
    2. fsync 文件
    3. os.replace 到 new/<name>   ← POSIX 保证同一文件系统内 rename 原子
    4. fsync 目录（best effort）

由此得到两个性质：消费方永远读不到半个文件；文件名是各写者独立生成的 ULID，
天然不冲突，所以**多写者并发投递同一邮箱无需任何锁**。
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

from anthill.core.errors import MailboxError

PART_SUFFIX = ".part"
_SEQ = itertools.count()


def _part_name(name: str) -> str:
    """临时文件名必须**每次都不一样**。

    信封的文件名是 ULID，天生不撞；但 `peers.json` / `status.json` 这些是固定名字，
    固定的 `.part` 会让两个写者撞在一起：A 写好 `x.part`，B 也写 `x.part` 并先
    rename 走了，A 再 rename 就找不到自己的临时文件 —— 报一个莫名其妙的
    「No such file or directory」。一个节点上跑着 serve 和好几个 agentd，
    这种并发是常态，不是意外。
    """
    return f"{name}.{os.getpid()}-{next(_SEQ)}{PART_SUFFIX}"


def ensure_same_filesystem(tmp_dir: Path, dst_dir: Path) -> None:
    """tmp 与目标必须同分区，否则 rename 退化为「拷贝+删除」，原子性就没了。

    这是 docs/05 §Q12 里点名的坑，所以在启动期显式检查而不是等出事。
    """
    try:
        tmp_dev = os.stat(tmp_dir).st_dev
        dst_dev = os.stat(dst_dir).st_dev
    except OSError as exc:
        raise MailboxError(f"无法检查目录 {tmp_dir} / {dst_dir}：{exc}") from exc
    if tmp_dev != dst_dev:
        raise MailboxError(
            f"{tmp_dir} 与 {dst_dir} 不在同一文件系统（{tmp_dev} != {dst_dev}），"
            "rename 将不再原子。请把整个 mailbox 放在同一分区。"
        )


def atomic_write(tmp_dir: Path, dst_dir: Path, name: str, data: bytes) -> Path:
    """把 `data` 原子地落到 `dst_dir/name`，返回最终路径。"""
    ensure_same_filesystem(tmp_dir, dst_dir)
    part = tmp_dir / _part_name(name)
    dst = dst_dir / name
    try:
        with open(part, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, dst)
        _fsync_dir(dst_dir)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise MailboxError(f"写入 {dst} 失败：{exc}") from exc
    return dst


def atomic_move(src: Path, dst: Path) -> Path:
    """同分区内移动文件（new→cur、cur→done）。目标已存在时直接覆盖。"""
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise MailboxError(f"移动 {src} → {dst} 失败：{exc}") from exc
    return dst


def _fsync_dir(path: Path) -> None:
    """目录 fsync 让 rename 本身也落盘。某些平台不支持，忽略即可。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
