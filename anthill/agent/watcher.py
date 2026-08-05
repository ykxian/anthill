"""邮箱 watcher 与 NFS 降级（03-tech-design §7）。

inotify 事件由**内核在本地**产生：学校服务器的 home 挂在 NFS 上时，
别的机器往你的 inbox 写文件，你的内核根本不知道 —— 事件永远不来。
所以启动时先判定文件系统类型，网络文件系统直接降级为轮询，
判定结果写进日志与 `anthill status`，排查「为什么收不到消息」时一眼可见。

即使走 inotify，也仍然每 30s 兜底全量扫一次：漏事件的代价是消息永远卡住，
而多扫一次的代价只是一次 iterdir。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from enum import StrEnum
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

RESCAN_INTERVAL = 30.0
PROBE_TIMEOUT = 1.5
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "afs",
        "9p",
        "lustre",
        "fuse.sshfs",
        "fuse.glusterfs",
        "fuse.s3fs",
    }
)


class WatchMode(StrEnum):
    INOTIFY = "inotify"
    POLL = "poll"


def filesystem_type(path: Path) -> str | None:
    """查 /proc/mounts 里最长匹配的挂载点。非 Linux 返回 None。"""
    mounts = Path("/proc/mounts")
    if not mounts.is_file():
        return None
    try:
        lines = mounts.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    target = str(path.resolve())
    best: tuple[int, str] | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fstype = parts[1], parts[2]
        covers = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if covers and (best is None or len(mount_point) > best[0]):
            best = (len(mount_point), fstype)
    return best[1] if best else None


def is_network_filesystem(path: Path) -> bool:
    fstype = filesystem_type(path)
    return fstype is not None and fstype in NETWORK_FILESYSTEMS


def _dedupe_key(path: Path) -> tuple[str, int] | None:
    """信封文件的身份 = 文件名 + inode。文件消失或不是信封时返回 None。"""
    if path.suffix != ".json":
        return None
    try:
        return (path.name, path.stat().st_ino)
    except OSError:
        return None


class _QueueHandler(FileSystemEventHandler):
    """watchdog 在自己的线程里回调，必须用 call_soon_threadsafe 递给事件循环。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
        self._loop = loop
        self._queue = queue

    def on_created(self, event: FileSystemEvent) -> None:
        self._push(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._push(getattr(event, "dest_path", event.src_path), event.is_directory)

    def _push(self, raw_path: Any, is_directory: bool) -> None:
        if is_directory:
            return
        path = raw_path.decode() if isinstance(raw_path, bytes) else str(raw_path)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)


