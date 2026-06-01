from __future__ import annotations

import csv
from datetime import date
import logging
from pathlib import Path
from typing import Any
import uuid

from app.media_settings import MediaSettings
from app.utils import dump_json, load_json, now_utc_iso


class MediaStorageService:
    def __init__(self, settings: MediaSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.runs_dir,
            self.settings.reports_dir,
            self.settings.pending_requests_dir,
            self.settings.state_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def generate_run_id(self) -> str:
        return f"run_{uuid.uuid4().hex[:12]}"

    def save_run(self, run_payload: dict[str, Any]) -> Path:
        run_id = str(run_payload.get("run_id", "")).strip()
        if not run_id:
            raise ValueError("Run payload thiếu run_id.")
        payload = dict(run_payload)
        payload.setdefault("updated_at", now_utc_iso())
        path = self.settings.runs_dir / f"{run_id}.json"
        dump_json(path, payload)
        return path

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        path = self.settings.runs_dir / f"{run_id}.json"
        if not path.exists():
            return None
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None

    def update_run(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.load_run(run_id)
        if not current:
            raise FileNotFoundError(f"Khong tim thay run: {run_id}")
        payload = dict(current)
        payload.update(dict(updates))
        payload["updated_at"] = now_utc_iso()
        self.save_run(payload)
        return payload

    def save_report_csv(
        self,
        *,
        run_id: str,
        rows: list[dict[str, Any]],
        headers: list[str],
    ) -> Path:
        path = self.settings.reports_dir / f"{run_id}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                if isinstance(row, dict):
                    writer.writerow(row)
        return path

    def create_pending_request(self, payload: dict[str, Any], request_type: str = "media_sheet_sync") -> str:
        request_id = f"req_{uuid.uuid4().hex[:10]}"
        record = dict(payload)
        record["request_id"] = request_id
        record["request_type"] = request_type
        record["created_at"] = now_utc_iso()
        dump_json(self.settings.pending_requests_dir / f"{request_id}.json", record)
        return request_id

    def get_pending_request(self, request_id: str) -> dict[str, Any] | None:
        path = self.settings.pending_requests_dir / f"{request_id}.json"
        if not path.exists():
            return None
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None

    def delete_pending_request(self, request_id: str) -> None:
        path = self.settings.pending_requests_dir / f"{request_id}.json"
        if path.exists():
            path.unlink()

    def get_today_quota_usage(self, local_date: date) -> int:
        data = self._load_quota_state()
        key = local_date.isoformat()
        if str(data.get("date", "")) != key:
            return 0
        return self._to_int(data.get("count"))

    def increment_today_quota(self, local_date: date) -> int:
        data = self._load_quota_state()
        key = local_date.isoformat()
        count = self._to_int(data.get("count")) if str(data.get("date", "")) == key else 0
        count += 1
        payload = {"date": key, "count": count, "updated_at": now_utc_iso()}
        dump_json(self.settings.quota_state_file, payload)
        return count

    def _load_quota_state(self) -> dict[str, Any]:
        path = self.settings.quota_state_file
        if not path.exists():
            return {}
        payload = load_json(path)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
