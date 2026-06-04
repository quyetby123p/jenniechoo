from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import os
from typing import Any

import requests


def _parse_bool(raw: str, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


@dataclass
class CloudScheduleGuardClient:
    mark_url: str
    secret: str
    logger: logging.Logger
    enabled: bool = True
    timeout_seconds: float = 8.0
    _marked_keys: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, *, logger: logging.Logger) -> CloudScheduleGuardClient:
        enabled = _parse_bool(os.getenv("CLOUD_SCHEDULE_GUARD_ENABLED", "1"), True)
        mark_url = os.getenv("CLOUD_SCHEDULE_GUARD_MARK_URL", "").strip()
        secret = (
            os.getenv("CLOUD_SCHEDULE_GUARD_SECRET", "").strip()
            or os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        )
        timeout_seconds = float(os.getenv("CLOUD_SCHEDULE_GUARD_TIMEOUT_SECONDS", "8") or "8")
        if os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true":
            enabled = False
        return cls(
            mark_url=mark_url,
            secret=secret,
            logger=logger,
            enabled=enabled,
            timeout_seconds=max(1.0, timeout_seconds),
        )

    def is_configured(self) -> bool:
        return bool(self.enabled and self.mark_url and self.secret)

    def mark_completed(
        self,
        *,
        task: str,
        slot: str | None = None,
        run_date: date | str | None = None,
        bucket: str | None = None,
        source: str = "local",
    ) -> bool:
        if not self.is_configured():
            return False

        body: dict[str, Any] = {
            "task": str(task).strip(),
            "source": str(source).strip() or "local",
        }
        if slot:
            body["slot"] = str(slot).strip()
        if run_date:
            body["run_date"] = run_date.isoformat() if isinstance(run_date, date) else str(run_date).strip()
        if bucket:
            body["bucket"] = str(bucket).strip()

        local_key = "|".join(str(body.get(name, "")) for name in ("task", "slot", "run_date", "bucket"))
        if local_key in self._marked_keys:
            return True

        try:
            response = requests.post(
                self.mark_url,
                json=body,
                headers={"X-Schedule-Guard-Secret": self.secret},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception:  # noqa: BLE001
            self.logger.exception("Cloud schedule guard mark failed for task=%s", body.get("task"))
            return False

        self._marked_keys.add(local_key)
        self.logger.info("Marked local scheduled task completion for cloud guard: %s", body)
        return True
