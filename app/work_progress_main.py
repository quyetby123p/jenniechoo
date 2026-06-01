from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
import threading

from app.work_progress_api import WorkProgressApiServer
from app.work_progress_scheduler import WorkProgressScheduler
from app.work_progress_service import WorkProgressService
from app.work_progress_settings import load_work_progress_settings


def _configure_logger(project_root: Path) -> logging.Logger:
    log_dir = project_root / "logs" / "work_progress"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("work_progress")
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

    file_handler = logging.FileHandler(log_dir / "work-progress.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def check_runtime_configuration() -> str:
    settings = load_work_progress_settings()
    missing: list[str] = []
    if not str(settings.database_url).strip():
        missing.append("env.WORK_PROGRESS_DATABASE_URL")
    if settings.api_port <= 0:
        missing.append("env.WORK_PROGRESS_API_PORT")
    if not settings.manager_telegram_user_ids:
        missing.append("env.WORK_PROGRESS_MANAGER_TELEGRAM_IDS")
    if not str(settings.telegram_bot_token).strip():
        missing.append("env.WORK_PROGRESS_TELEGRAM_BOT_TOKEN_or_BOT3_TELEGRAM_TOKEN")
    if settings.confidence_fast_track < 0.0 or settings.confidence_fast_track > 1.0:
        missing.append("env.WORK_PROGRESS_CONFIDENCE_FAST_TRACK")

    if missing:
        return "Config work progress chua hop le, thieu: " + ", ".join(missing)
    return "Config work progress hop le."


def main() -> int:
    parser = argparse.ArgumentParser(description="Work Progress multi-channel service.")
    parser.add_argument("--check-config", action="store_true", help="Kiem tra env cua work progress service.")
    parser.add_argument(
        "--serve-api",
        action="store_true",
        help="Chay HTTP API ingest/review/report.",
    )
    parser.add_argument(
        "--run-scheduler",
        action="store_true",
        help="Chay scheduler gui bao cao daily/weekly/monthly qua Telegram private.",
    )
    args = parser.parse_args()

    if args.check_config:
        print(check_runtime_configuration())
        return 0

    project_root = Path(__file__).resolve().parents[1]
    settings = load_work_progress_settings(project_root=project_root)
    logger = _configure_logger(project_root)
    service = WorkProgressService(settings=settings, logger=logger)

    run_api = bool(args.serve_api)
    run_scheduler = bool(args.run_scheduler)
    if not run_api and not run_scheduler:
        run_api = True
        run_scheduler = True

    api_server = WorkProgressApiServer(
        service=service,
        host=settings.api_host,
        port=settings.api_port,
        logger=logger,
    )
    scheduler = WorkProgressScheduler(
        settings=settings,
        service=service,
        logger=logger,
    )

    if run_api and run_scheduler:
        thread = threading.Thread(target=api_server.serve_forever, daemon=True, name="work-progress-api")
        thread.start()
        logger.info("Dang chay song song API + scheduler.")
        scheduler.run_forever()
        return 0
    if run_api:
        api_server.serve_forever()
        return 0
    scheduler.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

