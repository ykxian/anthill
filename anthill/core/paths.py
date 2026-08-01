"""工作区目录布局（01-architecture §5.1）。

workspace/.anthill/
├── node.toml
├── agents/<name>/mailbox/
├── blackboard/
└── logs/
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anthill.core.errors import ConfigError

ANTHILL_DIR = ".anthill"
NODE_TOML = "node.toml"


@dataclass(frozen=True, slots=True)
class NodeLayout:
    """把「路径怎么拼」这件事收在一个地方，别处一律问它要路径。"""

    workspace: Path

    @property
    def root(self) -> Path:
        return self.workspace / ANTHILL_DIR

    @property
    def node_toml(self) -> Path:
        return self.root / NODE_TOML

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def blackboard(self) -> Path:
        return self.root / "blackboard"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def agent_dir(self, name: str) -> Path:
        return self.agents_dir / name

    def mailbox_dir(self, name: str) -> Path:
        return self.agent_dir(name) / "mailbox"

    def log_file(self, name: str) -> Path:
        return self.logs / f"agentd-{name}.jsonl"

    def known_agents(self) -> list[str]:
        """磁盘上真实存在邮箱的 Agent（可能多于/少于配置，排查时有用）。"""
        if not self.agents_dir.is_dir():
            return []
        return sorted(p.name for p in self.agents_dir.iterdir() if p.is_dir())

    def ensure_base(self) -> NodeLayout:
        for directory in (self.root, self.agents_dir, self.blackboard, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def ensure_agent(self, name: str) -> Path:
        mailbox = self.mailbox_dir(name)
        mailbox.mkdir(parents=True, exist_ok=True)
        return mailbox

    @classmethod
    def discover(cls, start: Path | None = None) -> NodeLayout:
        """从当前目录向上找 `.anthill/`，像 git 找 `.git` 一样。"""
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            if (candidate / ANTHILL_DIR / NODE_TOML).is_file():
                return cls(workspace=candidate)
        raise ConfigError(
            f"从 {current} 向上没找到 {ANTHILL_DIR}/{NODE_TOML}；先跑一次 `anthill init`"
        )
