"""AntHill 的 MCP 两端。

- `server.py` —— 把 AntHill 暴露给 Claude Code 这类 MCP 客户端；
- 客户端方向在 `anthill/agent/tools/mcp_client.py`（给自己的 Agent 装外部工具）。

两个方向解决的是不同的问题，别搞混：server 让**别人**能调 AntHill，
client 让 **AntHill 的 Agent** 能用别人的工具。
"""
