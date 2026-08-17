from __future__ import annotations

from pathlib import Path

from app.settings import load_settings


def _write_required_config(root: Path, profile: str = "") -> None:
    config_dir = root / "config" / profile if profile else root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("audiences.json", "objective.json", "message_templates.json"):
        (config_dir / name).write_text("{}", encoding="utf-8")


def _set_default_required_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "1")
    monkeypatch.setenv("META_ACCESS_TOKEN", "meta")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_1")
    monkeypatch.setenv("META_PAGE_ID", "page_1")


def test_load_settings_accepts_default_instagram_token_alias(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _write_required_config(tmp_path)
    _set_default_required_env(monkeypatch)
    monkeypatch.delenv("META_CREATIVE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_IG_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("META_INSTAGRAM_ACCESS_TOKEN", "ig_user_token")

    settings = load_settings(project_root=tmp_path)

    assert settings.meta_creative_access_token == "ig_user_token"


def test_load_settings_accepts_profile_instagram_token_alias(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _write_required_config(tmp_path, "ads2")
    _set_default_required_env(monkeypatch)
    monkeypatch.setenv("ADS2_TELEGRAM_BOT_TOKEN", "telegram_ads2")
    monkeypatch.setenv("ADS2_META_ACCESS_TOKEN", "meta_ads2")
    monkeypatch.setenv("ADS2_META_PAGE_ACCESS_TOKEN", "page_token_ads2")
    monkeypatch.setenv("ADS2_META_AD_ACCOUNT_ID", "act_2")
    monkeypatch.setenv("ADS2_META_PAGE_ID", "page_2")
    monkeypatch.delenv("ADS2_META_CREATIVE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ADS2_META_IG_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("ADS2_META_INSTAGRAM_ACCESS_TOKEN", "ig_user_token_ads2")

    settings = load_settings(project_root=tmp_path, profile="ads2")

    assert settings.meta_creative_access_token == "ig_user_token_ads2"


def test_focused_worker_settings_do_not_require_telegram_or_meta(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _write_required_config(tmp_path)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "META_ACCESS_TOKEN",
        "META_AD_ACCOUNT_ID",
        "META_PAGE_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "pancake")
    monkeypatch.setenv("PANCAKE_SHOP_ID", "123")

    settings = load_settings(project_root=tmp_path, require_app_credentials=False)

    assert settings.telegram_bot_token == ""
    assert settings.telegram_allowed_user_id == 0
    assert settings.pancake_access_token == "pancake"
    assert settings.pancake_shop_id == 123


def test_ads2_daily_report_uses_profile_pancake_and_chat_fallback(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _write_required_config(tmp_path, "ads2")
    _set_default_required_env(monkeypatch)
    monkeypatch.setenv("ADS2_TELEGRAM_BOT_TOKEN", "telegram_ads2")
    monkeypatch.setenv("ADS2_META_ACCESS_TOKEN", "meta_ads2")
    monkeypatch.setenv("ADS2_META_PAGE_ACCESS_TOKEN", "page_token_ads2")
    monkeypatch.setenv("ADS2_META_AD_ACCOUNT_ID", "act_2")
    monkeypatch.setenv("ADS2_META_PAGE_ID", "page_2")
    monkeypatch.setenv("DAILY_REPORT_NOTIFY_CHAT_ID", "-5153224852")
    monkeypatch.delenv("ADS2_DAILY_REPORT_NOTIFY_CHAT_ID", raising=False)
    monkeypatch.setenv("PANCAKE_ACCESS_TOKEN", "pancake_main")
    monkeypatch.setenv("PANCAKE_SHOP_ID", "111")
    monkeypatch.setenv("ADS2_PANCAKE_ACCESS_TOKEN", "pancake_ads2")
    monkeypatch.setenv("ADS2_PANCAKE_SHOP_ID", "222")

    settings = load_settings(project_root=tmp_path, profile="ads2")

    assert settings.profile == "ads2"
    assert settings.daily_report_enabled is True
    assert settings.daily_report_notify_chat_id == -5153224852
    assert settings.pancake_access_token == "pancake_ads2"
    assert settings.pancake_shop_id == 222


def test_ads2_media_analytics_notify_defaults_to_private_user(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _write_required_config(tmp_path, "ads2")
    _set_default_required_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("ADS2_TELEGRAM_BOT_TOKEN", "telegram_ads2")
    monkeypatch.setenv("ADS2_META_ACCESS_TOKEN", "meta_ads2")
    monkeypatch.setenv("ADS2_META_PAGE_ACCESS_TOKEN", "page_token_ads2")
    monkeypatch.setenv("ADS2_META_AD_ACCOUNT_ID", "act_2")
    monkeypatch.setenv("ADS2_META_PAGE_ID", "page_2")
    monkeypatch.setenv("DAILY_REPORT_NOTIFY_CHAT_ID", "-5153224852")
    monkeypatch.delenv("ADS2_MEDIA_ANALYTICS_NOTIFY_CHAT_ID", raising=False)

    settings = load_settings(project_root=tmp_path, profile="ads2")

    assert settings.media_analytics_enabled is True
    assert settings.media_analytics_notify_chat_id == 12345
