from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.assistant_approval_service import AssistantApprovalService
from app.assistant_bot import TelegramAssistantBot
from app.assistant_google_service import AssistantGoogleService
from app.assistant_internal_ops_service import AssistantInternalOpsService
from app.assistant_memory_service import AssistantMemoryService
from app.assistant_openai_service import AssistantOpenAIService
from app.assistant_scheduler_service import AssistantSchedulerService
from app.assistant_settings import AssistantSettings, load_assistant_settings
from app.assistant_storage_service import AssistantStorageService
from app.assistant_task_service import AssistantTaskService
from app.fb_payment_reconcile_service import FbPaymentReconcileService
from app.approval_service import ApprovalService
from app.daily_report_service import DailyReportService
from app.daily_task_summary_service import DailyTaskSummaryService
from app.dedup_service import DedupService
from app.logger import configure_logger
from app.media_approval_service import MediaApprovalService
from app.media_bot import MediaResearchBot
from app.media_main import _configure_media_logger
from app.media_performance_service import MediaPerformanceService
from app.media_research_service import MediaResearchService
from app.media_settings import MediaSettings, load_media_settings
from app.media_sheet_service import MediaSheetService
from app.media_storage_service import MediaStorageService
from app.meta_ads_client import MetaAdsClient
from app.models import MediaPerformanceCommand
from app.pancake_pos_client import PancakePosClient
from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.rollback_service import RollbackService
from app.settings import Settings, load_settings
from app.storage_service import StorageService
from app.telegram_bot import TelegramAdsBot
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.work_progress_scheduler import WorkProgressScheduler
from app.work_progress_service import WorkProgressService
from app.work_progress_settings import WorkProgressSettings, load_work_progress_settings


@dataclass
class ScheduledRuntime:
    settings: Settings
    bot: TelegramAdsBot
    telegram: Bot


@dataclass
class AssistantScheduledRuntime:
    settings: AssistantSettings
    bot: TelegramAssistantBot
    storage: AssistantStorageService
    telegram: Bot


@dataclass
class MediaScheduledRuntime:
    settings: MediaSettings
    bot: MediaResearchBot
    work_progress_settings: WorkProgressSettings | None
    work_progress: WorkProgressService | None
    work_progress_scheduler: WorkProgressScheduler | None
    telegram: Bot


def build_runtime(project_root: Path | None = None, profile: str | None = None) -> ScheduledRuntime:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    settings = load_settings(project_root=project_root, profile=profile)
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
    media_performance = MediaPerformanceService(
        settings=settings,
        logger=logger,
        meta_client=meta,
        pancake_client=pancake,
    )
    daily_task_summary = DailyTaskSummaryService(settings=settings, logger=logger)
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
        media_performance_service=media_performance,
        daily_task_summary_service=daily_task_summary,
        reconcile_cod_service=reconcile,
        reconcile_cod_sheet_service=reconcile_sheet,
        pancake_td_sync_service=pancake_td_sync,
        thai_duong_client=thai_duong,
        approval_service=ApprovalService(),
        rollback_service=RollbackService(meta_client=meta, logger=logger),
    )
    bot._bot = telegram
    return ScheduledRuntime(settings=settings, bot=bot, telegram=telegram)


