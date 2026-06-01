from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from app.assistant_bot import (
    TelegramAssistantBot,
    _build_web_search_reply,
    _extract_recent_activity_lines,
    _looks_like_activity_question,
    _looks_like_external_lookup_question,
)
from app.assistant_models import AssistantMemoryHit
from app.assistant_settings import AssistantSettings


def _settings(tmp_path: Path, *, openai_enabled: bool = False) -> AssistantSettings:
    return AssistantSettings(
        project_root=tmp_path,
        workspace_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        memory_root=tmp_path / "memory",
        memory_index_path=tmp_path / "storage" / "assistant" / "memory.db",
        telegram_bot_token="token",
        telegram_allowed_user_id=1,
        timezone_name="Asia/Ho_Chi_Minh",
        proactive_enabled=True,
        agenda_hour=8,
        event_reminder_lead_minutes=30,
        eod_hour=21,
        redaction_enabled=True,
        rate_limit_per_minute=20,
        openai_enabled=openai_enabled,
        openai_api_key="",
        openai_model="gpt-4.1-mini",
        openai_timeout_seconds=30,
        openai_max_tokens=400,
        openai_retry_max=1,
        openai_retry_backoff_seconds=[1],
        google_oauth_client_id="id",
        google_oauth_client_secret="secret",
        google_oauth_refresh_token="refresh",
        google_oauth_token_uri="https://oauth2.googleapis.com/token",
        google_calendar_ids=["primary"],
        gmail_query_default="is:unread",
        sheets_spreadsheet_id="sheet",
        sheets_gid=0,
    )


def test_looks_like_activity_question_handles_natural_sentence() -> None:
    assert _looks_like_activity_question("nay anh va em da lam nhung viec gi") is True


def test_looks_like_external_lookup_question() -> None:
    assert _looks_like_external_lookup_question("python la gi") is True
    assert _looks_like_external_lookup_question("ket qua hom nay the nao") is False


def test_extract_recent_activity_lines_filters_sensitive_content(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    note_file = memory_dir / "2026-05-19.md"
    note_file.write_text(
        "- Đã hoàn thành báo cáo ngày.\n"
        "- BOT3_TELEGRAM_TOKEN=abc\n"
        "- Đã restart bot assistant để chạy ổn định.\n",
        encoding="utf-8",
    )
    lines = _extract_recent_activity_lines(note_file, max_items=10)
    assert any("hoàn thành báo cáo ngày" in line.lower() for line in lines)
    assert all("telegram_token" not in line.lower() for line in lines)


def test_general_qa_activity_question_returns_digest_without_command_prompt(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_enabled=False)
    memory_dir = settings.workspace_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    note_file = memory_dir / "2026-05-19.md"
    note_file.write_text(
        "- Updated bot 3 trả lời tự nhiên.\n"
        "- Added fallback tìm thông tin ngoài luồng.\n",
        encoding="utf-8",
    )

    bot = TelegramAssistantBot(
        settings=settings,
        logger=logging.getLogger("assistant_bot_general_qa_test"),
        storage=object(),  # type: ignore[arg-type]
        memory=_FakeMemory([]),  # type: ignore[arg-type]
        google=object(),  # type: ignore[arg-type]
        openai=_FakeOpenAI(),  # type: ignore[arg-type]
        internal_ops=_FakeInternalOps(),  # type: ignore[arg-type]
        approval=object(),  # type: ignore[arg-type]
        scheduler=_FakeScheduler(datetime(2026, 5, 19, 19, 30, 0)),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
    )

    reply = asyncio.run(bot._build_general_qa_reply("nay anh va em da lam nhung viec gi"))
    assert "Tóm tắt công việc ngày 19/05/2026" in reply
    assert "bot 3 trả lời tự nhiên" in reply.lower()
    assert "/agenda" not in reply


def test_general_qa_openai_disabled_prefers_local_reasoning(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_enabled=False)
    hits = [
        AssistantMemoryHit(
            source="workspace_memory",
            path="memory/projects/INDEX.md",
            excerpt="Project Memory Index. Mục tiêu giúp route nhanh đến đúng context.",
            score=3.0,
            timestamp="2026-05-19T00:00:00Z",
        )
    ]
    bot = TelegramAssistantBot(
        settings=settings,
        logger=logging.getLogger("assistant_bot_general_qa_test"),
        storage=object(),  # type: ignore[arg-type]
        memory=_FakeMemory(hits),  # type: ignore[arg-type]
        google=object(),  # type: ignore[arg-type]
        openai=_FakeOpenAI(),  # type: ignore[arg-type]
        internal_ops=_FakeInternalOps(),  # type: ignore[arg-type]
        approval=object(),  # type: ignore[arg-type]
        scheduler=_FakeScheduler(datetime(2026, 5, 19, 19, 30, 0)),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
    )

    reply = asyncio.run(bot._build_general_qa_reply("mục tiêu index project là gì"))
    assert "Theo dữ liệu nội bộ hiện có" in reply
    assert "route nhanh đến đúng context" in reply
    assert "BOT3_OPENAI_ENABLED" not in reply


def test_general_qa_openai_disabled_lookup_question_prefers_web(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _settings(tmp_path, openai_enabled=False)
    hits = [
        AssistantMemoryHit(
            source="workspace_memory",
            path="memory/2026-05-19.md",
            excerpt="Implemented MVP source code for Telegram bot using Python.",
            score=2.0,
            timestamp="2026-05-19T00:00:00Z",
        )
    ]
    bot = TelegramAssistantBot(
        settings=settings,
        logger=logging.getLogger("assistant_bot_general_qa_test"),
        storage=object(),  # type: ignore[arg-type]
        memory=_FakeMemory(hits),  # type: ignore[arg-type]
        google=object(),  # type: ignore[arg-type]
        openai=_FakeOpenAI(),  # type: ignore[arg-type]
        internal_ops=_FakeInternalOps(),  # type: ignore[arg-type]
        approval=object(),  # type: ignore[arg-type]
        scheduler=_FakeScheduler(datetime(2026, 5, 19, 19, 30, 0)),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
    )

    class _Resp:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "AbstractText": "Python là ngôn ngữ lập trình bậc cao.",
                "AbstractURL": "https://duckduckgo.com/Python_(programming_language)",
            }

    def _fake_get(*_args, **_kwargs):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr("requests.get", _fake_get)
    reply = asyncio.run(bot._build_general_qa_reply("python là gì"))
    assert "Em tìm nhanh ngoài luồng được như sau" in reply
    assert "Python là ngôn ngữ lập trình" in reply


def test_build_web_search_reply_from_duckduckgo_payload(monkeypatch) -> None:  # noqa: ANN001
    class _Resp:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "AbstractText": "Python là ngôn ngữ lập trình bậc cao.",
                "AbstractURL": "https://duckduckgo.com/Python_(programming_language)",
            }

    def _fake_get(*_args, **_kwargs):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr("requests.get", _fake_get)
    reply = _build_web_search_reply("python là gì")
    assert "Em tìm nhanh ngoài luồng được như sau" in reply
    assert "Python là ngôn ngữ lập trình" in reply
    assert "Nguồn web:" in reply


