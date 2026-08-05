"""改一行不必重写整个文件，找一个符号不必一层层 list_dir。

工具集原来只有 6 个，缺局部编辑、缺搜索、缺分页读 —— Agent 的能力天花板
比看上去低得多。`run_shell` 本可以顶上（grep / find / sed），但白名单只有
七条验证类命令，白名单外一律 HIGH → 无人值守时判 DENY。
结论应该是把只读检索做成受控工具，而不是留着 run_shell 形同虚设。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.agent.tools.base import ToolContext
from anthill.agent.tools.fs import MAX_READ_LINES, EditFileTool, ReadFileTool
from anthill.agent.tools.search import FindFilesTool, SearchTextTool
from anthill.core.config import SecuritySection
from anthill.core.payloads import RiskLevel


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (tmp_path / "blackboard").mkdir()
    (workspace / "src" / "date.py").write_text(
        "def parse(text):\n    return text\n\n\ndef fmt(value):\n    return str(value)\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_date.py").write_text(
        "from src.date import parse\n\n\ndef test_parse():\n    assert parse('x') == 'x'\n",
        encoding="utf-8",
    )
    return ToolContext(
        workspace=workspace,
        blackboard=tmp_path / "blackboard",
        security=SecuritySection(),
        thread="01J000000000000000000THRD",
    )


# ---------- 分页读 ----------


async def test_a_long_file_can_be_read_past_the_first_page(ctx: ToolContext) -> None:
    """以前 read_file 一次性读、超了直接截断 —— **文件后半部分永远读不到**，
    而且模型无从知道自己只看到了半个文件。"""
    big = ctx.workspace / "big.txt"
    big.write_text("\n".join(f"第{n}行" for n in range(1, 2001)), encoding="utf-8")

    first = await ReadFileTool().run({"path": "big.txt"}, ctx)
    second = await ReadFileTool().run({"path": "big.txt", "offset": 1901}, ctx)

    assert "第1行" in first.content
    assert f"第{MAX_READ_LINES}行" in first.content
    assert "继续读：offset=" in first.content, "读不完必须告诉模型还有"
    assert "第2000行" in second.content, "文件后半部分以前永远读不到"


async def test_reading_past_the_end_says_so(ctx: ToolContext) -> None:
    result = await ReadFileTool().run({"path": "src/date.py", "offset": 900}, ctx)

    assert not result.ok
    assert "只有" in result.content


async def test_a_string_offset_is_tolerated(ctx: ToolContext) -> None:
    """模型经常把整数写成字符串，别为这个报错。"""
    result = await ReadFileTool().run({"path": "src/date.py", "offset": "2", "limit": "1"}, ctx)

    assert result.ok
    assert result.content.startswith("    return text")


# ---------- 局部编辑 ----------


async def test_editing_replaces_only_the_matched_text(ctx: ToolContext) -> None:
    result = await EditFileTool().run(
        {"path": "src/date.py", "old": "    return text", "new": "    return text.strip()"}, ctx
    )

    assert result.ok, result.content
    after = (ctx.workspace / "src" / "date.py").read_text(encoding="utf-8")
    assert "return text.strip()" in after
    assert "def fmt(value):" in after  # 别的地方一个字没动


async def test_an_ambiguous_edit_is_refused_not_guessed(ctx: ToolContext) -> None:
    """改错地方比改不动更难查。"""
    result = await EditFileTool().run(
        {"path": "src/date.py", "old": "    return", "new": "    yield"}, ctx
    )

    assert not result.ok
    assert "2 次" in result.content
    assert "return text" in (ctx.workspace / "src" / "date.py").read_text(encoding="utf-8")


async def test_editing_text_that_is_not_there_is_an_error(ctx: ToolContext) -> None:
    result = await EditFileTool().run(
        {"path": "src/date.py", "old": "根本没有这段", "new": "x"}, ctx
    )

    assert not result.ok
    assert "找不到" in result.content


async def test_editing_reports_the_file_as_an_artifact(ctx: ToolContext) -> None:
    """产物要能被上游看见，否则 reviewer 不知道该看哪个文件。"""
    result = await EditFileTool().run(
        {"path": "src/date.py", "old": "def fmt(value):", "new": "def format_value(value):"}, ctx
    )

    assert result.artifacts == ("src/date.py",)


@pytest.mark.parametrize("escape", ["../outside.py", "/etc/passwd", "~/secrets"])
async def test_editing_cannot_escape_the_workspace(ctx: ToolContext, escape: str) -> None:
    result = await EditFileTool().run({"path": escape, "old": "a", "new": "b"}, ctx)

    assert not result.ok
    assert "越界" in result.content


# ---------- 搜索 ----------


async def test_searching_finds_the_file_and_line(ctx: ToolContext) -> None:
    result = await SearchTextTool().run({"pattern": "def parse"}, ctx)

    assert result.ok
    assert "src/date.py:1:" in result.content


async def test_searching_can_be_narrowed_by_glob(ctx: ToolContext) -> None:
    result = await SearchTextTool().run({"pattern": "parse", "glob": "test_*.py"}, ctx)

    assert "tests/test_date.py" in result.content
    assert "src/date.py" not in result.content


async def test_noise_directories_are_not_searched(ctx: ToolContext) -> None:
    """.venv 里几万个匹配会把真正想找的那几行淹掉，也会把上下文预算烧光。"""
    junk = ctx.workspace / ".venv" / "lib"
    junk.mkdir(parents=True)
    (junk / "vendored.py").write_text("def parse(text): ...\n", encoding="utf-8")

    result = await SearchTextTool().run({"pattern": "def parse"}, ctx)

    assert ".venv" not in result.content


async def test_a_bad_regex_is_a_clear_message_not_a_crash(ctx: ToolContext) -> None:
    result = await SearchTextTool().run({"pattern": "def ("}, ctx)

    assert not result.ok
    assert "正则" in result.content


async def test_nothing_found_is_a_normal_answer(ctx: ToolContext) -> None:
    result = await SearchTextTool().run({"pattern": "绝对找不到的东西"}, ctx)

    assert result.ok
    assert "没有匹配" in result.content


async def test_searching_cannot_escape_the_workspace(ctx: ToolContext) -> None:
    result = await SearchTextTool().run({"pattern": "x", "path": "../.."}, ctx)

    assert not result.ok
    assert "越界" in result.content


# ---------- 按名字找文件 ----------


async def test_finding_files_by_glob(ctx: ToolContext) -> None:
    result = await FindFilesTool().run({"glob": "**/*.py"}, ctx)

    assert "src/date.py" in result.content
    assert "tests/test_date.py" in result.content


async def test_finding_skips_noise_directories(ctx: ToolContext) -> None:
    junk = ctx.workspace / "node_modules" / "pkg"
    junk.mkdir(parents=True)
    (junk / "index.py").write_text("", encoding="utf-8")

    result = await FindFilesTool().run({"glob": "**/*.py"}, ctx)

    assert "node_modules" not in result.content


@pytest.mark.parametrize("escape", ["/etc/*", "../*.py"])
async def test_finding_cannot_escape_the_workspace(ctx: ToolContext, escape: str) -> None:
    result = await FindFilesTool().run({"glob": escape}, ctx)

    assert not result.ok


# ---------- 检索是只读的 ----------


def test_the_search_tools_are_low_risk_so_they_work_unattended() -> None:
    """这才是重点：无人值守时高风险工具会被判 DENY。
    检索做成受控的只读工具，才真的能用。"""
    assert SearchTextTool().risk is RiskLevel.LOW
    assert FindFilesTool().risk is RiskLevel.LOW
