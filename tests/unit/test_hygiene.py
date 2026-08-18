"""定期卫生：谁也不看的旧文件别长成山。

实测证据：cli 的收件箱 30 条 receipt/result 躺了三天没人消费 ——
role=user 的信箱**by design 没有 agentd**，落进去的信封只会越堆越多，
doctor 报「积压」、对话页每轮都要多读。归档进 done/<日期>/ 之后
对话页照常可见（traffic 读的就是 done），queue 计数归零。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from anthill.core.config import Config
from anthill.core.envelope import Address, Envelope
from anthill.core.hygiene import sweep_bridge_done, sweep_records, sweep_user_mailboxes
from anthill.core.ids import new_id, now
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.payloads import ChatPayload, MessageType, ReceiptPayload
from anthill.core.traffic import conversations

NODE_TOML = """
[node]
name = "box"
workspace = "."

[agents.cli]
role = "user"

[agents.worker]
role = "worker"

[agents.cc]
role = "worker"
bridge = true
"""


def _node(tmp_path: Path) -> tuple[NodeLayout, Config]:
    layout = NodeLayout(tmp_path).ensure_base()
    layout.node_toml.write_text(NODE_TOML, encoding="utf-8")
    return layout, Config.load_from(layout)


def _deliver(layout: NodeLayout, agent: str, *, kind: MessageType, age_hours: float) -> Envelope:
    from datetime import timedelta

    env = Envelope(
        id=new_id(),
        ts=now() - timedelta(hours=age_hours),
        from_=Address(node="box", agent="worker"),
        to=Address(node="box", agent=agent),
        type=kind,
        thread=new_id(),
        payload=(
            ReceiptPayload(ref="01X") if kind is MessageType.RECEIPT_ACCEPTED
            else ChatPayload(body="正文在这")
        ),
    )
    box = Mailbox(layout.mailbox_dir(agent)).ensure()
    box.deposit(env)
    # 年龄锚点是 max(信封时刻, 落地 mtime)：测试里把 mtime 也拨回去，
    # 模拟「早就落地一直没人看」而不是「刚迟到送达」
    stale = time.time() - age_hours * 3600
    for path in box.list_new():
        os.utime(path, (stale, stale))
    return env


def test_receipts_are_archived_on_first_sweep(tmp_path: Path) -> None:
    """回执是纯噪音（无行动价值），第一次清扫就归档 —— 那 30 条积压的主体。"""
    layout, config = _node(tmp_path)
    _deliver(layout, "cli", kind=MessageType.RECEIPT_ACCEPTED, age_hours=0.1)

    moved = sweep_user_mailboxes(layout, config, keep_hours=24.0)

    box = Mailbox(layout.mailbox_dir("cli"))
    assert moved == 1
    assert box.list_new() == []


def test_young_results_stay_as_a_visible_reminder(tmp_path: Path) -> None:
    """result 保 24h —— queue 徽标当一天的「有结果没看」提醒。"""
    layout, config = _node(tmp_path)
    _deliver(layout, "cli", kind=MessageType.CHAT, age_hours=1.0)

    moved = sweep_user_mailboxes(layout, config, keep_hours=24.0)

    assert moved == 0
    assert len(Mailbox(layout.mailbox_dir("cli")).list_new()) == 1


def test_old_results_are_archived_and_stay_visible_in_conversations(tmp_path: Path) -> None:
    layout, config = _node(tmp_path)
    env = _deliver(layout, "cli", kind=MessageType.CHAT, age_hours=48.0)

    moved = sweep_user_mailboxes(layout, config, keep_hours=24.0)

    assert moved == 1
    assert Mailbox(layout.mailbox_dir("cli")).list_new() == []
    bodies = [
        m["body"]
        for t in conversations(layout)["threads"]
        for m in t["messages"]
    ]
    assert "正文在这" in bodies, "归档进 done 之后对话页必须照常可见"
    assert env.thread in [t["thread"] for t in conversations(layout)["threads"]]


def test_worker_backlog_is_left_alone(tmp_path: Path) -> None:
    """worker 的积压是**待办**，不是垃圾 —— agentd 启动后要处理的。
    只清 role=user 的信箱（它 by design 没消费者）。"""
    layout, config = _node(tmp_path)
    _deliver(layout, "worker", kind=MessageType.CHAT, age_hours=100.0)

    moved = sweep_user_mailboxes(layout, config, keep_hours=24.0)

    assert moved == 0
    assert len(Mailbox(layout.mailbox_dir("worker")).list_new()) == 1


def test_broken_envelopes_are_quarantined_not_fatal(tmp_path: Path) -> None:
    layout, config = _node(tmp_path)
    box = Mailbox(layout.mailbox_dir("cli")).ensure()
    (box.new / "01BROKEN.json").write_text("不是 json", encoding="utf-8")

    sweep_user_mailboxes(layout, config, keep_hours=24.0)

    assert box.list_new() == [], "解析不了的也不能一直堵在队列里"
    assert (box.done / "invalid" / "01BROKEN.json").is_file()


def test_old_chat_records_are_pruned(tmp_path: Path) -> None:
    """发件记录的意义是补对话页的缺口 —— thread 冷了 30 天，记录也没意义了。
    取舍（有意）：冷 thread 复活时自己那半边的旧记录已清，对话页只剩对方
    归档 —— 预期行为，不是 bug。"""
    layout, _ = _node(tmp_path)
    chats = layout.root / "chats"
    chats.mkdir()
    old = chats / "OLD.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    young = chats / "YOUNG.jsonl"
    young.write_text("{}\n", encoding="utf-8")

    removed = sweep_records(layout, keep_days=30.0)

    assert removed == 1
    assert not old.exists() and young.exists()


def test_old_bridge_archives_are_pruned(tmp_path: Path) -> None:
    layout, config = _node(tmp_path)
    done = layout.agent_dir("cc") / "bridge" / "done"
    done.mkdir(parents=True)
    old = done / "OLD.md"
    old.write_text("x", encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    young = done / "YOUNG.md"
    young.write_text("x", encoding="utf-8")

    removed = sweep_bridge_done(layout, config, keep_days=30.0)

    assert removed == 1
    assert not old.exists() and young.exists()


def test_a_late_delivered_old_envelope_is_not_archived_on_arrival(tmp_path: Path) -> None:
    """跨机 spool 迟到投递：信封时刻很老、但**刚落地** —— 它才开始「躺」，
    不该一落地就被归档，人连 queue 徽标都没看见（tst2 的 L1）。"""
    layout, config = _node(tmp_path)
    _deliver(layout, "cli", kind=MessageType.CHAT, age_hours=0.0)  # mtime = 现在
    # 把信封时刻改老：重写文件内容为 48h 前的信封，但保持 mtime 新
    box = Mailbox(layout.mailbox_dir("cli"))
    (path,) = box.list_new()
    from datetime import timedelta

    old_env = Envelope(
        id=new_id(),
        ts=now() - timedelta(hours=48),
        from_=Address(node="far", agent="worker"),
        to=Address(node="box", agent="cli"),
        type=MessageType.CHAT,
        thread=new_id(),
        payload=ChatPayload(body="迟到的老信"),
    )
    path.write_bytes(old_env.to_json_bytes())

    moved = sweep_user_mailboxes(layout, config, keep_hours=24.0)

    assert moved == 0, "刚落地的信不该按信封时刻被判超龄"


def test_old_run_traces_are_pruned_but_state_stays(tmp_path: Path) -> None:
    """trace.jsonl 纳入 records_keep_days（与 tst2 对齐的补充 b）。

    只清流水不清快照：state.json 是历史任务列表的数据源，看板还要读；
    活跃 run 一直在追加事件，mtime 常新，天然不会被误清。
    """
    layout, _ = _node(tmp_path)
    task_dir = layout.blackboard / "tasks" / new_id()
    task_dir.mkdir(parents=True)
    (task_dir / "state.json").write_text("{}", encoding="utf-8")
    trace = task_dir / "trace.jsonl"
    trace.write_text('{"seq": 1}\n', encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(trace, (stale, stale))
    os.utime(task_dir / "state.json", (stale, stale))

    fresh_dir = layout.blackboard / "tasks" / new_id()
    fresh_dir.mkdir(parents=True)
    young = fresh_dir / "trace.jsonl"
    young.write_text('{"seq": 1}\n', encoding="utf-8")

    removed = sweep_records(layout, keep_days=30.0)

    assert removed == 1
    assert not trace.exists()
    assert (task_dir / "state.json").exists()
    assert young.exists()


def test_trace_glob_matches_the_real_trace_filename() -> None:
    """hygiene 里的 TRACE_GLOB 是字面量（不让 core 向上 import orchestrator），
    这里钉住它和真实文件名的等值 —— 改名时两边必须一起动。"""
    from anthill.core.hygiene import TRACE_GLOB
    from anthill.orchestrator.trace import TRACE_FILE

    assert f"*/{TRACE_FILE}" == TRACE_GLOB
