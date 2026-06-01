from datetime import date
from pathlib import Path

import pytest

from app.assistant_settings import AssistantSettings
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
        task_group_chat_id=-1001,
        task_manager_user_ids=[2],
        task_weekly_summary_enabled=True,
    )


def test_create_and_find_task_by_title(tmp_path: Path) -> None:
    service = AssistantTaskService(settings=_settings(tmp_path), logger=_fake_logger())
    created = service.create_task(
        title="Chốt báo cáo tuần",
        created_by=1,
        source_type="manager",
        assigned_by=2,
        group_chat_id=-1001,
        note="task test",
    )
    assert created["status"] == "todo"
    found = service.find_tasks_by_title("bao cao tuan", include_done=False)
    assert len(found) == 1
    assert found[0]["task_uid"] == created["task_uid"]


def test_update_requires_blocked_and_next_step_for_open_task(tmp_path: Path) -> None:
    service = AssistantTaskService(settings=_settings(tmp_path), logger=_fake_logger())
    created = service.create_task(
        title="Theo dõi đơn COD",
        created_by=1,
        source_type="self",
        assigned_by=1,
        group_chat_id=-1001,
    )
    with pytest.raises(ValueError):
        service.update_task(
            task_uid=created["task_uid"],
            updated_by=1,
            chat_id=1,
            status="doing",
            progress_percent=40,
            note="đang làm",
            blocked_reason="",
            next_step="",
        )


def test_mark_done_and_weekly_snapshot(tmp_path: Path) -> None:
    service = AssistantTaskService(settings=_settings(tmp_path), logger=_fake_logger())
    task_a = service.create_task(
        title="Task A",
        created_by=1,
        source_type="manager",
        assigned_by=2,
        group_chat_id=-1001,
    )
    task_b = service.create_task(
        title="Task B",
        created_by=1,
        source_type="self",
        assigned_by=1,
        group_chat_id=-1001,
    )
    service.update_task(
        task_uid=task_b["task_uid"],
        updated_by=1,
        chat_id=1,
        status="blocked",
        progress_percent=30,
        note="vướng token",
        blocked_reason="thiếu token",
        next_step="xin cấp quyền",
    )
    service.mark_done(task_uid=task_a["task_uid"], updated_by=1, chat_id=1, note="xong")

    snapshot = service.build_weekly_snapshot(reference_date=date.today(), timezone_name="Asia/Ho_Chi_Minh", max_items=5)
    assert snapshot["done_count"] >= 1
    assert snapshot["pending_count"] >= 1
    assert snapshot["blocked_count"] >= 1


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_task_service_test")
