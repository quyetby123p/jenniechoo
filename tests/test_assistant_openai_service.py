from pathlib import Path
from typing import Any

from app.assistant_openai_service import AssistantOpenAIService
from app.assistant_settings import AssistantSettings


def _settings(tmp_path: Path) -> AssistantSettings:
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
        openai_enabled=True,
        openai_api_key="sk-test-key",
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


def test_redact_text_masks_sensitive_values(tmp_path: Path) -> None:
    service = AssistantOpenAIService(settings=_settings(tmp_path), logger=_fake_logger())
    raw = "token EAAG1234567890 mail test@example.com phone 0912345678 key sk-abc123456789XYZ"
    redacted = service.redact_text(raw)
    assert "EAAG" not in redacted
    assert "test@example.com" not in redacted
    assert "0912345678" not in redacted
    assert "sk-abc123456789XYZ" not in redacted


def test_ask_success_extracts_output_text(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = AssistantOpenAIService(settings=_settings(tmp_path), logger=_fake_logger())

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "model": "gpt-4.1-mini",
                "output": [
                    {"content": [{"text": "Chao anh, day la cau tra loi."}]},
                ],
            }

        text = "{}"

    def _fake_request(**_kwargs):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr("requests.request", _fake_request)
    result = service.ask(question="Xin chao", context_blocks=["ctx"])
    assert result["ok"] is True
    assert "cau tra loi" in result["answer"].lower()


def test_ask_insufficient_quota_maps_user_message(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = AssistantOpenAIService(settings=_settings(tmp_path), logger=_fake_logger())

    class _Resp:
        status_code = 429
        text = '{"error":{"message":"You exceeded your current quota.","type":"insufficient_quota","code":"insufficient_quota"}}'

        @staticmethod
        def json() -> dict[str, Any]:
            return {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}

    def _fake_request(**_kwargs):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr("requests.request", _fake_request)
    result = service.ask(question="Xin chao", context_blocks=["ctx"])
    assert result["ok"] is False
    assert result.get("error_code") == "insufficient_quota"
    assert "quota" in str(result.get("user_message", "")).lower()


def test_ask_openai_disabled_returns_internal_mode_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings = AssistantSettings(
        **{**settings.__dict__, "openai_enabled": False, "openai_api_key": ""}
    )
    service = AssistantOpenAIService(settings=settings, logger=_fake_logger())
    result = service.ask(question="Xin chao", context_blocks=["ctx"])
    assert result["ok"] is False
    assert result.get("error_code") == "openai_disabled"
    assert "bot3_openai_enabled=0" in str(result.get("user_message", "")).lower()


def _fake_logger():  # noqa: ANN202
    import logging

    return logging.getLogger("assistant_openai_test")
