import logging
from pathlib import Path

from app.assistant_bot import TelegramAssistantBot
from app.assistant_models import AssistantIntent, ParsedAssistantCommand
from app.assistant_settings import AssistantSettings


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
        openai_enabled=False,
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
        tasks_enabled=True,
        task_group_chat_id=-5153224852,
        task_manager_user_ids=[2],
    )


def test_manager_can_query_task_in_group(tmp_path: Path) -> None:
    bot = _build_bot(tmp_path)
    command = ParsedAssistantCommand(intent=AssistantIntent.TASK, raw_text="/task report", task_action="report")
    assert bot._can_access_command(user_id=2, chat_id=-5153224852, command=command) is True


def test_manager_cannot_update_task_in_group(tmp_path: Path) -> None:
    bot = _build_bot(tmp_path)
    command = ParsedAssistantCommand(intent=AssistantIntent.TASK, raw_text="/task update a", task_action="update")
    assert bot._can_access_command(user_id=2, chat_id=-5153224852, command=command) is False


def _build_bot(tmp_path: Path) -> TelegramAssistantBot:
    settings = _settings(tmp_path)
    return TelegramAssistantBot(
        settings=settings,
        logger=logging.getLogger("assistant_bot_task_permission_test"),
        storage=object(),  # type: ignore[arg-type]
        memory=object(),  # type: ignore[arg-type]
        google=object(),  # type: ignore[arg-type]
        openai=object(),  # type: ignore[arg-type]
        internal_ops=object(),  # type: ignore[arg-type]
        approval=object(),  # type: ignore[arg-type]
        scheduler=object(),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
    )
