from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from app.utils import dump_json, load_json, now_utc_iso
from app.work_progress_service import WorkProgressService
from app.work_progress_settings import WorkProgressSettings


class WorkProgressScheduler:
    def __init__(
        self,
        *,
        settings: WorkProgressSettings,
        service: WorkProgressService,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.service = service
        self.logger = logger

    def run_forever(self) -> None:
        self.logger.info(
            "Bat work progress scheduler: daily=%02d:%02d, weekly=%s %02d:%02d, monthly day=%s %02d:%02d",
            self.settings.daily_report_hour,
            self.settings.daily_report_minute,
            self.settings.weekly_report_weekday,
            self.settings.weekly_report_hour,
            self.settings.weekly_report_minute,
            self.settings.monthly_report_day,
            self.settings.monthly_report_hour,
            self.settings.monthly_report_minute,
        )
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Scheduler work progress bi loi: %s", exc)
            time.sleep(20)

    def run_once(self) -> None:
        now_local = datetime.now(ZoneInfo(self.settings.timezone_name))
        day_key = now_local.date().isoformat()
        state = self._load_state()

        if (
            now_local.hour == int(self.settings.daily_report_hour)
            and now_local.minute == int(self.settings.daily_report_minute)
            and self._should_send(state, slot_name="daily", day_key=day_key)
        ):
            anchor_date = now_local.date() + timedelta(days=int(self.settings.daily_report_offset_days))
            report = self.service.build_report("daily", anchor_date=anchor_date)
            text = self.service.format_report_text(report)
            self._send_private_to_managers(text)
            self._mark_sent(state, slot_name="daily", day_key=day_key)
            self._save_state(state)
            return

        if (
            now_local.weekday() == int(self.settings.weekly_report_weekday)
            and now_local.hour == int(self.settings.weekly_report_hour)
            and now_local.minute == int(self.settings.weekly_report_minute)
            and self._should_send(state, slot_name="weekly", day_key=day_key)
        ):
            report = self.service.build_report("weekly", anchor_date=now_local.date())
            text = self.service.format_report_text(report)
            self._send_private_to_managers(text)
            self._mark_sent(state, slot_name="weekly", day_key=day_key)
            self._save_state(state)
            return

        target_day = min(
            int(self.settings.monthly_report_day),
            calendar.monthrange(now_local.year, now_local.month)[1],
        )
        if (
            now_local.day == target_day
            and now_local.hour == int(self.settings.monthly_report_hour)
            and now_local.minute == int(self.settings.monthly_report_minute)
            and self._should_send(state, slot_name="monthly", day_key=day_key)
        ):
            report = self.service.build_report("monthly", anchor_date=now_local.date())
            text = self.service.format_report_text(report)
            self._send_private_to_managers(text)
            self._mark_sent(state, slot_name="monthly", day_key=day_key)
            self._save_state(state)

    def _send_private_to_managers(self, text: str) -> None:
        bot_token = str(self.settings.telegram_bot_token or "").strip()
        if not bot_token:
            self.logger.warning("Khong gui duoc work progress report vi thieu WORK_PROGRESS_TELEGRAM_BOT_TOKEN.")
            return
        manager_ids = [int(item) for item in self.settings.manager_telegram_user_ids if int(item) != 0]
        if not manager_ids:
            self.logger.warning("Khong gui duoc work progress report vi thieu WORK_PROGRESS_MANAGER_TELEGRAM_IDS.")
            return

        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        for manager_id in manager_ids:
            try:
                response = requests.post(
                    endpoint,
                    json={"chat_id": manager_id, "text": text},
                    timeout=20,
                )
                if response.status_code >= 400:
                    self.logger.warning(
                        "Gui work progress report toi manager_id=%s that bai (%s): %s",
                        manager_id,
                        response.status_code,
                        response.text[:300],
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Gui work progress report toi manager_id=%s loi: %s", manager_id, exc)

    def _load_state(self) -> dict[str, Any]:
        path = self.settings.scheduler_state_file
        if not path.exists():
            return {}
        payload = load_json(path)
        return payload if isinstance(payload, dict) else {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        data = dict(payload)
        data["updated_at"] = now_utc_iso()
        dump_json(self.settings.scheduler_state_file, data)

    def _should_send(self, state: dict[str, Any], *, slot_name: str, day_key: str) -> bool:
        slots = state.get("slots", {})
        if not isinstance(slots, dict):
            return True
        return str(slots.get(slot_name, "")).strip() != str(day_key)

    def _mark_sent(self, state: dict[str, Any], *, slot_name: str, day_key: str) -> None:
        slots = state.get("slots", {})
        if not isinstance(slots, dict):
            slots = {}
        slots[str(slot_name)] = str(day_key)
        state["slots"] = slots