def build_assistant_runtime(project_root: Path | None = None) -> AssistantScheduledRuntime:
    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    settings = load_assistant_settings(project_root=project_root)
    logger = configure_logger(
        settings.logs_root,
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
    scheduler = AssistantSchedulerService(settings=settings, storage=storage)
    tasks = AssistantTaskService(settings=settings, logger=logger)
    fb_reconcile = FbPaymentReconcileService(
        settings=settings,
        google=google,
        storage=storage,
        logger=logger,
    )
    telegram = Bot(token=settings.telegram_bot_token)
    bot = TelegramAssistantBot(
        settings=settings,
        logger=logger,
        storage=storage,
        memory=memory,
        google=google,
        openai=openai,
        internal_ops=internal_ops,
        approval=AssistantApprovalService(),
        scheduler=scheduler,
        tasks=tasks,
        fb_reconcile_service=fb_reconcile,
    )
    bot._bot = telegram
    return AssistantScheduledRuntime(settings=settings, bot=bot, storage=storage, telegram=telegram)


def build_media_runtime(project_root: Path | None = None) -> MediaScheduledRuntime:
    """Build the media-research runtime used by webhook updates for bot=media."""
    if project_root is None:
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

    work_progress_settings = None
    work_progress = None
    work_progress_scheduler = None
    if settings.work_progress_enabled:
        work_progress_settings = load_work_progress_settings(project_root=project_root)
        work_progress = WorkProgressService(settings=work_progress_settings, logger=logger)
        work_progress_scheduler = WorkProgressScheduler(
            settings=work_progress_settings,
            service=work_progress,
            logger=logger,
        )

    telegram = Bot(token=settings.telegram_bot_token)
    bot = MediaResearchBot(
        settings=settings,
        logger=logger,
        storage=storage,
        research=research,
        sheet=sheet,
        approval=approval,
        work_progress_service=work_progress,
        work_progress_scheduler=work_progress_scheduler,
        work_progress_api_server=None,
    )
    bot._bot = telegram
    return MediaScheduledRuntime(
        settings=settings,
        bot=bot,
        work_progress_settings=work_progress_settings,
        work_progress=work_progress,
        work_progress_scheduler=work_progress_scheduler,
        telegram=telegram,
    )


def parse_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


async def run_token_health(runtime: ScheduledRuntime) -> None:
    await runtime.bot._send_token_health_report(
        chat_id=runtime.settings.telegram_allowed_user_id,
        trigger_label="Kiểm tra định kỳ GitHub Actions",
        # Scheduled health checks should stay quiet when everything is healthy.
        # The bot still sends the full report when Meta/Thai Duong reports a
        # failure, so this remains a monitor without spamming Telegram.
        notify_success=False,
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
            include_task_summary=(selected_slot == "evening"),
        )


async def run_media_performance(runtime: ScheduledRuntime, *, codes: str, days: int | None) -> None:
    if not runtime.settings.media_analytics_enabled:
        print("MEDIA_ANALYTICS_ENABLED=0, skip media performance.")
        return
    if not runtime.settings.media_analytics_auto_enabled:
        print("MEDIA_ANALYTICS_AUTO_ENABLED=0, skip media performance.")
        return
    parsed_codes = [
        code.strip().upper()
        for code in str(codes or "").replace(";", ",").replace(" ", ",").split(",")
        if code.strip()
    ]
    safe_days = days or runtime.settings.media_analytics_history_days
    end_date = datetime.now(ZoneInfo(runtime.settings.app_timezone)).date()
    start_date = end_date.fromordinal(end_date.toordinal() - max(1, int(safe_days)) + 1)
    command = MediaPerformanceCommand(
        codes=parsed_codes,
        start_date=start_date,
        end_date=end_date,
        days=max(1, int(safe_days)),
        raw_text="scheduled media-performance",
    )
    await runtime.bot._send_media_performance_report(
        chat_id=runtime.settings.media_analytics_notify_chat_id or runtime.settings.telegram_allowed_user_id,
        command=command,
        trigger_label="Báo cáo media ADS2 tự động",
    )


async def run_reconcile_cash_in(
    runtime: ScheduledRuntime,
    *,
    report_date: date | None = None,
    source: str = "",
) -> None:
    if not runtime.settings.reconcile_cod_enabled:
        raise RuntimeError("RECONCILE_COD_ENABLED=0; scheduled reconcile cash-in is disabled.")
    await runtime.bot._send_reconcile_cod_cash_in_report(
        chat_id=runtime.bot._resolve_reconcile_cod_notify_chat_id(),
        trigger_label="Báo cáo tiền về tự động Thái Dương (GitHub Actions)",
        settlement_date=report_date,
        # Explicitly dated manual/backfill runs are quiet. The Cloudflare
        # scheduler passes its source so regular cash-in reports stay visible.
        notify_cash_in=(report_date is None or str(source).strip().lower() == "cloudflare-cron"),
    )


async def run_reconcile_weekly(runtime: ScheduledRuntime) -> None:
    if not runtime.settings.reconcile_cod_enabled:
        print("RECONCILE_COD_ENABLED=0, skip weekly reconcile summary.")
        return
    await runtime.bot._send_reconcile_cod_weekly_summary_report(
        chat_id=runtime.bot._resolve_reconcile_cod_notify_chat_id(),
        trigger_label="Tổng tiền nhận tuần tự động Thái Dương (GitHub Actions)",
    )


async def run_pancake_td_sync(
    runtime: ScheduledRuntime,
    *,
    max_batch: int | None,
    notify: str,
    order_code: str | None = None,
) -> None:
    if not runtime.settings.pancake_td_sync_enabled:
        print("PANCAKE_TD_SYNC_ENABLED=0, skip Pancake -> Thai Duong sync.")
        return
    normalized_order_code = str(order_code or "").strip()
    if normalized_order_code:
        report = await asyncio.to_thread(
            runtime.bot.pancake_td_sync.sync_order_code_manual,
            normalized_order_code,
        )
    else:
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
    profile_suffix = (
        ""
        if runtime.settings.profile_name in {"", "main", "default"}
        else f", {runtime.settings.profile_name}"
    )
    trigger_label = (
        f"Đồng bộ thủ công Pancake -> Thái Dương (GitHub Actions{profile_suffix}, mã {normalized_order_code})"
        if normalized_order_code
        else f"Đồng bộ tự động Pancake -> Thái Dương (GitHub Actions{profile_suffix})"
    )
    text = runtime.bot.pancake_td_sync.build_message(
        report,
        trigger_label=trigger_label,
    )
    if len(text) > 3800:
        text = text[:3760] + "\n...\n(Đã rút gọn vì thông báo quá dài)"
    await runtime.telegram.send_message(chat_id=notify_chat_id, text=text)


async def run_bot3_daily_checkin(runtime: AssistantScheduledRuntime, *, slot: str, run_date: date | None) -> None:
    selected_slot = str(slot or "morning").strip().lower()
    if selected_slot not in {"morning", "evening"}:
        raise ValueError("--slot phai la morning hoac evening")
    if not runtime.settings.tasks_enabled:
        print("BOT3_TASKS_ENABLED=0, skip Bot 3 daily check-in.")
        return
    if not runtime.settings.daily_task_checkin_enabled:
        print("BOT3_DAILY_TASK_CHECKIN_ENABLED=0, skip Bot 3 daily check-in.")
        return

    if run_date is None:
        tzinfo = ZoneInfo(runtime.settings.timezone_name)
        run_date = datetime.now(tzinfo).date()
    if run_date.weekday() not in set(runtime.settings.daily_task_weekdays):
        print(f"Bot 3 daily check-in skipped for non-workday: {run_date.isoformat()}")
        return

    day_key = run_date.isoformat()
    state = runtime.storage.load_daily_task_checkin_state()
    day_state = runtime.bot._get_daily_task_day_state(state, day_key)
    if selected_slot == "morning":
        if bool(day_state.get("morning_sent")):
            print(f"Bot 3 morning check-in already sent for {day_key}.")
            return
        await runtime.bot._send_daily_task_morning_prompt(day_key=day_key, state=state, day_state=day_state)
        print(f"Bot 3 morning check-in sent for {day_key}.")
        return

    task_uids = [str(item).strip() for item in day_state.get("task_uids", []) if str(item).strip()]
    if bool(day_state.get("evening_sent")):
        print(f"Bot 3 evening check-in already sent for {day_key}.")
        return
    if not bool(day_state.get("morning_answered")):
        print(f"Bot 3 evening check-in skipped for {day_key}: morning not answered.")
        return
    if bool(day_state.get("no_tasks")):
        print(f"Bot 3 evening check-in skipped for {day_key}: no tasks.")
        return
    if not task_uids:
        print(f"Bot 3 evening check-in skipped for {day_key}: no task_uids.")
        return
    await runtime.bot._send_daily_task_evening_prompt(
        day_key=day_key,
        task_uids=task_uids,
        state=state,
        day_state=day_state,
    )
    print(f"Bot 3 evening check-in sent for {day_key}.")


async def run_task(args: argparse.Namespace) -> int:
    if str(args.task).strip() == "bot3-daily-checkin":
        assistant_runtime = build_assistant_runtime()
        try:
            await run_bot3_daily_checkin(
                assistant_runtime,
                slot=args.slot,
                run_date=parse_date(args.date),
            )
            print(f"Scheduled task completed: {args.task} at {datetime.now(timezone.utc).isoformat()}")
            return 0
        finally:
            await assistant_runtime.telegram.session.close()

    runtime = build_runtime(profile=args.profile)
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
        elif task == "media-performance":
            await run_media_performance(
                runtime,
                codes=args.codes,
                days=args.days,
            )
        elif task == "reconcile-cash-in":
            await run_reconcile_cash_in(
                runtime,
                report_date=parse_date(args.date),
                source=args.source,
            )
        elif task == "reconcile-weekly":
            await run_reconcile_weekly(runtime)
        elif task == "pancake-td-sync":
            await run_pancake_td_sync(
                runtime,
                max_batch=args.max_batch,
                notify=args.notify,
                order_code=args.order_code,
            )
        else:
            raise ValueError(f"Unknown task: {task}")
        print(f"Scheduled task completed: {task} at {datetime.now(timezone.utc).isoformat()}")
        return 0
    finally:
        await runtime.telegram.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one scheduled cloud task.")
    parser.add_argument("--profile", choices=["main", "ads2"], default="main")
    subparsers = parser.add_subparsers(dest="task", required=True)

    subparsers.add_parser("token-health", help="Send Meta/Thai Duong token health report.")

    daily = subparsers.add_parser("daily-report", help="Send daily sales report.")
    daily.add_argument("--slot", choices=["morning", "evening"], default="morning")
    daily.add_argument("--date", default="", help="Optional report date in YYYY-MM-DD.")

    media_performance = subparsers.add_parser("media-performance", help="Send ADS media performance report.")
    media_performance.add_argument("--codes", default="", help="Optional comma-separated VXV codes.")
    media_performance.add_argument("--days", type=int, default=None, help="Lookback days. Default from settings.")

    reconcile_cash_in = subparsers.add_parser("reconcile-cash-in", help="Send Thai Duong cash-in report.")
    reconcile_cash_in.add_argument(
        "--date",
        default="",
        help="Optional settlement date in YYYY-MM-DD; scheduled cloud runs pass this explicitly.",
    )
    reconcile_cash_in.add_argument(
        "--source",
        default="",
        help="Dispatch source; cloudflare-cron keeps the regular cash-in notification enabled.",
    )
    subparsers.add_parser("reconcile-weekly", help="Send weekly Thai Duong cash-in summary.")

    pancake = subparsers.add_parser("pancake-td-sync", help="Run one Pancake -> Thai Duong sync batch.")
    pancake.add_argument("--max-batch", type=int, default=None)
    pancake.add_argument("--notify", choices=["auto", "always"], default="auto")
    pancake.add_argument("--order-code", default="", help="Optional Pancake order code for one-order canary sync.")

    bot3_daily = subparsers.add_parser("bot3-daily-checkin", help="Send Bot 3 daily task check-in prompt.")
    bot3_daily.add_argument("--slot", choices=["morning", "evening"], default="morning")
    bot3_daily.add_argument("--date", default="", help="Optional check-in date in YYYY-MM-DD.")
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

