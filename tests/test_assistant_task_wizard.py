from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from app.assistant_bot import TelegramAssistantBot
from app.assistant_settings import AssistantSettings
from app.assistant_storage_service import AssistantStorageService
from app.assistant_task_service import AssistantTaskService


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
        proactive_enabled=False,
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
        sheets_spreadsheet_id="",
        sheets_gid=0,
        tasks_enabled=True,
        task_group_chat_id=-5153224852,
        task_manager_user_ids=[1],
        task_weekly_summary_enabled=True,
    )


def test_task_wizard_happy_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)

    reply1 = bot._try_start_task_wizard(raw="thêm công việc: Feedback khách hàng", user_id=1, chat_id=1)
    assert "Đã nhận tên task" in reply1

    reply2 = bot._continue_task_wizard_if_active(raw="Cần xử lý phản hồi khách chưa hài lòng", user_id=1, chat_id=1)
    assert "Anh nhập deadline" in reply2

    reply3 = bot._continue_task_wizard_if_active(raw="02/06/2026", user_id=1, chat_id=1)
    assert "Anh nhập tình trạng task" in reply3

    reply4 = bot._continue_task_wizard_if_active(raw="đang làm", user_id=1, chat_id=1)
    assert "Đã lưu task thành công" in reply4

    found = tasks.find_tasks_by_title("feedback khach hang", include_done=True, limit=5)
    assert len(found) == 1
    assert found[0]["status"] == "doing"


def test_task_wizard_cancel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)

    bot._try_start_task_wizard(raw="thêm công việc: Test hủy", user_id=1, chat_id=1)
    reply = bot._continue_task_wizard_if_active(raw="/cancel", user_id=1, chat_id=1)
    assert "Đã hủy" in reply
    assert storage.load_task_draft(user_id=1) is None


def _build_bot(
    *,
    settings: AssistantSettings,
    storage: AssistantStorageService,
    tasks: AssistantTaskService,
) -> TelegramAssistantBot:
    return TelegramAssistantBot(
        settings=settings,
        logger=_fake_logger(),
        storage=storage,
        memory=object(),  # type: ignore[arg-type]
        google=object(),  # type: ignore[arg-type]
        openai=object(),  # type: ignore[arg-type]
        internal_ops=object(),  # type: ignore[arg-type]
        approval=object(),  # type: ignore[arg-type]
        scheduler=_FakeScheduler(),  # type: ignore[arg-type]
        tasks=tasks,
    )


class _FakeScheduler:
    def now_local(self) -> datetime:
        return datetime(2026, 5, 28, 10, 0, 0)


def _fake_logger():  # noqa: ANN202
    return logging.getLogger("assistant_task_wizard_test")
