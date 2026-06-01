import logging
from pathlib import Path

from app.media_settings import MediaSettings
from app.media_sheet_service import MediaSheetService


def _dummy_settings(tmp_path: Path, **overrides) -> MediaSettings:
    base = MediaSettings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage" / "media_research",
        logs_root=tmp_path / "logs" / "media_bot",
        state_root=tmp_path / "state" / "media_bot",
        telegram_bot_token="token",
        telegram_allowed_user_id=1,
        daily_run_cap=30,
        timezone_name="Asia/Ho_Chi_Minh",
        serpapi_api_key="serpapi",
        max_image_results=20,
        max_video_results=20,
        max_api_calls_per_run=5,
        platform_allowlist=["facebook.com"],
        market_scope="VN+TH+GLOBAL",
        cloudinary_cloud_name="cloud",
        cloudinary_upload_preset="preset",
        sheet_enabled=True,
        sheet_mode="oauth_user",
        sheet_spreadsheet_id="sheet_123",
        sheet_gid=844064194,
        sheet_oauth_client_id="cid",
        sheet_oauth_client_secret="secret",
        sheet_oauth_refresh_token="refresh",
        sheet_oauth_token_uri="https://oauth2.googleapis.com/token",
    )
    payload = {**base.__dict__, **overrides}
    return MediaSettings(**payload)


def test_is_configured_requires_oauth_fields(tmp_path: Path) -> None:
    service = MediaSheetService(
        _dummy_settings(tmp_path, sheet_oauth_client_id="", sheet_oauth_client_secret="", sheet_oauth_refresh_token=""),
        logging.getLogger("test_media_sheet_cfg"),
    )
    ok, reason = service.is_configured()
    assert ok is False
    assert "MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID" in reason


def test_sync_rows_upsert(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaSheetService(_dummy_settings(tmp_path), logging.getLogger("test_media_sheet_sync"))

    updated_payload: list[tuple[int, list]] = []
    appended_payload: list[list] = []

    monkeypatch.setattr(service, "_refresh_oauth_user_access_token", lambda: "token")
    monkeypatch.setattr(service, "_resolve_sheet_title", lambda *_args, **_kwargs: "Reseach media")
    monkeypatch.setattr(service, "_ensure_header", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_load_existing_dedupe_map", lambda **_kwargs: {"SKU1|image|https://a": 5})

    def _capture_updates(*, updates, **_kwargs):  # noqa: ANN001
        updated_payload.extend(updates)

    def _capture_appends(*, rows, **_kwargs):  # noqa: ANN001
        appended_payload.extend(rows)

    monkeypatch.setattr(service, "_batch_update_rows", _capture_updates)
    monkeypatch.setattr(service, "_append_rows", _capture_appends)

    rows = [
        {
            "created_at": "2026-05-19T00:00:00Z",
            "run_id": "run_1",
            "product_code": "SKU1",
            "query_text": "sku",
            "market_scope": "VN+TH+GLOBAL",
            "media_type": "image",
            "platform": "facebook.com",
            "title": "A",
            "source_url": "https://facebook.com/a",
            "direct_media_url": "https://a",
            "thumbnail_url": "",
            "snippet": "",
            "engine": "google_images",
            "engine_query": "sku",
            "score": 80,
            "status": "ready",
            "dedupe_key": "SKU1|image|https://a",
        },
        {
            "created_at": "2026-05-19T00:00:00Z",
            "run_id": "run_1",
            "product_code": "SKU1",
            "query_text": "sku",
            "market_scope": "VN+TH+GLOBAL",
            "media_type": "video",
            "platform": "facebook.com",
            "title": "B",
            "source_url": "https://facebook.com/b",
            "direct_media_url": "https://b",
            "thumbnail_url": "",
            "snippet": "",
            "engine": "google_videos",
            "engine_query": "sku",
            "score": 70,
            "status": "ready",
            "dedupe_key": "SKU1|video|https://b",
        },
        {
            "created_at": "2026-05-19T00:00:00Z",
            "run_id": "run_1",
            "product_code": "SKU1",
            "query_text": "sku",
            "market_scope": "VN+TH+GLOBAL",
            "media_type": "video",
            "platform": "facebook.com",
            "title": "B2",
            "source_url": "https://facebook.com/b2",
            "direct_media_url": "https://b2",
            "thumbnail_url": "",
            "snippet": "",
            "engine": "google_videos",
            "engine_query": "sku",
            "score": 60,
            "status": "ready",
            "dedupe_key": "SKU1|video|https://b",
        },
    ]

    result = service.sync_rows(rows)

    assert result["ok"] is True
    assert result["updated"] == 1
    assert result["inserted"] == 1
    assert result["skipped"] == 1
    assert len(updated_payload) == 1
    assert len(appended_payload) == 1
