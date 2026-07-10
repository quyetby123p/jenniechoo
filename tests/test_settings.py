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
