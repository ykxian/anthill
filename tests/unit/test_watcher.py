"""03-tech-design §7：watcher 与 NFS 降级。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.agent.watcher import (
    MailboxWatcher,
    WatchMode,
    filesystem_type,
    is_network_filesystem,
)
from anthill.core.ids import new_id


async def collect(watcher: MailboxWatcher, count: int, timeout: float = 3.0) -> list[Path]:
    found: list[Path] = []
    stream = watcher.stream()

    async def drain() -> None:
        async for path in stream:
            found.append(path)
            if len(found) >= count:
                return

    try:
        await asyncio.wait_for(drain(), timeout=timeout)
    finally:
        await stream.aclose()
    return found


async def test_poll_mode_sees_files_written_after_start(tmp_path: Path):
    watcher = MailboxWatcher(tmp_path, mode="poll", poll_interval=0.05)

    async def write_later() -> None:
        await asyncio.sleep(0.1)
        (tmp_path / "01J000000000000000000000AA.json").write_text("{}", encoding="utf-8")

    task = asyncio.create_task(write_later())
    found = await collect(watcher, count=1)
    await task

    assert [p.name for p in found] == ["01J000000000000000000000AA.json"]


async def test_existing_files_are_picked_up_on_start(tmp_path: Path):
    """agentd 重启时 new/ 里的积压必须被处理，不能只等新事件。"""
    (tmp_path / "01J000000000000000000000AB.json").write_text("{}", encoding="utf-8")
    watcher = MailboxWatcher(tmp_path, mode="poll", poll_interval=0.05)

    found = await collect(watcher, count=1)

    assert len(found) == 1


async def test_non_json_files_are_ignored(tmp_path: Path):
    (tmp_path / "seen.db").write_text("x", encoding="utf-8")
    (tmp_path / "01J000000000000000000000AC.json").write_text("{}", encoding="utf-8")
    watcher = MailboxWatcher(tmp_path, mode="poll", poll_interval=0.05)

    found = await collect(watcher, count=1)

    assert [p.suffix for p in found] == [".json"]


async def test_each_file_is_yielded_once(tmp_path: Path):
    (tmp_path / "01J000000000000000000000AD.json").write_text("{}", encoding="utf-8")
    watcher = MailboxWatcher(tmp_path, mode="poll", poll_interval=0.02)

    with pytest.raises(TimeoutError):
        await collect(watcher, count=2, timeout=0.3)  # 只有一个文件，等第二个必然超时


async def test_inotify_mode_receives_events(tmp_path: Path):
    watcher = MailboxWatcher(tmp_path, mode="inotify")

    async def write_later() -> None:
        await asyncio.sleep(0.2)
        (tmp_path / "01J000000000000000000000AE.json").write_text("{}", encoding="utf-8")

    task = asyncio.create_task(write_later())
    found = await collect(watcher, count=1, timeout=5.0)
    await task

    assert len(found) == 1
    if watcher.mode is WatchMode.POLL:
        # 这台机器上 inotify 用不了（多半是别的进程把 max_user_watches 用光了）。
        # 消息**照样收到了**（上面那条断言），这正是降级该有的样子 ——
        # 但「inotify 真的能用」这件事在这个环境里没法验证，所以跳过而不是判失败。
        pytest.skip(f"环境不支持 inotify：{watcher.reason}")
    assert watcher.mode is WatchMode.INOTIFY


async def test_forced_poll_mode_is_reported(tmp_path: Path):
    watcher = MailboxWatcher(tmp_path, mode="poll")

    assert await watcher.detect_mode() is WatchMode.POLL
    assert "配置强制" in watcher.reason


async def test_auto_mode_probes_and_explains_itself(tmp_path: Path):
    watcher = MailboxWatcher(tmp_path, mode="auto")

    mode = await watcher.detect_mode()

    assert mode in {WatchMode.INOTIFY, WatchMode.POLL}
    assert watcher.reason  # 判定理由必须写得出来，否则排查无从下手
    assert not (tmp_path / ".inotify-probe").exists()  # 探针文件要清理干净


def test_network_filesystem_detection(tmp_path: Path, monkeypatch):
    """NFS 上远端写入不产生本地 inotify 事件 —— 必须降级轮询。"""
    assert filesystem_type(tmp_path) is not None or not Path("/proc/mounts").is_file()

    monkeypatch.setattr("anthill.agent.watcher.filesystem_type", lambda _: "nfs4")
    assert is_network_filesystem(tmp_path)

    monkeypatch.setattr("anthill.agent.watcher.filesystem_type", lambda _: "ext4")
    assert not is_network_filesystem(tmp_path)


async def test_network_filesystem_forces_poll(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("anthill.agent.watcher.filesystem_type", lambda _: "nfs")
    watcher = MailboxWatcher(tmp_path, mode="auto")

    assert await watcher.detect_mode() is WatchMode.POLL
    assert "nfs" in watcher.reason


async def test_forced_inotify_degrades_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    """内核说不行的时候要降级，不是崩掉。

    最常见的是 `[Errno 28] inotify watch limit reached` —— 机器上别的进程把
    `fs.inotify.max_user_watches` 用光了，和这个工作区毫无关系。
    `watch_mode = "inotify"` 的意思是「优先用它」，不该是「用不了就别活了」：
    轮询慢一点，但节点还在收消息；崩掉就是彻底掉线。
    """

    class Exhausted:
        def schedule(self, *args: object, **kwargs: object) -> None:
            raise OSError(28, "inotify watch limit reached")

        def start(self) -> None:  # pragma: no cover - schedule 先抛
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout: float = 0) -> None:
            pass

    monkeypatch.setattr("anthill.agent.watcher.Observer", Exhausted)
    watcher = MailboxWatcher(tmp_path, mode="inotify", poll_interval=0.05)

    stream = watcher.stream()
    envelope = tmp_path / f"{new_id()}.json"
    envelope.write_text("{}", encoding="utf-8")
    found = await asyncio.wait_for(anext(stream), timeout=3)
    await stream.aclose()

    assert found == envelope, "降级之后照样得收得到消息"
    assert watcher.mode is WatchMode.POLL
    assert "降级轮询" in watcher.reason
