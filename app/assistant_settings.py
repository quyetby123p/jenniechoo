from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv

from app.exceptions import ConfigError


@dataclass(frozen=True)
class AssistantSettings:
    project_root: Path
    workspace_root: Path
    storage_root: Path
    logs_root: Path
    state_root: Path
    memory_root: Path
    memory_index_path: Path
    telegram_bot_token: str
    telegram_allowed_user_id: int
    timezone_name: str
    proactive_enabled: bool
    agenda_hour: int
    event_reminder_lead_minutes: int
    eod_hour: int
    redaction_enabled: bool
    rate_limit_per_minute: int
    openai_enabled: bool
    openai_api_key: str
    openai_model: str
    openai_timeout_seconds: int
    openai_max_tokens: int
    openai_retry_max: int
    openai_retry_backoff_seconds: list[int]
    google_oauth_client_id: str
    google_oauth_client_secret: str
    google_oauth_refresh_token: str
    google_oauth_token_uri: str
    google_calendar_ids: list[str]
    gmail_query_default: str
    sheets_spreadsheet_id: str
    sheets_gid: int
    tasks_enabled: bool = False
    task_group_chat_id: int = 0
    task_require_tag: bool = True
    task_manager_user_ids: list[int] = field(default_factory=list)
    task_weekly_summary_enabled: bool = False
    task_weekly_summary_weekday: int = 5
    task_weekly_summary_hour: int = 15
    task_weekly_summary_minute: int = 0
    task_weekly_summary_max_items: int = 5
    task_db_path: Path | None = None

    @property
    def pending_requests_dir(self) -> Path:
        return self.state_root / "pending_requests"

    @property
    def conversation_logs_dir(self) -> Path:
        return self.storage_root / "conversations"

    @property
    def run_logs_dir(self) -> Path:
        return self.storage_root / "runs"

    @property
    def reminder_state_file(self) -> Path:
        return self.state_root / "reminder_state.json"

    @property
    def rate_limit_state_file(self) -> Path:
        return self.state_root / "rate_limit_state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_root / "assistant_bot.instance.lock"

    @property
    def app_log_dir(self) -> Path:
        return self.logs_root

    @property
    def resolved_task_db_path(self) -> Path:
        if isinstance(self.task_db_path, Path):
            return self.task_db_path
        return self.storage_root / "tasks.db"


def _require_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ConfigError(f"Bien moi truong bat buoc bi thieu: {key}")
    return value


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


def _parse_retry_backoff(raw: str) -> list[int]:
    values: list[int] = []
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(max(1, int(token)))
        except (TypeError, ValueError):
            continue
    return values or [2, 5, 10]


