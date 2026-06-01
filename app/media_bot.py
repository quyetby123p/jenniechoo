from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import io
import logging
import threading
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.exceptions import CommandParseError
from app.media_approval_service import MediaApprovalService
from app.media_command_parser import is_media_command_text, parse_media_caption
from app.media_constants import MEDIA_SHEET_HEADERS
from app.media_research_service import MediaResearchService
from app.media_settings import MediaSettings
from app.media_sheet_service import MediaSheetService
from app.media_storage_service import MediaStorageService
from app.utils import now_utc_iso
from app.work_progress_api import WorkProgressApiServer
from app.work_progress_command_parser import is_work_progress_command, parse_work_progress_command
from app.work_progress_scheduler import WorkProgressScheduler
from app.work_progress_service import WorkProgressService


class MediaResearchBot:
    def __init__(
        self,
        settings: MediaSettings,
        logger: logging.Logger,
        storage: MediaStorageService,
        research: MediaResearchService,
        sheet: MediaSheetService,
        approval: MediaApprovalService,
        work_progress_service: WorkProgressService | None = None,
        work_progress_scheduler: WorkProgressScheduler | None = None,
        work_progress_api_server: WorkProgressApiServer | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.storage = storage
        self.research = research
        self.sheet = sheet
        self.approval = approval
        self.work_progress = work_progress_service
        self.work_progress_scheduler = work_progress_scheduler
        self.work_progress_api_server = work_progress_api_server
        self.router = Router(name="media_research_router")
        self._bot: Bot | None = None
        self._work_progress_scheduler_task: asyncio.Task[None] | None = None
        self._work_progress_api_thread: threading.Thread | None = None

        self.router.message.register(self.handle_start_command, Command("start"))
        self.router.message.register(self.handle_media_help_command, Command("media_help"))
        self.router.message.register(self.handle_media_status_command, Command("media_status"))
        self.router.message.register(self.handle_progress_command, Command("progress"))
        self.router.message.register(self.handle_photo_message, F.photo)
        self.router.message.register(self.handle_text_message, F.text)
        self.router.callback_query.register(self.handle_callback, F.data)

    async def run(self) -> None:
        bot = Bot(token=self.settings.telegram_bot_token)
        self._bot = bot
        dispatcher = Dispatcher()
        dispatcher.include_router(self.router)
        if self.work_progress_api_server:
            self._work_progress_api_thread = threading.Thread(
                target=self.work_progress_api_server.serve_forever,
                daemon=True,
                name="work-progress-api",
            )
            self._work_progress_api_thread.start()
        if self.work_progress and self.work_progress_scheduler:
            self._work_progress_scheduler_task = asyncio.create_task(
                self._work_progress_scheduler_loop(),
                name="work_progress_scheduler_loop",
            )
        self.logger.info("Media bot đang chạy polling...")
        try:
            await dispatcher.start_polling(bot)
        finally:
            if self._work_progress_scheduler_task:
                self._work_progress_scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._work_progress_scheduler_task
            if self.work_progress_api_server:
                with suppress(Exception):
                    self.work_progress_api_server.shutdown()

    async def handle_start_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._help_text())

    async def handle_media_help_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._help_text())

    async def handle_media_status_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._status_text())

    async def handle_progress_command(self, message: Message) -> None:
        await self._handle_progress_command_text(message)

    async def handle_text_message(self, message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        if self.work_progress:
            await self._ingest_work_progress_from_telegram_message(message)
            if is_work_progress_command(text):
                await self._handle_progress_command_text(message)
                return

        if not self._is_authorized(message.from_user.id if message.from_user else None):
            return
        if is_media_command_text(text):
            if not self.settings.media_research_enabled:
                await message.answer("Media research đang tắt (`MEDIA_BOT_MEDIA_RESEARCH_ENABLED=0`).")
                return
            await message.answer(
                "Lệnh tìm media cần gửi kèm ảnh.\n"
                "Ví dụ: gửi ảnh + caption `Tìm media` hoặc `Tìm media JC123 váy hoa`."
            )

    async def handle_photo_message(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        if not self.settings.media_research_enabled:
            await message.answer("Media research đang tắt (`MEDIA_BOT_MEDIA_RESEARCH_ENABLED=0`).")
            return

        caption = str(message.caption or "").strip()
        try:
            command = parse_media_caption(caption)
        except CommandParseError as exc:
            await message.answer(str(exc))
            return
        product_code = command.product_code.strip().upper() or self._auto_product_code()

        local_date = self._now_local().date()
        used = self.storage.get_today_quota_usage(local_date)
        if used >= self.settings.daily_run_cap:
            await message.answer(
                "Đã chạm giới hạn run trong ngày.\n"
                f"- Đã dùng: {used}/{self.settings.daily_run_cap}\n"
                "Anh đợi sang ngày mới hoặc tăng MEDIA_BOT_DAILY_RUN_CAP."
            )
            return

        photo = message.photo[-1] if message.photo else None
        if not photo:
            await message.answer("Không tìm thấy ảnh hợp lệ trong tin nhắn.")
            return

        await message.answer("Đang phân tích ảnh và tìm media thị trường, anh chờ em vài giây...")

        run_id = self.storage.generate_run_id()
        self.storage.increment_today_quota(local_date)

        try:
            photo_bytes = await self._download_photo_bytes(photo.file_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Tai anh Telegram that bai")
            await message.answer(f"Tải ảnh từ Telegram thất bại: {exc}")
            return

        report = await asyncio.to_thread(
            self.research.run_research,
            run_id=run_id,
            product_code=product_code,
            keyword_text=command.keyword_text,
            photo_bytes=photo_bytes,
            photo_filename=f"{product_code}.jpg",
        )

        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        for row in items:
            if isinstance(row, dict):
                row["run_id"] = run_id

        run_payload = {
            "run_id": run_id,
            "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(),
            "status": "completed",
            "chat_id": message.chat.id,
            "product_code": product_code,
            "keyword_text": command.keyword_text,
            "query_text": report.get("query_text", ""),
            "market_scope": report.get("market_scope", self.settings.market_scope),
            "input_image_url": report.get("input_image_url", ""),
            "summary": {
                "ok": bool(report.get("ok")),
                "partial": bool(report.get("partial")),
                "raw_candidate_count": self._to_int(report.get("raw_candidate_count")),
                "selected_count": self._to_int(report.get("selected_count")),
                "image_count": self._to_int(report.get("image_count")),
                "video_count": self._to_int(report.get("video_count")),
            },
            "warnings": report.get("warnings", []),
            "errors": report.get("errors", []),
            "engine_logs": report.get("engine_logs", []),
            "items": items,
        }

        csv_path = self.storage.save_report_csv(run_id=run_id, rows=items, headers=MEDIA_SHEET_HEADERS)
        run_payload["csv_path"] = str(csv_path)
        self.storage.save_run(run_payload)

        await message.answer(self._build_preview_message(run_payload))

        if not items:
            return

        if not self.settings.sheet_enabled:
            await message.answer("Đang tắt ghi Google Sheet (MEDIA_RESEARCH_SHEET_ENABLED=0).")
            return

        is_configured, reason = self.sheet.is_configured()
        if not is_configured:
            await message.answer(
                "Đang bật luồng media nhưng cấu hình sheet chưa đủ.\n"
                f"Lý do: {reason}"
            )
            return

        request_id = self.storage.create_pending_request(
            {
                "run_id": run_id,
                "chat_id": message.chat.id,
                "product_code": product_code,
            },
            request_type="media_sheet_sync",
        )
        self.storage.update_run(run_id, {"status": "awaiting_sheet_approval", "sheet_request_id": request_id})

        await message.answer(
            "Batch media đã sẵn sàng ghi Google Sheet.\n"
            f"- Run ID: {run_id}\n"
            f"- Tổng media: {len(items):,}\n"
            "Anh bấm Duyệt để ghi, hoặc Hủy để bỏ qua.",
            reply_markup=self.approval.sheet_sync_keyboard(request_id=request_id),
        )

    async def handle_callback(self, query: CallbackQuery) -> None:
        if not self._is_authorized(query.from_user.id if query.from_user else None):
            await query.answer("Không có quyền.", show_alert=True)
            return

        action = self.approval.parse_callback(query.data)
        if not action:
            await query.answer()
            return

        if action.action == "media_sheet_apply":
            await self._on_sheet_apply(query, action.value)
            return
        if action.action == "media_sheet_cancel":
            await self._on_sheet_cancel(query, action.value)
            return

        await query.answer()

    async def _on_sheet_apply(self, query: CallbackQuery, request_id: str) -> None:
        request = self.storage.get_pending_request(request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc đã xử lý.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() != "media_sheet_sync":
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return

        run_id = str(request.get("run_id", "")).strip()
        if not run_id:
            await query.answer("Yêu cầu không hợp lệ (thiếu run_id).", show_alert=True)
            self.storage.delete_pending_request(request_id)
            return

        payload = self.storage.load_run(run_id)
        if not payload:
            await query.answer("Không tìm thấy dữ liệu run.", show_alert=True)
            self.storage.delete_pending_request(request_id)
            return

        items = payload.get("items", []) if isinstance(payload.get("items"), list) else []
        await query.answer("Đang ghi dữ liệu media lên Google Sheet...")

        sync_result = await asyncio.to_thread(self.sheet.sync_rows, items)
        self.storage.update_run(
            run_id,
            {
                "status": "sheet_synced" if sync_result.get("ok") else "sheet_sync_failed",
                "sheet_sync": sync_result,
            },
        )
        self.storage.delete_pending_request(request_id)

        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)

        if sync_result.get("ok"):
            if query.message:
                await query.message.answer(
                    "Ghi Google Sheet thành công.\n"
                    f"- Run ID: {run_id}\n"
                    f"- Ghi mới: {self._to_int(sync_result.get('inserted')):,}\n"
                    f"- Cập nhật: {self._to_int(sync_result.get('updated')):,}\n"
                    f"- Bỏ qua: {self._to_int(sync_result.get('skipped')):,}"
                )
            return

        if query.message:
            errors = sync_result.get("errors", []) if isinstance(sync_result.get("errors"), list) else []
            detail = "; ".join(str(err) for err in errors if str(err).strip()) or "Không rõ lỗi."
            await query.message.answer(
                "Ghi Google Sheet thất bại.\n"
                f"- Run ID: {run_id}\n"
                f"- Lỗi: {detail}"
            )

    async def _on_sheet_cancel(self, query: CallbackQuery, request_id: str) -> None:
        self.storage.delete_pending_request(request_id)
        await query.answer("Đã hủy ghi Google Sheet.")
        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy ghi kết quả media lên Google Sheet.")

    async def _work_progress_scheduler_loop(self) -> None:
        if not self.work_progress_scheduler:
            return
        self.logger.info("Bat work progress scheduler trong Bot 2.")
        while True:
            try:
                await asyncio.to_thread(self.work_progress_scheduler.run_once)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Work progress scheduler loop loi: %s", exc)
            await asyncio.sleep(20)

    async def _ingest_work_progress_from_telegram_message(self, message: Message) -> None:
        if not self.work_progress:
            return
        text = _clean_text(message.text or "")
        if not text:
            return
        if text.startswith("/progress"):
            return

        chat_id = str(message.chat.id) if message.chat else ""
        sender_id = str(message.from_user.id) if message.from_user else ""
        if not chat_id or not sender_id:
            return

        payload = {
            "event_id": f"tg:{chat_id}:{message.message_id}",
            "channel_id": chat_id,
            "sender_id": sender_id,
            "message_text": text,
            "event_time": message.date.isoformat() if message.date else "",
            "raw_payload": {
                "chat_id": chat_id,
                "message_id": message.message_id,
                "chat_type": message.chat.type if message.chat else "",
                "from_username": message.from_user.username if message.from_user else "",
                "text": text,
            },
        }
        try:
            await asyncio.to_thread(self.work_progress.ingest_event, "telegram", payload)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Ingest work progress tu Bot 2 that bai: %s", exc)

    async def _handle_progress_command_text(self, message: Message) -> None:
        if not self.work_progress:
            await message.answer("Work progress service đang tắt trong Bot 2.")
            return
        user_id = message.from_user.id if message.from_user else 0
        if not self._is_work_progress_manager(user_id):
            await message.answer("Anh/chị chưa có quyền review/report work progress.")
            return
        raw = str(message.text or "").strip()
        try:
            command = parse_work_progress_command(raw)
        except CommandParseError as exc:
            await message.answer(str(exc))
            return
        try:
            action = command.action
            args = command.args if isinstance(command.args, dict) else {}
            if action == "help":
                await message.answer(self._progress_help_text())
                return
            if action == "pending":
                limit = self._to_int(args.get("limit")) or 20
                rows = await asyncio.to_thread(self.work_progress.list_pending_updates, limit=limit)
                if not rows:
                    await message.answer("Không có update nào đang chờ duyệt.")
                    return
                lines = [f"Danh sách pending ({len(rows)}):"]
                for item in rows[:50]:
                    lines.append(
                        "- {uid} | {member} | {task} | {status} {pct}% | {state} | cf={cf}".format(
                            uid=str(item.get("update_id", "")),
                            member=str(item.get("member_id", "")),
                            task=str(item.get("task_key", "")),
                            status=str(item.get("status", "")),
                            pct=self._to_int(item.get("progress_pct")),
                            state=str(item.get("review_state", "")),
                            cf=round(float(item.get("confidence", 0.0)), 2),
                        )
                    )
                await message.answer("\n".join(lines))
                return
            if action == "unmapped":
                limit = self._to_int(args.get("limit")) or 20
                rows = await asyncio.to_thread(self.work_progress.list_pending_identity_events, limit=limit)
                if not rows:
                    await message.answer("Không có event nào bị thiếu mapping danh tính.")
                    return
                lines = [f"Event pending identity ({len(rows)}):"]
                for item in rows[:50]:
                    lines.append(
                        "- {eid} | {platform} | channel={channel} | sender={sender} | text={text}".format(
                            eid=str(item.get("event_id", "")),
                            platform=str(item.get("platform", "")),
                            channel=str(item.get("channel_id", "")),
                            sender=str(item.get("sender_id", "")),
                            text=str(item.get("message_text", ""))[:80],
                        )
                    )
                await message.answer("\n".join(lines))
                return
            if action == "approve":
                item = await asyncio.to_thread(
                    self.work_progress.approve_update,
                    update_id=str(args.get("update_id", "")),
                    reviewer_id=str(user_id),
                    note=str(args.get("note", "")),
                )
                await message.answer(f"Đã approve: {item.get('update_id')} | {item.get('member_id')} | {item.get('task_key')}")
                return
            if action == "reject":
                item = await asyncio.to_thread(
                    self.work_progress.reject_update,
                    update_id=str(args.get("update_id", "")),
                    reviewer_id=str(user_id),
                    note=str(args.get("note", "")),
                )
                await message.answer(f"Đã reject: {item.get('update_id')} | {item.get('member_id')} | {item.get('task_key')}")
                return
            if action == "edit":
                item = await asyncio.to_thread(
                    self.work_progress.edit_update,
                    update_id=str(args.get("update_id", "")),
                    reviewer_id=str(user_id),
                    patch=args.get("patch", {}),
                    note="edited from bot2",
                    approve_after_edit=True,
                )
                await message.answer(
                    "Đã edit+approve: {uid} | {status} {pct}% | {task}".format(
                        uid=item.get("update_id", ""),
                        status=item.get("status", ""),
                        pct=self._to_int(item.get("progress_pct")),
                        task=item.get("task_key", ""),
                    )
                )
                return
            if action == "map":
                item = await asyncio.to_thread(
                    self.work_progress.upsert_member_identity,
                    member_id=str(args.get("member_id", "")),
                    platform=str(args.get("platform", "")),
                    platform_user_id=str(args.get("platform_user_id", "")),
                    display_name=str(args.get("display_name", "")),
                )
                await message.answer(
                    "Đã map identity:\n"
                    f"- member_id: {item.get('member_id')}\n"
                    f"- platform: {item.get('platform')}\n"
                    f"- platform_user_id: {item.get('platform_user_id')}"
                )
                return
            if action == "report":
                report = await asyncio.to_thread(
                    self.work_progress.build_report,
                    str(args.get("report_type", "")),
                    anchor_date=args.get("anchor_date"),
                )
                text = await asyncio.to_thread(self.work_progress.format_report_text, report)
                await message.answer(text)
                return
            await message.answer("Lệnh progress chưa được hỗ trợ.")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Xử lý progress thất bại: {exc}")

    async def _download_photo_bytes(self, file_id: str) -> bytes:
        if not self._bot:
            raise RuntimeError("Bot chưa khởi tạo.")
        destination = io.BytesIO()
        await self._bot.download(file_id, destination=destination)
        destination.seek(0)
        content = destination.read()
        if not content:
            raise RuntimeError("Ảnh tải về trống.")
        return content

    def _status_text(self) -> str:
        local_date = self._now_local().date()
        used = self.storage.get_today_quota_usage(local_date)
        configured, reason = self.sheet.is_configured()
        sheet_status = "OK" if configured else f"LỖI ({reason})"
        wp_status = "Tắt"
        if self.work_progress:
            wp_status = "Bật"
            with suppress(Exception):
                pending = self.work_progress.list_pending_updates(limit=1)
                wp_status = f"Bật (pending={len(pending)})"
        return (
            "Trạng thái Media Bot:\n"
            f"- Ngày local: {local_date.isoformat()} ({self.settings.timezone_name})\n"
            f"- Media research: {'Bật' if self.settings.media_research_enabled else 'Tắt'}\n"
            f"- Quota hôm nay: {used}/{self.settings.daily_run_cap}\n"
            f"- Sheet sync: {sheet_status}\n"
            f"- Market scope: {self.settings.market_scope}\n"
            f"- Platform allowlist: {', '.join(self.settings.platform_allowlist)}\n"
            f"- Work progress: {wp_status}"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "Hướng dẫn Media Bot:\n"
            "1) Gửi 1 ảnh sản phẩm.\n"
            "2) Caption theo mẫu tự nhiên: Tìm media\n"
            "   Hoặc: Tìm media JC123 váy hoa\n"
            "   (Vẫn hỗ trợ: /media SKU123 váy hoa nữ)\n"
            "3) Bot tìm ảnh/video thị trường và gửi preview.\n"
            "4) Bấm Duyệt để ghi Google Sheet hoặc Hủy để bỏ qua.\n\n"
            "Lệnh bổ sung:\n"
            "- /media_help\n"
            "- /media_status\n"
            "- /progress help (nếu bật work progress trong Bot 2)"
        )

    @staticmethod
    def _progress_help_text() -> str:
        return (
            "Work progress commands (Bot 2):\n"
            "- /progress pending [limit]\n"
            "- /progress unmapped [limit]\n"
            "- /progress approve <update_id> [ghi chú]\n"
            "- /progress reject <update_id> [ghi chú]\n"
            "- /progress edit <update_id> | <status> | <progress_pct> | [blocker] | [next_step] | [deadline]\n"
            "- /progress map <member_id> | <platform> | <platform_user_id> | [display_name]\n"
            "- /progress report <daily|weekly|monthly> [YYYY-MM-DD]"
        )

    def _build_preview_message(self, run_payload: dict[str, Any]) -> str:
        summary = run_payload.get("summary", {}) if isinstance(run_payload.get("summary"), dict) else {}
        items = run_payload.get("items", []) if isinstance(run_payload.get("items"), list) else []
        preview_lines = [
            "Kết quả media research:",
            f"- Run ID: {run_payload.get('run_id', '')}",
            f"- Product code: {run_payload.get('product_code', '')}",
            f"- Query: {run_payload.get('query_text', '')}",
            f"- Raw candidates: {self._to_int(summary.get('raw_candidate_count')):,}",
            f"- Selected: {self._to_int(summary.get('selected_count')):,} (Ảnh: {self._to_int(summary.get('image_count')):,}, Video: {self._to_int(summary.get('video_count')):,})",
            f"- CSV: {run_payload.get('csv_path', '')}",
        ]

        warnings = run_payload.get("warnings", [])
        if isinstance(warnings, list) and warnings:
            preview_lines.append("- Cảnh báo: " + " | ".join(str(item) for item in warnings[:3]))
        errors = run_payload.get("errors", [])
        if isinstance(errors, list) and errors:
            preview_lines.append("- Lỗi: " + " | ".join(str(item) for item in errors[:2]))

        if items:
            preview_lines.append("")
            preview_lines.append("Top media:")
            for row in items[:8]:
                if not isinstance(row, dict):
                    continue
                media_type = str(row.get("media_type", "")).strip()
                platform = str(row.get("platform", "")).strip()
                source_url = str(row.get("source_url", "")).strip() or str(row.get("direct_media_url", "")).strip()
                preview_lines.append(f"- [{media_type}] {platform}: {source_url}")
        return "\n".join(preview_lines)

    def _is_authorized(self, user_id: int | None) -> bool:
        return user_id == self.settings.telegram_allowed_user_id

    def _is_work_progress_manager(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        if self._is_authorized(user_id):
            return True
        if not self.work_progress:
            return False
        manager_ids = set(self.work_progress.settings.manager_telegram_user_ids)
        return int(user_id) in manager_ids

    def _now_local(self) -> datetime:
        return datetime.now(self._resolve_timezone())

    def _auto_product_code(self) -> str:
        return "AUTO" + self._now_local().strftime("%Y%m%d%H%M%S")

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone_name)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()
