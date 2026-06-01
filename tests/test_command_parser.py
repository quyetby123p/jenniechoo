from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.command_parser import (
    parse_ads_command,
    parse_reconcile_cod_date_argument,
    parse_report_date_argument,
    try_parse_pancake_td_sync_command,
    try_parse_reconcile_cod_command,
    try_parse_report_command,
)
from app.exceptions import CommandParseError, ValidationError


def test_parse_ads_command_success_slash_ads() -> None:
    cmd = parse_ads_command(
        "/ads https://www.facebook.com/permalink.php?story_fbid=123&id=456 budget=300000 lên mới"
    )
    assert cmd.post_url.startswith("https://www.facebook.com")
    assert cmd.budget_daily_vnd == 300000


def test_parse_ads_command_success_plain_vietnamese() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 ngân sách 300000 lên mới"
    )
    assert cmd.post_url.startswith("https://www.facebook.com")
    assert cmd.budget_daily_vnd == 300000


def test_parse_ads_command_success_angle_bracket_and_newline() -> None:
    cmd = parse_ads_command(
        "/ads\n<https://www.facebook.com/permalink.php?story_fbid=123&id=456>\nbudget=<100000>\nlen moi"
    )
    assert cmd.post_url.startswith("https://www.facebook.com")
    assert cmd.budget_daily_vnd == 100000


def test_parse_ads_command_existing_mode_with_manual_sku() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 JCV140 JCA158 lên cũ"
    )
    assert cmd.use_existing_campaign is True
    assert cmd.manual_sku_keywords == ["JCV140", "JCA158"]
    assert cmd.existing_campaign_hint == ""
    assert cmd.budget_daily_vnd == 0


def test_parse_ads_command_existing_mode_without_manual_sku() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 lên cũ"
    )
    assert cmd.use_existing_campaign is True
    assert cmd.manual_sku_keywords == []
    assert cmd.existing_campaign_hint == ""
    assert cmd.budget_daily_vnd == 0


def test_parse_ads_command_existing_mode_still_accepts_budget_if_provided() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 ngân sách 300000 JCV140 lên cũ"
    )
    assert cmd.use_existing_campaign is True
    assert cmd.manual_sku_keywords == ["JCV140"]
    assert cmd.existing_campaign_hint == ""
    assert cmd.budget_daily_vnd == 300000


def test_parse_ads_command_existing_mode_with_campaign_hint() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 lên cũ camp video"
    )
    assert cmd.use_existing_campaign is True
    assert cmd.manual_sku_keywords == []
    assert cmd.existing_campaign_hint == "video"
    assert cmd.budget_daily_vnd == 0


def test_parse_ads_command_existing_mode_with_campaign_hint_and_sku() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 JCV140 lên cũ camp video"
    )
    assert cmd.use_existing_campaign is True
    assert cmd.manual_sku_keywords == ["JCV140"]
    assert cmd.existing_campaign_hint == "video"
    assert cmd.budget_daily_vnd == 0


def test_parse_ads_command_ignores_manual_sku_when_not_existing_mode() -> None:
    cmd = parse_ads_command(
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456 ngân sách 300000 JCV140 lên mới"
    )
    assert cmd.use_existing_campaign is False
    assert cmd.manual_sku_keywords == []


def test_parse_ads_command_wrong_format() -> None:
    with pytest.raises(CommandParseError):
        parse_ads_command("ngân sách 300000")


def test_parse_ads_command_requires_mode_flag() -> None:
    with pytest.raises(CommandParseError):
        parse_ads_command("https://www.facebook.com/permalink.php?story_fbid=123&id=456 ngân sách 300000")


def test_parse_ads_command_invalid_budget() -> None:
    with pytest.raises(ValidationError):
        parse_ads_command(
            "https://www.facebook.com/permalink.php?story_fbid=123&id=456 ngân sách 0 lên mới"
        )


def test_parse_ads_command_invalid_link() -> None:
    with pytest.raises(ValidationError):
        parse_ads_command("https://example.com/a-post ngân sách 200000 lên mới")


def test_parse_report_date_argument_empty() -> None:
    assert parse_report_date_argument("/report") is None


def test_parse_report_date_argument_success() -> None:
    parsed = parse_report_date_argument("/report 2026-05-15")
    assert parsed is not None
    assert parsed.isoformat() == "2026-05-15"


def test_parse_report_date_argument_invalid_format() -> None:
    with pytest.raises(CommandParseError):
        parse_report_date_argument("/report 15-05-2026")


def test_try_parse_report_command_plain_bao_cao() -> None:
    is_report, parsed = try_parse_report_command("Báo cáo", "Asia/Ho_Chi_Minh")
    assert is_report is True
    assert parsed is None


