"""`anthill state` —— 节点内共享公告的直接、零消息读写入口。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import typer
from pydantic import JsonValue, ValidationError

from anthill.cli.common import fail, load, read_body
from anthill.core.errors import AntHillError
from anthill.core.paths import NodeLayout
from anthill.core.state_sync import (
    STATE_SCOPE,
    StateDocumentUpdate,
    StatePublishStatus,
    StateRecord,
    StateStore,
)

state_app = typer.Typer(no_args_is_help=True, help="节点内共享状态公告（不发送消息、不调用模型）")


def _store(layout: NodeLayout) -> StateStore:
    root = layout.state_dir
    try:
        contained = root.resolve().is_relative_to(layout.workspace.resolve())
    except OSError as exc:
        fail(f"无法解析共享 state 路径：{exc}")
    if not contained:
        fail("共享 state 路径离开了当前 workspace，拒绝访问")
    return StateStore(root)


def _record_metadata(record: StateRecord, *, updated_at: str) -> dict[str, object]:
    return {
        "key": record.key,
        "publisher": record.publisher,
        "revision": record.revision,
        "digest": record.digest,
        "summary": record.summary,
        "updated_at": updated_at,
    }


def _record_json(record: StateRecord, *, updated_at: str) -> dict[str, object]:
    return {**_record_metadata(record, updated_at=updated_at), "snapshot": record.snapshot}


def _updated_at(store: StateStore, key: str) -> str:
    timestamp = store.path_for(key).stat().st_mtime
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


@state_app.command("list")
def list_state(
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """列出本节点共享公告的元数据；不返回 snapshot。"""
    layout, config = load(workspace)
    try:
        store = _store(layout)
        records = store.list()
        metadata = [
            _record_metadata(record, updated_at=_updated_at(store, record.key))
            for record in records
        ]
    except (AntHillError, OSError, ValueError) as exc:
        fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "scope": STATE_SCOPE,
                "node": config.node.name,
                "workspace": str(layout.workspace),
                "states": metadata,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@state_app.command("show")
def show_state(
    key: str = typer.Argument(..., help="状态 key，例如 project.board"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """按需输出本节点的一份完整状态公告。"""
    layout, config = load(workspace)
    try:
        store = _store(layout)
        record = store.load(key)
    except (AntHillError, OSError, ValueError) as exc:
        fail(str(exc))
    if record is None:
        fail(f"本节点没有 state key {key!r}")
    try:
        state = _record_json(record, updated_at=_updated_at(store, record.key))
    except OSError as exc:
        fail(str(exc))
    typer.echo(
        json.dumps(
            {
                "scope": STATE_SCOPE,
                "node": config.node.name,
                "workspace": str(layout.workspace),
                "state": state,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


@state_app.command("publish")
def publish_state(
    key: str = typer.Argument(..., help="状态 key，例如 project.board"),
    snapshot: str = typer.Argument(..., help="JSON object；`-` 读 stdin，`@文件` 读文件"),
    revision: int = typer.Option(..., "--revision", min=1, help="明确的单调 revision，不自动猜"),
    summary: str = typer.Option(..., "--summary", help="本版短摘要（最多 500 字）"),
    publisher_name: str = typer.Option("cli", "--from", "-f", help="以哪个已配置 Agent 发布"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
) -> None:
    """直接原子发布共享文档；不会创建信封、投递、回执或唤醒 Agent。"""
    layout, config = load(workspace)
    if publisher_name not in config.agents:
        fail(
            f"本节点没有 Agent {publisher_name!r}；可作为发布者的有："
            + ", ".join(sorted(config.agents))
        )
    raw = read_body(snapshot)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"snapshot 不是合法 JSON：{exc}")
    if not isinstance(parsed, dict):
        fail("snapshot 必须是 JSON object，不能是数组、字符串或标量")

    try:
        update = StateDocumentUpdate.from_snapshot(
            key=key,
            revision=revision,
            summary=summary,
            snapshot=cast(dict[str, JsonValue], parsed),
        )
        publisher = f"{config.node.name}:{publisher_name}"
        result = _store(layout).publish(update, publisher=publisher)
    except (AntHillError, ValidationError, ValueError) as exc:
        fail(str(exc))

    output = {
        "scope": STATE_SCOPE,
        "node": config.node.name,
        "status": str(result.status),
        "key": result.key,
        "publisher": result.publisher,
        "revision": result.incoming_revision,
        "current_revision": result.current_revision,
        "digest": result.digest,
        "path": str(result.path),
        "reason": result.reason,
        "skipped_revisions": result.skipped_revisions,
    }
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if result.status is StatePublishStatus.STALE:
        fail("旧 revision 未发布；请先读取当前状态后再生成新 revision")
    if result.status is StatePublishStatus.CONFLICT:
        fail(f"状态发布冲突：{result.reason}")
