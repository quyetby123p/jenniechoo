from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import sys
import time

from app.approval_service import ApprovalService
from app.daily_report_service import DailyReportService
from app.daily_task_summary_service import DailyTaskSummaryService
from app.dedup_service import DedupService
from app.instance_lock import single_instance_lock
from app.logger import configure_logger
from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.rollback_service import RollbackService
from app.settings import load_settings
from app.storage_service import StorageService
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.telegram_bot import TelegramAdsBot
from app.utils import load_json


def check_runtime_configuration() -> str:
    settings = load_settings()
    audiences = load_json(settings.audiences_config_path)
    objective = load_json(settings.objective_config_path)
    templates = load_json(settings.message_templates_path)

    missing = []
    placeholder_values = {"replace_me", "changeme", "your_value"}

    runtime_checks = {
        "env.TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
        "env.META_ACCESS_TOKEN": settings.meta_access_token,
        "env.META_PAGE_ACCESS_TOKEN": settings.meta_page_access_token,
        "env.META_AD_ACCOUNT_ID": settings.meta_ad_account_id,
        "env.META_PAGE_ID": settings.meta_page_id,
    }
    placeholder_exact = {
        "env.META_AD_ACCOUNT_ID": {"act_1234567890", "1234567890"},
        "env.META_PAGE_ID": {"1234567890"},
    }
    for key, value in runtime_checks.items():
        normalized = str(value).strip().lower()
        if not normalized or normalized in placeholder_values:
            missing.append(key)
            continue
        if key in placeholder_exact and normalized in placeholder_exact[key]:
            missing.append(key)

    if settings.telegram_allowed_user_id == 123456789:
        missing.append("env.TELEGRAM_ALLOWED_USER_ID")

    if settings.daily_report_enabled:
        access_token = str(settings.pancake_access_token).strip().lower()
        api_key = str(settings.pancake_api_key).strip().lower()
        has_access_token = bool(access_token and access_token not in placeholder_values)
        has_api_key = bool(api_key and api_key not in placeholder_values)
        if not has_access_token and not has_api_key:
            missing.append("env.PANCAKE_ACCESS_TOKEN_or_PANCAKE_API_KEY")
        if settings.pancake_shop_id <= 0:
            missing.append("env.PANCAKE_SHOP_ID")
        if settings.report_thb_to_vnd_rate <= 0:
            missing.append("env.REPORT_THB_TO_VND_RATE")
        if settings.report_thb_minor_unit_factor <= 0:
            missing.append("env.REPORT_THB_MINOR_UNIT_FACTOR")

    if settings.reconcile_cod_enabled:
        if settings.pancake_shop_id <= 0:
            missing.append("env.PANCAKE_SHOP_ID")
        if not settings.reconcile_cod_source_config_path.exists():
            missing.append("config.reconcile_cod_source.json")
        if not settings.reconcile_cod_match_config_path.exists():
            missing.append("config.reconcile_cod_match.json")
        if not settings.reconcile_cod_status_map_config_path.exists():
            missing.append(str(settings.reconcile_cod_status_map_config_path))
        if settings.reconcile_cod_sheet_enabled:
            sheet_mode = str(settings.reconcile_cod_sheet_mode).strip().lower()
            if sheet_mode in {"apps_script", "webhook"}:
                if not str(settings.reconcile_cod_sheet_webhook_url).strip():
                    missing.append("env.RECONCILE_COD_SHEET_WEBHOOK_URL")
            elif sheet_mode in {"service_account", "google_api"}:
                if not str(settings.reconcile_cod_sheet_spreadsheet_id).strip():
                    missing.append("env.RECONCILE_COD_SHEET_SPREADSHEET_ID")
                if not settings.reconcile_cod_sheet_credentials_file.exists():
                    missing.append(str(settings.reconcile_cod_sheet_credentials_file))
            elif sheet_mode in {"oauth_user", "oauth"}:
                if not str(settings.reconcile_cod_sheet_spreadsheet_id).strip():
                    missing.append("env.RECONCILE_COD_SHEET_SPREADSHEET_ID")
                if not str(settings.reconcile_cod_sheet_oauth_client_id).strip():
                    missing.append("env.RECONCILE_COD_SHEET_OAUTH_CLIENT_ID")
                if not str(settings.reconcile_cod_sheet_oauth_client_secret).strip():
                    missing.append("env.RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET")
                if not str(settings.reconcile_cod_sheet_oauth_refresh_token).strip():
                    missing.append("env.RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN")
            else:
                missing.append("env.RECONCILE_COD_SHEET_MODE")

    if settings.pancake_td_sync_enabled:
        access_token = str(settings.pancake_access_token).strip().lower()
        api_key = str(settings.pancake_api_key).strip().lower()
        has_access_token = bool(access_token and access_token not in placeholder_values)
        has_api_key = bool(api_key and api_key not in placeholder_values)
        if not has_access_token and not has_api_key:
            missing.append("env.PANCAKE_ACCESS_TOKEN_or_PANCAKE_API_KEY")
        if settings.pancake_shop_id <= 0:
            missing.append("env.PANCAKE_SHOP_ID")
        if not settings.pancake_td_sync_config_path.exists():
            missing.append(str(settings.pancake_td_sync_config_path))
        if not settings.pancake_td_color_alias_config_path.exists():
            missing.append(str(settings.pancake_td_color_alias_config_path))
        if not settings.thai_duong_order_payload_template_path.exists():
            missing.append(str(settings.thai_duong_order_payload_template_path))

    for key in (
        "thoi_trang_saved_audience_id",
        "du_lich_saved_audience_id",
        "tiec_saved_audience_id",
    ):
        value = str(audiences.get(key, "")).strip()
        if not value or value == "replace_me":
            missing.append(f"audiences.{key}")

    template_name = str(objective.get("message_template_name", "")).strip()
    if not template_name:
        missing.append("objective.message_template_name")
    elif template_name not in templates.get("templates", {}):
        missing.append(
            f"message_templates.templates['{template_name}']"
        )

    if missing:
        return "Config chua hop le, thieu cac muc: " + ", ".join(missing)
    return "Config hop le."


