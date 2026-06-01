import pytest

from app.exceptions import CommandParseError
from app.media_command_parser import is_media_command_text, parse_media_caption


def test_parse_media_caption_success_with_keywords() -> None:
    cmd = parse_media_caption("/media SKU123 vay hoa nu")
    assert cmd.product_code == "SKU123"
    assert cmd.keyword_text == "vay hoa nu"


def test_parse_media_caption_success_with_bot_mention() -> None:
    cmd = parse_media_caption("/media@my_bot jc-001")
    assert cmd.product_code == "JC-001"
    assert cmd.keyword_text == ""


def test_parse_media_caption_natural_minimal() -> None:
    cmd = parse_media_caption("Tìm media")
    assert cmd.product_code == ""
    assert cmd.keyword_text == ""


def test_parse_media_caption_natural_with_product_code() -> None:
    cmd = parse_media_caption("Tìm media jc123 vay hoa")
    assert cmd.product_code == "JC123"
    assert cmd.keyword_text == "vay hoa"


def test_parse_media_caption_natural_with_labelled_product_code() -> None:
    cmd = parse_media_caption("Tìm media mã jc-888 váy dự tiệc")
    assert cmd.product_code == "JC-888"
    assert cmd.keyword_text == "váy dự tiệc"


def test_parse_media_caption_invalid_format() -> None:
    with pytest.raises(CommandParseError):
        parse_media_caption("hello bot")


def test_parse_media_caption_invalid_product_code() -> None:
    with pytest.raises(CommandParseError):
        parse_media_caption("/media a")


def test_is_media_command_text() -> None:
    assert is_media_command_text("/media SKU123") is True
    assert is_media_command_text("Tìm media") is True
    assert is_media_command_text("tim media jc123") is True
    assert is_media_command_text("hello") is False
