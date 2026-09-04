from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.assistant_approval_service import AssistantApprovalService
from app.assistant_bot import TelegramAssistantBot, _format_time_label, _is_google_scope_error
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
    media_performance: MediaPerformanceService
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
    media_performance = MediaPerformanceService(
        settings=settings,
        logger=logger,
        meta_client=meta,
        pancake_client=pancake,
    )
    telegram = Bot(token=settings.telegram_bot_token)
    bot = TelegramAdsBot(
        settings=settings,
        logger=logger,
        storage=storage,
        dedup=DedupService(storage=storage),
        meta_client=meta,
        daily_report_service=reports,
        daily_task_summary_service=daily_task_summary,
        reconcile_cod_service=reconcile,
        reconcile_cod_sheet_service=reconcile_sheet,
        pancake_td_sync_service=pancake_td_sync,
        media_performance_service=media_performance,
        thai_duong_client=thai_duong,
        approval_service=ApprovalService(),
        rollback_service=RollbackService(meta_client=meta, logger=logger),
    )
    bot._bot = telegram
    return ScheduledRuntime(settings=settings, bot=bot, media_performance=media_performance, telegram=telegram)


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
            include_task_summary=(selected_slot == "evening"),
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


async def run_media_performance(runtime: ScheduledRuntime, *, codes: str, days: int | None, campaign: str) -> None:
    if not runtime.settings.media_analytics_enabled:
        print("MEDIA_ANALYTICS_ENABLED=0, skip ADS media performance.")
        return
    if not runtime.settings.media_analytics_auto_enabled:
        print("MEDIA_ANALYTICS_AUTO_ENABLED=0, skip ADS media performance.")
        return

    from app.models import MediaPerformanceCommand

    parsed_codes = [item.strip().upper() for item in str(codes or "").split(",") if item.strip()]
    safe_days = days or runtime.settings.media_analytics_history_days
    command = MediaPerformanceCommand(
        codes=parsed_codes,
        campaign_query=str(campaign or "").strip(),
        days=safe_days,
        raw_text="scheduled media-performance",
    )
    report = await asyncio.to_thread(runtime.media_performance.generate_report, command)
    messages = runtime.media_performance.build_messages(report)
    chat_id = runtime.settings.telegram_allowed_user_id
    for text in messages:
        await runtime.telegram.send_message(chat_id=chat_id, text=text)


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


def _assistant_run_date(runtime: AssistantScheduledRuntime, run_date: date | None) -> date:
    if run_date is not None:
        return run_date
    tzinfo = ZoneInfo(runtime.settings.timezone_name)
    return datetime.now(tzinfo).date()


async def run_bot3_agenda(runtime: AssistantScheduledRuntime, *, run_date: date | None) -> None:
    if not runtime.settings.proactive_enabled:
        print("BOT3_PROACTIVE_ENABLED=0, skip Bot 3 agenda.")
        return

    target_date = _assistant_run_date(runtime, run_date)
    day_key = target_date.isoformat()
    if not runtime.bot.scheduler.should_send_day_mark("agenda", day_key):
        print(f"Bot 3 agenda already sent for {day_key}.")
        return

    reply = await runtime.bot._build_agenda_reply(target_date)
    await runtime.bot._bot_send_message(
        int(runtime.settings.telegram_allowed_user_id),
        f"[Nhắc lịch {int(runtime.settings.agenda_hour):02d}:00]\n{reply}",
    )
    runtime.bot.scheduler.mark_day_sent("agenda", day_key)
    print(f"Bot 3 agenda sent for {day_key}.")


async def run_bot3_eod(runtime: AssistantScheduledRuntime, *, run_date: date | None) -> None:
    if not runtime.settings.proactive_enabled:
        print("BOT3_PROACTIVE_ENABLED=0, skip Bot 3 EOD.")
        return

    target_date = _assistant_run_date(runtime, run_date)
    day_key = target_date.isoformat()
    if not runtime.bot.scheduler.should_send_day_mark("eod", day_key):
        print(f"Bot 3 EOD already sent for {day_key}.")
        return

    reply = await runtime.bot._build_result_reply(target_date)
    await runtime.bot._bot_send_message(
        int(runtime.settings.telegram_allowed_user_id),
        f"[Tổng kết {int(runtime.settings.eod_hour):02d}:00]\n{reply}",
    )
    runtime.bot.scheduler.mark_day_sent("eod", day_key)
    print(f"Bot 3 EOD sent for {day_key}.")