async def run_bot() -> None:
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
    dedup = DedupService(storage=storage)
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
    approval = ApprovalService()
    rollback = RollbackService(meta_client=meta, logger=logger)

    bot = TelegramAdsBot(
        settings=settings,
        logger=logger,
        storage=storage,
        dedup=dedup,
        meta_client=meta,
        daily_report_service=reports,
        daily_task_summary_service=daily_task_summary,
        reconcile_cod_service=reconcile,
        reconcile_cod_sheet_service=reconcile_sheet,
        pancake_td_sync_service=pancake_td_sync,
        thai_duong_client=thai_duong,
        approval_service=approval,
        rollback_service=rollback,
    )
    await bot.run()


def run_bot_forever() -> None:
    attempt = 0
    while True:
        try:
            asyncio.run(run_bot())
            attempt = 0
            print("Bot polling da dung, thu khoi dong lai sau 5 giay...")
            time.sleep(5)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            delay_seconds = min(60, 5 * attempt)
            timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            print(
                f"[{timestamp}] Bot gap loi: {exc}. "
                f"Thu khoi dong lai sau {delay_seconds} giay..."
            )
            time.sleep(delay_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="FB Ads automation bot.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Kiem tra .env va config/*.json truoc khi chay bot.",
    )
    args = parser.parse_args()

    if args.check_config:
        try:
            print(check_runtime_configuration())
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Config check that bai: {exc}")
            return 1

    try:
        project_root = Path(__file__).resolve().parents[1]
        lock_file = project_root / "state" / "bot.instance.lock"
        with single_instance_lock(lock_file):
            run_bot_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Bot dung do loi: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
