from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any
import uuid

from app.settings import Settings
from app.utils import dump_json, load_json, now_utc_iso


_JOB_STATUSES = ("pending", "publishing", "published", "cancelled", "failed")


class StorageService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._job_status_lock = threading.Lock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.jobs_pending_dir,
            self.settings.jobs_publishing_dir,
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
        with self._job_status_lock:
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

    def update_pending_request(self, request_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        path = self.settings.pending_requests_dir / f"{request_id}.json"
        if not path.exists():
            return None
        payload = load_json(path)
        payload.update(dict(updates))
        payload["updated_at"] = now_utc_iso()
        dump_json(path, payload)
        return payload

    def find_latest_pending_web_media(
        self,
        *,
        chat_id: int,
        user_id: int,
        ttl_minutes: int,
    ) -> dict[str, Any] | None:
        return self.find_latest_pending_media(
            chat_id=chat_id,
            user_id=user_id,
            request_type="web_media",
            ttl_minutes=ttl_minutes,
        )

    def find_latest_pending_media(
        self,
        *,
        chat_id: int,
        user_id: int,
        request_type: str,
        ttl_minutes: int,
    ) -> dict[str, Any] | None:
        """Return the latest user-scoped media form, removing expired forms."""

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        expected_type = str(request_type or "").strip()
        for path in self.settings.pending_requests_dir.glob("*.json"):
            try:
                payload = load_json(path)
            except Exception:  # noqa: BLE001
                continue
            if str(payload.get("request_type", "")).strip() != expected_type:
                continue
            if int(payload.get("chat_id", 0) or 0) != int(chat_id):
                continue
            if int(payload.get("user_id", 0) or 0) != int(user_id):
                continue
            created_at = str(payload.get("created_at", "")).strip()
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - created).total_seconds() > max(1, int(ttl_minutes)) * 60:
                path.unlink(missing_ok=True)
                continue
            candidates.append(payload)
        candidates.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return candidates[0] if candidates else None

    def find_recoverable_web_media_forms(self, *, ttl_minutes: int) -> list[dict[str, Any]]:
        """Return complete Web forms left in the pre-creation caption state."""

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        required_fields = (
            "chat_id",
            "user_id",
            "caption",
            "headline",
            "cta_label",
            "cta_type",
            "destination_url",
            "budget_daily_vnd",
        )
        for path in self.settings.pending_requests_dir.glob("*.json"):
            try:
                payload = load_json(path)
            except Exception:  # noqa: BLE001
                continue
            if str(payload.get("request_type", "")).strip() != "web_media":
                continue
            if str(payload.get("stage", "")).strip().lower() != "awaiting_caption":
                continue
            created_at = str(payload.get("created_at", "")).strip()
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - created).total_seconds() > max(1, int(ttl_minutes)) * 60:
                path.unlink(missing_ok=True)
                continue
            if any(not str(payload.get(field, "")).strip() for field in required_fields):
                continue
            has_media = bool(
                str(payload.get("media_file_id", "")).strip()
                or str(payload.get("media_url", "")).strip()
            )
            if not has_media:
                continue
            candidates.append(payload)
        candidates.sort(key=lambda item: str(item.get("created_at", "")))
        return candidates

    def find_latest_pending_request_by_type(
        self,
        *,
        chat_id: int,
        user_id: int,
        request_type: str,
        ttl_minutes: int,
    ) -> dict[str, Any] | None:
        """Find the latest user-scoped pending step and remove expired records."""

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        expected_type = str(request_type or "").strip()
        for path in self.settings.pending_requests_dir.glob("*.json"):
            try:
                payload = load_json(path)
            except Exception:  # noqa: BLE001
                continue
            if str(payload.get("request_type", "")).strip() != expected_type:
                continue
            if int(payload.get("chat_id", 0) or 0) != int(chat_id):
                continue
            if int(payload.get("user_id", 0) or 0) != int(user_id):
                continue
            created_at = str(payload.get("created_at", "")).strip()
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - created).total_seconds() > max(1, int(ttl_minutes)) * 60:
                path.unlink(missing_ok=True)
                continue
            candidates.append(payload)
        candidates.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return candidates[0] if candidates else None
