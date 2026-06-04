from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from app.settings import Settings
from app.utils import load_json


class DailyTaskSummaryService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def build_summary(self, report_date: date | None = None) -> dict[str, Any]:
        target_date = report_date or datetime.now(self._resolve_timezone()).date()
        db_path = self.settings.daily_report_task_db_file
        if not db_path.exists():
            return {
                "available": False,
                "report_date": target_date.isoformat(),
                "reason": "Chưa tìm thấy task DB.",
                "db_path": str(db_path),
            }

        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                task_rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 500",
                ).fetchall()
                update_rows = conn.execute(
                    "SELECT * FROM task_updates ORDER BY created_at DESC LIMIT 1000",
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Khong doc duoc task DB cho daily report: %s", db_path)
            return {
                "available": False,
                "report_date": target_date.isoformat(),
                "reason": f"Không đọc được task DB: {exc}",
                "db_path": str(db_path),
            }

        tasks = [_row_to_dict(row) for row in task_rows]
        updates = [_row_to_dict(row) for row in update_rows]
        checkin_task_uids = self._load_checkin_task_uids(target_date)
        checkin_uid_set = set(checkin_task_uids)
        tasks_by_uid = {str(task.get("task_uid", "")).strip(): task for task in tasks}
        checkin_today_items = [tasks_by_uid[uid] for uid in checkin_task_uids if uid in tasks_by_uid]
        counts = {"todo": 0, "doing": 0, "blocked": 0, "done": 0}
        for task in tasks:
            status = _clean_text(task.get("status")).lower()
            if status in counts:
                counts[status] += 1

        max_items = max(1, min(20, int(self.settings.daily_report_task_summary_max_items)))
        done_today = [
            task
            for task in tasks
            if _clean_text(task.get("status")).lower() == "done"
            and self._local_date(task.get("closed_at")) == target_date
        ]
        created_today = [
            task
            for task in tasks
            if self._local_date(task.get("created_at")) == target_date
        ]
        updated_today = [
            update
            for update in updates
            if self._local_date(update.get("created_at")) == target_date
        ]
        pending_items = [
            task
            for task in tasks
            if _clean_text(task.get("status")).lower() != "done"
            and str(task.get("task_uid", "")).strip() not in checkin_uid_set
        ]
        pending_items.sort(key=self._pending_sort_key)

        return {
            "available": True,
            "report_date": target_date.isoformat(),
            "counts": counts,
            "total": sum(counts.values()),
            "created_today_count": len(created_today),
            "updated_today_count": len(updated_today),
            "done_today_count": len(done_today),
            "checkin_today_items": checkin_today_items[:max_items],
            "done_today_items": done_today[:max_items],
            "pending_items": pending_items[:max_items],
        }

    def build_message(self, summary: dict[str, Any]) -> str:
        lines = ["Task công việc cuối ngày:"]
        if not bool(summary.get("available")):
            reason = _clean_text(summary.get("reason")) or "chưa có dữ liệu task."
            lines.append(f"- {reason}")
            return "\n".join(lines)

        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        total = _to_int(summary.get("total"))
        if total <= 0:
            lines.append("- Chưa có task nào trong hệ thống.")
            return "\n".join(lines)

        lines.append(
            "- Tổng task: "
            f"{total:,} | Chưa làm: {_to_int(counts.get('todo')):,} | "
            f"Đang làm: {_to_int(counts.get('doing')):,} | "
            f"Blocked: {_to_int(counts.get('blocked')):,} | "
            f"Xong: {_to_int(counts.get('done')):,}"
        )
        lines.append(
            "- Hôm nay: "
            f"tạo mới {_to_int(summary.get('created_today_count')):,} | "
            f"cập nhật {_to_int(summary.get('updated_today_count')):,} | "
            f"hoàn thành {_to_int(summary.get('done_today_count')):,}"
        )

        checkin_items = summary.get("checkin_today_items") if isinstance(summary.get("checkin_today_items"), list) else []
        if checkin_items:
            lines.append("")
            lines.append("Công việc hôm nay:")
            for idx, task in enumerate(checkin_items, start=1):
                lines.append(f"{idx}) {self._format_pending_task(task)}")

        done_items = summary.get("done_today_items") if isinstance(summary.get("done_today_items"), list) else []
        if done_items:
            lines.append("")
            lines.append("Hoàn thành hôm nay:")
            for idx, task in enumerate(done_items, start=1):
                title = _short_text(task.get("title"), max_len=72) or "Không tên"
                lines.append(f"{idx}) {title}")

        pending_items = summary.get("pending_items") if isinstance(summary.get("pending_items"), list) else []
        if pending_items:
            lines.append("")
            lines.append("Việc đang mở cần chú ý:")
            for idx, task in enumerate(pending_items, start=1):
                lines.append(f"{idx}) {self._format_pending_task(task)}")

        return "\n".join(lines)

    def _format_pending_task(self, task: dict[str, Any]) -> str:
        status = _clean_text(task.get("status")).lower()
        title = _short_text(task.get("title"), max_len=64) or "Không tên"
        progress = _to_int(task.get("progress_percent"))
        parts = [f"[{_status_label_vi(status)} {progress}%] {title}"]

        deadline = _clean_text(task.get("deadline_date"))
        if deadline:
            parts.append(f"deadline {deadline}")

        blocked_reason = _short_text(task.get("blocked_reason"), max_len=80)
        if status == "blocked" and blocked_reason:
            parts.append(f"vướng: {blocked_reason}")

        next_step = _short_text(task.get("next_step"), max_len=80)
        if next_step:
            parts.append(f"tiếp: {next_step}")

        return " | ".join(parts)

    def _pending_sort_key(self, task: dict[str, Any]) -> tuple[int, str]:
        status = _clean_text(task.get("status")).lower()
        priority = {"blocked": 0, "doing": 1, "todo": 2}.get(status, 3)
        updated_at = _clean_text(task.get("updated_at"))
        return (priority, _reverse_text_sort_key(updated_at))

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    def _load_checkin_task_uids(self, target_date: date) -> list[str]:
        path = self.settings.project_root / "state" / "assistant_bot" / "daily_task_checkin_state.json"
        if not path.exists():
            return []
        payload = load_json(path)
        if not isinstance(payload, dict):
            return []
        days = payload.get("days", {})
        if not isinstance(days, dict):
            return []
        day_state = days.get(target_date.isoformat(), {})
        if not isinstance(day_state, dict):
            return []
        values = day_state.get("task_uids", [])
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for item in values:
            value = _clean_text(item)
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned

    def _local_date(self, raw: Any) -> date | None:
        value = _clean_text(raw)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(self._resolve_timezone()).date()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _short_text(value: Any, *, max_len: int) -> str:
    text = _clean_text(value)
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 3)].rstrip() + "..."


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _status_label_vi(status: str) -> str:
    return {
        "todo": "Chưa làm",
        "doing": "Đang làm",
        "blocked": "Blocked",
        "done": "Xong",
    }.get(_clean_text(status).lower(), "Khác")


def _reverse_text_sort_key(value: str) -> str:
    # Python sorts ascending; invert codepoints to keep newer ISO timestamps first.
    return "".join(chr(0x10FFFF - ord(char)) for char in value)
