"""接入配方要说运行它的那台机器的话。

serve 打印的每一条「粘去终端」的命令都是替**本机**用户写的 ——
bash 的 `VAR=x cmd` 前缀和 `&&` 在 Windows PowerShell 5.1 里都不成立，
Windows 实机粘过去直接报错（cli 踩过）。配方由 serve 按自己的平台生成。
"""

from __future__ import annotations

import sys
from pathlib import Path

from anthill.adapters.bridge_connect import connect_recipes, launch_command
from anthill.core.paths import NodeLayout


def test_recipes_speak_powershell_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    layout = NodeLayout(tmp_path)

    launch = launch_command(layout, "cc")
    assert launch.startswith('$env:ANTHILL_AGENT="cc"; '), "PowerShell 没有 VAR=x cmd 前缀语法"

    recipes = connect_recipes(layout, "cc")
    assert recipes["pin"].startswith("$env:ANTHILL_AGENT="), "钉死命令也得是 PowerShell 写法"
    assert "&&" not in recipes["mcp"], "Windows PowerShell 5.1 不认 &&，用分号"


def test_recipes_speak_bash_elsewhere(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    layout = NodeLayout(tmp_path)

    assert launch_command(layout, "cc").startswith("ANTHILL_AGENT=cc claude")
    recipes = connect_recipes(layout, "cc")
    assert recipes["pin"] == "ANTHILL_AGENT=cc claude"
    assert "&&" in recipes["mcp"]


def test_paths_with_spaces_survive_both_shells(tmp_path: Path, monkeypatch) -> None:
    """Windows 用户目录带空格是常态（C:\\Users\\My Name\\...）——
    粘贴命令里的路径不加引号，粘过去就断在空格上。"""
    import shlex

    spaced = tmp_path / "My Documents" / "ws"
    spaced.mkdir(parents=True)
    layout = NodeLayout(spaced)

    monkeypatch.setattr(sys, "platform", "win32")
    assert f'"{spaced}"' in launch_command(layout, "cc")
    assert f'"{spaced}"' in connect_recipes(layout, "cc")["mcp"]

    monkeypatch.setattr(sys, "platform", "linux")
    assert shlex.quote(str(spaced)) in launch_command(layout, "cc")
    assert shlex.quote(str(spaced)) in connect_recipes(layout, "cc")["mcp"]
