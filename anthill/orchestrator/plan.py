"""计划 schema 与「强制结构化输出」的生成流程（03-tech-design §5）。

关键不是让模型输出 JSON —— 是**校验失败时把错误原文喂回去重试**。
模型看得懂 "steps.0.assignee: devops 不在可用 Agent 名单里" 这种反馈，
原样再问一遍它只会再错一次。
"""

from __future__ import annotations

import json
import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from anthill.core.errors import PlanError, ProviderError
from anthill.providers.base import ChatProvider, Msg

MAX_PLAN_ATTEMPTS = 3
MAX_STEPS = 12
ROLE_PREFIX = "role:"
STEP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,16}$")

PLAN_PROMPT = """\
你是多 Agent 团队的编排者。把下面这个目标拆成一份可执行的计划。

目标：{goal}

可以派活的 Agent（只能用这些，不要发明新的）：
{roster}

要求：
- 只输出一个 JSON 对象，不要有任何解释文字。
- 步骤数 1~{max_steps}，能一步做完就别拆成三步。
- `assignee` 写具体 Agent 名，或 `role:角色名`（同角色多人时由系统挑负载最低的）。
- `depends_on` 写依赖的步骤 id；没有依赖就留空数组，它们会被并发派发。
- `done_when` 写一句可判定的完成标准，最后由你自己拿它对照结果。

JSON 结构：
{{
  "goal": "……",
  "steps": [
    {{"id": "s1", "assignee": "coder", "task": "要做什么，写清楚交付物", "depends_on": []}},
    {{"id": "s2", "assignee": "role:reviewer", "task": "……", "depends_on": ["s1"]}}
  ],
  "done_when": "……"
}}\
"""

RETRY_PROMPT = """\
上一次的输出不合格：{error}

请重新输出，**只要一个合法的 JSON 对象**，不要 markdown 代码块以外的任何文字。\
"""


class RosterEntry(BaseModel):
    """可派活的 Agent。给模型看的名片，不含任何密钥或路径。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    role: str
    persona: str = ""

    def render(self) -> str:
        suffix = f" —— {self.persona}" if self.persona else ""
        return f"- {self.name}（角色 {self.role}）{suffix}"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    assignee: str
    task: str = Field(min_length=1, max_length=4_000)
    depends_on: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        if not STEP_ID_RE.match(self.id):
            raise ValueError(f"非法步骤 id {self.id!r}：只允许字母数字与 _-，≤16 字符")
        return self

    @property
    def is_role(self) -> bool:
        return self.assignee.startswith(ROLE_PREFIX)

    @property
    def target(self) -> str:
        """去掉 `role:` 前缀后的名字，用于和 roster 对照。"""
        return self.assignee[len(ROLE_PREFIX) :] if self.is_role else self.assignee


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1, max_length=2_000)
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=MAX_STEPS)
    done_when: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"步骤 id 有重复：{ids}")
        known = set(ids)
        for step in self.steps:
            missing = [d for d in step.depends_on if d not in known]
            if missing:
                raise ValueError(f"步骤 {step.id} 依赖了不存在的步骤：{', '.join(missing)}")
            if step.id in step.depends_on:
                raise ValueError(f"步骤 {step.id} 依赖了自己")
        if _has_cycle(self.steps):
            raise ValueError("步骤依赖成环，无法拓扑调度")
        return self

    def step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"计划里没有步骤 {step_id!r}")

    def ready(self, *, done: set[str], taken: set[str]) -> tuple[PlanStep, ...]:
        """依赖已全部完成、且自己还没被处理过的步骤 —— 它们可以并发派发。

        `taken` 必须包含**所有非 pending 的步骤**（在跑的、成功的、失败的、跳过的）。
        只排除「在跑的」会让失败步骤看起来又变回可派发 —— 那就是每个 tick 重派一次的死循环。
        """
        return tuple(
            step
            for step in self.steps
            if step.id not in taken
            and step.id not in done
            and all(dep in done for dep in step.depends_on)
        )


def _has_cycle(steps: tuple[PlanStep, ...]) -> bool:
    """Kahn 拓扑排序：削不完就是有环。"""
    pending = {s.id: set(s.depends_on) for s in steps}
    while pending:
        free = [sid for sid, deps in pending.items() if not deps]
        if not free:
            return True
        for sid in free:
            del pending[sid]
        for deps in pending.values():
            deps.difference_update(free)
    return False


# ---------- 解析 ----------


def parse_plan(text: str) -> Plan:
    """从模型输出里抠出计划。模型常带 markdown 代码块或前后废话，都要能容忍。"""
    raw = _extract_json(text)
    if raw is None:
        raise PlanError("模型输出里找不到 JSON 对象；请只输出一个 JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"计划不是合法 JSON：{exc}") from exc
    try:
        return Plan.model_validate(data)
    except ValueError as exc:
        raise PlanError(f"计划不符合 schema：{exc}") from exc


def _extract_json(text: str) -> str | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def check_roster(plan: Plan, roster: tuple[RosterEntry, ...]) -> None:
    """模型爱编不存在的角色 —— 编了就等于把任务发进黑洞，必须当场打回。"""
    names = {e.name for e in roster}
    roles = {e.role for e in roster}
    unknown = [
        step.assignee
        for step in plan.steps
        if step.target not in (roles if step.is_role else names)
    ]
    if unknown:
        known = ", ".join(sorted(names)) + " / 角色 " + ", ".join(sorted(roles))
        raise PlanError(f"计划把任务派给了不存在的 {', '.join(unknown)}；可用的是：{known}")


# ---------- 生成 ----------


async def generate_plan(
    provider: ChatProvider,
    *,
    goal: str,
    roster: tuple[RosterEntry, ...],
    max_attempts: int = MAX_PLAN_ATTEMPTS,
) -> Plan:
    messages = [
        Msg.user(
            PLAN_PROMPT.format(
                goal=goal,
                roster="\n".join(e.render() for e in roster) or "（没有可用 Agent）",
                max_steps=MAX_STEPS,
            )
        )
    ]
    last_error = ""
    for _attempt in range(max_attempts):
        try:
            turn = await provider.complete(messages, [])
        except ProviderError as exc:
            raise PlanError(f"生成计划时模型调用失败：{exc}") from exc

        try:
            plan = parse_plan(turn.text)
            check_roster(plan, roster)
        except PlanError as exc:
            last_error = str(exc)
            messages = [
                *messages,
                turn.to_msg(),
                Msg.user(RETRY_PROMPT.format(error=last_error)),
            ]
            continue
        return plan

    raise PlanError(f"连续 {max_attempts} 次都没能生成合法计划；最后一次的问题：{last_error}")


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return plan.model_dump(mode="json")
