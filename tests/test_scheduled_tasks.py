from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from app.scheduled_tasks import (
    build_parser,
    run_bot3_task_weekly_summary,
    run_media_performance,
    run_work_progress_report,
)


def test_scheduled_parser_accepts_profile_for_daily_report() -> None:
    args = build_parser().parse_args(["--profile", "ads2", "daily-report", "--slot", "evening"])

    assert args.profile == "ads2"
    assert args.task == "daily-report"
    assert args.slot == "evening"


def test_scheduled_parser_accepts_bot3_weekly_summary_date() -> None:
    args = build_parser().parse_args(["bot3-task-weekly-summary", "--date", "2026-06-06"])

    assert args.task == "bot3-task-weekly-summary"
    assert args.date == "2026-06-06"


def test_scheduled_parser_accepts_work_progress_weekly_date() -> None:
    args = build_parser().parse_args(["work-progress-weekly", "--date", "2026-06-06"])

    assert args.task == "work-progress-weekly"
    assert args.date == "2026-06-06"


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


def test_run_bot3_task_weekly_summary_sends_to_task_group_once() -> None:
    sent_messages: list[tuple[int, str]] = []
    marked: list[tuple[str, str]] = []

    class FakeScheduler:
        def should_send_day_mark(self, mark: str, day_key: str) -> bool:
            return (mark, day_key) not in marked

        def mark_day_sent(self, mark: str, day_key: str) -> None:
            marked.append((mark, day_key))

    class FakeTasks:
        def build_weekly_snapshot(self, **kwargs):  # noqa: ANN003, ANN201
            return {"week_start": str(kwargs["reference_date"]), "week_end": str(kwargs["reference_date"])}

    class FakeBot:
        scheduler = FakeScheduler()
        tasks = FakeTasks()

        def _build_task_weekly_reply(self, *, snapshot, trigger_label):  # noqa: ANN001, ANN201
            return f"{trigger_label}: {snapshot['week_start']}"

        async def _bot_send_message(self, chat_id: int, text: str) -> None:
            sent_messages.append((chat_id, text))

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            tasks_enabled=True,
            task_weekly_summary_enabled=True,
            task_group_chat_id=-5153224852,
            task_weekly_summary_weekday=5,
            task_weekly_summary_max_items=5,
            timezone_name="Asia/Ho_Chi_Minh",
        ),
        bot=FakeBot(),
    )

    asyncio.run(run_bot3_task_weekly_summary(runtime, run_date=date(2026, 6, 6)))
    asyncio.run(run_bot3_task_weekly_summary(runtime, run_date=date(2026, 6, 6)))

    assert sent_messages == [(-5153224852, "Tổng kết tuần tự động: 2026-06-06")]
    assert marked == [("task_weekly_summary", "2026-06-06")]


def test_run_work_progress_weekly_sends_once() -> None:
    sent_messages: list[str] = []
    saved_states: list[dict[str, str]] = []

    class FakeWorkProgress:
        def build_report(self, report_type: str, *, anchor_date: date):  # noqa: ANN201
            return {"type": report_type, "date": anchor_date.isoformat()}

        def format_report_text(self, report):  # noqa: ANN001, ANN201
            return f"{report['type']} {report['date']}"

    class FakeScheduler:
        settings = SimpleNamespace(
            daily_report_offset_days=0,
            weekly_report_weekday=5,
            monthly_report_day=1,
        )

        def __init__(self) -> None:
            self.state: dict[str, str] = {}

        def _load_state(self):  # noqa: ANN202
            return self.state

        def _should_send(self, state, *, slot_name: str, day_key: str) -> bool:  # noqa: ANN001
            return state.get(slot_name) != day_key

        def _send_private_to_managers(self, text: str) -> None:
            sent_messages.append(text)

        def _mark_sent(self, state, *, slot_name: str, day_key: str) -> None:  # noqa: ANN001
            state[slot_name] = day_key

        def _save_state(self, state) -> None:  # noqa: ANN001
            saved_states.append(dict(state))

    runtime = SimpleNamespace(
        settings=SimpleNamespace(work_progress_enabled=True),
        work_progress=FakeWorkProgress(),
        work_progress_scheduler=FakeScheduler(),
    )

    asyncio.run(run_work_progress_report(runtime, report_type="weekly", run_date=date(2026, 6, 6)))
    asyncio.run(run_work_progress_report(runtime, report_type="weekly", run_date=date(2026, 6, 6)))

    assert sent_messages == ["weekly 2026-06-06"]
    assert saved_states == [{"weekly": "2026-06-06"}]
