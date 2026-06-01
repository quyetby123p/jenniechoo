from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from app.instance_lock import single_instance_lock
from app.logger import SecretMaskFilter
from app.media_approval_service import MediaApprovalService
from app.media_bot import MediaResearchBot
from app.media_research_service import MediaResearchService
from app.media_settings import load_media_settings
from app.media_sheet_service import MediaSheetService
from app.media_storage_service import MediaStorageService
from app.work_progress_api import WorkProgressApiServer
from app.work_progress_scheduler import WorkProgressScheduler
from app.work_progress_service import WorkProgressService
from app.work_progress_settings import load_work_progress_settings


def _print_line(text: str) -> None:
    sys.stdout.buffer.write((str(text) + "\n").encode("utf-8", errors="replace"))


def _configure_media_logger(log_dir: Path, secrets: list[str] | None = None) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("media_research_bot")
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

    file_handler = logging.FileHandler(log_dir / "media-bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if secrets:
        secret_filter = SecretMaskFilter(secrets=secrets)
        for handler in logger.handlers:
            handler.addFilter(secret_filter)

    return logger


def check_runtime_configuration() -> str:
    settings = load_media_settings()

    missing: list[str] = []
    placeholder_values = {"replace_me", "changeme", "your_value"}

    checks = {
        "env.MEDIA_BOT_TELEGRAM_TOKEN": settings.telegram_bot_token,
    }
    if settings.media_research_enabled:
        checks.update(
            {
                "env.MEDIA_RESEARCH_SERPAPI_API_KEY": settings.serpapi_api_key,
                "env.MEDIA_RESEARCH_CLOUDINARY_CLOUD_NAME": settings.cloudinary_cloud_name,
                "env.MEDIA_RESEARCH_CLOUDINARY_UPLOAD_PRESET": settings.cloudinary_upload_preset,
            }
        )

    for key, value in checks.items():
        normalized = str(value).strip().lower()
        if not normalized or normalized in placeholder_values:
            missing.append(key)

    if settings.telegram_allowed_user_id <= 0:
        missing.append("env.MEDIA_BOT_ALLOWED_USER_ID")

    if settings.media_research_enabled and settings.sheet_enabled:
        if settings.sheet_mode not in {"oauth_user", "oauth"}:
            missing.append("env.MEDIA_RESEARCH_SHEET_MODE")
        if not str(settings.sheet_spreadsheet_id).strip():
            missing.append("env.MEDIA_RESEARCH_SHEET_SPREADSHEET_ID")
        if not str(settings.sheet_oauth_client_id).strip():
            missing.append("env.MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID")
        if not str(settings.sheet_oauth_client_secret).strip():
            missing.append("env.MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET")
        if not str(settings.sheet_oauth_refresh_token).strip():
            missing.append("env.MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN")

    if settings.work_progress_enabled:
        wp_settings = load_work_progress_settings()
        if not wp_settings.manager_telegram_user_ids:
            missing.append("env.WORK_PROGRESS_MANAGER_TELEGRAM_IDS")
        if not str(wp_settings.telegram_bot_token).strip():
            missing.append("env.WORK_PROGRESS_TELEGRAM_BOT_TOKEN_or_MEDIA_BOT_TELEGRAM_TOKEN")

    if missing:
        return "Config media bot chua hop le, thieu cac muc: " + ", ".join(missing)
    return "Config media bot hop le."


async def run_bot() -> None:
    project_root = Path(__file__).resolve().parents[1]
    settings = load_media_settings(project_root=project_root)
    logger = _configure_media_logger(
        settings.logs_root,
        secrets=[
            settings.telegram_bot_token,
            settings.serpapi_api_key,
            settings.sheet_oauth_client_secret,
            settings.sheet_oauth_refresh_token,
        ],
    )

    storage = MediaStorageService(settings=settings, logger=logger)
    research = MediaResearchService(settings=settings, logger=logger)
    sheet = MediaSheetService(settings=settings, logger=logger)
    approval = MediaApprovalService()

    work_progress_service = None
    work_progress_scheduler = None
    work_progress_api_server = None
    if settings.work_progress_enabled:
        wp_settings = load_work_progress_settings(project_root=project_root)
        work_progress_service = WorkProgressService(settings=wp_settings, logger=logger)
        work_progress_scheduler = WorkProgressScheduler(
            settings=wp_settings,
            service=work_progress_service,
            logger=logger,
        )
        work_progress_api_server = WorkProgressApiServer(
            service=work_progress_service,
            host=wp_settings.api_host,
            port=wp_settings.api_port,
            logger=logger,
        )

    bot = MediaResearchBot(
        settings=settings,
        logger=logger,
        storage=storage,
        research=research,
        sheet=sheet,
        approval=approval,
        work_progress_service=work_progress_service,
        work_progress_scheduler=work_progress_scheduler,
        work_progress_api_server=work_progress_api_server,
    )
    await bot.run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Media Research Telegram Bot")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Kiem tra cau hinh .env cho media bot.",
    )
    args = parser.parse_args()

    if args.check_config:
        try:
            _print_line(check_runtime_configuration())
            return 0
        except Exception as exc:  # noqa: BLE001
            _print_line(f"Config check media bot that bai: {exc}")
            return 1

    try:
        settings = load_media_settings(project_root=Path(__file__).resolve().parents[1])
        with single_instance_lock(settings.lock_file):
            asyncio.run(run_bot())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        _print_line(f"Media bot dung do loi: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
