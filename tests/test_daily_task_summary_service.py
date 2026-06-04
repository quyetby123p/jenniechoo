from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sqlite3

from app.utils import dump_json
from app.daily_task_summary_service import DailyTaskSummaryService
from app.settings import Settings


def _dummy_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        telegram_bot_token="dummy",
        telegram_allowed_user_id=1,
        meta_access_token="dummy",
        meta_page_access_token="page_dummy",
        meta_ad_account_id="act_1",
        meta_page_id="61581440236157",
        meta_api_version="v21.0",
        app_timezone="Asia/Ho_Chi_Minh",
        app_currency="VND",
        retry_max=3,
        retry_backoff_seconds=[1, 2, 3],
        token_healthcheck_enabled=True,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=True,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="api_key_dummy",
        pancake_access_token="",
        pancake_shop_id=123,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


def _create_task_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
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
            CREATE TABLE task_updates (
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
        rows = [
            (
                "task_done",
                "Chốt landing page",
                "chot landing page",
                "",
                "manager",
                1,
                1,
                -1,
                "done",
                100,
                "",
                "",
                "2026-06-04",
                "2026-06-04T02:00:00+00:00",
                "2026-06-04T13:30:00+00:00",
                "2026-06-04T13:30:00+00:00",
            ),
            (
                "task_doing",
                "Tối ưu báo cáo cuối ngày",
                "toi uu bao cao cuoi ngay",
                "",
                "manager",
                1,
                1,
                -1,
                "doing",
                60,
                "",
                "thêm task summary vào báo cáo tối",
                "2026-06-05",
                "2026-06-04T03:00:00+00:00",
                "2026-06-04T12:00:00+00:00",
                "",
            ),
            (
                "task_blocked",
                "Kết nối sheet vận hành",
                "ket noi sheet van hanh",
                "",
                "manager",
                1,
                1,
                -1,
                "blocked",
                20,
                "thiếu quyền sheet",
                "xin quyền editor",
                "",
                "2026-06-03T03:00:00+00:00",
                "2026-06-04T11:00:00+00:00",
                "",
            ),
            (
                "task_todo",
                "Kiểm tra ROAS tuần",
                "kiem tra roas tuan",
                "",
                "self",
                1,
                1,
                -1,
                "todo",
                0,
                "",
                "",
                "",
                "2026-06-03T03:00:00+00:00",
                "2026-06-03T03:00:00+00:00",
                "",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO tasks(
                task_uid, title, title_norm, description, source_type,
                created_by, assigned_by, group_chat_id, status, progress_percent,
                blocked_reason, next_step, deadline_date, created_at, updated_at, closed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            """
            INSERT INTO task_updates(
                task_uid, updated_by, chat_id, action_name, status_before, status_after,
                progress_before, progress_after, note, blocked_reason, next_step, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("task_done", 1, -1, "done", "doing", "done", 80, 100, "", "", "", "2026-06-04T13:30:00+00:00"),
                (
                    "task_doing",
                    1,
                    -1,
                    "update",
                    "todo",
                    "doing",
                    0,
                    60,
                    "",
                    "",
                    "thêm task summary vào báo cáo tối",
                    "2026-06-04T12:00:00+00:00",
                ),
            ],
        )
        conn.commit()


def test_daily_task_summary_counts_and_formats_message(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _create_task_db(settings.daily_report_task_db_file)
    dump_json(
        settings.project_root / "state" / "assistant_bot" / "daily_task_checkin_state.json",
        {"days": {"2026-06-04": {"task_uids": ["task_doing", "task_done"]}}},
    )
    service = DailyTaskSummaryService(settings=settings, logger=logging.getLogger("test"))

    summary = service.build_summary(date(2026, 6, 4))
    text = service.build_message(summary)

    assert summary["available"] is True
    assert summary["counts"] == {"todo": 1, "doing": 1, "blocked": 1, "done": 1}
    assert summary["created_today_count"] == 2
    assert summary["updated_today_count"] == 2
    assert summary["done_today_count"] == 1
    assert [item["task_uid"] for item in summary["checkin_today_items"]] == ["task_doing", "task_done"]
    assert "Task công việc cuối ngày:" in text
    assert "Tổng task: 4" in text
    assert "Công việc hôm nay:" in text
    assert "Tối ưu báo cáo cuối ngày" in text
    assert "Hoàn thành hôm nay:" in text
    assert "Chốt landing page" in text
    assert "[Blocked 20%] Kết nối sheet vận hành" in text
    assert "vướng: thiếu quyền sheet" in text


def test_daily_task_summary_handles_missing_db(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = DailyTaskSummaryService(settings=settings, logger=logging.getLogger("test"))

    summary = service.build_summary(date(2026, 6, 4))
    text = service.build_message(summary)

    assert summary["available"] is False
    assert "Chưa tìm thấy task DB." in text
