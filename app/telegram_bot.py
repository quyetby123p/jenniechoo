from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import unicodedata
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
import requests

from app.approval_service import ApprovalService
from app.campaign_planner import (
    DEFAULT_AUDIENCE_LAYOUT,
    build_campaign_plan,
    build_existing_campaign_plan,
    build_messenger_media_campaign_plan,
    build_non_jc_hashtag_suffix,
    build_web_campaign_plan,
    extract_sku_codes,
    resolve_sku_catalog,
    resolve_web_sku_codes,
    resolve_sku_prefix,
)
from app.command_parser import (
    parse_ads_command,
    parse_reconcile_cod_date_argument,
    parse_report_date_argument,
    try_parse_pancake_td_sync_command,
    try_parse_media_performance_command,
    try_parse_reconcile_cod_command,
    try_parse_report_command,
    try_parse_web_ads_form,
    try_parse_web_media_url_message,
    parse_web_budget,
    parse_web_cta,
    parse_web_url,
    normalize_web_caption,
)
from app.cloud_schedule_guard import CloudScheduleGuardClient
from app.daily_report_service import DailyReportService
from app.daily_task_summary_service import DailyTaskSummaryService
from app.dedup_service import DedupService
from app.exceptions import CommandParseError, MetaApiError, ValidationError
from app.media_performance_service import MediaPerformanceService
from app.meta_ads_client import MetaAdsClient
from app.media_owner import normalize_media_owner
from app.models import (
    AdsCommand,
    AudienceSlot,
    MediaPerformanceCommand,
    MessengerMediaAdsInput,
    PlannedCampaign,
    WebAdsInput,
)
from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.rollback_service import RollbackService
from app.settings import Settings
from app.storage_service import StorageService
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import dump_json, fingerprint, load_json, normalize_facebook_url, now_utc_iso
from app.web_lifecycle import normalize_web_lifecycle_strategy, web_lifecycle_label


_DISABLED_LOCAL_SCHEDULE_CLAIM = object()
_TELEGRAM_BOT_API_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class _WebRecoveryMessage:
    """Small Message-compatible adapter for resuming a persisted Web form."""

    def __init__(self, bot: Bot, chat_id: int, user_id: int) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, text: str, **kwargs: Any) -> Any:
        return await self.bot.send_message(chat_id=self.chat.id, text=text, **kwargs)


