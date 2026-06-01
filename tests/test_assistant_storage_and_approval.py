from pathlib import Path

from app.assistant_approval_service import AssistantApprovalService
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


def test_storage_pending_request_and_idempotency(tmp_path: Path) -> None:
    storage = AssistantStorageService(settings=_settings(tmp_path), logger=_fake_logger())
    request_id = storage.create_pending_request({"action_name": "daily_report"})
    request = storage.get_pending_request(request_id)
    assert request is not None
    assert request["action_name"] == "daily_report"
    assert storage.is_request_processed(request_id) is False
    storage.mark_request_processed(request_id)
    assert storage.is_request_processed(request_id) is True


def test_approval_parse_callback() -> None:
    approval = AssistantApprovalService()
    parsed_ok = approval.parse_callback(f"{approval.CONFIRM_PREFIX}abc")
    parsed_no = approval.parse_callback(f"{approval.CANCEL_PREFIX}abc")
    assert parsed_ok is not None
    assert parsed_ok.action == "confirm_action"
    assert parsed_no is not None
    assert parsed_no.action == "cancel_action"


def test_task_draft_roundtrip(tmp_path: Path) -> None:
    storage = AssistantStorageService(settings=_settings(tmp_path), logger=_fake_logger())
    payload = {"mode": "task_create_wizard", "step": "await_deadline", "title": "Feedback khách hàng"}
    storage.save_task_draft(user_id=1, payload=payload)
    loaded = storage.load_task_draft(user_id=1)
    assert loaded is not None
    assert loaded["mode"] == "task_create_wizard"
    assert loaded["title"] == "Feedback khách hàng"
    storage.delete_task_draft(user_id=1)
    assert storage.load_task_draft(user_id=1) is None


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_storage_test")
