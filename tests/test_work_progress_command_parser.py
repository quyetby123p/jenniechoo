from __future__ import annotations

from datetime import date

import pytest

from app.exceptions import CommandParseError
from app.work_progress_command_parser import is_work_progress_command, parse_work_progress_command


def test_detect_progress_command() -> None:
    assert is_work_progress_command("/progress pending") is True
    assert is_work_progress_command("/media abc") is False


def test_parse_pending_command() -> None:
    cmd = parse_work_progress_command("/progress pending 15")
    assert cmd.action == "pending"
    assert cmd.args["limit"] == 15


def test_parse_unmapped_command() -> None:
    cmd = parse_work_progress_command("/progress unmapped 12")
    assert cmd.action == "unmapped"
    assert cmd.args["limit"] == 12


def test_parse_report_command_with_date() -> None:
    cmd = parse_work_progress_command("/progress report weekly 2026-05-28")
    assert cmd.action == "report"
    assert cmd.args["report_type"] == "weekly"
    assert cmd.args["anchor_date"] == date(2026, 5, 28)


def test_parse_map_command() -> None:
    cmd = parse_work_progress_command("/progress map thai | telegram | 123456 | Thai Team")
    assert cmd.action == "map"
    assert cmd.args["member_id"] == "thai"
    assert cmd.args["platform"] == "telegram"
    assert cmd.args["platform_user_id"] == "123456"


def test_parse_edit_command() -> None:
    cmd = parse_work_progress_command("/progress edit wupd_1 | doing | 55 | thieu quyen | xin quyen | 2026-06-02")
    assert cmd.action == "edit"
    patch = cmd.args["patch"]
    assert patch["status"] == "doing"
    assert patch["progress_pct"] == 55
    assert patch["blocker"] == "thieu quyen"


def test_parse_invalid_report_type() -> None:
    with pytest.raises(CommandParseError):
        parse_work_progress_command("/progress report quarter")
