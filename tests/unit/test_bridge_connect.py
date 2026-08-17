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
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "anthill.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    layout = NodeLayout(tmp_path / "ws")

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

    # 假 win32 下不能让查找落到 shutil.which（它会碰 Windows 专属 API）——
    # 摆一个假的 anthill.exe 让候选路径直接命中
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "anthill.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    assert f'"{spaced}"' in launch_command(layout, "cc")
    assert f'"{spaced}"' in connect_recipes(layout, "cc")["mcp"]

    monkeypatch.setattr(sys, "platform", "linux")
    assert shlex.quote(str(spaced)) in launch_command(layout, "cc")
    assert shlex.quote(str(spaced)) in connect_recipes(layout, "cc")["mcp"]


def test_the_exe_is_found_on_windows_despite_the_suffix(tmp_path: Path, monkeypatch) -> None:
    """Windows 上控制台脚本叫 anthill.exe —— 按 POSIX 的裸名去找必然落空，
    于是配方退化成裸 `anthill`，粘出去 exit 127。实机上 wtst 的值守会话
    为此白白烧了好几轮去满盘找可执行文件。"""
    from anthill.adapters.bridge_connect import anthill_exe

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "anthill.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    assert anthill_exe() == str(scripts / "anthill.exe")


def test_windows_paths_are_always_quoted_for_the_bash_tool(tmp_path: Path, monkeypatch) -> None:
    """粘贴目标不止 PowerShell：Claude Code 的 Bash 工具是 Git Bash，
    C:\\Users 这种反斜杠路径**不带引号**会被 bash 吃成 C:Users。
    双引号三个壳（PowerShell/cmd/Git Bash）都保平安 —— 有没有空格都引。"""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "anthill.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    layout = NodeLayout(tmp_path / "ws")

    launch = launch_command(layout, "cc")

    assert f'"{scripts / "anthill.exe"}"' in launch, "exe 路径必须带引号"
    assert f'"{layout.workspace}"' in launch, "工作区路径必须带引号"
    # PS 子表达式里带引号的命令词必须用调用操作符，否则整条是解析错误
    assert '"$(& "' in launch, "PowerShell 变体里带引号的命令要有 & 调用符"
