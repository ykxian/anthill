"""本机上的工作区清单：建、改、删，以及给每个工作区起一个 serve。

一个工作区 = 一个节点，peers、密钥、邮箱全都挂在它上面 —— 工作区之间是隔离的。
但**这不意味着每个工作区要一个 serve 进程**：路由键 `to.node` 本来就写在信封里，
一个 serve 完全可以按节点名把信分派到不同工作区的邮箱去（见 `NodeRegistry`）。

所以这一层只管「这台机器上有哪些工作区」这份清单，放在
`~/.anthill/workspaces.json` —— 它不属于任何一个工作区，跟着这台机器走。

删除刻意做成两档：从清单里移除（默认，只是不再显示）和连同文件一起删
（要显式要求）。后者会带走邮箱、密钥、黑板 —— 那不该是一次误点的代价。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.atomic import atomic_write
from anthill.core.errors import AntHillError
from anthill.core.paths import ANTHILL_DIR, NodeLayout
from anthill.core.workspace import create_workspace
from anthill.web.setup import is_workspace

REGISTRY_DIR = ANTHILL_DIR
REGISTRY_FILE = "workspaces.json"
MAX_WORKSPACES = 32


class WorkspaceSpec(BaseModel):
    """面板上「新建工作区」的表单。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)
    node_name: str = Field(default="", max_length=64)
    port: int = Field(default=0, ge=0, le=65535)
    """0 表示让它自己挑一个没被占的。"""


def registry_path() -> Path:
    return Path.home() / REGISTRY_DIR / REGISTRY_FILE


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    root = registry_path().parent
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root, root, REGISTRY_FILE, json.dumps(items, ensure_ascii=False).encode())


def remember(path: Path, *, port: int) -> None:
    """把一个工作区记进清单。同一个路径只留一条。"""
    items = [item for item in _read() if item.get("path") != str(path)]
    if len(items) >= MAX_WORKSPACES:
        raise AntHillError(f"清单里已经有 {MAX_WORKSPACES} 个工作区了")
    _write([*items, {"path": str(path), "port": port}])


def forget(path: Path) -> None:
    _write([item for item in _read() if item.get("path") != str(path)])


def listing(attached: Iterable[Path] = (), primary: Path | None = None) -> list[dict[str, Any]]:
    """清单 + 每个工作区当下的状态。

    `attached` 是本进程正照看着的那些工作区 —— 它们可能还没进过清单
    （命令行直接 `-w` 起的那个就是），补进来，省得面板上看不见自己。
    """
    known = _read()
    for path in attached:
        if not any(i.get("path") == str(path) for i in known):
            known = [*known, {"path": str(path), "port": 0}]
    live = {str(p) for p in attached}
    out = []
    for item in known:
        path = Path(str(item.get("path", "")))
        out.append(
            {
                "path": str(path),
                "name": path.name or str(path),
                "port": int(item.get("port", 0) or 0),
                "exists": is_workspace(path),
                "node": _node_name(path),
                "attached": str(path) in live,
                "current": primary is not None and path == primary,
            }
        )
    return sorted(out, key=lambda w: (not w["current"], not w["attached"], w["path"]))


def paths_for_node(node: str) -> list[Path]:
    """返回机器清单里属于 ``node`` 的有效本地工作区。

    这不只是给面板列目录：出站传输也要用同一份机器级清单判断一个节点是否
    就在本机。只看当前 ``node.toml`` 会把另一个已挂载的本机工作区错当远端，
    继而要求做一次毫无意义的 peer 配对。

    返回列表而不是偷偷挑第一项，是为了让调用方能拒绝重名。信封只写节点名，
    两个本地工作区同名时根本没有足够信息判断该投给谁。
    """
    found: list[Path] = []
    for item in _read():
        path = Path(str(item.get("path", ""))).expanduser().resolve()
        if path in found or not is_workspace(path) or _node_name(path) != node:
            continue
        found.append(path)
    return found