class TelegramAdsBot:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        storage: StorageService,
        dedup: DedupService,
        meta_client: MetaAdsClient,
        daily_report_service: DailyReportService,
        approval_service: ApprovalService,
        rollback_service: RollbackService,
        media_performance_service: MediaPerformanceService | None = None,
        daily_task_summary_service: DailyTaskSummaryService | None = None,
        reconcile_cod_service: ReconcileCodService | None = None,
        reconcile_cod_sheet_service: ReconcileCodSheetService | None = None,
        pancake_td_sync_service: PancakeToThaiDuongSyncService | None = None,
        thai_duong_client: ThaiDuongCodClient | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.cloud_schedule_guard = CloudScheduleGuardClient.from_env(logger=logger)
        self.storage = storage
        self.dedup = dedup
        self.meta = meta_client
        self.reports = daily_report_service
        self.media_performance = media_performance_service
        self.daily_task_summary = daily_task_summary_service
        self.reconcile = reconcile_cod_service
        self.reconcile_sheet = reconcile_cod_sheet_service
        self.pancake_td_sync = pancake_td_sync_service
        self.thai_duong = thai_duong_client
        self.approval = approval_service
        self.rollback = rollback_service
        self.router = Router(name="fb_ads_router")
        self._bot: Bot | None = None
        self._token_monitor_task: asyncio.Task[None] | None = None
        self._daily_report_task: asyncio.Task[None] | None = None
        self._reconcile_cod_task: asyncio.Task[None] | None = None
        self._pancake_td_sync_task: asyncio.Task[None] | None = None
        self._bot_username: str = ""
        self._job_action_locks: dict[str, asyncio.Lock] = {}
        self._last_daily_report_send_ok: bool = True

        self.router.message.register(self.handle_start_command, Command("start"))
        self.router.message.register(self.handle_ads_command, Command("ads"))
        self.router.message.register(self.handle_report_command, Command("report"))
        self.router.message.register(self.handle_media_performance_command, Command("media_perf"))
        self.router.message.register(self.handle_reconcile_command, Command("reconcile"))
        self.router.message.register(self.handle_token_command, Command("token"))
        self.router.message.register(self.handle_new_chat_members, F.new_chat_members)
        self.router.message.register(self.handle_media_message, F.photo | F.video)
        self.router.message.register(self.handle_text_message, F.text)
        self.router.callback_query.register(self.handle_callback, F.data)

    async def _answer_callback_safely(
            self,
            query: CallbackQuery,
            text: str = "",
            *,
            show_alert: bool = False,
        ) -> bool:
            try:
                await query.answer(text, show_alert=show_alert)
                return True
            except Exception as exc:  # noqa: BLE001
                error_text = str(exc).lower()
                if (
                    "query is too old" in error_text
                    or "response timeout expired" in error_text
                    or "query id is invalid" in error_text
                ):
                    self.logger.warning("Bo qua callback answer qua han: %s", exc)
                    return False
                raise

    @staticmethod
    def _as_messenger_only_asset_feed_spec(
            asset_feed_spec: dict[str, Any],
            page_id: str,
        ) -> dict[str, Any]:
            normalized = copy.deepcopy(asset_feed_spec)
            call_to_actions = normalized.get("call_to_actions")
            message_page_actions = [
                copy.deepcopy(item)
                for item in call_to_actions
                if isinstance(item, dict) and str(item.get("type", "")).strip().upper() == "MESSAGE_PAGE"
            ] if isinstance(call_to_actions, list) else []
            if not message_page_actions:
                message_page_actions = [
                    {
                        "type": "MESSAGE_PAGE",
                        "value": {
                            "app_destination": "MESSENGER",
                            "link": "https://fb.com/messenger_doc/",
                        },
                    }
                ]
            for action in message_page_actions:
                value = action.get("value")
                if not isinstance(value, dict):
                    value = {}
                value["app_destination"] = "MESSENGER"
                if not str(value.get("link", "")).strip():
                    value["link"] = "https://fb.com/messenger_doc/"
                action["value"] = value
            normalized["call_to_actions"] = message_page_actions[:1]
            normalized["ad_formats"] = ["CAROUSEL"]
            normalized["link_urls"] = [{"website_url": f"https://m.me/{str(page_id).strip()}"}]
            additional_data = normalized.get("additional_data")
            if not isinstance(additional_data, dict):
                additional_data = {}
            additional_data["is_click_to_message"] = True
            normalized["additional_data"] = additional_data
            return normalized

    @staticmethod
    def _config_int(value: Any, default: int) -> int:
            try:
                parsed = int(str(value).strip())
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

    @staticmethod
    def _config_list(value: Any, default: list[str] | None = None) -> list[str]:
            if value is None:
                return list(default or [])
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            text = str(value).strip()
            if not text:
                return list(default or [])
            return [part.strip() for part in text.split(",") if part.strip()]

    def _find_new_campaign_source_ads(
            self,
            plan: PlannedCampaign,
            objective_config: dict[str, Any],
        ) -> dict[str, Any] | None:
            keywords = [code for code in str(plan.sku_code_text or "").split("_") if code]
            if not keywords:
                return None

            source_campaign_template = objective_config.get(
                "new_campaign_source_campaign_name_template",
                "ADS:QUYET|MK:ThaiLan|{sku_code_text}|Codex",
            )
            source_campaign_name = self._format_new_campaign_template(
                source_campaign_template,
                plan,
            )
            source_campaign_contains = self._format_new_campaign_template(
                objective_config.get("new_campaign_source_campaign_name_contains", ""),
                plan,
            )
            source_adset_template = objective_config.get(
                "new_campaign_source_adset_name_template",
                "{source_campaign_name} - Thời trang",
            )
            source_adset_contains = self._format_new_campaign_template(
                objective_config.get("new_campaign_source_adset_name_contains", ""),
                plan,
                source_campaign_name=source_campaign_name,
            )
            source_ad_name_contains = self._format_new_campaign_template(
                objective_config.get("new_campaign_source_ad_name_contains", "SKU:{sku_code_text}"),
                plan,
                source_campaign_name=source_campaign_name,
            )
            max_campaigns = self._config_int(objective_config.get("new_campaign_source_max_campaigns"), 20)
            max_ads = self._config_int(objective_config.get("new_campaign_source_max_ads"), 50)
            include_statuses = self._config_list(
                objective_config.get("new_campaign_source_ad_statuses"),
                ["ACTIVE", "PAUSED"],
            )

            campaigns = self.meta.find_active_campaigns_by_keywords(keywords)
            campaign_checks = 0
            normalized_source_campaign_name = self._normalized_text(source_campaign_name)
            normalized_source_campaign_contains = self._normalized_text(source_campaign_contains)
            normalized_source_adset_contains = self._normalized_text(source_adset_contains)

            for campaign in campaigns:
                if campaign_checks >= max_campaigns:
                    break
                campaign_name = str(campaign.get("name", "")).strip()
                normalized_campaign_name = self._normalized_text(campaign_name)
                if normalized_source_campaign_name and normalized_campaign_name != normalized_source_campaign_name:
                    continue
                if (
                    normalized_source_campaign_contains
                    and normalized_source_campaign_contains not in normalized_campaign_name
                ):
                    continue
                campaign_id = str(campaign.get("id", "")).strip()
                if not campaign_id:
                    continue
                campaign_checks += 1
                adsets = self.meta.list_eligible_adsets(campaign_id, 20)
                expected_adset_name = self._format_new_campaign_template(
                    source_adset_template,
                    plan,
                    source_campaign_name=campaign_name,
                )
                normalized_expected_adset_name = self._normalized_text(expected_adset_name)
                for adset in adsets:
                    adset_id = str(adset.get("id", "")).strip()
                    adset_name = str(adset.get("name", "")).strip()
                    normalized_adset_name = self._normalized_text(adset_name)
                    if normalized_expected_adset_name and normalized_adset_name != normalized_expected_adset_name:
                        continue
                    if normalized_source_adset_contains and normalized_source_adset_contains not in normalized_adset_name:
                        continue
                    if not adset_id:
                        continue
                    ads = self.meta.list_adset_ads_for_copy(
                        adset_id,
                        max_ads=max_ads,
                        include_statuses=include_statuses,
                        name_contains=source_ad_name_contains,
                    )
                    if not ads:
                        continue
                    return {
                        "campaign_id": campaign_id,
                        "campaign_name": campaign_name,
                        "adset_id": adset_id,
                        "adset_name": adset_name,
                        "ads": ads,
                    }
            return None

    @staticmethod
    def _format_new_campaign_template(
            template: Any,
            plan: PlannedCampaign,
            *,
            source_campaign_name: str = "",
        ) -> str:
            text = str(template or "").strip()
            if not text:
                return ""
            return text.format(
                campaign_name=plan.campaign_name,
                sku_code_text=plan.sku_code_text,
                source_campaign_name=source_campaign_name,
            ).strip()

    @staticmethod
    def _merge_resolved_posts(
            primary_posts: list[Any],
            fallback_posts: list[Any],
        ) -> list[Any]:
            merged: list[Any] = []
            seen: set[str] = set()
            for post_item in [*primary_posts, *fallback_posts]:
                object_story_id = str(getattr(post_item, "object_story_id", "") or "").strip()
                if not object_story_id or object_story_id in seen:
                    continue
                seen.add(object_story_id)
                merged.append(post_item)
            return merged

    @staticmethod
    def _new_campaign_ad_name_for_post(
            plan: PlannedCampaign,
            resolved_post: Any,
            sku_prefix: str,
        ) -> str:
            media_label = str(
                getattr(resolved_post, "media_label", "") or plan.media_label or "Anh"
            ).strip() or "Anh"
            message_text = str(getattr(resolved_post, "message_text", "") or "")
            non_sku_suffix = build_non_jc_hashtag_suffix(message_text, sku_prefix)
            return f"ADS:QUYET|MK:ThaiLan|SKU:{plan.sku_code_text}|MED:{media_label}{non_sku_suffix}"

    @classmethod
    def _new_campaign_creative_overrides(
            cls,
            plan: PlannedCampaign,
            resolved_post: Any,
            active_destination_type: str,
            multi_destination_creative_overrides: dict[str, Any] | None,
            multi_destination_asset_feed_spec: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            creative_extra_overrides: dict[str, Any] | None = None
            if (
                active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                and isinstance(multi_destination_creative_overrides, dict)
                and multi_destination_creative_overrides
            ):
                creative_extra_overrides = dict(multi_destination_creative_overrides)
            elif (
                active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                and isinstance(multi_destination_asset_feed_spec, dict)
                and multi_destination_asset_feed_spec
            ):
                creative_extra_overrides = {
                    "asset_feed_spec": cls._with_message_page_cta_preferred(
                        multi_destination_asset_feed_spec
                    )
                }
            if (
                active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                and cls._should_force_message_page_cta_for_post(plan, resolved_post)
            ):
                if creative_extra_overrides is None:
                    creative_extra_overrides = {}
                creative_extra_overrides["call_to_action_type"] = "MESSAGE_PAGE"
            return creative_extra_overrides

    @staticmethod
    def _new_campaign_fallback_destination_type(objective_config: dict[str, Any]) -> str:
            value = objective_config.get("new_campaign_fallback_destination_type", "MESSENGER")
            normalized = str(value or "").strip().upper()
            if normalized in {"", "0", "FALSE", "NO", "NONE", "DISABLED", "OFF"}:
                return ""
            return normalized

    @classmethod
    def _new_campaign_messenger_retry_overrides(
            cls,
            page_id: str,
            multi_destination_creative_overrides: dict[str, Any] | None,
            multi_destination_asset_feed_spec: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            seed = (
                dict(multi_destination_creative_overrides)
                if isinstance(multi_destination_creative_overrides, dict)
                else {}
            )
            asset_feed_spec = seed.get("asset_feed_spec")
            if not isinstance(asset_feed_spec, dict) or not asset_feed_spec:
                asset_feed_spec = multi_destination_asset_feed_spec
            if not isinstance(asset_feed_spec, dict) or not asset_feed_spec:
                return None

            retry_overrides: dict[str, Any] = {
                "asset_feed_spec": cls._as_messenger_only_asset_feed_spec(asset_feed_spec, page_id),
            }
            for field in ("page_welcome_message", "instagram_user_id", "instagram_actor_id"):
                value = seed.get(field)
                if value not in (None, "", {}, []):
                    retry_overrides[field] = value
            return retry_overrides

    @staticmethod
    def _normalize_label_for_match(value: str) -> str:
            normalized = unicodedata.normalize("NFD", str(value or ""))
            without_marks = "".join(
                character for character in normalized if unicodedata.category(character) != "Mn"
            )
            return re.sub(r"[^0-9a-z]+", " ", without_marks.lower()).strip()

    @staticmethod
    def _normalized_text(value: Any) -> str:
            return str(value or "").strip().upper()

    @classmethod
    def _resolve_audience_layout_for_adset(
            cls,
            adset_name: str,
            fallback_index: int,
        ) -> tuple[str, str, str] | None:
            normalized_adset_name = cls._normalize_label_for_match(adset_name)
            for audience_key, audience_label, audience_suffix in DEFAULT_AUDIENCE_LAYOUT:
                normalized_label = cls._normalize_label_for_match(audience_label)
                if normalized_label and normalized_label in normalized_adset_name:
                    return audience_key, audience_label, audience_suffix
            if 0 <= fallback_index < len(DEFAULT_AUDIENCE_LAYOUT):
                return DEFAULT_AUDIENCE_LAYOUT[fallback_index]
            return None

    @staticmethod
    def _should_clone_new_campaign_source_ads(objective_config: dict[str, Any]) -> bool:
            value = objective_config.get("new_campaign_clone_source_adset_ads", False)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

    @staticmethod
    def _should_collect_sku_page_posts(objective_config: dict[str, Any]) -> bool:
            value = objective_config.get(
                "collect_sku_page_posts",
                objective_config.get("new_campaign_collect_sku_page_posts", False),
            )
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

    @staticmethod
    def _should_force_message_page_cta_for_post(
            plan: PlannedCampaign,
            resolved_post: Any,
        ) -> bool:
            adset_overrides = plan.raw.get("adset_payload_overrides", {}) if isinstance(plan.raw, dict) else {}
            destination_type = ""
            if isinstance(adset_overrides, dict):
                destination_type = str(adset_overrides.get("destination_type", "")).strip().upper()
            if destination_type != "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                return False
            sku_text = str(plan.sku_code_text or "").strip().upper()
            campaign_name = str(plan.campaign_name or "").strip().upper()
            if "VXV" not in sku_text and "VXV" not in campaign_name:
                return False
            _ = resolved_post
            return True

    @staticmethod
    def _should_reuse_same_story_ad_for_new_campaign(objective_config: dict[str, Any]) -> bool:
            value = objective_config.get(
                "new_campaign_reuse_same_story_ad",
                objective_config.get("reuse_same_story_ad", False),
            )
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

    @classmethod
    def _with_message_page_cta_preferred(cls, asset_feed_spec: dict[str, Any]) -> dict[str, Any]:
            normalized = copy.deepcopy(asset_feed_spec)
            call_to_actions = normalized.get("call_to_actions")
            if not isinstance(call_to_actions, list):
                return normalized
            normalized["call_to_actions"] = sorted(
                call_to_actions,
                key=lambda item: 0
                if isinstance(item, dict) and str(item.get("type", "")).strip().upper() == "MESSAGE_PAGE"
                else 1,
            )
            return normalized

    async def _send_missed_morning_daily_report_on_startup(self, notify_chat_ids: list[int]) -> None:
            if not notify_chat_ids:
                return
            tzinfo = self._resolve_timezone()
            now_local = datetime.now(tzinfo)
            state = self._load_daily_report_scheduler_state()
            if not self._should_send_morning_catchup_on_startup(now_local=now_local, state=state):
                return
            trigger_label = (
                "Báo cáo tự động buổi sáng "
                f"({self.settings.daily_report_hour:02d}:{self.settings.daily_report_minute:02d}) "
                "- gửi bù sau khi bot khởi động lại"
            )
            report_payload: dict[str, Any] | None = None
            all_delivered = True
            run_date = now_local.date()
            claim = self._try_claim_local_schedule(task="daily-report", slot="morning", run_date=run_date)
            if not claim:
                return
            for chat_id in notify_chat_ids:
                report_payload = await self._send_daily_report(
                    chat_id=chat_id,
                    trigger_label=trigger_label,
                    report_date=self._resolve_daily_report_date_for_slot("morning"),
                    notify_success=True,
                    report_payload=report_payload,
                    include_recent_rollups=self._is_report_group_chat(chat_id),
                    include_task_summary=False,
                )
                all_delivered = all_delivered and bool(self._last_daily_report_send_ok)
            if all_delivered:
                self._mark_daily_report_slot_sent("morning", run_date=run_date)
                await self._mark_cloud_schedule_completed(
                    task="daily-report",
                    slot="morning",
                    run_date=run_date,
                )
                self.logger.info("Da gui bu daily report buoi sang sau khoi dong lai bot.")
            else:
                self._release_local_schedule_claim(claim)
                self._mark_daily_report_slot_failed("morning", run_date=run_date)
                self.logger.warning("Gui bu daily report buoi sang that bai, se thu lai sau khi khoi dong bot.")

    def _should_send_morning_catchup_on_startup(self, *, now_local: datetime, state: dict[str, Any]) -> bool:
            run_date_key = now_local.date().isoformat()
            if str(state.get("morning_last_sent_run_date", "")).strip() == run_date_key:
                return False
            morning_at = now_local.replace(
                hour=self.settings.daily_report_hour,
                minute=self.settings.daily_report_minute,
                second=0,
                microsecond=0,
            )
            return now_local >= morning_at

    async def _retry_pending_daily_reports_on_startup(self, notify_chat_ids: list[int]) -> None:
            if not notify_chat_ids:
                return
            now_local = datetime.now(self._resolve_timezone())
            state = self._load_daily_report_scheduler_state()
            for slot in ("morning", "evening"):
                pending_key = f"{slot}_pending_run_date"
                pending_run_date = self._parse_iso_date(str(state.get(pending_key, "")).strip())
                if pending_run_date is None:
                    continue
                if not self._is_daily_report_slot_due_for_retry(slot=slot, run_date=pending_run_date, now_local=now_local):
                    continue

                trigger_label = (
                    f"Báo cáo tự động {'buổi sáng' if slot == 'morning' else 'buổi tối'} "
                    "- gửi lại do lần trước lỗi kết nối"
                )
                report_payload: dict[str, Any] | None = None
                all_delivered = True
                claim = self._try_claim_local_schedule(task="daily-report", slot=slot, run_date=pending_run_date)
                if not claim:
                    continue
                for chat_id in notify_chat_ids:
                    report_payload = await self._send_daily_report(
                        chat_id=chat_id,
                        trigger_label=trigger_label,
                        report_date=self._resolve_daily_report_date_for_slot_and_run_date(slot, pending_run_date),
                        notify_success=True,
                        report_payload=report_payload,
                        include_recent_rollups=(slot == "morning" and self._is_report_group_chat(chat_id)),
                        include_task_summary=(slot == "evening"),
                    )
                    all_delivered = all_delivered and bool(self._last_daily_report_send_ok)
                if all_delivered:
                    self._mark_daily_report_slot_sent(slot, run_date=pending_run_date)
                    await self._mark_cloud_schedule_completed(
                        task="daily-report",
                        slot=slot,
                        run_date=pending_run_date,
                    )
                    self.logger.info("Da gui lai daily report slot %s bi pending (%s).", slot, pending_run_date.isoformat())
                else:
                    self._release_local_schedule_claim(claim)
                    self._mark_daily_report_slot_failed(slot, run_date=pending_run_date)
                    self.logger.warning("Gui lai daily report slot %s pending that bai.", slot)

    def _load_objective_config(self) -> dict[str, Any]:
        return self._load_campaign_objective()

    def _load_campaign_objective(self) -> dict[str, Any]:
        """Load objective settings and merge the optional profile SKU snapshot."""

        objective = load_json(self.settings.objective_config_path)
        catalog_path = getattr(self.settings, "sku_catalog_path", None)
        if catalog_path is not None and Path(catalog_path).exists():
            objective["sku_catalog"] = load_json(catalog_path)
        return objective

    def _configured_sku_codes(self) -> tuple[str, ...]:
        return resolve_sku_catalog(self._load_campaign_objective())

    async def run(self) -> None:
        bot = Bot(token=self.settings.telegram_bot_token)
        self._bot = bot
        try:
            me = await bot.get_me()
            self._bot_username = str(getattr(me, "username", "") or "").strip().lstrip("@").lower()
        except Exception:  # noqa: BLE001
            self.logger.warning("Khong lay duoc username bot luc khoi dong, bo qua enforce tag trong group.")
        dispatcher = Dispatcher()
        dispatcher.include_router(self.router)
        self.logger.info("Telegram bot đang chạy polling...")
        await self._resume_recoverable_web_forms()
        if self.settings.token_healthcheck_enabled:
            self._token_monitor_task = asyncio.create_task(
                self._token_health_monitor_loop(),
                name="token_health_monitor",
            )
        if self.settings.daily_report_enabled:
            self._daily_report_task = asyncio.create_task(
                self._daily_report_monitor_loop(),
                name="daily_report_monitor",
            )
        if self.settings.reconcile_cod_enabled and self.settings.reconcile_cod_auto_enabled and self.reconcile:
            self._reconcile_cod_task = asyncio.create_task(
                self._reconcile_cod_monitor_loop(),
                name="reconcile_cod_monitor",
            )
        if self.settings.pancake_td_sync_enabled and self.pancake_td_sync:
            self._pancake_td_sync_task = asyncio.create_task(
                self._pancake_td_sync_monitor_loop(),
                name="pancake_td_sync_monitor",
            )
        try:
            await dispatcher.start_polling(bot)
        finally:
            if self._token_monitor_task:
                self._token_monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._token_monitor_task
            if self._daily_report_task:
                self._daily_report_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._daily_report_task
            if self._reconcile_cod_task:
                self._reconcile_cod_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reconcile_cod_task
            if self._pancake_td_sync_task:
                self._pancake_td_sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._pancake_td_sync_task

    async def _resume_recoverable_web_forms(self) -> None:
        """Resume forms stranded by the old missing-SKU validation."""

        if not self.settings.new_web_ads_enabled:
            return
        finder = getattr(self.storage, "find_recoverable_web_media_forms", None)
        if not callable(finder):
            return
        pending_forms = await asyncio.to_thread(
            finder,
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        for pending in pending_forms:
            request_id = str(pending.get("request_id", "")).strip()
            chat_id = int(pending.get("chat_id", 0) or 0)
            user_id = int(pending.get("user_id", 0) or 0)
            if not request_id or not chat_id or not user_id:
                continue
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"stage": "recovery_in_progress"},
            )
            try:
                await self._process_web_ads_form(
                    _WebRecoveryMessage(self._bot, chat_id, user_id),
                    {
                        "caption": str(pending.get("caption", "")),
                        "headline": str(pending.get("headline", "")),
                        "cta_label": str(pending.get("cta_label", "")),
                        "cta_type": str(pending.get("cta_type", "")),
                        "destination_url": str(pending.get("destination_url", "")),
                        "budget_daily_vnd": str(pending.get("budget_daily_vnd", "0")),
                        "sku_override": "",
                    },
                )
                self.logger.info("Da khoi phuc phien Web dang cho %s", request_id)
            except Exception:  # noqa: BLE001
                self.logger.exception("Khong khoi phuc duoc phien Web dang cho %s", request_id)
                await asyncio.to_thread(
                    self.storage.update_pending_request,
                    request_id,
                    {"stage": "awaiting_budget"},
                )

    async def handle_start_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return

        await message.answer(
            "Em đã sẵn sàng.\n"
            "Lên campaign mới: <link> ngân sách 300000 lên mới\n"
            "Lên campaign cũ: <link> JCV140 lên cũ\n"
            "Lên campaign cũ theo hint: <link> lên cũ camp video\n"
            "Trước khi tạo bất kỳ camp nào, em sẽ hỏi media này của ai để đặt tên quảng cáo\n"
            "Lên camp Web: gửi ảnh/video kèm caption, em sẽ hỏi lần lượt tiêu đề/nút/URL/ngân sách, "
            "media của ai; sau đó bắt buộc chọn chiến lược vòng đời khách hàng bằng nút Telegram\n"
            "Hoặc gửi media Google Drive: media: <link Drive> rồi gửi caption nếu cần\n"
            "Lên camp media trực tiếp: gửi ảnh/video hoặc link Drive; bot sẽ hỏi chọn Web Sales hay Messenger Purchase "
            "rồi mới hỏi các thông tin tiếp theo\n"
            "Khi cần kiểm tra token ngay: /token\n"
            "Khi cần xem báo cáo ngày: /report hoặc /report YYYY-MM-DD\n"
            "Khi cần phân tích media ADS2: /media_perf VXV011 7d hoặc phân tích media 7 ngày\n"
            "Khi cần đối soát COD: /reconcile cod hoặc /reconcile cod YYYY-MM-DD\n"
            "Khi cần lên đơn Thái Dương thủ công: lên đơn hôm nay, lên đơn JCT310 hoặc lên đơn 121"
        )

    async def handle_ads_command(self, message: Message) -> None:
        if self._is_report_group_chat(message.chat.id if message.chat else None):
            return
        await self._process_ads_input(message, message.text or "")

    async def handle_media_message(self, message: Message) -> None:
        if await self._has_pending_media_intake_mode(message):
            await self._handle_media_intake_message(message)
            return
        if await self._has_pending_messenger_media_mode(message):
            await self._handle_messenger_media_message(message)
            return
        await self._handle_media_intake_message(message, auto_start=True)

    async def _has_pending_media_intake_mode(self, message: Message) -> bool:
        if not message.chat:
            return False
        pending = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="media_intake_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        return bool(pending)

    async def _handle_media_intake_message(self, message: Message, auto_start: bool = False) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ media.")
            return

        media_kind = ""
        media_file_id = ""
        media_unique_id = ""
        media_filename = ""
        media_size_bytes = 0
        if message.photo:
            photo = message.photo[-1]
            media_kind = "image"
            media_file_id = str(photo.file_id).strip()
            media_unique_id = str(photo.file_unique_id).strip()
            media_filename = "creative.jpg"
            media_size_bytes = int(getattr(photo, "file_size", 0) or 0)
        elif message.video:
            video = message.video
            media_kind = "video"
            media_file_id = str(video.file_id).strip()
            media_unique_id = str(video.file_unique_id).strip()
            media_filename = str(video.file_name or "creative.mp4").strip() or "creative.mp4"
            media_size_bytes = int(getattr(video, "file_size", 0) or 0)
        if not media_file_id:
            await message.answer("Không tìm thấy ảnh/video hợp lệ trong tin nhắn.")
            return

        download_limit = self._web_media_download_limit_bytes()
        if media_size_bytes > download_limit:
            await message.answer(self._web_media_size_error(download_limit))
            return
        mode_request = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="media_intake_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not mode_request and not auto_start:
            await message.answer("Phiên nhận media đã hết hạn. Anh gửi lại `lên mới media` giúp em.")
            return
        if mode_request:
            await asyncio.to_thread(
                self.storage.delete_pending_request,
                str(mode_request.get("request_id", "")),
            )
        caption = str(getattr(message, "caption", "") or "").strip()
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_file_id": media_file_id,
                "media_unique_id": media_unique_id,
                "media_kind": media_kind,
                "media_filename": media_filename,
                "media_size_bytes": media_size_bytes,
                "caption": caption,
                "stage": "awaiting_flow",
            },
            "media_intake",
        )
        media_label = "ảnh" if media_kind == "image" else "video"
        await self._send_media_flow_prompt(message, request_id, media_label)

    async def _send_media_flow_prompt(
        self,
        message: Message,
        request_id: str,
        media_label: str,
    ) -> None:
        """Retry the chooser so a transient Telegram send failure does not strand the job."""

        text = f"Đã nhận {media_label} ({request_id}). Anh chọn luồng muốn lên:"
        reply_markup = self.approval.media_flow_keyboard(request_id)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                await message.answer(text, reply_markup=reply_markup)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.logger.warning(
                    "Gui media flow prompt that bai lan %s/3 cho request %s: %s",
                    attempt,
                    request_id,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt)
        if last_error:
            raise last_error

    async def _has_pending_messenger_media_mode(self, message: Message) -> bool:
        if not message.chat:
            return False
        pending = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="messenger_media_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        return bool(pending)

    async def _handle_messenger_media_message(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not self.settings.new_messenger_media_ads_enabled:
            await message.answer(
                "Luồng camp Messenger media đang tắt. Anh bật "
                "NEW_MESSENGER_MEDIA_ADS_ENABLED=1 rồi khởi động lại bot."
            )
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ media.")
            return

        media_kind = ""
        media_file_id = ""
        media_unique_id = ""
        media_filename = ""
        media_size_bytes = 0
        if message.photo:
            photo = message.photo[-1]
            media_kind = "image"
            media_file_id = str(photo.file_id).strip()
            media_unique_id = str(photo.file_unique_id).strip()
            media_filename = "creative.jpg"
            media_size_bytes = int(getattr(photo, "file_size", 0) or 0)
        elif message.video:
            video = message.video
            media_kind = "video"
            media_file_id = str(video.file_id).strip()
            media_unique_id = str(video.file_unique_id).strip()
            media_filename = str(video.file_name or "creative.mp4").strip() or "creative.mp4"
            media_size_bytes = int(getattr(video, "file_size", 0) or 0)
        if not media_file_id:
            await message.answer("Không tìm thấy ảnh/video hợp lệ trong tin nhắn.")
            return

        download_limit = self._web_media_download_limit_bytes()
        if media_size_bytes > download_limit:
            await message.answer(self._web_media_size_error(download_limit))
            return

        previous = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="messenger_media_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if previous:
            await asyncio.to_thread(self.storage.delete_pending_request, str(previous.get("request_id", "")))
        caption = str(getattr(message, "caption", "") or "").strip()
        stage = "awaiting_headline" if caption else "awaiting_caption"
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_file_id": media_file_id,
                "media_unique_id": media_unique_id,
                "media_kind": media_kind,
                "media_filename": media_filename,
                "media_size_bytes": media_size_bytes,
                "caption": caption,
                "stage": stage,
            },
            "messenger_media",
        )
        media_label = "ảnh" if media_kind == "image" else "video"
        if stage == "awaiting_caption":
            await message.answer(
                f"Đã nhận {media_label} Messenger ({request_id}) nhưng chưa có caption.\n"
                "Anh gửi caption trước, rồi em sẽ hỏi tiếp tiêu đề."
            )
        else:
            await message.answer(
                f"Đã nhận {media_label} kèm caption ({request_id}).\n"
                "Anh gửi tiêu đề muốn hiển thị trong quảng cáo Messenger."
            )

    async def handle_web_media_message(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not self.settings.new_web_ads_enabled:
            await message.answer("Luồng camp Web đang tắt. Anh bật NEW_WEB_ADS_ENABLED=1 rồi khởi động lại bot.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ media.")
            return

        media_kind = ""
        media_file_id = ""
        media_unique_id = ""
        media_filename = ""
        media_size_bytes = 0
        if message.photo:
            photo = message.photo[-1]
            media_kind = "image"
            media_file_id = str(photo.file_id).strip()
            media_unique_id = str(photo.file_unique_id).strip()
            media_filename = "creative.jpg"
            media_size_bytes = int(getattr(photo, "file_size", 0) or 0)
        elif message.video:
            video = message.video
            media_kind = "video"
            media_file_id = str(video.file_id).strip()
            media_unique_id = str(video.file_unique_id).strip()
            media_filename = str(video.file_name or "creative.mp4").strip() or "creative.mp4"
            media_size_bytes = int(getattr(video, "file_size", 0) or 0)

        if not media_file_id:
            await message.answer("Không tìm thấy ảnh/video hợp lệ trong tin nhắn.")
            return

        download_limit = self._web_media_download_limit_bytes()
        if media_size_bytes > download_limit:
            await message.answer(self._web_media_size_error(download_limit))
            return

        caption = str(getattr(message, "caption", "") or "").strip()
        stage = "awaiting_headline" if caption else "awaiting_caption"

        previous = await asyncio.to_thread(
            self.storage.find_latest_pending_web_media,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if previous:
            await asyncio.to_thread(self.storage.delete_pending_request, str(previous.get("request_id", "")))

        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_file_id": media_file_id,
                "media_unique_id": media_unique_id,
                "media_kind": media_kind,
                "media_filename": media_filename,
                "media_size_bytes": media_size_bytes,
                "caption": caption,
                "stage": stage,
            },
            "web_media",
        )
        media_label = "ảnh" if media_kind == "image" else "video"
        if stage == "awaiting_caption":
            await message.answer(
                f"Đã nhận {media_label} ({request_id}) nhưng chưa có caption.\n"
                "Anh gửi caption trước, rồi em sẽ hỏi tiếp tiêu đề."
            )
        else:
            await message.answer(
                f"Đã nhận {media_label} kèm caption ({request_id}).\n"
                "Anh gửi tiêu đề muốn hiển thị trong quảng cáo."
            )

    async def _process_web_media_url_message(self, message: Message, payload: dict[str, str]) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ link media.")
            return

        media_url = str(payload.get("media_url", "")).strip()
        caption = str(payload.get("caption", "")).strip()
        if not media_url:
            await message.answer("Em chưa nhận được link media Google Drive.")
            return

        previous = await asyncio.to_thread(
            self.storage.find_latest_pending_web_media,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if previous:
            await asyncio.to_thread(self.storage.delete_pending_request, str(previous.get("request_id", "")))

        stage = "awaiting_headline" if caption else "awaiting_caption"
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_source": "url",
                "media_url": media_url,
                "media_file_id": "",
                "media_unique_id": "",
                "media_kind": "auto",
                "media_filename": "creative.bin",
                "media_size_bytes": 0,
                "caption": caption,
                "stage": stage,
            },
            "web_media",
        )
        if stage == "awaiting_caption":
            await message.answer(
                f"Đã nhận link media Google Drive ({request_id}).\n"
                "Anh gửi caption, rồi em sẽ hỏi tiếp tiêu đề.\n"
                "Lưu ý: file Drive phải bật Anyone with the link / Viewer."
            )
        else:
            await message.answer(
                f"Đã nhận link media kèm caption ({request_id}).\n"
                "Anh gửi tiêu đề muốn hiển thị trong quảng cáo.\n"
                "Lưu ý: file Drive phải bật Anyone with the link / Viewer."
            )

    async def handle_token_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer("Đang kiểm tra token, anh chờ em vài giây...")
        await self._send_token_health_report(
            chat_id=message.chat.id,
            trigger_label="Kiểm tra thủ công",
            notify_success=True,
        )

    async def handle_report_command(self, message: Message) -> None:
        chat_id = message.chat.id if message.chat else None
        raw_text = message.text or ""
        if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(raw_text):
            return
        if not self._can_use_report(
            user_id=message.from_user.id if message.from_user else None,
            chat_id=chat_id,
        ):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        try:
            report_date = parse_report_date_argument(self._strip_bot_mention_tokens(raw_text))
        except CommandParseError as exc:
            await message.answer(str(exc))
            return
        await message.answer("Đang tổng hợp báo cáo, anh chờ em vài giây...")
        await self._send_daily_report_to_target_chats(
            target_chat_ids=self._resolve_daily_report_target_chat_ids(message.chat.id),
            trigger_label="Báo cáo thủ công",
            report_date=report_date,
            notify_success=True,
        )

    async def handle_media_performance_command(self, message: Message) -> None:
        chat_id = message.chat.id if message.chat else None
        raw_text = message.text or ""
        if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(raw_text):
            return
        if not self._can_use_media_performance(
            user_id=message.from_user.id if message.from_user else None,
            chat_id=chat_id,
        ):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not self.settings.media_analytics_enabled or not self.media_performance:
            await message.answer("Tool phân tích media đang tắt. Anh bật MEDIA_ANALYTICS_ENABLED=1 rồi chạy lại.")
            return
        try:
            _, command = try_parse_media_performance_command(
                self._strip_bot_mention_tokens(raw_text),
                self.settings.app_timezone,
            )
        except CommandParseError as exc:
            await message.answer(str(exc))
            return
        if command is None:
            await message.answer("Anh dùng: /media_perf VXV011 7d hoặc phân tích media 7 ngày.")
            return
        await message.answer("Đang phân tích media theo mã VXV, anh chờ em vài giây...")
        await self._send_media_performance_report(
            chat_id=message.chat.id,
            command=command,
            trigger_label="Phân tích media thủ công",
        )

    async def handle_reconcile_command(self, message: Message) -> None:
        chat_id = message.chat.id if message.chat else None
        raw_text = message.text or ""
        if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(raw_text):
            return
        if not self._can_use_reconcile(
            user_id=message.from_user.id if message.from_user else None,
            chat_id=chat_id,
        ):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not self.settings.reconcile_cod_enabled or not self.reconcile:
            await message.answer("Luồng đối soát COD đang tắt. Anh bật RECONCILE_COD_ENABLED=1 rồi chạy lại.")
            return
        try:
            settlement_date = parse_reconcile_cod_date_argument(
                self._strip_bot_mention_tokens(raw_text),
                self.settings.app_timezone,
            )
        except CommandParseError as exc:
            await message.answer(str(exc))
            return
        await message.answer("Đang chạy đối soát COD, anh chờ em vài giây...")
        await self._send_reconcile_cod_report(
            chat_id=message.chat.id,
            trigger_label="Đối soát COD thủ công",
            settlement_date=settlement_date,
            notify_success=True,
            allow_update_prompt=True,
            allow_sheet_sync=True,
        )

    async def handle_new_chat_members(self, message: Message) -> None:
        chat_id = message.chat.id if message.chat else None
        if not self._is_report_group_chat(chat_id):
            return
        members = list(message.new_chat_members or [])
        if not members:
            return
        names: list[str] = []
        for member in members:
            if bool(getattr(member, "is_bot", False)):
                continue
            full_name = str(getattr(member, "full_name", "")).strip()
            username = str(getattr(member, "username", "")).strip()
            if username:
                names.append(f"@{username}")
            elif full_name:
                names.append(full_name)
        if not names:
            return
        if len(names) == 1:
            text = f"Chào mừng {names[0]} vào nhóm ạ."
        else:
            text = "Chào mừng " + ", ".join(names) + " vào nhóm ạ."
        await message.answer(text)

    async def handle_text_message(self, message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        chat_id = message.chat.id if message.chat else None
        parse_text = self._strip_bot_mention_tokens(text)
        try:
            if self._is_media_intake_start(parse_text):
                if not self._is_authorized(message.from_user.id if message.from_user else None):
                    await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                    return
                if not message.chat:
                    await message.answer("Không xác định được cuộc trò chuyện để mở luồng media.")
                    return
                old_request = await asyncio.to_thread(
                    self.storage.find_latest_pending_request_by_type,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id if message.from_user else 0,
                    request_type="media_intake_mode",
                    ttl_minutes=self.settings.web_pending_ttl_minutes,
                )
                if old_request:
                    await asyncio.to_thread(
                        self.storage.delete_pending_request,
                        str(old_request.get("request_id", "")),
                    )
                request_id = await asyncio.to_thread(
                    self.storage.create_pending_request,
                    {
                        "chat_id": message.chat.id,
                        "user_id": message.from_user.id if message.from_user else 0,
                        "stage": "awaiting_media",
                    },
                    "media_intake_mode",
                )
                await message.answer(
                    f"Đã mở phiên nhận media ({request_id}). Anh gửi một ảnh/video hoặc link Google Drive; "
                    "sau đó em sẽ hỏi anh muốn lên Web Sales hay Messenger Purchase."
                )
                return
            if self._is_messenger_media_start(parse_text):
                if not self._is_authorized(message.from_user.id if message.from_user else None):
                    await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                    return
                if not self.settings.new_messenger_media_ads_enabled:
                    await message.answer(
                        "Luồng camp Messenger media đang tắt. Anh bật "
                        "NEW_MESSENGER_MEDIA_ADS_ENABLED=1 rồi khởi động lại bot."
                    )
                    return
                if not message.chat:
                    await message.answer("Không xác định được cuộc trò chuyện để mở luồng Messenger media.")
                    return
                old_request = await asyncio.to_thread(
                    self.storage.find_latest_pending_request_by_type,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id if message.from_user else 0,
                    request_type="messenger_media_mode",
                    ttl_minutes=self.settings.web_pending_ttl_minutes,
                )
                if old_request:
                    await asyncio.to_thread(
                        self.storage.delete_pending_request,
                        str(old_request.get("request_id", "")),
                    )
                request_id = await asyncio.to_thread(
                    self.storage.create_pending_request,
                    {
                        "chat_id": message.chat.id,
                        "user_id": message.from_user.id if message.from_user else 0,
                        "stage": "awaiting_media",
                    },
                    "messenger_media_mode",
                )
                await message.answer(
                    f"Đã mở luồng camp Messenger media ({request_id}).\n"
                    "Anh gửi một ảnh/video hoặc link Google Drive. Nếu gửi media không kèm caption, em sẽ hỏi caption tiếp theo."
                )
                return
            if await self._process_media_owner_step(message, parse_text):
                return
            is_media_url, media_url_payload = try_parse_web_media_url_message(parse_text)
            if is_media_url:
                if await self._has_pending_media_intake_mode(message):
                    await self._process_media_intake_url_message(message, media_url_payload or {})
                    return
                if self.settings.new_messenger_media_ads_enabled and await self._has_pending_messenger_media_mode(message):
                    await self._process_messenger_media_url_message(message, media_url_payload or {})
                    return
                await self._process_media_intake_url_message(
                    message,
                    media_url_payload or {},
                    allow_without_mode=True,
                )
                return
            if self.settings.new_messenger_media_ads_enabled:
                if await self._process_messenger_media_conversation_step(message, parse_text):
                    return
            if self.settings.new_web_ads_enabled:
                web_cta = load_json(self.settings.web_cta_config_path)
                is_web_ads_form, web_ads_form = try_parse_web_ads_form(parse_text, web_cta)
                if is_web_ads_form:
                    await self._process_web_ads_form(message, web_ads_form or {})
                    return
                if await self._process_web_conversation_step(message, parse_text, web_cta):
                    return
            is_reconcile_command, reconcile_date = try_parse_reconcile_cod_command(
                parse_text,
                self.settings.app_timezone,
            )
            if is_reconcile_command:
                if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(text):
                    return
                if not self._can_use_reconcile(
                    user_id=message.from_user.id if message.from_user else None,
                    chat_id=message.chat.id if message.chat else None,
                ):
                    await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                    return
                if not self.settings.reconcile_cod_enabled or not self.reconcile:
                    await message.answer("Luồng đối soát COD đang tắt. Anh bật RECONCILE_COD_ENABLED=1 rồi chạy lại.")
                    return
                await message.answer("Đang chạy đối soát COD, anh chờ em vài giây...")
                await self._send_reconcile_cod_report(
                    chat_id=message.chat.id,
                    trigger_label="Đối soát COD thủ công",
                    settlement_date=reconcile_date,
                    notify_success=True,
                    allow_update_prompt=True,
                    allow_sheet_sync=True,
                )
                return

            is_report_command, report_date = try_parse_report_command(
                parse_text,
                self.settings.app_timezone,
            )
            is_media_performance_command, media_performance_command = try_parse_media_performance_command(
                parse_text,
                self.settings.app_timezone,
            )
            is_pancake_td_sync_command, pancake_td_order_code = try_parse_pancake_td_sync_command(parse_text)
        except CommandParseError as exc:
            if self._is_report_group_chat(chat_id):
                return
            await message.answer(str(exc))
            return
        if is_report_command:
            if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(text):
                return
            if not self._can_use_report(
                user_id=message.from_user.id if message.from_user else None,
                chat_id=chat_id,
            ):
                await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                return
            await message.answer("Đang tổng hợp báo cáo, anh chờ em vài giây...")
            await self._send_daily_report_to_target_chats(
                target_chat_ids=self._resolve_daily_report_target_chat_ids(chat_id),
                trigger_label="Báo cáo thủ công",
                report_date=report_date,
                notify_success=True,
            )
            return
        if is_media_performance_command:
            if self._is_report_group_chat(chat_id) and not self._is_group_message_tagged_for_bot(text):
                return
            if not self._can_use_media_performance(
                user_id=message.from_user.id if message.from_user else None,
                chat_id=chat_id,
            ):
                await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                return
            if not self.settings.media_analytics_enabled or not self.media_performance:
                await message.answer("Tool phân tích media đang tắt. Anh bật MEDIA_ANALYTICS_ENABLED=1 rồi chạy lại.")
                return
            if media_performance_command is None:
                await message.answer("Anh dùng: /media_perf VXV011 7d hoặc phân tích media 7 ngày.")
                return
            await message.answer("Đang phân tích media theo mã VXV, anh chờ em vài giây...")
            await self._send_media_performance_report(
                chat_id=message.chat.id,
                command=media_performance_command,
                trigger_label="Phân tích media thủ công",
            )
            return
        if is_pancake_td_sync_command:
            if not self._is_authorized(message.from_user.id if message.from_user else None):
                await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
                return
            if not self.settings.pancake_td_sync_enabled or not self.pancake_td_sync:
                await message.answer(
                    "Luồng đồng bộ Pancake -> Thái Dương đang tắt. "
                    "Anh bật PANCAKE_TD_SYNC_ENABLED=1 rồi chạy lại."
                )
                return
            if pancake_td_order_code:
                await message.answer(
                    f"Đang lên đơn Thái Dương cho mã {pancake_td_order_code}, anh chờ em vài giây..."
                )
            else:
                await message.answer("Đang lên đơn Thái Dương cho đơn hôm nay, anh chờ em vài giây...")
            await self._send_manual_pancake_td_sync(chat_id=message.chat.id, order_code=pancake_td_order_code)
            return
        if self._is_report_group_chat(chat_id):
            return
        if text.startswith("/"):
            return
        await self._process_ads_input(message, text)

    @staticmethod
    def _is_media_intake_start(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return bool(
            re.fullmatch(
                r"(?:/media_ads|/new_media|/mess|/messenger|lên mới media|len moi media|lên mới mess|len moi mess)",
                normalized,
            )
        )

    @staticmethod
    def _is_messenger_media_start(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return bool(
            re.fullmatch(r"(?:/mess|/messenger|lên mới mess|len moi mess|lên mới messenger|len moi messenger)", normalized)
        )

    async def _process_media_owner_step(self, message: Message, text: str) -> bool:
        """Consume the mandatory media-owner answer before any Meta creation."""

        if not message.chat or text.startswith("/"):
            return False
        finder = getattr(self.storage, "find_latest_pending_request_by_type", None)
        if not callable(finder):
            return False
        pending = await asyncio.to_thread(
            finder,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="media_owner_select",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not pending:
            return False

        try:
            media_owner = normalize_media_owner(text)
        except ValueError as exc:
            await message.answer(str(exc))
            return True

        request_id = str(pending.get("request_id", "")).strip()
        target = str(pending.get("media_owner_target", "")).strip().lower()
        try:
            if target == "web":
                web_input = WebAdsInput(
                    **{
                        **dict(pending.get("web_input", {})),
                        "media_owner": media_owner,
                    }
                )
                await message.answer(
                    f"Đã nhận media của: {media_owner}. Em chuyển sang bước chọn chiến lược vòng đời khách hàng."
                )
                await self._prompt_web_lifecycle_selection(
                    message=message,
                    web_input=web_input,
                    post_fingerprint=str(pending.get("post_fingerprint", "")),
                    version=int(pending.get("version", 1)),
                    media_request_id=str(pending.get("media_request_id", "")),
                    source_request_id=request_id,
                )
                return True

            if target == "messenger_media":
                media_input = MessengerMediaAdsInput(
                    **{
                        **dict(pending.get("messenger_media_input", {})),
                        "media_owner": media_owner,
                    }
                )
                await message.answer(
                    f"Đã nhận media của: {media_owner}. Em chuyển sang bước chọn chiến lược vòng đời khách hàng."
                )
                await self._prompt_messenger_media_lifecycle_selection(
                    message=message,
                    media_input=media_input,
                    post_fingerprint=str(pending.get("post_fingerprint", "")),
                    version=int(pending.get("version", 1)),
                    media_request_id=str(pending.get("media_request_id", "")),
                    source_request_id=request_id,
                )
                return True

            if target != "command":
                raise ValidationError("Phiên hỏi người làm media không hợp lệ. Anh chạy lại lệnh giúp em.")
            command = AdsCommand(
                **{
                    **dict(pending.get("command", {})),
                    "media_owner": media_owner,
                }
            )
            await message.answer(f"Đã nhận media của: {media_owner}. Em đang tạo nháp campaign...")
            if command.use_existing_campaign:
                await self._start_existing_campaign_draft_flow(
                    chat_id=message.chat.id,
                    command=command,
                    post_fingerprint=str(pending.get("post_fingerprint", "")),
                    version=int(pending.get("version", 1)),
                    source_request_id=request_id,
                )
            else:
                await self._create_draft_and_send_review(
                    chat_id=message.chat.id,
                    command=command,
                    post_fingerprint=str(pending.get("post_fingerprint", "")),
                    version=int(pending.get("version", 1)),
                    request_id=request_id,
                )
            return True
        except (TypeError, ValueError, KeyError, ValidationError) as exc:
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            await message.answer(f"Không tiếp tục được yêu cầu tạo camp: {exc}")
            return True

    async def _prompt_media_owner_selection(
        self,
        *,
        message: Message,
        target: str,
        post_fingerprint: str,
        version: int,
        command: AdsCommand | None = None,
        web_input: WebAdsInput | None = None,
        messenger_media_input: MessengerMediaAdsInput | None = None,
        media_request_id: str = "",
        source_request_id: str = "",
    ) -> None:
        """Persist the mandatory owner answer before starting Meta API work."""

        if not message.chat:
            raise ValidationError("Không xác định được cuộc trò chuyện để hỏi người làm media.")
        payload: dict[str, Any] = {
            "chat_id": message.chat.id,
            "user_id": message.from_user.id if message.from_user else 0,
            "media_owner_target": target,
            "post_fingerprint": post_fingerprint,
            "version": version,
            "media_request_id": media_request_id,
        }
        if command is not None:
            payload["command"] = asdict(command)
        if web_input is not None:
            payload["web_input"] = asdict(web_input)
        if messenger_media_input is not None:
            payload["messenger_media_input"] = asdict(messenger_media_input)
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            payload,
            "media_owner_select",
        )
        try:
            await message.answer(
                "Trước khi tạo camp, media này của ai? Anh gửi tên người làm media "
                "để em thêm vào cuối tên quảng cáo."
            )
        except Exception:
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            raise

        if source_request_id:
            await asyncio.to_thread(self.storage.delete_pending_request, source_request_id)

    async def _process_messenger_media_url_message(
        self,
        message: Message,
        payload: dict[str, str],
    ) -> None:
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ link media.")
            return
        media_url = str(payload.get("media_url", "")).strip()
        caption = str(payload.get("caption", "")).strip()
        if not media_url:
            await message.answer("Em chưa nhận được link media Google Drive.")
            return
        mode_request = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="messenger_media_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not mode_request:
            await message.answer("Anh gửi `lên mới mess` trước rồi gửi link media Google Drive giúp em.")
            return
        await asyncio.to_thread(
            self.storage.delete_pending_request,
            str(mode_request.get("request_id", "")),
        )
        stage = "awaiting_headline" if caption else "awaiting_caption"
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_source": "url",
                "media_url": media_url,
                "media_file_id": "",
                "media_unique_id": "",
                "media_kind": "auto",
                "media_filename": "creative.bin",
                "media_size_bytes": 0,
                "caption": caption,
                "stage": stage,
            },
            "messenger_media",
        )
        if stage == "awaiting_caption":
            await message.answer(
                f"Đã nhận link media Messenger ({request_id}). Anh gửi caption, rồi em sẽ hỏi tiếp tiêu đề.\n"
                "File Drive phải bật Anyone with the link / Viewer."
            )
        else:
            await message.answer(
                f"Đã nhận link media Messenger kèm caption ({request_id}).\n"
                "Anh gửi tiêu đề muốn hiển thị trong quảng cáo Messenger."
            )

    async def _process_media_intake_url_message(
        self,
        message: Message,
        payload: dict[str, str],
        allow_without_mode: bool = False,
    ) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để giữ link media.")
            return
        media_url = str(payload.get("media_url", "")).strip()
        caption = str(payload.get("caption", "")).strip()
        if not media_url:
            await message.answer("Em chưa nhận được link media Google Drive.")
            return
        mode_request = await asyncio.to_thread(
            self.storage.find_latest_pending_request_by_type,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="media_intake_mode",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not mode_request and not allow_without_mode:
            await message.answer("Phiên nhận media đã hết hạn. Anh gửi lại `lên mới media` giúp em.")
            return
        if mode_request:
            await asyncio.to_thread(
                self.storage.delete_pending_request,
                str(mode_request.get("request_id", "")),
            )
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "media_source": "url",
                "media_url": media_url,
                "media_file_id": "",
                "media_unique_id": "",
                "media_kind": "auto",
                "media_filename": "creative.bin",
                "media_size_bytes": 0,
                "caption": caption,
                "stage": "awaiting_flow",
            },
            "media_intake",
        )
        await self._send_media_flow_prompt(message, request_id, "link media")

    async def _process_messenger_media_conversation_step(self, message: Message, text: str) -> bool:
        if not message.chat or text.startswith("/"):
            return False
        pending = await asyncio.to_thread(
            self.storage.find_latest_pending_media,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            request_type="messenger_media",
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not pending:
            return False

        request_id = str(pending.get("request_id", "")).strip()
        stage = str(pending.get("stage", "awaiting_headline")).strip().lower()
        if stage == "awaiting_caption":
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"caption": text, "stage": "awaiting_headline"},
            )
            await message.answer("Đã nhận caption. Anh gửi tiêu đề muốn hiển thị trong quảng cáo Messenger.")
            return True
        if stage == "awaiting_headline":
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"headline": text, "stage": "awaiting_budget"},
            )
            await message.answer("Đã nhận tiêu đề. Anh gửi ngân sách mỗi ngày bằng VND, ví dụ: 300000.")
            return True
        if stage == "awaiting_budget":
            try:
                budget_daily_vnd = parse_web_budget(text)
            except ValidationError as exc:
                await message.answer(str(exc))
                return True
            updated = await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"budget_daily_vnd": budget_daily_vnd, "stage": "creating"},
            )
            if not updated:
                await message.answer("Phiên tạo camp Messenger đã hết hạn. Anh gửi `lên mới mess` rồi gửi lại media.")
                return True
            await self._process_messenger_media_form(message, updated)
            return True
        return False

    async def _process_messenger_media_form(self, message: Message, pending: dict[str, Any]) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để tạo camp Messenger.")
            return
        await message.answer("Đã nhận đủ media và thông tin Messenger. Em đang kiểm tra trước khi tạo nháp...")
        user_id = message.from_user.id if message.from_user else 0
        try:
            media_source = str(pending.get("media_source", "telegram")).strip().lower() or "telegram"
            media_kind = str(pending.get("media_kind", "image")).strip().lower() or "image"
            media_filename = str(pending.get("media_filename", "creative.jpg")).strip() or "creative.jpg"
            if media_source == "url":
                media_content, media_kind, media_filename = await asyncio.to_thread(
                    self._download_web_media_url,
                    str(pending.get("media_url", "")).strip(),
                )
            else:
                download_limit = self._web_media_download_limit_bytes()
                media_size_bytes = int(pending.get("media_size_bytes", 0) or 0)
                if media_size_bytes > download_limit:
                    raise ValidationError(self._web_media_size_error(download_limit))
                media_content = await self._download_telegram_file_bytes(
                    message,
                    str(pending.get("media_file_id", "")).strip(),
                )
            if len(media_content) > self.settings.web_media_max_bytes:
                raise ValidationError(
                    f"Media vượt giới hạn {self.settings.web_media_max_bytes // (1024 * 1024)} MB."
                )
            self._validate_web_media_content(media_kind, media_content)
            media_sha256 = hashlib.sha256(media_content).hexdigest()
            media_input = MessengerMediaAdsInput(
                media_file_id=str(pending.get("media_file_id", "")),
                media_unique_id=str(pending.get("media_unique_id", "")),
                media_kind=media_kind,
                media_filename=media_filename,
                caption=normalize_web_caption(str(pending.get("caption", ""))),
                headline=str(pending.get("headline", "")).strip(),
                budget_daily_vnd=int(pending.get("budget_daily_vnd", 0) or 0),
                media_sha256=media_sha256,
                chat_id=message.chat.id,
                user_id=user_id,
                media_source=media_source,
                media_url=str(pending.get("media_url", "")),
            )
            if not media_input.caption:
                raise ValidationError("Caption không được để trống.")
            if not media_input.headline:
                raise ValidationError("Tiêu đề không được để trống.")
            if media_input.budget_daily_vnd <= 0:
                raise ValidationError("Ngân sách phải lớn hơn 0 VND.")
            objective_config = self._load_campaign_objective()
            sku_prefix = resolve_sku_prefix(objective_config)
            sku_code_text = "_".join(
                extract_sku_codes(
                    media_input.caption,
                    sku_prefix,
                    resolve_sku_catalog(objective_config),
                )
            ) or "ALL"
            post_fingerprint = fingerprint(
                "|".join(
                    [
                        "messenger_media",
                        media_input.media_sha256,
                        media_input.caption,
                        media_input.headline,
                        "MESSAGE_PAGE",
                        sku_code_text,
                    ]
                )
            )
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(self.storage.delete_pending_request, str(pending.get("request_id", "")))
            await message.answer(f"Kiểm tra media Messenger thất bại: {exc}")
            return

        dedup_info = await asyncio.to_thread(self.dedup.inspect, post_fingerprint)
        if dedup_info["is_duplicate"]:
            version = int(dedup_info["next_version"])
            duplicate_request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "campaign_variant": "messenger_media",
                    "messenger_media_input": asdict(media_input),
                    "post_fingerprint": post_fingerprint,
                    "version": version,
                    "media_request_id": str(pending.get("request_id", "")),
                },
                "duplicate_confirm",
            )
            await message.answer(
                self._build_messenger_media_duplicate_warning(dedup_info["active_jobs"], version),
                reply_markup=self.approval.duplicate_keyboard(duplicate_request_id, version),
            )
            return

        version = int(dedup_info["next_version"])
        await self._prompt_media_owner_selection(
            message=message,
            target="messenger_media",
            post_fingerprint=post_fingerprint,
            version=version,
            messenger_media_input=media_input,
            media_request_id=str(pending.get("request_id", "")),
        )

    async def _process_web_conversation_step(
        self,
        message: Message,
        text: str,
        cta_config: dict,
    ) -> bool:
        """Advance the media -> headline -> CTA -> URL -> budget conversation."""

        if not message.chat or text.startswith("/"):
            return False
        pending = await asyncio.to_thread(
            self.storage.find_latest_pending_web_media,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else 0,
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not pending:
            return False

        request_id = str(pending.get("request_id", "")).strip()
        stage = str(pending.get("stage", "awaiting_headline")).strip().lower()
        if stage == "awaiting_caption":
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"caption": text, "stage": "awaiting_headline"},
            )
            await message.answer("Đã nhận caption. Anh gửi tiêu đề muốn hiển thị trong quảng cáo.")
            return True

        if stage == "awaiting_headline":
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"headline": text, "stage": "awaiting_cta"},
            )
            await message.answer(
                "Đã nhận tiêu đề. Anh gửi nút hành động, ví dụ: Mua ngay, Xem thêm hoặc Đăng ký."
            )
            return True

        if stage == "awaiting_cta":
            try:
                cta_label, cta_type = parse_web_cta(text, cta_config)
            except ValidationError as exc:
                await message.answer(str(exc))
                return True
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"cta_label": cta_label, "cta_type": cta_type, "stage": "awaiting_url"},
            )
            await message.answer(
                "Đã nhận nút hành động. Anh gửi URL đích website, có thể là homepage hoặc product page."
            )
            return True

        if stage == "awaiting_url":
            try:
                destination_url = parse_web_url(text)
            except ValidationError as exc:
                await message.answer(str(exc))
                return True
            await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"destination_url": destination_url, "stage": "awaiting_budget"},
            )
            await message.answer("Đã nhận URL. Anh gửi ngân sách mỗi ngày bằng VND, ví dụ: 300000.")
            return True

        if stage == "awaiting_budget":
            try:
                budget_daily_vnd = parse_web_budget(text)
            except ValidationError as exc:
                await message.answer(str(exc))
                return True
            updated = await asyncio.to_thread(
                self.storage.update_pending_request,
                request_id,
                {"budget_daily_vnd": budget_daily_vnd, "stage": "creating"},
            )
            if not updated:
                await message.answer("Phiên tạo camp Web đã hết hạn. Anh gửi lại media kèm caption giúp em.")
                return True

            await self._process_web_ads_form(
                message,
                {
                    "caption": str(updated.get("caption", "")),
                    "headline": str(updated.get("headline", "")),
                    "cta_label": str(updated.get("cta_label", "")),
                    "cta_type": str(updated.get("cta_type", "")),
                    "destination_url": str(updated.get("destination_url", "")),
                    "budget_daily_vnd": str(updated.get("budget_daily_vnd", "0")),
                    "sku_override": "",
                },
            )
            return True

        return False

    async def _process_web_ads_form(self, message: Message, form: dict[str, str]) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not message.chat:
            await message.answer("Không xác định được cuộc trò chuyện để tạo camp Web.")
            return

        user_id = message.from_user.id if message.from_user else 0
        media_request = await asyncio.to_thread(
            self.storage.find_latest_pending_web_media,
            chat_id=message.chat.id,
            user_id=user_id,
            ttl_minutes=self.settings.web_pending_ttl_minutes,
        )
        if not media_request:
            await message.answer("Em chưa nhận được media. Anh gửi 1 ảnh hoặc video trước, rồi gửi mẫu thông tin Web.")
            return

        await message.answer("Đã nhận đủ media và thông tin Web. Em đang kiểm tra trước khi tạo nháp...")

        try:
            media_size_bytes = int(media_request.get("media_size_bytes", 0) or 0)
            media_source = str(media_request.get("media_source", "telegram")).strip().lower() or "telegram"
            media_kind = str(media_request.get("media_kind", "image")).strip().lower() or "image"
            media_filename = str(media_request.get("media_filename", "creative.jpg")).strip() or "creative.jpg"
            if media_source == "url":
                media_content, media_kind, media_filename = await asyncio.to_thread(
                    self._download_web_media_url,
                    str(media_request.get("media_url", "")).strip(),
                )
            else:
                download_limit = self._web_media_download_limit_bytes()
                if media_size_bytes > download_limit:
                    raise ValidationError(self._web_media_size_error(download_limit))
                media_content = await self._download_telegram_file_bytes(
                    message,
                    str(media_request.get("media_file_id", "")).strip(),
                )
            if len(media_content) > self.settings.web_media_max_bytes:
                raise ValidationError(
                    f"Media vượt giới hạn {self.settings.web_media_max_bytes // (1024 * 1024)} MB."
                )
            self._validate_web_media_content(media_kind, media_content)
            media_sha256 = hashlib.sha256(media_content).hexdigest()
            web_input = WebAdsInput(
                media_file_id=str(media_request.get("media_file_id", "")),
                media_unique_id=str(media_request.get("media_unique_id", "")),
                media_kind=media_kind,
                media_filename=media_filename,
                caption=normalize_web_caption(str(form.get("caption", ""))),
                headline=str(form.get("headline", "")).strip(),
                cta_label=str(form.get("cta_label", "")).strip(),
                cta_type=str(form.get("cta_type", "")).strip().upper(),
                destination_url=str(form.get("destination_url", "")).strip(),
                budget_daily_vnd=int(form.get("budget_daily_vnd", "0")),
                sku_override=str(form.get("sku_override", "")).strip(),
                media_sha256=media_sha256,
                chat_id=message.chat.id,
                user_id=user_id,
                media_source=media_source,
                media_url=str(media_request.get("media_url", "")).strip(),
            )
        except (ValidationError, ValueError) as exc:
            await asyncio.to_thread(
                self.storage.update_pending_request,
                str(media_request.get("request_id", "")),
                {"stage": "awaiting_budget"},
            )
            await message.answer(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tai media Web that bai")
            await asyncio.to_thread(
                self.storage.update_pending_request,
                str(media_request.get("request_id", "")),
                {"stage": "awaiting_budget"},
            )
            if media_source == "telegram" and self._is_telegram_file_too_large_error(exc):
                await message.answer(self._web_media_size_error(self._web_media_download_limit_bytes()))
            else:
                await message.answer(f"Tải media Web thất bại: {exc}")
            return

        try:
            objective_config = load_json(self.settings.web_objective_config_path)
            sku_prefix = resolve_sku_prefix(objective_config)
            caption_codes = extract_sku_codes(web_input.caption, sku_prefix)
            override_codes = extract_sku_codes(web_input.sku_override, sku_prefix)
        except (ValueError, KeyError, TypeError) as exc:
            await message.answer(str(exc))
            return

        if caption_codes and override_codes and caption_codes != override_codes:
            request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "campaign_variant": "web",
                    "web_input": asdict(web_input),
                    "caption_sku_codes": caption_codes,
                    "override_sku_codes": override_codes,
                    "media_request_id": str(media_request.get("request_id", "")),
                },
                "web_sku_confirm",
            )
            await message.answer(
                "Mã sản phẩm trong caption và dòng mã dự phòng đang khác nhau.\n"
                f"- Hashtag: {'_'.join(caption_codes)}\n"
                f"- Mã dự phòng: {'_'.join(override_codes)}\n"
                "Anh chọn nguồn mã muốn dùng trước khi em tạo campaign:",
                reply_markup=self.approval.web_sku_confirm_keyboard(request_id),
            )
            return

        effective_sku = "_".join(caption_codes or override_codes) or "ALL"

        post_fingerprint = fingerprint(
            "|".join(
                [
                    "web",
                    web_input.media_sha256,
                    web_input.caption,
                    web_input.headline,
                    web_input.cta_type,
                    web_input.destination_url,
                    effective_sku,
                ]
            )
        )
        dedup_info = await asyncio.to_thread(self.dedup.inspect, post_fingerprint)
        if dedup_info["is_duplicate"]:
            version = int(dedup_info["next_version"])
            request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "campaign_variant": "web",
                    "web_input": asdict(web_input),
                    "post_fingerprint": post_fingerprint,
                    "version": version,
                    "media_request_id": str(media_request.get("request_id", "")),
                },
                "duplicate_confirm",
            )
            await message.answer(
                self._build_web_duplicate_warning(dedup_info["active_jobs"], version),
                reply_markup=self.approval.duplicate_keyboard(request_id=request_id, version=version),
            )
            return

        version = int(dedup_info["next_version"])
        await self._prompt_media_owner_selection(
            message=message,
            target="web",
            post_fingerprint=post_fingerprint,
            version=version,
            web_input=web_input,
            media_request_id=str(media_request.get("request_id", "")),
        )

    async def _prompt_web_lifecycle_selection(
        self,
        *,
        message: Message,
        web_input: WebAdsInput,
        post_fingerprint: str,
        version: int,
        media_request_id: str = "",
        source_request_id: str = "",
    ) -> None:
        """Persist the Web draft request until the user chooses lifecycle strategy."""

        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "campaign_variant": "web",
                "web_input": asdict(web_input),
                "post_fingerprint": post_fingerprint,
                "version": version,
                "media_request_id": media_request_id,
            },
            "web_lifecycle_select",
        )
        try:
            await message.answer(
                "Đã kiểm tra xong thông tin Web. Trước khi tạo camp, anh bắt buộc chọn "
                "chiến lược vòng đời khách hàng:\n"
                "- Thu hút lượt chuyển đổi từ tất cả đối tượng\n"
                "- Chinh phục khách hàng mới",
                reply_markup=self.approval.web_lifecycle_keyboard(request_id),
            )
        except Exception:
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            raise

        # The lifecycle request contains enough data to re-download the media.
        # Remove intermediate records so free text cannot accidentally re-enter
        # the budget step while the user is choosing a strategy.
        for pending_id in (source_request_id, media_request_id):
            if pending_id:
                await asyncio.to_thread(self.storage.delete_pending_request, pending_id)

    async def _download_telegram_file_bytes(self, message: Message, file_id: str) -> bytes:
        bot = getattr(message, "bot", None) or self._bot
        if not bot:
            raise RuntimeError("Bot chưa khởi tạo để tải media Telegram.")
        if not file_id:
            raise ValidationError("Thiếu Telegram file_id của media.")
        destination = io.BytesIO()
        await bot.download(file_id, destination=destination)
        destination.seek(0)
        content = destination.read()
        if not content:
            raise ValidationError("Media tải về từ Telegram đang trống.")
        return content

    def _download_web_media_url(self, media_url: str) -> tuple[bytes, str, str]:
        """Download one public Drive file without allowing arbitrary URL fetching."""

        source_url = str(media_url or "").strip()
        parsed_source = urlparse(source_url)
        source_host = (parsed_source.hostname or "").lower().rstrip(".")
        if parsed_source.scheme.lower() != "https" or source_host not in {
            "drive.google.com",
            "drive.usercontent.google.com",
            "docs.google.com",
            "www.googleapis.com",
        }:
            raise ValidationError("Link media chỉ hỗ trợ Google Drive HTTPS.")

        file_id = self._extract_google_drive_file_id(source_url)
        download_url = source_url
        if file_id:
            download_url = (
                "https://drive.usercontent.google.com/download?"
                f"{urlencode({'id': file_id, 'export': 'download', 'confirm': 't'})}"
            )

        headers = {
            "User-Agent": "FBAdsAutomation/1.0",
            "Accept": "image/*,video/*,application/octet-stream,text/html;q=0.8",
        }
        response_url = download_url
        redirect_count = 0
        confirmation_used = False
        while True:
            response = requests.get(
                response_url,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=(15, 120),
            )
            try:
                if 300 <= int(response.status_code) < 400:
                    location = str(response.headers.get("Location", "")).strip()
                    if not location:
                        raise ValidationError("Link media chuyển hướng nhưng thiếu địa chỉ đích.")
                    next_url = urljoin(response_url, location)
                    next_parsed = urlparse(next_url)
                    next_host = (next_parsed.hostname or "").lower().rstrip(".")
                    if next_parsed.scheme.lower() != "https" or not self._is_allowed_web_media_host(next_host):
                        raise ValidationError("Link media chuyển hướng tới máy chủ không được hỗ trợ.")
                    redirect_count += 1
                    if redirect_count > 5:
                        raise ValidationError("Link media chuyển hướng quá nhiều lần.")
                    response_url = next_url
                    continue

                final_host = (urlparse(response_url).hostname or "").lower().rstrip(".")
                if not self._is_allowed_web_media_host(final_host):
                    raise ValidationError("Link media chuyển hướng tới máy chủ không được hỗ trợ.")
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length", "0") or 0)
                limit = max(1, int(self.settings.web_media_max_bytes or 0))
                if content_length > limit:
                    raise ValidationError(
                        f"Media vượt giới hạn {limit // (1024 * 1024)} MB đã cấu hình cho camp Web."
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise ValidationError(
                            f"Media vượt giới hạn {limit // (1024 * 1024)} MB đã cấu hình cho camp Web."
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise ValidationError("Link media trả về file trống.")

                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/html" in content_type:
                    confirmation_url = self._google_drive_confirmation_url(response_url, content)
                    if confirmation_url and not confirmation_used:
                        response_url = confirmation_url
                        confirmation_used = True
                        continue
                    raise ValidationError(
                        "Google Drive không trả về file media. Anh kiểm tra file đang ở chế độ "
                        "Anyone with the link / Viewer và gửi lại link."
                    )

                media_kind = self._detect_web_media_kind(content, content_type)
                if not media_kind:
                    raise ValidationError("Link không trả về ảnh JPG/PNG/GIF/WEBP hoặc video MP4/WebM hợp lệ.")
                filename = self._media_filename_from_response(response, media_kind)
                return content, media_kind, filename
            finally:
                response.close()

        raise ValidationError("Không tải được file media từ Google Drive.")

    async def _download_web_input_media(
        self,
        message: Message,
        web_input: WebAdsInput,
    ) -> tuple[bytes, WebAdsInput]:
        source = str(web_input.media_source or "telegram").strip().lower() or "telegram"
        if source == "url":
            media_content, media_kind, media_filename = await asyncio.to_thread(
                self._download_web_media_url,
                web_input.media_url,
            )
            return media_content, replace(
                web_input,
                media_kind=media_kind,
                media_filename=media_filename,
            )

        media_content = await self._download_telegram_file_bytes(message, web_input.media_file_id)
        if len(media_content) > self.settings.web_media_max_bytes:
            raise ValidationError(
                f"Media vượt giới hạn {self.settings.web_media_max_bytes // (1024 * 1024)} MB."
            )
        self._validate_web_media_content(web_input.media_kind, media_content)
        return media_content, web_input

    async def _download_messenger_media_input_media(
        self,
        message: Message,
        media_input: MessengerMediaAdsInput,
    ) -> tuple[bytes, MessengerMediaAdsInput]:
        web_input = WebAdsInput(
            media_file_id=media_input.media_file_id,
            media_unique_id=media_input.media_unique_id,
            media_kind=media_input.media_kind,
            media_filename=media_input.media_filename,
            caption=media_input.caption,
            headline=media_input.headline,
            cta_label="Gửi tin nhắn",
            cta_type="MESSAGE_PAGE",
            destination_url="https://facebook.com",
            budget_daily_vnd=media_input.budget_daily_vnd,
            media_source=media_input.media_source,
            media_url=media_input.media_url,
        )
        media_content, updated = await self._download_web_input_media(message, web_input)
        return media_content, replace(
            media_input,
            media_kind=updated.media_kind,
            media_filename=updated.media_filename,
            media_sha256=hashlib.sha256(media_content).hexdigest(),
        )

    @staticmethod
    def _build_web_video_thumbnail_url(media_url: str) -> str:
        file_id = TelegramAdsBot._extract_google_drive_file_id(media_url)
        if not file_id:
            return ""
        return "https://drive.google.com/thumbnail?" + urlencode(
            {"id": file_id, "sz": "w1200"}
        )

    def _download_web_video_thumbnail(self, media_url: str) -> bytes:
        """Download a Drive thumbnail so Meta can use a local image hash."""

        thumbnail_url = self._build_web_video_thumbnail_url(media_url)
        if not thumbnail_url:
            return b""
        response = requests.get(
            thumbnail_url,
            headers={"User-Agent": "FBAdsAutomation/1.0", "Accept": "image/*"},
            timeout=(15, 60),
            allow_redirects=True,
        )
        try:
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type", "")).lower()
            content = bytes(response.content or b"")
            if len(content) > 10 * 1024 * 1024:
                raise ValidationError("Thumbnail video tải về vượt giới hạn 10 MB.")
            if not content or self._detect_web_media_kind(content, content_type) != "image":
                raise ValidationError("Google Drive không trả về thumbnail ảnh hợp lệ cho video.")
            return content
        finally:
            response.close()

    @staticmethod
    def _extract_google_drive_file_id(media_url: str) -> str:
        parsed = urlparse(str(media_url or "").strip())
        path_match = re.search(r"/file/d/([^/]+)", parsed.path or "", flags=re.IGNORECASE)
        if path_match:
            return str(path_match.group(1)).strip()
        query_id = parse_qs(parsed.query or {}).get("id", [""])[0]
        return str(query_id).strip()

    @staticmethod
    def _is_allowed_web_media_host(host: str) -> bool:
        normalized = str(host or "").lower().rstrip(".")
        return normalized in {
            "drive.google.com",
            "drive.usercontent.google.com",
            "docs.google.com",
            "www.googleapis.com",
        } or normalized.endswith(".googleusercontent.com")

    @staticmethod
    def _google_drive_confirmation_url(request_url: str, content: bytes) -> str:
        try:
            html = content[:2 * 1024 * 1024].decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
        match = re.search(r"[?&]confirm=([0-9A-Za-z_-]+)", html)
        if not match:
            return ""
        parsed = urlparse(request_url)
        query = parse_qs(parsed.query or "", keep_blank_values=True)
        query["confirm"] = [match.group(1)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    @staticmethod
    def _media_filename_from_response(response: Any, media_kind: str) -> str:
        disposition = str(response.headers.get("Content-Disposition", ""))
        filename_match = re.search(
            r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)",
            disposition,
            flags=re.IGNORECASE,
        )
        if filename_match:
            filename = str(filename_match.group(1) or filename_match.group(2) or "").strip()
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
            if filename:
                return filename
        return "creative.mp4" if media_kind == "video" else "creative.jpg"

    @staticmethod
    def _detect_web_media_kind(content: bytes, content_type: str = "") -> str:
        if (
            content.startswith(b"\xff\xd8\xff")
            or content.startswith(b"\x89PNG\r\n\x1a\n")
            or content.startswith((b"GIF87a", b"GIF89a"))
            or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
            or str(content_type or "").startswith("image/")
        ):
            return "image"
        if (
            content.startswith(b"\x1a\x45\xdf\xa3")
            or (len(content) >= 12 and content[4:8] == b"ftyp")
            or (content.startswith(b"RIFF") and content[8:12] == b"AVI ")
            or str(content_type or "").startswith("video/")
        ):
            return "video"
        return ""

    @staticmethod
    def _validate_web_media_content(media_kind: str, content: bytes) -> None:
        kind = str(media_kind or "").strip().lower()
        if kind == "auto":
            kind = TelegramAdsBot._detect_web_media_kind(content)
        if kind == "image":
            valid = (
                content.startswith(b"\xff\xd8\xff")
                or content.startswith(b"\x89PNG\r\n\x1a\n")
                or content.startswith((b"GIF87a", b"GIF89a"))
                or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
            )
        elif kind == "video":
            valid = (
                content.startswith(b"\x1a\x45\xdf\xa3")
                or (len(content) >= 12 and content[4:8] == b"ftyp")
                or (content.startswith(b"RIFF") and content[8:12] == b"AVI ")
            )
        else:
            raise ValidationError("Media Web chỉ hỗ trợ một ảnh hoặc một video hợp lệ.")
        if not valid:
            label = "ảnh" if kind == "image" else "video"
            raise ValidationError(f"File media không có định dạng {label} hợp lệ.")

    def _web_media_download_limit_bytes(self) -> int:
        configured_limit = max(1, int(self.settings.web_media_max_bytes or 0))
        return min(configured_limit, _TELEGRAM_BOT_API_MAX_DOWNLOAD_BYTES)

    @staticmethod
    def _web_media_size_error(limit_bytes: int) -> str:
        limit_mb = max(1, int(limit_bytes) // (1024 * 1024))
        return (
            f"Media quá lớn để Telegram Bot tải xuống. Giới hạn hiện tại là {limit_mb} MB.\n"
            "Anh hãy nén video xuống dưới giới hạn này rồi gửi lại; ảnh hoặc video ngắn sẽ được xử lý bình thường."
        )

    @staticmethod
    def _is_telegram_file_too_large_error(exc: Exception) -> bool:
        error_text = str(exc).strip().lower()
        return "file is too big" in error_text or "file_too_big" in error_text

    async def _process_ads_input(self, message: Message, raw_text: str) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return

        try:
            allowed_sku_codes = self._configured_sku_codes()
            command = parse_ads_command(
                raw_text,
                allowed_sku_codes=allowed_sku_codes,
                allow_new_sku_override=bool(allowed_sku_codes),
            )
        except (CommandParseError, ValidationError) as exc:
            await message.answer(str(exc))
            return

        normalized_url = normalize_facebook_url(command.post_url)
        command = AdsCommand(
            post_url=normalized_url,
            budget_daily_vnd=command.budget_daily_vnd,
            use_existing_campaign=command.use_existing_campaign,
            manual_sku_keywords=list(command.manual_sku_keywords),
            existing_campaign_hint=str(command.existing_campaign_hint or ""),
            media_owner=str(command.media_owner or ""),
        )
        post_fingerprint = fingerprint(normalized_url)
        dedup_info = await asyncio.to_thread(self.dedup.inspect, post_fingerprint)

        if dedup_info["is_duplicate"]:
            version = int(dedup_info["next_version"])
            request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "chat_id": message.chat.id if message.chat else 0,
                    "user_id": message.from_user.id if message.from_user else 0,
                    "post_url": command.post_url,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "post_fingerprint": post_fingerprint,
                    "version": version,
                    "use_existing_campaign": command.use_existing_campaign,
                    "manual_sku_keywords": list(command.manual_sku_keywords),
                    "existing_campaign_hint": str(command.existing_campaign_hint or ""),
                    "media_owner": str(command.media_owner or ""),
                },
                "duplicate_confirm",
            )
            warning = self._build_duplicate_warning(
                command.post_url,
                dedup_info["active_jobs"],
                version,
            )
            await message.answer(
                warning,
                reply_markup=self.approval.duplicate_keyboard(request_id=request_id, version=version),
            )
            return

        version = int(dedup_info["next_version"])
        await self._prompt_media_owner_selection(
            message=message,
            target="command",
            command=command,
            post_fingerprint=post_fingerprint,
            version=version,
        )

    async def handle_callback(self, query: CallbackQuery) -> None:
        if not self._is_authorized(query.from_user.id if query.from_user else None):
            await query.answer("Không có quyền.", show_alert=True)
            return

        action = self.approval.parse_callback(query.data)
        if not action:
            await query.answer()
            return

        if action.action == "duplicate_confirm":
            await self._on_duplicate_confirm(query, action.value)
            return
        if action.action == "duplicate_cancel":
            await self._on_duplicate_cancel(query, action.value)
            return
        if action.action in {"web_sku_caption", "web_sku_override"}:
            await self._on_web_sku_confirm(query, action.value, action.action.removeprefix("web_sku_"))
            return
        if action.action == "web_sku_cancel":
            await self._on_web_sku_cancel(query, action.value)
            return
        if action.action in {"web_lifecycle_all", "web_lifecycle_new"}:
            strategy = "ALL_AUDIENCES" if action.action == "web_lifecycle_all" else "NEW_CUSTOMERS_ONLY"
            await self._on_web_lifecycle_select(query, action.value, strategy)
            return
        if action.action == "web_lifecycle_cancel":
            await self._on_web_lifecycle_cancel(query, action.value)
            return
        if action.action in {"media_flow_web", "media_flow_messenger"}:
            target = "web" if action.action == "media_flow_web" else "messenger"
            await self._on_media_flow_select(query, action.value, target)
            return
        if action.action == "media_flow_cancel":
            await self._on_media_flow_cancel(query, action.value)
            return
        if action.action in {"messenger_lifecycle_all", "messenger_lifecycle_new"}:
            strategy = "ALL_AUDIENCES" if action.action == "messenger_lifecycle_all" else "NEW_CUSTOMERS_ONLY"
            await self._on_messenger_media_lifecycle_select(query, action.value, strategy)
            return
        if action.action == "messenger_lifecycle_cancel":
            await self._on_messenger_media_lifecycle_cancel(query, action.value)
            return
        if action.action == "approve":
            await self._on_approve(query, action.value)
            return
        if action.action == "reject":
            await self._on_reject(query, action.value)
            return
        if action.action == "campaign_pick":
            await self._on_campaign_pick(query, action.value, action.index)
            return
        if action.action == "campaign_cancel":
            await self._on_campaign_cancel(query, action.value)
            return
        if action.action == "reconcile_apply":
            await self._on_reconcile_apply(query, action.value)
            return
        if action.action == "reconcile_cancel":
            await self._on_reconcile_cancel(query, action.value)
            return
        if action.action == "reconcile_sheet_apply":
            await self._on_reconcile_sheet_apply(query, action.value)
            return
        if action.action == "reconcile_sheet_cancel":
            await self._on_reconcile_sheet_cancel(query, action.value)
            return

        await query.answer()

    async def _on_duplicate_confirm(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() not in {"", "duplicate_confirm"}:
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return

        if str(request.get("campaign_variant", "")).strip().lower() == "web":
            try:
                web_input = WebAdsInput(**dict(request.get("web_input", {})))
                callback_message = query.message
                if callback_message is None:
                    await query.answer("Không xác định được cuộc trò chuyện.", show_alert=True)
                    return
                media_content, web_input = await self._download_web_input_media(callback_message, web_input)
            except Exception as exc:  # noqa: BLE001
                await query.answer("Không tải lại được media.", show_alert=True)
                if query.message:
                    await query.message.answer(f"Tạo phiên bản Web thất bại vì không tải lại được media: {exc}")
                return
            await query.answer("Đã xác nhận tạo v2. Anh gửi tên người làm media...")
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            await self._prompt_media_owner_selection(
                message=query.message,
                target="web",
                post_fingerprint=str(request["post_fingerprint"]),
                version=int(request["version"]),
                web_input=web_input,
                media_request_id=str(request.get("media_request_id", "")),
                source_request_id=request_id,
            )
            return

        if str(request.get("campaign_variant", "")).strip().lower() == "messenger_media":
            try:
                media_input = MessengerMediaAdsInput(**dict(request.get("messenger_media_input", {})))
                callback_message = query.message
                if callback_message is None:
                    await query.answer("Không xác định được cuộc trò chuyện.", show_alert=True)
                    return
                await self._download_messenger_media_input_media(callback_message, media_input)
            except Exception as exc:  # noqa: BLE001
                await query.answer("Không tải lại được media.", show_alert=True)
                if query.message:
                    await query.message.answer(f"Tạo phiên bản Messenger thất bại vì không tải lại được media: {exc}")
                return
            await query.answer("Đã xác nhận tạo phiên bản Messenger mới. Anh gửi tên người làm media...")
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            await self._prompt_media_owner_selection(
                message=query.message,
                target="messenger_media",
                post_fingerprint=str(request["post_fingerprint"]),
                version=int(request["version"]),
                messenger_media_input=media_input,
                media_request_id=str(request.get("media_request_id", "")),
                source_request_id=request_id,
            )
            return

        command = AdsCommand(
            post_url=str(request["post_url"]),
            budget_daily_vnd=int(request["budget_daily_vnd"]),
            use_existing_campaign=bool(request.get("use_existing_campaign", False)),
            manual_sku_keywords=[str(item) for item in request.get("manual_sku_keywords", [])],
            existing_campaign_hint=str(request.get("existing_campaign_hint", "") or ""),
            media_owner=str(request.get("media_owner", "") or ""),
        )
        post_fingerprint = str(request["post_fingerprint"])
        version = int(request["version"])

        await query.answer("Đã xác nhận tạo v2.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        target_chat_id = query.message.chat.id if query.message else query.from_user.id
        if not command.media_owner:
            await self._prompt_media_owner_selection(
                message=query.message,
                target="command",
                command=command,
                post_fingerprint=post_fingerprint,
                version=version,
                source_request_id=request_id,
            )
            return
        if command.use_existing_campaign:
            await self._start_existing_campaign_draft_flow(
                chat_id=target_chat_id,
                command=command,
                post_fingerprint=post_fingerprint,
                version=version,
                source_request_id=request_id,
            )
            return
        await self._create_draft_and_send_review(
            chat_id=target_chat_id,
            command=command,
            post_fingerprint=post_fingerprint,
            version=version,
            request_id=request_id,
        )

    async def _on_web_sku_confirm(self, query: CallbackQuery, request_id: str, source: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request or str(request.get("request_type", "")).strip() != "web_sku_confirm":
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            return
        try:
            web_input = WebAdsInput(
                **{**dict(request.get("web_input", {})), "sku_source": source}
            )
            callback_message = query.message
            if callback_message is None:
                raise ValidationError("Không xác định được cuộc trò chuyện để tải lại media.")
            media_content, web_input = await self._download_web_input_media(callback_message, web_input)
            objective_config = load_json(self.settings.web_objective_config_path)
            effective_sku = "_".join(
                resolve_web_sku_codes(
                    web_input.caption,
                    web_input.sku_override,
                    objective_config,
                    source,
                )
            )
            if not effective_sku:
                raise ValidationError("Nguồn SKU anh chọn không có mã sản phẩm hợp lệ.")
            post_fingerprint = fingerprint(
                "|".join(
                    [
                        "web",
                        web_input.media_sha256,
                        web_input.caption,
                        web_input.headline,
                        web_input.cta_type,
                        web_input.destination_url,
                        effective_sku,
                    ]
                )
            )
        except Exception as exc:  # noqa: BLE001
            await query.answer("Không xác nhận được SKU.", show_alert=True)
            if query.message:
                await query.message.answer(f"Xác nhận SKU thất bại: {exc}")
            return

        dedup_info = await asyncio.to_thread(self.dedup.inspect, post_fingerprint)
        if dedup_info["is_duplicate"]:
            version = int(dedup_info["next_version"])
            duplicate_request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "campaign_variant": "web",
                    "web_input": asdict(web_input),
                    "post_fingerprint": post_fingerprint,
                    "version": version,
                    "media_request_id": str(request.get("media_request_id", "")),
                },
                "duplicate_confirm",
            )
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            await query.answer("Nội dung đã tồn tại, cần xác nhận tạo phiên bản mới.")
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer(
                    self._build_web_duplicate_warning(dedup_info["active_jobs"], version),
                    reply_markup=self.approval.duplicate_keyboard(duplicate_request_id, version),
                )
            return

        version = int(dedup_info["next_version"])
        await query.answer("Đã xác nhận SKU. Anh gửi tên người làm media...")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        await self._prompt_media_owner_selection(
            message=query.message,
            target="web",
            post_fingerprint=post_fingerprint,
            version=version,
            web_input=web_input,
            media_request_id=str(request.get("media_request_id", "")),
            source_request_id=request_id,
        )

    async def _on_web_sku_cancel(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        if request:
            media_request_id = str(request.get("media_request_id", "")).strip()
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
        await query.answer("Đã hủy xác nhận SKU.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy yêu cầu tạo campaign Web.")

    async def _on_media_flow_select(
        self,
        query: CallbackQuery,
        request_id: str,
        target: str,
    ) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request or str(request.get("request_type", "")).strip() != "media_intake":
            await self._answer_callback_safely(
                query,
                "Yêu cầu media đã hết hạn hoặc đã được xử lý.",
                show_alert=True,
            )
            return
        normalized_target = str(target or "").strip().lower()
        if normalized_target == "web" and not self.settings.new_web_ads_enabled:
            await self._answer_callback_safely(query, "Luồng Web đang tắt.", show_alert=True)
            if query.message:
                await query.message.answer(
                    "Luồng Web đang tắt. Anh bật NEW_WEB_ADS_ENABLED=1 rồi khởi động lại bot, "
                    "hoặc chọn Messenger Purchase nếu luồng đó đã bật."
                )
            return
        if normalized_target == "messenger" and not self.settings.new_messenger_media_ads_enabled:
            await self._answer_callback_safely(query, "Luồng Messenger đang tắt.", show_alert=True)
            if query.message:
                await query.message.answer(
                    "Luồng Messenger media đang tắt. Anh bật NEW_MESSENGER_MEDIA_ADS_ENABLED=1 "
                    "rồi khởi động lại bot, hoặc chọn Web Sales nếu luồng đó đã bật."
                )
            return
        if normalized_target not in {"web", "messenger"}:
            await self._answer_callback_safely(query, "Luồng media không hợp lệ.", show_alert=True)
            return

        next_request_type = "web_media" if normalized_target == "web" else "messenger_media"
        caption = str(request.get("caption", "")).strip()
        stage = "awaiting_headline" if caption else "awaiting_caption"
        updated = await asyncio.to_thread(
            self.storage.update_pending_request,
            request_id,
            {
                "request_type": next_request_type,
                "stage": stage,
            },
        )
        if not updated:
            await self._answer_callback_safely(query, "Phiên media đã hết hạn.", show_alert=True)
            return
        await self._answer_callback_safely(query, "Đã chọn luồng media.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            label = "Web Sales" if normalized_target == "web" else "Messenger Purchase"
            if stage == "awaiting_caption":
                await query.message.answer(
                    f"Đã chọn luồng {label}. Anh gửi caption, rồi em sẽ gửi tiếp câu hỏi tiêu đề."
                )
            else:
                await query.message.answer(
                    f"Đã chọn luồng {label}. Anh gửi tiêu đề muốn hiển thị trong quảng cáo."
                )

    async def _on_media_flow_cancel(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if request and str(request.get("request_type", "")).strip() == "media_intake":
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        await self._answer_callback_safely(query, "Đã hủy nhận media.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy phiên nhận media.")

    async def _on_web_lifecycle_select(
        self,
        query: CallbackQuery,
        request_id: str,
        strategy: str,
    ) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request or str(request.get("request_type", "")).strip() != "web_lifecycle_select":
            await self._answer_callback_safely(
                query,
                "Yêu cầu đã hết hạn hoặc đã được xử lý.",
                show_alert=True,
            )
            return

        try:
            normalized_strategy = normalize_web_lifecycle_strategy(strategy)
            web_input = WebAdsInput(
                **{
                    **dict(request.get("web_input", {})),
                    "customer_lifecycle_strategy": normalized_strategy,
                }
            )
        except (TypeError, ValueError) as exc:
            await self._answer_callback_safely(query, str(exc), show_alert=True)
            return

        if normalized_strategy == "NEW_CUSTOMERS_ONLY":
            await self._answer_callback_safely(
                query,
                "Meta API chưa hỗ trợ chế độ khách hàng mới cho cấu trúc này.",
                show_alert=True,
            )
            if query.message:
                await query.message.answer(
                    "Anh đã chọn 'Chinh phục khách hàng mới'. Meta hiện chưa cung cấp "
                    "trường Customer Lifecycle Strategy này qua Graph API cho camp Web "
                    "đang dùng, nên em chưa tạo camp để tránh chạy sai. Anh chọn "
                    "'Tất cả đối tượng' để bot tạo tự động, hoặc thiết lập lựa chọn này "
                    "trực tiếp trong Ads Manager."
                )
            return

        callback_message = query.message
        if callback_message is None:
            await self._answer_callback_safely(query, "Không xác định được cuộc trò chuyện.", show_alert=True)
            return

        try:
            media_content, web_input = await self._download_web_input_media(callback_message, web_input)
        except Exception as exc:  # noqa: BLE001
            await self._answer_callback_safely(query, "Không tải lại được media.", show_alert=True)
            await callback_message.answer(f"Tạo nháp Web thất bại vì không tải lại được media: {exc}")
            return

        await self._answer_callback_safely(query, "Đã chọn tất cả đối tượng, đang tạo nháp Web...")
        await callback_message.edit_reply_markup(reply_markup=None)
        await self._create_web_draft_and_send_review(
            chat_id=callback_message.chat.id,
            web_input=web_input,
            media_content=media_content,
            post_fingerprint=str(request.get("post_fingerprint", "")),
            version=int(request.get("version", 1)),
            request_id=request_id,
            media_request_id=str(request.get("media_request_id", "")),
            telegram_bot=(getattr(callback_message, "bot", None) or getattr(query, "bot", None)),
        )

    async def _on_web_lifecycle_cancel(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        if request:
            media_request_id = str(request.get("media_request_id", "")).strip()
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
        await self._answer_callback_safely(query, "Đã hủy yêu cầu tạo campaign Web.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy yêu cầu tạo campaign Web.")

    async def _prompt_messenger_media_lifecycle_selection(
        self,
        *,
        message: Message,
        media_input: MessengerMediaAdsInput,
        post_fingerprint: str,
        version: int,
        media_request_id: str = "",
        source_request_id: str = "",
    ) -> None:
        if not message.chat:
            raise ValidationError("Không xác định được cuộc trò chuyện để chọn chiến lược Messenger.")
        request_id = await asyncio.to_thread(
            self.storage.create_pending_request,
            {
                "chat_id": message.chat.id,
                "user_id": message.from_user.id if message.from_user else 0,
                "campaign_variant": "messenger_media",
                "messenger_media_input": asdict(media_input),
                "post_fingerprint": post_fingerprint,
                "version": version,
                "media_request_id": media_request_id,
            },
            "messenger_media_lifecycle_select",
        )
        await message.answer(
            "Anh chọn chiến lược vòng đời khách hàng cho camp Messenger:",
            reply_markup=self.approval.messenger_lifecycle_keyboard(request_id),
        )
        if source_request_id:
            await asyncio.to_thread(self.storage.delete_pending_request, source_request_id)

    async def _on_messenger_media_lifecycle_select(
        self,
        query: CallbackQuery,
        request_id: str,
        strategy: str,
    ) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request or str(request.get("request_type", "")).strip() != "messenger_media_lifecycle_select":
            await self._answer_callback_safely(
                query,
                "Yêu cầu đã hết hạn hoặc đã được xử lý.",
                show_alert=True,
            )
            return
        try:
            normalized_strategy = normalize_web_lifecycle_strategy(strategy)
            media_input = MessengerMediaAdsInput(
                **{
                    **dict(request.get("messenger_media_input", {})),
                    "customer_lifecycle_strategy": normalized_strategy,
                }
            )
        except (TypeError, ValueError) as exc:
            await self._answer_callback_safely(query, str(exc), show_alert=True)
            return

        if normalized_strategy == "NEW_CUSTOMERS_ONLY":
            await self._answer_callback_safely(
                query,
                "Meta API chưa hỗ trợ chế độ khách hàng mới cho camp này.",
                show_alert=True,
            )
            if query.message:
                await query.message.answer(
                    "Anh đã chọn 'Chinh phục khách hàng mới'. Meta hiện chưa cho truyền lựa chọn này "
                    "qua Graph API cho camp Messenger media, nên em chưa tạo object nào. "
                    "Anh chọn 'Tất cả đối tượng' để tạo tự động hoặc thiết lập trực tiếp trong Ads Manager."
                )
            return

        callback_message = query.message
        if callback_message is None:
            await self._answer_callback_safely(query, "Không xác định được cuộc trò chuyện.", show_alert=True)
            return
        try:
            media_content, media_input = await self._download_messenger_media_input_media(
                callback_message,
                media_input,
            )
        except Exception as exc:  # noqa: BLE001
            await self._answer_callback_safely(query, "Không tải lại được media.", show_alert=True)
            await callback_message.answer(f"Tạo nháp Messenger thất bại vì không tải lại được media: {exc}")
            return

        await self._answer_callback_safely(query, "Đã chọn tất cả đối tượng, đang tạo nháp Messenger...")
        await callback_message.edit_reply_markup(reply_markup=None)
        await self._create_messenger_media_draft_and_send_review(
            chat_id=callback_message.chat.id,
            media_input=media_input,
            media_content=media_content,
            post_fingerprint=str(request.get("post_fingerprint", "")),
            version=int(request.get("version", 1)),
            request_id=request_id,
            media_request_id=str(request.get("media_request_id", "")),
            telegram_bot=(getattr(callback_message, "bot", None) or getattr(query, "bot", None)),
        )

    async def _on_messenger_media_lifecycle_cancel(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        if request:
            media_request_id = str(request.get("media_request_id", "")).strip()
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
        await self._answer_callback_safely(query, "Đã hủy yêu cầu tạo campaign Messenger.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy yêu cầu tạo campaign Messenger.")

    async def _on_duplicate_cancel(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        if request and str(request.get("campaign_variant", "")).strip().lower() in {"web", "messenger_media"}:
            media_request_id = str(request.get("media_request_id", "")).strip()
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
        await query.answer("Đã hủy yêu cầu tạo phiên bản mới.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy tạo campaign mới cho link trùng.")

    async def _on_campaign_pick(self, query: CallbackQuery, request_id: str, index: int | None) -> None:
        if index is None:
            await query.answer("Lựa chọn không hợp lệ.", show_alert=True)
            return
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() != "existing_campaign_select":
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return

        candidates = request.get("campaign_candidates", [])
        if not isinstance(candidates, list) or index < 0 or index >= len(candidates):
            await query.answer("Chiến dịch đã thay đổi, anh chạy lại lệnh giúp em.", show_alert=True)
            return

        selected_raw = candidates[index] if isinstance(candidates[index], dict) else {}
        selected_campaign = {
            "id": str(selected_raw.get("id", "")).strip(),
            "name": str(selected_raw.get("name", "")).strip(),
            "updated_time": str(selected_raw.get("updated_time", "")).strip(),
        }
        if not selected_campaign["id"]:
            await query.answer("Chiến dịch đã thay đổi, anh chạy lại lệnh giúp em.", show_alert=True)
            return

        command = AdsCommand(
            post_url=str(request["post_url"]),
            budget_daily_vnd=int(request["budget_daily_vnd"]),
            use_existing_campaign=True,
            manual_sku_keywords=[str(item) for item in request.get("manual_sku_keywords", [])],
            existing_campaign_hint=str(request.get("existing_campaign_hint", "") or ""),
            media_owner=str(request.get("media_owner", "") or ""),
        )
        post_fingerprint = str(request["post_fingerprint"])
        version = int(request["version"])
        keywords = [str(item) for item in request.get("campaign_match_keywords", [])]

        await query.answer("Đang tạo nháp vào campaign cũ...")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        await self._create_existing_campaign_draft_and_send_review(
            chat_id=query.message.chat.id if query.message else query.from_user.id,
            command=command,
            post_fingerprint=post_fingerprint,
            version=version,
            selected_campaign=selected_campaign,
            campaign_keywords=keywords,
            request_id=request_id,
        )

    async def _on_campaign_cancel(self, query: CallbackQuery, request_id: str) -> None:
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        await query.answer("Đã hủy chọn campaign cũ.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy yêu cầu lên campaign cũ.")

    async def _on_reconcile_apply(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() != "reconcile_cod_apply":
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return
        if not self.reconcile:
            await query.answer("Luồng đối soát COD chưa sẵn sàng.", show_alert=True)
            return
        run_id = str(request.get("run_id", "")).strip()
        if not run_id:
            await query.answer("Thiếu run_id đối soát.", show_alert=True)
            return

        await query.answer("Đang cập nhật trạng thái Pancake theo batch...")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)

        try:
            summary = await asyncio.to_thread(self.reconcile.apply_updates, run_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Cap nhat status Pancake tu doi soat COD that bai")
            if query.message:
                await query.message.answer(f"Cập nhật trạng thái thất bại.\nLỗi: {exc}")
            return
        finally:
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)

        if query.message:
            lines = self._build_reconcile_apply_summary_lines(
                run_id=run_id,
                summary=summary,
                intro="Kết quả cập nhật COD:",
            )
            await query.message.answer("\n".join(lines))

    async def _on_reconcile_cancel(self, query: CallbackQuery, request_id: str) -> None:
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        await query.answer("Đã hủy duyệt cập nhật COD.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy cập nhật trạng thái Pancake từ batch đối soát COD.")

    async def _on_reconcile_sheet_apply(self, query: CallbackQuery, request_id: str) -> None:
        request = await asyncio.to_thread(self.storage.get_pending_request, request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc đã được xử lý.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() != "reconcile_cod_sheet_sync":
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return
        if not self.reconcile_sheet:
            await query.answer("Luồng ghi Google Sheet chưa sẵn sàng.", show_alert=True)
            return

        run_id = str(request.get("run_id", "")).strip()
        if not run_id:
            await query.answer("Thiếu run_id đối soát.", show_alert=True)
            return
        run_path = self.settings.reconcile_cod_runs_dir / f"{run_id}.json"
        if not run_path.exists():
            await query.answer("Không tìm thấy run đối soát để ghi Sheet.", show_alert=True)
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            return

        await query.answer("Đang ghi dữ liệu đối soát lên Google Sheet...")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)

        try:
            payload = await asyncio.to_thread(load_json, run_path)
            if not isinstance(payload, dict):
                raise ValueError("Run đối soát COD không hợp lệ.")
            sync_result = await asyncio.to_thread(self.reconcile_sheet.sync_report, payload)
            payload["sheet_sync"] = sync_result
            await asyncio.to_thread(dump_json, run_path, payload)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Ghi Google Sheet tu batch doi soat COD that bai")
            if query.message:
                await query.message.answer(f"Ghi Google Sheet thất bại.\nLỗi: {exc}")
            return
        finally:
            await asyncio.to_thread(self.storage.delete_pending_request, request_id)

        if not query.message:
            return
        if sync_result.get("ok"):
            product_sync = sync_result.get("product_sync", {})
            product_result = product_sync if isinstance(product_sync, dict) else {}
            await query.message.answer(
                "Kết quả ghi Google Sheet:\n"
                f"- Tổng ghi thử: {self._to_int(sync_result.get('attempted')):,}\n"
                f"- Ghi mới: {self._to_int(sync_result.get('inserted')):,}\n"
                f"- Bỏ qua trùng: {self._to_int(sync_result.get('skipped_existing')):,}\n"
                f"- Tab Chi tiet SP COD ghi mới: {self._to_int(product_result.get('inserted')):,}\n"
                f"- Tab Chi tiet SP COD bỏ qua trùng: {self._to_int(product_result.get('skipped_existing')):,}\n"
                f"- Tab Chi tiet SP COD sửa dòng cũ: {self._to_int(product_result.get('updated_existing')):,}"
            )
            return
        errors = sync_result.get("errors", [])
        error_text = ", ".join(str(item) for item in errors if str(item).strip()) or "Không rõ lỗi."
        product_sync = sync_result.get("product_sync", {})
        product_error = ""
        if isinstance(product_sync, dict):
            product_errors = product_sync.get("errors", [])
            if isinstance(product_errors, list) and product_errors:
                product_error = "\n- Lỗi tab sản phẩm: " + ", ".join(str(item) for item in product_errors)
        await query.message.answer(
            "Ghi Google Sheet thất bại.\n"
            f"- Tổng ghi thử: {self._to_int(sync_result.get('attempted')):,}\n"
            f"- Lỗi: {error_text}{product_error}"
        )

    async def _on_reconcile_sheet_cancel(self, query: CallbackQuery, request_id: str) -> None:
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        await query.answer("Đã hủy ghi Google Sheet.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy ghi kết quả đối soát COD lên Google Sheet.")

    async def _create_messenger_media_draft_and_send_review(
        self,
        chat_id: int,
        media_input: MessengerMediaAdsInput,
        media_content: bytes,
        post_fingerprint: str,
        version: int,
        request_id: str | None = None,
        media_request_id: str | None = None,
        telegram_bot: Bot | None = None,
    ) -> None:
        audiences = load_json(self.settings.audiences_config_path)
        objective = self._load_campaign_objective()
        templates = load_json(self.settings.message_templates_path)
        campaign_id: str | None = None
        adset_ids: list[str] = []
        ad_ids: list[str] = []
        creative_ids: list[str] = []
        media_asset_id = ""
        plan: PlannedCampaign | None = None
        try:
            await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
            plan = build_messenger_media_campaign_plan(
                media_input=media_input,
                post_fingerprint=post_fingerprint,
                version=version,
                audiences_config=audiences,
                objective_config=objective,
                template_config=templates,
            )
            if plan.media_kind == "video":
                media_asset_id = await asyncio.to_thread(
                    self.meta.upload_video_asset,
                    media_content,
                    media_input.media_filename,
                )
            else:
                media_asset_id = await asyncio.to_thread(
                    self.meta.upload_image_asset,
                    media_content,
                    media_input.media_filename,
                )

            campaign_id = await asyncio.to_thread(self.meta.create_campaign, plan)
            for slot in plan.audiences:
                adset_id: str | None = None
                creative_id: str | None = None
                ad_id: str | None = None
                try:
                    adset_id = await asyncio.to_thread(
                        self.meta.create_messenger_media_adset,
                        plan,
                        campaign_id,
                        slot,
                    )
                    adset_ids.append(adset_id)
                    creative_id = await asyncio.to_thread(
                        self.meta.create_messenger_media_ad_creative,
                        plan,
                        slot,
                        media_asset_id,
                    )
                    creative_ids.append(creative_id)
                    ad_id = await asyncio.to_thread(
                        self.meta.create_messenger_media_ad,
                        plan,
                        slot,
                        adset_id,
                        creative_id,
                    )
                    ad_ids.append(ad_id)
                except Exception:
                    if ad_id and ad_id not in ad_ids:
                        ad_ids.append(ad_id)
                    if creative_id and creative_id not in creative_ids:
                        creative_ids.append(creative_id)
                    if adset_id and adset_id not in adset_ids:
                        adset_ids.append(adset_id)
                    raise

            job_id = await asyncio.to_thread(self.storage.generate_job_id)
            job_payload = {
                "job_id": job_id,
                "status": "pending",
                "version": version,
                "campaign_mode": "new",
                "campaign_variant": "messenger_media",
                "publish_scope": "tree",
                "post_url": "",
                "resolved_permalink_url": "",
                "post_fingerprint": post_fingerprint,
                "budget_daily_vnd": media_input.budget_daily_vnd,
                "campaign_name": plan.campaign_name,
                "sku_code_text": plan.sku_code_text,
                "media_label": plan.media_label,
                "media_owner": media_input.media_owner,
                "media_kind": media_input.media_kind,
                "media_sha256": media_input.media_sha256,
                "media_asset_id": media_asset_id,
                "caption": media_input.caption,
                "headline": media_input.headline,
                "cta_label": "Gửi tin nhắn",
                "cta_type": "MESSAGE_PAGE",
                "destination_url": "",
                "customer_lifecycle_strategy": str(
                    plan.raw.get("customer_lifecycle_strategy", "")
                ).strip().upper(),
                "customer_lifecycle_label": web_lifecycle_label(
                    str(plan.raw.get("customer_lifecycle_strategy", ""))
                ),
                "objective": plan.objective,
                "conversion_location": plan.conversion_location,
                "result_goal": plan.result_goal,
                "message_template_name": plan.message_template_name,
                "requested_destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                "active_destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                "campaign_id": campaign_id,
                "adset_ids": adset_ids,
                "ad_ids": ad_ids,
                "creative_ids": creative_ids,
                "ads_manager_url": self._build_ads_manager_url(campaign_id),
                "created_at": now_utc_iso(),
            }
            await asyncio.to_thread(self.storage.save_job, job_payload, "pending")
            if request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
            sender = telegram_bot or self._bot
            if not sender:
                raise RuntimeError("Bot Telegram chưa sẵn sàng để gửi thông báo review Messenger.")
            await sender.send_message(
                chat_id=chat_id,
                text=self._build_review_message(job_payload),
                reply_markup=self.approval.review_keyboard(job_id),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tạo nháp campaign Messenger media thất bại")
            await asyncio.to_thread(self.rollback.rollback, campaign_id, adset_ids, ad_ids, creative_ids)
            failed_job_id = await asyncio.to_thread(self.storage.generate_job_id)
            failed_payload = {
                "job_id": failed_job_id,
                "status": "failed",
                "version": version,
                "campaign_mode": "new",
                "campaign_variant": "messenger_media",
                "publish_scope": "tree",
                "post_url": "",
                "post_fingerprint": post_fingerprint,
                "budget_daily_vnd": media_input.budget_daily_vnd,
                "campaign_name": plan.campaign_name if plan else "",
                "sku_code_text": plan.sku_code_text if plan else "",
                "media_label": plan.media_label if plan else ("Video" if media_input.media_kind == "video" else "Anh"),
                "media_owner": media_input.media_owner,
                "media_kind": media_input.media_kind,
                "media_sha256": media_input.media_sha256,
                "media_asset_id": media_asset_id,
                "caption": media_input.caption,
                "headline": media_input.headline,
                "cta_label": "Gửi tin nhắn",
                "cta_type": "MESSAGE_PAGE",
                "destination_url": "",
                "customer_lifecycle_strategy": media_input.customer_lifecycle_strategy,
                "campaign_id": campaign_id,
                "adset_ids": adset_ids,
                "ad_ids": ad_ids,
                "creative_ids": creative_ids,
                "error": str(exc),
                "created_at": now_utc_iso(),
            }
            await asyncio.to_thread(self.storage.save_job, failed_payload, "failed")
            if request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
            sender = telegram_bot or self._bot
            if not sender:
                raise RuntimeError("Bot Telegram chưa sẵn sàng để gửi lỗi Messenger.")
            await sender.send_message(
                chat_id=chat_id,
                text=f"Tạo nháp campaign Messenger thất bại, đã rollback.\nLỗi: {exc}",
            )

    async def _create_web_draft_and_send_review(
        self,
        chat_id: int,
        web_input: WebAdsInput,
        media_content: bytes,
        post_fingerprint: str,
        version: int,
        request_id: str | None = None,
        media_request_id: str | None = None,
        telegram_bot: Bot | None = None,
    ) -> None:
        audiences = load_json(self.settings.audiences_config_path)
        objective = load_json(self.settings.web_objective_config_path)
        campaign_id: str | None = None
        adset_ids: list[str] = []
        ad_ids: list[str] = []
        creative_ids: list[str] = []
        media_asset_id = ""
        thumbnail_image_hash = ""

        try:
            await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
            await asyncio.to_thread(self.meta.ensure_pixel_can_access, self.settings.meta_pixel_id)
            plan = build_web_campaign_plan(
                web_input=web_input,
                post_fingerprint=post_fingerprint,
                version=version,
                audiences_config=audiences,
                objective_config=objective,
            )
            if plan.media_kind == "video":
                thumbnail_content = await asyncio.to_thread(
                    self._download_web_video_thumbnail,
                    web_input.media_url,
                )
                if thumbnail_content:
                    thumbnail_image_hash = await asyncio.to_thread(
                        self.meta.upload_image_asset,
                        thumbnail_content,
                        "video_thumbnail.jpg",
                    )
                media_asset_id = await asyncio.to_thread(
                    self.meta.upload_video_asset,
                    media_content,
                    web_input.media_filename,
                )
            else:
                media_asset_id = await asyncio.to_thread(
                    self.meta.upload_image_asset,
                    media_content,
                    web_input.media_filename,
                )

            campaign_id = await asyncio.to_thread(self.meta.create_campaign, plan)
            for slot in plan.audiences:
                adset_id: str | None = None
                creative_id: str | None = None
                ad_id: str | None = None
                try:
                    adset_id = await asyncio.to_thread(self.meta.create_web_adset, plan, campaign_id, slot)
                    adset_ids.append(adset_id)
                    creative_id = await asyncio.to_thread(
                        self.meta.create_web_ad_creative,
                        plan,
                        slot,
                        media_asset_id,
                        self._build_web_video_thumbnail_url(web_input.media_url),
                        thumbnail_image_hash,
                    )
                    creative_ids.append(creative_id)
                    ad_id = await asyncio.to_thread(self.meta.create_web_ad, plan, slot, adset_id, creative_id)
                    ad_ids.append(ad_id)
                except Exception:
                    if ad_id and ad_id not in ad_ids:
                        ad_ids.append(ad_id)
                    if creative_id and creative_id not in creative_ids:
                        creative_ids.append(creative_id)
                    if adset_id and adset_id not in adset_ids:
                        adset_ids.append(adset_id)
                    raise

            job_id = await asyncio.to_thread(self.storage.generate_job_id)
            job_payload = {
                "job_id": job_id,
                "status": "pending",
                "version": version,
                "campaign_mode": "new",
                "campaign_variant": "web",
                "publish_scope": "tree",
                "post_url": "",
                "resolved_permalink_url": "",
                "post_fingerprint": post_fingerprint,
                "budget_daily_vnd": web_input.budget_daily_vnd,
                "campaign_name": plan.campaign_name,
                "sku_code_text": plan.sku_code_text,
                "media_label": plan.media_label,
                "media_owner": web_input.media_owner,
                "media_kind": web_input.media_kind,
                "media_sha256": web_input.media_sha256,
                "media_asset_id": media_asset_id,
                "thumbnail_image_hash": thumbnail_image_hash,
                "caption": web_input.caption,
                "headline": web_input.headline,
                "cta_label": web_input.cta_label,
                "cta_type": web_input.cta_type,
                "destination_url": web_input.destination_url,
                "customer_lifecycle_strategy": str(
                    plan.raw.get("customer_lifecycle_strategy", "")
                ).strip().upper(),
                "customer_lifecycle_label": web_lifecycle_label(
                    str(plan.raw.get("customer_lifecycle_strategy", ""))
                ),
                "objective": plan.objective,
                "conversion_location": plan.conversion_location,
                "result_goal": plan.result_goal,
                "message_template_name": "",
                "requested_destination_type": "WEBSITE",
                "active_destination_type": "WEBSITE",
                "campaign_id": campaign_id,
                "adset_ids": adset_ids,
                "ad_ids": ad_ids,
                "creative_ids": creative_ids,
                "ads_manager_url": self._build_ads_manager_url(campaign_id),
                "created_at": now_utc_iso(),
            }
            await asyncio.to_thread(self.storage.save_job, job_payload, "pending")
            if request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, request_id)
            if media_request_id:
                await asyncio.to_thread(self.storage.delete_pending_request, media_request_id)
            sender = telegram_bot or self._bot
            if not sender:
                raise RuntimeError("Bot Telegram chưa sẵn sàng để gửi thông báo review Web.")
            await sender.send_message(
                chat_id=chat_id,
                text=self._build_review_message(job_payload),
                reply_markup=self.approval.review_keyboard(job_id),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tạo nháp campaign Web thất bại")
            await asyncio.to_thread(self.rollback.rollback, campaign_id, adset_ids, ad_ids, creative_ids)
            failed_job_id = await asyncio.to_thread(self.storage.generate_job_id)
            failed_payload = {
                "job_id": failed_job_id,
                "status": "failed",
                "version": version,
                "campaign_mode": "new",
                "campaign_variant": "web",
                "publish_scope": "tree",
                "post_url": "",
                "post_fingerprint": post_fingerprint,
                "budget_daily_vnd": web_input.budget_daily_vnd,
                "campaign_name": "",
                "media_kind": web_input.media_kind,
                "media_sha256": web_input.media_sha256,
                "media_owner": web_input.media_owner,
                "media_asset_id": media_asset_id,
                "thumbnail_image_hash": thumbnail_image_hash,
                "caption": web_input.caption,
                "headline": web_input.headline,
                "cta_label": web_input.cta_label,
                "cta_type": web_input.cta_type,
                "destination_url": web_input.destination_url,
                "customer_lifecycle_strategy": str(
                    plan.raw.get("customer_lifecycle_strategy", "")
                ).strip().upper(),
                "campaign_id": campaign_id,
                "adset_ids": adset_ids,
                "ad_ids": ad_ids,
                "creative_ids": creative_ids,
                "error": str(exc),
                "created_at": now_utc_iso(),
            }
            await asyncio.to_thread(self.storage.save_job, failed_payload, "failed")
            sender = telegram_bot or self._bot
            if not sender:
                raise RuntimeError("Bot Telegram chưa sẵn sàng để gửi lỗi Web.")
            await sender.send_message(
                chat_id=chat_id,
                text=f"Tạo nháp campaign Web thất bại, đã rollback.\nLỗi: {exc}",
            )

    def _get_job_action_lock(self, job_id: str) -> asyncio.Lock:
        normalized_job_id = str(job_id or "").strip()
        lock = self._job_action_locks.get(normalized_job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._job_action_locks[normalized_job_id] = lock
        return lock

    async def _on_approve(self, query: CallbackQuery, job_id: str) -> None:
            job_entry = await asyncio.to_thread(self.storage.find_job, job_id)
            if not job_entry:
                await self._answer_callback_safely(query, "Không tìm thấy job.", show_alert=True)
                if query.message:
                    await query.message.answer(
                        "Không tìm thấy dữ liệu job để publish. Anh chạy lại lệnh tạo nháp giúp em."
                    )
                return
            status, job = job_entry
            if status != "pending":
                await self._answer_callback_safely(query, f"Job đã ở trạng thái {status}.", show_alert=True)
                return

            publish_scope = str(job.get("publish_scope", "tree")).strip().lower()
            await self._answer_callback_safely(query, "Đang publish campaign...")
            try:
                if publish_scope == "ads_only":
                    await asyncio.to_thread(
                        self.meta.publish_ads,
                        [str(item) for item in job.get("ad_ids", [])],
                    )
                elif publish_scope == "adsets_ads":
                    await asyncio.to_thread(
                        self.meta.publish_adsets_and_ads,
                        [str(item) for item in job.get("adset_ids", [])],
                        [str(item) for item in job.get("ad_ids", [])],
                    )
                else:
                    await asyncio.to_thread(
                        self.meta.publish_tree,
                        str(job["campaign_id"]),
                        [str(item) for item in job["adset_ids"]],
                        [str(item) for item in job["ad_ids"]],
                    )
                updated = await asyncio.to_thread(
                    self.storage.move_job_status,
                    job_id,
                    "pending",
                    "published",
                    {"published_at": now_utc_iso()},
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Loi publish job %s", job_id)
                campaign_value = str(job.get("campaign_id", "")).strip()
                rollback_campaign_id = campaign_value if (publish_scope == "tree" and campaign_value) else None
                rollback_adset_ids = (
                    [str(item) for item in job.get("adset_ids", [])]
                    if publish_scope in {"tree", "adsets_ads"}
                    else []
                )
                await asyncio.to_thread(
                    self.rollback.rollback,
                    rollback_campaign_id,
                    rollback_adset_ids,
                    [str(item) for item in job.get("ad_ids", [])],
                    [str(item) for item in job.get("creative_ids", [])],
                )
                await asyncio.to_thread(
                    self.storage.move_job_status,
                    job_id,
                    "pending",
                    "failed",
                    {"error": str(exc), "failed_at": now_utc_iso()},
                )
                if query.message:
                    await query.message.answer(
                        f"Publish thất bại, đã rollback job {job_id}.\nLỗi: {exc}",
                    )
                return

            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer(
                    self._build_published_message(updated),
                )


    async def _on_approve_locked(self, query: CallbackQuery, job_id: str) -> None:
        job_entry = await asyncio.to_thread(self.storage.find_job, job_id)
        if not job_entry:
            await query.answer("Không tìm thấy job.", show_alert=True)
            return
        status, job = job_entry
        if status != "pending":
            await query.answer(f"Job đã ở trạng thái {status}.", show_alert=True)
            return

        try:
            job = await asyncio.to_thread(
                self.storage.move_job_status,
                job_id,
                "pending",
                "publishing",
                {"publishing_started_at": now_utc_iso()},
            )
        except FileNotFoundError:
            current_entry = await asyncio.to_thread(self.storage.find_job, job_id)
            current_status = current_entry[0] if current_entry else "không xác định"
            await query.answer(
                f"Job đang được xử lý hoặc đã chuyển sang trạng thái {current_status}; em bỏ qua lần bấm trùng.",
                show_alert=True,
            )
            return

        publish_scope = str(job.get("publish_scope", "tree")).strip().lower()
        await query.answer("Đang publish campaign...")
        try:
            if publish_scope == "ads_only":
                await asyncio.to_thread(
                    self.meta.publish_ads,
                    [str(item) for item in job.get("ad_ids", [])],
                )
            else:
                await asyncio.to_thread(
                    self.meta.publish_tree,
                    str(job["campaign_id"]),
                    [str(item) for item in job["adset_ids"]],
                    [str(item) for item in job["ad_ids"]],
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Loi publish job %s", job_id)
            campaign_value = str(job.get("campaign_id", "")).strip()
            rollback_campaign_id = campaign_value if (publish_scope != "ads_only" and campaign_value) else None
            rollback_adset_ids = [str(item) for item in job.get("adset_ids", [])] if publish_scope != "ads_only" else []
            try:
                await asyncio.to_thread(
                    self.rollback.rollback,
                    rollback_campaign_id,
                    rollback_adset_ids,
                    [str(item) for item in job.get("ad_ids", [])],
                    [str(item) for item in job.get("creative_ids", [])],
                )
            except Exception:  # noqa: BLE001
                self.logger.exception("Rollback publish job %s that bai", job_id)
            try:
                await asyncio.to_thread(
                    self.storage.move_job_status,
                    job_id,
                    "publishing",
                    "failed",
                    {"error": str(exc), "failed_at": now_utc_iso()},
                )
            except Exception:  # noqa: BLE001
                self.logger.exception("Khong ghi duoc trang thai failed cho job %s", job_id)
            if query.message:
                await query.message.answer(
                    f"Publish thất bại, đã rollback job {job_id}.\nLỗi: {exc}",
                )
            return

        try:
            updated = await asyncio.to_thread(
                self.storage.move_job_status,
                job_id,
                "publishing",
                "published",
                {"published_at": now_utc_iso()},
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Meta publish thanh cong nhung khong ghi duoc job %s", job_id)
            if query.message:
                await query.message.answer(
                    f"Meta đã publish job {job_id}, nhưng bot chưa ghi được trạng thái lưu trữ. "
                    f"Không rollback để tránh xóa campaign. Lỗi: {exc}",
                )
            return

        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer(
                self._build_published_message(updated),
            )

    async def _on_reject(self, query: CallbackQuery, job_id: str) -> None:
            job_entry = await asyncio.to_thread(self.storage.find_job, job_id)
            if not job_entry:
                await self._answer_callback_safely(query, "Không tìm thấy job.", show_alert=True)
                if query.message:
                    await query.message.answer(
                        "Không tìm thấy dữ liệu job để hủy. Anh kiểm tra Ads Manager giúp em."
                    )
                return
            status, job = job_entry
            if status != "pending":
                await self._answer_callback_safely(query, f"Job đã ở trạng thái {status}.", show_alert=True)
                return

            publish_scope = str(job.get("publish_scope", "tree")).strip().lower()
            campaign_value = str(job.get("campaign_id", "")).strip()
            rollback_campaign_id = campaign_value if (publish_scope == "tree" and campaign_value) else None
            rollback_adset_ids = (
                [str(item) for item in job.get("adset_ids", [])]
                if publish_scope in {"tree", "adsets_ads"}
                else []
            )
            await self._answer_callback_safely(query, "Đang hủy và rollback...")
            await asyncio.to_thread(
                self.rollback.rollback,
                rollback_campaign_id,
                rollback_adset_ids,
                [str(item) for item in job.get("ad_ids", [])],
                [str(item) for item in job.get("creative_ids", [])],
            )
            await asyncio.to_thread(
                self.storage.move_job_status,
                job_id,
                "pending",
                "cancelled",
                {"cancelled_at": now_utc_iso()},
            )
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer(f"Đã hủy job {job_id} và rollback dữ liệu.")


    async def _on_reject_locked(self, query: CallbackQuery, job_id: str) -> None:
        job_entry = await asyncio.to_thread(self.storage.find_job, job_id)
        if not job_entry:
            await query.answer("Không tìm thấy job.", show_alert=True)
            return
        status, job = job_entry
        if status != "pending":
            await query.answer(f"Job đã ở trạng thái {status}.", show_alert=True)
            return

        publish_scope = str(job.get("publish_scope", "tree")).strip().lower()
        campaign_value = str(job.get("campaign_id", "")).strip()
        rollback_campaign_id = campaign_value if (publish_scope != "ads_only" and campaign_value) else None
        rollback_adset_ids = [str(item) for item in job.get("adset_ids", [])] if publish_scope != "ads_only" else []
        await query.answer("Đang hủy và rollback...")
        await asyncio.to_thread(
            self.rollback.rollback,
            rollback_campaign_id,
            rollback_adset_ids,
            [str(item) for item in job.get("ad_ids", [])],
            [str(item) for item in job.get("creative_ids", [])],
        )
        await asyncio.to_thread(
            self.storage.move_job_status,
            job_id,
            "pending",
            "cancelled",
            {"cancelled_at": now_utc_iso()},
        )
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer(f"Đã hủy job {job_id} và rollback dữ liệu.")

    async def _create_draft_and_send_review(
            self,
            chat_id: int,
            command: AdsCommand,
            post_fingerprint: str,
            version: int,
            request_id: str | None = None,
        ) -> None:
            audiences = load_json(self.settings.audiences_config_path)
            objective = self._load_objective_config()
            templates = load_json(self.settings.message_templates_path)
            sku_prefix = resolve_sku_prefix(objective)

            campaign_id: str | None = None
            adset_ids: list[str] = []
            ad_ids: list[str] = []
            creative_ids: list[str] = []

            fallback_destination_type = "MESSENGER"
            try:
                resolved_post = await asyncio.to_thread(self.meta.resolve_post, command.post_url)
                await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
                plan = build_campaign_plan(
                    command=command,
                    resolved_post=resolved_post,
                    post_fingerprint=post_fingerprint,
                    version=version,
                    timezone_name=self.settings.app_timezone,
                    audiences_config=audiences,
                    objective_config=objective,
                    template_config=templates,
                )
                requested_destination_type = self.meta.effective_destination_type(plan)
                active_plan = plan
                active_destination_type = requested_destination_type
                destination_fallback_reason = ""
                fallback_destination_type = self._new_campaign_fallback_destination_type(objective)
                multi_destination_asset_feed_spec: dict[str, Any] | None = None
                multi_destination_creative_overrides: dict[str, Any] | None = None
                force_message_page_cta = self._should_force_message_page_cta_for_post(plan, resolved_post)
                creative_payload_overrides = plan.raw.get("creative_payload_overrides", {})
                expected_page_welcome_message = (
                    creative_payload_overrides.get("page_welcome_message")
                    if isinstance(creative_payload_overrides, dict)
                    else None
                )
                expected_reusable_call_to_action_type = "MESSAGE_PAGE" if force_message_page_cta else None
                campaign_posts: list[Any] = [resolved_post]
                source_ads_package: dict[str, Any] | None = None
                same_story_source_ad: dict[str, str] | None = None
                if self._should_clone_new_campaign_source_ads(objective):
                    try:
                        source_ads_package = await asyncio.to_thread(
                            self._find_new_campaign_source_ads,
                            active_plan,
                            objective,
                        )
                        if source_ads_package:
                            self.logger.info(
                                "Len moi se copy %s ads tu source adset %s cho campaign %s.",
                                len(source_ads_package.get("ads", [])),
                                source_ads_package.get("adset_id", ""),
                                plan.campaign_name,
                            )
                    except (ValidationError, MetaApiError, ValueError, TypeError) as exc:
                        self.logger.warning(
                            "Khong lay duoc source ads cho luong len moi, fallback scan page: %s",
                            exc,
                        )

                if source_ads_package is None and self._should_collect_sku_page_posts(objective):
                    try:
                        sku_posts = await asyncio.to_thread(
                            self.meta.list_page_posts_by_sku,
                            [code for code in plan.sku_code_text.split("_") if code],
                            max_scan=int(objective.get("collect_sku_page_posts_max_scan", 1000)),
                            max_posts=int(objective.get("collect_sku_page_posts_max_posts", 20)),
                        )
                        campaign_posts = self._merge_resolved_posts(sku_posts, [resolved_post])
                        self.logger.info(
                            "Len moi gom %s post cung SKU %s cho campaign %s.",
                            len(campaign_posts),
                            plan.sku_code_text,
                            plan.campaign_name,
                        )
                    except (ValidationError, MetaApiError, ValueError, TypeError) as exc:
                        self.logger.warning(
                            "Khong quet duoc cac post cung SKU cho luong len moi, fallback 1 link: %s",
                            exc,
                        )

                if (
                    source_ads_package is None
                    and len(campaign_posts) == 1
                    and self._should_reuse_same_story_ad_for_new_campaign(objective)
                ):
                    try:
                        same_story_source_ad = await asyncio.to_thread(
                            self.meta.find_latest_ad_by_story_ids,
                            self._build_story_id_candidates(resolved_post, command.post_url),
                            max_ads_scan=self._config_int(
                                objective.get("new_campaign_same_story_ad_max_scan"),
                                1600,
                            ),
                            expected_page_welcome_message=expected_page_welcome_message,
                            expected_call_to_action_type=expected_reusable_call_to_action_type,
                            expected_adset_destination_type=active_destination_type
                            if active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                            else None,
                        )
                        if same_story_source_ad:
                            self.logger.info(
                                "Len moi se duplicate same-story ad %s cho post %s.",
                                same_story_source_ad.get("id", ""),
                                resolved_post.object_story_id,
                            )
                    except (ValidationError, MetaApiError, ValueError, TypeError) as exc:
                        self.logger.warning(
                            "Khong tra cuu duoc same-story ad cho luong len moi, tiep tuc tao fresh: %s",
                            exc,
                        )

                if (
                    active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                    and source_ads_package is None
                    and same_story_source_ad is None
                ):
                    try:
                        if force_message_page_cta:
                            multi_destination_creative_overrides = await asyncio.to_thread(
                                self.meta.get_account_multi_destination_creative_overrides,
                                expected_call_to_action_type="MESSAGE_PAGE",
                            )
                        else:
                            multi_destination_asset_feed_spec = await asyncio.to_thread(
                                self.meta.get_account_multi_destination_asset_feed_spec
                            )
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong lay duoc creative seed/asset_feed_spec tham chieu cho luong len moi: %s",
                            exc,
                        )

                campaign_id = await asyncio.to_thread(self.meta.create_campaign, active_plan)

                async def _rollback_created_since(
                    adset_start: int,
                    ad_start: int,
                    creative_start: int,
                ) -> None:
                    rollback_adset_ids = adset_ids[adset_start:]
                    rollback_ad_ids = ad_ids[ad_start:]
                    rollback_creative_ids = creative_ids[creative_start:]
                    if rollback_adset_ids or rollback_ad_ids or rollback_creative_ids:
                        await asyncio.to_thread(
                            self.rollback.rollback,
                            None,
                            rollback_adset_ids,
                            rollback_ad_ids,
                            rollback_creative_ids,
                        )
                    del adset_ids[adset_start:]
                    del ad_ids[ad_start:]
                    del creative_ids[creative_start:]

                async def _create_fresh_ads_for_posts(
                    plan_to_use: PlannedCampaign,
                    destination_type: str,
                    posts_to_create: list[Any],
                    *,
                    messenger_retry_overrides: dict[str, Any] | None = None,
                ) -> None:
                    for slot in plan_to_use.audiences:
                        adset_id = await asyncio.to_thread(
                            self.meta.create_adset,
                            plan_to_use,
                            campaign_id,
                            slot,
                        )
                        adset_ids.append(adset_id)
                        for post_item in posts_to_create:
                            post_slot = replace(
                                slot,
                                ad_name=self._new_campaign_ad_name_for_post(plan, post_item, sku_prefix),
                            )
                            creative_extra_overrides = (
                                dict(messenger_retry_overrides)
                                if isinstance(messenger_retry_overrides, dict)
                                else self._new_campaign_creative_overrides(
                                    plan_to_use,
                                    post_item,
                                    destination_type,
                                    multi_destination_creative_overrides,
                                    multi_destination_asset_feed_spec,
                                )
                            )
                            creative_id = await asyncio.to_thread(
                                self.meta.create_ad_creative,
                                plan_to_use,
                                post_slot,
                                post_item,
                                None,
                                creative_extra_overrides,
                            )
                            creative_ids.append(creative_id)
                            ad_id = await asyncio.to_thread(
                                self.meta.create_ad,
                                plan_to_use,
                                post_slot,
                                adset_id,
                                creative_id,
                                "MESSENGER" if destination_type == "MESSENGER" else None,
                            )
                            ad_ids.append(ad_id)

                if source_ads_package is not None:
                    source_ads = [
                        item
                        for item in source_ads_package.get("ads", [])
                        if isinstance(item, dict) and str(item.get("id", "")).strip()
                    ]
                    if not source_ads:
                        raise ValidationError("Không tìm thấy source ads hợp lệ để copy sang campaign mới.")
                    for slot in active_plan.audiences:
                        adset_id = await asyncio.to_thread(self.meta.create_adset, active_plan, campaign_id, slot)
                        adset_ids.append(adset_id)
                        for source_ad in source_ads:
                            source_ad_id = str(source_ad.get("id", "")).strip()
                            target_ad_name = str(source_ad.get("name", "")).strip()
                            ad_id = await asyncio.to_thread(
                                self.meta.duplicate_ad_from_source,
                                source_ad_id,
                                target_ad_name,
                                target_adset_id=adset_id,
                                status_option="PAUSED",
                            )
                            ad_ids.append(ad_id)
                elif same_story_source_ad is not None and str(same_story_source_ad.get("id", "")).strip():
                    source_ad_id = str(same_story_source_ad.get("id", "")).strip()
                    for slot in active_plan.audiences:
                        adset_id = await asyncio.to_thread(self.meta.create_adset, active_plan, campaign_id, slot)
                        adset_ids.append(adset_id)
                        target_ad_name = self._new_campaign_ad_name_for_post(plan, resolved_post, sku_prefix)
                        ad_id = await asyncio.to_thread(
                            self.meta.duplicate_ad_from_source,
                            source_ad_id,
                            target_ad_name,
                            target_adset_id=adset_id,
                            status_option="PAUSED",
                        )
                        ad_ids.append(ad_id)
                elif len(campaign_posts) > 1:
                    adset_start = len(adset_ids)
                    ad_start = len(ad_ids)
                    creative_start = len(creative_ids)
                    try:
                        await _create_fresh_ads_for_posts(
                            active_plan,
                            active_destination_type,
                            campaign_posts,
                        )
                    except MetaApiError as exc:
                        error_text = str(exc)
                        if (
                            active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                            and fallback_destination_type
                            and (
                                self.meta.is_auto_destination_error(error_text)
                                or self.meta.is_instagram_media_requirement_error(error_text)
                            )
                        ):
                            await _rollback_created_since(adset_start, ad_start, creative_start)
                            active_plan = self._plan_with_destination_override(active_plan, fallback_destination_type)
                            active_destination_type = fallback_destination_type
                            destination_fallback_reason = error_text
                            messenger_retry_overrides = self._new_campaign_messenger_retry_overrides(
                                self.settings.meta_page_id,
                                multi_destination_creative_overrides,
                                multi_destination_asset_feed_spec,
                            )
                            self.logger.warning(
                                "Tu dong fallback len moi nhieu post sang MESSENGER asset_feed_spec "
                                "do Meta chan multi-destination: %s",
                                exc,
                            )
                            await _create_fresh_ads_for_posts(
                                active_plan,
                                active_destination_type,
                                campaign_posts,
                                messenger_retry_overrides=messenger_retry_overrides,
                            )
                        else:
                            raise
                else:
                    for slot in active_plan.audiences:
                        adset_id: str | None = None
                        creative_id: str | None = None
                        ad_id: str | None = None
                        creative_extra_overrides = self._new_campaign_creative_overrides(
                            active_plan,
                            resolved_post,
                            active_destination_type,
                            multi_destination_creative_overrides,
                            multi_destination_asset_feed_spec,
                        )
                        try:
                            adset_id = await asyncio.to_thread(self.meta.create_adset, active_plan, campaign_id, slot)
                            creative_id = await asyncio.to_thread(
                                self.meta.create_ad_creative,
                                active_plan,
                                slot,
                                resolved_post,
                                None,
                                creative_extra_overrides,
                            )
                            ad_id = await asyncio.to_thread(
                                self.meta.create_ad,
                                active_plan,
                                slot,
                                adset_id,
                                creative_id,
                            )
                        except MetaApiError as exc:
                            error_text = str(exc)
                            if (
                                active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                                and (
                                    self.meta.is_auto_destination_error(error_text)
                                    or self.meta.is_instagram_media_requirement_error(error_text)
                                )
                            ):
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                if adset_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [adset_id], [], [])
                                if not fallback_destination_type:
                                    raise
                                active_plan = self._plan_with_destination_override(active_plan, fallback_destination_type)
                                active_destination_type = fallback_destination_type
                                destination_fallback_reason = error_text
                                fallback_reason = (
                                    "media Instagram"
                                    if self.meta.is_instagram_media_requirement_error(error_text)
                                    else "auto-destination"
                                )
                                self.logger.warning(
                                    "Tu dong fallback destination sang MESSENGER do loi %s: %s",
                                    fallback_reason,
                                    exc,
                                )

                                adset_id = await asyncio.to_thread(self.meta.create_adset, active_plan, campaign_id, slot)
                                messenger_retry_overrides = self._new_campaign_messenger_retry_overrides(
                                    self.settings.meta_page_id,
                                    multi_destination_creative_overrides,
                                    multi_destination_asset_feed_spec,
                                )
                                creative_id = await asyncio.to_thread(
                                    self.meta.create_ad_creative,
                                    active_plan,
                                    slot,
                                    resolved_post,
                                    None,
                                    messenger_retry_overrides,
                                )
                                try:
                                    ad_id = await asyncio.to_thread(
                                        self.meta.create_ad,
                                        active_plan,
                                        slot,
                                        adset_id,
                                        creative_id,
                                    )
                                except MetaApiError as fallback_ad_exc:
                                    fallback_error_text = str(fallback_ad_exc)
                                    if self.meta.is_auto_destination_error(fallback_error_text):
                                        self.logger.warning(
                                            "Retry fallback len moi voi payload ad MESSENGER cho slot %s do loi objective: %s",
                                            slot.label,
                                            fallback_ad_exc,
                                        )
                                        try:
                                            ad_id = await asyncio.to_thread(
                                                self.meta.create_ad,
                                                active_plan,
                                                slot,
                                                adset_id,
                                                creative_id,
                                                "MESSENGER",
                                            )
                                        except MetaApiError:
                                            if creative_id:
                                                await asyncio.to_thread(
                                                    self.rollback.rollback,
                                                    None,
                                                    [],
                                                    [],
                                                    [creative_id],
                                                )
                                                creative_id = None
                                            if adset_id:
                                                await asyncio.to_thread(
                                                    self.rollback.rollback,
                                                    None,
                                                    [adset_id],
                                                    [],
                                                    [],
                                                )
                                                adset_id = None
                                            raise
                                    else:
                                        if creative_id:
                                            await asyncio.to_thread(
                                                self.rollback.rollback,
                                                None,
                                                [],
                                                [],
                                                [creative_id],
                                            )
                                            creative_id = None
                                        if adset_id:
                                            await asyncio.to_thread(
                                                self.rollback.rollback,
                                                None,
                                                [adset_id],
                                                [],
                                                [],
                                            )
                                            adset_id = None
                                        raise
                            else:
                                raise
                        adset_ids.append(adset_id)
                        creative_ids.append(creative_id)
                        ad_ids.append(ad_id)

                job_id = await asyncio.to_thread(self.storage.generate_job_id)
                job_payload = {
                    "job_id": job_id,
                    "status": "pending",
                    "version": version,
                    "campaign_name": plan.campaign_name,
                    "sku_code_text": plan.sku_code_text,
                    "media_label": plan.media_label,
                    "post_url": command.post_url,
                    "post_fingerprint": post_fingerprint,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "objective": active_plan.objective,
                    "conversion_location": active_plan.conversion_location,
                    "result_goal": active_plan.result_goal,
                    "message_template_name": active_plan.message_template_name,
                    "requested_destination_type": requested_destination_type,
                    "active_destination_type": active_destination_type,
                    "destination_fallback_reason": destination_fallback_reason,
                    "campaign_mode": "new",
                    "campaign_match_keywords": [],
                    "selected_campaign_id": campaign_id,
                    "selected_campaign_name": plan.campaign_name,
                    "selected_adset_ids": adset_ids,
                    "selected_adset_count": len(adset_ids),
                    "publish_scope": "tree",
                    "campaign_id": campaign_id,
                    "adset_ids": adset_ids,
                    "ad_ids": ad_ids,
                    "creative_ids": creative_ids,
                    "resolved_post_id": resolved_post.post_id,
                    "resolved_page_id": resolved_post.page_id,
                    "object_story_id": resolved_post.object_story_id,
                    "resolved_permalink_url": resolved_post.permalink_url,
                    "resolved_strategy": resolved_post.strategy,
                    "included_post_count": len(campaign_posts),
                    "included_posts": [
                        {
                            "post_id": post_item.post_id,
                            "object_story_id": post_item.object_story_id,
                            "media_label": post_item.media_label,
                            "permalink_url": post_item.permalink_url,
                        }
                        for post_item in campaign_posts
                    ],
                    "source_campaign_id": (
                        str(source_ads_package.get("campaign_id", "")).strip()
                        if isinstance(source_ads_package, dict)
                        else ""
                    ),
                    "source_campaign_name": (
                        str(source_ads_package.get("campaign_name", "")).strip()
                        if isinstance(source_ads_package, dict)
                        else ""
                    ),
                    "source_adset_id": (
                        str(source_ads_package.get("adset_id", "")).strip()
                        if isinstance(source_ads_package, dict)
                        else ""
                    ),
                    "source_adset_name": (
                        str(source_ads_package.get("adset_name", "")).strip()
                        if isinstance(source_ads_package, dict)
                        else ""
                    ),
                    "source_ad_count": (
                        len(source_ads_package.get("ads", []))
                        if isinstance(source_ads_package, dict)
                        else 0
                    ),
                    "source_ad_id": str(same_story_source_ad.get("id", "")).strip()
                    if isinstance(same_story_source_ad, dict)
                    else "",
                    "source_ad_name": str(same_story_source_ad.get("name", "")).strip()
                    if isinstance(same_story_source_ad, dict)
                    else "",
                    "ads_manager_url": self._build_ads_manager_url(campaign_id),
                    "created_at": now_utc_iso(),
                }
                await asyncio.to_thread(self.storage.save_job, job_payload, "pending")

                if request_id:
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

                if not self._bot:
                    raise RuntimeError("Telegram bot chua duoc khoi tao.")
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=self._build_review_message(job_payload),
                    reply_markup=self.approval.review_keyboard(job_id),
                )
            except (ValidationError, MetaApiError, ValueError, KeyError, TypeError) as exc:
                self.logger.exception("Tạo nháp thất bại")
                await asyncio.to_thread(self.rollback.rollback, campaign_id, adset_ids, ad_ids, creative_ids)
                failed_job_id = await asyncio.to_thread(self.storage.generate_job_id)
                failed_payload = {
                    "job_id": failed_job_id,
                    "status": "failed",
                    "version": version,
                    "campaign_mode": "new",
                    "publish_scope": "tree",
                    "post_url": command.post_url,
                    "post_fingerprint": post_fingerprint,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "campaign_id": campaign_id,
                    "adset_ids": adset_ids,
                    "ad_ids": ad_ids,
                    "creative_ids": creative_ids,
                    "error": str(exc),
                    "failed_at": now_utc_iso(),
                }
                await asyncio.to_thread(self.storage.save_job, failed_payload, "failed")
                if request_id:
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

                if not self._bot:
                    raise RuntimeError("Telegram bot chua duoc khoi tao.")
                error_text = str(exc)
                if self.meta.is_instagram_media_requirement_error(error_text) or self.meta.is_auto_destination_error(error_text):
                    if not fallback_destination_type:
                        user_guidance = (
                            "Meta API đang chặn tạo creative mới cho Đích đến tự động.\n"
                            "Em không tự chuyển sang Messenger-only nữa để giữ đúng target anh yêu cầu.\n"
                            "Bài này cần có một ad mẫu cùng post/reel để em duplicate, "
                            "hoặc cần cấp creative token/user token có quyền Instagram asset cho Bot 2."
                        )
                    else:
                        user_guidance = (
                            "Bài post/reel này không tương thích với mục tiêu/đích chạy hiện tại trên Meta API.\n"
                            "Em đã thử fallback destination tự động nhưng Meta vẫn từ chối.\n"
                            "Anh đổi sang bài khác hoặc xử lý qua Ads Manager UI cho bài này."
                        )
                else:
                    user_guidance = "Anh kiểm tra lại token/quyền Meta API, audiences và message template."
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Tạo campaign nháp thất bại, đã rollback.\n"
                        f"Lỗi: {exc}\n"
                        f"{user_guidance}"
                    ),
                )


    async def _start_existing_campaign_draft_flow(
            self,
            chat_id: int,
            command: AdsCommand,
            post_fingerprint: str,
            version: int,
            source_request_id: str | None = None,
        ) -> None:
            try:
                objective = self._load_objective_config()
                sku_prefix = resolve_sku_prefix(objective)
                resolved_post = await asyncio.to_thread(self.meta.resolve_post, command.post_url)
                await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
                keywords = self._resolve_existing_campaign_keywords(command, resolved_post.message_text, sku_prefix)
                campaign_candidates = await asyncio.to_thread(
                    self.meta.find_active_campaigns_by_keywords,
                    keywords,
                )
                if not campaign_candidates:
                    camp_hint = str(command.existing_campaign_hint or "").strip()
                    if camp_hint:
                        raise ValidationError(
                            f"Không tìm thấy campaign ACTIVE khớp gợi ý `camp {camp_hint}`.\n"
                            "Anh kiểm tra lại từ khóa hint hoặc tên campaign rồi gửi lại giúp em."
                        )
                    raise ValidationError(
                        "Không tìm thấy campaign ACTIVE chứa đủ SKU yêu cầu.\n"
                        "Anh kiểm tra lại mã SKU, hoặc nhập SKU cụ thể hơn rồi gửi lại."
                    )

                if len(campaign_candidates) > 1:
                    limited_candidates = campaign_candidates[:10]
                    request_id = await asyncio.to_thread(
                        self.storage.create_pending_request,
                        {
                            "post_url": command.post_url,
                            "budget_daily_vnd": command.budget_daily_vnd,
                            "post_fingerprint": post_fingerprint,
                            "version": version,
                            "use_existing_campaign": True,
                            "manual_sku_keywords": list(command.manual_sku_keywords),
                            "existing_campaign_hint": str(command.existing_campaign_hint or ""),
                            "campaign_match_keywords": keywords,
                            "campaign_candidates": limited_candidates,
                            "campaign_candidate_total": len(campaign_candidates),
                        },
                        "existing_campaign_select",
                    )
                    if source_request_id:
                        await asyncio.to_thread(self.storage.delete_pending_request, source_request_id)
                    if not self._bot:
                        raise RuntimeError("Telegram bot chua duoc khoi tao.")
                    labels = [self._campaign_candidate_label(item, index) for index, item in enumerate(limited_candidates)]
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=self._build_campaign_select_message(
                            keywords=keywords,
                            candidates=limited_candidates,
                            total_count=len(campaign_candidates),
                        ),
                        reply_markup=self.approval.existing_campaign_select_keyboard(
                            request_id=request_id,
                            campaign_options=labels,
                        ),
                    )
                    return

                await self._create_existing_campaign_draft_and_send_review(
                    chat_id=chat_id,
                    command=command,
                    post_fingerprint=post_fingerprint,
                    version=version,
                    selected_campaign=campaign_candidates[0],
                    campaign_keywords=keywords,
                    request_id=source_request_id,
                )
            except (ValidationError, MetaApiError, ValueError, KeyError, TypeError) as exc:
                self.logger.exception("Tạo nháp campaign cũ thất bại")
                if source_request_id:
                    await asyncio.to_thread(self.storage.delete_pending_request, source_request_id)
                failed_job_id = await asyncio.to_thread(self.storage.generate_job_id)
                failed_payload = {
                    "job_id": failed_job_id,
                    "status": "failed",
                    "version": version,
                    "campaign_mode": "existing",
                    "publish_scope": "ads_only",
                    "post_url": command.post_url,
                    "post_fingerprint": post_fingerprint,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "campaign_id": None,
                    "adset_ids": [],
                    "ad_ids": [],
                    "creative_ids": [],
                    "error": str(exc),
                    "failed_at": now_utc_iso(),
                }
                await asyncio.to_thread(self.storage.save_job, failed_payload, "failed")

                if not self._bot:
                    raise RuntimeError("Telegram bot chua duoc khoi tao.")
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Lên campaign cũ thất bại.\n"
                        f"Lỗi: {exc}\n"
                        "Anh kiểm tra lại SKU/campaign ACTIVE hoặc quyền Meta API rồi chạy lại giúp em."
                    ),
                )


    async def _create_existing_campaign_draft_and_send_review(
            self,
            chat_id: int,
            command: AdsCommand,
            post_fingerprint: str,
            version: int,
            selected_campaign: dict[str, str],
            campaign_keywords: list[str],
            request_id: str | None = None,
        ) -> None:
            objective = self._load_objective_config()
            templates = load_json(self.settings.message_templates_path)
            audiences = load_json(self.settings.audiences_config_path)
            sku_prefix = resolve_sku_prefix(objective)

            campaign_id = str(selected_campaign.get("id", "")).strip()
            selected_campaign_name = str(selected_campaign.get("name", "")).strip() or campaign_id
            selected_adset_ids: list[str] = []
            created_adset_ids: list[str] = []
            ad_ids: list[str] = []
            creative_ids: list[str] = []
            resolved_post = None
            account_multi_destination_asset_feed_spec: dict[str, Any] | None = None
            account_multi_destination_asset_feed_spec_checked = False
            account_multi_destination_creative_overrides: dict[str, Any] | None = None
            account_multi_destination_creative_overrides_checked = False
            account_message_destination_creative_overrides: dict[str, Any] | None = None
            account_message_destination_creative_overrides_checked = False
            instagram_post_access_diagnostics: dict[str, Any] | None = None
            instagram_post_access_diagnostics_checked = False
            reusable_story_creative_id: str | None = None
            reusable_story_creative_checked = False
            skipped_multi_destination_adsets = 0
            expected_page_welcome_message: Any | None = None

            try:
                if not campaign_id:
                    raise ValidationError("Campaign cũ không hợp lệ, thiếu campaign_id.")

                resolved_post = await asyncio.to_thread(self.meta.resolve_post, command.post_url)
                await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
                active_keywords = campaign_keywords or self._resolve_existing_campaign_keywords(
                    command,
                    resolved_post.message_text,
                    sku_prefix,
                )
                plan = build_existing_campaign_plan(
                    command=command,
                    resolved_post=resolved_post,
                    post_fingerprint=post_fingerprint,
                    version=version,
                    timezone_name=self.settings.app_timezone,
                    objective_config=objective,
                    template_config=templates,
                    sku_keywords=active_keywords,
                )
                creative_payload_overrides = plan.raw.get("creative_payload_overrides", {})
                expected_page_welcome_message = (
                    creative_payload_overrides.get("page_welcome_message")
                    if isinstance(creative_payload_overrides, dict)
                    else None
                )
                expected_reusable_call_to_action_type = (
                    "MESSAGE_PAGE"
                    if self._should_force_message_page_cta_for_post(plan, resolved_post)
                    else None
                )
                force_message_page_cta = expected_reusable_call_to_action_type == "MESSAGE_PAGE"
                ad_name_sku_code_text = "ALL" if str(command.existing_campaign_hint or "").strip() else plan.sku_code_text
                requested_destination_type = "INHERIT_ADSET"
                active_plan = plan
                active_destination_type = "INHERIT_ADSET"
                destination_fallback_reason = ""
                configured_existing_destination_type = str(
                    plan.raw.get("adset_payload_overrides", {}).get("destination_type", "")
                ).strip().upper()
                adsets = await asyncio.to_thread(self.meta.list_eligible_adsets, campaign_id, 20)
                if not adsets:
                    raise ValidationError(
                        "Campaign đã chọn không có adset hợp lệ (ACTIVE/PAUSED) để lên ads."
                    )
                selected_adset_ids = [str(item.get("id", "")).strip() for item in adsets if str(item.get("id", "")).strip()]
                non_jc_suffix = build_non_jc_hashtag_suffix(resolved_post.message_text, sku_prefix)
                story_id_candidates = self._build_story_id_candidates(resolved_post, command.post_url)

                async def _get_account_asset_feed_spec_cached() -> dict[str, Any] | None:
                    nonlocal account_multi_destination_asset_feed_spec
                    nonlocal account_multi_destination_asset_feed_spec_checked
                    if account_multi_destination_asset_feed_spec_checked:
                        return account_multi_destination_asset_feed_spec
                    account_multi_destination_asset_feed_spec_checked = True
                    try:
                        account_multi_destination_asset_feed_spec = await asyncio.to_thread(
                            self.meta.get_account_multi_destination_asset_feed_spec
                        )
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong lay duoc asset_feed_spec cap account cho luong len cu: %s",
                            exc,
                        )
                        account_multi_destination_asset_feed_spec = None
                    return account_multi_destination_asset_feed_spec

                async def _get_account_creative_overrides_cached() -> dict[str, Any] | None:
                    nonlocal account_multi_destination_creative_overrides
                    nonlocal account_multi_destination_creative_overrides_checked
                    if account_multi_destination_creative_overrides_checked:
                        return account_multi_destination_creative_overrides
                    account_multi_destination_creative_overrides_checked = True
                    try:
                        account_multi_destination_creative_overrides = await asyncio.to_thread(
                            self.meta.get_account_multi_destination_creative_overrides,
                            expected_call_to_action_type=expected_reusable_call_to_action_type,
                        )
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong lay duoc creative seed cap account cho luong len cu: %s",
                            exc,
                        )
                        account_multi_destination_creative_overrides = None
                    return account_multi_destination_creative_overrides

                async def _get_account_message_creative_overrides_cached() -> dict[str, Any] | None:
                    nonlocal account_message_destination_creative_overrides
                    nonlocal account_message_destination_creative_overrides_checked
                    if account_message_destination_creative_overrides_checked:
                        return account_message_destination_creative_overrides
                    account_message_destination_creative_overrides_checked = True
                    try:
                        account_message_destination_creative_overrides = await asyncio.to_thread(
                            self.meta.get_account_message_destination_creative_overrides,
                            expected_call_to_action_type=expected_reusable_call_to_action_type,
                        )
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong lay duoc creative seed Messenger cap account cho luong len cu: %s",
                            exc,
                        )
                        account_message_destination_creative_overrides = None
                    return account_message_destination_creative_overrides

                async def _build_messenger_asset_feed_retry_overrides(
                    base_overrides: dict[str, Any] | None,
                ) -> dict[str, Any] | None:
                    account_seed_overrides = await _get_account_creative_overrides_cached()
                    account_asset_feed_spec = (
                        account_seed_overrides.get("asset_feed_spec")
                        if isinstance(account_seed_overrides, dict)
                        else None
                    )
                    if not isinstance(account_asset_feed_spec, dict) or not account_asset_feed_spec:
                        account_asset_feed_spec = await _get_account_asset_feed_spec_cached()
                    if not isinstance(account_asset_feed_spec, dict) or not account_asset_feed_spec:
                        return None

                    messenger_asset_feed_spec = self._as_messenger_only_asset_feed_spec(
                        account_asset_feed_spec,
                        self.settings.meta_page_id,
                    )
                    if not messenger_asset_feed_spec:
                        return None

                    retry_overrides: dict[str, Any] = {
                        "asset_feed_spec": messenger_asset_feed_spec,
                    }
                    message_seed_overrides = await _get_account_message_creative_overrides_cached()
                    for source in (
                        message_seed_overrides if isinstance(message_seed_overrides, dict) else {},
                        base_overrides if isinstance(base_overrides, dict) else {},
                        {"page_welcome_message": expected_page_welcome_message},
                    ):
                        for field in ("page_welcome_message", "instagram_user_id", "instagram_actor_id"):
                            if field in retry_overrides:
                                continue
                            value = source.get(field) if isinstance(source, dict) else None
                            if value not in (None, "", {}, []):
                                retry_overrides[field] = value

                    retry_overrides.pop("call_to_action_type", None)
                    retry_overrides.pop("degrees_of_freedom_spec", None)
                    retry_overrides.pop("contextual_multi_ads", None)
                    return retry_overrides

                async def _get_instagram_post_access_diagnostics_cached() -> dict[str, Any]:
                    nonlocal instagram_post_access_diagnostics
                    nonlocal instagram_post_access_diagnostics_checked
                    if instagram_post_access_diagnostics_checked:
                        return dict(instagram_post_access_diagnostics or {})
                    instagram_post_access_diagnostics_checked = True
                    diagnose_fn = getattr(self.meta, "diagnose_instagram_post_access", None)
                    if not callable(diagnose_fn) or not resolved_post:
                        instagram_post_access_diagnostics = {}
                        return {}
                    try:
                        diagnostics = await asyncio.to_thread(
                            diagnose_fn,
                            resolved_post.object_story_id,
                        )
                        instagram_post_access_diagnostics = (
                            dict(diagnostics) if isinstance(diagnostics, dict) else {}
                        )
                    except Exception as diagnostics_exc:  # noqa: BLE001
                        self.logger.warning(
                            "Khong chan doan duoc quyen Instagram cho post %s: %s",
                            resolved_post.object_story_id,
                            diagnostics_exc,
                        )
                        instagram_post_access_diagnostics = {}
                    return dict(instagram_post_access_diagnostics or {})

                async def _get_reusable_story_creative_cached() -> str | None:
                    nonlocal reusable_story_creative_id
                    nonlocal reusable_story_creative_checked
                    if reusable_story_creative_checked:
                        return reusable_story_creative_id
                    reusable_story_creative_checked = True
                    try:
                        reusable_story_creative_id = await asyncio.to_thread(
                            self.meta.find_reusable_creative_id_by_story_ids,
                            story_id_candidates,
                            max_ads_scan=1600,
                            expected_page_welcome_message=expected_page_welcome_message,
                            expected_call_to_action_type=expected_reusable_call_to_action_type,
                        )
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong tra cuu duoc creative hop le cua cung bai post/reel: %s",
                            exc,
                        )
                        reusable_story_creative_id = None
                    return reusable_story_creative_id

                async def _try_duplicate_from_existing_ad(
                    adset_id: str,
                    slot: AudienceSlot,
                    trigger_error: str,
                    destination_type_override: str,
                ) -> str | None:
                    if not resolved_post:
                        return None
                    lookup_order: list[str] = []
                    seen_lookup: set[str] = set()
                    for lookup_adset_id in [adset_id, *selected_adset_ids]:
                        normalized_lookup = str(lookup_adset_id).strip()
                        if not normalized_lookup or normalized_lookup in seen_lookup:
                            continue
                        seen_lookup.add(normalized_lookup)
                        lookup_order.append(normalized_lookup)
                    matched_ad: dict[str, str] | None = None
                    matched_from_adset_id = adset_id
                    for lookup_adset_id in lookup_order:
                        try:
                            matched_ad = await asyncio.to_thread(
                                self.meta.find_latest_ad_by_story_ids,
                                story_id_candidates,
                                adset_id=lookup_adset_id,
                                max_ads_scan=1200,
                                expected_page_welcome_message=expected_page_welcome_message,
                                expected_call_to_action_type=expected_reusable_call_to_action_type,
                            )
                        except Exception as lookup_exc:  # noqa: BLE001
                            self.logger.warning(
                                "Khong tra cuu duoc ad fallback duplicate cho adset %s: %s",
                                lookup_adset_id,
                                lookup_exc,
                            )
                            continue
                        if matched_ad and str(matched_ad.get("id", "")).strip():
                            matched_from_adset_id = lookup_adset_id
                            break
                    if not matched_ad:
                        return None
                    source_ad_id = str(matched_ad.get("id", "")).strip()
                    if not source_ad_id:
                        return None
                    try:
                        copied_ad_id = await asyncio.to_thread(
                            self.meta.duplicate_ad_from_source,
                            source_ad_id,
                            slot.ad_name,
                            target_adset_id=adset_id,
                        )
                    except (MetaApiError, ValidationError) as copy_exc:
                        self.logger.warning(
                            "Fallback duplicate ad that bai cho adset %s (source=%s): %s",
                            adset_id,
                            source_ad_id,
                            copy_exc,
                        )
                        source_creative_id = str(matched_ad.get("creative_id", "")).strip()
                        if not source_creative_id:
                            return None
                        self.logger.warning(
                            "Thu fallback tao ad tu creative da duoc Meta duyet %s cho adset %s",
                            source_creative_id,
                            adset_id,
                        )
                        try:
                            return await asyncio.to_thread(
                                self.meta.create_ad,
                                active_plan,
                                slot,
                                adset_id,
                                source_creative_id,
                                destination_type_override,
                            )
                        except MetaApiError as reuse_exc:
                            self.logger.warning(
                                "Fallback dung lai creative %s that bai cho adset %s: %s",
                                source_creative_id,
                                adset_id,
                                reuse_exc,
                            )
                            return None
                    source_ad_name = str(matched_ad.get("name", "")).strip()
                    source_ref = source_ad_id if not source_ad_name else f"{source_ad_id} ({source_ad_name})"
                    self.logger.warning(
                        "Fallback len cu duplicate ad %s -> %s cho adset %s do loi: %s",
                        source_ref,
                        copied_ad_id,
                        matched_from_adset_id,
                        trigger_error,
                    )
                    return copied_ad_id

                if configured_existing_destination_type != "MESSENGER":
                    has_multi_destination_adsets = any(
                        str(item.get("destination_type", "")).strip().upper()
                        == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                        for item in adsets
                        if isinstance(item, dict)
                    )
                    if has_multi_destination_adsets:
                        diagnostics = await _get_instagram_post_access_diagnostics_cached()
                        if (
                            diagnostics.get("is_instagram_origin")
                            and not diagnostics.get("has_instagram_media_access")
                        ):
                            configured_existing_destination_type = "MESSENGER"
                            active_plan = self._plan_with_destination_override(active_plan, "MESSENGER")
                            active_destination_type = "MESSENGER"
                            destination_fallback_reason = (
                                "Reel này là bài Instagram liên kết; token cloud chưa đọc được IG media, "
                                "nên bot tạo adset Messenger-only mới trong campaign cũ."
                            )
                            self.logger.warning(
                                "Tu dong tao adset Messenger-only trong campaign cu %s vi post %s la IG-origin "
                                "va token cloud khong co quyen doc IG media.",
                                campaign_id,
                                resolved_post.object_story_id,
                            )

                for index, adset in enumerate(adsets, start=1):
                    adset_id = str(adset.get("id", "")).strip()
                    if not adset_id:
                        continue
                    adset_name = str(adset.get("name", "")).strip() or adset_id
                    adset_destination_type = str(adset.get("destination_type", "")).strip().upper() or "MESSENGER"
                    if (
                        configured_existing_destination_type == "MESSENGER"
                        and adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                    ):
                        layout = self._resolve_audience_layout_for_adset(adset_name, skipped_multi_destination_adsets)
                        if not layout:
                            raise ValidationError(
                                "Không map được adset đa đích cũ sang audience Messenger-only mới."
                            )
                        audience_key, audience_label, audience_suffix = layout
                        saved_audience_id = str(audiences.get(audience_key, "")).strip()
                        if not saved_audience_id or saved_audience_id == "replace_me":
                            raise ValidationError(
                                f"Saved Audience ID cho '{audience_label}' chưa được cấu hình trong audiences.json."
                            )
                        fallback_slot = AudienceSlot(
                            key=f"existing_messenger_{index}",
                            label=audience_label,
                            suffix=audience_suffix,
                            saved_audience_id=saved_audience_id,
                            adset_name=f"{selected_campaign_name} - {audience_label} | Messenger-only",
                            ad_name="",
                        )
                        new_adset_id = await asyncio.to_thread(
                            self.meta.create_adset,
                            active_plan,
                            campaign_id,
                            fallback_slot,
                        )
                        if not new_adset_id:
                            raise ValidationError(
                                f"Không tạo được adset Messenger-only cho '{audience_label}'."
                            )
                        created_adset_ids.append(new_adset_id)
                        self.logger.warning(
                            "Tao adset Messenger-only %s tu adset multi-destination %s vi profile dang cau hinh MESSENGER-only.",
                            new_adset_id,
                            adset_id,
                        )
                        skipped_multi_destination_adsets += 1
                        active_destination_type = "MESSENGER"
                        if not destination_fallback_reason:
                            destination_fallback_reason = (
                                "Campaign cũ chỉ có adset đa đích Messenger + Instagram; "
                                "bot đã tạo adset Messenger-only mới trong cùng campaign."
                            )
                        adset_id = new_adset_id
                        adset_name = fallback_slot.adset_name
                        adset_destination_type = "MESSENGER"
                    creative_extra_overrides: dict[str, Any] = {}
                    if adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                        if force_message_page_cta:
                            try:
                                seed_overrides = await asyncio.to_thread(
                                    self.meta.get_multi_destination_creative_overrides,
                                    adset_id,
                                    expected_call_to_action_type="MESSAGE_PAGE",
                                )
                                creative_extra_overrides.update(seed_overrides)
                            except (ValidationError, MetaApiError) as exc:
                                self.logger.warning(
                                    "Khong lay duoc creative seed MESSAGE_PAGE tu adset %s: %s",
                                    adset_id,
                                    exc,
                                )
                        if "asset_feed_spec" not in creative_extra_overrides:
                            if force_message_page_cta:
                                account_seed_overrides = await _get_account_creative_overrides_cached()
                                if isinstance(account_seed_overrides, dict) and account_seed_overrides:
                                    creative_extra_overrides.update(account_seed_overrides)
                            else:
                                try:
                                    asset_feed_spec = await asyncio.to_thread(
                                        self.meta.get_multi_destination_asset_feed_spec,
                                        adset_id,
                                    )
                                    creative_extra_overrides["asset_feed_spec"] = self._with_message_page_cta_preferred(
                                        asset_feed_spec
                                    )
                                except (ValidationError, MetaApiError) as exc:
                                    self.logger.warning(
                                        "Khong lay duoc asset_feed_spec tu adset %s: %s",
                                        adset_id,
                                        exc,
                                    )
                        if "asset_feed_spec" not in creative_extra_overrides:
                            account_asset_feed_spec = await _get_account_asset_feed_spec_cached()
                            if isinstance(account_asset_feed_spec, dict) and account_asset_feed_spec:
                                creative_extra_overrides["asset_feed_spec"] = self._with_message_page_cta_preferred(
                                    account_asset_feed_spec
                                )
                        if force_message_page_cta:
                            creative_extra_overrides["call_to_action_type"] = "MESSAGE_PAGE"
                    elif adset_destination_type == "MESSENGER" and force_message_page_cta:
                        try:
                            message_seed_overrides = await asyncio.to_thread(
                                self.meta.get_message_destination_creative_overrides,
                                adset_id,
                                expected_call_to_action_type="MESSAGE_PAGE",
                            )
                            creative_extra_overrides.update(message_seed_overrides)
                        except (ValidationError, MetaApiError) as exc:
                            self.logger.warning(
                                "Khong lay duoc creative seed Messenger MESSAGE_PAGE tu adset %s: %s",
                                adset_id,
                                exc,
                            )
                            account_message_seed_overrides = await _get_account_message_creative_overrides_cached()
                            if isinstance(account_message_seed_overrides, dict) and account_message_seed_overrides:
                                creative_extra_overrides.update(account_message_seed_overrides)
                        creative_extra_overrides["call_to_action_type"] = "MESSAGE_PAGE"
                    slot = AudienceSlot(
                        key=f"existing_{index}",
                        label=adset_name,
                        suffix="EX",
                        saved_audience_id="",
                        adset_name=adset_name,
                        ad_name=(
                            f"ADS:QUYET|MK:ThaiLan|SKU:{ad_name_sku_code_text}|MED:{plan.media_label}|ADSET:{adset_id}"
                            f"{non_jc_suffix}"
                        ),
                    )

                    creative_id: str | None = None
                    ad_id: str | None = None
                    source_creative_id = await _get_reusable_story_creative_cached()
                    if source_creative_id:
                        try:
                            ad_id = await asyncio.to_thread(
                                self.meta.create_ad,
                                active_plan,
                                slot,
                                adset_id,
                                source_creative_id,
                                adset_destination_type,
                            )
                        except MetaApiError as reuse_exc:
                            self.logger.warning(
                                "Creative %s cua cung post/reel khong gan duoc vao adset %s; "
                                "thu tao creative moi: %s",
                                source_creative_id,
                                adset_id,
                                reuse_exc,
                            )
                        else:
                            self.logger.info(
                                "Tai su dung creative %s cua cung post/reel cho adset cu %s.",
                                source_creative_id,
                                adset_id,
                            )
                            ad_ids.append(ad_id)
                            continue
                    try:
                        creative_id = await asyncio.to_thread(
                            self.meta.create_ad_creative,
                            active_plan,
                            slot,
                            resolved_post,
                            adset_destination_type,
                            creative_extra_overrides,
                        )
                        ad_id = await asyncio.to_thread(
                            self.meta.create_ad,
                            active_plan,
                            slot,
                            adset_id,
                            creative_id,
                            adset_destination_type,
                        )
                    except MetaApiError as exc:
                        error_text = str(exc)
                        if self.meta.is_link_ad_cta_locked_error(error_text):
                            can_retry_without_asset_feed_spec = (
                                adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                                and "asset_feed_spec" in creative_extra_overrides
                            )
                            if can_retry_without_asset_feed_spec:
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                retry_overrides = dict(creative_extra_overrides)
                                retry_overrides.pop("asset_feed_spec", None)
                                retry_extra_overrides = retry_overrides if retry_overrides else None
                                self.logger.warning(
                                    "Retry len cu bo asset_feed_spec cho adset %s do loi CTA link-ad lock: %s",
                                    adset_id,
                                    exc,
                                )
                                creative_id = await asyncio.to_thread(
                                    self.meta.create_ad_creative,
                                    active_plan,
                                    slot,
                                    resolved_post,
                                    adset_destination_type,
                                    retry_extra_overrides,
                                )
                                ad_id = await asyncio.to_thread(
                                    self.meta.create_ad,
                                    active_plan,
                                    slot,
                                    adset_id,
                                    creative_id,
                                    adset_destination_type,
                                )
                            else:
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                raise
                        elif self.meta.is_post_not_advertisable_error(error_text):
                            if (
                                adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                            ):
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                active_destination_type = "MESSENGER"
                                destination_fallback_reason = error_text
                                self.logger.warning(
                                    "Fallback len cu sang MESSENGER (bo asset_feed_spec) cho adset %s do loi post invalid: %s",
                                    adset_id,
                                    exc,
                                )
                                creative_id = await asyncio.to_thread(
                                    self.meta.create_ad_creative,
                                    active_plan,
                                    slot,
                                    resolved_post,
                                    "MESSENGER",
                                    None,
                                )
                                try:
                                    ad_id = await asyncio.to_thread(
                                        self.meta.create_ad,
                                        active_plan,
                                        slot,
                                        adset_id,
                                        creative_id,
                                        adset_destination_type,
                                    )
                                except MetaApiError as ad_exc:
                                    if self.meta.is_auto_destination_error(str(ad_exc)):
                                        self.logger.warning(
                                            "Retry fallback len cu voi payload ad MESSENGER cho adset %s do loi objective: %s",
                                            adset_id,
                                            ad_exc,
                                        )
                                        try:
                                            ad_id = await asyncio.to_thread(
                                                self.meta.create_ad,
                                                active_plan,
                                                slot,
                                                adset_id,
                                                creative_id,
                                                "MESSENGER",
                                            )
                                        except MetaApiError as retry_ad_exc:
                                            retry_error_text = str(retry_ad_exc)
                                            if (
                                                self.meta.is_auto_destination_error(retry_error_text)
                                                or self.meta.is_post_not_advertisable_error(retry_error_text)
                                            ):
                                                if creative_id:
                                                    await asyncio.to_thread(
                                                        self.rollback.rollback,
                                                        None,
                                                        [],
                                                        [],
                                                        [creative_id],
                                                    )
                                                    creative_id = None
                                                duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                                    adset_id,
                                                    slot,
                                                    retry_error_text,
                                                    adset_destination_type,
                                                )
                                                if duplicated_ad_id:
                                                    ad_id = duplicated_ad_id
                                                else:
                                                    raise
                                            else:
                                                raise
                                    elif self.meta.is_post_not_advertisable_error(str(ad_exc)):
                                        if creative_id:
                                            await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                            creative_id = None
                                        duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                            adset_id,
                                            slot,
                                            str(ad_exc),
                                            adset_destination_type,
                                        )
                                        if duplicated_ad_id:
                                            ad_id = duplicated_ad_id
                                        else:
                                            raise
                                    else:
                                        if ad_id:
                                            await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                            ad_id = None
                                        if creative_id:
                                            await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                            creative_id = None
                                        raise
                            else:
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                    adset_id,
                                    slot,
                                    error_text,
                                    adset_destination_type,
                                )
                                if duplicated_ad_id:
                                    ad_id = duplicated_ad_id
                                else:
                                    raise
                        elif (
                            adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                            and self.meta.is_auto_destination_error(error_text)
                        ):
                            account_seed_overrides = (
                                await _get_account_creative_overrides_cached()
                                if force_message_page_cta
                                else None
                            )
                            account_asset_feed_spec = (
                                account_seed_overrides.get("asset_feed_spec")
                                if isinstance(account_seed_overrides, dict)
                                else await _get_account_asset_feed_spec_cached()
                            )
                            can_retry_with_account_spec = (
                                isinstance(account_asset_feed_spec, dict)
                                and bool(account_asset_feed_spec)
                                and creative_extra_overrides.get("asset_feed_spec") != account_asset_feed_spec
                            )
                            retry_error: MetaApiError = exc
                            if can_retry_with_account_spec:
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                retry_overrides = dict(creative_extra_overrides)
                                if isinstance(account_seed_overrides, dict) and account_seed_overrides:
                                    retry_overrides.update(account_seed_overrides)
                                else:
                                    retry_overrides["asset_feed_spec"] = account_asset_feed_spec
                                self.logger.warning(
                                    "Retry len cu voi asset_feed_spec cap account cho adset %s do loi auto-destination: %s",
                                    adset_id,
                                    exc,
                                )
                                creative_id = await asyncio.to_thread(
                                    self.meta.create_ad_creative,
                                    active_plan,
                                    slot,
                                    resolved_post,
                                    adset_destination_type,
                                    retry_overrides,
                                )
                                try:
                                    ad_id = await asyncio.to_thread(
                                        self.meta.create_ad,
                                        active_plan,
                                        slot,
                                        adset_id,
                                        creative_id,
                                        adset_destination_type,
                                    )
                                except MetaApiError as account_retry_exc:
                                    retry_error = account_retry_exc
                                    retry_error_text = str(account_retry_exc)
                                    if not (
                                        self.meta.is_auto_destination_error(retry_error_text)
                                        or self.meta.is_post_not_advertisable_error(retry_error_text)
                                    ):
                                        if creative_id:
                                            await asyncio.to_thread(
                                                self.rollback.rollback,
                                                None,
                                                [],
                                                [],
                                                [creative_id],
                                            )
                                            creative_id = None
                                        raise

                            if not ad_id:
                                if ad_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                    ad_id = None
                                if creative_id:
                                    await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                    creative_id = None
                                active_destination_type = "MESSENGER"
                                destination_fallback_reason = str(retry_error)
                                self.logger.warning(
                                    "Fallback len cu sang creative/ad MESSENGER cho adset %s "
                                    "do loi objective tren multi-destination: %s",
                                    adset_id,
                                    retry_error,
                                )
                                try:
                                    creative_id = await asyncio.to_thread(
                                        self.meta.create_ad_creative,
                                        active_plan,
                                        slot,
                                        resolved_post,
                                        "MESSENGER",
                                        None,
                                    )
                                    ad_id = await asyncio.to_thread(
                                        self.meta.create_ad,
                                        active_plan,
                                        slot,
                                        adset_id,
                                        creative_id,
                                        "MESSENGER",
                                    )
                                except MetaApiError as messenger_exc:
                                    messenger_error_text = str(messenger_exc)
                                    if creative_id:
                                        await asyncio.to_thread(
                                            self.rollback.rollback,
                                            None,
                                            [],
                                            [],
                                            [creative_id],
                                        )
                                        creative_id = None
                                    if (
                                        self.meta.is_auto_destination_error(messenger_error_text)
                                        or self.meta.is_post_not_advertisable_error(messenger_error_text)
                                    ):
                                        duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                            adset_id,
                                            slot,
                                            messenger_error_text,
                                            "MESSENGER",
                                        )
                                        if duplicated_ad_id:
                                            ad_id = duplicated_ad_id
                                        else:
                                            raise
                                    else:
                                        raise
                        elif self.meta.is_auto_destination_error(error_text):
                            if ad_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                ad_id = None
                            if creative_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                creative_id = None
                            if adset_destination_type == "MESSENGER" and force_message_page_cta:
                                retry_overrides = await _build_messenger_asset_feed_retry_overrides(
                                    creative_extra_overrides,
                                )
                                if retry_overrides:
                                    try:
                                        self.logger.warning(
                                            "Retry len cu Messenger-only bang asset_feed_spec MESSAGE_PAGE "
                                            "cho adset %s do loi auto-destination: %s",
                                            adset_id,
                                            exc,
                                        )
                                        creative_id = await asyncio.to_thread(
                                            self.meta.create_ad_creative,
                                            active_plan,
                                            slot,
                                            resolved_post,
                                            "MESSENGER",
                                            retry_overrides,
                                        )
                                        ad_id = await asyncio.to_thread(
                                            self.meta.create_ad,
                                            active_plan,
                                            slot,
                                            adset_id,
                                            creative_id,
                                            "MESSENGER",
                                        )
                                        destination_fallback_reason = error_text
                                    except MetaApiError as retry_exc:
                                        self.logger.warning(
                                            "Retry asset_feed_spec MESSAGE_PAGE that bai cho adset %s: %s",
                                            adset_id,
                                            retry_exc,
                                        )
                                        if ad_id:
                                            await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                            ad_id = None
                                        if creative_id:
                                            await asyncio.to_thread(
                                                self.rollback.rollback,
                                                None,
                                                [],
                                                [],
                                                [creative_id],
                                            )
                                            creative_id = None

                            if not ad_id:
                                duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                    adset_id,
                                    slot,
                                    error_text,
                                    adset_destination_type,
                                )
                                if duplicated_ad_id:
                                    ad_id = duplicated_ad_id
                                    destination_fallback_reason = error_text
                            if not ad_id:
                                diagnostics = await _get_instagram_post_access_diagnostics_cached()
                                if (
                                    diagnostics.get("is_instagram_origin")
                                    and not diagnostics.get("has_instagram_media_access")
                                ):
                                    if adset_destination_type != "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                                        raise ValidationError(
                                            "Meta API không có quyền đọc bài Instagram gốc của reel liên kết này.\n"
                                            "Token cloud đang thiếu quyền `instagram_basic` hoặc tài khoản Instagram "
                                            "chưa được gán cho token/ad account, nên Ads Manager UI lên được nhưng API bị chặn.\n"
                                            "Em chưa tìm thấy ad thủ công nào dùng đúng bài này để tái sử dụng creative.\n"
                                            "Anh tạo 1 ad PAUSED thủ công từ reel này trong campaign rồi chạy lại; "
                                            "bot sẽ dùng creative đã được Meta duyệt để tạo ads cho các adset còn lại."
                                        ) from exc
                                    layout = self._resolve_audience_layout_for_adset(
                                        adset_name,
                                        skipped_multi_destination_adsets,
                                    )
                                    if not layout:
                                        raise ValidationError(
                                            "Không map được adset đa đích cũ sang audience Messenger-only mới."
                                        ) from exc
                                    audience_key, audience_label, audience_suffix = layout
                                    saved_audience_id = str(audiences.get(audience_key, "")).strip()
                                    if not saved_audience_id or saved_audience_id == "replace_me":
                                        raise ValidationError(
                                            f"Saved Audience ID cho '{audience_label}' chưa được cấu hình trong audiences.json."
                                        ) from exc
                                    fallback_slot = AudienceSlot(
                                        key=f"existing_messenger_retry_{index}",
                                        label=audience_label,
                                        suffix=audience_suffix,
                                        saved_audience_id=saved_audience_id,
                                        adset_name=f"{selected_campaign_name} - {audience_label} | Messenger-only",
                                        ad_name="",
                                    )
                                    messenger_plan = self._plan_with_destination_override(active_plan, "MESSENGER")
                                    new_adset_id = await asyncio.to_thread(
                                        self.meta.create_adset,
                                        messenger_plan,
                                        campaign_id,
                                        fallback_slot,
                                    )
                                    if not new_adset_id:
                                        raise ValidationError(
                                            f"Không tạo được adset Messenger-only cho '{audience_label}'."
                                        ) from exc
                                    created_adset_ids.append(new_adset_id)
                                    skipped_multi_destination_adsets += 1
                                    active_plan = messenger_plan
                                    active_destination_type = "MESSENGER"
                                    destination_fallback_reason = (
                                        "Reel này là bài Instagram liên kết; token cloud chưa đọc được IG media, "
                                        "nên bot tạo adset Messenger-only mới trong campaign cũ."
                                    )
                                    messenger_slot = AudienceSlot(
                                        key=f"existing_messenger_retry_{index}_ad",
                                        label=f"{audience_label} | Messenger-only",
                                        suffix=audience_suffix,
                                        saved_audience_id=saved_audience_id,
                                        adset_name=fallback_slot.adset_name,
                                        ad_name=(
                                            f"ADS:QUYET|MK:ThaiLan|SKU:{ad_name_sku_code_text}|"
                                            f"MED:{plan.media_label}|ADSET:{new_adset_id}{non_jc_suffix}"
                                        ),
                                    )
                                    self.logger.warning(
                                        "Retry tao adset Messenger-only %s trong campaign cu %s cho IG-origin reel %s "
                                        "sau khi adset da dich bi Meta chan.",
                                        new_adset_id,
                                        campaign_id,
                                        resolved_post.object_story_id,
                                    )
                                    creative_id = await asyncio.to_thread(
                                        self.meta.create_ad_creative,
                                        messenger_plan,
                                        messenger_slot,
                                        resolved_post,
                                        "MESSENGER",
                                        None,
                                    )
                                    ad_id = await asyncio.to_thread(
                                        self.meta.create_ad,
                                        messenger_plan,
                                        messenger_slot,
                                        new_adset_id,
                                        creative_id,
                                        "MESSENGER",
                                    )
                                if not ad_id:
                                    raise
                        elif (
                            adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                            and self.meta.is_instagram_media_requirement_error(error_text)
                        ):
                            if ad_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                ad_id = None
                            if creative_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                creative_id = None
                            active_destination_type = "MESSENGER"
                            destination_fallback_reason = str(exc)
                            self.logger.warning(
                                "Fallback len cu sang MESSENGER cho adset %s do loi media Instagram: %s",
                                adset_id,
                                exc,
                            )
                            creative_id = await asyncio.to_thread(
                                self.meta.create_ad_creative,
                                active_plan,
                                slot,
                                resolved_post,
                                "MESSENGER",
                                None,
                            )
                            try:
                                ad_id = await asyncio.to_thread(
                                    self.meta.create_ad,
                                    active_plan,
                                    slot,
                                    adset_id,
                                    creative_id,
                                    adset_destination_type,
                                )
                            except MetaApiError as ad_exc:
                                if self.meta.is_auto_destination_error(str(ad_exc)):
                                    self.logger.warning(
                                        "Retry fallback len cu voi payload ad MESSENGER cho adset %s do loi objective: %s",
                                        adset_id,
                                        ad_exc,
                                    )
                                    try:
                                        ad_id = await asyncio.to_thread(
                                            self.meta.create_ad,
                                            active_plan,
                                            slot,
                                            adset_id,
                                            creative_id,
                                            "MESSENGER",
                                        )
                                    except MetaApiError as retry_ad_exc:
                                        retry_error_text = str(retry_ad_exc)
                                        if (
                                            self.meta.is_auto_destination_error(retry_error_text)
                                            or self.meta.is_post_not_advertisable_error(retry_error_text)
                                        ):
                                            if creative_id:
                                                await asyncio.to_thread(
                                                    self.rollback.rollback,
                                                    None,
                                                    [],
                                                    [],
                                                    [creative_id],
                                                )
                                                creative_id = None
                                            duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                                adset_id,
                                                slot,
                                                retry_error_text,
                                                "MESSENGER",
                                            )
                                            if duplicated_ad_id:
                                                ad_id = duplicated_ad_id
                                            else:
                                                raise
                                        else:
                                            raise
                                elif self.meta.is_post_not_advertisable_error(str(ad_exc)):
                                    if creative_id:
                                        await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                        creative_id = None
                                    duplicated_ad_id = await _try_duplicate_from_existing_ad(
                                        adset_id,
                                        slot,
                                        str(ad_exc),
                                        "MESSENGER",
                                    )
                                    if duplicated_ad_id:
                                        ad_id = duplicated_ad_id
                                    else:
                                        raise
                                else:
                                    if ad_id:
                                        await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                        ad_id = None
                                    if creative_id:
                                        await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                        creative_id = None
                                    raise
                        else:
                            if ad_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                ad_id = None
                            if creative_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                creative_id = None
                            raise
                    if creative_id:
                        creative_ids.append(creative_id)
                    if ad_id:
                        ad_ids.append(ad_id)

                if not ad_ids:
                    if skipped_multi_destination_adsets:
                        raise ValidationError(
                            "Campaign cũ hiện chỉ có adset đa đích Messenger + Instagram, "
                            "nhưng bot chưa tạo được adset Messenger-only thay thế.\n"
                            "Anh kiểm tra lại Saved Audience/config ADS2 rồi chạy lại giúp em."
                        )
                    raise ValidationError("Không tạo được ads mới trong campaign đã chọn.")

                job_id = await asyncio.to_thread(self.storage.generate_job_id)
                job_payload = {
                    "job_id": job_id,
                    "status": "pending",
                    "version": version,
                    "campaign_name": selected_campaign_name,
                    "sku_code_text": ad_name_sku_code_text,
                    "media_label": plan.media_label,
                    "post_url": command.post_url,
                    "post_fingerprint": post_fingerprint,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "objective": active_plan.objective,
                    "conversion_location": active_plan.conversion_location,
                    "result_goal": active_plan.result_goal,
                    "message_template_name": active_plan.message_template_name,
                    "requested_destination_type": requested_destination_type,
                    "active_destination_type": active_destination_type,
                    "destination_fallback_reason": destination_fallback_reason,
                    "campaign_mode": "existing",
                    "campaign_match_keywords": active_keywords,
                    "selected_campaign_id": campaign_id,
                    "selected_campaign_name": selected_campaign_name,
                    "selected_adset_ids": selected_adset_ids,
                    "selected_adset_count": len(selected_adset_ids),
                    "publish_scope": "adsets_ads" if created_adset_ids else "ads_only",
                    "campaign_id": campaign_id,
                    "adset_ids": created_adset_ids,
                    "ad_ids": ad_ids,
                    "creative_ids": creative_ids,
                    "resolved_post_id": resolved_post.post_id,
                    "resolved_page_id": resolved_post.page_id,
                    "object_story_id": resolved_post.object_story_id,
                    "resolved_permalink_url": resolved_post.permalink_url,
                    "resolved_strategy": resolved_post.strategy,
                    "ads_manager_url": self._build_ads_manager_url(campaign_id),
                    "created_at": now_utc_iso(),
                }
                await asyncio.to_thread(self.storage.save_job, job_payload, "pending")

                if request_id:
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

                if not self._bot:
                    raise RuntimeError("Telegram bot chua duoc khoi tao.")
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=self._build_review_message(job_payload),
                    reply_markup=self.approval.review_keyboard(job_id),
                )
            except (ValidationError, MetaApiError, ValueError, KeyError, TypeError) as exc:
                self.logger.exception("Tạo nháp campaign cũ thất bại")
                await asyncio.to_thread(self.rollback.rollback, None, created_adset_ids, ad_ids, creative_ids)
                failed_job_id = await asyncio.to_thread(self.storage.generate_job_id)
                failed_payload = {
                    "job_id": failed_job_id,
                    "status": "failed",
                    "version": version,
                    "campaign_mode": "existing",
                    "publish_scope": "ads_only",
                    "post_url": command.post_url,
                    "post_fingerprint": post_fingerprint,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "campaign_id": campaign_id,
                    "publish_scope": "adsets_ads" if created_adset_ids else "ads_only",
                    "selected_campaign_id": campaign_id,
                    "selected_campaign_name": selected_campaign_name,
                    "selected_adset_ids": selected_adset_ids,
                    "selected_adset_count": len(selected_adset_ids),
                    "adset_ids": created_adset_ids,
                    "ad_ids": ad_ids,
                    "creative_ids": creative_ids,
                    "error": str(exc),
                    "failed_at": now_utc_iso(),
                }
                await asyncio.to_thread(self.storage.save_job, failed_payload, "failed")
                if request_id:
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

                if not self._bot:
                    raise RuntimeError("Telegram bot chua duoc khoi tao.")
                error_text = str(exc)
                if self.meta.is_post_not_advertisable_error(error_text):
                    matched_ad: dict[str, str] | None = None
                    if resolved_post:
                        try:
                            adset_lookup_id = selected_adset_ids[0] if len(selected_adset_ids) == 1 else None
                            matched_ad = await asyncio.to_thread(
                                self.meta.find_latest_ad_by_story_ids,
                                self._build_story_id_candidates(resolved_post, command.post_url),
                                adset_id=adset_lookup_id,
                                max_ads_scan=1200,
                                expected_page_welcome_message=expected_page_welcome_message,
                            )
                        except Exception as lookup_exc:  # noqa: BLE001
                            self.logger.warning(
                                "Khong tra cuu duoc ad dang dung chung Post ID khi xu ly loi post khong quang cao duoc: %s",
                                lookup_exc,
                            )
                    if matched_ad and matched_ad.get("id"):
                        ad_id = str(matched_ad.get("id", "")).strip()
                        ad_name = str(matched_ad.get("name", "")).strip()
                        ad_ref = ad_id if not ad_name else f"{ad_id} ({ad_name})"
                        user_guidance = (
                            "Meta API đang chặn tạo thêm ad mới từ Post ID của reel này.\n"
                            f"Em thấy trong adset đã có ad dùng đúng post: {ad_ref}.\n"
                            "Anh có thể duplicate ad này trực tiếp trong Ads Manager (UI) để chạy ngay."
                        )
                    else:
                        user_guidance = (
                            "Meta API đang chặn Post ID của reel này cho thao tác tạo ad mới.\n"
                            "Trường hợp này thường vẫn thao tác được trên Ads Manager (UI), "
                            "nhưng API sẽ bị từ chối."
                        )
                elif self.meta.is_link_ad_cta_locked_error(error_text):
                    user_guidance = (
                        "Post này đang ở trạng thái link-ad nên API chặn chỉnh CTA khi tạo creative theo luồng hiện tại.\n"
                        "Trên Ads Manager UI anh vẫn có thể lên được do UI tự xử lý/duplicate theo luồng nội bộ của Meta."
                    )
                elif self.meta.is_instagram_media_requirement_error(error_text) or self.meta.is_auto_destination_error(error_text):
                    user_guidance = (
                        "Bài viết này không tương thích với objective/destination của adset hiện tại trong campaign cũ.\n"
                        "Theo rule lên cũ, em chỉ được tạo ad mới và không sửa campaign/adset cũ, nên không thể ép chạy bài này.\n"
                        "Anh đổi sang bài post/reel khác hoặc chọn campaign/adset khác rồi gửi lại giúp em."
                    )
                else:
                    user_guidance = "Anh kiểm tra lại token/quyền Meta API hoặc adset trong campaign rồi chạy lại."
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Tạo nháp campaign cũ thất bại, đã rollback ads/creative mới tạo.\n"
                        f"Lỗi: {exc}\n"
                        f"{user_guidance}"
                    ),
                )


    def _resolve_existing_campaign_keywords(
        self,
        command: AdsCommand,
        post_message: str,
        sku_prefix: str = "JC",
    ) -> list[str]:
        hint_keywords = self._extract_campaign_hint_keywords(command.existing_campaign_hint)
        if hint_keywords:
            return hint_keywords

        if command.manual_sku_keywords:
            deduped: list[str] = []
            seen: set[str] = set()
            for item in command.manual_sku_keywords:
                value = str(item).strip().upper()
                if not value or value in seen:
                    continue
                seen.add(value)
                deduped.append(value)
            if deduped:
                return deduped

        hashtags = extract_sku_codes(
            post_message or "",
            sku_prefix,
            self._configured_sku_codes(),
        )
        if hashtags:
            return hashtags
        sku_example = "JCV140" if sku_prefix == "JC" else f"{sku_prefix}001"
        raise ValidationError(
            "Mode lên cũ cần SKU để map campaign.\n"
            f"Anh thêm SKU trong lệnh (ví dụ {sku_example} lên cũ) "
            f"hoặc thêm hashtag #{sku_prefix}... vào bài viết."
        )

    @staticmethod
    def _extract_campaign_hint_keywords(campaign_hint: str) -> list[str]:
        raw_hint = re.sub(r"\s+", " ", str(campaign_hint or "").strip())
        if not raw_hint:
            return []
        normalized = re.sub(r"[^0-9A-Za-zÀ-ỹĐđ]+", " ", raw_hint, flags=re.UNICODE)
        keywords: list[str] = []
        seen: set[str] = set()
        for token in normalized.split():
            value = token.strip().upper()
            if not value or value in seen:
                continue
            seen.add(value)
            keywords.append(value)
        if keywords:
            return keywords
        raise ValidationError(
            "Camp hint chưa hợp lệ.\n"
            "Anh gửi theo cú pháp: <link> lên cũ camp video"
        )

    @staticmethod
    def _build_story_id_candidates(resolved_post: Any, post_url: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            key = str(value).strip()
            if not key or key in seen:
                return
            seen.add(key)
            candidates.append(key)

        object_story_id = str(getattr(resolved_post, "object_story_id", "")).strip()
        page_id = str(getattr(resolved_post, "page_id", "")).strip()
        post_id = str(getattr(resolved_post, "post_id", "")).strip()
        _add(object_story_id)
        if page_id and post_id:
            _add(f"{page_id}_{post_id}")

        normalized_url = normalize_facebook_url(post_url or "")
        reel_match = re.search(r"/reel/(?P<reel_id>\d+)", normalized_url, flags=re.IGNORECASE)
        if reel_match and page_id:
            _add(f"{page_id}_{reel_match.group('reel_id')}")
        return candidates

    def _build_campaign_select_message(
        self,
        keywords: list[str],
        candidates: list[dict[str, str]],
        total_count: int,
    ) -> str:
        lines = [
            "Tìm thấy nhiều campaign ACTIVE khớp điều kiện.",
            f"- Từ khóa match: {', '.join(keywords)}",
            f"- Số campaign khớp: {total_count}",
            "",
            "Anh chọn campaign muốn lên ads:",
        ]
        for index, item in enumerate(candidates, start=1):
            name = str(item.get("name", "")).strip() or "(không tên)"
            lines.append(f"{index}. {name} | {item.get('id', '')}")
        if total_count > len(candidates):
            lines.append("")
            lines.append(
                f"Em chỉ hiển thị {len(candidates)} campaign mới cập nhật gần nhất. "
                "Nếu chưa thấy đúng campaign, anh nhập SKU/camp hint cụ thể hơn rồi gửi lại."
            )
        return "\n".join(lines)

    @staticmethod
    def _campaign_candidate_label(candidate: dict[str, str], index: int) -> str:
        name = str(candidate.get("name", "")).strip() or "(không tên)"
        label = f"{index + 1}. {name}"
        if len(label) <= 58:
            return label
        return label[:55] + "..."

    async def _mark_cloud_schedule_completed(
        self,
        *,
        task: str,
        slot: str | None = None,
        run_date: date | None = None,
        bucket: str | None = None,
        profile: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self.cloud_schedule_guard.mark_completed,
            task=task,
            slot=slot,
            run_date=run_date,
            bucket=bucket,
            profile=profile or self.settings.profile_name,
        )

    def _current_pancake_td_sync_bucket(self) -> str:
        now_local = datetime.now(self._resolve_timezone())
        minute = 30 if now_local.minute >= 30 else 0
        bucket_at = now_local.replace(minute=minute, second=0, microsecond=0)
        return bucket_at.strftime("%Y-%m-%dT%H:%M")

    def _local_schedule_key(
        self,
        *,
        task: str,
        slot: str | None = None,
        run_date: date | None = None,
        bucket: str | None = None,
        profile: str | None = None,
    ) -> str:
        parts = [str(task).strip(), str(profile or self.settings.profile_name or "main").strip()]
        if slot:
            parts.append(str(slot).strip())
        if run_date:
            parts.append(run_date.isoformat())
        if bucket:
            parts.append(str(bucket).strip())
        return ":".join(part for part in parts if part)

    def _local_schedule_mark_path(self, key: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_") or "schedule"
        return self.settings.state_root / "local_schedule_marks" / f"{safe_name}.json"

    def _try_claim_local_schedule(
        self,
        *,
        task: str,
        slot: str | None = None,
        run_date: date | None = None,
        bucket: str | None = None,
        profile: str | None = None,
    ) -> Path | object | None:
        if not self.cloud_schedule_guard.is_configured():
            return _DISABLED_LOCAL_SCHEDULE_CLAIM
        profile_name = str(profile or self.settings.profile_name or "main").strip() or "main"
        key = self._local_schedule_key(task=task, slot=slot, run_date=run_date, bucket=bucket, profile=profile_name)
        path = self._local_schedule_mark_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self.logger.info("Bo qua local scheduled task %s vi slot da duoc process khac claim.", key)
            return None
        payload = {
            "key": key,
            "task": task,
            "slot": slot or "",
            "run_date": run_date.isoformat() if run_date else "",
            "bucket": bucket or "",
            "profile": profile_name,
            "claimed_at": now_utc_iso(),
            "pid": os.getpid(),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def _release_local_schedule_claim(self, claim_path: Path | object | None) -> None:
        if not claim_path:
            return
        if claim_path is _DISABLED_LOCAL_SCHEDULE_CLAIM:
            return
        if not isinstance(claim_path, Path):
            return
        with suppress(FileNotFoundError):
            claim_path.unlink()

    async def _token_health_monitor_loop(self) -> None:
        self.logger.info(
            "Bat token healthcheck scheduler: %02d:%02d (%s)",
            self.settings.token_healthcheck_hour,
            self.settings.token_healthcheck_minute,
            self.settings.app_timezone,
        )
        await asyncio.sleep(3)
        await self._send_token_health_report(
            chat_id=self.settings.telegram_allowed_user_id,
            trigger_label="Kiểm tra lúc khởi động bot",
            notify_success=not self.settings.token_healthcheck_startup_alert_only_on_failure,
        )
        while True:
            wait_seconds = self._seconds_until_next_token_check()
            self.logger.info("Token healthcheck lan tiep theo sau %s giay", wait_seconds)
            await asyncio.sleep(wait_seconds)
            run_date = datetime.now(self._resolve_timezone()).date()
            claim = self._try_claim_local_schedule(task="token-health", run_date=run_date)
            if not claim:
                continue
            await self._send_token_health_report(
                chat_id=self.settings.telegram_allowed_user_id,
                trigger_label=(
                    "Kiểm tra định kỳ buổi sáng "
                    f"({self.settings.token_healthcheck_hour:02d}:{self.settings.token_healthcheck_minute:02d})"
                ),
                notify_success=True,
            )
            await self._mark_cloud_schedule_completed(
                task="token-health",
                run_date=run_date,
            )

    async def _daily_report_monitor_loop(self) -> None:
        self.logger.info(
            "Bat daily report scheduler: %02d:%02d (ngay hom qua) va 21:00 (ngay hom nay) (%s)",
            self.settings.daily_report_hour,
            self.settings.daily_report_minute,
            self.settings.app_timezone,
        )
        notify_chat_ids = self._resolve_daily_report_notify_chat_ids()
        self._clear_startup_daily_report_retry_state()
        while True:
            wait_seconds, slot = self._seconds_until_next_daily_report_schedule()
            self.logger.info("Daily report lan tiep theo sau %s giay (%s)", wait_seconds, slot)
            await asyncio.sleep(wait_seconds)
            run_date = datetime.now(self._resolve_timezone()).date()
            if self._daily_report_slot_already_sent(slot, run_date):
                self.logger.info(
                    "Bo qua daily report slot %s ngay %s vi da gui truoc do.",
                    slot,
                    run_date.isoformat(),
                )
                continue
            claim = self._try_claim_local_schedule(task="daily-report", slot=slot, run_date=run_date)
            if not claim:
                continue
            if slot == "morning":
                trigger_label = (
                    "Báo cáo tự động buổi sáng "
                    f"({self.settings.daily_report_hour:02d}:{self.settings.daily_report_minute:02d})"
                )
            else:
                trigger_label = "Báo cáo tự động buổi tối (21:00)"
            report_payload: dict[str, Any] | None = None
            all_delivered = True
            for chat_id in notify_chat_ids:
                report_payload = await self._send_daily_report(
                    chat_id=chat_id,
                    trigger_label=trigger_label,
                    report_date=self._resolve_daily_report_date_for_slot(slot),
                    notify_success=True,
                    report_payload=report_payload,
                    include_recent_rollups=(slot == "morning" and self._is_report_group_chat(chat_id)),
                    include_task_summary=(slot == "evening"),
                )
                all_delivered = all_delivered and bool(self._last_daily_report_send_ok)
            if all_delivered:
                self._mark_daily_report_slot_sent(slot, run_date=run_date)
                await self._mark_cloud_schedule_completed(
                    task="daily-report",
                    slot=slot,
                    run_date=run_date,
                )
            else:
                self._release_local_schedule_claim(claim)
                self._mark_daily_report_slot_failed(slot, run_date=run_date)
                self.logger.warning(
                    "Daily report slot %s that bai mot phan/hoan toan. Bot se cho moc bao cao tiep theo thay vi gui bu khi khoi dong lai.",
                    slot,
                )

    async def _reconcile_cod_monitor_loop(self) -> None:
        if not self.reconcile:
            return
        weekday_labels = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
        auto_weekdays = ",".join(
            weekday_labels[idx]
            for idx in self.settings.reconcile_cod_auto_weekdays
            if 0 <= int(idx) <= 6
        )
        weekly_label = (
            weekday_labels[self.settings.reconcile_cod_weekly_summary_weekday]
            if 0 <= int(self.settings.reconcile_cod_weekly_summary_weekday) <= 6
            else str(self.settings.reconcile_cod_weekly_summary_weekday)
        )
        notify_chat_id = self._resolve_reconcile_cod_notify_chat_id()
        self.logger.info(
            (
                "Bat reconcile COD scheduler: %02d:%02d (%s) "
                "| cash-in weekdays=%s | weekly_summary=%s@%s | notify_chat_id=%s"
            ),
            self.settings.reconcile_cod_hour,
            self.settings.reconcile_cod_minute,
            self.settings.app_timezone,
            auto_weekdays or "none",
            "ON" if self.settings.reconcile_cod_weekly_summary_enabled else "OFF",
            weekly_label,
            notify_chat_id,
        )
        while True:
            wait_seconds, slots = self._seconds_until_next_reconcile_schedule()
            self.logger.info(
                "Reconcile COD lan tiep theo sau %s giay (slots=%s)",
                wait_seconds,
                ",".join(slots) if slots else "none",
            )
            await asyncio.sleep(wait_seconds)
            run_date = datetime.now(self._resolve_timezone()).date()
            if "cash_in" in slots:
                claim = self._try_claim_local_schedule(task="reconcile-cash-in", run_date=run_date)
                if claim:
                    sent = await self._send_reconcile_cod_cash_in_report(
                        chat_id=notify_chat_id,
                        trigger_label=(
                            "Báo cáo tiền về tự động Thái Dương "
                            f"({self.settings.reconcile_cod_hour:02d}:{self.settings.reconcile_cod_minute:02d})"
                        ),
                    )
                    if sent:
                        await self._mark_cloud_schedule_completed(task="reconcile-cash-in", run_date=run_date)
                    else:
                        self._release_local_schedule_claim(claim)
            if "weekly_summary" in slots:
                claim = self._try_claim_local_schedule(task="reconcile-weekly", run_date=run_date)
                if claim:
                    sent = await self._send_reconcile_cod_weekly_summary_report(
                        chat_id=notify_chat_id,
                        trigger_label=(
                            "Tổng tiền nhận tuần tự động Thái Dương "
                            f"({self.settings.reconcile_cod_hour:02d}:{self.settings.reconcile_cod_minute:02d})"
                        ),
                    )
                    if sent:
                        await self._mark_cloud_schedule_completed(task="reconcile-weekly", run_date=run_date)
                    else:
                        self._release_local_schedule_claim(claim)

    async def _pancake_td_sync_monitor_loop(self) -> None:
        if not self.pancake_td_sync:
            return
        interval_seconds = max(5, int(self.settings.pancake_td_sync_poll_seconds))
        notify_chat_id = (
            int(self.settings.pancake_td_sync_notify_chat_id)
            if int(self.settings.pancake_td_sync_notify_chat_id) != 0
            else int(self.settings.telegram_allowed_user_id)
        )
        self.logger.info(
            "Bat Pancake -> Thai Duong sync poller moi %s giay",
            interval_seconds,
        )
        while True:
            try:
                report = await asyncio.to_thread(self.pancake_td_sync.sync_once)
            except Exception:  # noqa: BLE001
                self.logger.exception("Pancake -> Thai Duong sync loop gap loi")
                await asyncio.sleep(interval_seconds)
                continue
            await self._mark_cloud_schedule_completed(
                task="pancake-td-sync",
                bucket=self._current_pancake_td_sync_bucket(),
                profile=self.settings.profile_name,
            )

            should_notify = bool(report.get("notify"))
            if should_notify and self._bot:
                try:
                    text = self.pancake_td_sync.build_message(
                        report,
                        trigger_label="Đồng bộ tự động Pancake -> Thái Dương",
                    )
                    if len(text) > 3800:
                        text = text[:3760] + "\n...\n(Đã rút gọn vì thông báo quá dài)"
                    await self._bot.send_message(
                        chat_id=notify_chat_id,
                        text=text,
                    )
                except Exception:  # noqa: BLE001
                    self.logger.exception("Gui thong bao Pancake -> Thai Duong that bai")
            await asyncio.sleep(interval_seconds)

    async def _send_manual_pancake_td_sync(self, *, chat_id: int, order_code: str | None = None) -> None:
        if not self.pancake_td_sync:
            return
        if not self._bot:
            return
        try:
            if order_code:
                report = await asyncio.to_thread(
                    self.pancake_td_sync.sync_order_code_manual,
                    order_code,
                )
            else:
                report = await asyncio.to_thread(self.pancake_td_sync.sync_today_manual)
        except Exception:  # noqa: BLE001
            self.logger.exception("Dong bo thu cong Pancake -> Thai Duong that bai")
            await self._bot.send_message(
                chat_id=chat_id,
                text=(
                    "Đồng bộ thủ công Pancake -> Thái Dương bị lỗi hệ thống, "
                    "anh thử lại sau 1-2 phút giúp em."
                ),
            )
            return

        trigger_label = "Đồng bộ thủ công Pancake -> Thái Dương (hôm nay)"
        if order_code:
            trigger_label = f"Đồng bộ thủ công Pancake -> Thái Dương (mã {order_code})"
        text = self.pancake_td_sync.build_message(
            report,
            trigger_label=trigger_label,
        )
        if len(text) > 3800:
            text = text[:3760] + "\n...\n(Đã rút gọn vì thông báo quá dài)"
        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
        )

    def _seconds_until_next_daily_report_schedule(self) -> tuple[int, str]:
        tzinfo = self._resolve_timezone()
        now_local = datetime.now(tzinfo)
        slots = [
            ("morning", self.settings.daily_report_hour, self.settings.daily_report_minute),
            ("evening", 21, 0),
        ]
        next_slot = "morning"
        next_run: datetime | None = None
        for slot_name, hour, minute in slots:
            run_at = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= now_local:
                run_at = run_at + timedelta(days=1)
            if next_run is None or run_at < next_run:
                next_run = run_at
                next_slot = slot_name
        if next_run is None:
            return self._seconds_until_next_schedule(
                self.settings.daily_report_hour,
                self.settings.daily_report_minute,
            ), "morning"
        delta = next_run - now_local
        return max(1, math.ceil(delta.total_seconds())), next_slot

    def _seconds_until_next_reconcile_schedule(self) -> tuple[int, list[str]]:
        tzinfo = self._resolve_timezone()
        now_local = datetime.now(tzinfo)
        candidates: list[tuple[datetime, str]] = []

        for weekday in self.settings.reconcile_cod_auto_weekdays:
            run_at = self._next_scheduled_weekday_run(
                now_local=now_local,
                weekday=int(weekday),
                hour=self.settings.reconcile_cod_hour,
                minute=self.settings.reconcile_cod_minute,
            )
            candidates.append((run_at, "cash_in"))

        if self.settings.reconcile_cod_weekly_summary_enabled:
            run_at = self._next_scheduled_weekday_run(
                now_local=now_local,
                weekday=int(self.settings.reconcile_cod_weekly_summary_weekday),
                hour=self.settings.reconcile_cod_hour,
                minute=self.settings.reconcile_cod_minute,
            )
            candidates.append((run_at, "weekly_summary"))

        if not candidates:
            fallback_seconds = self._seconds_until_next_schedule(
                self.settings.reconcile_cod_hour,
                self.settings.reconcile_cod_minute,
            )
            return fallback_seconds, ["cash_in"]

        next_run = min(item[0] for item in candidates)
        slots = sorted({slot for run_at, slot in candidates if run_at == next_run})
        delta = next_run - now_local
        return max(1, math.ceil(delta.total_seconds())), slots

    @staticmethod
    def _next_scheduled_weekday_run(
        *,
        now_local: datetime,
        weekday: int,
        hour: int,
        minute: int,
    ) -> datetime:
        safe_weekday = min(max(int(weekday), 0), 6)
        days_ahead = (safe_weekday - now_local.weekday()) % 7
        run_at = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
        if run_at <= now_local:
            run_at = run_at + timedelta(days=7)
        return run_at

    def _resolve_daily_report_date_for_slot(self, slot: str) -> date:
        now_local = datetime.now(self._resolve_timezone()).date()
        if slot == "morning":
            return now_local - timedelta(days=1)
        return now_local

    def _clear_startup_daily_report_retry_state(self) -> None:
        state = self._load_daily_report_scheduler_state()
        removed: list[str] = []
        for slot in ("morning", "evening"):
            pending_key = f"{slot}_pending_run_date"
            failed_key = f"{slot}_last_failed_at"
            if pending_key in state:
                state.pop(pending_key, None)
                removed.append(pending_key)
            if failed_key in state:
                state.pop(failed_key, None)
                removed.append(failed_key)
        if removed:
            self._save_daily_report_scheduler_state(state)
            self.logger.info(
                "Da xoa daily report pending startup state (%s); bot se khong gui bu khi khoi dong lai.",
                ", ".join(removed),
            )

    def _mark_daily_report_slot_sent(self, slot: str, run_date: date | None = None) -> None:
        normalized_slot = str(slot).strip().lower()
        if normalized_slot not in {"morning", "evening"}:
            return
        state = self._load_daily_report_scheduler_state()
        run_date_key = (run_date or datetime.now(self._resolve_timezone()).date()).isoformat()
        state[f"{normalized_slot}_last_sent_run_date"] = run_date_key
        state[f"{normalized_slot}_last_sent_at"] = now_utc_iso()
        state.pop(f"{normalized_slot}_pending_run_date", None)
        state.pop(f"{normalized_slot}_last_failed_at", None)
        self._save_daily_report_scheduler_state(state)

    def _daily_report_slot_already_sent(self, slot: str, run_date: date) -> bool:
        normalized_slot = str(slot).strip().lower()
        if normalized_slot not in {"morning", "evening"}:
            return False
        state = self._load_daily_report_scheduler_state()
        return str(state.get(f"{normalized_slot}_last_sent_run_date", "")).strip() == run_date.isoformat()

    def _mark_daily_report_slot_failed(self, slot: str, run_date: date | None = None) -> None:
        normalized_slot = str(slot).strip().lower()
        if normalized_slot not in {"morning", "evening"}:
            return
        state = self._load_daily_report_scheduler_state()
        run_date_key = (run_date or datetime.now(self._resolve_timezone()).date()).isoformat()
        state[f"{normalized_slot}_pending_run_date"] = run_date_key
        state[f"{normalized_slot}_last_failed_at"] = now_utc_iso()
        self._save_daily_report_scheduler_state(state)

    def _daily_report_scheduler_state_path(self) -> Path:
        return self.settings.state_root / "daily_report_scheduler_state.json"

    def _load_daily_report_scheduler_state(self) -> dict[str, Any]:
        path = self._daily_report_scheduler_state_path()
        if not path.exists():
            return {}
        try:
            payload = load_json(path)
        except Exception:  # noqa: BLE001
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _save_daily_report_scheduler_state(self, state: dict[str, Any]) -> None:
        try:
            dump_json(self._daily_report_scheduler_state_path(), state)
        except Exception:  # noqa: BLE001
            self.logger.exception("Luu state daily report scheduler that bai")

    @staticmethod
    def _parse_iso_date(raw: str) -> date | None:
        text = str(raw).strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _resolve_daily_report_date_for_slot_and_run_date(self, slot: str, run_date: date) -> date:
        if str(slot).strip().lower() == "morning":
            return run_date - timedelta(days=1)
        return run_date

    def _is_daily_report_slot_due_for_retry(self, *, slot: str, run_date: date, now_local: datetime) -> bool:
        if run_date < now_local.date():
            return True
        target_hour = self.settings.daily_report_hour if slot == "morning" else 21
        target_minute = self.settings.daily_report_minute if slot == "morning" else 0
        slot_time = now_local.replace(
            hour=target_hour,
            minute=target_minute,
            second=0,
            microsecond=0,
        )
        return now_local >= slot_time

    def _seconds_until_next_token_check(self) -> int:
        return self._seconds_until_next_schedule(
            self.settings.token_healthcheck_hour,
            self.settings.token_healthcheck_minute,
        )

    def _seconds_until_next_schedule(self, hour: int, minute: int) -> int:
        tzinfo = self._resolve_timezone()
        now_local = datetime.now(tzinfo)
        next_run = now_local.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if next_run <= now_local:
            next_run = next_run + timedelta(days=1)
        delta = next_run - now_local
        return max(1, math.ceil(delta.total_seconds()))

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    def _resolve_daily_report_notify_chat_ids(self) -> list[int]:
        primary_chat_id = int(self.settings.telegram_allowed_user_id)
        extra_chat_id = int(self.settings.daily_report_notify_chat_id)
        chat_ids: list[int] = [primary_chat_id]
        if extra_chat_id != 0 and extra_chat_id != primary_chat_id:
            chat_ids.append(extra_chat_id)
        return chat_ids

    def _resolve_reconcile_cod_notify_chat_id(self) -> int:
        configured = int(self.settings.reconcile_cod_notify_chat_id)
        if configured != 0:
            return configured
        group_chat_id = int(self.settings.daily_report_notify_chat_id)
        if group_chat_id != 0:
            return group_chat_id
        return int(self.settings.telegram_allowed_user_id)

    def _resolve_reconcile_cod_error_notify_chat_id(self) -> int:
        """Keep automatic reconcile errors in the owner's private chat."""
        return int(self.settings.telegram_allowed_user_id)

    def _resolve_daily_report_target_chat_ids(self, source_chat_id: int | None = None) -> list[int]:
        chat_ids: list[int] = []
        if source_chat_id is not None and int(source_chat_id) != 0:
            chat_ids.append(int(source_chat_id))
        for chat_id in self._resolve_daily_report_notify_chat_ids():
            if chat_id not in chat_ids:
                chat_ids.append(chat_id)
        return chat_ids

    async def _send_daily_report_to_target_chats(
        self,
        *,
        target_chat_ids: list[int],
        trigger_label: str,
        report_date: date | None,
        notify_success: bool,
    ) -> None:
        report_payload: dict[str, Any] | None = None
        for chat_id in target_chat_ids:
            report_payload = await self._send_daily_report(
                chat_id=chat_id,
                trigger_label=trigger_label,
                report_date=report_date,
                notify_success=notify_success,
                report_payload=report_payload,
            )

    async def _send_token_health_report(self, chat_id: int, trigger_label: str, notify_success: bool) -> None:
        if not self._bot:
            return
        meta_report: dict[str, Any]
        try:
            meta_report = await asyncio.to_thread(self.meta.check_token_health)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Kiem tra token that bai")
            meta_report = {
                "ok": False,
                "checks": {
                    "runtime": {
                        "ok": False,
                        "error": f"Lỗi runtime khi kiểm tra token: {exc}",
                    }
                },
            }
        thai_duong_report: dict[str, Any] = {
            "ok": False,
            "configured": False,
            "error": "Chua khoi tao Thai Duong client.",
            "api_probe": {
                "ok": False,
                "skipped": True,
                "reason": "Chua khoi tao Thai Duong client.",
            },
        }
        if self.thai_duong:
            try:
                thai_duong_report = await asyncio.to_thread(self.thai_duong.check_token_health)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Kiem tra token Thai Duong that bai")
                thai_duong_report = {
                    "ok": False,
                    "configured": True,
                    "error": f"Lỗi runtime khi kiểm tra token Thai Duong: {exc}",
                    "api_probe": {
                        "ok": False,
                        "skipped": True,
                        "reason": "Runtime error.",
                    },
                }

        checks = {}
        if isinstance(meta_report.get("checks"), dict):
            checks.update(meta_report["checks"])
        checks["thai_duong_token"] = thai_duong_report
        report = {
            "ok": bool(meta_report.get("ok")) and bool(thai_duong_report.get("ok")),
            "checks": checks,
        }
        self.logger.info(
            "Ket qua token healthcheck: %s",
            "OK" if report.get("ok") else "FAIL",
        )
        if report.get("ok") and not notify_success:
            return
        text = self._build_token_health_message(report, trigger_label)
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao token qua Telegram that bai")

    async def _send_daily_report(
        self,
        chat_id: int,
        trigger_label: str,
        report_date: date | None,
        notify_success: bool,
        report_payload: dict[str, Any] | None = None,
        include_recent_rollups: bool = False,
        include_task_summary: bool = False,
    ) -> dict[str, Any] | None:
        if not self._bot:
            return report_payload
        report = report_payload
        if report is None:
            try:
                report = await asyncio.to_thread(self.reports.generate_report, report_date)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Tao bao cao ngay that bai")
                report = {
                    "ok": False,
                    "partial": False,
                    "report_date": (report_date or self.reports.default_report_date()).isoformat(),
                    "generated_at": now_utc_iso(),
                    "warnings": [f"Lỗi runtime khi tạo báo cáo: {exc}"],
                    "errors": {"runtime": str(exc)},
                    "pos": None,
                    "ads": None,
                    "top_products": [],
                    "roas": 0.0,
                }
            self.logger.info(
                "Ket qua daily report: %s",
                "OK" if report.get("ok") else ("PARTIAL" if report.get("partial") else "FAIL"),
            )
        if report.get("ok") and not notify_success:
            self._last_daily_report_send_ok = True
            return report
        text = self.reports.build_message(report, trigger_label=trigger_label)
        if include_recent_rollups:
            target_date = self._extract_report_date(report=report, fallback=report_date)
            rollup_text = await asyncio.to_thread(
                self._build_recent_rollup_text_sync,
                target_date,
                report,
            )
            if rollup_text:
                text = text + "\n\n" + rollup_text
        if include_task_summary and self.settings.daily_report_task_summary_enabled:
            target_date = self._extract_report_date(report=report, fallback=report_date)
            task_text = await asyncio.to_thread(
                self._build_daily_task_summary_text_sync,
                target_date,
            )
            if task_text:
                text = text + "\n\n" + task_text
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
            self._last_daily_report_send_ok = True
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao ngay qua Telegram that bai")
            self._last_daily_report_send_ok = False
        return report

    async def _send_media_performance_report(
        self,
        *,
        chat_id: int,
        command: MediaPerformanceCommand,
        trigger_label: str,
    ) -> dict[str, Any] | None:
        if not self._bot or not self.media_performance:
            return None
        try:
            report = await asyncio.to_thread(self.media_performance.generate_report, command)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tao bao cao media performance that bai")
            report = {
                "ok": False,
                "start_date": command.start_date.isoformat() if command.start_date else "",
                "end_date": command.end_date.isoformat() if command.end_date else "",
                "generated_at": now_utc_iso(),
                "requested_codes": list(command.codes),
                "campaign_query": command.campaign_query,
                "summary": {},
                "codes": [],
                "warnings": [f"Lỗi runtime khi tạo báo cáo media: {exc}"],
                "errors": {"runtime": str(exc)},
            }
        messages = self.media_performance.build_messages(report)
        if trigger_label and messages:
            messages[0] = f"{trigger_label}\n{messages[0]}"
        for text in messages:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
            except Exception:  # noqa: BLE001
                self.logger.exception("Gui bao cao media performance qua Telegram that bai")
                break
        return report

    def _build_daily_task_summary_text_sync(self, target_date: date | None) -> str:
        if not self.daily_task_summary:
            return ""
        try:
            summary = self.daily_task_summary.build_summary(target_date)
            return self.daily_task_summary.build_message(summary)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tong hop task cuoi ngay that bai")
            return "Task công việc cuối ngày:\n- Không tổng hợp được task công việc: " + self._short_error(
                str(exc),
                max_len=180,
            )

    async def _send_reconcile_cod_cash_in_report(self, *, chat_id: int, trigger_label: str) -> bool:
        if not self._bot or not self.reconcile:
            return False
        report: dict[str, Any] | None = None
        send_chat_id = int(chat_id)
        try:
            report = await asyncio.to_thread(self.reconcile.generate_report_if_settlement_exists, None)
            if report is None:
                self.logger.info("Bo qua bao cao tien ve tu dong vi hom nay khong co ky doi soat Thai Duong")
                return True
            summary = await asyncio.to_thread(self.reconcile.summarize_cash_in_from_report, report)
            text = self._build_reconcile_cash_in_message(summary=summary, trigger_label=trigger_label)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Bao cao tien ve tu dong that bai")
            text = (
                f"{trigger_label}\n"
                "Tổng quan: LỖI\n"
                f"Chi tiết: {self._short_error(str(exc), max_len=220)}"
            )
            send_chat_id = self._resolve_reconcile_cod_error_notify_chat_id()
        try:
            await self._bot.send_message(chat_id=send_chat_id, text=text)
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao tien ve tu dong qua Telegram that bai")
            return False

        # The cash-in slot is also the automatic reconciliation trigger. Reuse
        # the already fetched report so the same settlement is not fetched twice.
        if report is None:
            return True
        return await self._send_reconcile_cod_report(
            chat_id=chat_id,
            trigger_label="Đối soát COD tự động theo cập nhật dòng tiền Thái Dương",
            settlement_date=None,
            notify_success=True,
            allow_update_prompt=True,
            allow_sheet_sync=True,
            report_override=report,
            auto_sheet_sync=True,
            notify_reconcile_detail=False,
        )

    async def _send_reconcile_cod_weekly_summary_report(self, *, chat_id: int, trigger_label: str) -> bool:
        if not self._bot or not self.reconcile:
            return False
        send_chat_id = int(chat_id)
        try:
            weekly = await asyncio.to_thread(self.reconcile.build_weekly_cash_in_summary)
            text = self._build_reconcile_weekly_cash_in_message(weekly=weekly, trigger_label=trigger_label)
            if not isinstance(weekly, dict) or not bool(weekly.get("ok")):
                send_chat_id = self._resolve_reconcile_cod_error_notify_chat_id()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Bao cao tong tuan tu dong that bai")
            text = (
                f"{trigger_label}\n"
                "Tổng quan: LỖI\n"
                f"Chi tiết: {self._short_error(str(exc), max_len=220)}"
            )
            send_chat_id = self._resolve_reconcile_cod_error_notify_chat_id()
        try:
            await self._bot.send_message(chat_id=send_chat_id, text=text)
            return True
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao tong tuan tu dong qua Telegram that bai")
            return False

    def _build_reconcile_cash_in_message(self, *, summary: dict[str, Any], trigger_label: str) -> str:
        settlement_date = str(summary.get("settlement_date", "")).strip()
        parsed = None
        if settlement_date:
            try:
                parsed = datetime.strptime(settlement_date, "%Y-%m-%d").date()
            except ValueError:
                parsed = None
        settlement_text = parsed.strftime("%d/%m/%Y") if parsed else (settlement_date or "N/A")
        lines = [
            trigger_label,
            f"Kỳ đối soát: {settlement_text}",
            f"Tiền về (THB): {self._fmt_thb(self._to_float(summary.get('thb_total')))}",
            f"Tiền về (VNĐ): {self._fmt_vnd(self._to_int(round(self._to_float(summary.get('vnd_total')))))}",
        ]
        converted_count = self._to_int(summary.get("vnd_converted_count"))
        if converted_count > 0:
            lines.append(
                f"(Có {converted_count:,} mục quy đổi theo tỷ giá {self._to_float(self.settings.report_thb_to_vnd_rate):,.0f})"
            )
        return "\n".join(lines)

    def _build_reconcile_weekly_cash_in_message(self, *, weekly: dict[str, Any], trigger_label: str) -> str:
        week_start = str(weekly.get("week_start", "")).strip()
        week_end = str(weekly.get("week_end", "")).strip()
        week_start_text = week_start
        week_end_text = week_end
        try:
            if week_start:
                week_start_text = datetime.strptime(week_start, "%Y-%m-%d").strftime("%d/%m/%Y")
            if week_end:
                week_end_text = datetime.strptime(week_end, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass
        if not bool(weekly.get("ok")):
            return (
                f"{trigger_label}\n"
                f"Khung tuần: {week_start_text} - {week_end_text}\n"
                "Tổng quan: LỖI\n"
                f"Chi tiết: {self._short_error(str(weekly.get('error', 'Không rõ lỗi.')), max_len=220)}"
            )

        lines = [
            trigger_label,
            f"Khung tuần: {week_start_text} - {week_end_text} (T2-T6)",
            f"Số kỳ đối soát đã ghi nhận: {len(weekly.get('days', [])):,}",
            f"Tổng tiền nhận tuần (THB): {self._fmt_thb(self._to_float(weekly.get('thb_total')))}",
            f"Tổng tiền nhận tuần (VNĐ): {self._fmt_vnd(self._to_int(round(self._to_float(weekly.get('vnd_total')))))}",
        ]
        converted_count = self._to_int(weekly.get("vnd_converted_count"))
        if converted_count > 0:
            lines.append(
                f"(Có {converted_count:,} mục quy đổi theo tỷ giá {self._to_float(self.settings.report_thb_to_vnd_rate):,.0f})"
            )
        return "\n".join(lines)

    def _extract_report_date(self, *, report: dict[str, Any] | None, fallback: date | None) -> date:
        if isinstance(report, dict):
            raw = str(report.get("report_date", "")).strip()
            if raw:
                try:
                    return datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    pass
        if fallback is not None:
            return fallback
        return self.reports.default_report_date()

    def _build_recent_rollup_text_sync(
        self,
        target_date: date,
        current_report: dict[str, Any] | None = None,
    ) -> str:
        report_cache: dict[str, dict[str, Any]] = {}
        if isinstance(current_report, dict):
            current_report_date = self._extract_report_date(report=current_report, fallback=target_date)
            report_cache[current_report_date.isoformat()] = current_report
        blocks: list[str] = []
        for window_days in (3, 7):
            block = self._build_recent_rollup_window_text_sync(
                target_date=target_date,
                window_days=window_days,
                report_cache=report_cache,
            )
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    def _build_recent_rollup_window_text_sync(
        self,
        *,
        target_date: date,
        window_days: int,
        report_cache: dict[str, dict[str, Any]],
    ) -> str:
        safe_window_days = max(1, int(window_days))
        day_range = [
            target_date - timedelta(days=offset)
            for offset in range(safe_window_days - 1, -1, -1)
        ]
        total_revenue_thb = 0.0
        total_revenue_vnd = 0
        total_orders = 0
        total_ads_spend_vnd = 0
        data_days = 0
        pos_days = 0
        ads_days = 0

        for item_date in day_range:
            report = self._load_report_for_rollup_sync(
                report_date=item_date,
                report_cache=report_cache,
            )
            if not isinstance(report, dict):
                continue
            has_data = False
            pos = report.get("pos") if isinstance(report.get("pos"), dict) else None
            if pos:
                has_data = True
                pos_days += 1
                total_revenue_thb += self._to_float(pos.get("revenue_total_thb"))
                total_revenue_vnd += self._to_int(pos.get("revenue_total_vnd"))
                total_orders += self._to_int(pos.get("order_count"))
            ads = report.get("ads") if isinstance(report.get("ads"), dict) else None
            if ads:
                has_data = True
                ads_days += 1
                total_ads_spend_vnd += self._to_int(ads.get("spend_vnd"))
            if has_data:
                data_days += 1

        start_date = day_range[0]
        end_date = day_range[-1]
        lines = [
            (
                f"Tổng hợp {safe_window_days} ngày gần nhất "
                f"({start_date.strftime('%d/%m')} - {end_date.strftime('%d/%m')}):"
            ),
            f"- Độ phủ dữ liệu: {data_days}/{safe_window_days} ngày",
        ]
        if pos_days > 0:
            lines.append(
                f"- Doanh thu POS cộng dồn: {self._fmt_thb(total_revenue_thb)} "
                f"(~{self._fmt_vnd(total_revenue_vnd)})"
            )
            lines.append(f"- Số đơn POS cộng dồn: {total_orders:,}")
        else:
            lines.append("- Doanh thu POS cộng dồn: chưa có dữ liệu.")
            lines.append("- Số đơn POS cộng dồn: chưa có dữ liệu.")
        if ads_days > 0:
            lines.append(f"- Chi phí Ads cộng dồn: {self._fmt_vnd(total_ads_spend_vnd)}")
        else:
            lines.append("- Chi phí Ads cộng dồn: chưa có dữ liệu.")
        if total_ads_spend_vnd > 0:
            roas = total_revenue_vnd / float(total_ads_spend_vnd)
            lines.append(f"- ROAS cộng dồn: {roas:.2f}")
        return "\n".join(lines)

    def _load_report_for_rollup_sync(
        self,
        *,
        report_date: date,
        report_cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        report_key = report_date.isoformat()
        cached = report_cache.get(report_key)
        if isinstance(cached, dict):
            return cached
        candidates = [
            self.settings.reports_daily_dir / f"report_{report_key}.json",
            self.settings.reports_error_dir / f"report_{report_key}.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = load_json(path)
            except Exception:  # noqa: BLE001
                self.logger.warning("Khong doc duoc report cache %s", path)
                continue
            if isinstance(payload, dict):
                if self._rollup_report_needs_refresh(payload):
                    refreshed = self._generate_report_for_rollup_sync(report_date=report_date, report_key=report_key)
                    if isinstance(refreshed, dict):
                        report_cache[report_key] = refreshed
                        return refreshed
                report_cache[report_key] = payload
                return payload
        payload = self._generate_report_for_rollup_sync(report_date=report_date, report_key=report_key)
        if isinstance(payload, dict):
            report_cache[report_key] = payload
            return payload
        return None

    @staticmethod
    def _rollup_report_needs_refresh(report: dict[str, Any]) -> bool:
        errors = report.get("errors")
        if bool(report.get("partial")):
            return True
        if isinstance(errors, dict) and errors:
            return True
        pos = report.get("pos")
        if not isinstance(pos, dict):
            return True
        if str(pos.get("basis", "")).strip() != "tong_hang_chot":
            return True
        if not isinstance(report.get("ads"), dict):
            return True
        return False

    def _generate_report_for_rollup_sync(self, *, report_date: date, report_key: str) -> dict[str, Any] | None:
        try:
            payload = self.reports.generate_report(report_date)
        except Exception:  # noqa: BLE001
            self.logger.warning("Khong tao duoc report bo sung cho rollup ngay %s", report_key)
            return None
        if isinstance(payload, dict):
            return payload
        return None

    async def _send_reconcile_cod_report(
        self,
        *,
        chat_id: int,
        trigger_label: str,
        settlement_date: date | None,
        notify_success: bool,
        allow_update_prompt: bool,
        allow_sheet_sync: bool,
        report_override: dict[str, Any] | None = None,
        auto_sheet_sync: bool = False,
        notify_reconcile_detail: bool = True,
    ) -> bool:
        if not self._bot or not self.reconcile:
            return False
        sheet_sync_ok = True
        if report_override is not None:
            report = report_override
        else:
            try:
                report = await asyncio.to_thread(self.reconcile.generate_report, settlement_date)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Doi soat COD that bai")
                report = {
                    "ok": False,
                    "partial": False,
                    "settlement_date": (settlement_date or self.reconcile.default_settlement_date()).isoformat(),
                    "generated_at": now_utc_iso(),
                    "summary": {
                        "matched_unique": 0,
                        "already_correct": 0,
                        "ambiguous": 0,
                        "not_found": 0,
                        "unmapped_status": 0,
                        "update_candidates": 0,
                        "total": 0,
                    },
                    "csv_path": "",
                    "warnings": [f"Lỗi runtime khi đối soát COD: {exc}"],
                    "errors": {"runtime": str(exc)},
                    "source_mode": "unknown",
                    "detail_count": 0,
                }

        self.logger.info(
            "Ket qua doi soat COD: %s",
            "OK" if report.get("ok") else ("PARTIAL" if report.get("partial") else "FAIL"),
        )
        if report.get("ok") and not notify_success and not (allow_sheet_sync or allow_update_prompt):
            return True

        if notify_reconcile_detail:
            text = self.reconcile.build_message(report, trigger_label=trigger_label)
            report_chat_id = (
                self._resolve_reconcile_cod_error_notify_chat_id()
                if report.get("ok") is False
                else int(chat_id)
            )
            try:
                await self._bot.send_message(chat_id=report_chat_id, text=text)
            except Exception:  # noqa: BLE001
                self.logger.exception("Gui ket qua doi soat COD qua Telegram that bai")
                return False

            csv_path = Path(str(report.get("csv_path", "")).strip())
            if csv_path.exists():
                try:
                    await self._bot.send_document(
                        chat_id=report_chat_id,
                        document=FSInputFile(csv_path),
                        caption=f"CSV đối soát COD ({csv_path.name})",
                    )
                except Exception:  # noqa: BLE001
                    self.logger.exception("Gui file CSV doi soat COD that bai")

        if (
            allow_sheet_sync
            and self.reconcile_sheet
            and self.settings.reconcile_cod_sheet_enabled
            and report.get("ok")
        ):
            is_configured = True
            reason = ""
            check_fn = getattr(self.reconcile_sheet, "is_configured", None)
            if callable(check_fn):
                try:
                    is_configured, reason = check_fn()
                except Exception as exc:  # noqa: BLE001
                    is_configured = False
                    reason = str(exc)
            if not is_configured:
                if notify_reconcile_detail:
                    try:
                        await self._bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "Đang bật ghi Google Sheet nhưng cấu hình chưa đủ.\n"
                                f"Lý do: {reason or 'Không rõ lỗi cấu hình.'}"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        self.logger.exception("Gui canh bao cau hinh Google Sheet that bai")
                else:
                    self.logger.error(
                        "Tu dong doi soat COD khong ghi duoc Google Sheet do thieu cau hinh: %s",
                        reason or "Khong ro loi cau hinh",
                    )
                    try:
                        await self._bot.send_message(
                            chat_id=self._resolve_reconcile_cod_error_notify_chat_id(),
                            text=(
                                "Đối soát COD tự động: LỖI\n"
                                "Chi tiết: Không ghi được Google Sheet do cấu hình chưa đủ. "
                                f"{reason or 'Không rõ lỗi cấu hình.'}"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        self.logger.exception("Gui loi cau hinh Google Sheet qua Telegram that bai")
                return False
            run_id = str(report.get("run_id", "")).strip()
            records = report.get("records", [])
            record_count = len(records) if isinstance(records, list) else 0
            product_detail = report.get("product_detail", {})
            product_line_count = (
                self._to_int(product_detail.get("line_count"))
                if isinstance(product_detail, dict)
                else 0
            )
            missing_product_count = (
                self._to_int(product_detail.get("missing_product_order_count"))
                if isinstance(product_detail, dict)
                else 0
            )
            missing_product_codes = (
                product_detail.get("missing_product_order_codes", [])
                if isinstance(product_detail, dict)
                else []
            )
            if not isinstance(missing_product_codes, list):
                missing_product_codes = []
            missing_product_codes = [
                str(item).strip() for item in missing_product_codes if str(item).strip()
            ]
            missing_product_preview = ""
            if missing_product_codes:
                preview = ", ".join(missing_product_codes[:20])
                suffix = "..." if len(missing_product_codes) > 20 else ""
                missing_product_preview = f"\n- Mã đơn chưa lấy được sản phẩm: {preview}{suffix}"
            if auto_sheet_sync:
                try:
                    sync_result = await asyncio.to_thread(self.reconcile_sheet.sync_report, report)
                    if not isinstance(sync_result, dict):
                        raise ValueError("Kết quả ghi Google Sheet không hợp lệ.")
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Tu dong ghi Google Sheet tu batch doi soat COD that bai")
                    sync_result = {
                        "enabled": True,
                        "ok": False,
                        "attempted": record_count,
                        "inserted": 0,
                        "skipped_existing": 0,
                        "errors": [str(exc)],
                        "product_sync": {"inserted": 0, "skipped_existing": 0, "updated_existing": 0, "errors": []},
                    }
                report["sheet_sync"] = sync_result
                run_path_raw = str(report.get("run_path", "")).strip()
                run_path = Path(run_path_raw) if run_path_raw else self.settings.reconcile_cod_runs_dir / f"{run_id}.json"
                if run_id and run_path.exists():
                    try:
                        await asyncio.to_thread(dump_json, run_path, report)
                    except Exception:  # noqa: BLE001
                        self.logger.exception("Khong luu duoc ket qua tu dong ghi Sheet vao run doi soat")

                product_sync = sync_result.get("product_sync", {})
                product_result = product_sync if isinstance(product_sync, dict) else {}
                sync_ok = bool(sync_result.get("ok"))
                sheet_sync_ok = sheet_sync_ok and sync_ok
                sync_intro = "Đã tự động ghi Google Sheet từ phiên đối soát COD." if sync_ok else "Tự động ghi Google Sheet có cảnh báo."
                sync_errors = sync_result.get("errors", [])
                error_line = ""
                if isinstance(sync_errors, list):
                    error_text = ", ".join(str(item) for item in sync_errors if str(item).strip())
                    if error_text:
                        error_line = f"\n- Lỗi: {error_text}"
                if notify_reconcile_detail:
                    try:
                        await self._bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"{sync_intro}\n"
                                f"- Tổng ghi thử: {self._to_int(sync_result.get('attempted')):,}\n"
                                f"- Ghi mới: {self._to_int(sync_result.get('inserted')):,}\n"
                                f"- Bỏ qua trùng: {self._to_int(sync_result.get('skipped_existing')):,}\n"
                                f"- Tab Chi tiet SP COD ghi mới: {self._to_int(product_result.get('inserted')):,}\n"
                                f"- Tab Chi tiet SP COD bỏ qua trùng: {self._to_int(product_result.get('skipped_existing')):,}\n"
                                f"- Tab Chi tiet SP COD sửa dòng cũ: {self._to_int(product_result.get('updated_existing')):,}"
                                f"{error_line}"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        self.logger.exception("Gui ket qua tu dong ghi Google Sheet that bai")
                elif not sync_ok:
                    self.logger.error(
                        "Tu dong ghi Google Sheet co canh bao: %s",
                        error_line.lstrip("\n- ") or "Khong ro loi",
                    )
                    sync_error_text = error_line.strip()
                    if sync_error_text.startswith("- "):
                        sync_error_text = sync_error_text[2:].strip()
                    try:
                        await self._bot.send_message(
                            chat_id=self._resolve_reconcile_cod_error_notify_chat_id(),
                            text=(
                                "Đối soát COD tự động: LỖI\n"
                                "Chi tiết: Tự động ghi Google Sheet không thành công. "
                                f"{sync_error_text or 'Không rõ lỗi.'}"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        self.logger.exception("Gui loi dong bo Google Sheet qua Telegram that bai")
            elif notify_reconcile_detail and run_id and record_count > 0:
                request_id = await asyncio.to_thread(
                    self.storage.create_pending_request,
                    {
                        "request_type": "reconcile_cod_sheet_sync",
                        "run_id": run_id,
                        "settlement_date": report.get("settlement_date", ""),
                    },
                    "reconcile_cod_sheet_sync",
                )
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Batch đối soát COD đã sẵn sàng ghi Google Sheet.\n"
                            f"- Run ID: {run_id}\n"
                            f"- Tổng bản ghi dự kiến ghi: {record_count:,}\n"
                            f"- Dòng sản phẩm dự kiến ghi tab Chi tiet SP COD: {product_line_count:,}\n"
                            f"- Đơn thiếu dữ liệu sản phẩm: {missing_product_count:,}"
                            f"{missing_product_preview}\n"
                            "Anh bấm Duyệt để ghi dữ liệu lên Sheet hoặc Hủy để bỏ qua."
                        ),
                        reply_markup=self.approval.reconcile_sheet_sync_keyboard(request_id=request_id),
                    )
                except Exception:  # noqa: BLE001
                    self.logger.exception("Gui yeu cau duyet ghi Google Sheet that bai")
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

        if not allow_update_prompt or not self.settings.reconcile_cod_update_enabled:
            return sheet_sync_ok
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if self._to_int(summary.get("update_candidates")) <= 0:
            return sheet_sync_ok
        run_id = str(report.get("run_id", "")).strip()
        if not run_id:
            return sheet_sync_ok
        try:
            apply_summary = await asyncio.to_thread(self.reconcile.apply_updates, run_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tu dong cap nhat status Pancake tu doi soat COD that bai")
            try:
                await self._bot.send_message(
                    chat_id=self._resolve_reconcile_cod_error_notify_chat_id(),
                    text=(
                        "Đối soát COD tự động: LỖI\n"
                        f"Chi tiết: Không cập nhật được trạng thái Pancake. {self._short_error(str(exc), max_len=220)}"
                    ),
                )
            except Exception:  # noqa: BLE001
                self.logger.exception("Gui loi cap nhat Pancake qua Telegram that bai")
            return True
        if notify_reconcile_detail:
            try:
                failed = self._to_int(apply_summary.get("failed"))
                status_text = "OK" if failed == 0 else "CẢNH BÁO"
                lines = self._build_reconcile_apply_summary_lines(
                    run_id=run_id,
                    summary=apply_summary,
                    intro="Đã tự động cập nhật trạng thái Pancake từ batch đối soát COD.",
                    status_text=status_text,
                )
                await self._bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                )
            except Exception:  # noqa: BLE001
                self.logger.exception("Gui ket qua tu dong cap nhat COD that bai")
        else:
            self.logger.info("Da tu dong cap nhat trang thai Pancake tu batch doi soat COD; an thong bao chi tiet")
        return sheet_sync_ok

    def _build_reconcile_apply_summary_lines(
        self,
        *,
        run_id: str,
        summary: dict[str, Any],
        intro: str,
        status_text: str | None = None,
    ) -> list[str]:
        failed = self._to_int(summary.get("failed"))
        lines: list[str] = [intro, f"- Run ID: {run_id}"]
        if status_text:
            lines.append(f"- Kết quả: {status_text}")
        lines.extend(
            [
                f"- Đã cập nhật: {self._to_int(summary.get('updated')):,}",
                f"- Đã fallback chuyển trạng thái: {self._to_int(summary.get('transitioned')):,}",
                f"- Bỏ qua: {self._to_int(summary.get('skipped')):,}",
                f"- Lỗi: {failed:,}",
            ]
        )

        failed_orders = summary.get("failed_orders", [])
        if isinstance(failed_orders, list) and failed_orders:
            lines.append("Mã đơn lỗi cần xử lý:")
            for item in failed_orders[:15]:
                if not isinstance(item, dict):
                    continue
                order_id = str(item.get("order_id", "")).strip()
                display_id = str(item.get("display_id", "")).strip()
                awb = str(item.get("awb", "")).strip()
                label = display_id or order_id
                detail = f"{label} (ID:{order_id})" if order_id and label != order_id else label
                if awb:
                    detail = f"{detail} | AWB:{awb}"
                error_code = str(item.get("error_code", "")).strip()
                if error_code:
                    detail = f"{detail} | Mã lỗi:{error_code}"
                error_text = self._short_error(str(item.get("error", "")), max_len=140)
                lines.append(f"- {detail} | {error_text}")
            if len(failed_orders) > 15:
                lines.append(f"- ... và {len(failed_orders) - 15} đơn lỗi khác")
            return lines

        errors = summary.get("errors", [])
        if isinstance(errors, list) and errors:
            lines.append("Chi tiết lỗi:")
            for item in errors[:5]:
                lines.append(f"- {self._short_error(str(item), max_len=200)}")
            if len(errors) > 5:
                lines.append(f"- ... và {len(errors) - 5} lỗi khác")
        return lines

    def _build_token_health_message(self, report: dict[str, Any], trigger_label: str) -> str:
        checks = report.get("checks", {})
        ads_account = checks.get("ads_account", {})
        ads_identity = checks.get("ads_identity", {})
        ads_page_access = checks.get("ads_page_access", {})
        page_identity = checks.get("page_identity", {})
        page_posts = checks.get("page_posts", {})
        thai_duong_token = checks.get("thai_duong_token", {})

        now_local = datetime.now(self._resolve_timezone())
        lines = [
            f"{trigger_label}",
            f"Thời gian: {now_local.strftime('%d/%m/%Y %H:%M')} ({self.settings.app_timezone})",
            f"Tổng quan: {'OK' if report.get('ok') else 'CẢNH BÁO'}",
            "",
        ]

        if ads_account.get("ok"):
            lines.append(
                "Ads token (mã truy cập quảng cáo): OK "
                f"| account={ads_account.get('id', '')} | currency={ads_account.get('currency', '')}"
            )
        else:
            lines.append(
                "Ads token (mã truy cập quảng cáo): LỖI | "
                + self._short_error(str(ads_account.get("error", "Không rõ lỗi.")))
            )

        if ads_identity.get("ok"):
            lines.append(
                "Ads identity (định danh token ads): OK "
                f"| {ads_identity.get('name', '')} ({ads_identity.get('id', '')})"
            )
        else:
            lines.append(
                "Ads identity (định danh token ads): LỖI | "
                + self._short_error(str(ads_identity.get("error", "Không rõ lỗi.")))
            )

        if ads_page_access.get("ok"):
            lines.append(
                "Ads token quảng cáo page: OK "
                f"| {ads_page_access.get('name', '')} ({ads_page_access.get('id', '')})"
            )
        else:
            lines.append(
                "Ads token quảng cáo page: LỖI | "
                + self._short_error(str(ads_page_access.get("error", "Không rõ lỗi.")))
            )
            hint = str(ads_page_access.get("hint", "")).strip()
            if hint:
                lines.append("Hướng xử lý nhanh: " + self._short_error(hint, max_len=360))

        if page_identity.get("ok"):
            lines.append(
                "Page token (mã truy cập trang): OK "
                f"| {page_identity.get('name', '')} ({page_identity.get('id', '')})"
            )
        else:
            lines.append(
                "Page token (mã truy cập trang): LỖI | "
                + self._short_error(str(page_identity.get("error", "Không rõ lỗi.")))
            )

        if page_posts.get("ok"):
            first_post = str(page_posts.get("first_post_id", "")).strip()
            first_post_text = first_post if first_post else "không có post gần đây"
            lines.append(f"Đọc posts của page: OK | first_post={first_post_text}")
        else:
            lines.append(
                "Đọc posts của page: LỖI | "
                + self._short_error(str(page_posts.get("error", "Không rõ lỗi.")))
            )

        token_exp_ts = self._to_int(thai_duong_token.get("token_exp_ts"))
        remaining_seconds = self._to_int(thai_duong_token.get("token_remaining_seconds"))
        exp_text = ""
        if token_exp_ts > 0:
            exp_utc = datetime.fromtimestamp(token_exp_ts, tz=timezone.utc)
            exp_local = exp_utc.astimezone(self._resolve_timezone())
            exp_text = (
                f" | exp={exp_local.strftime('%d/%m/%Y %H:%M')} ({self.settings.app_timezone})"
            )
            if remaining_seconds > 0:
                exp_text += f" | còn ~{self._format_duration(remaining_seconds)}"

        if thai_duong_token.get("ok"):
            lines.append("Token Thai Duong: OK" + exp_text)
        else:
            lines.append(
                "Token Thai Duong: LỖI | "
                + self._short_error(str(thai_duong_token.get("error", "Không rõ lỗi.")))
                + exp_text
            )

        auto_auth_enabled = bool(thai_duong_token.get("auto_auth_enabled"))
        lines.append(f"Tự gia hạn token Thái Dương: {'BẬT' if auto_auth_enabled else 'TẮT'}")
        auto_auth = thai_duong_token.get("auto_auth", {})
        if isinstance(auto_auth, dict) and auto_auth_enabled:
            if auto_auth.get("rotated"):
                lines.append(
                    "Lần kiểm tra này đã gia hạn token: OK "
                    f"| method={auto_auth.get('method', '')}"
                )
            elif auto_auth.get("ok") is False:
                reason = self._short_error(str(auto_auth.get("reason", "Không rõ lý do.")), max_len=180)
                lines.append(f"Lần kiểm tra này chưa gia hạn được: {reason}")

        warnings = thai_duong_token.get("warnings", [])
        if isinstance(warnings, list):
            for item in warnings[:2]:
                lines.append("Cảnh báo Thai Duong: " + self._short_error(str(item), max_len=220))

        api_probe = thai_duong_token.get("api_probe", {})
        if isinstance(api_probe, dict):
            if api_probe.get("ok"):
                lines.append("Probe API Thai Duong: OK")
            elif api_probe.get("skipped"):
                reason = self._short_error(str(api_probe.get("reason", "Không rõ lý do.")), max_len=220)
                lines.append("Probe API Thai Duong: BỎ QUA | " + reason)
            else:
                lines.append(
                    "Probe API Thai Duong: LỖI | "
                    + self._short_error(str(api_probe.get("error", "Không rõ lỗi.")))
                )

        if report.get("ok"):
            lines.append("")
            lines.append("Token Meta và Thái Dương đang ổn, bot có thể chạy bình thường.")
        else:
            lines.append("")
            lines.append(
                "Bot đang có rủi ro dừng chạy ads/đồng bộ do token hoặc quyền API. "
                "Anh cần cập nhật token hoặc quyền trước khi vận hành."
            )
        return "\n".join(lines)

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        seconds = max(0, int(total_seconds))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m"

    @staticmethod
    def _short_error(error: str, max_len: int = 260) -> str:
        normalized = " ".join(str(error).split())
        if len(normalized) <= max_len:
            return normalized
        return normalized[: max_len - 3] + "..."

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fmt_vnd(value: int) -> str:
        return f"{int(value):,} VND"

    @staticmethod
    def _fmt_thb(value: float) -> str:
        numeric = float(value)
        if abs(numeric - int(numeric)) < 1e-9:
            return f"{int(numeric):,} THB"
        return f"{numeric:,.2f} THB"

    def _is_group_message_tagged_for_bot(self, raw_text: str) -> bool:
        username = str(self._bot_username or "").strip().lower()
        if not username:
            return True
        lowered = str(raw_text or "").lower()
        return f"@{username}" in lowered

    def _strip_bot_mention_tokens(self, raw_text: str) -> str:
        username = str(self._bot_username or "").strip()
        text = str(raw_text or "")
        if not username:
            return text
        pattern = rf"(?i)@{re.escape(username)}\b"
        cleaned = re.sub(pattern, " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _is_authorized(self, user_id: int | None) -> bool:
        return user_id == self.settings.telegram_allowed_user_id

    def _is_report_group_chat(self, chat_id: int | None) -> bool:
        notify_chat_id = int(self.settings.daily_report_notify_chat_id)
        if notify_chat_id == 0 or chat_id is None:
            return False
        return int(chat_id) == notify_chat_id

    def _can_use_report(self, *, user_id: int | None, chat_id: int | None) -> bool:
        if self._is_authorized(user_id):
            return True
        return self._is_report_group_chat(chat_id)

    def _can_use_reconcile(self, *, user_id: int | None, chat_id: int | None) -> bool:
        if self._is_authorized(user_id):
            return True
        return self._is_report_group_chat(chat_id)

    def _can_use_media_performance(self, *, user_id: int | None, chat_id: int | None) -> bool:
        if self._is_authorized(user_id):
            return True
        return self._is_report_group_chat(chat_id)

    def _build_duplicate_warning(self, post_url: str, active_jobs: list[dict[str, Any]], next_version: int) -> str:
        lines = [
            "Link này đã tồn tại trong hệ thống.",
            f"Post: {post_url}",
            "",
            "Job đang mở:",
        ]
        for job in active_jobs:
            lines.append(
                f"- {job.get('job_id')} | {job.get('campaign_name')} | v{job.get('version')} | {job.get('status')}"
            )
        lines.extend(
            [
                "",
                f"Anh có muốn tạo phiên bản mới v{next_version} không?",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _build_web_duplicate_warning(active_jobs: list[dict[str, Any]], next_version: int) -> str:
        lines = [
            "Nội dung media Web này đã tồn tại trong hệ thống.",
            "Các job đang mở:",
        ]
        for job in active_jobs:
            lines.append(
                f"- {job.get('job_id')} | {job.get('campaign_name')} | v{job.get('version')} | {job.get('status')}"
            )
        lines.append("")
        lines.append(f"Anh có muốn tạo phiên bản Web mới v{next_version} không?")
        return "\n".join(lines)

    @staticmethod
    def _build_messenger_media_duplicate_warning(
        active_jobs: list[dict[str, Any]],
        next_version: int,
    ) -> str:
        lines = [
            "Nội dung media Messenger này đã tồn tại trong hệ thống.",
            "Các job đang mở:",
        ]
        for job in active_jobs:
            lines.append(
                f"- {job.get('job_id')} | {job.get('campaign_name')} | v{job.get('version')} | {job.get('status')}"
            )
        lines.append("")
        lines.append(f"Anh có muốn tạo phiên bản Messenger mới v{next_version} không?")
        return "\n".join(lines)

    def _build_review_message(self, job: dict[str, Any]) -> str:
        if str(job.get("campaign_variant", "")).strip().lower() == "web":
            return self._build_web_review_message(job)
        if str(job.get("campaign_variant", "")).strip().lower() == "messenger_media":
            return self._build_messenger_media_review_message(job)
        campaign_mode = str(job.get("campaign_mode", "new")).strip().lower()
        adset_lines = "\n".join(f"- {adset_id}" for adset_id in job.get("adset_ids", []))
        ad_lines = "\n".join(f"- {ad_id}" for ad_id in job["ad_ids"])
        selected_adset_lines = "\n".join(f"- {adset_id}" for adset_id in job.get("selected_adset_ids", []))
        fallback_note = ""
        if (
            job.get("requested_destination_type") == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
            and job.get("active_destination_type") == "MESSENGER"
        ):
            fallback_note = (
                "\n\nLưu ý: Meta API của app hiện chưa cho chạy chế độ đích đến tự động ổn định, "
                "nên em đã tự chuyển sang Messenger-only để chiến dịch tạo thành công."
            )
        if campaign_mode == "existing":
            budget_text = (
                f"{job['budget_daily_vnd']:,} VND"
                if int(job.get("budget_daily_vnd", 0)) > 0
                else "không nhập (dùng ngân sách sẵn của campaign cũ)"
            )
            return (
                "Đã tạo nháp ads vào campaign cũ thành công.\n"
                f"- Job ID: {job['job_id']}\n"
                f"- Campaign mode: existing\n"
                f"- Campaign chọn: {job.get('selected_campaign_name', job.get('campaign_name', ''))}\n"
                f"- Campaign ID: {job.get('selected_campaign_id', job.get('campaign_id', ''))}\n"
                f"- Campaign match: {', '.join(job.get('campaign_match_keywords', []))}\n"
                f"- Version: v{job['version']}\n"
                f"- Budget/ngày: {budget_text}\n"
                f"- Message template: {job['message_template_name']}\n"
                f"- Destination type: {job.get('active_destination_type', '')}\n"
                f"- Post map dùng để chạy ads: {job.get('resolved_permalink_url', job.get('post_url', ''))}\n"
                f"- Adset đích ({job.get('selected_adset_count', 0)}):\n{selected_adset_lines}\n"
                f"- Ad IDs mới tạo:\n{ad_lines}\n"
                f"- Link Ads Manager: {job['ads_manager_url']}\n"
                "\n\nKhi bấm Duyệt, em chỉ bật ACTIVE cho các ad mới tạo (không đổi trạng thái campaign/adset cũ)."
                + fallback_note
                + "\n\nAnh bấm Duyệt để publish, hoặc Hủy để rollback ads/creative mới tạo."
            )
        source_copy_note = ""
        if str(job.get("source_adset_id", "")).strip():
            source_copy_note = (
                f"- Source copy: {job.get('source_campaign_name', '')} | "
                f"{job.get('source_adset_name', '')}\n"
                f"- Source ad count: {job.get('source_ad_count', 0)}\n"
            )
        elif str(job.get("source_ad_id", "")).strip():
            source_copy_note = (
                f"- Source copy: same post ad {job.get('source_ad_id', '')}"
                f" ({job.get('source_ad_name', '')})\n"
            )
        return (
            "Đã tạo nháp campaign thành công.\n"
            f"- Job ID: {job['job_id']}\n"
            f"- Campaign: {job['campaign_name']}\n"
            f"- SKU: {job.get('sku_code_text', '')}\n"
            f"- MED: {job.get('media_label', '')}\n"
            f"- Media của: {job.get('media_owner', '')}\n"
            f"- Version: v{job['version']}\n"
            f"- Budget/ngay: {job['budget_daily_vnd']:,} VND\n"
            f"- Objective: {job['objective']}\n"
            f"- Conversion location: {job['conversion_location']}\n"
            f"- Result goal: {job['result_goal']}\n"
            f"- Message template: {job['message_template_name']}\n"
            f"- Destination type: {job.get('active_destination_type', '')}\n"
            f"- Post map dùng để chạy ads: {job.get('resolved_permalink_url', job.get('post_url', ''))}\n"
            f"{source_copy_note}"
            f"- Campaign ID: {job['campaign_id']}\n"
            f"- Adset IDs:\n{adset_lines}\n"
            f"- Ad IDs:\n{ad_lines}\n"
            f"- Link Ads Manager: {job['ads_manager_url']}\n"
            + (
                "\n\nLưu ý: Link pfbid không map trực tiếp được, em tạm dùng bài post thường mới nhất của page. "
                "Anh kiểm tra kỹ link post map rồi hãy bấm Duyệt."
                if job.get("resolved_strategy") == "fallback_latest_non_reel"
                else ""
            )
            + fallback_note
            + "\n\nAnh bấm Duyệt để publish, hoặc Hủy để rollback."
        )

    def _build_web_review_message(self, job: dict[str, Any]) -> str:
        adset_lines = "\n".join(f"- {adset_id}" for adset_id in job.get("adset_ids", []))
        ad_lines = "\n".join(f"- {ad_id}" for ad_id in job.get("ad_ids", []))
        return (
            "Đã tạo nháp campaign Web Sales thành công.\n"
            f"- Job ID: {job['job_id']}\n"
            f"- Campaign: {job.get('campaign_name', '')}\n"
            f"- SKU: {job.get('sku_code_text', '')}\n"
            f"- MED: {job.get('media_label', '')}\n"
            f"- Media của: {job.get('media_owner', '')}\n"
            f"- Version: v{job.get('version', 1)}\n"
            f"- Budget/ngày: {int(job.get('budget_daily_vnd', 0)):,} VND\n"
            f"- Objective: {job.get('objective', '')}\n"
            f"- Conversion location: {job.get('conversion_location', '')}\n"
            f"- Result goal: {job.get('result_goal', '')}\n"
            f"- Media: {job.get('media_kind', '')}\n"
            f"- Tiêu đề: {job.get('headline', '')}\n"
            f"- Nút: {job.get('cta_label', '')} ({job.get('cta_type', '')})\n"
            f"- URL: {job.get('destination_url', '')}\n"
            f"- Chiến lược vòng đời khách hàng: {job.get('customer_lifecycle_label', job.get('customer_lifecycle_strategy', 'Chưa chọn'))}\n"
            f"- Campaign ID: {job.get('campaign_id', '')}\n"
            f"- Adset IDs:\n{adset_lines}\n"
            f"- Ad IDs:\n{ad_lines}\n"
            f"- Link Ads Manager: {job.get('ads_manager_url', '')}\n\n"
            "Bài viết là bài ẩn trong quảng cáo, chưa đăng lên Page. "
            "Anh bấm Duyệt để publish hoặc Hủy để rollback."
        )

    def _build_messenger_media_review_message(self, job: dict[str, Any]) -> str:
        adset_lines = "\n".join(f"- {adset_id}" for adset_id in job.get("adset_ids", []))
        ad_lines = "\n".join(f"- {ad_id}" for ad_id in job.get("ad_ids", []))
        return (
            "Đã tạo nháp campaign Messenger Purchase thành công.\n"
            f"- Job ID: {job.get('job_id', '')}\n"
            f"- Campaign: {job.get('campaign_name', '')}\n"
            f"- SKU: {job.get('sku_code_text', '')}\n"
            f"- MED: {job.get('media_label', '')}\n"
            f"- Media của: {job.get('media_owner', '')}\n"
            f"- Version: v{job.get('version', 1)}\n"
            f"- Budget/ngày: {int(job.get('budget_daily_vnd', 0)):,} VND\n"
            f"- Objective: {job.get('objective', '')}\n"
            f"- Conversion location: {job.get('conversion_location', '')}\n"
            f"- Result goal: {job.get('result_goal', '')}\n"
            f"- Media: {job.get('media_kind', '')}\n"
            f"- Tiêu đề: {job.get('headline', '')}\n"
            "- Nút: Gửi tin nhắn (MESSAGE_PAGE)\n"
            f"- Mẫu tin nhắn: {job.get('message_template_name', 'Chào JC')}\n"
            f"- Chiến lược vòng đời khách hàng: {job.get('customer_lifecycle_label', job.get('customer_lifecycle_strategy', 'Chưa chọn'))}\n"
            f"- Campaign ID: {job.get('campaign_id', '')}\n"
            f"- Adset IDs:\n{adset_lines}\n"
            f"- Ad IDs:\n{ad_lines}\n"
            f"- Link Ads Manager: {job.get('ads_manager_url', '')}\n\n"
            "Creative là bài ẩn trong quảng cáo, không đăng lên Page và không dùng URL website. "
            "Anh bấm Duyệt để publish hoặc Hủy để rollback."
        )

    def _build_published_message(self, job: dict[str, Any]) -> str:
        if str(job.get("campaign_mode", "new")).strip().lower() == "existing":
            return (
                "Publish ads vào campaign cũ thành công.\n"
                f"- Job ID: {job['job_id']}\n"
                f"- Campaign ID: {job['campaign_id']}\n"
                f"- Trạng thái: {job['status']}\n"
                f"- Link Ads Manager: {job['ads_manager_url']}"
            )
        return (
            "Publish thành công.\n"
            f"- Job ID: {job['job_id']}\n"
            f"- Campaign ID: {job['campaign_id']}\n"
            f"- Trạng thái: {job['status']}\n"
            f"- Link Ads Manager: {job['ads_manager_url']}"
        )

    def _build_ads_manager_url(self, campaign_id: str) -> str:
        account = self.settings.meta_ad_account_id
        if account.startswith("act_"):
            account = account[4:]
        return (
            "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
            f"?act={account}&selected_campaign_ids={campaign_id}"
        )

    def _plan_with_destination_override(self, plan: PlannedCampaign, destination_type: str) -> PlannedCampaign:
        raw = copy.deepcopy(plan.raw)
        adset_overrides = raw.setdefault("adset_payload_overrides", {})
        if not isinstance(adset_overrides, dict):
            adset_overrides = {}
            raw["adset_payload_overrides"] = adset_overrides
        adset_overrides["destination_type"] = destination_type
        return replace(plan, raw=raw)
