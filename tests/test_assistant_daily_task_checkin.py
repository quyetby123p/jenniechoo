from __future__ import annotations

import asyncio
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
        daily_task_checkin_enabled=True,
    )


def test_daily_task_morning_reply_creates_tasks_and_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)
    storage.save_task_draft(user_id=1, payload={"mode": "daily_task_morning", "date": "2026-06-04"})

    reply = bot._continue_daily_task_checkin_if_active(
        raw="- Báo cáo task công việc hằng ngày AI\n2. Kiểm tra web report",
        user_id=1,
        chat_id=1,
    )

    assert "Em đã lưu 2 task" in reply
    listed = tasks.list_tasks(status="pending", limit=10)
    assert [item["deadline_date"] for item in listed] == ["2026-06-04", "2026-06-04"]
    state = storage.load_daily_task_checkin_state()
    day_state = state["days"]["2026-06-04"]
    assert day_state["morning_answered"] is True
    assert len(day_state["task_uids"]) == 2


def test_daily_task_morning_no_tasks_skips_evening(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)
    storage.save_task_draft(user_id=1, payload={"mode": "daily_task_morning", "date": "2026-06-04"})

    reply = bot._continue_daily_task_checkin_if_active(raw="không có", user_id=1, chat_id=1)

    assert "không hỏi tiến độ" in reply
    state = storage.load_daily_task_checkin_state()
    day_state = state["days"]["2026-06-04"]
    assert day_state["no_tasks"] is True
    assert tasks.list_tasks(status="pending", limit=10) == []


def test_daily_task_evening_reply_updates_by_index_and_reports_unmatched(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    first = tasks.create_task(
        title="Báo cáo task công việc hằng ngày AI",
        created_by=1,
        source_type="self",
        assigned_by=1,
        group_chat_id=-5153224852,
        deadline_date="2026-06-04",
    )
    second = tasks.create_task(
        title="Kiểm tra web report",
        created_by=1,
        source_type="self",
        assigned_by=1,
        group_chat_id=-5153224852,
        deadline_date="2026-06-04",
    )
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)
    storage.save_task_draft(
        user_id=1,
        payload={
            "mode": "daily_task_evening",
            "date": "2026-06-04",
            "task_uids": [first["task_uid"], second["task_uid"]],
        },
    )

    reply = bot._continue_daily_task_checkin_if_active(
        raw="1. xong\n2. đang làm 60% - còn phần test\n3. dòng lạc",
        user_id=1,
        chat_id=1,
    )

    assert "Em đã cập nhật 2 task" in reply
    assert "dòng lạc" in reply
    assert tasks.get_task(first["task_uid"])["status"] == "done"
    updated_second = tasks.get_task(second["task_uid"])
    assert updated_second["status"] == "doing"
    assert updated_second["progress_percent"] == 60
    assert updated_second["next_step"] == "Tiếp tục xử lý"


def test_daily_task_prompt_writes_drafts_and_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    storage = AssistantStorageService(settings=settings, logger=_fake_logger())
    tasks = AssistantTaskService(settings=settings, logger=_fake_logger())
    bot = _build_bot(settings=settings, storage=storage, tasks=tasks)
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
        sent.append((chat_id, text))

    bot._bot_send_message = fake_send  # type: ignore[method-assign]
    state: dict = {}
    day_state = bot._get_daily_task_day_state(state, "2026-06-04")

    asyncio.run(bot._send_daily_task_morning_prompt(day_key="2026-06-04", state=state, day_state=day_state))

    assert sent and "hôm nay anh có công việc gì" in sent[0][1]
    assert storage.load_task_draft(user_id=1)["mode"] == "daily_task_morning"
    assert storage.load_daily_task_checkin_state()["days"]["2026-06-04"]["morning_sent"] is True


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
        return datetime(2026, 6, 4, 10, 0, 0)


def _fake_logger():  # noqa: ANN202
    return logging.getLogger("assistant_daily_task_checkin_test")
