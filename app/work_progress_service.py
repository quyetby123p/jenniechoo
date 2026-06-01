from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.utils import fingerprint, now_utc_iso
from app.work_progress_extractor import ExtractedProgress, extract_progress_signal
from app.work_progress_settings import WorkProgressSettings

try:
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception:  # noqa: BLE001
    psycopg = None
    dict_row = None


VALID_STATUSES = {"todo", "doing", "blocked", "done"}
PENDING_STATES = {"pending_fast", "pending_manual"}


class WorkProgressService:
    def __init__(self, settings: WorkProgressSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._db_kind, self._db_target = self._resolve_database_target(settings.database_url)
        if self._db_kind == "sqlite":
            sqlite_path = Path(self._db_target)
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def upsert_member_identity(
        self,
        *,
        member_id: str,
        platform: str,
        platform_user_id: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        clean_member_id = _clean_text(member_id)
        clean_platform = _normalize_platform(platform)
        clean_platform_user_id = _clean_text(platform_user_id)
        clean_display_name = _clean_text(display_name)
        if not clean_member_id:
            raise ValueError("member_id khong duoc de trong.")
        if not clean_platform_user_id:
            raise ValueError("platform_user_id khong duoc de trong.")

        with self._connect() as conn:
            self._execute(
                conn,
                """
                INSERT INTO member_identity_map(member_id, platform, platform_user_id, display_name, created_at, updated_at)
                VALUES(%s, %s, %s, %s, %s, %s)
                ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                    member_id=excluded.member_id,
                    display_name=excluded.display_name,
                    updated_at=excluded.updated_at
                """,
                (
                    clean_member_id,
                    clean_platform,
                    clean_platform_user_id,
                    clean_display_name,
                    now_utc_iso(),
                    now_utc_iso(),
                ),
            )
            conn.commit()
        return {
            "member_id": clean_member_id,
            "platform": clean_platform,
            "platform_user_id": clean_platform_user_id,
            "display_name": clean_display_name,
        }

    def list_member_identities(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT member_id, platform, platform_user_id, display_name, updated_at
                FROM member_identity_map
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
        return rows

    def ingest_event(self, platform: str, payload: dict[str, Any]) -> dict[str, Any]:
        source_platform = _normalize_platform(platform)
        channel_id = _clean_text(payload.get("channel_id") or payload.get("chat_id") or payload.get("thread_id"))
        sender_id = _clean_text(payload.get("sender_id") or payload.get("user_id") or payload.get("from_id"))
        message_text = _clean_text(payload.get("message_text") or payload.get("text") or payload.get("content"))
        event_time_iso, event_epoch = _normalize_event_time(payload.get("event_time"))
        raw_payload = payload.get("raw_payload")
        if raw_payload is None:
            raw_payload = payload
        raw_payload_json = json.dumps(raw_payload, ensure_ascii=False)

        external_event_id = _clean_text(payload.get("event_id") or payload.get("message_id"))
        if external_event_id:
            event_id = f"{source_platform}:{external_event_id}"
        else:
            event_id = f"{source_platform}:{fingerprint('|'.join([channel_id, sender_id, event_time_iso, message_text]))[:24]}"

        with self._connect() as conn:
            existing = self._fetchone(
                conn,
                """
                SELECT event_id, ingest_status
                FROM work_events
                WHERE event_id = %s
                LIMIT 1
                """,
                (event_id,),
            )
            if existing:
                return {
                    "ok": True,
                    "deduped": True,
                    "event_id": event_id,
                    "ingest_status": str(existing.get("ingest_status", "duplicate")),
                    "progress_update": None,
                }

            self._execute(
                conn,
                """
                INSERT INTO work_events(
                    event_id, platform, channel_id, sender_id, message_text, event_time,
                    event_epoch, raw_payload, ingest_status, created_at, updated_at
                )
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_id,
                    source_platform,
                    channel_id,
                    sender_id,
                    message_text,
                    event_time_iso,
                    event_epoch,
                    raw_payload_json,
                    "received",
                    now_utc_iso(),
                    now_utc_iso(),
                ),
            )
            conn.commit()

            if not self._is_channel_allowed(source_platform, channel_id):
                self._update_event_status(conn, event_id=event_id, status="ignored_not_allowlisted")
                conn.commit()
                return {
                    "ok": True,
                    "deduped": False,
                    "event_id": event_id,
                    "ingest_status": "ignored_not_allowlisted",
                    "progress_update": None,
                }

            if not message_text:
                self._update_event_status(conn, event_id=event_id, status="ignored_empty")
                conn.commit()
                return {
                    "ok": True,
                    "deduped": False,
                    "event_id": event_id,
                    "ingest_status": "ignored_empty",
                    "progress_update": None,
                }

            member_id = self._resolve_member_id(conn, platform=source_platform, sender_id=sender_id)
            if not member_id:
                self._update_event_status(conn, event_id=event_id, status="pending_identity_map")
                conn.commit()
                return {
                    "ok": True,
                    "deduped": False,
                    "event_id": event_id,
                    "ingest_status": "pending_identity_map",
                    "progress_update": None,
                }

            context_text = self._build_context_window_text(
                conn,
                platform=source_platform,
                channel_id=channel_id,
                sender_id=sender_id,
                event_epoch=event_epoch,
                window_minutes=self.settings.context_window_minutes,
            )
            extracted = extract_progress_signal(
                message_text,
                context_text=context_text,
                today=_local_date_from_epoch(event_epoch, tz_name=self.settings.timezone_name),
            )
            if not extracted:
                self._update_event_status(conn, event_id=event_id, status="ignored_no_signal")
                conn.commit()
                return {
                    "ok": True,
                    "deduped": False,
                    "event_id": event_id,
                    "ingest_status": "ignored_no_signal",
                    "progress_update": None,
                }

            progress_update = self._create_progress_update(
                conn,
                event_id=event_id,
                member_id=member_id,
                extracted=extracted,
                source_event_time=event_time_iso,
                source_event_epoch=event_epoch,
            )
            self._update_event_status(conn, event_id=event_id, status="extracted")
            conn.commit()
            return {
                "ok": True,
                "deduped": False,
                "event_id": event_id,
                "ingest_status": "extracted",
                "progress_update": progress_update,
            }

    def list_pending_updates(self, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT *
                FROM progress_updates
                WHERE review_state IN ('pending_fast', 'pending_manual')
                ORDER BY
                    CASE WHEN review_state='pending_fast' THEN 0 ELSE 1 END ASC,
                    source_event_epoch DESC,
                    created_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
        return rows

    def list_pending_identity_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(500, int(limit)))
        with self._connect() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT event_id, platform, channel_id, sender_id, message_text, event_time, ingest_status, created_at
                FROM work_events
                WHERE ingest_status='pending_identity_map'
                ORDER BY event_epoch DESC, created_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
        return rows

    def approve_update(self, *, update_id: str, reviewer_id: str, note: str = "") -> dict[str, Any]:
        return self._apply_review_action(
            update_id=_clean_text(update_id),
            reviewer_id=_clean_text(reviewer_id),
            action_name="approve",
            note=_clean_text(note),
            patch={},
            next_state="approved",
        )

    def reject_update(self, *, update_id: str, reviewer_id: str, note: str = "") -> dict[str, Any]:
        return self._apply_review_action(
            update_id=_clean_text(update_id),
            reviewer_id=_clean_text(reviewer_id),
            action_name="reject",
            note=_clean_text(note),
            patch={},
            next_state="rejected",
        )

    def edit_update(
        self,
        *,
        update_id: str,
        reviewer_id: str,
        patch: dict[str, Any],
        note: str = "",
        approve_after_edit: bool = True,
    ) -> dict[str, Any]:
        clean_update_id = _clean_text(update_id)
        clean_reviewer_id = _clean_text(reviewer_id)
        if not clean_update_id:
            raise ValueError("update_id khong hop le.")
        if not clean_reviewer_id:
            raise ValueError("reviewer_id khong hop le.")

        allowed_fields = {
            "task_key",
            "status",
            "progress_pct",
            "blocker",
            "next_step",
            "deadline_date",
            "member_id",
        }
        clean_patch: dict[str, Any] = {}
        for key, value in (patch or {}).items():
            if key not in allowed_fields:
                continue
            if key == "status":
                clean_patch[key] = _normalize_status(value)
            elif key == "progress_pct":
                clean_patch[key] = max(0, min(100, int(value)))
            elif key in {"task_key", "blocker", "next_step", "deadline_date", "member_id"}:
                clean_patch[key] = _clean_text(value)

        if not clean_patch:
            raise ValueError("Patch edit rong hoac khong co field hop le.")
        if "status" in clean_patch and clean_patch["status"] not in VALID_STATUSES:
            raise ValueError("status edit khong hop le.")
        if "deadline_date" in clean_patch and clean_patch["deadline_date"]:
            normalized_deadline = _normalize_date_token(clean_patch["deadline_date"])
            if not normalized_deadline:
                raise ValueError("deadline_date phai theo YYYY-MM-DD hoac DD/MM.")
            clean_patch["deadline_date"] = normalized_deadline

        with self._connect() as conn:
            current = self._fetchone(
                conn,
                """
                SELECT *
                FROM progress_updates
                WHERE update_id = %s
                LIMIT 1
                """,
                (clean_update_id,),
            )
            if not current:
                raise KeyError(f"Khong tim thay update: {clean_update_id}")

            merged = dict(current)
            merged.update(clean_patch)
            merged_status = _normalize_status(merged.get("status", "todo"))
            merged["status"] = merged_status
            if merged_status == "done":
                merged["progress_pct"] = 100
            merged["updated_at"] = now_utc_iso()

            next_state = "approved" if approve_after_edit else "pending_manual"
            approved_by = clean_reviewer_id if next_state == "approved" else ""
            approved_at = now_utc_iso() if next_state == "approved" else ""

            self._execute(
                conn,
                """
                UPDATE progress_updates
                SET member_id=%s, task_key=%s, status=%s, progress_pct=%s, blocker=%s, next_step=%s,
                    deadline_date=%s, review_state=%s, approved_by=%s, approved_at=%s, updated_at=%s
                WHERE update_id=%s
                """,
                (
                    merged.get("member_id", ""),
                    merged.get("task_key", ""),
                    merged.get("status", "todo"),
                    int(merged.get("progress_pct", 0)),
                    merged.get("blocker", ""),
                    merged.get("next_step", ""),
                    merged.get("deadline_date", ""),
                    next_state,
                    approved_by,
                    approved_at,
                    merged["updated_at"],
                    clean_update_id,
                ),
            )
            self._insert_review_audit(
                conn,
                update_id=clean_update_id,
                action_name="edit",
                reviewer_id=clean_reviewer_id,
                note=_clean_text(note),
                patch=clean_patch,
            )
            conn.commit()
            fresh = self._fetchone(
                conn,
                "SELECT * FROM progress_updates WHERE update_id=%s LIMIT 1",
                (clean_update_id,),
            )
        return fresh or {}

    def build_report(self, report_type: str, *, anchor_date: date | None = None) -> dict[str, Any]:
        clean_type = str(report_type or "").strip().lower()
        if clean_type not in {"daily", "weekly", "monthly"}:
            raise ValueError("report_type phai la daily|weekly|monthly.")
        if anchor_date is None:
            anchor_date = datetime.now(ZoneInfo(self.settings.timezone_name)).date()

        start_dt, end_dt, previous_start_dt, previous_end_dt = _resolve_period(clean_type, anchor_date, self.settings.timezone_name)
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()
        prev_start_ts = previous_start_dt.timestamp()
        prev_end_ts = previous_end_dt.timestamp()

        with self._connect() as conn:
            rows = self._fetchall(
                conn,
                """
                SELECT *
                FROM progress_updates
                WHERE review_state='approved'
                  AND source_event_epoch >= %s
                  AND source_event_epoch < %s
                ORDER BY source_event_epoch ASC, created_at ASC
                """,
                (start_ts, end_ts),
            )
            previous_done_rows = self._fetchall(
                conn,
                """
                SELECT member_id, COUNT(1) AS done_count
                FROM progress_updates
                WHERE review_state='approved'
                  AND status='done'
                  AND source_event_epoch >= %s
                  AND source_event_epoch < %s
                GROUP BY member_id
                """,
                (prev_start_ts, prev_end_ts),
            )

        previous_done_map = {str(item.get("member_id", "")): int(item.get("done_count", 0)) for item in previous_done_rows}
        per_member_latest: dict[tuple[str, str], dict[str, Any]] = {}
        member_updates_counter: Counter[str] = Counter()
        blocker_counter: Counter[str] = Counter()
        for row in rows:
            member_id = _clean_text(row.get("member_id", "")) or "unmapped"
            task_key = _clean_text(row.get("task_key", "")) or "general"
            member_updates_counter[member_id] += 1
            if row.get("blocker"):
                blocker_counter[_clean_text(row.get("blocker", ""))] += 1
            per_member_latest[(member_id, task_key)] = row

        member_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for (member_id, _task_key), row in per_member_latest.items():
            member_bucket[member_id].append(row)

        member_reports: list[dict[str, Any]] = []
        team_done_total = 0
        team_blocked_total = 0
        overdue_total = 0
        anchor_local_date = anchor_date

        for member_id, task_rows in sorted(member_bucket.items(), key=lambda item: item[0]):
            done_tasks: list[str] = []
            doing_tasks: list[str] = []
            blocked_tasks: list[str] = []
            todo_tasks: list[str] = []
            blockers: list[str] = []
            next_steps: list[str] = []
            overdue_tasks: list[str] = []

            for row in task_rows:
                status = _normalize_status(row.get("status", "todo"))
                task_key = _clean_text(row.get("task_key", "")) or "general"
                if status == "done":
                    done_tasks.append(task_key)
                elif status == "doing":
                    doing_tasks.append(task_key)
                elif status == "blocked":
                    blocked_tasks.append(task_key)
                else:
                    todo_tasks.append(task_key)

                blocker_value = _clean_text(row.get("blocker", ""))
                next_step_value = _clean_text(row.get("next_step", ""))
                if blocker_value:
                    blockers.append(blocker_value)
                if next_step_value:
                    next_steps.append(next_step_value)

                deadline_value = _normalize_date_token(_clean_text(row.get("deadline_date", "")))
                if deadline_value and status != "done":
                    try:
                        deadline_obj = date.fromisoformat(deadline_value)
                        if deadline_obj < anchor_local_date:
                            overdue_tasks.append(task_key)
                    except ValueError:
                        pass

            total_tasks = max(1, len(task_rows))
            completion_pct = round(((len(done_tasks) + 0.5 * len(doing_tasks)) / total_tasks) * 100, 2)
            team_done_total += len(done_tasks)
            team_blocked_total += len(blocked_tasks)
            overdue_total += len(overdue_tasks)
            previous_done_count = previous_done_map.get(member_id, 0)
            trend_done_delta = len(done_tasks) - previous_done_count

            member_reports.append(
                {
                    "member_id": member_id,
                    "updates_count": member_updates_counter.get(member_id, 0),
                    "completion_pct": completion_pct,
                    "done_tasks": sorted(set(done_tasks)),
                    "doing_tasks": sorted(set(doing_tasks)),
                    "blocked_tasks": sorted(set(blocked_tasks)),
                    "todo_tasks": sorted(set(todo_tasks)),
                    "blockers": sorted(set(blockers)),
                    "next_steps": sorted(set(next_steps)),
                    "overdue_tasks": sorted(set(overdue_tasks)),
                    "trend_done_delta": trend_done_delta,
                }
            )

        top_blockers = [
            {"blocker": blocker, "count": count}
            for blocker, count in blocker_counter.most_common(5)
            if blocker
        ]

        return {
            "ok": True,
            "report_type": clean_type,
            "timezone": self.settings.timezone_name,
            "anchor_date": anchor_date.isoformat(),
            "period_start": start_dt.isoformat(),
            "period_end_exclusive": end_dt.isoformat(),
            "team_summary": {
                "members_count": len(member_reports),
                "approved_updates": len(rows),
                "done_tasks_total": team_done_total,
                "blocked_tasks_total": team_blocked_total,
                "overdue_tasks_total": overdue_total,
                "top_blockers": top_blockers,
            },
            "members": member_reports,
        }

    def format_report_text(self, report: dict[str, Any]) -> str:
        report_type = str(report.get("report_type", "")).strip().upper()
        anchor_date = str(report.get("anchor_date", "")).strip()
        team_summary = report.get("team_summary", {}) if isinstance(report.get("team_summary"), dict) else {}
        members = report.get("members", []) if isinstance(report.get("members"), list) else []
        lines = [
            f"[WORK PROGRESS {report_type}] {anchor_date}",
            f"- Thanh vien co du lieu: {team_summary.get('members_count', 0)}",
            f"- Ban ghi da duyet: {team_summary.get('approved_updates', 0)}",
            f"- Task done: {team_summary.get('done_tasks_total', 0)}",
            f"- Task blocked: {team_summary.get('blocked_tasks_total', 0)}",
            f"- Task qua han: {team_summary.get('overdue_tasks_total', 0)}",
        ]
        if members:
            lines.append("")
            lines.append("Chi tiet theo thanh vien:")
            for item in members[:30]:
                member_id = str(item.get("member_id", ""))
                completion_pct = item.get("completion_pct", 0)
                done_tasks = item.get("done_tasks", [])
                doing_tasks = item.get("doing_tasks", [])
                blockers = item.get("blockers", [])
                next_steps = item.get("next_steps", [])
                lines.append(f"- {member_id}: {completion_pct}%")
                lines.append(f"  done={len(done_tasks)} | doing={len(doing_tasks)} | blocker={len(blockers)}")
                if blockers:
                    lines.append(f"  blocker detail: {', '.join(str(v) for v in blockers[:3])}")
                if next_steps:
                    lines.append(f"  next: {', '.join(str(v) for v in next_steps[:3])}")
        else:
            lines.append("")
            lines.append("Khong co du lieu da duyet trong ky.")
        return "\n".join(lines)

    def _create_progress_update(
        self,
        conn,
        *,
        event_id: str,
        member_id: str,
        extracted: ExtractedProgress,
        source_event_time: str,
        source_event_epoch: float,
    ) -> dict[str, Any]:
        update_id = f"wupd_{uuid4().hex[:12]}"
        review_state = "pending_fast" if extracted.confidence >= float(self.settings.confidence_fast_track) else "pending_manual"
        now_iso = now_utc_iso()
        self._execute(
            conn,
            """
            INSERT INTO progress_updates(
                update_id, member_id, task_key, status, progress_pct, blocker, next_step,
                deadline_date, confidence, source_event_id, source_event_time, source_event_epoch,
                review_state, review_note, approved_by, approved_at, created_at, updated_at
            )
            VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                update_id,
                member_id,
                extracted.task_key,
                extracted.status,
                int(extracted.progress_pct),
                extracted.blocker,
                extracted.next_step,
                extracted.deadline_date,
                float(extracted.confidence),
                event_id,
                source_event_time,
                float(source_event_epoch),
                review_state,
                extracted.summary,
                "",
                "",
                now_iso,
                now_iso,
            ),
        )
        row = self._fetchone(
            conn,
            "SELECT * FROM progress_updates WHERE update_id=%s LIMIT 1",
            (update_id,),
        )
        return row or {}

    def _build_context_window_text(
        self,
        conn,
        *,
        platform: str,
        channel_id: str,
        sender_id: str,
        event_epoch: float,
        window_minutes: int,
    ) -> str:
        start_epoch = float(event_epoch) - max(1, int(window_minutes)) * 60.0
        rows = self._fetchall(
            conn,
            """
            SELECT message_text
            FROM work_events
            WHERE platform=%s
              AND channel_id=%s
              AND sender_id=%s
              AND event_epoch >= %s
              AND event_epoch <= %s
            ORDER BY event_epoch DESC
            LIMIT 10
            """,
            (platform, channel_id, sender_id, start_epoch, float(event_epoch)),
        )
        texts = [_clean_text(item.get("message_text", "")) for item in rows]
        texts = [value for value in texts if value]
        return "\n".join(texts[:10])

    def _update_event_status(self, conn, *, event_id: str, status: str) -> None:
        self._execute(
            conn,
            """
            UPDATE work_events
            SET ingest_status=%s, updated_at=%s
            WHERE event_id=%s
            """,
            (status, now_utc_iso(), event_id),
        )

    def _resolve_member_id(self, conn, *, platform: str, sender_id: str) -> str:
        if not sender_id:
            return ""
        row = self._fetchone(
            conn,
            """
            SELECT member_id
            FROM member_identity_map
            WHERE platform=%s AND platform_user_id=%s
            LIMIT 1
            """,
            (platform, sender_id),
        )
        return _clean_text(row.get("member_id", "")) if row else ""

    def _is_channel_allowed(self, platform: str, channel_id: str) -> bool:
        if not channel_id:
            return False
        mapping = {
            "telegram": self.settings.telegram_allowlist_channel_ids,
            "zalo": self.settings.zalo_allowlist_channel_ids,
            "pancake-work": self.settings.pancake_allowlist_channel_ids,
            "forwarded": self.settings.forwarded_allowlist_channel_ids,
        }
        allowlist = mapping.get(platform, [])
        if not allowlist:
            return True
        return str(channel_id) in {str(item) for item in allowlist}

    def _apply_review_action(
        self,
        *,
        update_id: str,
        reviewer_id: str,
        action_name: str,
        note: str,
        patch: dict[str, Any],
        next_state: str,
    ) -> dict[str, Any]:
        if not update_id:
            raise ValueError("update_id khong hop le.")
        if not reviewer_id:
            raise ValueError("reviewer_id khong hop le.")
        if next_state not in {"approved", "rejected"}:
            raise ValueError("next_state khong hop le.")

        with self._connect() as conn:
            row = self._fetchone(
                conn,
                """
                SELECT *
                FROM progress_updates
                WHERE update_id=%s
                LIMIT 1
                """,
                (update_id,),
            )
            if not row:
                raise KeyError(f"Khong tim thay update: {update_id}")

            approved_by = reviewer_id if next_state == "approved" else ""
            approved_at = now_utc_iso() if next_state == "approved" else ""
            self._execute(
                conn,
                """
                UPDATE progress_updates
                SET review_state=%s, review_note=%s, approved_by=%s, approved_at=%s, updated_at=%s
                WHERE update_id=%s
                """,
                (next_state, note, approved_by, approved_at, now_utc_iso(), update_id),
            )
            self._insert_review_audit(
                conn,
                update_id=update_id,
                action_name=action_name,
                reviewer_id=reviewer_id,
                note=note,
                patch=patch,
            )
            conn.commit()
            fresh = self._fetchone(
                conn,
                "SELECT * FROM progress_updates WHERE update_id=%s LIMIT 1",
                (update_id,),
            )
        return fresh or {}

    def _insert_review_audit(
        self,
        conn,
        *,
        update_id: str,
        action_name: str,
        reviewer_id: str,
        note: str,
        patch: dict[str, Any],
    ) -> None:
        self._execute(
            conn,
            """
            INSERT INTO review_audits(
                update_id, action_name, reviewer_id, note, patch_json, created_at
            )
            VALUES(%s, %s, %s, %s, %s, %s)
            """,
            (
                update_id,
                action_name,
                reviewer_id,
                note,
                json.dumps(patch, ensure_ascii=False),
                now_utc_iso(),
            ),
        )

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            if self._db_kind == "postgres":
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS member_identity_map (
                        map_id BIGSERIAL PRIMARY KEY,
                        member_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        platform_user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform, platform_user_id)
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS work_events (
                        event_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        message_text TEXT NOT NULL,
                        event_time TEXT NOT NULL,
                        event_epoch DOUBLE PRECISION NOT NULL,
                        raw_payload TEXT NOT NULL,
                        ingest_status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS progress_updates (
                        update_id TEXT PRIMARY KEY,
                        member_id TEXT NOT NULL,
                        task_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress_pct INTEGER NOT NULL,
                        blocker TEXT NOT NULL,
                        next_step TEXT NOT NULL,
                        deadline_date TEXT NOT NULL,
                        confidence DOUBLE PRECISION NOT NULL,
                        source_event_id TEXT NOT NULL,
                        source_event_time TEXT NOT NULL,
                        source_event_epoch DOUBLE PRECISION NOT NULL,
                        review_state TEXT NOT NULL,
                        review_note TEXT NOT NULL,
                        approved_by TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS review_audits (
                        audit_id BIGSERIAL PRIMARY KEY,
                        update_id TEXT NOT NULL,
                        action_name TEXT NOT NULL,
                        reviewer_id TEXT NOT NULL,
                        note TEXT NOT NULL,
                        patch_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """,
                )
            else:
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS member_identity_map (
                        map_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        member_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        platform_user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform, platform_user_id)
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS work_events (
                        event_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        message_text TEXT NOT NULL,
                        event_time TEXT NOT NULL,
                        event_epoch REAL NOT NULL,
                        raw_payload TEXT NOT NULL,
                        ingest_status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS progress_updates (
                        update_id TEXT PRIMARY KEY,
                        member_id TEXT NOT NULL,
                        task_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress_pct INTEGER NOT NULL,
                        blocker TEXT NOT NULL,
                        next_step TEXT NOT NULL,
                        deadline_date TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        source_event_id TEXT NOT NULL,
                        source_event_time TEXT NOT NULL,
                        source_event_epoch REAL NOT NULL,
                        review_state TEXT NOT NULL,
                        review_note TEXT NOT NULL,
                        approved_by TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """,
                )
                self._execute(
                    conn,
                    """
                    CREATE TABLE IF NOT EXISTS review_audits (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        update_id TEXT NOT NULL,
                        action_name TEXT NOT NULL,
                        reviewer_id TEXT NOT NULL,
                        note TEXT NOT NULL,
                        patch_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """,
                )

            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_work_events_lookup ON work_events(platform, channel_id, sender_id, event_epoch DESC)",
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_progress_updates_review ON progress_updates(review_state, source_event_epoch DESC)",
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_progress_updates_member ON progress_updates(member_id, source_event_epoch DESC)",
            )
            self._execute(
                conn,
                "CREATE INDEX IF NOT EXISTS idx_review_audits_update ON review_audits(update_id, created_at DESC)",
            )
            conn.commit()

    def _resolve_database_target(self, raw_url: str) -> tuple[str, str]:
        url = str(raw_url or "").strip()
        if not url:
            return "sqlite", str(self.settings.default_sqlite_path)
        lowered = url.lower()
        if lowered.startswith("sqlite:///"):
            return "sqlite", url[10:]
        if lowered.startswith("postgres://") or lowered.startswith("postgresql://"):
            if psycopg is None:
                raise RuntimeError("WORK_PROGRESS_DATABASE_URL dang dung PostgreSQL nhung chua co psycopg.")
            return "postgres", url
        raise ValueError("WORK_PROGRESS_DATABASE_URL chi ho tro sqlite:///... hoac postgresql://...")

    def _connect(self):
        if self._db_kind == "postgres":
            assert psycopg is not None
            assert dict_row is not None
            return psycopg.connect(self._db_target, row_factory=dict_row)
        conn = sqlite3.connect(self._db_target)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, conn, sql: str, params: tuple[Any, ...] | None = None):
        bound_params = params or tuple()
        if self._db_kind == "sqlite":
            sql = sql.replace("%s", "?")
        return conn.execute(sql, bound_params)

    def _fetchone(self, conn, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        cursor = self._execute(conn, sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        if isinstance(row, dict):
            return row
        if hasattr(row, "_asdict"):
            return row._asdict()
        return dict(row)

    def _fetchall(self, conn, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        cursor = self._execute(conn, sql, params)
        rows = cursor.fetchall() or []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, sqlite3.Row):
                normalized.append(dict(row))
            elif isinstance(row, dict):
                normalized.append(row)
            elif hasattr(row, "_asdict"):
                normalized.append(row._asdict())
            else:
                normalized.append(dict(row))
        return normalized


def _normalize_platform(value: Any) -> str:
    token = _clean_text(value).lower().replace("_", "-")
    mapping = {
        "telegram": "telegram",
        "zalo": "zalo",
        "pancake-work": "pancake-work",
        "pancake": "pancake-work",
        "forwarded": "forwarded",
    }
    return mapping.get(token, token or "unknown")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_status(value: Any) -> str:
    token = _clean_text(value).lower()
    mapping = {
        "done": "done",
        "hoan thanh": "done",
        "xong": "done",
        "blocked": "blocked",
        "vuong": "blocked",
        "doing": "doing",
        "dang lam": "doing",
        "todo": "todo",
        "to do": "todo",
        "chua lam": "todo",
    }
    return mapping.get(token, "todo")


def _normalize_event_time(raw_value: Any) -> tuple[str, float]:
    text = _clean_text(raw_value)
    if text:
        numeric = _try_parse_epoch_value(text)
        if numeric is not None:
            dt = datetime.fromtimestamp(numeric, tz=timezone.utc)
            return dt.isoformat(), float(numeric)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return dt.isoformat(), dt.timestamp()
        except ValueError:
            pass
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


def _try_parse_epoch_value(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    # Milliseconds epoch in most webhook systems.
    if value > 10_000_000_000:
        value = value / 1000.0
    # Basic sanity range ~ year 2001..2286 in seconds.
    if value < 978307200 or value > 9_999_999_999:
        return None
    return float(value)


def _local_date_from_epoch(epoch: float, *, tz_name: str) -> date:
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    return dt.date()


def _normalize_date_token(value: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    try:
        if "-" in raw:
            return date.fromisoformat(raw).isoformat()
        parts = [part.strip() for part in raw.split("/") if part.strip()]
        if len(parts) == 2:
            day = int(parts[0])
            month = int(parts[1])
            year = datetime.now().year
            return date(year, month, day).isoformat()
        if len(parts) == 3:
            day = int(parts[0])
            month = int(parts[1])
            year = int(parts[2])
            if year < 100:
                year += 2000
            return date(year, month, day).isoformat()
    except (TypeError, ValueError):
        return ""
    return ""


def _resolve_period(report_type: str, anchor_date: date, timezone_name: str) -> tuple[datetime, datetime, datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    if report_type == "daily":
        start_date = anchor_date
        end_date = anchor_date + timedelta(days=1)
        prev_start = start_date - timedelta(days=1)
        prev_end = start_date
    elif report_type == "weekly":
        start_date = anchor_date - timedelta(days=anchor_date.weekday())
        end_date = start_date + timedelta(days=7)
        prev_start = start_date - timedelta(days=7)
        prev_end = start_date
    else:
        start_date = anchor_date.replace(day=1)
        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(start_date.year, start_date.month + 1, 1)
        if start_date.month == 1:
            prev_start = date(start_date.year - 1, 12, 1)
        else:
            prev_start = date(start_date.year, start_date.month - 1, 1)
        prev_end = start_date
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    prev_start_dt = datetime.combine(prev_start, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    prev_end_dt = datetime.combine(prev_end, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    return start_dt, end_dt, prev_start_dt, prev_end_dt