def _parse_csv(raw: str) -> list[str]:
    values = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _parse_csv_int(raw: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for item in str(raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = int(text)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def _parse_optional_int(raw: str, *, default: int) -> int:
    text = str(raw).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _resolve_path(raw: str, *, base_dir: Path) -> Path:
    text = str(raw or "").strip()
    if not text:
        return base_dir
    path = Path(text)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_assistant_settings(project_root: Path | None = None) -> AssistantSettings:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    workspace_root = project_root.parent.parent

    token = _require_env("BOT3_TELEGRAM_TOKEN")
    allowed_user_id = int(_require_env("BOT3_ALLOWED_USER_ID"))
    timezone_name = os.getenv("BOT3_TIMEZONE", "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"

    proactive_enabled = _parse_bool(os.getenv("BOT3_PROACTIVE_ENABLED", "1"), default=True)
    agenda_hour = _parse_int(os.getenv("BOT3_AGENDA_HOUR", "8"), default=8, min_value=0, max_value=23)
    reminder_lead = _parse_int(
        os.getenv("BOT3_EVENT_REMINDER_LEAD_MINUTES", "30"),
        default=30,
        min_value=5,
        max_value=1440,
    )
    eod_hour = _parse_int(os.getenv("BOT3_EOD_HOUR", "21"), default=21, min_value=0, max_value=23)
    redaction_enabled = _parse_bool(os.getenv("BOT3_REDACTION_ENABLED", "1"), default=True)
    rate_limit_per_minute = _parse_int(
        os.getenv("BOT3_RATE_LIMIT_PER_MINUTE", "20"),
        default=20,
        min_value=1,
        max_value=1000,
    )

    openai_api_key = os.getenv("BOT3_OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("BOT3_OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    openai_timeout_seconds = _parse_int(
        os.getenv("BOT3_OPENAI_TIMEOUT_SECONDS", "45"),
        default=45,
        min_value=5,
        max_value=600,
    )
    openai_max_tokens = _parse_int(
        os.getenv("BOT3_OPENAI_MAX_TOKENS", "800"),
        default=800,
        min_value=64,
        max_value=12000,
    )
    openai_retry_max = _parse_int(
        os.getenv("BOT3_OPENAI_RETRY_MAX", "2"),
        default=2,
        min_value=0,
        max_value=10,
    )
    openai_retry_backoff = _parse_retry_backoff(os.getenv("BOT3_OPENAI_RETRY_BACKOFF", "2,5"))
    openai_enabled = _parse_bool(os.getenv("BOT3_OPENAI_ENABLED", "1"), default=True)

    google_oauth_client_id = os.getenv("BOT3_GOOGLE_OAUTH_CLIENT_ID", "").strip()
    google_oauth_client_secret = os.getenv("BOT3_GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    google_oauth_refresh_token = os.getenv("BOT3_GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    google_oauth_token_uri = (
        os.getenv("BOT3_GOOGLE_OAUTH_TOKEN_URI", "https://oauth2.googleapis.com/token").strip()
        or "https://oauth2.googleapis.com/token"
    )
    google_calendar_ids = _parse_csv(os.getenv("BOT3_GOOGLE_CALENDAR_IDS", "primary"))
    gmail_query_default = (
        os.getenv("BOT3_GMAIL_QUERY_DEFAULT", "is:unread category:primary newer_than:2d").strip()
        or "is:unread category:primary newer_than:2d"
    )
    sheets_spreadsheet_id = os.getenv("BOT3_SHEETS_SPREADSHEET_ID", "").strip()
    sheets_gid = _parse_int(
        os.getenv("BOT3_SHEETS_GID", "0"),
        default=0,
        min_value=0,
        max_value=2_147_483_647,
    )
    tasks_enabled = _parse_bool(os.getenv("BOT3_TASKS_ENABLED", "0"), default=False)
    task_group_chat_id = _parse_optional_int(os.getenv("BOT3_TASK_GROUP_CHAT_ID", ""), default=0)
    task_require_tag = _parse_bool(os.getenv("BOT3_TASK_REQUIRE_TAG", "1"), default=True)
    task_manager_user_ids = _parse_csv_int(os.getenv("BOT3_MANAGER_USER_IDS", ""))
    task_weekly_summary_enabled = _parse_bool(
        os.getenv("BOT3_TASK_WEEKLY_SUMMARY_ENABLED", "1"),
        default=True,
    )
    task_weekly_summary_weekday = _parse_int(
        os.getenv("BOT3_TASK_WEEKLY_SUMMARY_WEEKDAY", "5"),
        default=5,
        min_value=0,
        max_value=6,
    )
    task_weekly_summary_hour = _parse_int(
        os.getenv("BOT3_TASK_WEEKLY_SUMMARY_HOUR", "15"),
        default=15,
        min_value=0,
        max_value=23,
    )
    task_weekly_summary_minute = _parse_int(
        os.getenv("BOT3_TASK_WEEKLY_SUMMARY_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    task_weekly_summary_max_items = _parse_int(
        os.getenv("BOT3_TASK_WEEKLY_SUMMARY_MAX_ITEMS", "5"),
        default=5,
        min_value=1,
        max_value=20,
    )

    storage_root = project_root / "storage" / "assistant_bot"
    logs_root = project_root / "logs" / "assistant_bot"
    state_root = project_root / "state" / "assistant_bot"

    memory_root = _resolve_path(
        os.getenv("BOT3_MEMORY_ROOT", "memory"),
        base_dir=workspace_root,
    )
    memory_index_path = _resolve_path(
        os.getenv("BOT3_MEMORY_INDEX_PATH", "storage/assistant_bot/memory.db"),
        base_dir=project_root,
    )
    task_db_path = _resolve_path(
        os.getenv("BOT3_TASK_DB_PATH", "storage/assistant_bot/tasks.db"),
        base_dir=project_root,
    )

    return AssistantSettings(
        project_root=project_root,
        workspace_root=workspace_root,
        storage_root=storage_root,
        logs_root=logs_root,
        state_root=state_root,
        memory_root=memory_root,
        memory_index_path=memory_index_path,
        telegram_bot_token=token,
        telegram_allowed_user_id=allowed_user_id,
        timezone_name=timezone_name,
        proactive_enabled=proactive_enabled,
        agenda_hour=agenda_hour,
        event_reminder_lead_minutes=reminder_lead,
        eod_hour=eod_hour,
        redaction_enabled=redaction_enabled,
        rate_limit_per_minute=rate_limit_per_minute,
        openai_enabled=openai_enabled,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_timeout_seconds=openai_timeout_seconds,
        openai_max_tokens=openai_max_tokens,
        openai_retry_max=openai_retry_max,
        openai_retry_backoff_seconds=openai_retry_backoff,
        google_oauth_client_id=google_oauth_client_id,
        google_oauth_client_secret=google_oauth_client_secret,
        google_oauth_refresh_token=google_oauth_refresh_token,
        google_oauth_token_uri=google_oauth_token_uri,
        google_calendar_ids=google_calendar_ids,
        gmail_query_default=gmail_query_default,
        sheets_spreadsheet_id=sheets_spreadsheet_id,
        sheets_gid=sheets_gid,
        tasks_enabled=tasks_enabled,
        task_group_chat_id=task_group_chat_id,
        task_require_tag=task_require_tag,
        task_manager_user_ids=task_manager_user_ids,
        task_weekly_summary_enabled=task_weekly_summary_enabled,
        task_weekly_summary_weekday=task_weekly_summary_weekday,
        task_weekly_summary_hour=task_weekly_summary_hour,
        task_weekly_summary_minute=task_weekly_summary_minute,
        task_weekly_summary_max_items=task_weekly_summary_max_items,
        task_db_path=task_db_path,
    )
