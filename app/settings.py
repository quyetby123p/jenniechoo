from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
    report_pos_default_currency: str = "THB"
    meta_creative_access_token: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_pixel_id: str = ""
    meta_instagram_user_id: str = ""
    new_web_ads_enabled: bool = False
    new_messenger_media_ads_enabled: bool = False
    daily_report_notify_chat_id: int = 0
    daily_report_pos_lookback_days: int = 30
    daily_report_task_summary_enabled: bool = True
    daily_report_task_summary_max_items: int = 5
    daily_report_task_db_path: str = "storage/assistant_bot/tasks.db"
    daily_report_task_snapshot_enabled: bool = True
    daily_report_task_snapshot_url: str = ""
    daily_report_task_snapshot_secret: str = ""
    daily_report_task_snapshot_timeout_seconds: int = 8
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
    reconcile_cod_product_sheet_enabled: bool = True
    reconcile_cod_product_sheet_name: str = "Chi tiet SP COD"
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
    profile: str = "main"
    profile_name: str = "main"
    audiences_config_path_raw: str = "config/audiences.json"
    objective_config_path_raw: str = "config/objective.json"
    message_templates_path_raw: str = "config/message_templates.json"
    pancake_td_sync_config_path_raw: str = "config/pancake_td_sync.json"
    pancake_td_color_alias_config_path_raw: str = "config/pancake_td_color_alias.json"
    thai_duong_order_payload_template_path_raw: str = "config/thai_duong_order_payload_template.json"
    web_objective_config_path_raw: str = "config/web_objective.json"
    web_cta_config_path_raw: str = "config/web_cta.json"
    sku_catalog_path_raw: str = ""
    sku_catalog_spreadsheet_id: str = ""
    sku_catalog_sheet_name: str = "Sản phẩm và giá"
    sku_catalog_range: str = "J2:J1000"
    web_media_max_bytes: int = 500 * 1024 * 1024
    web_pending_ttl_minutes: int = 30

    @property
    def audiences_config_path(self) -> Path:
        return self._resolve_project_path(self.audiences_config_path_raw, "config/audiences.json")

    @property
    def objective_config_path(self) -> Path:
        return self._resolve_project_path(self.objective_config_path_raw, "config/objective.json")

    @property
    def message_templates_path(self) -> Path:
        return self._resolve_project_path(self.message_templates_path_raw, "config/message_templates.json")

    @property
    def web_objective_config_path(self) -> Path:
        return self._resolve_project_path(self.web_objective_config_path_raw, "config/web_objective.json")

    @property
    def web_cta_config_path(self) -> Path:
        return self._resolve_project_path(self.web_cta_config_path_raw, "config/web_cta.json")

    @property
    def sku_catalog_path(self) -> Path:
        return self._resolve_project_path(self.sku_catalog_path_raw, "config/sku_catalog.json")

    def _resolve_project_path(self, raw: str, default: str) -> Path:
        path = Path(str(raw or default).strip() or default)
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
    def jobs_publishing_dir(self) -> Path:
        return self.jobs_root / "publishing"

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
        return self._resolve_project_path(
            self.pancake_td_sync_config_path_raw,
            "config/pancake_td_sync.json",
        )

    @property
    def pancake_td_color_alias_config_path(self) -> Path:
        return self._resolve_project_path(
            self.pancake_td_color_alias_config_path_raw,
            "config/pancake_td_color_alias.json",
        )

    @property
    def thai_duong_order_payload_template_path(self) -> Path:
        return self._resolve_project_path(
            self.thai_duong_order_payload_template_path_raw,
            "config/thai_duong_order_payload_template.json",
        )

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
    normalized = str(profile or os.getenv("ADS_PROFILE", "main")).strip().lower()
    return normalized or "main"


def _profile_prefix(profile_name: str) -> str:
    if profile_name in {"", "main", "default"}:
        return ""
    return profile_name.upper().replace("-", "_")


def _profile_env(name: str, prefix: str, default: str = "", *, allow_fallback: bool = True) -> str:
    if prefix:
        value = os.getenv(f"{prefix}_{name}", "").strip()
        if value:
            return value
        if not allow_fallback:
            return str(default or "").strip()
    return os.getenv(name, default).strip()


def _require_profile_env(name: str, prefix: str, *, allow_fallback: bool = True) -> str:
    value = _profile_env(name, prefix, allow_fallback=allow_fallback)
    if not value:
        key = f"{prefix}_{name}" if prefix else name
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {key}")
    return value


