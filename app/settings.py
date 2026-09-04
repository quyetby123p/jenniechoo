from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os

from app.exceptions import ConfigError


@dataclass(frozen=True)
class Settings:
    project_root: Path
    storage_root: Path
    logs_root: Path
    state_root: Path
    config_root: Path
    telegram_bot_token: str
    telegram_allowed_user_id: int
    meta_access_token: str
    meta_page_access_token: str
    meta_ad_account_id: str
    meta_page_id: str
    meta_api_version: str
    app_timezone: str
    app_currency: str
    retry_max: int
    retry_backoff_seconds: list[int]
    token_healthcheck_enabled: bool
    token_healthcheck_hour: int
    token_healthcheck_minute: int
    token_healthcheck_startup_alert_only_on_failure: bool
    daily_report_enabled: bool
    daily_report_hour: int
    daily_report_minute: int
    daily_report_history_days: int
    daily_report_startup_alert_only_on_failure: bool
    pancake_api_base_url: str
    pancake_api_key: str
    pancake_access_token: str
    pancake_shop_id: int
    pancake_page_size: int
    report_thb_to_vnd_rate: float
    report_thb_minor_unit_factor: int
    daily_report_notify_chat_id: int = 0
    daily_report_task_summary_enabled: bool = True
    daily_report_task_summary_max_items: int = 5
    daily_report_task_db_path: str = "storage/assistant_bot/tasks.db"
    media_analytics_enabled: bool = False
    media_analytics_auto_enabled: bool = False
    media_analytics_notify_chat_id: int = 0
    media_analytics_history_days: int = 7
    media_analytics_auto_hour: int = 16
    media_analytics_auto_minute: int = 0
    media_analytics_min_spend_vnd: int = 50000
    media_analytics_sheet_enabled: bool = False
    media_analytics_sheet_spreadsheet_id: str = ""
    media_analytics_sheet_gid: int = 0
    media_analytics_sheet_oauth_client_id: str = ""
    media_analytics_sheet_oauth_client_secret: str = ""
    media_analytics_sheet_oauth_refresh_token: str = ""
    media_analytics_sheet_oauth_token_uri: str = "https://oauth2.googleapis.com/token"
    reconcile_cod_enabled: bool = False
    reconcile_cod_auto_enabled: bool = False
    reconcile_cod_hour: int = 15
    reconcile_cod_minute: int = 0
    reconcile_cod_auto_weekdays: tuple[int, ...] = (0, 4)
    reconcile_cod_weekly_summary_enabled: bool = False
    reconcile_cod_weekly_summary_weekday: int = 5
    reconcile_cod_notify_chat_id: int = 0
    reconcile_cod_batch_limit: int = 100
    reconcile_cod_update_enabled: bool = False
    reconcile_cod_status_map_path: str = "config/reconcile_cod_status_map.json"
    reconcile_cod_pancake_lookback_days: int = 3650
    reconcile_cod_extra_pancake_profiles: tuple[str, ...] = ()
    reconcile_cod_sheet_enabled: bool = False
    reconcile_cod_sheet_spreadsheet_id: str = ""
    reconcile_cod_sheet_gid: int = 1034910254
    reconcile_cod_sheet_credentials_path: str = ""
    reconcile_cod_sheet_mode: str = "apps_script"
    reconcile_cod_sheet_webhook_url: str = ""
    reconcile_cod_sheet_webhook_secret: str = ""
    reconcile_cod_sheet_webhook_timeout_seconds: int = 30
    reconcile_cod_sheet_oauth_client_id: str = ""
    reconcile_cod_sheet_oauth_client_secret: str = ""
    reconcile_cod_sheet_oauth_refresh_token: str = ""
    reconcile_cod_sheet_oauth_token_uri: str = "https://oauth2.googleapis.com/token"
    pancake_td_sync_enabled: bool = False
    pancake_td_sync_poll_seconds: int = 30
    pancake_td_sync_batch_limit: int = 50
    pancake_td_sync_notify_chat_id: int = 0
    pancake_td_sync_product_refresh_minutes: int = 30
    pancake_td_sync_state_path: str = "storage/pancake_td_sync/state.json"
    web_report_refresh_seconds: int = 600
    web_report_status_map_path: str = "config/web_report_status_map.json"
    web_report_source_costs_path: str = "config/web_report_source_costs.json"
    web_report_host: str = "0.0.0.0"
    web_report_port: int = 8000
    profile: str = "default"
    audiences_config_path_raw: str = "config/audiences.json"
    objective_config_path_raw: str = "config/objective.json"
    message_templates_path_raw: str = "config/message_templates.json"
    meta_creative_access_token: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""

    @property
    def audiences_config_path(self) -> Path:
        return self._resolve_project_path(self.audiences_config_path_raw, "config/audiences.json")

    @property
    def objective_config_path(self) -> Path:
        return self._resolve_project_path(self.objective_config_path_raw, "config/objective.json")

    @property
    def message_templates_path(self) -> Path:
        return self._resolve_project_path(self.message_templates_path_raw, "config/message_templates.json")

    def _resolve_project_path(self, raw: str, default: str) -> Path:
        text = str(raw or default).strip() or default
        path = Path(text)
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def pending_requests_dir(self) -> Path:
        return self.state_root / "pending_requests"

    @property
    def jobs_root(self) -> Path:
        return self.storage_root / "jobs"

    @property
    def jobs_pending_dir(self) -> Path:
        return self.jobs_root / "pending"

    @property
    def jobs_published_dir(self) -> Path:
        return self.jobs_root / "published"

    @property
    def jobs_cancelled_dir(self) -> Path:
        return self.jobs_root / "cancelled"

    @property
    def jobs_failed_dir(self) -> Path:
        return self.jobs_root / "failed"

    @property
    def app_logs_dir(self) -> Path:
        return self.logs_root / "app"

    @property
    def reports_root(self) -> Path:
        return self.storage_root / "reports"

    @property
    def reports_daily_dir(self) -> Path:
        return self.reports_root / "daily"

    @property
    def reports_error_dir(self) -> Path:
        return self.reports_root / "errors"

    @property
    def reconcile_cod_root(self) -> Path:
        return self.storage_root / "reconcile_cod"

    @property
    def reconcile_cod_runs_dir(self) -> Path:
        return self.reconcile_cod_root / "runs"

    @property
    def reconcile_cod_reports_dir(self) -> Path:
        return self.reconcile_cod_root / "reports"

    @property
    def reconcile_cod_applied_dir(self) -> Path:
        return self.reconcile_cod_root / "applied"

    @property
    def reconcile_cod_import_history_dir(self) -> Path:
        return self.reconcile_cod_root / "imports" / "history"

    @property
    def reconcile_cod_import_detail_dir(self) -> Path:
        return self.reconcile_cod_root / "imports" / "detail"

    @property
    def reconcile_cod_source_config_path(self) -> Path:
        return self.config_root / "reconcile_cod_source.json"

    @property
    def reconcile_cod_match_config_path(self) -> Path:
        return self.config_root / "reconcile_cod_match.json"

    @property
    def reconcile_cod_status_map_config_path(self) -> Path:
        raw = str(self.reconcile_cod_status_map_path).strip()
        path = Path(raw) if raw else Path("config/reconcile_cod_status_map.json")
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def reconcile_cod_sheet_credentials_file(self) -> Path:
        raw = str(self.reconcile_cod_sheet_credentials_path).strip()
        path = Path(raw) if raw else Path("config/reconcile_cod_sheet_service_account.json")
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def pancake_td_sync_root(self) -> Path:
        return self.storage_root / "pancake_td_sync"

    @property
    def pancake_td_sync_runs_dir(self) -> Path:
        return self.pancake_td_sync_root / "runs"

    @property
    def pancake_td_sync_state_file(self) -> Path:
        raw = str(self.pancake_td_sync_state_path).strip()
        path = Path(raw) if raw else Path("storage/pancake_td_sync/state.json")
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def pancake_td_sync_config_path(self) -> Path:
        return self.config_root / "pancake_td_sync.json"

    @property
    def pancake_td_color_alias_config_path(self) -> Path:
        return self.config_root / "pancake_td_color_alias.json"

    @property
    def thai_duong_order_payload_template_path(self) -> Path:
        return self.config_root / "thai_duong_order_payload_template.json"

    @property
    def web_report_status_map_config_path(self) -> Path:
        raw = str(self.web_report_status_map_path).strip()
        path = Path(raw) if raw else Path("config/web_report_status_map.json")
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def web_report_source_costs_config_path(self) -> Path:
        raw = str(self.web_report_source_costs_path).strip()
        path = Path(raw) if raw else Path("config/web_report_source_costs.json")
        if path.is_absolute():
            return path
        return self.project_root / path

    @property
    def daily_report_task_db_file(self) -> Path:
        raw = str(self.daily_report_task_db_path).strip()
        path = Path(raw) if raw else Path("storage/assistant_bot/tasks.db")
        if path.is_absolute():
            return path
        return self.project_root / path


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {key}")
    return value


