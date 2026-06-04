from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.approval_service import ApprovalService
from app.campaign_planner import (
    build_campaign_plan,
    build_existing_campaign_plan,
    build_non_jc_hashtag_suffix,
    extract_jc_codes,
)
from app.command_parser import (
    parse_ads_command,
    parse_reconcile_cod_date_argument,
    parse_report_date_argument,
    try_parse_pancake_td_sync_command,
    try_parse_reconcile_cod_command,
    try_parse_report_command,
)
from app.cloud_schedule_guard import CloudScheduleGuardClient
from app.daily_report_service import DailyReportService
from app.dedup_service import DedupService
from app.exceptions import CommandParseError, MetaApiError, ValidationError
from app.meta_ads_client import MetaAdsClient
from app.models import AdsCommand, AudienceSlot, PlannedCampaign
from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.rollback_service import RollbackService
from app.settings import Settings
from app.storage_service import StorageService
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import dump_json, fingerprint, load_json, normalize_facebook_url, now_utc_iso


_DISABLED_LOCAL_SCHEDULE_CLAIM = object()


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
        self._last_daily_report_send_ok: bool = True

        self.router.message.register(self.handle_start_command, Command("start"))
        self.router.message.register(self.handle_ads_command, Command("ads"))
        self.router.message.register(self.handle_report_command, Command("report"))
        self.router.message.register(self.handle_reconcile_command, Command("reconcile"))
        self.router.message.register(self.handle_token_command, Command("token"))
        self.router.message.register(self.handle_new_chat_members, F.new_chat_members)
        self.router.message.register(self.handle_text_message, F.text)
        self.router.callback_query.register(self.handle_callback, F.data)

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

    async def handle_start_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return

        await message.answer(
            "Em đã sẵn sàng.\n"
            "Lên campaign mới: <link> ngân sách 300000 lên mới\n"
            "Lên campaign cũ: <link> JCV140 lên cũ\n"
            "Lên campaign cũ theo hint: <link> lên cũ camp video\n"
            "Khi cần kiểm tra token ngay: /token\n"
            "Khi cần xem báo cáo ngày: /report hoặc /report YYYY-MM-DD\n"
            "Khi cần đối soát COD: /reconcile cod hoặc /reconcile cod YYYY-MM-DD\n"
            "Khi cần lên đơn Thái Dương thủ công: lên đơn hôm nay hoặc lên đơn JCT310"
        )

    async def handle_ads_command(self, message: Message) -> None:
        if self._is_report_group_chat(message.chat.id if message.chat else None):
            return
        await self._process_ads_input(message, message.text or "")

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

    async def _process_ads_input(self, message: Message, raw_text: str) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return

        try:
            command = parse_ads_command(raw_text)
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
        )
        post_fingerprint = fingerprint(normalized_url)
        dedup_info = await asyncio.to_thread(self.dedup.inspect, post_fingerprint)

        if dedup_info["is_duplicate"]:
            version = int(dedup_info["next_version"])
            request_id = await asyncio.to_thread(
                self.storage.create_pending_request,
                {
                    "post_url": command.post_url,
                    "budget_daily_vnd": command.budget_daily_vnd,
                    "post_fingerprint": post_fingerprint,
                    "version": version,
                    "use_existing_campaign": command.use_existing_campaign,
                    "manual_sku_keywords": list(command.manual_sku_keywords),
                    "existing_campaign_hint": str(command.existing_campaign_hint or ""),
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
        if command.use_existing_campaign:
            await self._start_existing_campaign_draft_flow(
                chat_id=message.chat.id,
                command=command,
                post_fingerprint=post_fingerprint,
                version=version,
            )
            return
        await self._create_draft_and_send_review(message.chat.id, command, post_fingerprint, version)

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

        command = AdsCommand(
            post_url=str(request["post_url"]),
            budget_daily_vnd=int(request["budget_daily_vnd"]),
            use_existing_campaign=bool(request.get("use_existing_campaign", False)),
            manual_sku_keywords=[str(item) for item in request.get("manual_sku_keywords", [])],
            existing_campaign_hint=str(request.get("existing_campaign_hint", "") or ""),
        )
        post_fingerprint = str(request["post_fingerprint"])
        version = int(request["version"])

        await query.answer("Đang tạo nháp v2...")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        target_chat_id = query.message.chat.id if query.message else query.from_user.id
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

    async def _on_duplicate_cancel(self, query: CallbackQuery, request_id: str) -> None:
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
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
            await query.message.answer(
                "Kết quả ghi Google Sheet:\n"
                f"- Tổng ghi thử: {self._to_int(sync_result.get('attempted')):,}\n"
                f"- Ghi mới: {self._to_int(sync_result.get('inserted')):,}\n"
                f"- Bỏ qua trùng: {self._to_int(sync_result.get('skipped_existing')):,}"
            )
            return
        errors = sync_result.get("errors", [])
        error_text = ", ".join(str(item) for item in errors if str(item).strip()) or "Không rõ lỗi."
        await query.message.answer(
            "Ghi Google Sheet thất bại.\n"
            f"- Tổng ghi thử: {self._to_int(sync_result.get('attempted')):,}\n"
            f"- Lỗi: {error_text}"
        )

    async def _on_reconcile_sheet_cancel(self, query: CallbackQuery, request_id: str) -> None:
        await asyncio.to_thread(self.storage.delete_pending_request, request_id)
        await query.answer("Đã hủy ghi Google Sheet.")
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy ghi kết quả đối soát COD lên Google Sheet.")

    async def _on_approve(self, query: CallbackQuery, job_id: str) -> None:
        job_entry = await asyncio.to_thread(self.storage.find_job, job_id)
        if not job_entry:
            await query.answer("Không tìm thấy job.", show_alert=True)
            return
        status, job = job_entry
        if status != "pending":
            await query.answer(f"Job đã ở trạng thái {status}.", show_alert=True)
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
            rollback_campaign_id = campaign_value if (publish_scope != "ads_only" and campaign_value) else None
            rollback_adset_ids = [str(item) for item in job.get("adset_ids", [])] if publish_scope != "ads_only" else []
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

    async def _on_reject(self, query: CallbackQuery, job_id: str) -> None:
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
        objective = load_json(self.settings.objective_config_path)
        templates = load_json(self.settings.message_templates_path)

        campaign_id: str | None = None
        adset_ids: list[str] = []
        ad_ids: list[str] = []
        creative_ids: list[str] = []

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
            multi_destination_asset_feed_spec: dict[str, Any] | None = None

            if active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                try:
                    multi_destination_asset_feed_spec = await asyncio.to_thread(
                        self.meta.get_account_multi_destination_asset_feed_spec
                    )
                except (ValidationError, MetaApiError) as exc:
                    self.logger.warning(
                        "Khong lay duoc asset_feed_spec tham chieu cho luong len moi: %s",
                        exc,
                    )

            campaign_id = await asyncio.to_thread(self.meta.create_campaign, active_plan)

            for slot in active_plan.audiences:
                adset_id: str | None = None
                creative_id: str | None = None
                ad_id: str | None = None
                creative_extra_overrides: dict[str, Any] | None = None
                if (
                    active_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                    and isinstance(multi_destination_asset_feed_spec, dict)
                    and multi_destination_asset_feed_spec
                ):
                    creative_extra_overrides = {"asset_feed_spec": multi_destination_asset_feed_spec}
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
                        active_plan = self._plan_with_destination_override(active_plan, "MESSENGER")
                        active_destination_type = "MESSENGER"
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
                        creative_id = await asyncio.to_thread(
                            self.meta.create_ad_creative,
                            active_plan,
                            slot,
                            resolved_post,
                            None,
                            None,
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
            resolved_post = await asyncio.to_thread(self.meta.resolve_post, command.post_url)
            await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
            keywords = self._resolve_existing_campaign_keywords(command, resolved_post.message_text)
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
        objective = load_json(self.settings.objective_config_path)
        templates = load_json(self.settings.message_templates_path)

        campaign_id = str(selected_campaign.get("id", "")).strip()
        selected_campaign_name = str(selected_campaign.get("name", "")).strip() or campaign_id
        selected_adset_ids: list[str] = []
        ad_ids: list[str] = []
        creative_ids: list[str] = []
        resolved_post = None
        account_multi_destination_asset_feed_spec: dict[str, Any] | None = None
        account_multi_destination_asset_feed_spec_checked = False

        try:
            if not campaign_id:
                raise ValidationError("Campaign cũ không hợp lệ, thiếu campaign_id.")

            resolved_post = await asyncio.to_thread(self.meta.resolve_post, command.post_url)
            await asyncio.to_thread(self.meta.ensure_ads_token_can_access_page)
            active_keywords = campaign_keywords or self._resolve_existing_campaign_keywords(
                command,
                resolved_post.message_text,
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
            ad_name_sku_code_text = "ALL" if str(command.existing_campaign_hint or "").strip() else plan.sku_code_text
            requested_destination_type = "INHERIT_ADSET"
            active_plan = plan
            active_destination_type = "INHERIT_ADSET"
            destination_fallback_reason = ""

            adsets = await asyncio.to_thread(self.meta.list_eligible_adsets, campaign_id, 20)
            if not adsets:
                raise ValidationError(
                    "Campaign đã chọn không có adset hợp lệ (ACTIVE/PAUSED) để lên ads."
                )
            selected_adset_ids = [str(item.get("id", "")).strip() for item in adsets if str(item.get("id", "")).strip()]
            non_jc_suffix = build_non_jc_hashtag_suffix(resolved_post.message_text)

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

            async def _try_duplicate_from_existing_ad(
                adset_id: str,
                slot: AudienceSlot,
                trigger_error: str,
            ) -> str | None:
                if not resolved_post:
                    return None
                story_id_candidates = self._build_story_id_candidates(resolved_post, command.post_url)
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

            for index, adset in enumerate(adsets, start=1):
                adset_id = str(adset.get("id", "")).strip()
                if not adset_id:
                    continue
                adset_name = str(adset.get("name", "")).strip() or adset_id
                adset_destination_type = str(adset.get("destination_type", "")).strip().upper() or "MESSENGER"
                creative_extra_overrides: dict[str, Any] = {}
                if adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                    try:
                        asset_feed_spec = await asyncio.to_thread(
                            self.meta.get_multi_destination_asset_feed_spec,
                            adset_id,
                        )
                        creative_extra_overrides["asset_feed_spec"] = asset_feed_spec
                    except (ValidationError, MetaApiError) as exc:
                        self.logger.warning(
                            "Khong lay duoc asset_feed_spec tu adset %s: %s",
                            adset_id,
                            exc,
                        )
                    if "asset_feed_spec" not in creative_extra_overrides:
                        account_asset_feed_spec = await _get_account_asset_feed_spec_cached()
                        if isinstance(account_asset_feed_spec, dict) and account_asset_feed_spec:
                            creative_extra_overrides["asset_feed_spec"] = account_asset_feed_spec
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
                            )
                            if duplicated_ad_id:
                                ad_id = duplicated_ad_id
                            else:
                                raise
                    elif (
                        adset_destination_type == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"
                        and self.meta.is_auto_destination_error(error_text)
                    ):
                        account_asset_feed_spec = await _get_account_asset_feed_spec_cached()
                        can_retry_with_account_spec = (
                            isinstance(account_asset_feed_spec, dict)
                            and bool(account_asset_feed_spec)
                            and creative_extra_overrides.get("asset_feed_spec") != account_asset_feed_spec
                        )
                        if can_retry_with_account_spec:
                            if ad_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [ad_id], [])
                                ad_id = None
                            if creative_id:
                                await asyncio.to_thread(self.rollback.rollback, None, [], [], [creative_id])
                                creative_id = None
                            retry_overrides = {"asset_feed_spec": account_asset_feed_spec}
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
                            raise ValidationError(
                                "Mode lên cũ chỉ tạo ads, không đổi cấu hình campaign/adset.\n"
                                "Adset hiện tại đang đa đích (Messenger + Instagram) và Meta từ chối creative mới cho post này.\n"
                                "Anh thử lại sau vài phút hoặc chọn adset khác trong cùng campaign."
                            ) from exc
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
                "publish_scope": "ads_only",
                "campaign_id": campaign_id,
                "adset_ids": [],
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
            await asyncio.to_thread(self.rollback.rollback, None, [], ad_ids, creative_ids)
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
                "selected_campaign_id": campaign_id,
                "selected_campaign_name": selected_campaign_name,
                "selected_adset_ids": selected_adset_ids,
                "selected_adset_count": len(selected_adset_ids),
                "adset_ids": [],
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

    def _resolve_existing_campaign_keywords(self, command: AdsCommand, post_message: str) -> list[str]:
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

        hashtags = extract_jc_codes(post_message or "")
        if hashtags:
            return hashtags
        raise ValidationError(
            "Mode lên cũ cần SKU để map campaign.\n"
            "Anh thêm SKU trong lệnh (ví dụ JCV140 lên cũ) hoặc thêm hashtag #JC... vào bài viết."
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
    ) -> None:
        await asyncio.to_thread(
            self.cloud_schedule_guard.mark_completed,
            task=task,
            slot=slot,
            run_date=run_date,
            bucket=bucket,
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
    ) -> str:
        parts = [str(task).strip()]
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
    ) -> Path | object | None:
        if not self.cloud_schedule_guard.is_configured():
            return _DISABLED_LOCAL_SCHEDULE_CLAIM
        key = self._local_schedule_key(task=task, slot=slot, run_date=run_date, bucket=bucket)
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
        await self._retry_pending_daily_reports_on_startup(notify_chat_ids)
        await self._send_missed_morning_daily_report_on_startup(notify_chat_ids)
        while True:
            wait_seconds, slot = self._seconds_until_next_daily_report_schedule()
            self.logger.info("Daily report lan tiep theo sau %s giay (%s)", wait_seconds, slot)
            await asyncio.sleep(wait_seconds)
            run_date = datetime.now(self._resolve_timezone()).date()
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
                    "Daily report slot %s that bai mot phan/hoan toan, se thu gui lai khi bot khoi dong lai va co mang.",
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
        return max(1, int(delta.total_seconds())), next_slot

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
        return max(1, int(delta.total_seconds())), slots

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
        return max(1, int(delta.total_seconds()))

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
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
            self._last_daily_report_send_ok = True
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao ngay qua Telegram that bai")
            self._last_daily_report_send_ok = False
        return report

    async def _send_reconcile_cod_cash_in_report(self, *, chat_id: int, trigger_label: str) -> bool:
        if not self._bot or not self.reconcile:
            return False
        try:
            report = await asyncio.to_thread(self.reconcile.generate_report, None)
            summary = await asyncio.to_thread(self.reconcile.summarize_cash_in_from_report, report)
            text = self._build_reconcile_cash_in_message(summary=summary, trigger_label=trigger_label)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Bao cao tien ve tu dong that bai")
            text = (
                f"{trigger_label}\n"
                "Tổng quan: LỖI\n"
                f"Chi tiết: {self._short_error(str(exc), max_len=220)}"
            )
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui bao cao tien ve tu dong qua Telegram that bai")
            return False

    async def _send_reconcile_cod_weekly_summary_report(self, *, chat_id: int, trigger_label: str) -> bool:
        if not self._bot or not self.reconcile:
            return False
        try:
            weekly = await asyncio.to_thread(self.reconcile.build_weekly_cash_in_summary)
            text = self._build_reconcile_weekly_cash_in_message(weekly=weekly, trigger_label=trigger_label)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Bao cao tong tuan tu dong that bai")
            text = (
                f"{trigger_label}\n"
                "Tổng quan: LỖI\n"
                f"Chi tiết: {self._short_error(str(exc), max_len=220)}"
            )
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
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
                report_cache[report_key] = payload
                return payload
        try:
            payload = self.reports.generate_report(report_date)
        except Exception:  # noqa: BLE001
            self.logger.warning("Khong tao duoc report bo sung cho rollup ngay %s", report_key)
            return None
        if isinstance(payload, dict):
            report_cache[report_key] = payload
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
    ) -> None:
        if not self._bot or not self.reconcile:
            return
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
        if report.get("ok") and not notify_success:
            return

        text = self.reconcile.build_message(report, trigger_label=trigger_label)
        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            self.logger.exception("Gui ket qua doi soat COD qua Telegram that bai")
            return

        csv_path = Path(str(report.get("csv_path", "")).strip())
        if csv_path.exists():
            try:
                await self._bot.send_document(
                    chat_id=chat_id,
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
                return
            run_id = str(report.get("run_id", "")).strip()
            records = report.get("records", [])
            record_count = len(records) if isinstance(records, list) else 0
            if run_id and record_count > 0:
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
                            "Anh bấm Duyệt để ghi dữ liệu lên Sheet hoặc Hủy để bỏ qua."
                        ),
                        reply_markup=self.approval.reconcile_sheet_sync_keyboard(request_id=request_id),
                    )
                except Exception:  # noqa: BLE001
                    self.logger.exception("Gui yeu cau duyet ghi Google Sheet that bai")
                    await asyncio.to_thread(self.storage.delete_pending_request, request_id)

        if not allow_update_prompt or not self.settings.reconcile_cod_update_enabled:
            return
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if self._to_int(summary.get("update_candidates")) <= 0:
            return
        run_id = str(report.get("run_id", "")).strip()
        if not run_id:
            return
        try:
            apply_summary = await asyncio.to_thread(self.reconcile.apply_updates, run_id)
        except Exception:  # noqa: BLE001
            self.logger.exception("Tu dong cap nhat status Pancake tu doi soat COD that bai")
            return
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

    def _build_review_message(self, job: dict[str, Any]) -> str:
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
        return (
            "Đã tạo nháp campaign thành công.\n"
            f"- Job ID: {job['job_id']}\n"
            f"- Campaign: {job['campaign_name']}\n"
            f"- SKU: {job.get('sku_code_text', '')}\n"
            f"- MED: {job.get('media_label', '')}\n"
            f"- Version: v{job['version']}\n"
            f"- Budget/ngay: {job['budget_daily_vnd']:,} VND\n"
            f"- Objective: {job['objective']}\n"
            f"- Conversion location: {job['conversion_location']}\n"
            f"- Result goal: {job['result_goal']}\n"
            f"- Message template: {job['message_template_name']}\n"
            f"- Destination type: {job.get('active_destination_type', '')}\n"
            f"- Post map dùng để chạy ads: {job.get('resolved_permalink_url', job.get('post_url', ''))}\n"
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
