from pathlib import Path

from app.assistant_memory_service import AssistantMemoryService
from app.assistant_settings import AssistantSettings


def _settings(tmp_path: Path) -> AssistantSettings:
    memory_root = tmp_path / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    return AssistantSettings(
        project_root=tmp_path,
        workspace_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        memory_root=memory_root,
        memory_index_path=tmp_path / "storage" / "assistant_bot" / "memory.db",
        telegram_bot_token="token",
        telegram_allowed_user_id=1,
        timezone_name="Asia/Ho_Chi_Minh",
        proactive_enabled=True,
        agenda_hour=8,
        event_reminder_lead_minutes=30,
        eod_hour=21,
        redaction_enabled=True,
        rate_limit_per_minute=20,
        openai_enabled=True,
        openai_api_key="sk-test",
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


def test_rebuild_index_and_search(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.memory_root / "2026-05-19.md").write_text(
        "Hôm nay đã hoàn thành daily report và đối soát COD thành công.\n",
        encoding="utf-8",
    )
    logger = _fake_logger()
    service = AssistantMemoryService(settings=settings, logger=logger)
    result = service.rebuild_index()
    assert result["ok"] is True
    assert result["files_total"] >= 1

    hits = service.search("đối soát cod", limit=3)
    assert len(hits) >= 1
    assert "cod" in hits[0].excerpt.lower()


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_memory_test")
