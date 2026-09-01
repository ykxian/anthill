from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anthill.core.errors import AntHillError
from anthill.core.process_lock import ProcessLock, locked_owner


def test_querying_a_missing_lock_does_not_create_it(tmp_path: Path) -> None:
    path = tmp_path / "missing.lock"

    assert locked_owner(path) is None
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlink 权限不稳定")
def test_lock_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("不能覆盖", encoding="utf-8")
    link = tmp_path / "agentd.lock"
    link.symlink_to(target)

    with pytest.raises(AntHillError, match="安全打开"):
        ProcessLock(link, label="box:echo agentd").acquire()

    assert target.read_text(encoding="utf-8") == "不能覆盖"
    assert locked_owner(link) is None


def test_process_lock_is_a_real_singleton_and_leaves_no_stale_owner(tmp_path: Path) -> None:
    path = tmp_path / "agentd.lock"
    first = ProcessLock(path, label="box:echo agentd")
    second = ProcessLock(path, label="box:echo agentd")

    first.acquire()
    try:
        assert locked_owner(path) == os.getpid()
        assert not second.try_acquire()
        with pytest.raises(AntHillError, match="已有一个实例"):
            second.acquire()
    finally:
        first.release()

    assert locked_owner(path) is None
    assert second.try_acquire()
    second.release()


def test_lock_is_released_when_a_real_holder_process_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "agentd.lock"
    ready = tmp_path / "ready"
    code = (
        "import sys,time; from pathlib import Path; "
        "from anthill.core.process_lock import ProcessLock; "
        "lock=ProcessLock(Path(sys.argv[1]), label='child'); lock.acquire(); "
        "Path(sys.argv[2]).write_text('ready'); time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(path), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.is_file() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.is_file(), "持锁子进程没有就绪"
        assert locked_owner(path) == child.pid

        with pytest.raises(AntHillError, match="已有一个实例"):
            ProcessLock(path, label="contender").acquire()

        child.kill()
        child.wait(timeout=5)
        replacement = ProcessLock(path, label="replacement")
        replacement.acquire()
        try:
            assert locked_owner(path) == os.getpid()
        finally:
            replacement.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
