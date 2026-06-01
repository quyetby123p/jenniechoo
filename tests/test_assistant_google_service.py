from pathlib import Path
from typing import Any

from app.assistant_google_service import AssistantGoogleService
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
        sheets_spreadsheet_id="sheet_123",
        sheets_gid=777,
    )


def test_fetch_agenda_success(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = AssistantGoogleService(settings=_settings(tmp_path), logger=_fake_logger())

    class _Resp:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self) -> dict[str, Any]:
            return self._payload

    def _fake_request(method: str, url: str, **_kwargs):  # noqa: ANN001
        if "oauth2.googleapis.com/token" in url:
            return _Resp(200, {"access_token": "token", "expires_in": 3600})
        if "/calendar/v3/calendars/" in url and "/events" in url:
            return _Resp(
                200,
                {
                    "items": [
                        {
                            "id": "evt_1",
                            "summary": "Hop buoi sang",
                            "start": {"dateTime": "2026-05-19T09:00:00+07:00"},
                            "end": {"dateTime": "2026-05-19T09:30:00+07:00"},
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr("app.assistant_google_service.requests.request", _fake_request)
    payload = service.fetch_agenda()
    assert payload["count"] == 1
    assert payload["events"][0]["summary"] == "Hop buoi sang"


def test_fetch_sheet_snapshot_success(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = AssistantGoogleService(settings=_settings(tmp_path), logger=_fake_logger())

    class _Resp:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self) -> dict[str, Any]:
            return self._payload

    def _fake_request(method: str, url: str, **_kwargs):  # noqa: ANN001
        if "oauth2.googleapis.com/token" in url:
            return _Resp(200, {"access_token": "token", "expires_in": 3600})
        if "/spreadsheets/sheet_123" in url and "/values/" not in url:
            return _Resp(200, {"sheets": [{"properties": {"sheetId": 777, "title": "Data"}}]})
        if "/spreadsheets/sheet_123/values/" in url:
            return _Resp(200, {"values": [["A", "B"], ["1", "2"]]})
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr("app.assistant_google_service.requests.request", _fake_request)
    payload = service.fetch_sheet_snapshot(max_rows=5)
    assert payload["ok"] is True
    assert payload["sheet_title"] == "Data"
    assert payload["row_count"] == 2


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_google_test")