class MailboxWatcher:
    """产出 `inbox/new` 里新出现的信封路径。两种模式对调用方完全透明。"""

    def __init__(
        self,
        directory: Path,
        *,
        mode: str = "auto",
        poll_interval: float = 2.0,
        rescan_interval: float = RESCAN_INTERVAL,
    ) -> None:
        self.directory = directory
        self._requested_mode = mode
        self._poll_interval = poll_interval
        self._rescan_interval = rescan_interval
        self._mode: WatchMode | None = None
        self._reason: str = ""
        self._observer: Any = None

    @property
    def mode(self) -> WatchMode | None:
        return self._mode

    @property
    def reason(self) -> str:
        return self._reason

    async def detect_mode(self) -> WatchMode:
        """auto 模式的判定顺序：显式配置 → 文件系统类型 → 实际探针。"""
        if self._requested_mode == WatchMode.POLL:
            self._reason = "配置强制 poll"
            return WatchMode.POLL
        if self._requested_mode == WatchMode.INOTIFY:
            self._reason = "配置强制 inotify"
            return WatchMode.INOTIFY

        fstype = filesystem_type(self.directory)
        if fstype is not None and fstype in NETWORK_FILESYSTEMS:
            self._reason = f"{fstype} 是网络文件系统，远端写入不产生本地 inotify 事件"
            return WatchMode.POLL
        if await self._probe_inotify():
            self._reason = f"inotify 自检通过（fs={fstype or 'unknown'}）"
            return WatchMode.INOTIFY
        self._reason = f"inotify 自检未收到事件（fs={fstype or 'unknown'}），降级轮询"
        return WatchMode.POLL

    async def _probe_inotify(self) -> bool:
        """写一个探针文件，看事件能不能在超时内到达。捕获 watch 数耗尽等异常情况。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        observer = Observer()
        try:
            observer.schedule(_QueueHandler(loop, queue), str(self.directory), recursive=False)
            observer.start()
        except (OSError, RuntimeError):
            return False
        probe = self.directory / ".inotify-probe"
        try:
            await asyncio.sleep(0.05)
            probe.write_text("probe", encoding="utf-8")
            await asyncio.wait_for(queue.get(), timeout=PROBE_TIMEOUT)
            return True
        except (TimeoutError, OSError):
            return False
        finally:
            probe.unlink(missing_ok=True)
            observer.stop()
            await asyncio.to_thread(observer.join, 2.0)

    async def prepare(self) -> WatchMode:
        """提前把模式定下来，好在启动日志与 `anthill status` 里立刻可见。"""
        if self._mode is None:
            self._mode = await self.detect_mode()
        return self._mode

    async def stream(self) -> AsyncGenerator[Path, None]:
        """异步产出新信封路径。已产出过的文件名不会重复产出。"""
        await self.prepare()
        queue: asyncio.Queue[str] = asyncio.Queue()
        if self._mode is WatchMode.INOTIFY:
            loop = asyncio.get_running_loop()
            try:
                self._observer = Observer()
                self._observer.schedule(
                    _QueueHandler(loop, queue), str(self.directory), recursive=False
                )
                self._observer.start()
            except (OSError, RuntimeError) as exc:
                # **内核说不行的时候要降级，不是崩掉。** 最常见的是
                # `[Errno 28] inotify watch limit reached` —— 机器上别的进程
                # 把 fs.inotify.max_user_watches 用光了，和这个工作区没关系。
                # `watch_mode = "inotify"` 是「优先用它」，不该是「用不了就别活了」：
                # 轮询慢一点，但节点还在收消息，而崩掉就是彻底掉线。
                # （auto 那条路本来就会降级，只有显式配 inotify 的会走到这儿。）
                self._observer = None
                self._mode = WatchMode.POLL
                self._reason = f"inotify 起不来（{exc}），降级轮询"
                # scan_interval 在下面按 self._mode 算，这里改完模式就够了

        # 去重键是 (文件名, inode)：同一个 ULID 被重复投递时是新文件（新 inode），
        # 必须重新产出交给幂等层判定，不能被 watcher 悄悄吞掉。
        yielded: set[tuple[str, int]] = set()
        scan_interval = (
            self._poll_interval if self._mode is WatchMode.POLL else self._rescan_interval
        )
        try:
            while True:
                for path in self._scan(yielded):
                    yield path
                if self._mode is WatchMode.POLL:
                    await asyncio.sleep(scan_interval)
                    continue
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=scan_interval)
                except TimeoutError:
                    continue  # 超时即到了兜底全量扫描的时候
                path = Path(raw)
                key = _dedupe_key(path)
                if key is not None and key not in yielded:
                    yielded.add(key)
                    yield path
        finally:
            await self.stop()

    def _scan(self, yielded: set[tuple[str, int]]) -> list[Path]:
        """全量扫描一次；顺便把已消失的文件从去重集里摘掉，防止无限增长。"""
        snapshot: dict[tuple[str, int], Path] = {}
        try:
            entries = sorted(self.directory.iterdir())
        except OSError:
            return []
        for path in entries:
            key = _dedupe_key(path)
            if key is not None:
                snapshot[key] = path
        yielded.intersection_update(snapshot.keys())  # 必须原地改，调用方持有同一个 set
        fresh = [key for key in snapshot if key not in yielded]
        yielded.update(fresh)
        return [snapshot[key] for key in fresh]

    async def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join, 2.0)
            self._observer = None
