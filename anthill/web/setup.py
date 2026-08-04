"""还没配工作区时的那一步：在面板上挑一个目录。

上一版 `serve` 撞上空目录会**直接就地建**一个工作区。装好就能用是对的，
但「就地」这个决定不该由程序替人做 —— 你可能只是 cd 到了别处，
结果 `.anthill` 撒在一个你根本不想要的目录里。

所以改成：找不到工作区就进「未配置」状态，面板给一个目录浏览器，
你挑好地方再建（或者直接指一个已有的工作区）。在那之前**磁盘一个字都不写**。

浏览目录这件事只对回环开放 —— 和面板其他写操作同一道闸。
能在这台机器上开浏览器访问回环的人，本来就能 `ls`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anthill.core.errors import AntHillError
from anthill.core.paths import ANTHILL_DIR, NODE_TOML

MAX_ENTRIES = 300


def home() -> Path:
    return Path.home()


def browse(raw: str) -> dict[str, Any]:
    """列一个目录。只列目录本身 —— 这是在挑「工作区放哪」，不是文件管理器。

    同时标出哪些已经是工作区（里面有 `.anthill/node.toml`），
    好让人一眼看出「哦这个是我上次建的」。
    """
    target = _resolve(raw)
    try:
        entries = sorted(
            (p for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )[:MAX_ENTRIES]
    except OSError as exc:
        raise AntHillError(f"读不了这个目录：{exc.strerror or exc}") from exc

    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "is_workspace": is_workspace(target),
        "entries": [
            {"name": p.name, "path": str(p), "is_workspace": is_workspace(p)} for p in entries
        ],
    }


def is_workspace(path: Path) -> bool:
    try:
        return (path / ANTHILL_DIR / NODE_TOML).is_file()
    except OSError:
        return False


def _resolve(raw: str) -> Path:
    target = Path(raw).expanduser() if raw.strip() else home()
    try:
        target = target.resolve()
    except OSError as exc:
        raise AntHillError(f"路径不合法：{exc}") from exc
    if not target.is_dir():
        raise AntHillError(f"{target} 不是一个目录")
    return target
