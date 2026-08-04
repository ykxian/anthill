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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anthill.core.atomic import atomic_write
from anthill.core.errors import AntHillError
from anthill.core.paths import ANTHILL_DIR, NodeLayout
from anthill.core.workspace import create_workspace, default_node_name
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


def listing(current: NodeLayout | None = None) -> list[dict[str, Any]]:
    """清单 + 每个工作区当下的状态。当前这个 serve 用的那个会标出来。"""
    known = _read()
    if current is not None and not any(i.get("path") == str(current.workspace) for i in known):
        # 命令行直接起的那个可能没进过清单，补上，省得面板上看不见自己
        known = [*known, {"path": str(current.workspace), "port": 0}]
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
                "current": current is not None and path == current.workspace,
            }
        )
    return sorted(out, key=lambda w: (not w["current"], w["path"]))


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
        create_workspace(layout, node_name=spec.node_name or default_node_name())
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
