from datetime import date
import logging
from pathlib import Path

from app.media_settings import MediaSettings
from app.media_storage_service import MediaStorageService


def _dummy_settings(tmp_path: Path) -> MediaSettings:
    return MediaSettings(
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
        platform_allowlist=["facebook.com", "tiktok.com"],
        market_scope="VN+TH+GLOBAL",
        cloudinary_cloud_name="cloud",
        cloudinary_upload_preset="preset",
        sheet_enabled=True,
        sheet_mode="oauth_user",
        sheet_spreadsheet_id="sheet_1",
        sheet_gid=844064194,
        sheet_oauth_client_id="cid",
        sheet_oauth_client_secret="csecret",
        sheet_oauth_refresh_token="rtoken",
        sheet_oauth_token_uri="https://oauth2.googleapis.com/token",
    )


def test_quota_counter_resets_by_date(tmp_path: Path) -> None:
    service = MediaStorageService(_dummy_settings(tmp_path), logging.getLogger("test_media_storage"))

    d1 = date(2026, 5, 19)
    assert service.get_today_quota_usage(d1) == 0
    assert service.increment_today_quota(d1) == 1
    assert service.increment_today_quota(d1) == 2
    assert service.get_today_quota_usage(d1) == 2

    d2 = date(2026, 5, 20)
    assert service.get_today_quota_usage(d2) == 0


def test_pending_request_roundtrip(tmp_path: Path) -> None:
    service = MediaStorageService(_dummy_settings(tmp_path), logging.getLogger("test_media_storage_req"))
    req_id = service.create_pending_request({"run_id": "run_1"}, request_type="media_sheet_sync")
    loaded = service.get_pending_request(req_id)
    assert loaded is not None
    assert loaded["run_id"] == "run_1"
    assert loaded["request_type"] == "media_sheet_sync"
    service.delete_pending_request(req_id)
    assert service.get_pending_request(req_id) is None


def test_run_save_load_update(tmp_path: Path) -> None:
    service = MediaStorageService(_dummy_settings(tmp_path), logging.getLogger("test_media_storage_run"))
    payload = {"run_id": "run_abc", "status": "completed", "items": []}
    service.save_run(payload)
    loaded = service.load_run("run_abc")
    assert loaded is not None
    assert loaded["status"] == "completed"

    updated = service.update_run("run_abc", {"status": "sheet_synced"})
    assert updated["status"] == "sheet_synced"