def test_try_parse_report_command_hom_qua() -> None:
    is_report, parsed = try_parse_report_command("báo cáo ngày hôm qua", "Asia/Ho_Chi_Minh")
    assert is_report is True
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)
    assert parsed == expected


def test_try_parse_report_command_hom_nay() -> None:
    is_report, parsed = try_parse_report_command("báo cáo hôm nay", "Asia/Ho_Chi_Minh")
    assert is_report is True
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    assert parsed == expected


def test_try_parse_report_command_day_month_without_year() -> None:
    is_report, parsed = try_parse_report_command("báo cáo 15/6", "Asia/Ho_Chi_Minh")
    assert is_report is True
    assert parsed is not None
    assert parsed.day == 15
    assert parsed.month == 6


def test_try_parse_report_command_not_report() -> None:
    is_report, parsed = try_parse_report_command("hello anh oi", "Asia/Ho_Chi_Minh")
    assert is_report is False
    assert parsed is None


def test_try_parse_report_command_invalid_phrase() -> None:
    with pytest.raises(CommandParseError):
        try_parse_report_command("báo cáo abcxyz", "Asia/Ho_Chi_Minh")


def test_parse_reconcile_cod_date_argument_empty_date() -> None:
    parsed = parse_reconcile_cod_date_argument("/reconcile cod", "Asia/Ho_Chi_Minh")
    assert parsed is None


def test_parse_reconcile_cod_date_argument_specific_date() -> None:
    parsed = parse_reconcile_cod_date_argument("/reconcile cod 2026-05-09", "Asia/Ho_Chi_Minh")
    assert parsed is not None
    assert parsed.isoformat() == "2026-05-09"


def test_parse_reconcile_cod_date_argument_supports_command_with_bot_mention() -> None:
    parsed = parse_reconcile_cod_date_argument("/reconcile@testbot cod 2026-05-09", "Asia/Ho_Chi_Minh")
    assert parsed is not None
    assert parsed.isoformat() == "2026-05-09"


def test_parse_reconcile_cod_date_argument_hom_qua() -> None:
    parsed = parse_reconcile_cod_date_argument("/reconcile cod hôm qua", "Asia/Ho_Chi_Minh")
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)
    assert parsed == expected


def test_try_parse_reconcile_cod_command_plain() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát cod", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    assert parsed is None


def test_try_parse_reconcile_command_plain_without_cod() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    assert parsed is None


def test_try_parse_reconcile_cod_command_hom_qua() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("doi soat cod hom qua", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)
    assert parsed == expected


def test_try_parse_reconcile_command_hom_qua_without_cod() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát hôm qua", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)
    assert parsed == expected


def test_try_parse_reconcile_command_hom_nay_without_cod() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát hôm nay", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    expected = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    assert parsed == expected


def test_try_parse_reconcile_cod_command_specific_date_natural_text() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát cod 2026-05-09", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    assert parsed == datetime(2026, 5, 9).date()


def test_try_parse_reconcile_cod_command_day_month_text() -> None:
    is_reconcile, parsed = try_parse_reconcile_cod_command("đối soát cod ngày 9/5", "Asia/Ho_Chi_Minh")
    assert is_reconcile is True
    assert parsed is not None
    assert parsed.day == 9
    assert parsed.month == 5


def test_try_parse_reconcile_cod_command_invalid_phrase() -> None:
    with pytest.raises(CommandParseError):
        try_parse_reconcile_cod_command("doi soat cod abcxyz", "Asia/Ho_Chi_Minh")


def test_try_parse_pancake_td_sync_command_hom_nay() -> None:
    assert try_parse_pancake_td_sync_command("lên đơn hôm nay") == (True, None)


def test_try_parse_pancake_td_sync_command_plain() -> None:
    assert try_parse_pancake_td_sync_command("len don") == (True, None)


def test_try_parse_pancake_td_sync_command_order_code() -> None:
    assert try_parse_pancake_td_sync_command("lên đơn JCT310") == (True, "JCT310")


def test_try_parse_pancake_td_sync_command_order_code_without_space() -> None:
    assert try_parse_pancake_td_sync_command("lên đơnJCT310") == (True, "JCT310")


def test_try_parse_pancake_td_sync_command_not_match() -> None:
    assert try_parse_pancake_td_sync_command("hello anh oi") == (False, None)


def test_try_parse_pancake_td_sync_command_invalid_phrase() -> None:
    with pytest.raises(CommandParseError):
        try_parse_pancake_td_sync_command("lên đơn tuần này")
