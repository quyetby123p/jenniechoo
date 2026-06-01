from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class AssistantIntent(str, Enum):
    AGENDA = "agenda"
    PLAN = "plan"
    RESULT = "result"
    ACTION = "action"
    TASK = "task"
    GENERAL_QA = "general_qa"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AssistantActionRequest:
    request_id: str
    action_type: str
    payload: dict[str, Any]
    risk_level: str
    created_at: str


@dataclass(frozen=True)
class AssistantMemoryHit:
    source: str
    path: str
    excerpt: str
    score: float
    timestamp: str


@dataclass(frozen=True)
class AssistantReply:
    text: str
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ParsedAssistantCommand:
    intent: AssistantIntent
    raw_text: str
    date_value: date | None = None
    week_mode: bool = False
    action_name: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    task_action: str = ""
    task_args: dict[str, Any] = field(default_factory=dict)
    question_text: str = ""
