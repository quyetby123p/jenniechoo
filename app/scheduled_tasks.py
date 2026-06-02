from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from typing import Any

from aiogram import Bot

from app.approval_service import ApprovalService
from app.daily_report_service import DailyReportService
from app.dedup_service import DedupService
from app.logger import configure_logger
from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.rollback_service import RollbackService
from app.settings import Settings, load_settings
from app.storage_service import StorageService
from app.telegram_bot import TelegramAdsBot
from app.thai_duong_cod_client import ThaiDuongCodClient


@dataclass
class ScheduledRuntime:
    settings: Settings
    bot: TelegramAdsBot
    telegram: Bot


def build_runtime(project_root: Path | None = None) -> ScheduledRuntime:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    settings = load_settings(project_root=project_root)
    logger = configure_logger(
        settings.app_logs_dir,
        secrets=[
            settings.telegram_bot_token,
            settings.meta_access_token,
            settings.meta_page_access_token,
            settings.pancake_api_key,
            settings.pancake_access_token,
            settings.reconcile_cod_sheet_webhook_secret,
        ],
    )
    storage = StorageService(settings=settings, logger=logger)
    meta = MetaAdsClient(settings=settings, logger=logger)
    pancake = PancakePosClient(settings=settings, logger=logger)
    thai_duong = ThaiDuongCodClient(settings=settings, logger=logger)
    reports = DailyReportService(
        settings=settings,
        logger=logger,
        pancake_client=pancake,
        meta_client=meta,
    )
    reconcile = ReconcileCodService(
        settings=settings,
        logger=logger,
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )
    reconcile_sheet = ReconcileCodSheetService(
        settings=settings,
        logger=logger,
    )
    pancake_td_sync = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logger,
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )
    telegram = Bot(token=settings.telegram_bot_token)
    bot = TelegramAdsBot(
        settings=settings,
        logger=logger,
        storage=storage,
        dedup=DedupService(storage=storage),
        meta_client=meta,
        daily_report_service=reports,
        reconcile_cod_service=reconcile,
        reconcile_cod_sheet_service=reconcile_sheet,
        pancake_td_sync_service=pancake_td_sync,
        thai_duong_client=thai_duong,
        approval_service=ApprovalService(),
        rollback_service=RollbackService(meta_client=meta, logger=logger),
    )
    bot._bot = telegram
    return ScheduledRuntime(settings=settings, bot=bot, telegram=telegram)


def parse_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


async def run_token_health(runtime: ScheduledRuntime) -> None:
    await runtime.bot._send_token_health_report(
        chat_id=runtime.settings.telegram_allowed_user_id,
        trigger_label="Kiểm tra định kỳ GitHub Actions",
        notify_success=True,
    )


async def run_daily_report(runtime: ScheduledRuntime, *, slot: str, report_date: date | None) -> None:
    selected_slot = str(slot or "morning").strip().lower()
    if selected_slot not in {"morning", "evening"}:
        raise ValueError("--slot phai la morning hoac evening")

    if report_date is None:
        report_date = runtime.bot._resolve_daily_report_date_for_slot(selected_slot)
    trigger_label = (
        "Báo cáo tự động buổi sáng (GitHub Actions)"
        if selected_slot == "morning"
        else "Báo cáo tự động buổi tối (GitHub Actions)"
    )

    report_payload: dict[str, Any] | None = None
    for chat_id in runtime.bot._resolve_daily_report_notify_chat_ids():
        report_payload = await runtime.bot._send_daily_report(
            chat_id=chat_id,
            trigger_label=trigger_label,
            report_date=report_date,
            notify_success=True,
            report_payload=report_payload,
            include_recent_rollups=(selected_slot == "morning" and runtime.bot._is_report_group_chat(chat_id)),
        )


