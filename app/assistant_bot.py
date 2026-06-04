from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta
import html
import logging
from pathlib import Path
import re
from typing import Any
import unicodedata

import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    MenuButtonCommands,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    Message,
)

from app.assistant_approval_service import AssistantApprovalService
from app.assistant_command_parser import parse_assistant_command
from app.assistant_memory_service import AssistantMemoryService
from app.assistant_models import AssistantIntent, ParsedAssistantCommand
from app.assistant_google_service import AssistantGoogleService
from app.assistant_internal_ops_service import AssistantInternalOpsService
from app.assistant_openai_service import AssistantOpenAIService
from app.assistant_scheduler_service import AssistantSchedulerService
from app.assistant_settings import AssistantSettings
from app.assistant_storage_service import AssistantStorageService
from app.assistant_task_service import AssistantTaskService
from app.exceptions import CommandParseError


class TelegramAssistantBot:
    def __init__(
        self,
        settings: AssistantSettings,
        logger: logging.Logger,
        storage: AssistantStorageService,
        memory: AssistantMemoryService,
        google: AssistantGoogleService,
        openai: AssistantOpenAIService,
        internal_ops: AssistantInternalOpsService,
        approval: AssistantApprovalService,
        scheduler: AssistantSchedulerService,
        tasks: AssistantTaskService,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.storage = storage
        self.memory = memory
        self.google = google
        self.openai = openai
        self.internal_ops = internal_ops
        self.approval = approval
        self.scheduler = scheduler
        self.tasks = tasks
        self.router = Router(name="assistant_router")
        self._bot: Bot | None = None
        self._agenda_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._eod_task: asyncio.Task[None] | None = None
        self._task_weekly_task: asyncio.Task[None] | None = None
        self._daily_task_checkin_task: asyncio.Task[None] | None = None
        self._memory_rebuild_task: asyncio.Task[None] | None = None
        self._bot_username: str = ""

        self.router.message.register(self.handle_start_command, Command("start"))
        self.router.message.register(self.handle_help_command, Command("assistant_help"))
        self.router.message.register(self.handle_status_command, Command("assistant_status"))
        self.router.message.register(self.handle_agenda_command, Command("agenda"))
        self.router.message.register(self.handle_plan_command, Command("plan"))
        self.router.message.register(self.handle_result_command, Command("result"))
        self.router.message.register(self.handle_run_command, Command("run"))
        self.router.message.register(self.handle_ask_command, Command("ask"))
        self.router.message.register(self.handle_task_command, Command("task"))
        self.router.message.register(self.handle_text_message, F.text)
        self.router.callback_query.register(self.handle_callback, F.data)

    async def run(self) -> None:
        self._memory_rebuild_task = asyncio.create_task(
            self._rebuild_memory_index_background(),
            name="assistant_memory_rebuild",
        )
        bot = Bot(token=self.settings.telegram_bot_token)
        self._bot = bot
        with suppress(Exception):
            me = await bot.get_me()
            self._bot_username = str(me.username or "").strip().lower()
        await self._setup_bot_commands()
        dispatcher = Dispatcher()
        dispatcher.include_router(self.router)

        if self.settings.proactive_enabled:
            self._agenda_task = asyncio.create_task(self._agenda_scheduler_loop(), name="assistant_agenda_loop")
            self._event_task = asyncio.create_task(self._event_scheduler_loop(), name="assistant_event_loop")
            self._eod_task = asyncio.create_task(self._eod_scheduler_loop(), name="assistant_eod_loop")
        if self.settings.tasks_enabled and self.settings.task_weekly_summary_enabled:
            # Keep task weekly summary independent from legacy proactive schedulers.
            self._task_weekly_task = asyncio.create_task(
                self._task_weekly_summary_loop(),
                name="assistant_task_weekly_summary_loop",
            )
        if self.settings.tasks_enabled and self.settings.daily_task_checkin_enabled:
            self._daily_task_checkin_task = asyncio.create_task(
                self._daily_task_checkin_loop(),
                name="assistant_daily_task_checkin_loop",
            )

        self.logger.info("Assistant bot dang chay polling...")
        try:
            await dispatcher.start_polling(bot)
        finally:
            for task in (
                self._agenda_task,
                self._event_task,
                self._eod_task,
                self._task_weekly_task,
                self._daily_task_checkin_task,
                self._memory_rebuild_task,
            ):
                if task:
                    task.cancel()
            for task in (
                self._agenda_task,
                self._event_task,
                self._eod_task,
                self._task_weekly_task,
                self._daily_task_checkin_task,
                self._memory_rebuild_task,
            ):
                if task:
                    with suppress(asyncio.CancelledError):
                        await task

    async def _rebuild_memory_index_background(self) -> None:
        try:
            result = await asyncio.to_thread(self.memory.rebuild_index)
            self.logger.info(
                "Rebuild assistant memory index xong: files=%s inserted=%s updated=%s skipped=%s",
                result.get("files_total"),
                result.get("inserted"),
                result.get("updated"),
                result.get("skipped"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Rebuild assistant memory index that bai: %s", exc)

    async def handle_start_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._help_text())

    async def handle_help_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._help_text())

    async def handle_status_command(self, message: Message) -> None:
        if not self._is_authorized(message.from_user.id if message.from_user else None):
            await message.answer("Xin lỗi, anh/chị không có quyền sử dụng bot này.")
            return
        await message.answer(self._status_text())

    async def handle_agenda_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_plan_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_result_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_run_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_ask_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_task_command(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_text_message(self, message: Message) -> None:
        await self._handle_message_by_parser(message)

    async def handle_callback(self, query: CallbackQuery) -> None:
        if not self._is_authorized(query.from_user.id if query.from_user else None):
            await query.answer("Không có quyền.", show_alert=True)
            return
        action = self.approval.parse_callback(query.data)
        if not action:
            await query.answer()
            return
        if action.action == "confirm_action":
            await self._on_confirm_action(query, action.value)
            return
        if action.action == "cancel_action":
            await self._on_cancel_action(query, action.value)
            return
        await query.answer()

    async def _handle_message_by_parser(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        chat_id = message.chat.id if message.chat else None
        raw = str(message.text or "").strip()
        if not raw:
            return
        if self._is_private_chat(message) and self._is_authorized(user_id) and self.settings.tasks_enabled:
            daily_reply = self._continue_daily_task_checkin_if_active(
                raw=raw,
                user_id=int(user_id or 0),
                chat_id=int(chat_id or 0),
            )
            if daily_reply:
                await self._send_and_log(chat_id or 0, raw_text=raw, intent="daily_task_checkin", reply=daily_reply)
                return
            draft_reply = self._continue_task_wizard_if_active(raw=raw, user_id=int(user_id or 0), chat_id=int(chat_id or 0))
            if draft_reply:
                await self._send_and_log(chat_id or 0, raw_text=raw, intent="task_wizard", reply=draft_reply)
                return
            start_reply = self._try_start_task_wizard(raw=raw, user_id=int(user_id or 0), chat_id=int(chat_id or 0))
            if start_reply:
                await self._send_and_log(chat_id or 0, raw_text=raw, intent="task_wizard", reply=start_reply)
                return
        raw_for_parse = self._strip_bot_mention_tokens(raw)
        try:
            command = parse_assistant_command(raw_for_parse, self.settings.timezone_name)
        except CommandParseError as exc:
            if self._is_task_group_chat(chat_id) and self.settings.task_require_tag and not self._is_group_message_tagged_for_bot(raw):
                return
            await message.answer(str(exc))
            return

        if not self._can_access_command(user_id=user_id, chat_id=chat_id, command=command):
            return
        if command.intent == AssistantIntent.TASK and self._is_task_group_chat(chat_id):
            if self.settings.task_require_tag and not self._is_group_message_tagged_for_bot(raw):
                return

        ok, count = self.storage.check_and_increment_rate_limit(user_id=user_id or 0)
        if not ok:
            await message.answer(
                "Đang chạm giới hạn tần suất xử lý. "
                f"Anh đợi 1 phút rồi thử lại giúp em. (count={count}/{self.settings.rate_limit_per_minute})"
            )
            return
        await self._handle_parsed_command(message, command, raw_text=raw)

    async def _handle_parsed_command(self, message: Message, command: ParsedAssistantCommand, *, raw_text: str) -> None:
        chat_id = message.chat.id
        if command.intent == AssistantIntent.AGENDA:
            reply = await self._build_agenda_reply(command.date_value)
            await self._send_and_log(chat_id, raw_text=raw_text, intent=command.intent.value, reply=reply)
            return
        if command.intent == AssistantIntent.PLAN:
            reply = await self._build_plan_reply(command.date_value, week_mode=command.week_mode)
            await self._send_and_log(chat_id, raw_text=raw_text, intent=command.intent.value, reply=reply)
            return
        if command.intent == AssistantIntent.RESULT:
            reply = await self._build_result_reply(command.date_value)
            await self._send_and_log(chat_id, raw_text=raw_text, intent=command.intent.value, reply=reply)
            return
        if command.intent == AssistantIntent.ACTION:
            if not self._is_authorized(message.from_user.id if message.from_user else None):
                return
            await self._request_action_confirmation(chat_id, command, raw_text=raw_text)
            return
        if command.intent == AssistantIntent.TASK:
            await self._handle_task_intent(message, command, raw_text=raw_text)
            return
        if command.intent == AssistantIntent.GENERAL_QA:
            reply = await self._build_general_qa_reply(command.question_text or raw_text)
            await self._send_and_log(chat_id, raw_text=raw_text, intent=command.intent.value, reply=reply)
            return
        await self._bot_send_message(chat_id, self._help_text())

    def _can_access_command(self, *, user_id: int | None, chat_id: int | None, command: ParsedAssistantCommand) -> bool:
        if self._is_authorized(user_id):
            return True
        if command.intent != AssistantIntent.TASK:
            return False
        if not self._is_task_group_chat(chat_id):
            return False
        action = str(command.task_action or "").strip()
        if action not in {"report", "week", "pending_report", "done_report", "list"}:
            return False
        return self._is_task_group_viewer(user_id)

    def _try_start_task_wizard(self, *, raw: str, user_id: int, chat_id: int) -> str:
        title = self._extract_task_title_from_natural(raw)
        if not title:
            return ""
        draft = {
            "mode": "task_create_wizard",
            "step": "await_description",
            "chat_id": int(chat_id),
            "title": title,
            "source_type": "manager",
            "description": "",
            "deadline_date": "",
        }
        self.storage.save_task_draft(user_id=user_id, payload=draft)
        return (
            f"Đã nhận tên task: `{title}`.\n"
            "Anh gửi nội dung task giúp em (mô tả chi tiết việc cần làm).\n"
            "Nếu muốn hủy: `/cancel`."
        )

    def _continue_task_wizard_if_active(self, *, raw: str, user_id: int, chat_id: int) -> str:
        draft = self.storage.load_task_draft(user_id=user_id)
        if not draft or str(draft.get("mode", "")).strip() != "task_create_wizard":
            return ""

        normalized = _normalize_question_text(raw)
        if normalized in {"cancel", "huy", "huy bo", "bo qua"} or str(raw).strip().lower() == "/cancel":
            self.storage.delete_task_draft(user_id=user_id)
            return "Đã hủy tạo task."

        step = str(draft.get("step", "")).strip()
        if step == "await_description":
            description = " ".join(str(raw or "").split()).strip()
            if not description:
                return "Nội dung task đang trống, anh gửi lại mô tả giúp em."
            draft["description"] = description
            draft["step"] = "await_deadline"
            self.storage.save_task_draft(user_id=user_id, payload=draft)
            return (
                "Đã nhận nội dung task.\n"
                "Anh nhập deadline (YYYY-MM-DD hoặc DD/MM hoặc 'không')."
            )

        if step == "await_deadline":
            ok, deadline_value, error_text = self._parse_deadline_input(raw)
            if not ok:
                return error_text
            draft["deadline_date"] = deadline_value
            draft["step"] = "await_status"
            self.storage.save_task_draft(user_id=user_id, payload=draft)
            return "Anh nhập tình trạng task: `chưa làm` / `đang làm` / `hoàn thành`."

        if step == "await_status":
            status = self._parse_task_status_input(raw)
            if not status:
                return "Tình trạng chưa hợp lệ. Anh nhập: `chưa làm` / `đang làm` / `hoàn thành`."
            title = str(draft.get("title", "")).strip()
            description = str(draft.get("description", "")).strip()
            deadline_date = str(draft.get("deadline_date", "")).strip()
            try:
                task = self.tasks.create_task(
                    title=title,
                    created_by=int(user_id),
                    source_type=str(draft.get("source_type", "manager")),
                    assigned_by=int(self.settings.task_manager_user_ids[0]) if self.settings.task_manager_user_ids else 0,
                    group_chat_id=int(self.settings.task_group_chat_id),
                    note=description,
                    deadline_date=deadline_date,
                )
                task_uid = str(task.get("task_uid", "")).strip()
                if status == "doing":
                    task = self.tasks.update_task(
                        task_uid=task_uid,
                        updated_by=int(user_id),
                        chat_id=int(chat_id),
                        status="doing",
                        progress_percent=50,
                        note="Khởi tạo từ wizard",
                        blocked_reason="Chưa cập nhật",
                        next_step="Chưa cập nhật",
                        action_name="wizard_init",
                    )
                elif status == "done":
                    task = self.tasks.mark_done(
                        task_uid=task_uid,
                        updated_by=int(user_id),
                        chat_id=int(chat_id),
                        note="Khởi tạo từ wizard",
                    )
            except Exception as exc:  # noqa: BLE001
                return f"Tạo task thất bại: {exc}"
            finally:
                self.storage.delete_task_draft(user_id=user_id)
            deadline_label = deadline_date or "Không có"
            return (
                "Đã lưu task thành công:\n"
                f"- ID: {task.get('task_uid')}\n"
                f"- Tên: {task.get('title')}\n"
                f"- Deadline: {deadline_label}\n"
                f"- Trạng thái: {self._status_label_vi(str(task.get('status', '')))}"
            )
        self.storage.delete_task_draft(user_id=user_id)
        return "Phiên tạo task bị lỗi bước xử lý, anh thử lại: `thêm công việc: <tên task>`."

    def _continue_daily_task_checkin_if_active(self, *, raw: str, user_id: int, chat_id: int) -> str:
        draft = self.storage.load_task_draft(user_id=user_id)
        if not draft:
            return ""
        mode = str(draft.get("mode", "")).strip()
        if mode not in {"daily_task_morning", "daily_task_evening"}:
            return ""

        normalized = _normalize_question_text(raw)
        if normalized in {"cancel", "huy", "huy bo", "bo qua"} or str(raw).strip().lower() == "/cancel":
            self.storage.delete_task_draft(user_id=user_id)
            return "Đã hủy phiên check-in task hôm nay."

        if mode == "daily_task_morning":
            return self._handle_daily_task_morning_reply(raw=raw, user_id=user_id, chat_id=chat_id, draft=draft)
        return self._handle_daily_task_evening_reply(raw=raw, user_id=user_id, chat_id=chat_id, draft=draft)

    def _handle_daily_task_morning_reply(
        self,
        *,
        raw: str,
        user_id: int,
        chat_id: int,
        draft: dict[str, Any],
    ) -> str:
        day_key = str(draft.get("date", "")).strip() or self.scheduler.now_local().date().isoformat()
        state = self.storage.load_daily_task_checkin_state()
        day_state = self._get_daily_task_day_state(state, day_key)

        normalized = _normalize_question_text(raw)
        if normalized in {"khong co", "khong", "hom nay khong co", "khong co viec"}:
            day_state["morning_answered"] = True
            day_state["no_tasks"] = True
            day_state["task_uids"] = []
            self._save_daily_task_day_state(state, day_key, day_state)
            self.storage.delete_task_draft(user_id=user_id)
            return "Em đã ghi nhận hôm nay anh chưa có task mới. 17h em sẽ không hỏi tiến độ."

        titles = self._parse_daily_task_title_lines(raw)
        if not titles:
            return "Em chưa tách được task nào. Anh gửi mỗi dòng một việc, hoặc gửi `không có`."

        max_items = int(self.settings.daily_task_max_items)
        if len(titles) > max_items:
            titles = titles[:max_items]

        created: list[dict[str, Any]] = []
        for title in titles:
            task = self.tasks.create_task(
                title=title,
                created_by=int(user_id),
                source_type="self",
                assigned_by=int(user_id),
                group_chat_id=int(self.settings.task_group_chat_id),
                note=f"Daily check-in {day_key}",
                deadline_date=day_key,
            )
            if task:
                created.append(task)

        existing_uids = [str(item).strip() for item in day_state.get("task_uids", []) if str(item).strip()]
        new_uids = [str(item.get("task_uid", "")).strip() for item in created if str(item.get("task_uid", "")).strip()]
        day_state["morning_answered"] = True
        day_state["no_tasks"] = False
        day_state["task_uids"] = [*existing_uids, *new_uids]
        self._save_daily_task_day_state(state, day_key, day_state)
        self.storage.delete_task_draft(user_id=user_id)

        lines = [f"Em đã lưu {len(created)} task cho hôm nay:"]
        for idx, task in enumerate(created, start=1):
            lines.append(f"{idx}. {task.get('title')}")
        lines.append("17h em sẽ hỏi lại tiến độ các việc này.")
        return "\n".join(lines)

    def _handle_daily_task_evening_reply(
        self,
        *,
        raw: str,
        user_id: int,
        chat_id: int,
        draft: dict[str, Any],
    ) -> str:
        day_key = str(draft.get("date", "")).strip() or self.scheduler.now_local().date().isoformat()
        task_uids = [str(item).strip() for item in draft.get("task_uids", []) if str(item).strip()]
        tasks = self.tasks.list_tasks_by_uids(task_uids)
        if not tasks:
            self.storage.delete_task_draft(user_id=user_id)
            return "Em không còn thấy task nào của hôm nay để cập nhật."

        updates, errors = self._parse_daily_task_progress_lines(raw, tasks)
        updated_items: list[dict[str, Any]] = []
        for item in updates:
            task = item["task"]
            payload = self.tasks.update_task(
                task_uid=str(task.get("task_uid", "")),
                updated_by=int(user_id),
                chat_id=int(chat_id),
                status=str(item.get("status", "")),
                progress_percent=int(item.get("progress_percent", 0)),
                note=str(item.get("note", "")),
                blocked_reason=str(item.get("blocked_reason", "")),
                next_step=str(item.get("next_step", "")),
                action_name="daily_checkin_update",
            )
            updated_items.append(payload)

        lines: list[str] = []
        if updated_items:
            lines.append(f"Em đã cập nhật {len(updated_items)} task hôm nay:")
            for item in updated_items:
                lines.append(f"- {self._format_task_item(item)}")
        if errors:
            if lines:
                lines.append("")
            lines.append("Có dòng em chưa khớp được, anh gửi lại riêng các dòng này nhé:")
            lines.extend(f"- {item}" for item in errors)
            return "\n".join(lines)

        state = self.storage.load_daily_task_checkin_state()
        day_state = self._get_daily_task_day_state(state, day_key)
        day_state["evening_answered"] = True
        self._save_daily_task_day_state(state, day_key, day_state)
        self.storage.delete_task_draft(user_id=user_id)
        return "\n".join(lines) if lines else "Em chưa thấy dòng tiến độ nào để cập nhật."

    def _extract_task_title_from_natural(self, raw: str) -> str:
        text = " ".join(str(raw or "").split()).strip()
        if not text:
            return ""
        normalized = _normalize_question_text(text)
        if not (
            normalized.startswith("them cong viec")
            or normalized.startswith("tao cong viec")
            or normalized.startswith("them task")
            or normalized.startswith("tao task")
        ):
            return ""
        if ":" in text:
            title = text.split(":", 1)[1].strip()
            return title
        parts = text.split()
        if len(parts) >= 4:
            return " ".join(parts[3:]).strip()
        return ""

    def _parse_deadline_input(self, raw: str) -> tuple[bool, str, str]:
        text = " ".join(str(raw or "").split()).strip()
        normalized = _normalize_question_text(text)
        if not text:
            return False, "", "Deadline đang trống. Anh nhập YYYY-MM-DD hoặc DD/MM hoặc 'không'."
        if normalized in {"khong", "none", "bo qua", "skip", "-"}:
            return True, "", ""
        now_local = self.scheduler.now_local().date()
        if normalized in {"hom nay", "ngay hom nay"}:
            return True, now_local.isoformat(), ""
        if normalized in {"ngay mai", "hom sau"}:
            return True, (now_local + timedelta(days=1)).isoformat(), ""
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
            return True, parsed.isoformat(), ""
        except ValueError:
            pass
        match = re.match(r"^(?:ngay\s+)?(\d{1,2})[\s/-](\d{1,2})(?:[\s/-](\d{2,4}))?$", normalized)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year_raw = match.group(3)
            year = now_local.year
            if year_raw:
                year = int(year_raw)
                if len(year_raw) == 2:
                    year = 2000 + year
            try:
                parsed = date(year, month, day)
                return True, parsed.isoformat(), ""
            except ValueError:
                return False, "", "Deadline chưa đúng ngày/tháng. Anh nhập lại (ví dụ 2026-06-02 hoặc 02/06)."
        return False, "", "Deadline chưa đúng định dạng. Anh nhập YYYY-MM-DD hoặc DD/MM hoặc 'không'."

    def _parse_task_status_input(self, raw: str) -> str:
        normalized = _normalize_question_text(raw)
        if normalized in {"todo", "chua lam", "chua bat dau", "pending"}:
            return "todo"
        if normalized in {"doing", "dang lam", "in progress", "tien hanh"}:
            return "doing"
        if normalized in {"done", "hoan thanh", "xong", "completed"}:
            return "done"
        return ""

    def _parse_daily_task_title_lines(self, raw: str) -> list[str]:
        titles: list[str] = []
        seen: set[str] = set()
        for line in _split_nonempty_lines(raw):
            title = _strip_daily_task_line_prefix(line)
            if not title:
                continue
            normalized = _normalize_question_text(title)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            titles.append(title)
        return titles

    def _parse_daily_task_progress_lines(
        self,
        raw: str,
        tasks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        updates: list[dict[str, Any]] = []
        errors: list[str] = []
        matched_uids: set[str] = set()
        for line in _split_nonempty_lines(raw):
            task = self._match_daily_task_progress_line(line, tasks)
            if not task:
                errors.append(line)
                continue
            task_uid = str(task.get("task_uid", "")).strip()
            if not task_uid or task_uid in matched_uids:
                continue
            matched_uids.add(task_uid)
            updates.append(self._build_daily_task_progress_update(line=line, task=task))
        return updates, errors

    def _match_daily_task_progress_line(self, line: str, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
        match = re.match(r"^\s*(\d{1,2})[\).\-\s]+(.+)$", str(line or "").strip())
        if match:
            idx = int(match.group(1))
            if 1 <= idx <= len(tasks):
                return tasks[idx - 1]

        normalized_line = _normalize_question_text(line)
        if not normalized_line:
            return None
        best: tuple[int, dict[str, Any]] | None = None
        for task in tasks:
            title_norm = _normalize_question_text(str(task.get("title", "")))
            if not title_norm:
                continue
            score = 0
            if title_norm in normalized_line:
                score = len(title_norm.split()) + 10
            else:
                tokens = [item for item in title_norm.split() if len(item) >= 3]
                score = sum(1 for token in tokens if token in normalized_line)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, task)
        return best[1] if best else None

    def _build_daily_task_progress_update(self, *, line: str, task: dict[str, Any]) -> dict[str, Any]:
        raw_note = _strip_daily_task_line_prefix(line)
        normalized = _normalize_question_text(raw_note)
        percent_match = re.search(r"(\d{1,3})\s*%", raw_note)
        percent = _to_int(task.get("progress_percent"), fallback=0)
        if percent_match:
            percent = max(0, min(100, int(percent_match.group(1))))

        if any(token in normalized for token in ("xong", "hoan thanh", "done", "completed")):
            status = "done"
            percent = 100
            blocked_reason = ""
            next_step = ""
        elif any(token in normalized for token in ("blocked", "block", "vuong", "ket", "bi chan")):
            status = "blocked"
            if percent <= 0:
                percent = 1
            blocked_reason = raw_note or "Cần cập nhật"
            next_step = "Cần cập nhật bước tiếp theo"
        elif "chua lam" in normalized or "chua bat dau" in normalized:
            status = "todo"
            percent = 0
            blocked_reason = "Chưa bắt đầu"
            next_step = "Bắt đầu xử lý"
        else:
            status = "doing"
            if percent <= 0:
                percent = max(1, _to_int(task.get("progress_percent"), fallback=50) or 50)
            blocked_reason = "Đang triển khai"
            next_step = "Tiếp tục xử lý"

        return {
            "task": task,
            "status": status,
            "progress_percent": percent,
            "note": raw_note,
            "blocked_reason": blocked_reason,
            "next_step": next_step,
        }

    def _get_daily_task_day_state(self, state: dict[str, Any], day_key: str) -> dict[str, Any]:
        days = state.get("days", {})
        if not isinstance(days, dict):
            days = {}
        raw = days.get(day_key, {})
        day_state = raw if isinstance(raw, dict) else {}
        day_state.setdefault("date", day_key)
        day_state.setdefault("task_uids", [])
        day_state.setdefault("morning_sent", False)
        day_state.setdefault("morning_answered", False)
        day_state.setdefault("evening_sent", False)
        day_state.setdefault("evening_answered", False)
        day_state.setdefault("no_tasks", False)
        return day_state

    def _save_daily_task_day_state(self, state: dict[str, Any], day_key: str, day_state: dict[str, Any]) -> None:
        days = state.get("days", {})
        if not isinstance(days, dict):
            days = {}
        days[day_key] = day_state
        if len(days) > 60:
            keys = sorted(str(key) for key in days.keys())
            days = {key: days[key] for key in keys[-60:] if key in days}
        state["days"] = days
        self.storage.save_daily_task_checkin_state(state)

    async def _handle_task_intent(self, message: Message, command: ParsedAssistantCommand, *, raw_text: str) -> None:
        if not self.settings.tasks_enabled:
            await self._bot_send_message(message.chat.id, "Task tracker đang tắt (`BOT3_TASKS_ENABLED=0`).")
            return

        user_id = message.from_user.id if message.from_user else 0
        chat_id = message.chat.id if message.chat else 0
        action = str(command.task_action or "").strip() or "list"
        args = command.task_args if isinstance(command.task_args, dict) else {}

        is_private = str(message.chat.type).lower() == "private" if message.chat else False
        if is_private and not self._is_authorized(user_id):
            return

        if not is_private:
            if not self._is_task_group_chat(chat_id):
                return
            if not self._is_task_group_viewer(user_id):
                return
            if action not in {"report", "week", "pending_report", "done_report", "list"}:
                await self._bot_send_message(chat_id, "Anh cập nhật task trong chat riêng với bot giúp em.")
                return

        try:
            if action == "add":
                task = await asyncio.to_thread(
                    self.tasks.create_task,
                    title=str(args.get("title", "")),
                    created_by=int(user_id),
                    source_type=str(args.get("source_type", "manager")),
                    assigned_by=int(self.settings.task_manager_user_ids[0]) if self.settings.task_manager_user_ids else 0,
                    group_chat_id=int(self.settings.task_group_chat_id),
                    note=str(args.get("note", "")),
                    deadline_date=str(args.get("deadline_date", "")),
                )
                reply = (
                    "Đã lưu task mới:\n"
                    f"- ID: {task.get('task_uid')}\n"
                    f"- Tiêu đề: {task.get('title')}\n"
                    f"- Nguồn: {task.get('source_type')}\n"
                    f"- Trạng thái: {self._status_label_vi(str(task.get('status', '')))}"
                )
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            if action == "update":
                await self._handle_task_update_by_title(chat_id=chat_id, user_id=user_id, args=args, raw_text=raw_text)
                return

            if action == "done":
                await self._handle_task_done_by_title(chat_id=chat_id, user_id=user_id, args=args, raw_text=raw_text)
                return

            if action == "pick":
                await self._handle_task_pick(chat_id=chat_id, user_id=user_id, args=args, raw_text=raw_text)
                return

            if action == "list":
                status = str(args.get("status", "")).strip()
                tasks = await asyncio.to_thread(self.tasks.list_tasks, status=status, limit=20)
                if not tasks:
                    reply = "Chưa có task nào khớp điều kiện."
                else:
                    title = "Danh sách task:"
                    if status:
                        title = f"Danh sách task ({status}):"
                    lines = [title]
                    for item in tasks[:20]:
                        lines.append(f"- {self._format_task_item(item)}")
                    reply = "\n".join(lines)
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            if action == "report":
                snapshot = await asyncio.to_thread(
                    self.tasks.build_overview_snapshot,
                    max_items=int(self.settings.task_weekly_summary_max_items),
                )
                reply = self._build_task_overview_reply(snapshot=snapshot, trigger_label="Task report")
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            if action == "week":
                snapshot = await asyncio.to_thread(
                    self.tasks.build_weekly_snapshot,
                    reference_date=None,
                    timezone_name=self.settings.timezone_name,
                    max_items=int(self.settings.task_weekly_summary_max_items),
                )
                reply = self._build_task_weekly_reply(snapshot=snapshot, trigger_label="Task week")
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            if action == "pending_report":
                tasks = await asyncio.to_thread(self.tasks.list_tasks, status="pending", limit=20)
                if not tasks:
                    reply = "Hiện không còn task chưa hoàn thành."
                else:
                    lines = ["Task chưa hoàn thành:"]
                    for item in tasks[:20]:
                        lines.append(f"- {self._format_task_item(item)}")
                    reply = "\n".join(lines)
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            if action == "done_report":
                tasks = await asyncio.to_thread(self.tasks.list_tasks, status="done", limit=20)
                if not tasks:
                    reply = "Chưa có task hoàn thành."
                else:
                    lines = ["Task đã hoàn thành:"]
                    for item in tasks[:20]:
                        lines.append(f"- {self._format_task_item(item)}")
                    reply = "\n".join(lines)
                await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)
                return

            await self._bot_send_message(chat_id, "Task command chưa hỗ trợ.")
        except Exception as exc:  # noqa: BLE001
            await self._bot_send_message(chat_id, f"Xử lý task thất bại: {exc}")

    async def _handle_task_update_by_title(
        self,
        *,
        chat_id: int,
        user_id: int,
        args: dict[str, Any],
        raw_text: str,
    ) -> None:
        title = str(args.get("title", "")).strip()
        if not title:
            await self._bot_send_message(chat_id, "Thiếu tên việc cần cập nhật.")
            return
        candidates = await asyncio.to_thread(self.tasks.find_tasks_by_title, title, include_done=False, limit=10)
        if not candidates:
            await self._bot_send_message(chat_id, f"Không tìm thấy task khớp: {title}")
            return
        if len(candidates) > 1:
            request_id = self.storage.create_pending_request(
                {
                    "request_type": "assistant_task_select",
                    "mode": "update",
                    "user_id": int(user_id),
                    "chat_id": int(chat_id),
                    "action_args": args,
                    "candidate_task_ids": [str(item.get("task_uid", "")) for item in candidates],
                },
                request_type="assistant_task_select",
            )
            lines = [
                "Có nhiều task trùng tên. Chọn bằng lệnh:",
                f"/task pick {request_id} <index>",
                "",
            ]
            for idx, item in enumerate(candidates, start=1):
                lines.append(f"{idx}. {self._format_task_item(item)}")
            await self._bot_send_message(chat_id, "\n".join(lines))
            return

        task_uid = str(candidates[0].get("task_uid", "")).strip()
        payload = await asyncio.to_thread(
            self.tasks.update_task,
            task_uid=task_uid,
            updated_by=int(user_id),
            chat_id=int(chat_id),
            status=str(args.get("status", "")),
            progress_percent=int(args.get("progress_percent", 0)),
            note=str(args.get("note", "")),
            blocked_reason=str(args.get("blocked_reason", "")),
            next_step=str(args.get("next_step", "")),
            deadline_date=str(args.get("deadline_date", "")) if str(args.get("deadline_date", "")).strip() else None,
            action_name="update",
        )
        reply = f"Đã cập nhật task:\n- {self._format_task_item(payload)}"
        await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)

    async def _handle_task_done_by_title(
        self,
        *,
        chat_id: int,
        user_id: int,
        args: dict[str, Any],
        raw_text: str,
    ) -> None:
        title = str(args.get("title", "")).strip()
        if not title:
            await self._bot_send_message(chat_id, "Thiếu tên việc cần chốt done.")
            return
        candidates = await asyncio.to_thread(self.tasks.find_tasks_by_title, title, include_done=False, limit=10)
        if not candidates:
            await self._bot_send_message(chat_id, f"Không tìm thấy task khớp: {title}")
            return
        if len(candidates) > 1:
            request_id = self.storage.create_pending_request(
                {
                    "request_type": "assistant_task_select",
                    "mode": "done",
                    "user_id": int(user_id),
                    "chat_id": int(chat_id),
                    "action_args": args,
                    "candidate_task_ids": [str(item.get("task_uid", "")) for item in candidates],
                },
                request_type="assistant_task_select",
            )
            lines = [
                "Có nhiều task trùng tên. Chọn bằng lệnh:",
                f"/task pick {request_id} <index>",
                "",
            ]
            for idx, item in enumerate(candidates, start=1):
                lines.append(f"{idx}. {self._format_task_item(item)}")
            await self._bot_send_message(chat_id, "\n".join(lines))
            return

        task_uid = str(candidates[0].get("task_uid", "")).strip()
        payload = await asyncio.to_thread(
            self.tasks.mark_done,
            task_uid=task_uid,
            updated_by=int(user_id),
            chat_id=int(chat_id),
            note=str(args.get("note", "")),
        )
        reply = f"Đã chốt hoàn thành:\n- {self._format_task_item(payload)}"
        await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)

    async def _handle_task_pick(
        self,
        *,
        chat_id: int,
        user_id: int,
        args: dict[str, Any],
        raw_text: str,
    ) -> None:
        request_id = str(args.get("request_id", "")).strip()
        candidate_index = _to_int(args.get("candidate_index"), fallback=0)
        if not request_id or candidate_index <= 0:
            await self._bot_send_message(chat_id, "Lệnh pick chưa đúng. Ví dụ: /task pick <request_id> <index>")
            return
        request = self.storage.get_pending_request(request_id)
        if not request:
            await self._bot_send_message(chat_id, "Yêu cầu chọn task đã hết hạn hoặc không tồn tại.")
            return
        if str(request.get("request_type", "")).strip() != "assistant_task_select":
            await self._bot_send_message(chat_id, "Yêu cầu chọn task không hợp lệ.")
            return
        owner_id = _to_int(request.get("user_id"), fallback=0)
        if owner_id != int(user_id):
            await self._bot_send_message(chat_id, "Chỉ người tạo yêu cầu mới được chọn task.")
            return

        candidate_ids = request.get("candidate_task_ids", [])
        if not isinstance(candidate_ids, list) or candidate_index > len(candidate_ids):
            await self._bot_send_message(chat_id, "Index task không hợp lệ.")
            return
        task_uid = str(candidate_ids[candidate_index - 1]).strip()
        mode = str(request.get("mode", "")).strip()
        action_args = request.get("action_args", {}) if isinstance(request.get("action_args"), dict) else {}
        if mode == "update":
            payload = await asyncio.to_thread(
                self.tasks.update_task,
                task_uid=task_uid,
                updated_by=int(user_id),
                chat_id=int(chat_id),
                status=str(action_args.get("status", "")),
                progress_percent=int(action_args.get("progress_percent", 0)),
                note=str(action_args.get("note", "")),
                blocked_reason=str(action_args.get("blocked_reason", "")),
                next_step=str(action_args.get("next_step", "")),
                deadline_date=str(action_args.get("deadline_date", "")) if str(action_args.get("deadline_date", "")).strip() else None,
                action_name="update",
            )
            reply = f"Đã cập nhật task:\n- {self._format_task_item(payload)}"
        elif mode == "done":
            payload = await asyncio.to_thread(
                self.tasks.mark_done,
                task_uid=task_uid,
                updated_by=int(user_id),
                chat_id=int(chat_id),
                note=str(action_args.get("note", "")),
            )
            reply = f"Đã chốt hoàn thành:\n- {self._format_task_item(payload)}"
        else:
            await self._bot_send_message(chat_id, "Mode chọn task không hợp lệ.")
            return
        self.storage.mark_request_processed(request_id)
        self.storage.delete_pending_request(request_id)
        await self._send_and_log(chat_id, raw_text=raw_text, intent="task", reply=reply)

    async def _request_action_confirmation(self, chat_id: int, command: ParsedAssistantCommand, *, raw_text: str) -> None:
        action_name = command.action_name
        payload = {
            "action_name": action_name,
            "action_args": command.action_args,
            "raw_text": raw_text,
            "chat_id": chat_id,
            "risk_level": "high",
        }
        request_id = self.storage.create_pending_request(payload, request_type="assistant_action")
        await self._bot_send_message(
            chat_id,
            "Xác nhận chạy tác vụ nhạy cảm:\n"
            f"- Action: {action_name}\n"
            f"- Args: {command.action_args}\n"
            "Anh bấm Xác nhận chạy để thực thi, hoặc Hủy để bỏ qua.",
            reply_markup=self.approval.action_confirm_keyboard(request_id=request_id),
        )

    async def _on_confirm_action(self, query: CallbackQuery, request_id: str) -> None:
        if self.storage.is_request_processed(request_id):
            await query.answer("Yêu cầu đã xử lý trước đó.", show_alert=True)
            return
        request = self.storage.get_pending_request(request_id)
        if not request:
            await query.answer("Yêu cầu đã hết hạn hoặc không tồn tại.", show_alert=True)
            return
        if str(request.get("request_type", "")).strip() != "assistant_action":
            await query.answer("Yêu cầu không hợp lệ.", show_alert=True)
            return

        await query.answer("Đang chạy tác vụ...")
        action_name = str(request.get("action_name", "")).strip()
        action_args = request.get("action_args", {}) if isinstance(request.get("action_args"), dict) else {}
        result = await asyncio.to_thread(self._execute_action, action_name, action_args)

        self.storage.mark_request_processed(request_id)
        self.storage.delete_pending_request(request_id)
        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer(result["text"])

    async def _on_cancel_action(self, query: CallbackQuery, request_id: str) -> None:
        self.storage.mark_request_processed(request_id)
        self.storage.delete_pending_request(request_id)
        await query.answer("Đã hủy tác vụ.")
        if query.message:
            with suppress(Exception):
                await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer("Đã hủy, bot không thực thi tác vụ.")

    def _execute_action(self, action_name: str, action_args: dict[str, Any]) -> dict[str, Any]:
        try:
            if action_name == "daily_report":
                target = _parse_iso_date(action_args.get("report_date"))
                payload = self.internal_ops.generate_daily_report(target)
                ok = bool(payload.get("ok") or payload.get("partial"))
                return {
                    "ok": ok,
                    "text": self._format_action_result("daily_report", payload),
                }
            if action_name == "reconcile_cod_report":
                target = _parse_iso_date(action_args.get("settlement_date"))
                payload = self.internal_ops.generate_reconcile_cod_report(target)
                ok = bool(payload.get("ok") or payload.get("partial"))
                return {
                    "ok": ok,
                    "text": self._format_action_result("reconcile_cod_report", payload),
                }
            if action_name == "reconcile_sheet_sync":
                run_id = str(action_args.get("run_id", "")).strip()
                payload = self.internal_ops.sync_reconcile_sheet(run_id)
                ok = bool(payload.get("ok"))
                return {"ok": ok, "text": self._format_action_result("reconcile_sheet_sync", payload)}
            if action_name == "media_sheet_sync":
                run_id = str(action_args.get("run_id", "")).strip()
                payload = self.internal_ops.sync_media_sheet(run_id)
                ok = bool(payload.get("ok"))
                return {"ok": ok, "text": self._format_action_result("media_sheet_sync", payload)}
            return {"ok": False, "text": f"Action chưa hỗ trợ: {action_name}"}
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Thuc thi assistant action that bai: %s", action_name)
            return {"ok": False, "text": f"Thực thi action thất bại: {exc}"}

    async def _build_agenda_reply(self, target_date: date | None) -> str:
        try:
            agenda = await asyncio.to_thread(self.google.fetch_agenda, target_date)
        except Exception as exc:  # noqa: BLE001
            return self._friendly_google_failure("Calendar", exc)

        day_label = str(agenda.get("date", "")).strip()
        events = agenda.get("events", []) if isinstance(agenda.get("events"), list) else []
        lines = [f"Lịch ngày {day_label} ({self.settings.timezone_name}):", f"- Tổng sự kiện: {len(events):,}"]
        if not events:
            lines.append("- Hôm nay chưa có sự kiện nào.")
        else:
            for item in events[:12]:
                if not isinstance(item, dict):
                    continue
                summary = str(item.get("summary", "")).strip() or "(Không tiêu đề)"
                start_iso = str(item.get("start_iso", "")).strip()
                start_label = _format_time_label(start_iso)
                lines.append(f"- {start_label} | {summary}")

        try:
            mail = await asyncio.to_thread(self.google.fetch_gmail_summary, query=None, max_items=4)
            estimate = _to_int(mail.get("estimate_total"), fallback=0)
            lines.append("")
            lines.append(f"Email ưu tiên chưa đọc: {estimate:,}")
            for item in (mail.get("messages", []) if isinstance(mail.get("messages"), list) else [])[:3]:
                if not isinstance(item, dict):
                    continue
                lines.append(f"- {str(item.get('subject', '(Không tiêu đề)')).strip()}")
        except Exception as exc:  # noqa: BLE001
            lines.append("")
            lines.append(self._friendly_google_failure("Gmail", exc))
        return "\n".join(lines)

    async def _build_plan_reply(self, target_date: date | None, *, week_mode: bool) -> str:
        try:
            if week_mode:
                payload = await asyncio.to_thread(self.google.fetch_week_plan, target_date)
                events = payload.get("events", []) if isinstance(payload.get("events"), list) else []
                lines = [
                    f"Kế hoạch tuần {payload.get('week_start')} -> {payload.get('week_end')}:",
                    f"- Tổng sự kiện: {len(events):,}",
                ]
            else:
                payload = await asyncio.to_thread(self.google.fetch_agenda, target_date)
                events = payload.get("events", []) if isinstance(payload.get("events"), list) else []
                lines = [
                    f"Kế hoạch ngày {payload.get('date')}:",
                    f"- Tổng sự kiện: {len(events):,}",
                ]
        except Exception as exc:  # noqa: BLE001
            return self._friendly_google_failure("Calendar", exc)

        for item in events[:20]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "")).strip() or "(Không tiêu đề)"
            start_iso = str(item.get("start_iso", "")).strip()
            lines.append(f"- {_format_time_label(start_iso)} | {summary}")
        if not events:
            lines.append("- Chưa có sự kiện.")
        return "\n".join(lines)

    async def _build_result_reply(self, target_date: date | None) -> str:
        try:
            snapshot = await asyncio.to_thread(self.internal_ops.collect_result_snapshot, target_date)
        except Exception as exc:  # noqa: BLE001
            return f"Lấy kết quả nội bộ thất bại: {exc}"

        report_date = str(snapshot.get("report_date", "")).strip()
        lines = [f"Kết quả ngày {report_date}:", ""]

        daily = snapshot.get("daily_report", {}) if isinstance(snapshot.get("daily_report"), dict) else {}
        if daily:
            pos = daily.get("pos", {}) if isinstance(daily.get("pos"), dict) else {}
            ads = daily.get("ads", {}) if isinstance(daily.get("ads"), dict) else {}
            lines.append(
                "Daily report: "
                f"POS ~{_to_int(pos.get('revenue_total_vnd'), fallback=0):,} VND | "
                f"Ads ~{_to_int(ads.get('spend_vnd'), fallback=0):,} VND | "
                f"ROAS {daily.get('roas', 0)}"
            )
        else:
            lines.append("Daily report: chưa có file cho ngày này.")

        reconcile = snapshot.get("reconcile", {}) if isinstance(snapshot.get("reconcile"), dict) else {}
        if reconcile:
            summary = reconcile.get("summary", {}) if isinstance(reconcile.get("summary"), dict) else {}
            lines.append(
                "Đối soát COD: "
                f"khớp { _to_int(summary.get('matched_unique'), fallback=0):, } | "
                f"không tìm thấy { _to_int(summary.get('not_found'), fallback=0):, } | "
                f"cần cập nhật { _to_int(summary.get('update_candidates'), fallback=0):, }"
            )
        else:
            lines.append("Đối soát COD: chưa có report.")

        media = snapshot.get("media", {}) if isinstance(snapshot.get("media"), dict) else {}
        media_count = _to_int(media.get("count"), fallback=0)
        lines.append(f"Media runs: {media_count:,}")
        latest = media.get("latest", []) if isinstance(media.get("latest"), list) else []
        for row in latest[:5]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- {row.get('run_id', '')} | {row.get('status', '')} | "
                f"{_to_int(row.get('selected_count'), fallback=0)} media"
            )

        try:
            sheet = await asyncio.to_thread(self.google.fetch_sheet_snapshot, max_rows=10, max_cols="F")
            if sheet.get("ok"):
                lines.append("")
                lines.append(
                    f"Google Sheet snapshot ({sheet.get('sheet_title', '')}): "
                    f"{_to_int(sheet.get('row_count'), fallback=0)} rows đọc thử."
                )
            else:
                lines.append("")
                lines.append(f"Google Sheet snapshot lỗi: {sheet.get('error', 'Không rõ lỗi')}")
        except Exception as exc:  # noqa: BLE001
            lines.append("")
            lines.append(f"Google Sheet snapshot lỗi: {exc}")
        return "\n".join(lines)

    async def _build_general_qa_reply(self, question: str) -> str:
        normalized_question = " ".join(str(question or "").split())
        normalized_lookup = _normalize_question_text(normalized_question)
        activity_date = self._resolve_activity_question_date(question)
        if activity_date is not None:
            activity_reply = await asyncio.to_thread(self._build_activity_digest_reply, activity_date)
            if activity_reply:
                return activity_reply

        memory_hits = await asyncio.to_thread(self.memory.search, normalized_question, limit=6)
        filtered_hits = [hit for hit in memory_hits if str(hit.source).strip().lower() != "runtime_log"]
        if not filtered_hits:
            filtered_hits = memory_hits
        local_reply = self._build_local_reasoned_reply(normalized_question, filtered_hits)

        if not self.settings.openai_enabled:
            if _looks_like_external_lookup_question(normalized_lookup):
                web_reply = await asyncio.to_thread(_build_web_search_reply, normalized_question)
                if web_reply:
                    return web_reply
            if local_reply:
                return local_reply
            web_reply = await asyncio.to_thread(_build_web_search_reply, normalized_question)
            if web_reply:
                return web_reply
            return (
                "Em chưa đủ dữ liệu nội bộ để trả lời chắc chắn câu này. "
                "Anh cho em thêm ngữ cảnh (mốc thời gian/tên hệ thống/tên dự án), em trả lời sâu hơn ngay."
            )

        contexts: list[str] = []
        sources: list[str] = []
        for hit in filtered_hits[:4]:
            compact = _compact_excerpt(hit.excerpt, max_len=220)
            contexts.append(f"[{hit.source}] {hit.path} | {compact}")
            sources.append(hit.path)

        if not contexts:
            # Build minimal context from current status so OpenAI still has grounding.
            contexts.append(self._status_text())

        result = await asyncio.to_thread(self.openai.ask, question=normalized_question, context_blocks=contexts)
        if result.get("ok"):
            answer = str(result.get("answer", "")).strip()
            if sources:
                source_lines = ["", "Nguồn nội bộ tham chiếu:"]
                for path in sources[:4]:
                    source_lines.append(f"- {_short_source_path(path)}")
                answer = answer + "\n" + "\n".join(source_lines)
            return answer

        if local_reply:
            return local_reply
        web_reply = await asyncio.to_thread(_build_web_search_reply, normalized_question)
        if web_reply:
            return web_reply
        user_message = str(result.get("user_message", "")).strip() or "Em đang không gọi được nguồn trả lời ngoài luồng."
        return user_message

    async def _send_and_log(self, chat_id: int, *, raw_text: str, intent: str, reply: str) -> None:
        await self._bot_send_message(chat_id, reply)
        self.storage.append_conversation_log(
            user_text=raw_text,
            bot_text=reply,
            intent=intent,
            sources=[],
        )

    async def _agenda_scheduler_loop(self) -> None:
        self.logger.info("Bat assistant agenda scheduler: %02d:00 (%s)", self.settings.agenda_hour, self.settings.timezone_name)
        while True:
            try:
                now_local = self.scheduler.now_local()
                day_key = now_local.date().isoformat()
                if (
                    now_local.hour == self.settings.agenda_hour
                    and now_local.minute == 0
                    and self.scheduler.should_send_day_mark("agenda", day_key)
                ):
                    reply = await self._build_agenda_reply(now_local.date())
                    await self._bot_send_message(
                        self.settings.telegram_allowed_user_id,
                        f"[Nhắc lịch 08:00]\n{reply}",
                    )
                    self.scheduler.mark_day_sent("agenda", day_key)
                    await asyncio.sleep(65)
                    continue
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Agenda scheduler loop loi: %s", exc)
            await asyncio.sleep(20)

    async def _event_scheduler_loop(self) -> None:
        self.logger.info("Bat assistant event reminder loop: -%s phut", self.settings.event_reminder_lead_minutes)
        while True:
            try:
                now_local = self.scheduler.now_local()
                end_local = now_local + timedelta(minutes=self.settings.event_reminder_lead_minutes + 5)
                events = await asyncio.to_thread(self.google.fetch_events_between, now_local, end_local, max_per_calendar=20)
                due = self.scheduler.pick_due_event_reminders(events, now_local=now_local)
                for event in due:
                    summary = str(event.get("summary", "")).strip() or "(Không tiêu đề)"
                    start_label = _format_time_label(str(event.get("start_iso", "")))
                    await self._bot_send_message(
                        self.settings.telegram_allowed_user_id,
                        "[Nhắc trước sự kiện]\n"
                        f"- {summary}\n"
                        f"- Bắt đầu lúc: {start_label}\n"
                        f"- Còn khoảng: {self.settings.event_reminder_lead_minutes} phút",
                    )
                    self.scheduler.mark_event_reminded(event)
            except Exception as exc:  # noqa: BLE001
                if _is_google_scope_error(exc):
                    self.logger.warning(
                        "Event reminder tam dung do thieu scope Google Calendar/Gmail. "
                        "Can cap lai refresh token voi scope day du."
                    )
                else:
                    self.logger.exception("Event reminder loop loi: %s", exc)
            await asyncio.sleep(60)

    async def _eod_scheduler_loop(self) -> None:
        self.logger.info("Bat assistant EOD scheduler: %02d:00 (%s)", self.settings.eod_hour, self.settings.timezone_name)
        while True:
            try:
                now_local = self.scheduler.now_local()
                day_key = now_local.date().isoformat()
                if (
                    now_local.hour == self.settings.eod_hour
                    and now_local.minute == 0
                    and self.scheduler.should_send_day_mark("eod", day_key)
                ):
                    reply = await self._build_result_reply(now_local.date())
                    await self._bot_send_message(self.settings.telegram_allowed_user_id, f"[Tổng kết 21:00]\n{reply}")
                    self.scheduler.mark_day_sent("eod", day_key)
                    await asyncio.sleep(65)
                    continue
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("EOD scheduler loop loi: %s", exc)
            await asyncio.sleep(20)

    async def _task_weekly_summary_loop(self) -> None:
        if int(self.settings.task_group_chat_id) == 0:
            return
        self.logger.info(
            "Bat assistant task weekly scheduler: thu=%s %02d:%02d",
            self.settings.task_weekly_summary_weekday,
            self.settings.task_weekly_summary_hour,
            self.settings.task_weekly_summary_minute,
        )
        while True:
            try:
                now_local = self.scheduler.now_local()
                day_key = now_local.date().isoformat()
                if (
                    now_local.weekday() == int(self.settings.task_weekly_summary_weekday)
                    and now_local.hour == int(self.settings.task_weekly_summary_hour)
                    and now_local.minute == int(self.settings.task_weekly_summary_minute)
                    and self.scheduler.should_send_day_mark("task_weekly_summary", day_key)
                ):
                    snapshot = await asyncio.to_thread(
                        self.tasks.build_weekly_snapshot,
                        reference_date=now_local.date(),
                        timezone_name=self.settings.timezone_name,
                        max_items=int(self.settings.task_weekly_summary_max_items),
                    )
                    text = self._build_task_weekly_reply(snapshot=snapshot, trigger_label="Tổng kết tuần tự động")
                    await self._bot_send_message(int(self.settings.task_group_chat_id), text)
                    self.scheduler.mark_day_sent("task_weekly_summary", day_key)
                    await asyncio.sleep(65)
                    continue
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Task weekly summary loop loi: %s", exc)
            await asyncio.sleep(20)

    async def _daily_task_checkin_loop(self) -> None:
        self.logger.info(
            "Bat daily task check-in: morning=%02d:%02d evening=%02d:%02d weekdays=%s",
            int(self.settings.daily_task_morning_hour),
            int(self.settings.daily_task_morning_minute),
            int(self.settings.daily_task_evening_hour),
            int(self.settings.daily_task_evening_minute),
            ",".join(str(item) for item in self.settings.daily_task_weekdays),
        )
        while True:
            try:
                now_local = self.scheduler.now_local()
                if now_local.weekday() not in set(self.settings.daily_task_weekdays):
                    await asyncio.sleep(20)
                    continue

                day_key = now_local.date().isoformat()
                state = self.storage.load_daily_task_checkin_state()
                day_state = self._get_daily_task_day_state(state, day_key)
                if (
                    now_local.hour == int(self.settings.daily_task_morning_hour)
                    and now_local.minute == int(self.settings.daily_task_morning_minute)
                    and not bool(day_state.get("morning_sent"))
                ):
                    await self._send_daily_task_morning_prompt(day_key=day_key, state=state, day_state=day_state)
                    await asyncio.sleep(65)
                    continue

                task_uids = [str(item).strip() for item in day_state.get("task_uids", []) if str(item).strip()]
                if (
                    now_local.hour == int(self.settings.daily_task_evening_hour)
                    and now_local.minute == int(self.settings.daily_task_evening_minute)
                    and not bool(day_state.get("evening_sent"))
                    and bool(day_state.get("morning_answered"))
                    and not bool(day_state.get("no_tasks"))
                    and task_uids
                ):
                    await self._send_daily_task_evening_prompt(
                        day_key=day_key,
                        task_uids=task_uids,
                        state=state,
                        day_state=day_state,
                    )
                    await asyncio.sleep(65)
                    continue
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Daily task check-in loop loi: %s", exc)
            await asyncio.sleep(20)

    async def _send_daily_task_morning_prompt(
        self,
        *,
        day_key: str,
        state: dict[str, Any],
        day_state: dict[str, Any],
    ) -> None:
        user_id = int(self.settings.telegram_allowed_user_id)
        self.storage.save_task_draft(
            user_id=user_id,
            payload={
                "mode": "daily_task_morning",
                "date": day_key,
                "chat_id": user_id,
                "user_id": user_id,
            },
        )
        await self._bot_send_message(
            user_id,
            "Anh ơi, hôm nay anh có công việc gì?\n"
            "Anh gửi mỗi dòng một việc nhé. Nếu không có, gửi: `không có`.",
        )
        day_state["morning_sent"] = True
        self._save_daily_task_day_state(state, day_key, day_state)

    async def _send_daily_task_evening_prompt(
        self,
        *,
        day_key: str,
        task_uids: list[str],
        state: dict[str, Any],
        day_state: dict[str, Any],
    ) -> None:
        user_id = int(self.settings.telegram_allowed_user_id)
        tasks = await asyncio.to_thread(self.tasks.list_tasks_by_uids, task_uids)
        if not tasks:
            day_state["evening_sent"] = True
            self._save_daily_task_day_state(state, day_key, day_state)
            return
        self.storage.save_task_draft(
            user_id=user_id,
            payload={
                "mode": "daily_task_evening",
                "date": day_key,
                "chat_id": user_id,
                "user_id": user_id,
                "task_uids": [str(item.get("task_uid", "")).strip() for item in tasks],
            },
        )
        lines = ["17h rồi anh ơi, anh cập nhật tiến độ các việc hôm nay giúp em:"]
        for idx, task in enumerate(tasks, start=1):
            lines.append(f"{idx}. {task.get('title')}")
        lines.append("")
        lines.append("Ví dụ:")
        lines.append("1. xong")
        lines.append("2. đang làm 60% - còn phần test")
        lines.append("3. blocked - thiếu data, mai xin Huy")
        await self._bot_send_message(user_id, "\n".join(lines))
        day_state["evening_sent"] = True
        self._save_daily_task_day_state(state, day_key, day_state)

    async def _setup_bot_commands(self) -> None:
        if not self._bot:
            return
        private_commands = [
            BotCommand(command="start", description="Huong dan nhanh"),
            BotCommand(command="assistant_help", description="Xem danh sach lenh"),
            BotCommand(command="assistant_status", description="Kiem tra trang thai bot"),
            BotCommand(command="task", description="Quan ly task (add/update/done/list/report/week)"),
            BotCommand(command="ask", description="Hoi bat ky"),
            BotCommand(command="run", description="Chay tac vu noi bo co xac nhan"),
            BotCommand(command="agenda", description="Xem lich"),
            BotCommand(command="plan", description="Xem ke hoach"),
            BotCommand(command="result", description="Xem ket qua tong hop"),
        ]
        group_commands = [
            BotCommand(command="task", description="Bao cao task: /task report | /task week | /task pending"),
            BotCommand(command="assistant_help", description="Huong dan dung bot"),
        ]
        default_commands = [
            BotCommand(command="task", description="Quan ly va bao cao task"),
            BotCommand(command="assistant_help", description="Xem huong dan"),
            BotCommand(command="assistant_status", description="Kiem tra trang thai bot"),
        ]
        try:
            # Set default scope first for client compatibility (some Telegram clients
            # only refresh slash suggestions reliably when default commands exist).
            await self._bot.set_my_commands(default_commands)
            await self._bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
            await self._bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
            # Force menu button to Commands for private chats.
            await self._bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            if int(self.settings.task_group_chat_id) != 0:
                await self._bot.set_my_commands(
                    group_commands,
                    scope=BotCommandScopeChat(chat_id=int(self.settings.task_group_chat_id)),
                )
            self.logger.info("Da cap nhat command menu cho Bot 3.")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Khong cap nhat duoc command menu Bot 3: %s", exc)

    async def _bot_send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
        if not self._bot:
            raise RuntimeError("Telegram bot chua duoc khoi tao.")
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    def _status_text(self) -> str:
        google_ok, google_reason = self.google.is_configured()
        openai_ok, openai_reason = self.openai.is_configured()
        memory_status = self.memory.get_status()
        if not self.settings.openai_enabled:
            openai_status = "TẮT (BOT3_OPENAI_ENABLED=0)"
        else:
            openai_status = "OK" if openai_ok else f"LỖI ({openai_reason})"
        return (
            "Trạng thái Assistant Bot:\n"
            f"- Timezone: {self.settings.timezone_name}\n"
            f"- Nhắc chủ động: {'Bật' if self.settings.proactive_enabled else 'Tắt'}\n"
            f"- Agenda giờ: {self.settings.agenda_hour:02d}:00\n"
            f"- Nhắc trước sự kiện: {self.settings.event_reminder_lead_minutes} phút\n"
            f"- EOD giờ: {self.settings.eod_hour:02d}:00\n"
            f"- Task tracker: {'Bật' if self.settings.tasks_enabled else 'Tắt'}\n"
            f"- Task group chat id: {self.settings.task_group_chat_id}\n"
            f"- Daily task check-in: {'Bật' if self.settings.daily_task_checkin_enabled else 'Tắt'} "
            f"({self.settings.daily_task_morning_hour:02d}:{self.settings.daily_task_morning_minute:02d}/"
            f"{self.settings.daily_task_evening_hour:02d}:{self.settings.daily_task_evening_minute:02d})\n"
            f"- Google connector: {'OK' if google_ok else f'LỖI ({google_reason})'}\n"
            f"- OpenAI connector: {openai_status}\n"
            f"- Memory index: {memory_status.get('doc_count', 0)} docs"
        )

    def _friendly_google_failure(self, connector: str, exc: Exception) -> str:
        if _is_google_scope_error(exc):
            return (
                f"{connector} chưa đủ quyền OAuth scope. "
                "Anh cần cấp lại refresh token có đủ Calendar/Gmail scope cho Bot 3."
            )
        return f"{connector} lỗi: {str(exc)[:180]}"

    def _resolve_activity_question_date(self, question: str) -> date | None:
        normalized = _normalize_question_text(question)
        if not _looks_like_activity_question(normalized):
            return None
        now_local = self.scheduler.now_local().date()
        if "hom qua" in normalized or "ngay hom qua" in normalized:
            return now_local - timedelta(days=1)
        return now_local

    def _build_activity_digest_reply(self, target_date: date) -> str:
        file_path = self.settings.workspace_root / "memory" / f"{target_date.isoformat()}.md"
        lines = _extract_recent_activity_lines(file_path, max_items=10)

        header = f"Tóm tắt công việc ngày {target_date.strftime('%d/%m/%Y')}:"
        reply_lines = [header]
        if lines:
            for item in lines[:8]:
                reply_lines.append(f"- {item}")
        else:
            reply_lines.append("- Em chưa thấy log chi tiết cho ngày này trong memory.")

        try:
            snapshot = self.internal_ops.collect_result_snapshot(target_date)
            daily = snapshot.get("daily_report", {}) if isinstance(snapshot.get("daily_report"), dict) else {}
            if daily:
                pos = daily.get("pos", {}) if isinstance(daily.get("pos"), dict) else {}
                ads = daily.get("ads", {}) if isinstance(daily.get("ads"), dict) else {}
                reply_lines.append("")
                reply_lines.append(
                    "Kết quả vận hành nhanh:"
                    f" POS ~{_to_int(pos.get('revenue_total_vnd'), fallback=0):,} VND,"
                    f" Ads ~{_to_int(ads.get('spend_vnd'), fallback=0):,} VND,"
                    f" ROAS {daily.get('roas', 0)}"
                )
        except Exception:  # noqa: BLE001
            pass

        return "\n".join(reply_lines)

    def _build_local_reasoned_reply(self, question: str, hits: list[Any]) -> str:
        if not hits:
            return ""
        query_tokens = _extract_query_tokens(question)
        selected: list[str] = []
        for hit in hits:
            source = str(getattr(hit, "source", "")).strip()
            excerpt = str(getattr(hit, "excerpt", "")).strip()
            compact = _compact_excerpt(excerpt, max_len=170)
            if not compact:
                continue
            if _is_noise_excerpt(compact):
                continue
            if query_tokens and not _excerpt_matches_tokens(compact, query_tokens):
                continue
            selected.append(f"[{source}] {compact}")
            if len(selected) >= 3:
                break
        if not selected:
            for hit in hits[:2]:
                source = str(getattr(hit, "source", "")).strip()
                compact = _compact_excerpt(str(getattr(hit, "excerpt", "")).strip(), max_len=150)
                if compact and not _is_noise_excerpt(compact):
                    selected.append(f"[{source}] {compact}")
        if not selected:
            return ""
        lines = ["Theo dữ liệu nội bộ hiện có, em tổng hợp được:"]
        lines.extend(f"- {item}" for item in selected[:3])
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "Hướng dẫn Bot 3 - Trợ lý cá nhân:\n"
            "Anh cứ hỏi tự nhiên, ví dụ:\n"
            "- hôm nay anh và em đã làm những việc gì\n"
            "- lịch ngày mai thế nào\n"
            "- kết quả hôm qua ra sao\n"
            "- chạy đối soát cod hôm qua\n\n"
            "Nếu cần dùng lệnh tắt:\n"
            "- /assistant_help | /assistant_status\n"
            "- /agenda [hôm nay|ngày mai|YYYY-MM-DD]\n"
            "- /plan [tuần này|YYYY-MM-DD]\n"
            "- /result [hôm qua|YYYY-MM-DD]\n"
            "- /run report|reconcile cod|reconcile sheet|media sheet\n"
            "- /ask <câu hỏi bất kỳ>\n"
            "- /task add|update|done|list|report|week|pending\n"
            "- Hoặc nhập tự nhiên: thêm công việc: <tên task>\n"
            "- Check-in task ngày: bot hỏi 09:00 và 17:00 từ T2-T7 nếu đang bật"
        )

    def _format_action_result(self, action_name: str, payload: dict[str, Any]) -> str:
        ok = bool(payload.get("ok") or payload.get("partial"))
        lines = [f"Kết quả action `{action_name}`: {'OK' if ok else 'LỖI'}"]
        if action_name in {"daily_report", "reconcile_cod_report"}:
            lines.append(f"- Ngày: {payload.get('report_date') or payload.get('settlement_date') or 'N/A'}")
            if action_name == "daily_report":
                lines.append(f"- ROAS: {payload.get('roas', 0)}")
            if action_name == "reconcile_cod_report":
                summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
                lines.append(f"- Khớp duy nhất: {_to_int(summary.get('matched_unique'), fallback=0):,}")
                lines.append(f"- Không tìm thấy: {_to_int(summary.get('not_found'), fallback=0):,}")
        if action_name in {"reconcile_sheet_sync", "media_sheet_sync"}:
            lines.append(f"- Ghi mới: {_to_int(payload.get('inserted'), fallback=0):,}")
            lines.append(f"- Cập nhật: {_to_int(payload.get('updated'), fallback=0):,}")
            lines.append(f"- Bỏ qua: {_to_int(payload.get('skipped') or payload.get('skipped_existing'), fallback=0):,}")
        warnings = payload.get("warnings", []) if isinstance(payload.get("warnings"), list) else []
        errors = payload.get("errors", []) if isinstance(payload.get("errors"), list) else []
        if warnings:
            lines.append(f"- Cảnh báo: {warnings[0]}")
        if errors:
            lines.append(f"- Lỗi: {errors[0]}")
        if not ok and not errors:
            lines.append("- Lỗi: action trả trạng thái fail.")
        return "\n".join(lines)

    def _is_task_group_chat(self, chat_id: int | None) -> bool:
        group_id = int(self.settings.task_group_chat_id)
        if group_id == 0 or chat_id is None:
            return False
        return int(chat_id) == group_id

    @staticmethod
    def _is_private_chat(message: Message) -> bool:
        if not message.chat:
            return False
        return str(message.chat.type).lower() == "private"

    def _is_task_group_viewer(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        allowed = {int(self.settings.telegram_allowed_user_id)}
        allowed.update(int(item) for item in self.settings.task_manager_user_ids)
        return int(user_id) in allowed

    def _is_group_message_tagged_for_bot(self, text: str) -> bool:
        username = str(self._bot_username or "").strip().lower()
        if not username:
            return False
        return f"@{username}" in str(text or "").lower()

    def _strip_bot_mention_tokens(self, text: str) -> str:
        raw = str(text or "")
        username = str(self._bot_username or "").strip()
        if not username:
            return raw.strip()
        pattern = re.compile(rf"/([a-zA-Z0-9_]+)@{re.escape(username)}\b", flags=re.IGNORECASE)
        replaced = pattern.sub(r"/\1", raw)
        replaced = re.sub(rf"@{re.escape(username)}\b", "", replaced, flags=re.IGNORECASE)
        return " ".join(replaced.split())

    def _status_label_vi(self, status: str) -> str:
        mapping = {
            "todo": "Chưa làm",
            "doing": "Đang làm",
            "blocked": "Bị chặn",
            "done": "Hoàn thành",
        }
        key = str(status or "").strip().lower()
        return mapping.get(key, key or "N/A")

    def _format_task_item(self, task: dict[str, Any]) -> str:
        title = str(task.get("title", "")).strip() or "(Không tiêu đề)"
        status = self._status_label_vi(str(task.get("status", "")))
        percent = _to_int(task.get("progress_percent"), fallback=0)
        blocked = str(task.get("blocked_reason", "")).strip()
        next_step = str(task.get("next_step", "")).strip()
        suffix_parts: list[str] = []
        if blocked:
            suffix_parts.append(f"blocked={blocked}")
        if next_step:
            suffix_parts.append(f"next={next_step}")
        suffix = f" | {'; '.join(suffix_parts)}" if suffix_parts else ""
        return f"{title} [{status}] {percent}%{suffix}"

    def _build_task_overview_reply(self, *, snapshot: dict[str, Any], trigger_label: str) -> str:
        counts = snapshot.get("counts", {}) if isinstance(snapshot.get("counts"), dict) else {}
        pending = snapshot.get("pending_items", []) if isinstance(snapshot.get("pending_items"), list) else []
        done = snapshot.get("done_items", []) if isinstance(snapshot.get("done_items"), list) else []
        lines = [
            f"[{trigger_label}] Tổng quan tiến độ:",
            f"- Tổng task: {_to_int(snapshot.get('total'), fallback=0):,}",
            f"- Chưa làm: {_to_int(counts.get('todo'), fallback=0):,}",
            f"- Đang làm: {_to_int(counts.get('doing'), fallback=0):,}",
            f"- Bị chặn: {_to_int(counts.get('blocked'), fallback=0):,}",
            f"- Hoàn thành: {_to_int(counts.get('done'), fallback=0):,}",
        ]
        if pending:
            lines.append("")
            lines.append("Top task chưa xong:")
            for item in pending[: int(self.settings.task_weekly_summary_max_items)]:
                if isinstance(item, dict):
                    lines.append(f"- {self._format_task_item(item)}")
        if done:
            lines.append("")
            lines.append("Task hoàn thành gần nhất:")
            for item in done[: int(self.settings.task_weekly_summary_max_items)]:
                if isinstance(item, dict):
                    lines.append(f"- {self._format_task_item(item)}")
        return "\n".join(lines)

    def _build_task_weekly_reply(self, *, snapshot: dict[str, Any], trigger_label: str) -> str:
        pending = snapshot.get("pending_items", []) if isinstance(snapshot.get("pending_items"), list) else []
        done = snapshot.get("done_items", []) if isinstance(snapshot.get("done_items"), list) else []
        lines = [
            f"[{trigger_label}] Tuần {snapshot.get('week_start')} -> {snapshot.get('week_end')} (T2-T7):",
            f"- Hoàn thành trong tuần: {_to_int(snapshot.get('done_count'), fallback=0):,}",
            f"- Chưa hoàn thành: {_to_int(snapshot.get('pending_count'), fallback=0):,}",
            f"- Đang blocked: {_to_int(snapshot.get('blocked_count'), fallback=0):,}",
            f"- Thiếu blocked/next-step: {_to_int(snapshot.get('missing_detail_count'), fallback=0):,}",
        ]
        if done:
            lines.append("")
            lines.append("Top task đã hoàn thành:")
            for item in done[: int(self.settings.task_weekly_summary_max_items)]:
                if isinstance(item, dict):
                    lines.append(f"- {self._format_task_item(item)}")
        if pending:
            lines.append("")
            lines.append("Top task chưa hoàn thành:")
            for item in pending[: int(self.settings.task_weekly_summary_max_items)]:
                if not isinstance(item, dict):
                    continue
                blocked = str(item.get("blocked_reason", "")).strip() or "Chưa cập nhật"
                next_step = str(item.get("next_step", "")).strip() or "Chưa cập nhật"
                lines.append(f"- {self._format_task_item(item)}")
                lines.append(f"  blocked: {blocked} | next: {next_step}")
        return "\n".join(lines)

    def _is_authorized(self, user_id: int | None) -> bool:
        return user_id == self.settings.telegram_allowed_user_id


def _split_nonempty_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for line in str(raw or "").replace("\r", "\n").split("\n"):
        cleaned = " ".join(line.split()).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _strip_daily_task_line_prefix(line: str) -> str:
    text = " ".join(str(line or "").split()).strip()
    text = re.sub(r"^\s*[-*•]+\s*", "", text)
    text = re.sub(r"^\s*\d{1,2}[\).\-\s]+", "", text)
    return text.strip(" -:\t")


def _parse_iso_date(raw: Any) -> date | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_int(value: Any, *, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _format_time_label(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return "N/A"
    # All-day date.
    if len(value) <= 10 and value.count("-") == 2:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d/%m %H:%M")
    except ValueError:
        return value


def _compact_excerpt(raw: str, *, max_len: int) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _short_source_path(path: str) -> str:
    value = str(path or "").replace("\\", "/")
    parts = [part for part in value.split("/") if part]
    if len(parts) <= 3:
        return value
    return "/".join(parts[-3:])


def _is_google_scope_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "insufficient authentication scopes" in text
        or "insufficientpermissions" in text
        or "insufficient permission" in text
        or "permission_denied" in text
    )


def _normalize_question_text(text: str) -> str:
    folded = unicodedata.normalize("NFD", str(text or ""))
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accents.lower().replace("đ", "d")
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _looks_like_activity_question(normalized_text: str) -> bool:
    text = _normalize_question_text(normalized_text)
    if not text:
        return False
    time_keywords = {"hom nay", "ngay hom nay", "hom qua", "ngay hom qua", "nay"}
    activity_patterns = (
        r"\bda lam\b",
        r"\blam gi\b",
        r"\bnhung viec gi\b",
        r"\btong ket\b",
        r"\btom tat\b",
        r"\bcap nhat\b",
    )
    has_activity_pattern = any(re.search(pattern, text) for pattern in activity_patterns)
    has_time = any(keyword in text for keyword in time_keywords)
    has_pair_reference = any(
        token in text
        for token in (
            "anh va em",
            "minh da",
            "chung ta",
            "team minh",
            "ben minh",
        )
    )
    return has_activity_pattern and (has_time or has_pair_reference)


def _looks_like_external_lookup_question(normalized_text: str) -> bool:
    text = _normalize_question_text(normalized_text)
    if not text:
        return False
    internal_markers = (
        "bao cao",
        "doi soat",
        "reconcile",
        "run",
        "campaign",
        "adset",
        "media",
        "lich",
        "ke hoach",
        "ket qua",
        "telegram",
        "bot",
        "sheet",
        "calendar",
        "gmail",
        "memory",
        "index",
        "context",
        "project",
        "workspace",
    )
    if any(marker in text for marker in internal_markers):
        return False
    lookup_patterns = (
        r"\bla gi\b",
        r"\bla ai\b",
        r"\bo dau\b",
        r"\bbao nhieu\b",
        r"\bvi sao\b",
        r"\bnhu the nao\b",
        r"\bgiai thich\b",
        r"\bdinh nghia\b",
        r"\bhuong dan\b",
        r"\bcach .*?\b",
    )
    return any(re.search(pattern, text) for pattern in lookup_patterns)


def _extract_recent_activity_lines(file_path: Path, *, max_items: int) -> list[str]:
    if not file_path.exists():
        return []
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            continue
        if stripped.startswith("- "):
            candidate = stripped[2:].strip()
        elif re.match(r"^\d+\.\s+", stripped):
            candidate = re.sub(r"^\d+\.\s+", "", stripped).strip()
        else:
            continue
        candidate = _clean_activity_line(candidate)
        if not candidate:
            continue
        lines.append(candidate)
        if len(lines) >= max_items:
            break
    return lines


def _clean_activity_line(text: str) -> str:
    cleaned = re.sub(r"`+", "", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 8:
        return ""
    if _contains_sensitive_marker(cleaned):
        return ""
    if len(cleaned) > 220:
        cleaned = cleaned[:217].rstrip() + "..."
    return cleaned


def _contains_sensitive_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    blocked_tokens = (
        "api key",
        "api_key",
        "client secret",
        "client_secret",
        "refresh token",
        "refresh_token",
        "bot3_telegram_token",
        "authorization: bearer",
    )
    if any(token in lowered for token in blocked_tokens):
        return True
    if re.search(r"\bsk-[a-z0-9_-]{12,}\b", lowered):
        return True
    if re.search(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", lowered):
        return True
    return False


def _extract_query_tokens(question: str) -> list[str]:
    normalized = _normalize_question_text(question)
    if not normalized:
        return []
    stop_words = {
        "anh",
        "em",
        "la",
        "va",
        "voi",
        "cho",
        "de",
        "tu",
        "trong",
        "ngoai",
        "nay",
        "hom",
        "ngay",
        "qua",
        "da",
        "gi",
        "nao",
        "di",
        "nhe",
        "roi",
        "voi",
        "a",
        "ha",
        "vay",
        "the",
        "co",
        "khong",
        "duoc",
    }
    tokens: list[str] = []
    for token in normalized.split():
        if len(token) < 3:
            continue
        if token in stop_words:
            continue
        if token in tokens:
            continue
        tokens.append(token)
    return tokens


def _is_noise_excerpt(text: str) -> bool:
    normalized = _normalize_question_text(text)
    if not normalized:
        return True
    noisy_markers = (
        "traceback",
        "exception",
        "stack",
        "polling",
        "insufficientquota",
        "api_error",
        "logs",
        "debug",
        "runtime",
    )
    return any(marker in normalized for marker in noisy_markers)


def _excerpt_matches_tokens(excerpt: str, tokens: list[str]) -> bool:
    if not tokens:
        return True
    normalized = _normalize_question_text(excerpt)
    if not normalized:
        return False
    matched = sum(1 for token in tokens if token in normalized)
    if len(tokens) <= 2:
        return matched >= 1
    return matched >= 2


def _build_web_search_reply(question: str) -> str:
    query = " ".join(str(question or "").split())
    if not query:
        return ""
    duck_reply = _build_duckduckgo_reply(query)
    if duck_reply:
        return duck_reply
    wiki_reply = _build_wikipedia_reply(query)
    if wiki_reply:
        return wiki_reply
    return ""


def _build_duckduckgo_reply(query: str) -> str:
    headers = {
        "User-Agent": "FBPersonalAssistantBot/1.0 (+https://duckduckgo.com)",
        "Accept": "application/json",
    }
    params = {
        "q": str(query or "").strip(),
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "no_redirect": "1",
    }
    try:
        response = requests.get(
            "https://api.duckduckgo.com/",
            params=params,
            headers=headers,
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return ""
    except Exception:
        return ""

    snippets: list[str] = []
    for key in ("AbstractText", "Answer", "Definition"):
        value = html.unescape(str(payload.get(key, "")).strip())
        if value and value not in snippets:
            snippets.append(value)
    if not snippets:
        related = payload.get("RelatedTopics", [])
        if isinstance(related, list):
            for item in related:
                topic_text = _extract_related_topic_text(item)
                if not topic_text:
                    continue
                if topic_text in snippets:
                    continue
                snippets.append(topic_text)
                if len(snippets) >= 2:
                    break
    if not snippets:
        return ""

    lines = ["Em tìm nhanh ngoài luồng được như sau:"]
    for snippet in snippets[:2]:
        compact = _compact_excerpt(snippet, max_len=240)
        lines.append(f"- {compact}")
    source_url = str(payload.get("AbstractURL", "")).strip()
    if not source_url:
        source_url = _extract_related_topic_url(payload.get("RelatedTopics"))
    if source_url:
        lines.append(f"Nguồn web: {source_url}")
    return "\n".join(lines)


def _build_wikipedia_reply(query: str) -> str:
    for language in ("vi", "en"):
        result = _query_wikipedia_summary(query=query, language=language)
        if not result:
            continue
        title, summary, url = result
        lines = ["Em tìm nhanh ngoài luồng được như sau:"]
        lines.append(f"- {_compact_excerpt(summary, max_len=260)}")
        lines.append(f"- Chủ đề: {title}")
        lines.append(f"Nguồn web: {url}")
        return "\n".join(lines)
    return ""


def _query_wikipedia_summary(query: str, *, language: str) -> tuple[str, str, str] | None:
    endpoint = f"https://{language}.wikipedia.org/w/api.php"
    headers = {"User-Agent": "FBPersonalAssistantBot/1.0 (contact: local assistant)"}

    try:
        search_response = requests.get(
            endpoint,
            params={
                "action": "query",
                "list": "search",
                "srsearch": str(query or "").strip(),
                "format": "json",
                "utf8": "1",
                "srlimit": "1",
            },
            headers=headers,
            timeout=8,
        )
        search_response.raise_for_status()
        search_payload = search_response.json()
    except Exception:
        return None
    if not isinstance(search_payload, dict):
        return None

    query_block = search_payload.get("query", {})
    if not isinstance(query_block, dict):
        return None
    search_results = query_block.get("search", [])
    if not isinstance(search_results, list) or not search_results:
        return None
    first = search_results[0] if isinstance(search_results[0], dict) else {}
    pageid = str(first.get("pageid", "")).strip()
    title = str(first.get("title", "")).strip()
    snippet_html = str(first.get("snippet", "")).strip()
    snippet = _strip_html(snippet_html)
    if not pageid or not title:
        return None

    summary = snippet
    try:
        extract_response = requests.get(
            endpoint,
            params={
                "action": "query",
                "prop": "extracts",
                "pageids": pageid,
                "exintro": "1",
                "explaintext": "1",
                "format": "json",
                "utf8": "1",
            },
            headers=headers,
            timeout=8,
        )
        extract_response.raise_for_status()
        extract_payload = extract_response.json()
        if isinstance(extract_payload, dict):
            pages = extract_payload.get("query", {}).get("pages", {})
            if isinstance(pages, dict):
                page_block = pages.get(pageid, {})
                if isinstance(page_block, dict):
                    extracted = str(page_block.get("extract", "")).strip()
                    if extracted:
                        summary = extracted
    except Exception:
        pass

    summary = " ".join(str(summary or "").split())
    if not summary:
        return None
    source_url = f"https://{language}.wikipedia.org/?curid={pageid}"
    return title, summary, source_url


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_related_topic_text(item: Any) -> str:
    if isinstance(item, dict):
        direct = html.unescape(str(item.get("Text", "")).strip())
        if direct:
            return direct
        nested = item.get("Topics")
        if isinstance(nested, list):
            for child in nested:
                text = _extract_related_topic_text(child)
                if text:
                    return text
    return ""


def _extract_related_topic_url(related_topics: Any) -> str:
    if not isinstance(related_topics, list):
        return ""
    for item in related_topics:
        if isinstance(item, dict):
            first_url = str(item.get("FirstURL", "")).strip()
            if first_url:
                return first_url
            nested = item.get("Topics")
            nested_url = _extract_related_topic_url(nested)
            if nested_url:
                return nested_url
    return ""
