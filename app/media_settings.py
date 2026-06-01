from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

from app.exceptions import ConfigError


@dataclass(frozen=True)
class MediaSettings:
    project_root: Path
    storage_root: Path
    logs_root: Path
    state_root: Path
    telegram_bot_token: str
    telegram_allowed_user_id: int
    daily_run_cap: int
    timezone_name: str
    serpapi_api_key: str
    max_image_results: int
    max_video_results: int
    max_api_calls_per_run: int
    platform_allowlist: list[str]
    market_scope: str
    cloudinary_cloud_name: str
    cloudinary_upload_preset: str
    sheet_enabled: bool
    sheet_mode: str
    sheet_spreadsheet_id: str
    sheet_gid: int
    sheet_oauth_client_id: str
    sheet_oauth_client_secret: str
    sheet_oauth_refresh_token: str
    sheet_oauth_token_uri: str
    media_research_enabled: bool = True
    work_progress_enabled: bool = False

    @property
    def runs_dir(self) -> Path:
        return self.storage_root / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.storage_root / "reports"

    @property
    def pending_requests_dir(self) -> Path:
        return self.state_root / "pending_requests"

    @property
    def quota_state_file(self) -> Path:
        return self.state_root / "quota_state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_root / "media_bot.instance.lock"


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {key}")
    return value


def _optional_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _parse_bool(raw: str, default: bool) -> bool:
    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int(raw: str, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < min_value or value > max_value:
        return default
    return value


def _parse_csv_list(raw: str, *, default: list[str]) -> list[str]:
    parts = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    return parts or list(default)


def load_media_settings(project_root: Path | None = None) -> MediaSettings:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    telegram_bot_token = _require_env("MEDIA_BOT_TELEGRAM_TOKEN")
    telegram_allowed_user_id = int(_require_env("MEDIA_BOT_ALLOWED_USER_ID"))
    serpapi_api_key = _optional_env("MEDIA_RESEARCH_SERPAPI_API_KEY")
    cloudinary_cloud_name = _optional_env("MEDIA_RESEARCH_CLOUDINARY_CLOUD_NAME")
    cloudinary_upload_preset = _optional_env("MEDIA_RESEARCH_CLOUDINARY_UPLOAD_PRESET")
    media_research_enabled = _parse_bool(
        os.getenv("MEDIA_BOT_MEDIA_RESEARCH_ENABLED", "1"),
        default=True,
    )

    daily_run_cap = _parse_int(
        os.getenv("MEDIA_BOT_DAILY_RUN_CAP", "30"),
        default=30,
        min_value=1,
        max_value=10000,
    )
    timezone_name = os.getenv("MEDIA_BOT_TIMEZONE", "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"

    max_image_results = _parse_int(
        os.getenv("MEDIA_RESEARCH_MAX_IMAGE_RESULTS", "20"),
        default=20,
        min_value=1,
        max_value=200,
    )
    max_video_results = _parse_int(
        os.getenv("MEDIA_RESEARCH_MAX_VIDEO_RESULTS", "20"),
        default=20,
        min_value=1,
        max_value=200,
    )
    max_api_calls_per_run = _parse_int(
        os.getenv("MEDIA_RESEARCH_MAX_API_CALLS_PER_RUN", "5"),
        default=5,
        min_value=1,
        max_value=20,
    )
    platform_allowlist = _parse_csv_list(
        os.getenv(
            "MEDIA_RESEARCH_PLATFORM_ALLOWLIST",
            "tiktok.com,instagram.com,facebook.com,pinterest.com,shopee.vn,lazada.vn,tokopedia.com,aliexpress.com,amazon.com,etsy.com,youtube.com",
        ),
        default=["tiktok.com", "instagram.com", "facebook.com", "youtube.com"],
    )
    market_scope = os.getenv("MEDIA_RESEARCH_MARKET_SCOPE", "VN+TH+GLOBAL").strip() or "VN+TH+GLOBAL"

    sheet_enabled = _parse_bool(os.getenv("MEDIA_RESEARCH_SHEET_ENABLED", "1"), default=True)
    sheet_mode = os.getenv("MEDIA_RESEARCH_SHEET_MODE", "oauth_user").strip().lower() or "oauth_user"
    sheet_spreadsheet_id = os.getenv("MEDIA_RESEARCH_SHEET_SPREADSHEET_ID", "").strip()
    sheet_gid = _parse_int(
        os.getenv("MEDIA_RESEARCH_SHEET_GID", "844064194"),
        default=844064194,
        min_value=0,
        max_value=2_147_483_647,
    )
    sheet_oauth_client_id = os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID", "").strip()
    sheet_oauth_client_secret = os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET", "").strip()
    sheet_oauth_refresh_token = os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN", "").strip()
    sheet_oauth_token_uri = os.getenv(
        "MEDIA_RESEARCH_SHEET_OAUTH_TOKEN_URI",
        "https://oauth2.googleapis.com/token",
    ).strip() or "https://oauth2.googleapis.com/token"
    work_progress_enabled = _parse_bool(
        os.getenv("MEDIA_BOT_WORK_PROGRESS_ENABLED", "0"),
        default=False,
    )

    storage_root = project_root / "storage" / "media_research"
    logs_root = project_root / "logs" / "media_bot"
    state_root = project_root / "state" / "media_bot"

    return MediaSettings(
        project_root=project_root,
        storage_root=storage_root,
        logs_root=logs_root,
        state_root=state_root,
        telegram_bot_token=telegram_bot_token,
        telegram_allowed_user_id=telegram_allowed_user_id,
        daily_run_cap=daily_run_cap,
        timezone_name=timezone_name,
        serpapi_api_key=serpapi_api_key,
        max_image_results=max_image_results,
        max_video_results=max_video_results,
        max_api_calls_per_run=max_api_calls_per_run,
        platform_allowlist=platform_allowlist,
        market_scope=market_scope,
        cloudinary_cloud_name=cloudinary_cloud_name,
        cloudinary_upload_preset=cloudinary_upload_preset,
        sheet_enabled=sheet_enabled,
        sheet_mode=sheet_mode,
        sheet_spreadsheet_id=sheet_spreadsheet_id,
        sheet_gid=sheet_gid,
        sheet_oauth_client_id=sheet_oauth_client_id,
        sheet_oauth_client_secret=sheet_oauth_client_secret,
        sheet_oauth_refresh_token=sheet_oauth_refresh_token,
        sheet_oauth_token_uri=sheet_oauth_token_uri,
        media_research_enabled=media_research_enabled,
        work_progress_enabled=work_progress_enabled,
    )
