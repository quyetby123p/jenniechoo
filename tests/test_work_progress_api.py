from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
from urllib.request import Request, urlopen

from app.work_progress_api import WorkProgressApiServer
from app.work_progress_service import WorkProgressService
from app.work_progress_settings import WorkProgressSettings


def _settings(tmp_path: Path, *, port: int) -> WorkProgressSettings:
    return WorkProgressSettings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        state_root=tmp_path / "state",
        database_url=f"sqlite:///{(tmp_path / 'storage' / 'work_progress' / 'progress.db').as_posix()}",
        api_host="127.0.0.1",
        api_port=port,
        timezone_name="Asia/Ho_Chi_Minh",
        confidence_fast_track=0.7,
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
    return logging.getLogger("test_work_progress_api")


def test_api_ingest_review_and_report(tmp_path: Path) -> None:
    settings = _settings(tmp_path, port=18199)
    service = WorkProgressService(settings=settings, logger=_logger())
    service.upsert_member_identity(member_id="thai", platform="telegram", platform_user_id="u1")
    api = WorkProgressApiServer(service=service, host=settings.api_host, port=settings.api_port, logger=_logger())

    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        ingest_payload = {
            "event_id": "api-1",
            "channel_id": "-1001",
            "sender_id": "u1",
            "message_text": "task: tong hop so lieu #BCAPI dang lam 70%",
            "event_time": "2026-05-28T10:00:00+07:00",
        }
        ingest_result = _post_json("http://127.0.0.1:18199/ingest/telegram", ingest_payload)
        assert ingest_result["ok"] is True
        update_id = str(ingest_result["progress_update"]["update_id"])

        approve_result = _post_json(
            f"http://127.0.0.1:18199/review/{update_id}/approve",
            {"reviewer_id": "manager_api", "note": "ok"},
        )
        assert approve_result["ok"] is True
        assert approve_result["item"]["review_state"] == "approved"

        daily = _get_json("http://127.0.0.1:18199/reports/daily?date=2026-05-28")
        assert daily["ok"] is True
        assert daily["report_type"] == "daily"
        assert len(daily["members"]) >= 1
    finally:
        api.shutdown()
        thread.join(timeout=2)


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    req = Request(url, method="GET")
    with urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))