async def run_bot3_task_weekly_summary(runtime: AssistantScheduledRuntime, *, run_date: date | None) -> None:
    if not runtime.settings.tasks_enabled:
        print("BOT3_TASKS_ENABLED=0, skip Bot 3 task weekly summary.")
        return
    if not runtime.settings.task_weekly_summary_enabled:
        print("BOT3_TASK_WEEKLY_SUMMARY_ENABLED=0, skip Bot 3 task weekly summary.")
        return
    if int(runtime.settings.task_group_chat_id) == 0:
        print("BOT3_TASK_GROUP_CHAT_ID is empty, skip Bot 3 task weekly summary.")
        return

    target_date = _assistant_run_date(runtime, run_date)
    if target_date.weekday() != int(runtime.settings.task_weekly_summary_weekday):
        print(f"Bot 3 task weekly summary skipped for non-summary day: {target_date.isoformat()}")
        return

    day_key = target_date.isoformat()
    if not runtime.bot.scheduler.should_send_day_mark("task_weekly_summary", day_key):
        print(f"Bot 3 task weekly summary already sent for {day_key}.")
        return

    snapshot = await asyncio.to_thread(
        runtime.bot.tasks.build_weekly_snapshot,
        reference_date=target_date,
        timezone_name=runtime.settings.timezone_name,
        max_items=int(runtime.settings.task_weekly_summary_max_items),
    )
    text = runtime.bot._build_task_weekly_reply(snapshot=snapshot, trigger_label="Tổng kết tuần tự động")
    await runtime.bot._bot_send_message(int(runtime.settings.task_group_chat_id), text)
    runtime.bot.scheduler.mark_day_sent("task_weekly_summary", day_key)
    print(f"Bot 3 task weekly summary sent for {day_key}.")


