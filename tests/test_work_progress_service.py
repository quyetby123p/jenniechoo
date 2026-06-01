from __future__ import annotations

from datetime import date
import logging
from pathlib import Path

from app.work_progress_service import WorkProgressService
from app.work_progress_settings import WorkProgressSettings


def _settings(tmp_path: Path) -> WorkProgressSettings:
    return WorkProgressSettings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        state_root=tmp_path / "state",
        database_url=f"sqlite:///{(tmp_path / 'storage' / 'work_progress' / 'progress.db').as_posix()}",
        api_host="127.0.0.1",
        api_port=8099,
        timezone_name="Asia/Ho_Chi_Minh",
        confidence_fast_track=0.9,
        context_window_minutes=30,
        manager_telegram_user_ids=[1],
        telegram_bot_token="test",
        daily_report_hour=21,
        daily_report_minute=0,
        daily_report_offset_days=0,
        weekly_report_weekday=5,
        weekly_report_hour=15,
        weekly_report_minute=0,
        monthly_report_day=1,
        monthly_report_hour=9,
        monthly_report_minute=0,
        telegram_allowlist_channel_ids=["-1001"],
        zalo_allowlist_channel_ids=["zalo-team-1"],
        pancake_allowlist_channel_ids=["pancake-team-1"],
        forwarded_allowlist_channel_ids=["forward-room"],
    )


def _logger() -> logging.Logger:
    return logging.getLogger("test_work_progress_service")


def test_ingest_requires_identity_mapping(tmp_path: Path) -> None:
    service = WorkProgressService(settings=_settings(tmp_path), logger=_logger())
    result = service.ingest_event(
        "telegram",
        {
            "event_id": "m1",
            "channel_id": "-1001",
            "sender_id": "u1",
            "message_text": "task: Bao cao tuan #BC01 dang lam 50%",
            "event_time": "2026-05-28T10:00:00+07:00",
        },
    )
    assert result["ok"] is True
    assert result["ingest_status"] == "pending_identity_map"
    assert result["progress_update"] is None


def test_ingest_extract_review_and_report(tmp_path: Path) -> None:
    service = WorkProgressService(settings=_settings(tmp_path), logger=_logger())
    service.upsert_member_identity(
        member_id="thai",
        platform="telegram",
        platform_user_id="u1",
        display_name="Thai",
    )

    ingest_result = service.ingest_event(
        "telegram",
        {
            "event_id": "m2",
            "channel_id": "-1001",
            "sender_id": "u1",
            "message_text": "task: Bao cao tuan #BC01 dang lam 60% blocker: thieu token buoc tiep theo: xin cap quyen deadline: 2026-05-30",
            "event_time": "2026-05-28T11:00:00+07:00",
        },
    )
    assert ingest_result["ingest_status"] == "extracted"
    update = ingest_result["progress_update"]
    assert isinstance(update, dict)
    assert update["review_state"] in {"pending_fast", "pending_manual"}

    update_id = str(update["update_id"])
    approved = service.edit_update(
        update_id=update_id,
        reviewer_id="manager_1",
        patch={"status": "done", "progress_pct": 100, "next_step": "xac nhan ket qua"},
        note="review chot",
        approve_after_edit=True,
    )
    assert approved["review_state"] == "approved"
    assert approved["status"] == "done"
    assert int(approved["progress_pct"]) == 100

    report = service.build_report("daily", anchor_date=date(2026, 5, 28))
    assert report["ok"] is True
    assert report["report_type"] == "daily"
    assert len(report["members"]) == 1
    member = report["members"][0]
    assert member["member_id"] == "thai"
    assert "BC01" in "".join(member["done_tasks"])


def test_ingest_dedup_and_allowlist(tmp_path: Path) -> None:
    service = WorkProgressService(settings=_settings(tmp_path), logger=_logger())
    service.upsert_member_identity(
        member_id="linh",
        platform="telegram",
        platform_user_id="u2",
    )
    payload = {
        "event_id": "msg-dup",
        "channel_id": "-1001",
        "sender_id": "u2",
        "message_text": "task: check QA #QA01 done 100%",
        "event_time": "2026-05-28T14:00:00+07:00",
    }
    first = service.ingest_event("telegram", payload)
    second = service.ingest_event("telegram", payload)
    assert first["deduped"] is False
    assert second["deduped"] is True

    blocked_channel = service.ingest_event(
        "telegram",
        {
            "event_id": "msg-deny",
            "channel_id": "-9999",
            "sender_id": "u2",
            "message_text": "task: not allowlisted #NO01 doing 20%",
            "event_time": "2026-05-28T14:05:00+07:00",
        },
    )
    assert blocked_channel["ingest_status"] == "ignored_not_allowlisted"


def test_approve_and_reject_flow(tmp_path: Path) -> None:
    service = WorkProgressService(settings=_settings(tmp_path), logger=_logger())
    service.upsert_member_identity(member_id="huy", platform="zalo", platform_user_id="z1")
    ingest = service.ingest_event(
        "zalo",
        {
            "event_id": "zalo-1",
            "channel_id": "zalo-team-1",
            "sender_id": "z1",
            "message_text": "task: doi soat COD #COD99 dang lam 40%",
            "event_time": "2026-05-28T12:00:00+07:00",
        },
    )
    update_id = str(ingest["progress_update"]["update_id"])
    pending = service.list_pending_updates(limit=10)
    assert any(str(item.get("update_id")) == update_id for item in pending)

    rejected = service.reject_update(update_id=update_id, reviewer_id="manager_2", note="noi dung chua ro")
    assert rejected["review_state"] == "rejected"