def test_build_web_search_reply_fallbacks_to_wikipedia(monkeypatch) -> None:  # noqa: ANN001
    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        @staticmethod
        def raise_for_status() -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    calls: list[str] = []

    def _fake_get(url: str, *args, **kwargs):  # noqa: ANN001
        calls.append(url)
        if "duckduckgo.com" in url:
            return _Resp(
                {
                    "AbstractText": "",
                    "Answer": "",
                    "Definition": "",
                    "RelatedTopics": [],
                }
            )
        if "wikipedia.org" in url and kwargs.get("params", {}).get("list") == "search":
            return _Resp(
                {
                    "query": {
                        "search": [
                            {
                                "title": "Python (ngôn ngữ lập trình)",
                                "pageid": 21782,
                                "snippet": "Python là ngôn ngữ lập trình bậc cao.",
                            }
                        ]
                    }
                }
            )
        if "wikipedia.org" in url and kwargs.get("params", {}).get("prop") == "extracts":
            return _Resp(
                {
                    "query": {
                        "pages": {
                            "21782": {
                                "extract": "Python là ngôn ngữ lập trình bậc cao đa năng."
                            }
                        }
                    }
                }
            )
        return _Resp({})

    monkeypatch.setattr("requests.get", _fake_get)
    reply = _build_web_search_reply("python là gì")
    assert "Em tìm nhanh ngoài luồng được như sau" in reply
    assert "Python là ngôn ngữ lập trình bậc cao đa năng" in reply
    assert "wikipedia.org" in reply
    assert any("duckduckgo.com" in url for url in calls)


class _FakeMemory:
    def __init__(self, hits: list[AssistantMemoryHit]) -> None:
        self._hits = hits

    def search(self, _query: str, *, limit: int = 6) -> list[AssistantMemoryHit]:
        return self._hits[:limit]


class _FakeOpenAI:
    def ask(self, **_kwargs):  # noqa: ANN003, ANN204
        return {"ok": False, "user_message": "OpenAI unavailable"}

    def is_configured(self) -> tuple[bool, str]:
        return False, "disabled"


class _FakeInternalOps:
    def collect_result_snapshot(self, _target_date):  # noqa: ANN001, ANN202
        return {
            "daily_report": {
                "pos": {"revenue_total_vnd": 1200000},
                "ads": {"spend_vnd": 300000},
                "roas": 4.0,
            }
        }


class _FakeScheduler:
    def __init__(self, now_value: datetime) -> None:
        self._now_value = now_value

    def now_local(self) -> datetime:
        return self._now_value
