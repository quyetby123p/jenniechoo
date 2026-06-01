from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any
import uuid

from app.assistant_settings import AssistantSettings
from app.utils import dump_json, load_json, now_utc_iso


class AssistantStorageService:
    def __init__(self, settings: AssistantSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.storage_root,
            self.settings.logs_root,
            self.settings.state_root,
            self.settings.pending_requests_dir,
            self.settings.conversation_logs_dir,
            self.settings.run_logs_dir,
            self.settings.state_root / "task_drafts",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def create_pending_request(self, payload: dict[str, Any], request_type: str = "assistant_action") -> str:
        request_id = f"asreq_{uuid.uuid4().hex[:12]}"
        record = dict(payload)
        record["request_id"] = request_id
        record["request_type"] = request_type
        record["created_at"] = now_utc_iso()
        record["status"] = "pending"
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

    def is_request_processed(self, request_id: str) -> bool:
        state = self._load_request_state()
        values = state.get("processed_request_ids", [])
        if not isinstance(values, list):
            return False
        return request_id in {str(item).strip() for item in values if str(item).strip()}

    def mark_request_processed(self, request_id: str) -> None:
        if not request_id:
            return
        state = self._load_request_state()
        values = state.get("processed_request_ids", [])
        if not isinstance(values, list):
            values = []
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if request_id not in cleaned:
            cleaned.append(request_id)
        # Keep state bounded.
        cleaned = cleaned[-5000:]
        state["processed_request_ids"] = cleaned
        state["updated_at"] = now_utc_iso()
        dump_json(self.settings.state_root / "request_state.json", state)

    def append_conversation_log(
        self,
        *,
        user_text: str,
        bot_text: str,
        intent: str,
        sources: list[str] | None = None,
    ) -> Path:
        now = datetime.now(timezone.utc)
        path = self.settings.conversation_logs_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
        entry = {
            "timestamp": now_utc_iso(),
            "intent": str(intent).strip(),
            "user_text": str(user_text or ""),
            "bot_text": str(bot_text or ""),
            "sources": list(sources or []),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_json_line(entry))
        return path

    def save_run_payload(self, payload: dict[str, Any]) -> Path:
        run_id = str(payload.get("run_id", "")).strip() or f"run_{uuid.uuid4().hex[:12]}"
        data = dict(payload)
        data["run_id"] = run_id
        data.setdefault("created_at", now_utc_iso())
        data["updated_at"] = now_utc_iso()
        path = self.settings.run_logs_dir / f"{run_id}.json"
        dump_json(path, data)
        return path

    def load_reminder_state(self) -> dict[str, Any]:
        path = self.settings.reminder_state_file
        if not path.exists():
            return {}
        payload = load_json(path)
        return payload if isinstance(payload, dict) else {}

    def save_reminder_state(self, payload: dict[str, Any]) -> None:
        data = dict(payload)
        data["updated_at"] = now_utc_iso()
        dump_json(self.settings.reminder_state_file, data)

    def check_and_increment_rate_limit(self, *, user_id: int) -> tuple[bool, int]:
        state = self._load_rate_limit_state()
        minute_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        users = state.get("users", {})
        if not isinstance(users, dict):
            users = {}
        raw = users.get(str(user_id), {})
        if not isinstance(raw, dict):
            raw = {}
        key = str(raw.get("minute_key", "")).strip()
        count = _to_int(raw.get("count"), fallback=0)
        if key != minute_key:
            count = 0
        count += 1
        users[str(user_id)] = {"minute_key": minute_key, "count": count}
        state["users"] = users
        state["updated_at"] = now_utc_iso()
        dump_json(self.settings.rate_limit_state_file, state)
        return count <= self.settings.rate_limit_per_minute, count

    def _load_request_state(self) -> dict[str, Any]:
        path = self.settings.state_root / "request_state.json"
        if not path.exists():
            return {}
        payload = load_json(path)
        return payload if isinstance(payload, dict) else {}

    def _load_rate_limit_state(self) -> dict[str, Any]:
        path = self.settings.rate_limit_state_file
        if not path.exists():
            return {}
        payload = load_json(path)
        return payload if isinstance(payload, dict) else {}

    def load_task_draft(self, *, user_id: int) -> dict[str, Any] | None:
        path = self._task_draft_file(user_id)
        if not path.exists():
            return None
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None

    def save_task_draft(self, *, user_id: int, payload: dict[str, Any]) -> None:
        record = dict(payload)
        record["updated_at"] = now_utc_iso()
        dump_json(self._task_draft_file(user_id), record)

    def delete_task_draft(self, *, user_id: int) -> None:
        path = self._task_draft_file(user_id)
        if path.exists():
            path.unlink()

    def _task_draft_file(self, user_id: int) -> Path:
        safe_user_id = max(0, int(user_id))
        return self.settings.state_root / "task_drafts" / f"{safe_user_id}.json"


def _json_line(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False) + "\n"


def _to_int(value: Any, *, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
