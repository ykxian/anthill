"""ULID 生成与校验。

选 ULID 而非 UUID 的理由（02-protocol §1）：单调递增、可直接作文件名、
按字典序排序即时间序，多个发送方并发生成不冲突 —— 因此邮箱投递无需加锁。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ulid import ULID

ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
"""Crockford Base32，26 位，首位受 48bit 时间戳限制只能是 0-7。"""


def new_id() -> str:
    """生成一条消息 ID（同时是信封文件名）。"""
    return str(ULID())


def new_thread_id() -> str:
    """生成一个会话线程 ID。语义不同于 msg id，故单独开一个函数。"""
    return str(ULID())


def is_valid_id(value: str) -> bool:
    """严格校验：格式对且能被解析。"""
    if not ULID_RE.match(value):
        return False
    try:
        ULID.from_str(value)
    except ValueError:
        return False
    return True


def id_timestamp(value: str) -> datetime:
    """取出 ULID 前缀里的生成时间（UTC）。用于排查乱序/时钟问题。"""
    return ULID.from_str(value).datetime


def now() -> datetime:
    """统一时间源。带时区，避免 naive/aware 混用导致的比较崩溃。"""
    return datetime.now(UTC)
