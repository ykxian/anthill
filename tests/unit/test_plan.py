"""计划 schema 与「强制结构化输出」的生成流程。"""

from __future__ import annotations

import json

import pytest

from anthill.core.errors import PlanError
from anthill.orchestrator.plan import (
    MAX_PLAN_ATTEMPTS,
    Plan,
    PlanStep,
    RosterEntry,
    generate_plan,
    parse_plan,
)
from anthill.providers.base import Turn
from anthill.providers.fake import FakeProvider

ROSTER = (
    RosterEntry(name="coder", role="worker", persona="写代码"),
    RosterEntry(name="reviewer", role="reviewer", persona="审代码"),
)

GOOD_PLAN = {
    "goal": "为 date.py 补单测并通过审查",
    "steps": [
        {"id": "s1", "assignee": "coder", "task": "写测试", "depends_on": []},
        {"id": "s2", "assignee": "role:reviewer", "task": "审查 s1 产物", "depends_on": ["s1"]},
        {"id": "s3", "assignee": "coder", "task": "按意见修改", "depends_on": ["s2"]},
    ],
    "done_when": "reviewer approve 且 pytest 全绿",
}


# ---------- schema ----------


def test_plan_parses_a_well_formed_plan() -> None:
    plan = Plan.model_validate(GOOD_PLAN)

    assert plan.goal.startswith("为 date.py")
    assert [s.id for s in plan.steps] == ["s1", "s2", "s3"]


def test_plan_rejects_duplicate_step_ids() -> None:
    bad = {**GOOD_PLAN, "steps": [GOOD_PLAN["steps"][0], GOOD_PLAN["steps"][0]]}

    with pytest.raises(ValueError, match="重复"):
        Plan.model_validate(bad)


def test_plan_rejects_dangling_dependency() -> None:
    bad = {
        **GOOD_PLAN,
        "steps": [{"id": "s1", "assignee": "coder", "task": "写", "depends_on": ["s9"]}],
    }

    with pytest.raises(ValueError, match="s9"):
        Plan.model_validate(bad)


def test_plan_rejects_dependency_cycle() -> None:
    bad = {
        **GOOD_PLAN,
        "steps": [
            {"id": "a", "assignee": "coder", "task": "1", "depends_on": ["b"]},
            {"id": "b", "assignee": "coder", "task": "2", "depends_on": ["a"]},
        ],
    }

    with pytest.raises(ValueError, match="环"):
        Plan.model_validate(bad)


def test_plan_rejects_empty_step_list() -> None:
    with pytest.raises(ValueError):
        Plan.model_validate({**GOOD_PLAN, "steps": []})


def test_ready_steps_respects_dependencies() -> None:
    plan = Plan.model_validate(GOOD_PLAN)

    assert [s.id for s in plan.ready(done=set(), taken=set())] == ["s1"]
    assert [s.id for s in plan.ready(done={"s1"}, taken=set())] == ["s2"]
    assert plan.ready(done={"s1"}, taken={"s2"}) == ()
    assert plan.ready(done={"s1", "s2", "s3"}, taken=set()) == ()


def test_independent_steps_are_all_ready_at_once() -> None:
    plan = Plan.model_validate(
        {
            **GOOD_PLAN,
            "steps": [
                {"id": "a", "assignee": "coder", "task": "1"},
                {"id": "b", "assignee": "coder", "task": "2"},
            ],
        }
    )

    assert len(plan.ready(done=set(), taken=set())) == 2  # 无依赖的步骤并发派发


# ---------- 从模型输出里抠 JSON ----------


def test_parse_plan_tolerates_markdown_code_fence() -> None:
    text = f"好的，计划如下：\n```json\n{json.dumps(GOOD_PLAN, ensure_ascii=False)}\n```\n"

    plan = parse_plan(text)

    assert len(plan.steps) == 3


def test_parse_plan_tolerates_prose_around_bare_json() -> None:
    text = f"我想了想\n{json.dumps(GOOD_PLAN, ensure_ascii=False)}\n就这样"

    assert parse_plan(text).done_when


def test_parse_plan_reports_what_is_wrong() -> None:
    with pytest.raises(PlanError, match="JSON"):
        parse_plan("我觉得应该先写测试，然后审查。")


# ---------- 生成（校验失败要带着错误重试）----------


async def test_generate_plan_succeeds_on_first_try() -> None:
    provider = FakeProvider([Turn(text=json.dumps(GOOD_PLAN, ensure_ascii=False))])

    plan = await generate_plan(provider, goal="补单测", roster=ROSTER)

    assert len(plan.steps) == 3
    assert len(provider.calls) == 1


async def test_generate_plan_retries_with_the_validation_error_attached() -> None:
    # Arrange：第一次吐废话，第二次才给合法计划
    provider = FakeProvider(
        [Turn(text="我先说点别的"), Turn(text=json.dumps(GOOD_PLAN, ensure_ascii=False))]
    )

    # Act
    plan = await generate_plan(provider, goal="补单测", roster=ROSTER)

    # Assert：重试时把错误原文喂回给模型，而不是原样再问一遍
    assert len(plan.steps) == 3
    retry_prompt = provider.calls[1].messages[-1].content
    assert "JSON" in retry_prompt


async def test_generate_plan_gives_up_after_max_attempts() -> None:
    provider = FakeProvider([Turn(text="就是不给 JSON")])

    with pytest.raises(PlanError, match="3"):
        await generate_plan(provider, goal="补单测", roster=ROSTER)

    assert len(provider.calls) == MAX_PLAN_ATTEMPTS


async def test_generate_plan_rejects_assignee_outside_the_roster() -> None:
    # 模型爱编不存在的角色，编了就等于把任务发进黑洞
    bad = {**GOOD_PLAN, "steps": [{"id": "s1", "assignee": "devops", "task": "部署"}]}
    provider = FakeProvider([Turn(text=json.dumps(bad, ensure_ascii=False))])

    with pytest.raises(PlanError, match="devops"):
        await generate_plan(provider, goal="部署", roster=ROSTER)


async def test_generate_plan_prompt_lists_the_available_roster() -> None:
    provider = FakeProvider([Turn(text=json.dumps(GOOD_PLAN, ensure_ascii=False))])

    await generate_plan(provider, goal="补单测", roster=ROSTER)

    prompt = provider.calls[0].messages[-1].content
    assert "coder" in prompt and "reviewer" in prompt


def test_step_assignee_accepts_both_name_and_role_forms() -> None:
    assert PlanStep(id="s1", assignee="coder", task="x").is_role is False
    assert PlanStep(id="s2", assignee="role:reviewer", task="x").is_role is True