async def run_bot3_event_reminders(runtime: AssistantScheduledRuntime) -> None:
    if not runtime.settings.proactive_enabled:
        print("BOT3_PROACTIVE_ENABLED=0, skip Bot 3 event reminders.")
        return

    now_local = runtime.bot.scheduler.now_local()
    end_local = now_local + timedelta(minutes=int(runtime.settings.event_reminder_lead_minutes) + 5)
    try:
        events = await asyncio.to_thread(
            runtime.bot.google.fetch_events_between,
            now_local,
            end_local,
            max_per_calendar=20,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_google_scope_error(exc):
            print("Bot 3 event reminders skipped: missing Google Calendar/Gmail scope.")
            return
        raise

    due = runtime.bot.scheduler.pick_due_event_reminders(events, now_local=now_local)
    for event in due:
        summary = str(event.get("summary", "")).strip() or "(Không tiêu đề)"
        start_label = _format_time_label(str(event.get("start_iso", "")))
        await runtime.bot._bot_send_message(
            int(runtime.settings.telegram_allowed_user_id),
            "[Nhắc trước sự kiện]\n"
            f"- {summary}\n"
            f"- Bắt đầu lúc: {start_label}\n"
            f"- Còn khoảng: {int(runtime.settings.event_reminder_lead_minutes)} phút",
        )
        runtime.bot.scheduler.mark_event_reminded(event)
    print(f"Bot 3 event reminder check completed: {len(due)} due.")


def _work_progress_run_date(runtime: MediaScheduledRuntime, run_date: date | None) -> date:
    if run_date is not None:
        return run_date
    settings = runtime.work_progress_settings
    timezone_name = settings.timezone_name if settings else "Asia/Ho_Chi_Minh"
    return datetime.now(ZoneInfo(timezone_name)).date()


async def run_work_progress_report(
    runtime: MediaScheduledRuntime,
    *,
    report_type: str,
    run_date: date | None,
) -> None:
    if not runtime.settings.work_progress_enabled:
        print("MEDIA_BOT_WORK_PROGRESS_ENABLED=0, skip work-progress report.")
        return
    if not runtime.work_progress or not runtime.work_progress_scheduler:
        print("Work-progress runtime is not configured, skip report.")
        return

    selected_type = str(report_type or "").strip().lower()
    if selected_type not in {"daily", "weekly", "monthly"}:
        raise ValueError("report_type phai la daily, weekly hoac monthly")

    target_date = _work_progress_run_date(runtime, run_date)
    settings = runtime.work_progress_scheduler.settings
    if selected_type == "weekly" and target_date.weekday() != int(settings.weekly_report_weekday):
        print(f"Work-progress weekly skipped for non-weekly day: {target_date.isoformat()}")
        return
    if selected_type == "monthly":
        target_day = min(int(settings.monthly_report_day), _month_last_day(target_date))
        if target_date.day != target_day:
            print(f"Work-progress monthly skipped for non-monthly day: {target_date.isoformat()}")
            return

    day_key = target_date.isoformat()
    state = runtime.work_progress_scheduler._load_state()
    if not runtime.work_progress_scheduler._should_send(state, slot_name=selected_type, day_key=day_key):
        print(f"Work-progress {selected_type} already sent for {day_key}.")
        return

    anchor_date = target_date
    if selected_type == "daily":
        anchor_date = target_date + timedelta(days=int(settings.daily_report_offset_days))
    report = await asyncio.to_thread(runtime.work_progress.build_report, selected_type, anchor_date=anchor_date)
    text = await asyncio.to_thread(runtime.work_progress.format_report_text, report)
    await asyncio.to_thread(runtime.work_progress_scheduler._send_private_to_managers, text)
    runtime.work_progress_scheduler._mark_sent(state, slot_name=selected_type, day_key=day_key)
    runtime.work_progress_scheduler._save_state(state)
    print(f"Work-progress {selected_type} report sent for {day_key}.")


def _month_last_day(value: date) -> int:
    import calendar

    return calendar.monthrange(value.year, value.month)[1]


async def run_task(args: argparse.Namespace) -> int:
    assistant_tasks = {
        "bot3-agenda",
        "bot3-event-reminders",
        "bot3-eod",
        "bot3-task-weekly-summary",
        "bot3-daily-checkin",
    }
    if str(args.task).strip() in assistant_tasks:
        assistant_runtime = build_assistant_runtime()
        try:
            task = str(args.task).strip()
            if task == "bot3-daily-checkin":
                await run_bot3_daily_checkin(
                    assistant_runtime,
                    slot=args.slot,
                    run_date=parse_date(args.date),
                )
            elif task == "bot3-agenda":
                await run_bot3_agenda(assistant_runtime, run_date=parse_date(args.date))
            elif task == "bot3-event-reminders":
                await run_bot3_event_reminders(assistant_runtime)
            elif task == "bot3-eod":
                await run_bot3_eod(assistant_runtime, run_date=parse_date(args.date))
            elif task == "bot3-task-weekly-summary":
                await run_bot3_task_weekly_summary(assistant_runtime, run_date=parse_date(args.date))
            print(f"Scheduled task completed: {args.task} at {datetime.now(timezone.utc).isoformat()}")
            return 0
        finally:
            await assistant_runtime.telegram.session.close()

    work_progress_tasks = {
        "work-progress-daily": "daily",
        "work-progress-weekly": "weekly",
        "work-progress-monthly": "monthly",
    }
    if str(args.task).strip() in work_progress_tasks:
        media_runtime = build_media_runtime()
        try:
            task = str(args.task).strip()
            await run_work_progress_report(
                media_runtime,
                report_type=work_progress_tasks[task],
                run_date=parse_date(args.date),
            )
            print(f"Scheduled task completed: {args.task} at {datetime.now(timezone.utc).isoformat()}")
            return 0
        finally:
            await media_runtime.telegram.session.close()

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
        elif task == "media-performance":
            await run_media_performance(
                runtime,
                codes=args.codes,
                days=args.days,
                campaign=args.campaign,
            )
        else:
            raise ValueError(f"Unknown task: {task}")
        print(f"Scheduled task completed: {task} at {datetime.now(timezone.utc).isoformat()}")
        return 0
    finally:
        await runtime.telegram.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one scheduled cloud task.")
    parser.add_argument("--profile", default="", help="Ads profile for scheduled tasks.")
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

    media_performance = subparsers.add_parser("media-performance", help="Send ADS media performance report.")
    media_performance.add_argument("--codes", default="", help="Optional comma-separated VXV codes.")
    media_performance.add_argument("--days", type=int, default=None, help="Optional lookback days.")
    media_performance.add_argument("--campaign", default="", help="Optional campaign id/name hint.")

    bot3_agenda = subparsers.add_parser("bot3-agenda", help="Send Bot 3 morning agenda.")
    bot3_agenda.add_argument("--date", default="", help="Optional agenda date in YYYY-MM-DD.")

    subparsers.add_parser("bot3-event-reminders", help="Check and send Bot 3 event reminders.")

    bot3_eod = subparsers.add_parser("bot3-eod", help="Send Bot 3 end-of-day summary.")
    bot3_eod.add_argument("--date", default="", help="Optional summary date in YYYY-MM-DD.")

    bot3_weekly = subparsers.add_parser("bot3-task-weekly-summary", help="Send Bot 3 weekly task summary.")
    bot3_weekly.add_argument("--date", default="", help="Optional summary date in YYYY-MM-DD.")

    bot3_daily = subparsers.add_parser("bot3-daily-checkin", help="Send Bot 3 daily task check-in prompt.")
    bot3_daily.add_argument("--slot", choices=["morning", "evening"], default="morning")
    bot3_daily.add_argument("--date", default="", help="Optional check-in date in YYYY-MM-DD.")

    work_progress_daily = subparsers.add_parser("work-progress-daily", help="Send work-progress daily report.")
    work_progress_daily.add_argument("--date", default="", help="Optional report date in YYYY-MM-DD.")

    work_progress_weekly = subparsers.add_parser("work-progress-weekly", help="Send work-progress weekly report.")
    work_progress_weekly.add_argument("--date", default="", help="Optional report date in YYYY-MM-DD.")

    work_progress_monthly = subparsers.add_parser("work-progress-monthly", help="Send work-progress monthly report.")
    work_progress_monthly.add_argument("--date", default="", help="Optional report date in YYYY-MM-DD.")
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
