from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.assistant_settings import AssistantSettings
from app.assistant_storage_service import AssistantStorageService


class AssistantSchedulerService:
    def __init__(self, settings: AssistantSettings, storage: AssistantStorageService) -> None:
        self.settings = settings
        self.storage = storage

    def now_local(self) -> datetime:
        return datetime.now(self._resolve_timezone())

    def seconds_until_schedule(self, hour: int, minute: int = 0) -> int:
        now_local = self.now_local()
        next_run = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now_local:
            next_run = next_run + timedelta(days=1)
        delta = next_run - now_local
        return max(1, int(delta.total_seconds()))

    def should_send_day_mark(self, mark: str, day_key: str) -> bool:
        state = self.storage.load_reminder_state()
        marks = state.get("day_marks", {})
        if not isinstance(marks, dict):
            marks = {}
        return str(marks.get(mark, "")).strip() != day_key

    def mark_day_sent(self, mark: str, day_key: str) -> None:
        state = self.storage.load_reminder_state()
        marks = state.get("day_marks", {})
        if not isinstance(marks, dict):
            marks = {}
        marks[str(mark).strip()] = str(day_key).strip()
        state["day_marks"] = marks
        self.storage.save_reminder_state(state)

    def pick_due_event_reminders(
        self,
        events: list[dict[str, Any]],
        *,
        now_local: datetime | None = None,
    ) -> list[dict[str, Any]]:
        now_local = now_local or self.now_local()
        due: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_key = self._event_key(event)
            if not event_key or self.was_event_reminded(event_key):
                continue
            start_iso = str(event.get("start_iso", "")).strip()
            start_dt = self._parse_event_datetime(start_iso)
            if not start_dt:
                continue
            lead_time = start_dt - timedelta(minutes=self.settings.event_reminder_lead_minutes)
            late_grace = start_dt + timedelta(minutes=2)
            if lead_time <= now_local <= late_grace:
                due.append(event)
        return due

    def was_event_reminded(self, event_key: str) -> bool:
        state = self.storage.load_reminder_state()
        reminded = state.get("event_reminders", {})
        if not isinstance(reminded, dict):
            reminded = {}
        return event_key in reminded

    def mark_event_reminded(self, event: dict[str, Any]) -> None:
        event_key = self._event_key(event)
        if not event_key:
            return
        state = self.storage.load_reminder_state()
        reminded = state.get("event_reminders", {})
        if not isinstance(reminded, dict):
            reminded = {}
        reminded[event_key] = datetime.now(timezone.utc).isoformat()
        # Keep reminder state bounded.
        if len(reminded) > 2000:
            sorted_items = sorted(reminded.items(), key=lambda item: str(item[1]))
            reminded = dict(sorted_items[-1500:])
        state["event_reminders"] = reminded
        self.storage.save_reminder_state(state)

    def _event_key(self, event: dict[str, Any]) -> str:
        event_id = str(event.get("event_id", "")).strip()
        start_iso = str(event.get("start_iso", "")).strip()
        if not event_id or not start_iso:
            return ""
        return f"{event_id}|{start_iso}"

    def _parse_event_datetime(self, raw: str) -> datetime | None:
        value = str(raw or "").strip()
        if not value:
            return None
        # All-day events only have YYYY-MM-DD, skip reminder.
        if len(value) <= 10 and value.count("-") == 2:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self._resolve_timezone())
        return dt.astimezone(self._resolve_timezone())

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone_name)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))
