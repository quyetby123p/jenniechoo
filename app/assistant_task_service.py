from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any
import unicodedata
import uuid
from zoneinfo import ZoneInfo

from app.assistant_settings import AssistantSettings
from app.utils import now_utc_iso


class AssistantTaskService:
    VALID_STATUSES = {"todo", "doing", "blocked", "done"}
    VALID_SOURCES = {"manager", "self"}

    def __init__(self, settings: AssistantSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.db_path = settings.resolved_task_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create_task(
        self,
        *,
        title: str,
        created_by: int,
        source_type: str,
        assigned_by: int = 0,
        group_chat_id: int = 0,
        note: str = "",
        deadline_date: str = "",
    ) -> dict[str, Any]:
        clean_title = _clean_text(title)
        if not clean_title:
            raise ValueError("Tiêu đề công việc không được để trống.")
        normalized_title = _normalize_lookup(clean_title)
        if not normalized_title:
            raise ValueError("Tiêu đề công việc chưa hợp lệ.")

        normalized_source = _normalize_source(source_type)
        normalized_deadline = _normalize_deadline(deadline_date)
        task_uid = f"task_{uuid.uuid4().hex[:10]}"
        now_iso = now_utc_iso()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_uid, title, title_norm, description, source_type,
                    created_by, assigned_by, group_chat_id, status, progress_percent,
                    blocked_reason, next_step, deadline_date, created_at, updated_at, closed_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_uid,
                    clean_title,
                    normalized_title,
                    _clean_text(note),
                    normalized_source,
                    int(created_by),
                    int(assigned_by or 0),
                    int(group_chat_id or 0),
                    "todo",
                    0,
                    "",
                    "",
                    normalized_deadline,
                    now_iso,
                    now_iso,
                    "",
                ),
            )
            conn.execute(
                """
                INSERT INTO task_updates(
                    task_uid, updated_by, chat_id, action_name,
                    status_before, status_after, progress_before, progress_after,
                    note, blocked_reason, next_step, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_uid,
                    int(created_by),
                    int(group_chat_id or 0),
                    "create",
                    "",
                    "todo",
                    0,
                    0,
                    _clean_text(note),
                    "",
                    "",
                    now_iso,
                ),
            )
            conn.commit()
        return self.get_task(task_uid) or {}

    def get_task(self, task_uid: str) -> dict[str, Any] | None:
        value = _clean_text(task_uid)
        if not value:
            return None
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE task_uid = ? LIMIT 1", (value,)).fetchone()
        return _row_to_dict(row)

    def list_tasks(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        normalized_status = _normalize_status(status) if status else ""
        safe_limit = max(1, min(100, int(limit)))
        where = ""
        params: list[Any] = []
        if normalized_status:
            where = "WHERE status = ?"
            params.append(normalized_status)
        elif str(status or "").strip().lower() in {"pending", "open"}:
            where = "WHERE status != 'done'"
        params.append(safe_limit)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    def find_tasks_by_title(self, title_query: str, *, include_done: bool = True, limit: int = 12) -> list[dict[str, Any]]:
        normalized_query = _normalize_lookup(title_query)
        if not normalized_query:
            return []
        safe_limit = max(1, min(30, int(limit)))
        params: list[Any] = [f"%{normalized_query}%"]
        where = "title_norm LIKE ?"
        if not include_done:
            where = where + " AND status != 'done'"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                (
                    "SELECT * FROM tasks "
                    f"WHERE {where} "
                    "ORDER BY CASE WHEN status='done' THEN 1 ELSE 0 END ASC, updated_at DESC "
                    "LIMIT ?"
                ),
                [*params, safe_limit],
            ).fetchall()
            if rows:
                return [_row_to_dict(row) for row in rows if row is not None]

            tokens = [item for item in normalized_query.split() if len(item) >= 2][:4]
            if not tokens:
                return []
            like_clauses = " OR ".join(["title_norm LIKE ?" for _ in tokens])
            token_params: list[Any] = [f"%{token}%" for token in tokens]
            if not include_done:
                like_clauses = f"({like_clauses}) AND status != 'done'"
            rows = conn.execute(
                (
                    "SELECT * FROM tasks "
                    f"WHERE {like_clauses} "
                    "ORDER BY CASE WHEN status='done' THEN 1 ELSE 0 END ASC, updated_at DESC "
                    "LIMIT ?"
                ),
                [*token_params, safe_limit],
            ).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    def update_task(
        self,
        *,
        task_uid: str,
        updated_by: int,
        chat_id: int,
        status: str | None,
        progress_percent: int | None,
        note: str,
        blocked_reason: str | None = None,
        next_step: str | None = None,
        deadline_date: str | None = None,
        action_name: str = "update",
    ) -> dict[str, Any]:
        task = self.get_task(task_uid)
        if not task:
            raise KeyError(f"Không tìm thấy task: {task_uid}")

        old_status = str(task.get("status", "todo")).strip() or "todo"
        old_progress = _to_int(task.get("progress_percent"), fallback=0)

        new_status = _normalize_status(status) if status else old_status
        if new_status not in self.VALID_STATUSES:
            raise ValueError("Trạng thái không hợp lệ. Dùng: todo|doing|blocked|done.")

        if progress_percent is None:
            new_progress = old_progress
        else:
            new_progress = int(progress_percent)
        if new_progress < 0 or new_progress > 100:
            raise ValueError("Phần trăm tiến độ phải nằm trong khoảng 0-100.")

        if new_status == "done":
            new_progress = 100

        clean_blocked = _clean_text(blocked_reason) if blocked_reason is not None else _clean_text(task.get("blocked_reason", ""))
        clean_next = _clean_text(next_step) if next_step is not None else _clean_text(task.get("next_step", ""))
        if new_status == "done":
            clean_blocked = ""
            clean_next = ""
        elif not clean_blocked or not clean_next:
            raise ValueError("Task chưa hoàn thành bắt buộc có lý do blocked và bước tiếp theo.")

        new_deadline = _clean_text(task.get("deadline_date", ""))
        if deadline_date is not None:
            new_deadline = _normalize_deadline(deadline_date)

        now_iso = now_utc_iso()
        closed_at = now_iso if new_status == "done" else ""
        note_text = _clean_text(note)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, progress_percent = ?, blocked_reason = ?, next_step = ?,
                    deadline_date = ?, updated_at = ?, closed_at = ?
                WHERE task_uid = ?
                """,
                (
                    new_status,
                    int(new_progress),
                    clean_blocked,
                    clean_next,
                    new_deadline,
                    now_iso,
                    closed_at,
                    _clean_text(task_uid),
                ),
            )
            conn.execute(
                """
                INSERT INTO task_updates(
                    task_uid, updated_by, chat_id, action_name,
                    status_before, status_after, progress_before, progress_after,
                    note, blocked_reason, next_step, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _clean_text(task_uid),
                    int(updated_by),
                    int(chat_id),
                    _clean_text(action_name) or "update",
                    old_status,
                    new_status,
                    int(old_progress),
                    int(new_progress),
                    note_text,
                    clean_blocked,
                    clean_next,
                    now_iso,
                ),
            )
            conn.commit()
        return self.get_task(task_uid) or {}

    def mark_done(self, *, task_uid: str, updated_by: int, chat_id: int, note: str) -> dict[str, Any]:
        return self.update_task(
            task_uid=task_uid,
            updated_by=updated_by,
            chat_id=chat_id,
            status="done",
            progress_percent=100,
            note=note,
            blocked_reason="",
            next_step="",
            action_name="done",
        )

    def build_overview_snapshot(self, *, max_items: int) -> dict[str, Any]:
        safe_max = max(1, min(20, int(max_items)))
        counts = {"todo": 0, "doing": 0, "blocked": 0, "done": 0}
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT status, COUNT(1) AS c FROM tasks GROUP BY status").fetchall()
            for row in rows:
                key = str(row["status"] or "").strip()
                if key in counts:
                    counts[key] = int(row["c"] or 0)
            pending_rows = conn.execute(
                "SELECT * FROM tasks WHERE status != 'done' ORDER BY updated_at DESC LIMIT ?",
                (safe_max,),
            ).fetchall()
            done_rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'done' ORDER BY closed_at DESC, updated_at DESC LIMIT ?",
                (safe_max,),
            ).fetchall()

        return {
            "counts": counts,
            "total": int(sum(counts.values())),
            "pending_items": [_row_to_dict(row) for row in pending_rows if row is not None],
            "done_items": [_row_to_dict(row) for row in done_rows if row is not None],
        }

    def build_weekly_snapshot(
        self,
        *,
        reference_date: date | None,
        timezone_name: str,
        max_items: int,
    ) -> dict[str, Any]:
        tz = _resolve_timezone(timezone_name)
        now_local_date = datetime.now(tz).date()
        target_date = reference_date or now_local_date
        week_start = target_date - timedelta(days=target_date.weekday())
        week_end = week_start + timedelta(days=5)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()

        tasks = [_row_to_dict(row) for row in rows if row is not None]
        done_in_week: list[dict[str, Any]] = []
        pending_current: list[dict[str, Any]] = []
        blocked_count = 0
        missing_detail_count = 0

        for task in tasks:
            status = str(task.get("status", "")).strip()
            if status == "blocked":
                blocked_count += 1
            if status == "done":
                closed_local = _parse_iso_to_local_date(task.get("closed_at"), tz=tz)
                if closed_local and week_start <= closed_local <= week_end:
                    done_in_week.append(task)
                continue

            created_local = _parse_iso_to_local_date(task.get("created_at"), tz=tz)
            if created_local and created_local <= week_end:
                pending_current.append(task)
                if not _clean_text(task.get("blocked_reason", "")) or not _clean_text(task.get("next_step", "")):
                    missing_detail_count += 1

        safe_max = max(1, min(20, int(max_items)))
        done_in_week.sort(key=lambda item: str(item.get("closed_at", "")), reverse=True)
        pending_current.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)

        return {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "done_count": len(done_in_week),
            "pending_count": len(pending_current),
            "blocked_count": blocked_count,
            "missing_detail_count": missing_detail_count,
            "done_items": done_in_week[:safe_max],
            "pending_items": pending_current[:safe_max],
        }

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_uid TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    title_norm TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    assigned_by INTEGER NOT NULL,
                    group_chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL,
                    blocked_reason TEXT NOT NULL,
                    next_step TEXT NOT NULL,
                    deadline_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_updates (
                    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_uid TEXT NOT NULL,
                    updated_by INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    action_name TEXT NOT NULL,
                    status_before TEXT NOT NULL,
                    status_after TEXT NOT NULL,
                    progress_before INTEGER NOT NULL,
                    progress_after INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    blocked_reason TEXT NOT NULL,
                    next_step TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_title_norm ON tasks(title_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_updates_uid ON task_updates(task_uid, created_at DESC)")
            conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {str(key): row[key] for key in row.keys()}


def _clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text.strip()


def _normalize_lookup(text: str) -> str:
    cleaned = _clean_text(text)
    folded = unicodedata.normalize("NFD", cleaned)
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accents.lower().replace("đ", "d")
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _normalize_source(value: str) -> str:
    token = _normalize_lookup(value)
    if token in {"self", "tu them", "tu giao"}:
        return "self"
    if token in {"manager", "sep", "s ep", "boss", "giao"}:
        return "manager"
    if token in AssistantTaskService.VALID_SOURCES:
        return token
    return "manager"


def _normalize_status(value: str | None) -> str:
    token = _normalize_lookup(value or "")
    mapping = {
        "todo": "todo",
        "to do": "todo",
        "chua lam": "todo",
        "pending": "todo",
        "doing": "doing",
        "in progress": "doing",
        "dang lam": "doing",
        "blocked": "blocked",
        "block": "blocked",
        "vuong": "blocked",
        "bi chan": "blocked",
        "done": "done",
        "xong": "done",
        "hoan thanh": "done",
        "completed": "done",
    }
    return mapping.get(token, token)


def _normalize_deadline(raw: str | None) -> str:
    value = _clean_text(raw or "")
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        return parsed.isoformat()
    except ValueError:
        raise ValueError("Deadline cần đúng định dạng YYYY-MM-DD hoặc để trống.")


def _to_int(value: Any, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Ho_Chi_Minh")


def _parse_iso_to_local_date(raw: Any, *, tz: ZoneInfo) -> date | None:
    value = _clean_text(raw)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date()