async def run_reconcile_cash_in(runtime: ScheduledRuntime) -> None:
    if not runtime.settings.reconcile_cod_enabled:
        print("RECONCILE_COD_ENABLED=0, skip reconcile cash-in.")
        return
    await runtime.bot._send_reconcile_cod_cash_in_report(
        chat_id=runtime.bot._resolve_reconcile_cod_notify_chat_id(),
        trigger_label="Báo cáo tiền về tự động Thái Dương (GitHub Actions)",
    )


async def run_reconcile_weekly(runtime: ScheduledRuntime) -> None:
    if not runtime.settings.reconcile_cod_enabled:
        print("RECONCILE_COD_ENABLED=0, skip weekly reconcile summary.")
        return
    await runtime.bot._send_reconcile_cod_weekly_summary_report(
        chat_id=runtime.bot._resolve_reconcile_cod_notify_chat_id(),
        trigger_label="Tổng tiền nhận tuần tự động Thái Dương (GitHub Actions)",
    )


async def run_pancake_td_sync(runtime: ScheduledRuntime, *, max_batch: int | None, notify: str) -> None:
    if not runtime.settings.pancake_td_sync_enabled:
        print("PANCAKE_TD_SYNC_ENABLED=0, skip Pancake -> Thai Duong sync.")
        return
    report = await asyncio.to_thread(
        runtime.bot.pancake_td_sync.sync_once,
        max_batch=max_batch,
    )
    should_notify = bool(report.get("notify")) or str(notify).strip().lower() == "always"
    if not should_notify:
        print("Pancake -> Thai Duong sync completed without notification.")
        return

    notify_chat_id = (
        int(runtime.settings.pancake_td_sync_notify_chat_id)
        if int(runtime.settings.pancake_td_sync_notify_chat_id) != 0
        else int(runtime.settings.telegram_allowed_user_id)
    )
    text = runtime.bot.pancake_td_sync.build_message(
        report,
        trigger_label="Đồng bộ tự động Pancake -> Thái Dương (GitHub Actions)",
    )
    if len(text) > 3800:
        text = text[:3760] + "\n...\n(Đã rút gọn vì thông báo quá dài)"
    await runtime.telegram.send_message(chat_id=notify_chat_id, text=text)


async def run_task(args: argparse.Namespace) -> int:
    runtime = build_runtime()
    try:
        task = str(args.task).strip()
        if task == "token-health":
            await run_token_health(runtime)
        elif task == "daily-report":
            await run_daily_report(
                runtime,
                slot=args.slot,
                report_date=parse_date(args.date),
            )
        elif task == "reconcile-cash-in":
            await run_reconcile_cash_in(runtime)
        elif task == "reconcile-weekly":
            await run_reconcile_weekly(runtime)
        elif task == "pancake-td-sync":
            await run_pancake_td_sync(
                runtime,
                max_batch=args.max_batch,
                notify=args.notify,
            )
        else:
            raise ValueError(f"Unknown task: {task}")
        print(f"Scheduled task completed: {task} at {datetime.now(timezone.utc).isoformat()}")
        return 0
    finally:
        await runtime.telegram.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one scheduled cloud task.")
    subparsers = parser.add_subparsers(dest="task", required=True)

    subparsers.add_parser("token-health", help="Send Meta/Thai Duong token health report.")

    daily = subparsers.add_parser("daily-report", help="Send daily sales report.")
    daily.add_argument("--slot", choices=["morning", "evening"], default="morning")
    daily.add_argument("--date", default="", help="Optional report date in YYYY-MM-DD.")

    subparsers.add_parser("reconcile-cash-in", help="Send Thai Duong cash-in report.")
    subparsers.add_parser("reconcile-weekly", help="Send weekly Thai Duong cash-in summary.")

    pancake = subparsers.add_parser("pancake-td-sync", help="Run one Pancake -> Thai Duong sync batch.")
    pancake.add_argument("--max-batch", type=int, default=None)
    pancake.add_argument("--notify", choices=["auto", "always"], default="auto")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_task(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Scheduled task failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