def _node_name(path: Path) -> str:
    toml = path / ANTHILL_DIR / "node.toml"
    try:
        for line in toml.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("name") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def create(spec: WorkspaceSpec) -> dict[str, Any]:
    """建一个新工作区并记进清单。已经是工作区的目录直接收编，不覆盖。"""
    path = Path(spec.path).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        path = path.resolve()
    except OSError as exc:
        raise AntHillError(f"建不了这个目录：{exc.strerror or exc}") from exc

    layout = NodeLayout(path)
    if not layout.node_toml.is_file():
        # 不传 node_name 时按目录名起 —— create_workspace 里就是这么做的
        create_workspace(layout, node_name=spec.node_name)
    remember(path, port=spec.port)
    return {"ok": True, "path": str(path), "node": _node_name(path)}


def delete(raw: str, *, purge: bool = False) -> dict[str, Any]:
    """默认只从清单里移除；`purge` 才真的删文件。

    删文件会带走邮箱、密钥、黑板 —— 所以要显式要求，而且只删 `.anthill/`，
    不碰你放在那个目录里的别的东西。
    """
    path = Path(raw).expanduser().resolve()
    forget(path)
    if not purge:
        return {"ok": True, "path": str(path), "purged": False}

    import shutil

    root = path / ANTHILL_DIR
    if root.is_dir():
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise AntHillError(f"删不掉 {root}：{exc.strerror or exc}") from exc
    return {"ok": True, "path": str(path), "purged": True}


def doomed(*, keep: Iterable[Path] = (), stale_only: bool = False) -> list[str]:
    """这次清理会碰到哪些工作区。**先算出来给人看，再动手。**

    批量删除最该有的一道闸不是「你确定吗」，而是「你确定要删**这几个**吗」——
    所以这个函数单独存在：路由拿它算出名单发给前端，前端把数目回传，
    对不上就整个拒绝。中间有人加了/删了工作区，这一次就作废，不会误伤。
    """
    safe = {str(Path(p).expanduser().resolve()) for p in keep}
    return [
        raw
        for item in _read()
        if (raw := str(item.get("path", "")))
        and raw not in safe
        and not (stale_only and Path(raw).is_dir())
    ]


def clear(
    *,
    keep: Iterable[Path] = (),
    stale_only: bool = False,
    purge: bool = False,
    expect: int | None = None,
) -> dict[str, Any]:
    """一次把清单清干净。

    `purge=False`（默认）**只动清单，一个文件都不删**。
    `purge=True` 连 `.anthill/` 一起删 —— 那会带走邮箱、黑板、密钥。

    只删 `.anthill/`，**不碰你放在那个目录里的别的东西**（源码、笔记、
    你自己的文件）—— 和单个删除同一条规矩。

    `expect` 是防误点那道闸：调用方必须先看到「要删几个」并把这个数回传，
    对不上就整个拒绝。中间有人加了/删了工作区，这一次就作废，不会误伤。

    `keep` 是本进程正照看着的那些 —— 把自己从清单里踢掉，面板下一秒就找不着
    自己了，那不是用户想要的「清理」。

    `stale_only=True` 只清路径已经不存在的那些：那是最常见也最安全的一次清理。
    """
    targets = doomed(keep=keep, stale_only=stale_only)
    if expect is not None and expect != len(targets):
        raise AntHillError(
            f"清单和你看到的对不上了（你确认的是 {expect} 个，现在是 {len(targets)} 个）——"
            "这一次没有执行。刷新一下再来。"
        )

    condemned = set(targets)
    _write([item for item in _read() if str(item.get("path", "")) not in condemned])

    purged: list[str] = []
    failed: list[str] = []
    if purge:
        import shutil

        for raw in targets:
            root = Path(raw) / ANTHILL_DIR
            if not root.is_dir():
                continue
            try:
                shutil.rmtree(root)
                purged.append(raw)
            except OSError:
                # 删不掉一个，不该让剩下的都不删 —— 汇总上报，让人知道哪些还留着
                failed.append(raw)
    return {
        "ok": True,
        "removed": len(targets),
        "kept": len(_read()),
        "purged": purged,
        "failed": failed,
    }
