from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.assistant_command_parser import parse_assistant_command
from app.assistant_models import AssistantIntent
from app.exceptions import CommandParseError


def test_parse_agenda_natural_hom_nay() -> None:
    cmd = parse_assistant_command("lịch hôm nay", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.AGENDA
    assert cmd.date_value == datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def test_parse_plan_week() -> None:
    cmd = parse_assistant_command("kế hoạch tuần này", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.PLAN
    assert cmd.week_mode is True


def test_parse_result_hom_qua() -> None:
    cmd = parse_assistant_command("kết quả hôm qua", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.RESULT
    assert cmd.date_value == datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)


def test_parse_action_run_reconcile_cod() -> None:
    cmd = parse_assistant_command("/run reconcile cod 2026-05-19", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.ACTION
    assert cmd.action_name == "reconcile_cod_report"
    assert cmd.action_args["settlement_date"] == "2026-05-19"


def test_parse_action_run_sheet_requires_run_id() -> None:
    with pytest.raises(CommandParseError):
        parse_assistant_command("/run media sheet", "Asia/Ho_Chi_Minh")


def test_parse_general_qa_default() -> None:
    cmd = parse_assistant_command("hôm nay học tiếng anh sao cho nhanh", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.GENERAL_QA
    assert "tiếng anh" in cmd.question_text


def test_parse_ask_command() -> None:
    cmd = parse_assistant_command("/ask tóm tắt thị trường hôm nay", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.GENERAL_QA
    assert cmd.question_text == "tóm tắt thị trường hôm nay"


def test_parse_task_add_command() -> None:
    cmd = parse_assistant_command("/task add manager | Chốt báo cáo tuần | Ưu tiên cao", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.TASK
    assert cmd.task_action == "add"
    assert cmd.task_args["source_type"] == "manager"
    assert cmd.task_args["title"] == "Chốt báo cáo tuần"


def test_parse_task_update_command() -> None:
    cmd = parse_assistant_command(
        "/task update Chốt báo cáo tuần | doing | 60 | Đang chạy số liệu | thiếu data ads | lấy lại token",
        "Asia/Ho_Chi_Minh",
    )
    assert cmd.intent == AssistantIntent.TASK
    assert cmd.task_action == "update"
    assert cmd.task_args["title"] == "Chốt báo cáo tuần"
    assert cmd.task_args["progress_percent"] == 60


def test_parse_task_natural_week_query() -> None:
    cmd = parse_assistant_command("tổng kết tuần công việc", "Asia/Ho_Chi_Minh")
    assert cmd.intent == AssistantIntent.TASK
    assert cmd.task_action == "week"
