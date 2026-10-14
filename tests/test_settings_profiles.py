from __future__ import annotations

from pathlib import Path

import pytest

from app.exceptions import ConfigError
from app.settings import load_settings


def _write_required_profile_configs(project_root: Path, profile: str = "main") -> None:
    config_dir = project_root / "config"
    if profile not in {"", "main", "default"}:
        config_dir = config_dir / profile
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("audiences.json", "objective.json", "message_templates.json"):
        (config_dir / name).write_text("{}", encoding="utf-8")


def _set_common_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "main_bot")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    monkeypatch.setenv("META_ACCESS_TOKEN", "main_meta")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "main_page_meta")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_1")
    monkeypatch.setenv("META_PAGE_ID", "page_1")


def _set_ads2_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADS2_TELEGRAM_BOT_TOKEN", "ads2_bot")
    monkeypatch.setenv("ADS2_TELEGRAM_ALLOWED_USER_ID", "456")
    monkeypatch.setenv("ADS2_META_ACCESS_TOKEN", "ads2_meta")
    monkeypatch.setenv("ADS2_META_PAGE_ACCESS_TOKEN", "ads2_page_meta")
    monkeypatch.setenv("ADS2_META_AD_ACCOUNT_ID", "act_2")
    monkeypatch.setenv("ADS2_META_PAGE_ID", "page_2")


def test_load_settings_main_uses_legacy_pancake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_required_profile_configs(tmp_path)
    _set_common_env(monkeypatch)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "main_pancake_token")
    monkeypatch.setenv("PANCAKE_SHOP_ID", "111")
    monkeypatch.setenv("PANCAKE_TD_SYNC_ENABLED", "1")

    settings = load_settings(project_root=tmp_path, profile="main")

    assert settings.profile_name == "main"
    assert settings.pancake_access_token == "main_pancake_token"
    assert settings.pancake_shop_id == 111
    assert settings.pancake_td_sync_enabled is True
    assert settings.media_analytics_enabled is False


def test_load_settings_ads2_uses_prefixed_shop_and_enables_daily_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_required_profile_configs(tmp_path, "ads2")
    _set_common_env(monkeypatch)
    _set_ads2_required_env(monkeypatch)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "shared_pancake_token")
    monkeypatch.setenv("PANCAKE_SHOP_ID", "111")
    monkeypatch.setenv("DAILY_REPORT_ENABLED", "1")
    monkeypatch.setenv("DAILY_REPORT_NOTIFY_CHAT_ID", "-5153224852")
    monkeypatch.setenv("RECONCILE_COD_ENABLED", "1")
    monkeypatch.setenv("PANCAKE_TD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ADS2_PANCAKE_SHOP_ID", "222")
    monkeypatch.setenv("ADS2_PANCAKE_TD_SYNC_ENABLED", "1")

    settings = load_settings(project_root=tmp_path, profile="ads2")

    assert settings.profile_name == "ads2"
    assert settings.telegram_bot_token == "ads2_bot"
    assert settings.pancake_access_token == "shared_pancake_token"
    assert settings.pancake_shop_id == 222
    assert settings.pancake_td_sync_enabled is True
    assert settings.daily_report_enabled is True
    assert settings.daily_report_notify_chat_id == -5153224852
    assert settings.reconcile_cod_enabled is False
    assert settings.media_analytics_enabled is True
    assert settings.media_analytics_auto_enabled is True
    assert settings.media_analytics_history_days == 7
    assert settings.media_analytics_auto_hour == 16
    assert settings.media_analytics_auto_minute == 0
    assert settings.media_analytics_sheet_enabled is True
    assert settings.media_analytics_sheet_spreadsheet_id == "1-ywfhrjlnEAob2Cn3znpKhTA4buKyQZrI5XXjiRCAAo"
    assert settings.media_analytics_sheet_gid == 424378234
    assert settings.pancake_td_sync_state_path == "storage/ads2/pancake_td_sync/state.json"


def test_load_settings_ads2_daily_report_notify_chat_prefers_prefixed_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_required_profile_configs(tmp_path, "ads2")
    _set_common_env(monkeypatch)
    _set_ads2_required_env(monkeypatch)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "shared_pancake_token")
    monkeypatch.setenv("ADS2_PANCAKE_SHOP_ID", "222")
    monkeypatch.setenv("DAILY_REPORT_NOTIFY_CHAT_ID", "-5153224852")
    monkeypatch.setenv("ADS2_DAILY_REPORT_NOTIFY_CHAT_ID", "-100222333")

    settings = load_settings(project_root=tmp_path, profile="ads2")

    assert settings.daily_report_enabled is True
    assert settings.daily_report_notify_chat_id == -100222333


def test_load_settings_ads2_sync_requires_ads2_shop_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_required_profile_configs(tmp_path, "ads2")
    _set_common_env(monkeypatch)
    _set_ads2_required_env(monkeypatch)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "shared_pancake_token")
    monkeypatch.setenv("PANCAKE_SHOP_ID", "111")
    monkeypatch.setenv("ADS2_PANCAKE_TD_SYNC_ENABLED", "1")

    with pytest.raises(ConfigError, match="ADS2_PANCAKE_SHOP_ID"):
        load_settings(project_root=tmp_path, profile="ads2")
