"""基于文件的审批：让「远端跑的危险操作」由本机的人点头（03-tech-design §6）。

问题：agentd 在服务器上无人值守地跑，遇到 high 风险操作需要人确认，
但那台机器前面根本没人。而**用消息来问会死锁** —— agentd 的消费循环是串行的，
handler 里 await 一条回信，那条回信却要经同一个循环投递进来。

所以走文件，不走消息：

    远端 agentd  →  写 .anthill/approvals/<id>.json，然后轮询等答复
    本机的人     →  `anthill approve --peer lab`（SFTP 读列表、写回答复）
    远端 agentd  →  读到 <id>.answer.json，继续或放弃

轮询是在**同一个协程**里等，不经过消费循环，所以不会死锁。
超时按拒绝处理：没人管的危险操作，默认不做。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from anthill.agent.tools.base import Confirmer
from anthill.core.atomic import atomic_write
from anthill.core.ids import is_valid_id, new_id, now

APPROVALS_DIR = "approvals"
ANSWER_SUFFIX = ".answer.json"
REQUEST_SUFFIX = ".json"
DEFAULT_TIMEOUT = 600.0
POLL_INTERVAL = 1.0


class ApprovalRequest(BaseModel):
    """一条待审批的请求。

    `id` 强制 ULID：它会被拼进文件路径，而这个模型是从**远端机器**读来的
    （`anthill approve --peer`）—— 不校验的话，一个被攻陷的远端只要把 id 写成
    `../../..../x` 就能指使我们往它任意路径写文件。校验放在模型上，
    所有读到它的地方就都挡住了。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    agent: str
    prompt: str
    thread: str = ""
    created_at: str = ""

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not is_valid_id(value):
            raise ValueError(f"非法审批 id {value!r}：只接受 ULID")
        return value

    @classmethod
    def create(cls, *, agent: str, prompt: str, thread: str = "") -> ApprovalRequest:
        return cls(
            id=new_id(),
            agent=agent,
            prompt=prompt,
            thread=thread,
            created_at=now().isoformat(),
        )


class ApprovalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    approved: bool
    by: str = ""
    answered_at: str = ""


class ApprovalStore:
    """`.anthill/approvals/` 的读写。请求与答复是两个文件，好让两侧各写各的。"""

    def __init__(self, root: Path) -> None:
        self._dir = root / APPROVALS_DIR

    @property
    def dir(self) -> Path:
        return self._dir

    def request_path(self, request_id: str) -> Path:
        if not is_valid_id(request_id):
            raise ValueError(f"非法审批 id {request_id!r}：只接受 ULID")
        return self._dir / f"{request_id}{REQUEST_SUFFIX}"

    def answer_path(self, request_id: str) -> Path:
        if not is_valid_id(request_id):
            raise ValueError(f"非法审批 id {request_id!r}：只接受 ULID")
        return self._dir / f"{request_id}{ANSWER_SUFFIX}"

    # ---------- 远端（提问方）----------

    def submit(self, request: ApprovalRequest) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.request_path(request.id)
        return atomic_write(
            self._dir, self._dir, path.name, request.model_dump_json(indent=2).encode()
        )

    def answer_of(self, request_id: str) -> ApprovalAnswer | None:
        path = self.answer_path(request_id)
        if not path.is_file():
            return None
        try:
            return ApprovalAnswer.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            return None  # 半写状态：当作还没答，下一轮再看

    def close(self, request_id: str) -> None:
        self.request_path(request_id).unlink(missing_ok=True)
        self.answer_path(request_id).unlink(missing_ok=True)

    # ---------- 本机（审批方）----------

    def pending(self) -> list[ApprovalRequest]:
        if not self._dir.is_dir():
            return []
        out: list[ApprovalRequest] = []
        for path in sorted(self._dir.glob(f"*{REQUEST_SUFFIX}")):
            if path.name.endswith(ANSWER_SUFFIX):
                continue
            try:
                request = ApprovalRequest.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if self.answer_of(request.id) is None:
                out.append(request)
        return out

    def answer(self, request_id: str, *, approved: bool, by: str = "") -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = ApprovalAnswer(
            id=request_id, approved=approved, by=by, answered_at=now().isoformat()
        )
        path = self.answer_path(request_id)
        return atomic_write(
            self._dir, self._dir, path.name, payload.model_dump_json(indent=2).encode()
        )

    async def wait(
        self, request_id: str, *, timeout: float, poll: float = POLL_INTERVAL
    ) -> ApprovalAnswer | None:
        """轮询等答复。在调用方的协程里等，不经过消费循环 —— 所以不会死锁。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            answer = self.answer_of(request_id)
            if answer is not None:
                return answer
            await asyncio.sleep(poll)
        return None


def approval_confirmer(
    store: ApprovalStore,
    *,
    agent: str,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = POLL_INTERVAL,
) -> Confirmer:
    """把「等一个人点头」变成策略引擎认识的 Confirmer。"""

    async def ask(prompt: str) -> bool:
        request = ApprovalRequest.create(agent=agent, prompt=prompt)
        store.submit(request)
        try:
            answer = await store.wait(request.id, timeout=timeout, poll=poll)
        finally:
            store.close(request.id)  # 不管等到没等到，别把请求留在目录里
        return bool(answer and answer.approved)  # 超时 = 没人管 = 不做

    return ask
