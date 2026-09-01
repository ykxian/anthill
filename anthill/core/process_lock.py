"""跨进程单实例锁。

PID 文件只能说明「最后是谁写过」，不能阻止两个进程同时启动。这里把 PID 留作
诊断信息，真正的互斥交给内核文件锁：进程正常退出、异常崩溃或被 kill -9，锁都会
随文件描述符自动释放，不需要猜一份 lockfile 是否已经过期。
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from pathlib import Path
from typing import BinaryIO

from anthill.core.errors import AntHillError
from anthill.core.procs import process_alive


class ProcessLock:
    """一条进程生命期内持有的非阻塞独占锁。锁文件本身不会删除。"""

    def __init__(self, path: Path, *, label: str) -> None:
        self.path = path
        self.label = label
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self.try_acquire():
            return
        owner = locked_owner(self.path)
        detail = f"（pid {owner}）" if owner is not None else ""
        raise AntHillError(f"{self.label} 已有一个实例在运行{detail}")

    def try_acquire(self) -> bool:
        """非阻塞尝试持锁；被别的进程占用返回假，其余错误仍明确抛出。"""
        if self._handle is not None:
            raise RuntimeError(f"{self.label} 的进程锁已持有")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = _open_lock(self.path, create=True)
        except OSError as exc:
            raise AntHillError(f"无法安全打开进程锁 {self.path}：{exc}") from exc
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            if not _busy(exc):
                raise AntHillError(f"无法锁定 {self.path}：{exc}") from exc
            return False

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode())
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                _unlock(handle)
            finally:
                handle.close()
            raise AntHillError(f"无法写入进程锁 {self.path}：{exc}") from exc
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def locked_owner(path: Path) -> int | None:
    """若文件此刻确实被锁住，返回持有者写下的 PID；陈旧文件返回 ``None``。"""
    try:
        # 查询绝不能顺手创建 lockfile；控制面轮询一个停着的 Agent 不应改磁盘。
        handle = _open_lock(path, create=False)
    except OSError:
        return None
    try:
        try:
            _lock(handle)
        except OSError as exc:
            if not _busy(exc):
                return None
            # acquire() 先拿内核锁、紧接着才写 PID。给这个极窄窗口几次机会，
            # 避免面板因一瞬间读到空内容又多拉一个必然失败的子进程。
            for _ in range(4):
                pid = _read_pid(handle)
                if pid is not None and process_alive(pid):
                    return pid
                time.sleep(0.01)
            return None
        else:
            _unlock(handle)
            return None
    finally:
        handle.close()


def _read_pid(handle: BinaryIO) -> int | None:
    try:
        handle.seek(0)
        pid = int(handle.readline().strip())
    except (OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _open_lock(path: Path, *, create: bool) -> BinaryIO:
    """不跟随最终路径的符号链接，也拒绝可借来覆盖别处文件的硬链接。"""
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    # Windows 没有 O_NOFOLLOW；先做 best-effort 的 reparse/symlink 拒绝。
    # POSIX 的真正竞态边界由上面的 O_NOFOLLOW 保证。
    if sys.platform == "win32" and path.is_symlink():
        raise OSError(errno.ELOOP, "进程锁不能是符号链接", str(path))

    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "进程锁不是普通文件", str(path))
        if info.st_nlink != 1:
            raise OSError(errno.EMLINK, "进程锁不能有硬链接", str(path))
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "r+b")
    except Exception:
        os.close(fd)
        raise


def _lock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _busy(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
