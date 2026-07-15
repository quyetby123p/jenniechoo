from __future__ import annotations

from app.scheduled_tasks import build_parser


def test_scheduled_parser_accepts_profile_for_daily_report() -> None:
    args = build_parser().parse_args(["--profile", "ads2", "daily-report", "--slot", "evening"])

    assert args.profile == "ads2"
    assert args.task == "daily-report"
    assert args.slot == "evening"
