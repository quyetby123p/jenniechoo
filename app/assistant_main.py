from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import logging
from pathlib import Path
import sys
import time

from app.assistant_approval_service import AssistantApprovalService
from app.assistant_bot import TelegramAssistantBot
from app.assistant_google_service import AssistantGoogleService
from app.assistant_internal_ops_service import AssistantInternalOpsService
from app.assistant_memory_service import AssistantMemoryService
from app.assistant_openai_service import AssistantOpenAIService
from app.assistant_scheduler_service import AssistantSchedulerService
from app.assistant_settings import load_assistant_settings
from app.assistant_storage_service import AssistantStorageService
from app.assistant_task_service import AssistantTaskService
from app.instance_lock import single_instance_lock
from app.logger import SecretMaskFilter


def _print_line(text: str) -> None:
    sys.stdout.buffer.write((str(text) + "\n").encode("utf-8", errors="replace"))


def _configure_assistant_logger(log_dir: Path, secrets: list[str] | None = None) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("assistant_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_dir / "assistant-bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if secrets:
        secret_filter = SecretMaskFilter(secrets=secrets)
        for handler in logger.handlers:
            handler.addFilter(secret_filter)

    return logger


def check_runtime_configuration() -> str:
    settings = load_assistant_settings()
    missing: list[str] = []
    placeholder_values = {"replace_me", "changeme", "your_value"}

    checks = {
        "env.BOT3_TELEGRAM_TOKEN": settings.telegram_bot_token,
        "env.BOT3_ALLOWED_USER_ID": settings.telegram_allowed_user_id,
        "env.BOT3_GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
        "env.BOT3_GOOGLE_OAUTH_CLIENT_SECRET": settings.google_oauth_client_secret,
        "env.BOT3_GOOGLE_OAUTH_REFRESH_TOKEN": settings.google_oauth_refresh_token,
    }
    for key, value in checks.items():
        normalized = str(value).strip().lower()
        if not normalized or normalized in placeholder_values:
            missing.append(key)

    if settings.telegram_allowed_user_id <= 0:
        missing.append("env.BOT3_ALLOWED_USER_ID")
    if settings.openai_enabled and not str(settings.openai_api_key).strip():
        missing.append("env.BOT3_OPENAI_API_KEY")
    if not settings.google_calendar_ids:
        missing.append("env.BOT3_GOOGLE_CALENDAR_IDS")
    if settings.agenda_hour == settings.eod_hour:
        missing.append("env.BOT3_EOD_HOUR (khong nen trung BOT3_AGENDA_HOUR)")
    if settings.event_reminder_lead_minutes <= 0:
        missing.append("env.BOT3_EVENT_REMINDER_LEAD_MINUTES")
    if not settings.memory_root.exists():
        missing.append(f"path.BOT3_MEMORY_ROOT ({settings.memory_root})")
    if settings.tasks_enabled:
        if int(settings.task_group_chat_id) == 0:
            missing.append("env.BOT3_TASK_GROUP_CHAT_ID")
        if not settings.task_manager_user_ids:
            missing.append("env.BOT3_MANAGER_USER_IDS")
        if int(settings.task_weekly_summary_weekday) < 0 or int(settings.task_weekly_summary_weekday) > 6:
            missing.append("env.BOT3_TASK_WEEKLY_SUMMARY_WEEKDAY")
        if settings.daily_task_checkin_enabled:
            if not settings.daily_task_weekdays:
                missing.append("env.BOT3_DAILY_TASK_WEEKDAYS")
            if int(settings.daily_task_max_items) <= 0:
                missing.append("env.BOT3_DAILY_TASK_MAX_ITEMS")

    if missing:
        return "Config assistant bot chua hop le, thieu cac muc: " + ", ".join(missing)
    return "Config assistant bot hop le."


async def run_bot() -> None:
    project_root = Path(__file__).resolve().parents[1]
    settings = load_assistant_settings(project_root=project_root)
    logger = _configure_assistant_logger(
        settings.app_log_dir,
        secrets=[
            settings.telegram_bot_token,
            settings.openai_api_key,
            settings.google_oauth_client_secret,
            settings.google_oauth_refresh_token,
        ],
    )
    storage = AssistantStorageService(settings=settings, logger=logger)
    memory = AssistantMemoryService(settings=settings, logger=logger)
    google = AssistantGoogleService(settings=settings, logger=logger)
    openai = AssistantOpenAIService(settings=settings, logger=logger)
    internal_ops = AssistantInternalOpsService(project_root=project_root, logger=logger)
    approval = AssistantApprovalService()
    scheduler = AssistantSchedulerService(settings=settings, storage=storage)
    tasks = AssistantTaskService(settings=settings, logger=logger)

    bot = TelegramAssistantBot(
        settings=settings,
        logger=logger,
        storage=storage,
        memory=memory,
        google=google,
        openai=openai,
        internal_ops=internal_ops,
        approval=approval,
        scheduler=scheduler,
        tasks=tasks,
    )
    await bot.run()


def run_bot_forever() -> None:
    attempt = 0
    while True:
        try:
            asyncio.run(run_bot())
            attempt = 0
            _print_line("Assistant bot polling da dung, thu khoi dong lai sau 5 giay...")
            time.sleep(5)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            delay_seconds = min(60, 5 * attempt)
            timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            _print_line(
                f"[{timestamp}] Assistant bot gap loi: {exc}. "
                f"Thu khoi dong lai sau {delay_seconds} giay..."
            )
            time.sleep(delay_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal assistant Telegram bot (Bot 3).")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Kiem tra cau hinh .env cho bot 3.",
    )
    args = parser.parse_args()

    if args.check_config:
        try:
            _print_line(check_runtime_configuration())
            return 0
        except Exception as exc:  # noqa: BLE001
            _print_line(f"Config check assistant bot that bai: {exc}")
            return 1

    try:
        settings = load_assistant_settings(project_root=Path(__file__).resolve().parents[1])
        with single_instance_lock(settings.lock_file):
            run_bot_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        _print_line(f"Assistant bot dung do loi: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
