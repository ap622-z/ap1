"""领域记录 dataclass，供 repository / worker / agent 传递。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class UserRow:
    id: int
    nickname: str
    token_hash: str
    status: int
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


@dataclass
class SessionRow:
    id: int
    user_id: int
    status: int
    summary_text: str | None
    summary_tokens: int | None
    summary_seq: int | None
    last_active_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class MessageRow:
    id: int
    session_id: int
    seq: int
    type: str  # user / agent / tool
    payload: dict[str, Any]
    token_count: int
    client_message_id: str | None
    status: str | None  # received / processing / replied（仅 user 消息有值）
    origin_message_id: int | None
    created_at: datetime


@dataclass
class ToolRecord:
    name: str
    input: dict[str, Any]
    output: Any
    tool_call_id: str | None = None


@dataclass
class AgentRunResult:
    reply_text: str
    tool_records: list[ToolRecord] = field(default_factory=list)
    run_id: str = ""
    compressed: bool = False


@dataclass
class IdolInfoRow:
    id: int
    tag: str
    content: str
    event_time: str | None
    content_hash: str
    status: int


@dataclass
class SongRow:
    id: int
    song_title: str
    album: str | None
    release_time: str | None
    company: str | None
    creators: str | None
    collaboration: str | None
    intro: str | None
    content_hash: str
    status: int


@dataclass
class LyricRow:
    id: int
    song_id: int
    seg_no: int
    content: str
    content_hash: str
    status: int
