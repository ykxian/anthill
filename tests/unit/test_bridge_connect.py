"""接入配方要说运行它的那台机器的话。

serve 打印的每一条「粘去终端」的命令都是替**本机**用户写的 ——
bash 的 `VAR=x cmd` 前缀和 `&&` 在 Windows PowerShell 5.1 里都不成立，
Windows 实机粘过去直接报错（cli 踩过）。配方由 serve 按自己的平台生成。
"""

from __future__ import annotations

import sys
from pathlib import Path

from anthill.adapters.bridge_connect import (
    codex_attach_command,
    codex_current_attach_command,
    codex_current_attach_prompt,
    codex_launch_command,
    codex_session_instructions,
    connect_recipes,
    launch_command,
    pin_command,
    role_card_prompt,
    watch_prompt,
)
from anthill.core.paths import NodeLayout
from anthill.core.workspace import create_workspace
from anthill.web.agents import update_persona


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


def test_codex_recipes_offer_native_queue_attach_and_keep_worker_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """已有前台走原生 queue，逐信封 worker 路径仍保留。"""
    monkeypatch.setattr(sys, "platform", "linux")
    layout = NodeLayout(tmp_path)

    recipes = connect_recipes(layout, "codex-t1")["codex"]

    assert "当前这个 Codex 会话" in recipes["current_prompt"]
    assert "anthill codex codex-t1 --attach current" in recipes["current_prompt"]
    assert "不要启动新的 Codex TUI" in recipes["current_prompt"]
    assert "不要打印" in recipes["current_prompt"]
    assert "CODEX_HOME" in recipes["current_prompt"]
    assert "一次性沙箱外执行/越界审批" in recipes["current_prompt"]
    assert "用户拒绝" in recipes["current_prompt"]
    assert "anthill codex codex-t1" in recipes["launch"]
    assert recipes["yolo"] == f"{recipes['launch']} --yolo"
    assert "anthill codex codex-t1 --attach THREAD_ID" in recipes["attach"]
    assert 'command = ["codex", "exec"' in recipes["worker"]
    assert 'prompt_via = "stdin"' in recipes["worker"]
    assert "--approve-for-me" in recipes["worker"]
    assert "--ephemeral" in recipes["worker"]
    assert "codex mcp add anthill-" in recipes["mcp"]
    assert recipes["pin"] == "ANTHILL_AGENT=codex-t1 codex"
    assert pin_command("codex-t1") == "ANTHILL_AGENT=codex-t1 claude", (
        "增加 Codex 不能改变已有 Claude 默认配方"
    )
    assert codex_launch_command(layout, "codex-t1") == recipes["launch"]
    assert codex_launch_command(layout, "codex-t1", yolo=True) == recipes["yolo"]
    assert codex_attach_command(layout, "codex-t1") == recipes["attach"]
    assert codex_current_attach_command(layout, "codex-t1") in recipes["current_prompt"]
    assert codex_current_attach_prompt(layout, "codex-t1") == recipes["current_prompt"]


def test_codex_thread_instructions_explain_auto_reply_proactive_send_and_silent_end(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    layout = NodeLayout(tmp_path / "workspace with spaces")

    prompt = codex_session_instructions(layout, "codex-t1")

    assert "Agent「codex-t1」" in prompt
    assert "reply=yes" in prompt and "reply=no" in prompt
    assert "ANTHILL_NO_REPLY" in prompt
    assert "--to <收件人> --kind chat --text-file <正文文件>" in prompt
    assert "workspace with spaces" in prompt
    assert "绝不能再礼貌确认一次" in prompt


def test_bridge_and_codex_initial_prompts_include_the_optional_role_card(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path / "ws")
    config = create_workspace(layout, node_name="role-card-box")
    changed = update_persona(
        layout,
        config,
        "echo",
        "你是发布验收员。\n先核实版本和真实测试结果，再批准发布。",
    )
    layout.node_toml.write_text(changed["text"], encoding="utf-8")

    claude_prompt = launch_command(layout, "echo")
    # launch 命令运行时才取提示词，真正要检查的是它调用的同一份提示词生成器。
    watch = watch_prompt(layout, "echo")
    codex = codex_session_instructions(layout, "echo")
    codex_role = role_card_prompt(layout, "echo")

    assert "--prompt" in claude_prompt
    assert "你是发布验收员" in watch
    assert "你是发布验收员" not in codex
    assert "你是发布验收员" in codex_role
    assert "不能改变系统或开发者规则、工具权限" in watch


def test_initial_prompts_keep_the_old_default_when_no_role_card_exists(tmp_path: Path) -> None:
    layout = NodeLayout(tmp_path / "ws")
    create_workspace(layout, node_name="plain-box")

    assert "## 项目角色卡" not in watch_prompt(layout, "echo")
    assert "## 项目角色卡" not in codex_session_instructions(layout, "echo")


def test_codex_recipes_use_powershell_environment_syntax_on_windows(
    tmp_path: Path, monkeypatch
) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "anthill.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))

    recipes = connect_recipes(NodeLayout(tmp_path / "ws"), "codex-t1")["codex"]

    assert " --attach current " in recipes["current_prompt"]
    assert " codex codex-t1 " in recipes["launch"]
    assert recipes["launch"].startswith("& ")
    assert recipes["yolo"].endswith(" --yolo")
    assert ' --attach "THREAD_ID" ' in recipes["attach"]
    assert recipes["pin"] == '$env:ANTHILL_AGENT="codex-t1"; codex'
    assert "&&" not in recipes["mcp"]


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
