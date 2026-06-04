from __future__ import annotations

from datetime import date
import logging
from types import SimpleNamespace

from app.cloud_schedule_guard import CloudScheduleGuardClient


def test_from_env_disables_guard_in_github_actions(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CLOUD_SCHEDULE_GUARD_MARK_URL", "https://worker.example/schedule/mark")
    monkeypatch.setenv("CLOUD_SCHEDULE_GUARD_SECRET", "secret")

    client = CloudScheduleGuardClient.from_env(logger=logging.getLogger("test"))

    assert client.enabled is False
    assert client.is_configured() is False


def test_mark_completed_posts_payload_and_dedupes(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_post(url, json, headers, timeout):  # noqa: ANN001
        calls.append({"url": url, "json": dict(json), "headers": dict(headers), "timeout": timeout})
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("app.cloud_schedule_guard.requests.post", fake_post)
    client = CloudScheduleGuardClient(
        mark_url="https://worker.example/schedule/mark",
        secret="secret",
        logger=logging.getLogger("test"),
    )

    assert client.mark_completed(task="daily-report", slot="morning", run_date=date(2026, 6, 4)) is True
    assert client.mark_completed(task="daily-report", slot="morning", run_date=date(2026, 6, 4)) is True

    assert len(calls) == 1
    assert calls[0]["url"] == "https://worker.example/schedule/mark"
    assert calls[0]["headers"]["X-Schedule-Guard-Secret"] == "secret"
    assert calls[0]["json"] == {
        "task": "daily-report",
        "source": "local",
        "slot": "morning",
        "run_date": "2026-06-04",
    }
