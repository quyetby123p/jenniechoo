from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.scheduled_tasks import build_parser, run_media_performance


def test_scheduled_parser_accepts_profile_for_daily_report() -> None:
    args = build_parser().parse_args(["--profile", "ads2", "daily-report", "--slot", "evening"])

    assert args.profile == "ads2"
    assert args.task == "daily-report"
    assert args.slot == "evening"


def test_run_media_performance_sends_to_private_user_not_group_notify() -> None:
    sent_messages: list[tuple[int, str]] = []

    class FakeMediaPerformance:
        def generate_report(self, command):  # noqa: ANN001
            return {"command_days": command.days}

        def build_messages(self, report):  # noqa: ANN001
            return [f"report {report['command_days']}"]

    class FakeTelegram:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            sent_messages.append((chat_id, text))

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            media_analytics_enabled=True,
            media_analytics_auto_enabled=True,
            media_analytics_history_days=7,
            media_analytics_notify_chat_id=-5153224852,
            telegram_allowed_user_id=12345,
        ),
        media_performance=FakeMediaPerformance(),
        telegram=FakeTelegram(),
    )

    asyncio.run(run_media_performance(runtime, codes="", days=None, campaign=""))

    assert sent_messages == [(12345, "report 7")]
