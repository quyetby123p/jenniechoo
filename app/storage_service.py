from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import uuid

from app.settings import Settings
from app.utils import dump_json, load_json, now_utc_iso


_JOB_STATUSES = ("pending", "published", "cancelled", "failed")


class StorageService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.jobs_pending_dir,
            self.settings.jobs_published_dir,
            self.settings.jobs_cancelled_dir,
            self.settings.jobs_failed_dir,
            self.settings.pending_requests_dir,
            self.settings.reconcile_cod_runs_dir,
            self.settings.reconcile_cod_reports_dir,
            self.settings.reconcile_cod_applied_dir,
            self.settings.reconcile_cod_import_history_dir,
            self.settings.reconcile_cod_import_detail_dir,
            self.settings.pancake_td_sync_runs_dir,
            self.settings.pancake_td_sync_state_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _status_dir(self, status: str) -> Path:
        if status not in _JOB_STATUSES:
            raise ValueError(f"Trang thai khong hop le: {status}")
        return self.settings.jobs_root / status

    def generate_job_id(self) -> str:
        return f"job_{uuid.uuid4().hex[:12]}"

    def save_job(self, payload: dict[str, Any], status: str = "pending") -> Path:
        payload = dict(payload)
        payload["status"] = status
        payload.setdefault("updated_at", now_utc_iso())
        path = self._status_dir(status) / f"{payload['job_id']}.json"
        dump_json(path, payload)
        return path

    def move_job_status(
        self, job_id: str, from_status: str, to_status: str, extra_updates: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        source = self._status_dir(from_status) / f"{job_id}.json"
        if not source.exists():
            raise FileNotFoundError(f"Khong tim thay job {job_id} trong {from_status}")

        payload = load_json(source)
        payload["status"] = to_status
        payload["updated_at"] = now_utc_iso()
        if extra_updates:
            payload.update(extra_updates)

        target = self._status_dir(to_status) / f"{job_id}.json"
        dump_json(target, payload)
        source.unlink()
        return payload

    def find_job(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        for status in _JOB_STATUSES:
            path = self._status_dir(status) / f"{job_id}.json"
            if path.exists():
                return status, load_json(path)
        return None

    def list_jobs_by_fingerprint(self, post_fingerprint: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for status in _JOB_STATUSES:
            for path in self._status_dir(status).glob("*.json"):
                payload = load_json(path)
                if payload.get("post_fingerprint") == post_fingerprint:
                    payload["status"] = status
                    matches.append(payload)
        return matches

    def create_pending_request(self, payload: dict[str, Any], request_type: str = "duplicate_confirm") -> str:
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
        return load_json(path)

    def delete_pending_request(self, request_id: str) -> None:
        path = self.settings.pending_requests_dir / f"{request_id}.json"
        if path.exists():
            path.unlink()
