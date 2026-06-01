from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class WorkProgressSettings:
    project_root: Path
    storage_root: Path
    state_root: Path
    database_url: str
    api_host: str
    api_port: int
    timezone_name: str
    confidence_fast_track: float
    context_window_minutes: int
    manager_telegram_user_ids: list[int]
    telegram_bot_token: str
    daily_report_hour: int
    daily_report_minute: int
    daily_report_offset_days: int
    weekly_report_weekday: int
    weekly_report_hour: int
    weekly_report_minute: int
    monthly_report_day: int
    monthly_report_hour: int
    monthly_report_minute: int
    telegram_allowlist_channel_ids: list[str]
    zalo_allowlist_channel_ids: list[str]
    pancake_allowlist_channel_ids: list[str]
    forwarded_allowlist_channel_ids: list[str]

    @property
    def default_sqlite_path(self) -> Path:
        return self.storage_root / "work_progress" / "progress.db"

    @property
    def scheduler_state_file(self) -> Path:
        return self.state_root / "work_progress" / "scheduler_state.json"


def _parse_int(raw: str, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < min_value or value > max_value:
        return default
    return value


def _parse_float(raw: str, *, default: float, min_value: float, max_value: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < min_value or value > max_value:
        return default
    return value


def _parse_csv(raw: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        values.append(token)
    return values


def _parse_csv_int(raw: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def load_work_progress_settings(project_root: Path | None = None) -> WorkProgressSettings:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    storage_root = project_root / "storage"
    state_root = project_root / "state"

    database_url = os.getenv("WORK_PROGRESS_DATABASE_URL", "").strip()
    if not database_url:
        sqlite_path = project_root / "storage" / "work_progress" / "progress.db"
        database_url = f"sqlite:///{sqlite_path.as_posix()}"

    api_host = os.getenv("WORK_PROGRESS_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    api_port = _parse_int(
        os.getenv("WORK_PROGRESS_API_PORT", "8099"),
        default=8099,
        min_value=1,
        max_value=65535,
    )
    timezone_name = os.getenv("WORK_PROGRESS_TIMEZONE", "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"
    confidence_fast_track = _parse_float(
        os.getenv("WORK_PROGRESS_CONFIDENCE_FAST_TRACK", "0.75"),
        default=0.75,
        min_value=0.0,
        max_value=1.0,
    )
    context_window_minutes = _parse_int(
        os.getenv("WORK_PROGRESS_CONTEXT_WINDOW_MINUTES", "20"),
        default=20,
        min_value=1,
        max_value=240,
    )

    manager_ids = _parse_csv_int(os.getenv("WORK_PROGRESS_MANAGER_TELEGRAM_IDS", ""))
    telegram_bot_token = os.getenv("WORK_PROGRESS_TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token:
        telegram_bot_token = os.getenv("MEDIA_BOT_TELEGRAM_TOKEN", "").strip()
    if not telegram_bot_token:
        telegram_bot_token = os.getenv("BOT3_TELEGRAM_TOKEN", "").strip()

    daily_report_hour = _parse_int(
        os.getenv("WORK_PROGRESS_DAILY_REPORT_HOUR", "21"),
        default=21,
        min_value=0,
        max_value=23,
    )
    daily_report_minute = _parse_int(
        os.getenv("WORK_PROGRESS_DAILY_REPORT_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    daily_report_offset_days = _parse_int(
        os.getenv("WORK_PROGRESS_DAILY_REPORT_OFFSET_DAYS", "0"),
        default=0,
        min_value=-1,
        max_value=1,
    )
    weekly_report_weekday = _parse_int(
        os.getenv("WORK_PROGRESS_WEEKLY_REPORT_WEEKDAY", "5"),
        default=5,
        min_value=0,
        max_value=6,
    )
    weekly_report_hour = _parse_int(
        os.getenv("WORK_PROGRESS_WEEKLY_REPORT_HOUR", "15"),
        default=15,
        min_value=0,
        max_value=23,
    )
    weekly_report_minute = _parse_int(
        os.getenv("WORK_PROGRESS_WEEKLY_REPORT_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )
    monthly_report_day = _parse_int(
        os.getenv("WORK_PROGRESS_MONTHLY_REPORT_DAY", "1"),
        default=1,
        min_value=1,
        max_value=31,
    )
    monthly_report_hour = _parse_int(
        os.getenv("WORK_PROGRESS_MONTHLY_REPORT_HOUR", "9"),
        default=9,
        min_value=0,
        max_value=23,
    )
    monthly_report_minute = _parse_int(
        os.getenv("WORK_PROGRESS_MONTHLY_REPORT_MINUTE", "0"),
        default=0,
        min_value=0,
        max_value=59,
    )

    telegram_allowlist = _parse_csv(os.getenv("WORK_PROGRESS_TELEGRAM_ALLOWLIST_CHANNEL_IDS", ""))
    zalo_allowlist = _parse_csv(os.getenv("WORK_PROGRESS_ZALO_ALLOWLIST_CHANNEL_IDS", ""))
    pancake_allowlist = _parse_csv(os.getenv("WORK_PROGRESS_PANCAKE_ALLOWLIST_CHANNEL_IDS", ""))
    forwarded_allowlist = _parse_csv(os.getenv("WORK_PROGRESS_FORWARDED_ALLOWLIST_CHANNEL_IDS", ""))

    return WorkProgressSettings(
        project_root=project_root,
        storage_root=storage_root,
        state_root=state_root,
        database_url=database_url,
        api_host=api_host,
        api_port=api_port,
        timezone_name=timezone_name,
        confidence_fast_track=confidence_fast_track,
        context_window_minutes=context_window_minutes,
        manager_telegram_user_ids=manager_ids,
        telegram_bot_token=telegram_bot_token,
        daily_report_hour=daily_report_hour,
        daily_report_minute=daily_report_minute,
        daily_report_offset_days=daily_report_offset_days,
        weekly_report_weekday=weekly_report_weekday,
        weekly_report_hour=weekly_report_hour,
        weekly_report_minute=weekly_report_minute,
        monthly_report_day=monthly_report_day,
        monthly_report_hour=monthly_report_hour,
        monthly_report_minute=monthly_report_minute,
        telegram_allowlist_channel_ids=telegram_allowlist,
        zalo_allowlist_channel_ids=zalo_allowlist,
        pancake_allowlist_channel_ids=pancake_allowlist,
        forwarded_allowlist_channel_ids=forwarded_allowlist,
    )
