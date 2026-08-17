"""定期卫生：谁也不看的旧文件别让它长成山。

实测证据（08-17 体检）：cli 的收件箱 30 条 receipt/result 躺了三天 ——
role=user 的信箱 **by design 没有 agentd 消费者**，落进去的信封只会越堆
越多：doctor 报「积压」、对话页每轮多读、queue 徽标常年不归零。

三条清扫规则（数字都在 [runtime] 里可调）：

- **role=user 的信箱**：回执（receipt.*）无行动价值，首次清扫即归档；
  其余（result / chat）保 keep_hours —— queue 徽标当一天的「有结果没看」
  提醒。归档进 done/<日期>/，对话页读的就是 done，**可见性无损**。
  worker 的积压是待办不是垃圾，一概不动。
- **chats 发件记录**：thread 文件 keep_days 没动过就删 —— 它的意义是补
  对话页的发件半句，thread 冷透了记录也没意义。有意的取舍：冷 thread
  复活时自己那半边旧记录已清、只剩对方归档 —— 预期行为，不是 bug。
- **bridge done/ 归档**：同 keep_days。读取端（bridge_history）只看最近
  40 条本就有界，这里清的是磁盘。

并发容错：两个进程同时路过（serve 与 doctor）抢到已消失的文件不抛 ——
搬移走 Mailbox.archive（原子），删除包 FileNotFoundError。
"""

from __future__ import annotations

import time
from contextlib import suppress

from anthill.core.config import Config
from anthill.core.errors import AntHillError
from anthill.core.ids import now
from anthill.core.mailbox import Mailbox
from anthill.core.paths import NodeLayout
from anthill.core.traffic import RECEIPT_PREFIX


def sweep_user_mailboxes(layout: NodeLayout, config: Config, *, keep_hours: float) -> int:
    """归档 role=user 信箱里的旧信封。返回搬走的条数。"""
    moved = 0
    cutoff = now().timestamp() - keep_hours * 3600
    for name, agent in config.agents.items():
        if agent.role != "user":
            continue
        box = Mailbox(layout.mailbox_dir(name))
        if not box.exists:
            continue
        for path in box.list_new():
            try:
                env = Mailbox.read_envelope(path)
            except AntHillError as exc:
                with suppress(FileNotFoundError, OSError):
                    box.quarantine(path, str(exc))
                    moved += 1
                continue
            # 年龄取信封时刻与文件落地时刻中**较新**者 —— 跨机迟到投递的旧信封
            # 落地那一刻才开始「躺」，只看 env.ts 会让它一落地就被归档，
            # 人连 queue 徽标都没来得及看见
            try:
                landed = path.stat().st_mtime
            except OSError:
                continue
            age_anchor = max(env.ts.timestamp(), landed)
            is_receipt = str(env.type).startswith(RECEIPT_PREFIX)
            if is_receipt or age_anchor < cutoff:
                with suppress(FileNotFoundError, OSError):
                    box.archive(path)
                    moved += 1
    return moved


def sweep_records(layout: NodeLayout, *, keep_days: float) -> int:
    """清掉 keep_days 没动过的 chats 发件记录文件。"""
    removed = 0
    cutoff = time.time() - keep_days * 86400
    chats = layout.root / "chats"
    if not chats.is_dir():
        return 0
    for path in chats.glob("*.jsonl"):
        with suppress(FileNotFoundError, OSError):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    return removed


def sweep_bridge_done(layout: NodeLayout, config: Config, *, keep_days: float) -> int:
    """清掉桥接归档里 keep_days 之前的旧文件。"""
    removed = 0
    cutoff = time.time() - keep_days * 86400
    for name, agent in config.agents.items():
        if not agent.bridge:
            continue
        done = layout.agent_dir(name) / "bridge" / "done"
        if not done.is_dir():
            continue
        for path in done.iterdir():
            if not path.is_file():
                continue
            with suppress(FileNotFoundError, OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
    return removed
