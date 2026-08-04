"""让 `python -m anthill` 等价于 `anthill`。

面板要启动 agentd 时用得上：`anthill` 这个可执行文件在不在 PATH 上，
取决于装的方式（uv run / pipx / 手工建的 venv）。而 `sys.executable -m anthill`
用的是**当前进程自己的解释器**，一定指向同一套代码，不会启到别的环境去。
"""

from anthill.cli.main import app

if __name__ == "__main__":
    app()
