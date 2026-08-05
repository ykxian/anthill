"""`anthill cost` —— 这些 Agent 到底烧了多少。

roadmap 里「花销记录…防止调试烧钱失控」是一条持续事项，实际只做了一半：
`Usage` 在循环里累加、每个任务结束打一条 `task.done ... tokens=N`，
然后就没有然后了 —— 没有聚合、没有折算、CLI 没有命令、面板上也看不到。
数据一路算好，在最后一米被丢掉。

这里只做一件事：把日志里那些 `task.done` 收拢起来。
**钱不瞎猜** —— 单价来自 `[providers.*]` 的 `price_in` / `price_out`
（每百万 token）。没标价就只报 token 数，因为写死在代码里的价格迟早会过期，
而一个过期的价格比没有价格更糟。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import typer
from rich.table import Table

from anthill.cli.common import console, load
from anthill.core.config import Config

PER_MILLION = 1_000_000
TASK_DONE = "task.done"


@dataclass(frozen=True, slots=True)
class Tally:
    tasks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, *, inp: int, out: int) -> Tally:
        return replace(
            self,
            tasks=self.tasks + 1,
            input_tokens=self.input_tokens + inp,
            output_tokens=self.output_tokens + out,
        )


def cost_command(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON，便于接进脚本"),
) -> None:
    """按 Agent 汇总 token 用量与花费。"""
    layout, config = load(workspace)
    by_agent, by_model = _collect(layout.logs)

    if as_json:
        console.print_json(
            data={
                "agents": {
                    name: {
                        "tasks": t.tasks,
                        "input_tokens": t.input_tokens,
                        "output_tokens": t.output_tokens,
                        "cost": _price(config, model_of(by_model, name), t),
                    }
                    for name, t in sorted(by_agent.items())
                },
                "total_tokens": sum(t.total for t in by_agent.values()),
            }
        )
        return

    if not by_agent:
        console.print(
            "[dim]还没有任何模型调用记录。"
            "（只有配了 provider 的 Agent 才会产生用量 —— echo 不花钱。）[/dim]"
        )
        return

    table = Table(title="token 用量", header_style="bold cyan")
    for column in ("Agent", "模型", "任务数", "输入", "输出", "合计", "花费"):
        table.add_column(column, justify="right" if column != "Agent" else "left")
    priced = False
    for name, tally in sorted(by_agent.items()):
        model = model_of(by_model, name)
        money = _price(config, model, tally)
        priced = priced or money is not None
        table.add_row(
            name,
            model or "-",
            str(tally.tasks),
            f"{tally.input_tokens:,}",
            f"{tally.output_tokens:,}",
            f"{tally.total:,}",
            f"{money:.4f}" if money is not None else "—",
        )
    console.print(table)
    if not priced:
        console.print(
            "[dim]没折算成钱：在 [providers.<名字>] 里加 price_in / price_out"
            "（每百万 token 的价格）就会有这一列。[/dim]"
        )


def _collect(logs_dir: Path) -> tuple[dict[str, Tally], dict[str, str]]:
    """扫日志。**读不动的行直接跳过** —— 统计花销不该因为一行坏日志就失败。"""
    by_agent: dict[str, Tally] = {}
    by_model: dict[str, str] = {}
    if not logs_dir.is_dir():
        return by_agent, by_model
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in _lines(path):
            if line.get("event") != TASK_DONE:
                continue
            agent = str(line.get("agent") or path.stem)
            inp = _int(line.get("in_tokens"))
            out = _int(line.get("out_tokens"))
            if not inp and not out:
                # 老日志只有 tokens 总数，算不出分项 —— 全记到输入侧，至少总数是对的
                inp = _int(line.get("tokens"))
            by_agent[agent] = by_agent.get(agent, Tally()).plus(inp=inp, out=out)
            if line.get("model"):
                by_model[agent] = str(line["model"])
    return by_agent, by_model


def _lines(path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def model_of(by_model: dict[str, str], agent: str) -> str:
    return by_model.get(agent, "")


def _price(config: Config, model: str, tally: Tally) -> float | None:
    """标了价才算。没标就返回 None —— 不瞎猜。"""
    section = next((p for p in config.providers.values() if p.model == model), None)
    if section is None or (not section.price_in and not section.price_out):
        return None
    return (
        tally.input_tokens * section.price_in + tally.output_tokens * section.price_out
    ) / PER_MILLION


def _int(value: object) -> int:
    """日志字段来自磁盘，什么类型都可能 —— 读不出数就当 0。"""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