def _profile_path_default(base: str, profile_name: str) -> str:
    if profile_name in {"", "main", "default"}:
        return base
    return f"{base.rstrip('/')}/{profile_name}"


def _profile_config_default(project_root: Path, profile_name: str, filename: str) -> str:
    name = str(filename or "").strip().lstrip("/\\")
    if profile_name in {"", "main", "default"}:
        return f"config/{name}"
    candidate = project_root / "config" / profile_name / name
    if candidate.exists():
        return f"config/{profile_name}/{name}"
    return f"config/{name}"


def _resolve_project_path(project_root: Path, raw: str, default: str) -> Path:
    path = Path(str(raw or default).strip() or default)
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


def _derive_task_snapshot_url(raw_mark_url: str) -> str:
    text = str(raw_mark_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/schedule/mark"):
        return ""
    base_path = path[: -len("/schedule/mark")]
    return urlunparse(parsed._replace(path=f"{base_path}/task-snapshot", query="", fragment=""))


def load_settings(
    project_root: Path | None = None,
    profile: str | None = None,
    *,
    require_app_credentials: bool = True,
) -> Settings:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)

    profile_name = _normalize_profile(profile)
    prefix = _profile_prefix(profile_name)

    if require_app_credentials:
        telegram_bot_token = _require_profile_env("TELEGRAM_BOT_TOKEN", prefix, allow_fallback=False)
        telegram_allowed_user_id = int(_require_profile_env("TELEGRAM_ALLOWED_USER_ID", prefix))
        meta_access_token = _require_profile_env("META_ACCESS_TOKEN", prefix)
        meta_ad_account_id = _require_profile_env("META_AD_ACCOUNT_ID", prefix, allow_fallback=False)
        meta_page_id = _require_profile_env("META_PAGE_ID", prefix, allow_fallback=False)
    else:
        telegram_bot_token = _profile_env("TELEGRAM_BOT_TOKEN", prefix, "", allow_fallback=False)
        telegram_allowed_user_id = _parse_optional_int(
            _profile_env("TELEGRAM_ALLOWED_USER_ID", prefix, ""),
            default=0,
        )
        meta_access_token = _profile_env("META_ACCESS_TOKEN", prefix, "")
        meta_ad_account_id = _profile_env("META_AD_ACCOUNT_ID", prefix, "", allow_fallback=False)
        meta_page_id = _profile_env("META_PAGE_ID", prefix, "", allow_fallback=False)
    meta_page_access_token = _profile_env("META_PAGE_ACCESS_TOKEN", prefix, allow_fallback=False)
    meta_creative_access_token = _profile_env(
        "META_CREATIVE_ACCESS_TOKEN",
        prefix,
        _profile_env("META_INSTAGRAM_ACCESS_TOKEN", prefix, _profile_env("META_IG_ACCESS_TOKEN", prefix, "")),
    )
    meta_app_id = _profile_env("META_APP_ID", prefix, "")
    meta_app_secret = _profile_env("META_APP_SECRET", prefix, "")
    meta_pixel_id = _profile_env("META_PIXEL_ID", prefix, "", allow_fallback=False)
    meta_instagram_user_id = _profile_env("META_INSTAGRAM_USER_ID", prefix, "", allow_fallback=False)
    meta_api_version = _profile_env("META_API_VERSION", prefix, "v21.0")

    app_timezone = os.getenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
    app_currency = os.getenv("APP_CURRENCY", "VND").strip()
    retry_max = int(os.getenv("APP_RETRY_MAX", "3").strip())
    retry_backoff = _parse_retry_backoff(os.getenv("APP_RETRY_BACKOFF", "2,5,10"))
    new_web_ads_enabled = _parse_bool(
        _profile_env("NEW_WEB_ADS_ENABLED", prefix, "0", allow_fallback=False),
        default=False,
    )
    new_messenger_media_ads_enabled = _parse_bool(
        _profile_env("NEW_MESSENGER_MEDIA_ADS_ENABLED", prefix, "0", allow_fallback=False),
        default=False,
    )
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
    profile_is_main = not bool(prefix)
    profile_is_ads2 = profile_name == "ads2"
    daily_report_enabled = _parse_bool(
        _profile_env(
            "DAILY_REPORT_ENABLED",
            prefix,
            "1" if profile_is_main or profile_is_ads2 else "0",
            allow_fallback=False,
        ),
        default=profile_is_main or profile_is_ads2,
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
            prefix,
            "",
            allow_fallback=profile_is_main or profile_is_ads2,
        ),
        default=0,
    )
    media_analytics_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_ENABLED",
            prefix,
            "1" if profile_is_ads2 else "0",
            allow_fallback=False,
        ),
        default=profile_is_ads2,
    )
    media_analytics_auto_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_AUTO_ENABLED",
            prefix,
            "1" if profile_is_ads2 else "0",
            allow_fallback=False,
        ),
        default=profile_is_ads2,
    )
    media_analytics_notify_chat_id = _parse_optional_int(
        _profile_env("MEDIA_ANALYTICS_NOTIFY_CHAT_ID", prefix, "", allow_fallback=profile_is_main),
        default=0,
    )
    if media_analytics_notify_chat_id <= 0:
        media_analytics_notify_chat_id = (
            telegram_allowed_user_id
            if profile_is_ads2
            else daily_report_notify_chat_id or telegram_allowed_user_id
        )
    media_analytics_history_days = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_HISTORY_DAYS", prefix, "7", allow_fallback=True),
        default=7,
        min_value=1,
        max_value=90,
    )
    media_analytics_auto_hour = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_AUTO_HOUR", prefix, "16", allow_fallback=True),
        default=16,
        min_value=0,
        max_value=23,
    )
    media_analytics_auto_minute = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_AUTO_MINUTE", prefix, "0", allow_fallback=True),
        default=0,
        min_value=0,
        max_value=59,
    )
    media_analytics_min_spend_vnd = _parse_int_with_range(
        _profile_env("MEDIA_ANALYTICS_MIN_SPEND_VND", prefix, "50000", allow_fallback=True),
        default=50000,
        min_value=0,
        max_value=1_000_000_000,
    )
    media_analytics_sheet_enabled = _parse_bool(
        _profile_env(
            "MEDIA_ANALYTICS_SHEET_ENABLED",
            prefix,
            "1" if profile_is_ads2 else "0",
            allow_fallback=False,
        ),
        default=profile_is_ads2,
    )
    media_analytics_sheet_spreadsheet_id = _profile_env(
        "MEDIA_ANALYTICS_SHEET_SPREADSHEET_ID",
        prefix,
        "1-ywfhrjlnEAob2Cn3znpKhTA4buKyQZrI5XXjiRCAAo" if profile_is_ads2 else "",
        allow_fallback=False,
    )
    media_analytics_sheet_gid = _parse_optional_int(
        _profile_env(
            "MEDIA_ANALYTICS_SHEET_GID",
            prefix,
            "424378234" if profile_is_ads2 else "",
            allow_fallback=False,
        ),
        default=0,
    )
    media_analytics_sheet_oauth_client_id = _profile_env(
        "MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_ID",
        prefix,
        os.getenv("BOT3_GOOGLE_OAUTH_CLIENT_ID", os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID", "")),
        allow_fallback=True,
    )
    media_analytics_sheet_oauth_client_secret = _profile_env(
        "MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_SECRET",
        prefix,
        os.getenv("BOT3_GOOGLE_OAUTH_CLIENT_SECRET", os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET", "")),
        allow_fallback=True,
    )
    media_analytics_sheet_oauth_refresh_token = _profile_env(
        "MEDIA_ANALYTICS_SHEET_OAUTH_REFRESH_TOKEN",
        prefix,
        os.getenv("BOT3_GOOGLE_OAUTH_REFRESH_TOKEN", os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN", "")),
        allow_fallback=True,
    )
    media_analytics_sheet_oauth_token_uri = _profile_env(
        "MEDIA_ANALYTICS_SHEET_OAUTH_TOKEN_URI",
        prefix,
        os.getenv(
            "BOT3_GOOGLE_OAUTH_TOKEN_URI",
            os.getenv("MEDIA_RESEARCH_SHEET_OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        ),
        allow_fallback=True,
    ) or "https://oauth2.googleapis.com/token"
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
    daily_report_task_snapshot_enabled = _parse_bool(
        os.getenv("DAILY_REPORT_TASK_SNAPSHOT_ENABLED", os.getenv("BOT3_TASK_SNAPSHOT_ENABLED", "1")),
        default=True,
    )
    daily_report_task_snapshot_url = os.getenv(
        "DAILY_REPORT_TASK_SNAPSHOT_URL",
        os.getenv("BOT3_TASK_SNAPSHOT_URL", ""),
    ).strip()
    if not daily_report_task_snapshot_url:
        daily_report_task_snapshot_url = _derive_task_snapshot_url(
            os.getenv("CLOUD_SCHEDULE_GUARD_MARK_URL", "").strip(),
        )
    daily_report_task_snapshot_secret = os.getenv(
        "DAILY_REPORT_TASK_SNAPSHOT_SECRET",
        os.getenv(
            "BOT3_TASK_SNAPSHOT_SECRET",
            os.getenv("CLOUD_SCHEDULE_GUARD_SECRET", os.getenv("TELEGRAM_WEBHOOK_SECRET", "")),
        ),
    ).strip()
    daily_report_task_snapshot_timeout_seconds = _parse_int_with_range(
        os.getenv("DAILY_REPORT_TASK_SNAPSHOT_TIMEOUT_SECONDS", os.getenv("BOT3_TASK_SNAPSHOT_TIMEOUT_SECONDS", "8")),
        default=8,
        min_value=1,
        max_value=60,
    )
    pancake_api_base_url = _profile_env(
        "PANCAKE_API_BASE_URL",
        prefix,
        "https://pos.pancake.vn/api/v1",
        allow_fallback=True,
    ).rstrip("/")
    pancake_api_key = _profile_env("PANCAKE_API_KEY", prefix, "", allow_fallback=True)
    pancake_access_token = _profile_env("PANCAKE_ACCESS_TOKEN", prefix, "", allow_fallback=True)
    pancake_shop_id = _parse_optional_int(
        _profile_env("PANCAKE_SHOP_ID", prefix, "", allow_fallback=profile_is_main),
        default=0,
    )
    pancake_page_size = _parse_int_with_range(
        _profile_env("PANCAKE_PAGE_SIZE", prefix, "200", allow_fallback=True),
        default=200,
        min_value=1,
        max_value=500,
    )
    report_thb_to_vnd_rate = _parse_optional_float(
        os.getenv("REPORT_THB_TO_VND_RATE", "810"),
        default=810.0,
    )
    report_thb_minor_unit_factor = _parse_int_with_range(
        os.getenv("REPORT_THB_MINOR_UNIT_FACTOR", "100"),
        default=100,
        min_value=1,
        max_value=10000,
    )
    report_pos_default_currency = os.getenv("REPORT_POS_DEFAULT_CURRENCY", "THB").strip().upper() or "THB"
    daily_report_pos_lookback_days = _parse_int_with_range(
        os.getenv("DAILY_REPORT_POS_LOOKBACK_DAYS", "30"),
        default=30,
        min_value=0,
        max_value=365,
    )
    reconcile_cod_enabled = _parse_bool(
        _profile_env("RECONCILE_COD_ENABLED", prefix, "0", allow_fallback=False),
        default=False,
    )
    reconcile_cod_auto_enabled = _parse_bool(
        _profile_env("RECONCILE_COD_AUTO_ENABLED", prefix, "0", allow_fallback=False),
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
        _profile_env("RECONCILE_COD_NOTIFY_CHAT_ID", prefix, "", allow_fallback=profile_is_main),
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
        os.getenv("RECONCILE_COD_EXTRA_PANCAKE_PROFILES", "ads2" if profile_is_main else ""),
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
    reconcile_cod_product_sheet_enabled = _parse_bool(
        os.getenv("RECONCILE_COD_PRODUCT_SHEET_ENABLED", "1"),
        default=True,
    )
    reconcile_cod_product_sheet_name = os.getenv(
        "RECONCILE_COD_PRODUCT_SHEET_NAME",
        "Chi tiet SP COD",
    ).strip() or "Chi tiet SP COD"
    pancake_td_sync_enabled = _parse_bool(
        _profile_env("PANCAKE_TD_SYNC_ENABLED", prefix, "0", allow_fallback=False),
        default=False,
    )
    pancake_td_sync_poll_seconds = _parse_int_with_range(
        _profile_env("PANCAKE_TD_SYNC_POLL_SECONDS", prefix, "30", allow_fallback=True),
        default=30,
        min_value=5,
        max_value=3600,
    )
    pancake_td_sync_batch_limit = _parse_int_with_range(
        _profile_env("PANCAKE_TD_SYNC_BATCH_LIMIT", prefix, "50", allow_fallback=True),
        default=50,
        min_value=1,
        max_value=500,
    )
    pancake_td_sync_notify_chat_id = _parse_optional_int(
        _profile_env("PANCAKE_TD_SYNC_NOTIFY_CHAT_ID", prefix, "", allow_fallback=profile_is_main),
        default=0,
    )
    pancake_td_sync_product_refresh_minutes = _parse_int_with_range(
        _profile_env("PANCAKE_TD_SYNC_PRODUCT_REFRESH_MINUTES", prefix, "30", allow_fallback=True),
        default=30,
        min_value=1,
        max_value=1440,
    )
    pancake_td_sync_state_path = os.getenv(
        f"{prefix}_PANCAKE_TD_SYNC_STATE_PATH",
        "",
    ).strip()
    if not pancake_td_sync_state_path:
        pancake_td_sync_state_path = (
            f"storage/{profile_name}/pancake_td_sync/state.json"
            if prefix
            else os.getenv("PANCAKE_TD_SYNC_STATE_PATH", "storage/pancake_td_sync/state.json").strip()
        )
    if prefix and pancake_td_sync_enabled and pancake_shop_id <= 0:
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {prefix}_PANCAKE_SHOP_ID")
    pancake_td_sync_config_path_raw = _profile_env(
        "PANCAKE_TD_SYNC_CONFIG_PATH",
        prefix,
        _profile_config_default(project_root, profile_name, "pancake_td_sync.json"),
        allow_fallback=False,
    )
    pancake_td_color_alias_config_path_raw = _profile_env(
        "PANCAKE_TD_COLOR_ALIAS_CONFIG_PATH",
        prefix,
        _profile_config_default(project_root, profile_name, "pancake_td_color_alias.json"),
        allow_fallback=False,
    )
    thai_duong_order_payload_template_path_raw = _profile_env(
        "THAI_DUONG_ORDER_PAYLOAD_TEMPLATE_PATH",
        prefix,
        _profile_config_default(project_root, profile_name, "thai_duong_order_payload_template.json"),
        allow_fallback=False,
    )
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

    default_config_dir = f"config/{profile_name}" if prefix else "config"
    audiences_config_path_raw = _profile_env(
        "AUDIENCES_CONFIG_PATH",
        prefix,
        f"{default_config_dir}/audiences.json",
        allow_fallback=False,
    )
    objective_config_path_raw = _profile_env(
        "OBJECTIVE_CONFIG_PATH",
        prefix,
        f"{default_config_dir}/objective.json",
        allow_fallback=False,
    )
    message_templates_path_raw = _profile_env(
        "MESSAGE_TEMPLATES_PATH",
        prefix,
        f"{default_config_dir}/message_templates.json",
        allow_fallback=False,
    )
    web_objective_config_path_raw = _profile_env(
        "WEB_OBJECTIVE_CONFIG_PATH",
        prefix,
        f"{default_config_dir}/web_objective.json",
        allow_fallback=False,
    )
    web_cta_config_path_raw = _profile_env(
        "WEB_CTA_CONFIG_PATH",
        prefix,
        f"{default_config_dir}/web_cta.json",
        allow_fallback=False,
    )
    sku_catalog_path_raw = _profile_env(
        "SKU_CATALOG_PATH",
        prefix,
        f"{default_config_dir}/sku_catalog.json" if profile_is_ads2 else "",
        allow_fallback=False,
    )
    sku_catalog_spreadsheet_id = _profile_env(
        "SKU_CATALOG_SPREADSHEET_ID",
        prefix,
        "12s9p2o11HOPp83AX3mLEXmphHxEMWSxNJGkgkXGbrVQ" if profile_is_ads2 else "",
        allow_fallback=False,
    )
    sku_catalog_sheet_name = _profile_env(
        "SKU_CATALOG_SHEET_NAME",
        prefix,
        "Sản phẩm và giá",
        allow_fallback=False,
    )
    sku_catalog_range = _profile_env(
        "SKU_CATALOG_RANGE",
        prefix,
        "J2:J1000",
        allow_fallback=False,
    )
    web_media_max_bytes = _parse_int_with_range(
        _profile_env("WEB_MEDIA_MAX_BYTES", prefix, str(500 * 1024 * 1024), allow_fallback=False),
        default=500 * 1024 * 1024,
        min_value=1 * 1024 * 1024,
        max_value=2 * 1024 * 1024 * 1024,
    )
    web_pending_ttl_minutes = _parse_int_with_range(
        _profile_env("WEB_PENDING_TTL_MINUTES", prefix, "30", allow_fallback=False),
        default=30,
        min_value=1,
        max_value=1440,
    )

    storage_root = _resolve_project_path(
        project_root,
        _profile_env(
            "STORAGE_ROOT",
            prefix,
            _profile_path_default("storage", profile_name),
            allow_fallback=False,
        ),
        "storage",
    )
    logs_root = _resolve_project_path(
        project_root,
        _profile_env(
            "LOGS_ROOT",
            prefix,
            _profile_path_default("logs", profile_name),
            allow_fallback=False,
        ),
        "logs",
    )
    state_root = _resolve_project_path(
        project_root,
        _profile_env(
            "STATE_ROOT",
            prefix,
            _profile_path_default("state", profile_name),
            allow_fallback=False,
        ),
        "state",
    )
    config_root = project_root / "config"

    for required in (
        _resolve_project_path(project_root, audiences_config_path_raw, "config/audiences.json"),
        _resolve_project_path(project_root, objective_config_path_raw, "config/objective.json"),
        _resolve_project_path(project_root, message_templates_path_raw, "config/message_templates.json"),
    ):
        if not required.exists():
            raise ConfigError(f"Khong tim thay file cau hinh: {required}")
    if new_web_ads_enabled:
        for required in (
            _resolve_project_path(project_root, web_objective_config_path_raw, "config/web_objective.json"),
            _resolve_project_path(project_root, web_cta_config_path_raw, "config/web_cta.json"),
        ):
            if not required.exists():
                raise ConfigError(f"Khong tim thay file cau hinh Web: {required}")

    return Settings(
        project_root=project_root,
        storage_root=storage_root,
        logs_root=logs_root,
        state_root=state_root,
        config_root=config_root,
        telegram_bot_token=telegram_bot_token,
        telegram_allowed_user_id=telegram_allowed_user_id,
        meta_access_token=meta_access_token,
        meta_page_access_token=meta_page_access_token,
        meta_ad_account_id=meta_ad_account_id,
        meta_page_id=meta_page_id,
        meta_creative_access_token=meta_creative_access_token,
        meta_app_id=meta_app_id,
        meta_app_secret=meta_app_secret,
        meta_pixel_id=meta_pixel_id,
        meta_instagram_user_id=meta_instagram_user_id,
        meta_api_version=meta_api_version,
        app_timezone=app_timezone,
        app_currency=app_currency,
        retry_max=retry_max,
        retry_backoff_seconds=retry_backoff,
        new_web_ads_enabled=new_web_ads_enabled,
        new_messenger_media_ads_enabled=new_messenger_media_ads_enabled,
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
        report_pos_default_currency=report_pos_default_currency,
        daily_report_notify_chat_id=daily_report_notify_chat_id,
        daily_report_pos_lookback_days=daily_report_pos_lookback_days,
        daily_report_task_summary_enabled=daily_report_task_summary_enabled,
        daily_report_task_summary_max_items=daily_report_task_summary_max_items,
        daily_report_task_db_path=daily_report_task_db_path,
        daily_report_task_snapshot_enabled=daily_report_task_snapshot_enabled,
        daily_report_task_snapshot_url=daily_report_task_snapshot_url,
        daily_report_task_snapshot_secret=daily_report_task_snapshot_secret,
        daily_report_task_snapshot_timeout_seconds=daily_report_task_snapshot_timeout_seconds,
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
        reconcile_cod_product_sheet_enabled=reconcile_cod_product_sheet_enabled,
        reconcile_cod_product_sheet_name=reconcile_cod_product_sheet_name,
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
        profile=profile_name,
        profile_name=profile_name,
        audiences_config_path_raw=audiences_config_path_raw,
        objective_config_path_raw=objective_config_path_raw,
        message_templates_path_raw=message_templates_path_raw,
        pancake_td_sync_config_path_raw=pancake_td_sync_config_path_raw,
        pancake_td_color_alias_config_path_raw=pancake_td_color_alias_config_path_raw,
        thai_duong_order_payload_template_path_raw=thai_duong_order_payload_template_path_raw,
        web_objective_config_path_raw=web_objective_config_path_raw,
        web_cta_config_path_raw=web_cta_config_path_raw,
        sku_catalog_path_raw=sku_catalog_path_raw,
        sku_catalog_spreadsheet_id=sku_catalog_spreadsheet_id,
        sku_catalog_sheet_name=sku_catalog_sheet_name,
        sku_catalog_range=sku_catalog_range,
        web_media_max_bytes=web_media_max_bytes,
        web_pending_ttl_minutes=web_pending_ttl_minutes,
    )
