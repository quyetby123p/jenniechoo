from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.assistant_scheduler_service import AssistantSchedulerService
from app.assistant_settings import AssistantSettings
from app.assistant_storage_service import AssistantStorageService


def _settings(tmp_path: Path) -> AssistantSettings:
    return AssistantSettings(
        project_root=tmp_path,
        workspace_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        memory_root=tmp_path / "memory",
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


def test_day_mark_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    scheduler = AssistantSchedulerService(settings=settings, storage=storage)

    assert scheduler.should_send_day_mark("agenda", "2026-05-19") is True
    scheduler.mark_day_sent("agenda", "2026-05-19")
    assert scheduler.should_send_day_mark("agenda", "2026-05-19") is False
    assert scheduler.should_send_day_mark("agenda", "2026-05-20") is True


def test_pick_due_event_reminders(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    scheduler = AssistantSchedulerService(settings=settings, storage=storage)

    now_local = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    start_dt = now_local + timedelta(minutes=settings.event_reminder_lead_minutes)
    events = [
        {
            "event_id": "evt_1",
            "start_iso": start_dt.isoformat(),
            "summary": "Hop team",
        }
    ]
    due = scheduler.pick_due_event_reminders(events, now_local=now_local)
    assert len(due) == 1
    scheduler.mark_event_reminded(due[0])
    due_after = scheduler.pick_due_event_reminders(events, now_local=now_local)
    assert due_after == []


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_scheduler_test")