def _normalize_profile(profile: str | None) -> str:
    normalized = str(profile or "default").strip().lower()
    return normalized or "default"


def _profile_env(
    key: str,
    profile: str,
    *,
    default: str = "",
    required: bool = False,
    allow_base_fallback: bool = True,
) -> str:
    normalized_profile = _normalize_profile(profile)
    keys = [key] if normalized_profile == "default" else [f"{normalized_profile.upper()}_{key}"]
    if allow_base_fallback and key not in keys:
        keys.append(key)
    for env_key in keys:
        value = os.getenv(env_key, "").strip()
        if value:
            return value
    if required:
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {keys[0]}")
    return str(default or "").strip()


def _profile_env_first(
    keys: tuple[str, ...],
    profile: str,
    *,
    default: str = "",
    allow_base_fallback: bool = True,
) -> str:
    for key in keys:
        value = _profile_env(
            key,
            profile,
            default="",
            allow_base_fallback=allow_base_fallback,
        )
        if value:
            return value
    return str(default or "").strip()


def _resolve_project_path(project_root: Path, raw: str, default: str) -> Path:
    text = str(raw or default).strip() or default
    path = Path(text)
    if path.is_absolute():
        return path
    return project_root / path


def _parse_retry_backoff(raw: str) -> list[int]:
    values = []
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        values.append(int(stripped))
    return values or [2, 5, 10]


