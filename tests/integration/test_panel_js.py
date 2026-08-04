"""面板前端脚本的逻辑检查（见 tests/js/panel_check.mjs）。

页面上的合并逻辑没人跑就没人知道它坏了 —— 总控面板恰恰是最容易在这里出错的：
WebSocket 推的是本机快照，稍不注意就会把别的机器从列表里抹掉。
没装 node 的环境跳过，不因此挡住 Python 侧的测试。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import anthill.web.app as web_app

CHECK = Path(__file__).resolve().parents[1] / "js" / "panel_check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node，跳过前端逻辑检查")
def test_panel_script_merges_the_two_data_sources_correctly() -> None:
    result = subprocess.run(
        ["node", str(CHECK), str(web_app.PANEL_HTML)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