def _parse_bool(raw: str, default: bool) -> bool:
    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int_with_range(raw: str, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < min_value or value > max_value:
        return default
    return value


def _parse_optional_int(raw: str, *, default: int) -> int:
    text = str(raw).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _parse_optional_float(raw: str, *, default: float) -> float:
    text = str(raw).strip()
    if not text:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _parse_weekday_list(raw: str, *, default: tuple[int, ...]) -> tuple[int, ...]:
    text = str(raw).strip()
    if not text:
        return tuple(default)
    values: list[int] = []
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            weekday = int(token)
        except (TypeError, ValueError):
            continue
        if weekday < 0 or weekday > 6:
            continue
        if weekday not in values:
            values.append(weekday)
    if not values:
        return tuple(default)
    return tuple(sorted(values))


def _parse_string_list(raw: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return tuple(default)
    values: list[str] = []
    for part in text.split(","):
        token = part.strip().lower()
        if not token or token in values:
            continue
        values.append(token)
    return tuple(values)


def load_settings(
    project_root: Path | None = None,
    profile: str | None = None,
    *,
    require_app_credentials: bool = True,
) -> Settings:
    """Load settings for the bot or a focused integration worker.

    The Pancake bridge only needs Pancake and Google Sheets credentials.  Keep
    the existing strict defaults for the bot, while allowing focused workers
    to avoid requiring unrelated Telegram/Meta credentials.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]
    profile_name = _normalize_profile(profile)

    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)

    is_default_profile = profile_name == "default"
    is_ads2_profile = profile_name == "ads2"
    telegram_bot_token = _profile_env(
        "TELEGRAM_BOT_TOKEN",
        profile_name,
        required=require_app_credentials,
        allow_base_fallback=is_default_profile,
    )
    telegram_allowed_user_id = int(
        _profile_env(
            "TELEGRAM_ALLOWED_USER_ID",
            profile_name,
            default="0" if not require_app_credentials else "",
            required=require_app_credentials,
            allow_base_fallback=True,
        )
    )
    meta_access_token = _profile_env(
        "META_ACCESS_TOKEN",
        profile_name,
        required=require_app_credentials,
        allow_base_fallback=True,
    )
    if is_default_profile:
        meta_page_access_token = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
    else:
        meta_page_access_token = _profile_env(
            "META_PAGE_ACCESS_TOKEN",
            profile_name,
            required=require_app_credentials,
            allow_base_fallback=False,
        )
    meta_ad_account_id = _profile_env(
        "META_AD_ACCOUNT_ID",
        profile_name,
        required=require_app_credentials,
        allow_base_fallback=is_default_profile,
    )
    meta_page_id = _profile_env(
        "META_PAGE_ID",
        profile_name,
        required=require_app_credentials,
        allow_base_fallback=is_default_profile,
    )
    meta_api_version = _profile_env(
        "META_API_VERSION",
        profile_name,
        default=os.getenv("META_API_VERSION", "v21.0"),
        allow_base_fallback=True,
    )
    meta_creative_access_token = _profile_env_first(
        (
            "META_CREATIVE_ACCESS_TOKEN",
            "META_INSTAGRAM_ACCESS_TOKEN",
            "META_IG_ACCESS_TOKEN",
        ),
        profile_name,
        default="",
        allow_base_fallback=is_default_profile,
    )
    meta_app_id = _profile_env(
        "META_APP_ID",
        profile_name,
        default="",
        allow_base_fallback=is_default_profile,
    )
    meta_app_secret = _profile_env(
        "META_APP_SECRET",
        profile_name,
        default="",
        allow_base_fallback=is_default_profile,
    )

    app_timezone = os.getenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
    app_currency = os.getenv("APP_CURRENCY", "VND").strip()
    retry_max = int(os.getenv("APP_RETRY_MAX", "3").strip())
    retry_backoff = _parse_retry_backoff(os.getenv("APP_RETRY_BACKOFF", "2,5,10"))
    token_healthcheck_enabled = _parse_bool(
        os.getenv("TOKEN_HEALTHCHECK_ENABLED", "1"),
        default=True,
    )
    token_healthcheck_hour = _parse_int_with_range(
        os.getenv("TOKEN_HEALTHCHECK_HOUR", "9"),
        default=9,
        min_value=0,
        max_value=23,
    )
    token_healthcheck_minute = _parse_int_with_range(
        os.getenv("TOKEN_HEALTHCHECK_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    token_healthcheck_startup_alert_only_on_failure = _parse_bool(
        os.getenv("TOKEN_HEALTHCHECK_STARTUP_ALERT_ONLY_ON_FAILURE", "1"),
        default=True,
    )
    daily_report_enabled = _parse_bool(
        _profile_env(
            "DAILY_REPORT_ENABLED",
            profile_name,
            default="1" if is_default_profile or is_ads2_profile else "0",
            allow_base_fallback=is_default_profile,
        ),
        default=is_default_profile or is_ads2_profile,
    )
    daily_report_hour = _parse_int_with_range(
        os.getenv("DAILY_REPORT_HOUR", "8"),
        default=8,
        min_value=0,
        max_value=23,
    )
    daily_report_minute = _parse_int_with_range(
        os.getenv("DAILY_REPORT_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    daily_report_history_days = _parse_int_with_range(
        os.getenv("DAILY_REPORT_HISTORY_DAYS", "90"),
        default=90,
        min_value=1,
        max_value=3650,
    )
    daily_report_startup_alert_only_on_failure = _parse_bool(
        os.getenv("DAILY_REPORT_STARTUP_ALERT_ONLY_ON_FAILURE", "1"),
        default=True,
    )
    daily_report_notify_chat_id = _parse_optional_int(
        _profile_env(
            "DAILY_REPORT_NOTIFY_CHAT_ID",
            profile_name,
            default="",
            allow_base_fallback=True,
        ),
        default=0,
    )
    daily_report_task_summary_enabled = _parse_bool(
        os.getenv("DAILY_REPORT_TASK_SUMMARY_ENABLED", "1"),
        default=True,
    )
    daily_report_task_summary_max_items = _parse_int_with_range(
        os.getenv("DAILY_REPORT_TASK_SUMMARY_MAX_ITEMS", "5"),
        default=5,
        min_value=1,
        max_value=20,
    )
    daily_report_task_db_path = os.getenv(
        "DAILY_REPORT_TASK_DB_PATH",
        os.getenv("BOT3_TASK_DB_PATH", "storage/assistant_bot/tasks.db"),
    ).strip()
    media_analytics_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_ENABLED",
            profile_name,
            default="1" if is_ads2_profile else "0",
            allow_base_fallback=False,
        ),
        default=is_ads2_profile,
    )
    media_analytics_auto_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_AUTO_ENABLED",
            profile_name,
            default="1" if is_ads2_profile else "0",
            allow_base_fallback=False,
        ),
        default=is_ads2_profile,
    )
    media_analytics_notify_chat_id = _parse_optional_int(
        _profile_env(
            "MEDIA_ANALYTICS_NOTIFY_CHAT_ID",
            profile_name,
            default="",
            allow_base_fallback=is_default_profile,
        ),
        default=0,
    )
    if media_analytics_notify_chat_id <= 0:
        media_analytics_notify_chat_id = telegram_allowed_user_id
    media_analytics_history_days = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_HISTORY_DAYS", profile_name, default="7", allow_base_fallback=True),
        default=7,
        min_value=1,
        max_value=90,
    )
    media_analytics_auto_hour = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_AUTO_HOUR", profile_name, default="16", allow_base_fallback=True),
        default=16,
        min_value=0,
        max_value=23,
    )
    media_analytics_auto_minute = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_AUTO_MINUTE", profile_name, default="0", allow_base_fallback=True),
        default=0,
        min_value=0,
        max_value=59,
    )
    media_analytics_min_spend_vnd = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_MIN_SPEND_VND", profile_name, default="50000", allow_base_fallback=True),
        default=50000,
        min_value=0,
        max_value=1_000_000_000,
    )
    media_analytics_sheet_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_SHEET_ENABLED",
            profile_name,
            default="1" if is_ads2_profile else "0",
            allow_base_fallback=False,
        ),
        default=is_ads2_profile,
    )
    media_analytics_sheet_spreadsheet_id = _profile_env(
        "MEDIA_ANALYTICS_SHEET_SPREADSHEET_ID",
        profile_name,
        default="1-ywfhrjlnEAob2Cn3znpKhTA4buKyQZrI5XXjiRCAAo" if is_ads2_profile else "",
        allow_base_fallback=False,
    )
    media_analytics_sheet_gid = _parse_optional_int(
        _profile_env(
            "MEDIA_ANALYTICS_SHEET_GID",
            profile_name,
            default="424378234" if is_ads2_profile else "",
            allow_base_fallback=False,
        ),
        default=0,
    )
    media_analytics_sheet_oauth_client_id = _profile_env_first(
        ("MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_ID", "BOT3_GOOGLE_OAUTH_CLIENT_ID", "MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID"),
        profile_name,
        default="",
        allow_base_fallback=True,
    )
    media_analytics_sheet_oauth_client_secret = _profile_env_first(
        (
            "MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_SECRET",
            "BOT3_GOOGLE_OAUTH_CLIENT_SECRET",
            "MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET",
        ),
        profile_name,
        default="",
        allow_base_fallback=True,
    )
    media_analytics_sheet_oauth_refresh_token = _profile_env_first(
        (
            "MEDIA_ANALYTICS_SHEET_OAUTH_REFRESH_TOKEN",
            "BOT3_GOOGLE_OAUTH_REFRESH_TOKEN",
            "MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN",
        ),
        profile_name,
        default="",
        allow_base_fallback=True,
    )
    media_analytics_sheet_oauth_token_uri = _profile_env_first(
        (
            "MEDIA_ANALYTICS_SHEET_OAUTH_TOKEN_URI",
            "BOT3_GOOGLE_OAUTH_TOKEN_URI",
            "MEDIA_RESEARCH_SHEET_OAUTH_TOKEN_URI",
        ),
        profile_name,
        default="https://oauth2.googleapis.com/token",
        allow_base_fallback=True,
    )
    pancake_api_base_url = _profile_env(
        "PANCAKE_API_BASE_URL",
        profile_name,
        default="https://pos.pancake.vn/api/v1",
        allow_base_fallback=True,
    ).rstrip("/")
    pancake_api_key = _profile_env(
        "PANCAKE_API_KEY",
        profile_name,
        default="",
        allow_base_fallback=True,
    )
    pancake_access_token = _profile_env(
        "PANCAKE_ACCESS_TOKEN",
        profile_name,
        default="",
        allow_base_fallback=True,
    )
    pancake_shop_id = _parse_optional_int(
        _profile_env(
            "PANCAKE_SHOP_ID",
            profile_name,
            default="",
            allow_base_fallback=is_default_profile,
        ),
        default=0,
    )
    pancake_page_size = _parse_int_with_range(
        _profile_env(
            "PANCAKE_PAGE_SIZE",
            profile_name,
            default="200",
            allow_base_fallback=True,
        ),
        default=200,
        min_value=1,
        max_value=500,
    )
    report_thb_to_vnd_rate = _parse_optional_float(
        os.getenv("REPORT_THB_TO_VND_RATE", "770"),
        default=770.0,
    )
    report_thb_minor_unit_factor = _parse_int_with_range(
        os.getenv("REPORT_THB_MINOR_UNIT_FACTOR", "100"),
        default=100,
        min_value=1,
        max_value=10000,
    )
    reconcile_cod_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_ENABLED", "0"),
        default=False,
    )
    reconcile_cod_auto_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_AUTO_ENABLED", "0"),
        default=False,
    )
    reconcile_cod_hour = _parse_int_with_range(
        os.getenv("RECONCILE_COD_HOUR", "15"),
        default=15,
        min_value=0,
        max_value=23,
    )
    reconcile_cod_minute = _parse_int_with_range(
        os.getenv("RECONCILE_COD_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    reconcile_cod_auto_weekdays = _parse_weekday_list(
        os.getenv("RECONCILE_COD_AUTO_WEEKDAYS", "0,4"),
        default=(0, 4),
    )
    reconcile_cod_weekly_summary_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_WEEKLY_SUMMARY_ENABLED", "1"),
        default=True,
    )
    reconcile_cod_weekly_summary_weekday = _parse_int_with_range(
        os.getenv("RECONCILE_COD_WEEKLY_SUMMARY_WEEKDAY", "5"),
        default=5,
        min_value=0,
        max_value=6,
    )
    reconcile_cod_notify_chat_id = _parse_optional_int(
        os.getenv("RECONCILE_COD_NOTIFY_CHAT_ID", ""),
        default=0,
    )
    reconcile_cod_batch_limit = _parse_int_with_range(
        os.getenv("RECONCILE_COD_BATCH_LIMIT", "100"),
        default=100,
        min_value=1,
        max_value=10000,
    )
    reconcile_cod_update_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_UPDATE_ENABLED", "0"),
        default=False,
    )
    reconcile_cod_status_map_path = os.getenv(
        "RECONCILE_COD_STATUS_MAP_PATH",
        "config/reconcile_cod_status_map.json",
    ).strip()
    reconcile_cod_pancake_lookback_days = _parse_int_with_range(
        os.getenv("RECONCILE_COD_PANCAKE_LOOKBACK_DAYS", "3650"),
        default=3650,
        min_value=1,
        max_value=36500,
    )
    reconcile_cod_extra_pancake_profiles = _parse_string_list(
        os.getenv("RECONCILE_COD_EXTRA_PANCAKE_PROFILES", "ads2" if is_default_profile else ""),
    )
    reconcile_cod_sheet_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_SHEET_ENABLED", "0"),
        default=False,
    )
    reconcile_cod_sheet_spreadsheet_id = os.getenv(
        "RECONCILE_COD_SHEET_SPREADSHEET_ID",
        "",
    ).strip()
    reconcile_cod_sheet_gid = _parse_int_with_range(
        os.getenv("RECONCILE_COD_SHEET_GID", "1034910254"),
        default=1034910254,
        min_value=0,
        max_value=2_147_483_647,
    )
    reconcile_cod_sheet_credentials_path = os.getenv(
        "RECONCILE_COD_SHEET_CREDENTIALS_PATH",
        "",
    ).strip()
    reconcile_cod_sheet_mode = os.getenv(
        "RECONCILE_COD_SHEET_MODE",
        "apps_script",
    ).strip()
    reconcile_cod_sheet_webhook_url = os.getenv(
        "RECONCILE_COD_SHEET_WEBHOOK_URL",
        "",
    ).strip()
    reconcile_cod_sheet_webhook_secret = os.getenv(
        "RECONCILE_COD_SHEET_WEBHOOK_SECRET",
        "",
    ).strip()
    reconcile_cod_sheet_webhook_timeout_seconds = _parse_int_with_range(
        os.getenv("RECONCILE_COD_SHEET_WEBHOOK_TIMEOUT_SECONDS", "30"),
        default=30,
        min_value=5,
        max_value=120,
    )
    reconcile_cod_sheet_oauth_client_id = os.getenv(
        "RECONCILE_COD_SHEET_OAUTH_CLIENT_ID",
        "",
    ).strip()
    reconcile_cod_sheet_oauth_client_secret = os.getenv(
        "RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET",
        "",
    ).strip()
    reconcile_cod_sheet_oauth_refresh_token = os.getenv(
        "RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN",
        "",
    ).strip()
    reconcile_cod_sheet_oauth_token_uri = os.getenv(
        "RECONCILE_COD_SHEET_OAUTH_TOKEN_URI",
        "https://oauth2.googleapis.com/token",
    ).strip() or "https://oauth2.googleapis.com/token"
    pancake_td_sync_enabled = _parse_bool(
        os.getenv("PANCAKE_TD_SYNC_ENABLED", "0"),
        default=False,
    )
    pancake_td_sync_poll_seconds = _parse_int_with_range(
        os.getenv("PANCAKE_TD_SYNC_POLL_SECONDS", "30"),
        default=30,
        min_value=5,
        max_value=3600,
    )
    pancake_td_sync_batch_limit = _parse_int_with_range(
        os.getenv("PANCAKE_TD_SYNC_BATCH_LIMIT", "50"),
        default=50,
        min_value=1,
        max_value=500,
    )
    pancake_td_sync_notify_chat_id = _parse_optional_int(
        os.getenv("PANCAKE_TD_SYNC_NOTIFY_CHAT_ID", ""),
        default=0,
    )
    pancake_td_sync_product_refresh_minutes = _parse_int_with_range(
        os.getenv("PANCAKE_TD_SYNC_PRODUCT_REFRESH_MINUTES", "30"),
        default=30,
        min_value=1,
        max_value=1440,
    )
    pancake_td_sync_state_path = os.getenv(
        "PANCAKE_TD_SYNC_STATE_PATH",
        "storage/pancake_td_sync/state.json",
    ).strip()
    web_report_refresh_seconds = _parse_int_with_range(
        os.getenv("WEB_REPORT_REFRESH_SECONDS", "600"),
        default=600,
        min_value=30,
        max_value=3600,
    )
    web_report_status_map_path = os.getenv(
        "WEB_REPORT_STATUS_MAP_PATH",
        "config/web_report_status_map.json",
    ).strip()
    web_report_source_costs_path = os.getenv(
        "WEB_REPORT_SOURCE_COSTS_PATH",
        "config/web_report_source_costs.json",
    ).strip()
    web_report_host = os.getenv("WEB_REPORT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    web_report_port = _parse_int_with_range(
        os.getenv("WEB_REPORT_PORT", "8000"),
        default=8000,
        min_value=1,
        max_value=65535,
    )

    profile_suffix = "" if is_default_profile else f"/{profile_name}"
    audiences_config_path_raw = _profile_env(
        "AUDIENCES_CONFIG_PATH",
        profile_name,
        default=f"config{profile_suffix}/audiences.json",
        allow_base_fallback=False,
    )
    objective_config_path_raw = _profile_env(
        "OBJECTIVE_CONFIG_PATH",
        profile_name,
        default=f"config{profile_suffix}/objective.json",
        allow_base_fallback=False,
    )
    message_templates_path_raw = _profile_env(
        "MESSAGE_TEMPLATES_PATH",
        profile_name,
        default=f"config{profile_suffix}/message_templates.json",
        allow_base_fallback=False,
    )
    storage_root_raw = _profile_env(
        "STORAGE_ROOT",
        profile_name,
        default=f"storage{profile_suffix}",
        allow_base_fallback=False,
    )
    logs_root_raw = _profile_env(
        "LOGS_ROOT",
        profile_name,
        default=f"logs{profile_suffix}",
        allow_base_fallback=False,
    )
    state_root_raw = _profile_env(
        "STATE_ROOT",
        profile_name,
        default=f"state{profile_suffix}",
        allow_base_fallback=False,
    )

    storage_root = _resolve_project_path(project_root, storage_root_raw, "storage")
    logs_root = _resolve_project_path(project_root, logs_root_raw, "logs")
    state_root = _resolve_project_path(project_root, state_root_raw, "state")
    config_root = project_root / "config"

    for required in (
        _resolve_project_path(project_root, audiences_config_path_raw, "config/audiences.json"),
        _resolve_project_path(project_root, objective_config_path_raw, "config/objective.json"),
        _resolve_project_path(project_root, message_templates_path_raw, "config/message_templates.json"),
    ):
        if not required.exists():
            raise ConfigError(f"Khong tim thay file cau hinh: {required}")

    return Settings(
        project_root=project_root,
        storage_root=storage_root,
        logs_root=logs_root,
        state_root=state_root,
        config_root=config_root,
        profile=profile_name,
        audiences_config_path_raw=audiences_config_path_raw,
        objective_config_path_raw=objective_config_path_raw,
        message_templates_path_raw=message_templates_path_raw,
        telegram_bot_token=telegram_bot_token,
        telegram_allowed_user_id=telegram_allowed_user_id,
        meta_access_token=meta_access_token,
        meta_page_access_token=meta_page_access_token,
        meta_ad_account_id=meta_ad_account_id,
        meta_page_id=meta_page_id,
        meta_api_version=meta_api_version,
        meta_creative_access_token=meta_creative_access_token,
        meta_app_id=meta_app_id,
        meta_app_secret=meta_app_secret,
        app_timezone=app_timezone,
        app_currency=app_currency,
        retry_max=retry_max,
        retry_backoff_seconds=retry_backoff,
        token_healthcheck_enabled=token_healthcheck_enabled,
        token_healthcheck_hour=token_healthcheck_hour,
        token_healthcheck_minute=token_healthcheck_minute,
        token_healthcheck_startup_alert_only_on_failure=token_healthcheck_startup_alert_only_on_failure,
        daily_report_enabled=daily_report_enabled,
        daily_report_hour=daily_report_hour,
        daily_report_minute=daily_report_minute,
        daily_report_history_days=daily_report_history_days,
        daily_report_startup_alert_only_on_failure=daily_report_startup_alert_only_on_failure,
        pancake_api_base_url=pancake_api_base_url,
        pancake_api_key=pancake_api_key,
        pancake_access_token=pancake_access_token,
        pancake_shop_id=pancake_shop_id,
        pancake_page_size=pancake_page_size,
        report_thb_to_vnd_rate=report_thb_to_vnd_rate,
        report_thb_minor_unit_factor=report_thb_minor_unit_factor,
        daily_report_notify_chat_id=daily_report_notify_chat_id,
        daily_report_task_summary_enabled=daily_report_task_summary_enabled,
        daily_report_task_summary_max_items=daily_report_task_summary_max_items,
        daily_report_task_db_path=daily_report_task_db_path,
        media_analytics_enabled=media_analytics_enabled,
        media_analytics_auto_enabled=media_analytics_auto_enabled,
        media_analytics_notify_chat_id=media_analytics_notify_chat_id,
        media_analytics_history_days=media_analytics_history_days,
        media_analytics_auto_hour=media_analytics_auto_hour,
        media_analytics_auto_minute=media_analytics_auto_minute,
        media_analytics_min_spend_vnd=media_analytics_min_spend_vnd,
        media_analytics_sheet_enabled=media_analytics_sheet_enabled,
        media_analytics_sheet_spreadsheet_id=media_analytics_sheet_spreadsheet_id,
        media_analytics_sheet_gid=media_analytics_sheet_gid,
        media_analytics_sheet_oauth_client_id=media_analytics_sheet_oauth_client_id,
        media_analytics_sheet_oauth_client_secret=media_analytics_sheet_oauth_client_secret,
        media_analytics_sheet_oauth_refresh_token=media_analytics_sheet_oauth_refresh_token,
        media_analytics_sheet_oauth_token_uri=media_analytics_sheet_oauth_token_uri,
        reconcile_cod_enabled=reconcile_cod_enabled,
        reconcile_cod_auto_enabled=reconcile_cod_auto_enabled,
        reconcile_cod_hour=reconcile_cod_hour,
        reconcile_cod_minute=reconcile_cod_minute,
        reconcile_cod_auto_weekdays=reconcile_cod_auto_weekdays,
        reconcile_cod_weekly_summary_enabled=reconcile_cod_weekly_summary_enabled,
        reconcile_cod_weekly_summary_weekday=reconcile_cod_weekly_summary_weekday,
        reconcile_cod_notify_chat_id=reconcile_cod_notify_chat_id,
        reconcile_cod_batch_limit=reconcile_cod_batch_limit,
        reconcile_cod_update_enabled=reconcile_cod_update_enabled,
        reconcile_cod_status_map_path=reconcile_cod_status_map_path,
        reconcile_cod_pancake_lookback_days=reconcile_cod_pancake_lookback_days,
        reconcile_cod_extra_pancake_profiles=reconcile_cod_extra_pancake_profiles,
        reconcile_cod_sheet_enabled=reconcile_cod_sheet_enabled,
        reconcile_cod_sheet_spreadsheet_id=reconcile_cod_sheet_spreadsheet_id,
        reconcile_cod_sheet_gid=reconcile_cod_sheet_gid,
        reconcile_cod_sheet_credentials_path=reconcile_cod_sheet_credentials_path,
        reconcile_cod_sheet_mode=reconcile_cod_sheet_mode,
        reconcile_cod_sheet_webhook_url=reconcile_cod_sheet_webhook_url,
        reconcile_cod_sheet_webhook_secret=reconcile_cod_sheet_webhook_secret,
        reconcile_cod_sheet_webhook_timeout_seconds=reconcile_cod_sheet_webhook_timeout_seconds,
        reconcile_cod_sheet_oauth_client_id=reconcile_cod_sheet_oauth_client_id,
        reconcile_cod_sheet_oauth_client_secret=reconcile_cod_sheet_oauth_client_secret,
        reconcile_cod_sheet_oauth_refresh_token=reconcile_cod_sheet_oauth_refresh_token,
        reconcile_cod_sheet_oauth_token_uri=reconcile_cod_sheet_oauth_token_uri,
        pancake_td_sync_enabled=pancake_td_sync_enabled,
        pancake_td_sync_poll_seconds=pancake_td_sync_poll_seconds,
        pancake_td_sync_batch_limit=pancake_td_sync_batch_limit,
        pancake_td_sync_notify_chat_id=pancake_td_sync_notify_chat_id,
        pancake_td_sync_product_refresh_minutes=pancake_td_sync_product_refresh_minutes,
        pancake_td_sync_state_path=pancake_td_sync_state_path,
        web_report_refresh_seconds=web_report_refresh_seconds,
        web_report_status_map_path=web_report_status_map_path,
        web_report_source_costs_path=web_report_source_costs_path,
        web_report_host=web_report_host,
        web_report_port=web_report_port,
    )
