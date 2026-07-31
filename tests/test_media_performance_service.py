from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from typing import Any

from app.media_performance_service import (
    MEDIA_PERFORMANCE_SHEET_HEADERS,
    MEDIA_PERFORMANCE_SHEET_KEYS,
    MediaPerformanceService,
)
from app.models import MediaPerformanceCommand
from app.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage" / "ads2",
        logs_root=tmp_path / "logs" / "ads2",
        state_root=tmp_path / "state" / "ads2",
        config_root=tmp_path / "config",
        telegram_bot_token="dummy",
        telegram_allowed_user_id=1,
        meta_access_token="dummy",
        meta_page_access_token="page_dummy",
        meta_ad_account_id="act_894644562928775",
        meta_page_id="649228828282105",
        meta_api_version="v21.0",
        app_timezone="Asia/Ho_Chi_Minh",
        app_currency="VND",
        retry_max=3,
        retry_backoff_seconds=[1, 2, 3],
        token_healthcheck_enabled=False,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=False,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="",
        pancake_access_token="pancake",
        pancake_shop_id=222,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
        media_analytics_enabled=True,
        media_analytics_auto_enabled=True,
        media_analytics_history_days=7,
        media_analytics_min_spend_vnd=50000,
    )


def _settings_with_sheet(tmp_path: Path) -> Settings:
    return _settings(tmp_path).__class__(
        **{
            **_settings(tmp_path).__dict__,
            "media_analytics_sheet_enabled": True,
            "media_analytics_sheet_spreadsheet_id": "sheet_123",
            "media_analytics_sheet_gid": 424378234,
            "media_analytics_sheet_oauth_client_id": "client",
            "media_analytics_sheet_oauth_client_secret": "secret",
            "media_analytics_sheet_oauth_refresh_token": "refresh",
        }
    )


class FakeMeta:
    ad_account_id = "act_894644562928775"

    def get_ad_insights_for_range(self, start_date: date, end_date: date, timezone_name: str) -> list[dict[str, Any]]:
        assert start_date.isoformat() == "2026-07-01"
        assert end_date.isoformat() == "2026-07-07"
        assert timezone_name == "Asia/Ho_Chi_Minh"
        return [
            {
                "campaign_id": "camp_1",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|VXV011|Codex",
                "adset_id": "adset_1",
                "adset_name": "Thời trang",
                "ad_id": "ad_1",
                "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                "spend": "100000",
                "impressions": "1000",
                "clicks": "50",
                "actions": [
                    {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "10"},
                    {"action_type": "onsite_conversion.total_messaging_connection", "value": "12"},
                    {"action_type": "onsite_conversion.messaging_first_reply", "value": "9"},
                    {"action_type": "omni_purchase", "value": "2"},
                    {"action_type": "video_view", "value": "500"},
                    {"action_type": "post_reaction", "value": "7"},
                ],
                "action_values": [
                    {"action_type": "omni_purchase", "value": "500000"},
                ],
            },
            {
                "campaign_id": "camp_1",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|VXV011|Codex",
                "adset_id": "adset_2",
                "adset_name": "Du lịch",
                "ad_id": "ad_2",
                "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                "spend": "50000",
                "impressions": "500",
                "clicks": "10",
                "actions": [
                    {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "1"},
                    {"action_type": "onsite_conversion.total_messaging_connection", "value": "2"},
                    {"action_type": "post_reaction", "value": "3"},
                ],
            },
            {
                "campaign_id": "camp_2",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|Codex",
                "adset_id": "adset_3",
                "adset_name": "Tiệc",
                "ad_id": "ad_3",
                "ad_name": "Media không có mã trong tên",
                "spend": "80000",
                "impressions": "1000",
                "clicks": "0",
                "actions": [
                    {"action_type": "omni_purchase", "value": "1"},
                    {"action_type": "post_reaction", "value": "2"},
                ],
                "action_values": [
                    {"action_type": "omni_purchase", "value": "300000"},
                ],
            },
        ]

    def get_ads_metadata(self, ad_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert set(ad_ids) == {"ad_1", "ad_2", "ad_3"}
        return {
            "ad_1": {
                "ad_id": "ad_1",
                "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                "adset_id": "adset_1",
                "adset_name": "Thời trang",
                "campaign_id": "camp_1",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|VXV011|Codex",
                "creative_id": "creative_1",
                "object_story_id": "649228828282105_111",
                "effective_object_story_id": "649228828282105_111",
            },
            "ad_2": {
                "ad_id": "ad_2",
                "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                "adset_id": "adset_2",
                "adset_name": "Du lịch",
                "campaign_id": "camp_1",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|VXV011|Codex",
                "creative_id": "creative_1",
                "object_story_id": "649228828282105_111",
                "effective_object_story_id": "649228828282105_111",
            },
            "ad_3": {
                "ad_id": "ad_3",
                "ad_name": "Media không có mã trong tên",
                "adset_id": "adset_3",
                "adset_name": "Tiệc",
                "campaign_id": "camp_2",
                "campaign_name": "ADS:QUYET|MK:ThaiLan|Codex",
                "creative_id": "creative_2",
                "object_story_id": "649228828282105_222",
                "effective_object_story_id": "649228828282105_222",
            },
        }

    def get_post_metadata_for_story_ids(self, story_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert set(story_ids) == {"649228828282105_111", "649228828282105_222"}
        return {
            "649228828282105_111": {
                "message": "Velour test #VXV011",
                "permalink_url": "https://www.facebook.com/p/111",
            },
            "649228828282105_222": {
                "message": "Caption chỉ có #VXV012",
                "permalink_url": "https://www.facebook.com/p/222",
            },
        }


class FakePancake:
    def is_configured(self) -> bool:
        return True

    def fetch_orders_snapshot_for_range(
        self,
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> dict[str, Any]:
        assert start_date.isoformat() == "2026-07-01"
        assert end_date.isoformat() == "2026-07-07"
        assert timezone_name == "Asia/Ho_Chi_Minh"
        return {
            "orders": [
                {
                    "id": "order_1",
                    "order_currency": "THB",
                    "total_price": 20000,
                    "items": [
                        {
                            "quantity": 2,
                            "variation_info": {
                                "name": "VXV011 Velvet Dress",
                                "retail_price": 10000,
                            },
                        }
                    ],
                },
                {
                    "id": "order_2",
                    "order_currency": "THB",
                    "total_price": 15000,
                    "items": [
                        {
                            "quantity": 1,
                            "variation_info": {
                                "name": "VXV012 Muse Set",
                                "retail_price": 15000,
                            },
                        }
                    ],
                },
            ]
        }


class FakeMultiCodeMeta:
    ad_account_id = "act_894644562928775"

    def get_ad_insights_for_range(self, start_date: date, end_date: date, timezone_name: str) -> list[dict[str, Any]]:
        assert start_date.isoformat() == "2026-07-12"
        assert end_date.isoformat() == "2026-07-18"
        assert timezone_name == "Asia/Ho_Chi_Minh"
        return [
            {
                "campaign_id": "camp_combo",
                "campaign_name": "ADS:QUYET|VXV002_VXV001_VXV004|Codex",
                "adset_id": "adset_combo_1",
                "adset_name": "Combo 1",
                "ad_id": "ad_combo_1",
                "ad_name": "ADS:QUYET|SKU:VXV004_VXV002_VXV001|MED:Video",
                "spend": "100000",
                "impressions": "1000",
                "clicks": "40",
                "actions": [
                    {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "5"},
                    {"action_type": "video_view", "value": "300"},
                ],
            },
            {
                "campaign_id": "camp_combo",
                "campaign_name": "ADS:QUYET|VXV001_VXV002_VXV004|Codex",
                "adset_id": "adset_combo_2",
                "adset_name": "Combo 2",
                "ad_id": "ad_combo_2",
                "ad_name": "ADS:QUYET|SKU:VXV001_VXV002_VXV004|MED:Video",
                "spend": "50000",
                "impressions": "500",
                "clicks": "10",
                "actions": [
                    {"action_type": "onsite_conversion.messaging_conversation_started_7d", "value": "1"},
                    {"action_type": "video_view", "value": "100"},
                ],
            },
        ]

    def get_ads_metadata(self, ad_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert set(ad_ids) == {"ad_combo_1", "ad_combo_2"}
        return {
            ad_id: {
                "ad_id": ad_id,
                "campaign_id": "camp_combo",
                "campaign_name": "ADS:QUYET|VXV001_VXV002_VXV004|Codex",
                "creative_id": "creative_combo",
                "object_story_id": "649228828282105_4236666629977775",
                "effective_object_story_id": "649228828282105_4236666629977775",
            }
            for ad_id in ad_ids
        }

    def get_post_metadata_for_story_ids(self, story_ids: list[str]) -> dict[str, dict[str, Any]]:
        assert story_ids == ["649228828282105_4236666629977775"]
        return {
            "649228828282105_4236666629977775": {
                "message": "Combo media #VXV001 #VXV002 #VXV004",
                "permalink_url": "https://www.facebook.com/reel/4236666629977775/",
            }
        }


class FakeMultiCodePancake:
    def is_configured(self) -> bool:
        return True

    def fetch_orders_snapshot_for_range(
        self,
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> dict[str, Any]:
        assert start_date.isoformat() == "2026-07-12"
        assert end_date.isoformat() == "2026-07-18"
        assert timezone_name == "Asia/Ho_Chi_Minh"
        return {
            "orders": [
                {
                    "id": "order_vxv001",
                    "order_currency": "THB",
                    "total_price": 10000,
                    "items": [{"quantity": 1, "variation_info": {"name": "VXV001", "retail_price": 10000}}],
                },
                {
                    "id": "order_vxv002",
                    "order_currency": "THB",
                    "total_price": 20000,
                    "items": [{"quantity": 1, "variation_info": {"name": "VXV002", "retail_price": 20000}}],
                },
                {
                    "id": "order_vxv004",
                    "order_currency": "THB",
                    "total_price": 30000,
                    "items": [{"quantity": 1, "variation_info": {"name": "VXV004", "retail_price": 30000}}],
                },
            ]
        }


def test_generate_report_groups_by_code_then_media_and_adds_meta_revenue(tmp_path: Path) -> None:
    service = MediaPerformanceService(
        settings=_settings(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )

    report = service.generate_report(
        MediaPerformanceCommand(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 7),
            days=7,
        )
    )

    assert report["ok"] is True
    assert report["summary"]["code_count"] == 2
    codes = {item["code"]: item for item in report["codes"]}
    assert codes["VXV011"]["totals"]["spend_vnd"] == 150000
    assert codes["VXV011"]["totals"]["messages"] == 11
    assert codes["VXV011"]["totals"]["reactions"] == 10
    assert codes["VXV011"]["totals"]["order_count"] == 2
    assert codes["VXV011"]["revenue"]["revenue_vnd"] == 500000
    assert codes["VXV011"]["revenue"]["order_count"] == 2
    assert codes["VXV011"]["revenue"]["source"] == "meta_insights"
    assert codes["VXV011"]["media_count"] == 1
    assert codes["VXV011"]["media"][0]["totals"]["reactions"] == 10
    assert codes["VXV011"]["media"][0]["totals"]["revenue_vnd"] == 500000
    assert codes["VXV011"]["media"][0]["roas"] == 3.33
    assert len(codes["VXV011"]["media"][0]["ads"]) == 2
    assert codes["VXV012"]["totals"]["spend_vnd"] == 80000
    assert codes["VXV012"]["totals"]["reactions"] == 2
    assert codes["VXV012"]["revenue"]["revenue_vnd"] == 300000

    messages = service.build_messages(report)
    assert "Mã VXV011" in "\n".join(messages)
    assert "Mã VXV012" in "\n".join(messages)


def test_generate_report_filters_requested_code(tmp_path: Path) -> None:
    service = MediaPerformanceService(
        settings=_settings(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )

    report = service.generate_report(
        MediaPerformanceCommand(
            codes=["VXV011"],
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 7),
            days=7,
        )
    )

    assert [item["code"] for item in report["codes"]] == ["VXV011"]
    assert report["summary"]["code_count"] == 1


def test_generate_report_keeps_multi_code_media_as_one_combo_sku(tmp_path: Path) -> None:
    service = MediaPerformanceService(
        settings=_settings(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMultiCodeMeta(),  # type: ignore[arg-type]
        pancake_client=FakeMultiCodePancake(),  # type: ignore[arg-type]
    )

    report = service.generate_report(
        MediaPerformanceCommand(
            codes=["VXV002"],
            start_date=date(2026, 7, 12),
            end_date=date(2026, 7, 18),
            days=7,
        )
    )

    assert report["ok"] is True
    assert [item["code"] for item in report["codes"]] == ["VXV001_VXV002_VXV004"]
    assert report["warnings"] == []
    code = report["codes"][0]
    assert code["codes"] == ["VXV001", "VXV002", "VXV004"]
    assert code["totals"]["spend_vnd"] == 150000
    assert code["totals"]["messages"] == 6
    assert code["multi_code_ad_count"] == 2
    assert code["revenue"]["source"] == "pancake_fallback"
    assert code["revenue"]["revenue_vnd"] == 489000
    assert code["revenue"]["order_count"] == 3
    assert code["roas"] == 3.26
    assert code["media_count"] == 1
    media = code["media"][0]
    assert media["media_key"] == "649228828282105_4236666629977775"
    assert media["permalink_url"] == "https://www.facebook.com/reel/4236666629977775/"
    assert media["multi_code"] is True
    assert media["totals"]["spend_vnd"] == 150000
    assert len(media["ads"]) == 2

    rows = service._report_to_sheet_rows(report)

    assert len(rows) == 1
    assert rows[0]["code"] == "VXV001_VXV002_VXV004"
    assert rows[0]["dedupe_key"] == (
        "2026-07-12:2026-07-18:VXV001_VXV002_VXV004:media:649228828282105_4236666629977775"
    )


def test_sync_report_to_sheet_upserts_media_rows_only(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    report = {
        "generated_at": "2026-07-07T10:00:00+07:00",
        "start_date": "2026-07-01",
        "end_date": "2026-07-07",
        "codes": [
            {
                "code": "VXV011",
                "totals": {
                    "spend_vnd": 150000,
                    "messages": 11,
                    "views": 500,
                    "reactions": 17,
                    "clicks": 60,
                    "ctr": 4.0,
                    "cpc_vnd": 2500,
                    "cost_per_message_vnd": 13636,
                    "impressions": 1500,
                    "reach": 1200,
                },
                "revenue": {"revenue_vnd": 163000, "order_count": 1},
                "roas": 1.09,
                "multi_code_ad_count": 0,
                "media": [
                    {
                        "media_key": "649228828282105_111",
                        "story_id": "649228828282105_111",
                        "permalink_url": "https://www.facebook.com/p/111",
                        "label": "SCALE",
                        "score": 84.5,
                        "roas": 1.09,
                        "multi_code": False,
                        "totals": {
                            "spend_vnd": 150000,
                            "messages": 11,
                            "views": 500,
                            "reactions": 17,
                            "clicks": 60,
                            "ctr": 4.0,
                            "cpc_vnd": 2500,
                            "cost_per_message_vnd": 13636,
                            "impressions": 1500,
                            "reach": 1200,
                            "order_count": 1,
                            "revenue_vnd": 163000,
                        },
                        "ads": [
                            {
                                "ad_id": "ad_1",
                                "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                                "adset_id": "adset_1",
                                "adset_name": "Thời trang",
                                "campaign_id": "camp_1",
                                "campaign_name": "ADS:QUYET|MK:ThaiLan|VXV011|Codex",
                                "metrics": {
                                    "spend_vnd": 100000,
                                    "messages": 10,
                                    "views": 500,
                                    "reactions": 7,
                                    "clicks": 50,
                                    "ctr": 5.0,
                                    "cpc_vnd": 2000,
                                    "cost_per_message_vnd": 10000,
                                    "impressions": 1000,
                                    "reach": 900,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }
    appends: list[list[Any]] = []

    monkeypatch.setattr(service, "_refresh_sheet_access_token", lambda: "token")
    monkeypatch.setattr(service, "_resolve_sheet_title", lambda **_: "Media")
    monkeypatch.setattr(service, "_ensure_sheet_header", lambda **_: None)
    monkeypatch.setattr(service, "_format_sheet_header", lambda **_: None)
    monkeypatch.setattr(service, "_format_sheet_data_rows", lambda **_: None)
    monkeypatch.setattr(service, "_delete_stale_sheet_rows_for_period", lambda **_: 3)
    monkeypatch.setattr(
        service,
        "_load_existing_sheet_map",
        lambda **_: {"2026-07-01:2026-07-07:VXV011:media:649228828282105_111": 2},
    )
    def capture_updates(*, updates: list[tuple[int, list[Any]]], **_: Any) -> None:
        for item in updates:
            updates_capture.append(item)

    updates_capture: list[tuple[int, list[Any]]] = []
    monkeypatch.setattr(service, "_batch_update_sheet_rows", capture_updates)
    monkeypatch.setattr(service, "_append_sheet_rows", lambda *, rows, **_: appends.extend(rows))

    result = service.sync_report_to_sheet(report)

    assert result["ok"] is True
    assert result["updated"] == 1
    assert result["inserted"] == 0
    assert result["deleted_stale_ad_rows"] == 3
    assert result["deleted_stale_rows"] == 3
    assert len(updates_capture) == 1
    assert len(appends) == 0
    updated_values = updates_capture[0][1]
    assert len(updated_values) == len(MEDIA_PERFORMANCE_SHEET_KEYS)
    assert str(updated_values[3]).strip().lower() == "media"
    assert updated_values[6] == "https://www.facebook.com/p/111"
    assert updated_values[9:23] == [
        150000,
        11,
        13636,
        1,
        150000,
        163000,
        1.09,
        17,
        1200,
        500,
        60,
        2500,
        4.0,
        100000,
    ]
    assert result["sheet_url"].endswith("gid=424378234#gid=424378234")


def test_sheet_headers_are_bilingual_uppercase_and_metric_ordered() -> None:
    assert len(MEDIA_PERFORMANCE_SHEET_HEADERS) == len(MEDIA_PERFORMANCE_SHEET_KEYS)
    assert MEDIA_PERFORMANCE_SHEET_HEADERS[:9] == [
        "START_DATE / TỪ NGÀY",
        "END_DATE / ĐẾN NGÀY",
        "CODE / MÃ VXV",
        "LEVEL / CẤP",
        "MEDIA_KEY / MÃ MEDIA",
        "STORY_ID / ID BÀI",
        "PERMALINK_URL / LINK BÀI",
        "LABEL / NHÃN",
        "SCORE / ĐIỂM",
    ]
    assert MEDIA_PERFORMANCE_SHEET_HEADERS[9:] == [
        "SPEND / CHI PHÍ ADS",
        "MESS / TIN NHẮN",
        "AVG_MESS_COST / GIÁ MESS TB",
        "ORDER_COUNT / SỐ LƯỢNG ĐƠN",
        "COST_PER_ORDER / CHI PHÍ TB/ĐƠN",
        "REVENUE / DOANH THU",
        "ROAS / TỶ SUẤT DOANH THU",
        "REACTIONS / THẢ CẢM XÚC",
        "REACH / TIẾP CẬN",
        "VIEWS / LƯỢT XEM",
        "CLICKS / LƯỢT CLICK",
        "CPC / CHI PHÍ CLICK",
        "CTR / TỶ LỆ CLICK",
        "CPM / CHI PHÍ 1.000 HIỂN THỊ",
    ]
    assert MEDIA_PERFORMANCE_SHEET_KEYS[:9] == [
        "start_date",
        "end_date",
        "code",
        "level",
        "media_key",
        "story_id",
        "permalink_url",
        "label",
        "score",
    ]
    assert MEDIA_PERFORMANCE_SHEET_KEYS[9:] == [
        "spend_vnd",
        "messages",
        "cost_per_message_vnd",
        "order_count",
        "cost_per_order_vnd",
        "revenue_vnd",
        "roas",
        "reactions",
        "reach",
        "views",
        "clicks",
        "cpc_vnd",
        "ctr",
        "cpm_vnd",
    ]


def test_build_messages_includes_sheet_sync_link(tmp_path: Path) -> None:
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    report = {
        "ok": True,
        "start_date": "2026-07-01",
        "end_date": "2026-07-07",
        "summary": {"code_count": 0, "media_count": 0, "ad_count": 0},
        "codes": [],
        "warnings": [],
        "errors": {},
        "sheet_sync": {
            "enabled": True,
            "ok": True,
            "sheet_url": "https://docs.google.com/spreadsheets/d/sheet_123/edit?gid=424378234#gid=424378234",
            "inserted": 2,
            "updated": 1,
        },
    }

    text = "\n".join(service.build_messages(report))

    assert "Google Sheet:" in text
    assert "sheet_123" in text


def test_ensure_sheet_header_deletes_legacy_metadata_and_ad_columns(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    old_header = [
        "dedupe_key",
        "generated_at",
        "start_date",
        "end_date",
        "code",
        "level",
        "media_key",
        "story_id",
        "permalink_url",
        "label",
        "score",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "spend_vnd",
    ]
    batch_payloads: list[dict[str, Any]] = []
    updated_ranges: list[str] = []

    monkeypatch.setattr(service, "_sheet_values_get", lambda **_: {"values": [old_header]})

    def capture_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "POST"
        assert url.endswith(":batchUpdate")
        batch_payloads.append(kwargs.get("data", {}))
        return {}

    def capture_update(*, write_range: str, **_: Any) -> None:
        updated_ranges.append(write_range)

    monkeypatch.setattr(service, "_sheet_request_json", capture_request)
    monkeypatch.setattr(service, "_sheet_values_update", capture_update)

    service._ensure_sheet_header(spreadsheet_id="sheet_123", sheet_title="Media", headers={})

    assert batch_payloads
    metadata_delete_range = batch_payloads[0]["requests"][0]["deleteDimension"]["range"]
    assert metadata_delete_range["dimension"] == "COLUMNS"
    assert metadata_delete_range["startIndex"] == 0
    assert metadata_delete_range["endIndex"] == 2
    ad_delete_range = batch_payloads[1]["requests"][0]["deleteDimension"]["range"]
    assert ad_delete_range["dimension"] == "COLUMNS"
    assert ad_delete_range["startIndex"] == 9
    assert ad_delete_range["endIndex"] == 15
    assert updated_ranges == ["'Media'!A1:W1"]


def test_ensure_sheet_header_inserts_reactions_column_for_legacy_metrics(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    old_header = [
        header for header in MEDIA_PERFORMANCE_SHEET_HEADERS if header != "REACTIONS / THẢ CẢM XÚC"
    ]
    batch_payloads: list[dict[str, Any]] = []
    updated_ranges: list[str] = []

    monkeypatch.setattr(service, "_sheet_values_get", lambda **_: {"values": [old_header]})
    monkeypatch.setattr(service, "_sheet_request_json", lambda *_, **kwargs: batch_payloads.append(kwargs["data"]) or {})
    monkeypatch.setattr(service, "_sheet_values_update", lambda *, write_range, **_: updated_ranges.append(write_range))

    service._ensure_sheet_header(spreadsheet_id="sheet_123", sheet_title="Media", headers={})

    insert_range = batch_payloads[0]["requests"][0]["insertDimension"]["range"]
    assert insert_range["dimension"] == "COLUMNS"
    assert insert_range["startIndex"] == 16
    assert insert_range["endIndex"] == 17
    assert updated_ranges == ["'Media'!A1:W1"]


def test_ensure_sheet_header_repairs_shifted_reactions_data(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    shifted_row = [
        "2026-07-19",
        "2026-07-25",
        "VXV011",
        "media",
        "649228828282105_111",
        "649228828282105_111",
        "https://www.facebook.com/reel/1",
        "SCALE",
        "93",
        "1.215.973 đ",
        "9",
        "135.108 đ",
        "4",
        "303.993 đ",
        "2.679.313 đ",
        "2,2",
        "2031",
        "3946",
        "171",
        "7.111 đ",
        "5,68",
        "459.172 đ",
    ]
    batch_payloads: list[dict[str, Any]] = []
    updated_ranges: list[str] = []

    def fake_values_get(*, read_range: str, **_: Any) -> dict[str, Any]:
        if "A1:AE1" in read_range:
            return {"values": [MEDIA_PERFORMANCE_SHEET_HEADERS]}
        return {"values": [shifted_row]}

    monkeypatch.setattr(service, "_sheet_values_get", fake_values_get)
    monkeypatch.setattr(service, "_sheet_request_json", lambda *_, **kwargs: batch_payloads.append(kwargs["data"]) or {})
    monkeypatch.setattr(service, "_sheet_values_update", lambda *, write_range, **_: updated_ranges.append(write_range))

    service._ensure_sheet_header(spreadsheet_id="sheet_123", sheet_title="Media", headers={})

    insert_range = batch_payloads[0]["requests"][0]["insertDimension"]["range"]
    assert insert_range["startIndex"] == 16
    assert insert_range["endIndex"] == 17
    trailing_delete_range = batch_payloads[1]["requests"][0]["deleteDimension"]["range"]
    assert trailing_delete_range["startIndex"] == 23
    assert trailing_delete_range["endIndex"] == 24
    assert updated_ranges == ["'Media'!A1:W1"]


def test_format_sheet_header_bolds_display_row(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    payloads: list[dict[str, Any]] = []

    def capture_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "POST"
        assert url.endswith(":batchUpdate")
        payloads.append(kwargs.get("data", {}))
        return {}

    monkeypatch.setattr(service, "_sheet_request_json", capture_request)

    service._format_sheet_header(spreadsheet_id="sheet_123", headers={})

    repeat_cell = payloads[0]["requests"][0]["repeatCell"]
    assert repeat_cell["range"]["endColumnIndex"] == len(MEDIA_PERFORMANCE_SHEET_HEADERS)
    assert repeat_cell["cell"]["userEnteredFormat"]["textFormat"]["bold"] is True


def test_load_existing_sheet_map_rebuilds_key_from_visible_columns(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        service,
        "_sheet_values_get",
        lambda **_: {
            "values": [
                ["2026-07-01", "2026-07-07", "VXV011", "code", ""],
                ["2026-07-01", "2026-07-07", "VXV011", "media", "649228828282105_111"],
            ]
        },
    )

    result = service._load_existing_sheet_map(spreadsheet_id="sheet_123", sheet_title="Media", headers={})

    assert result == {
        "2026-07-01:2026-07-07:VXV011:code": 2,
        "2026-07-01:2026-07-07:VXV011:media:649228828282105_111": 3,
    }


def test_delete_stale_sheet_rows_removes_split_multi_code_media(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    deleted_start_indexes: list[int] = []

    monkeypatch.setattr(
        service,
        "_sheet_values_get",
        lambda **_: {
            "values": [
                ["2026-07-12", "2026-07-18", "VXV001_VXV002_VXV004", "media", "649228828282105_4236666629977775"],
                ["2026-07-12", "2026-07-18", "VXV001", "media", "649228828282105_4236666629977775"],
                ["2026-07-12", "2026-07-18", "VXV002", "media", "649228828282105_4236666629977775"],
                ["2026-07-12", "2026-07-18", "VXV001", "code", ""],
                ["2026-07-12", "2026-07-18", "VXV001", "ad", "ad_1"],
                ["2026-07-19", "2026-07-25", "VXV001", "media", "649228828282105_4236666629977775"],
            ]
        },
    )

    def capture_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "POST"
        assert url.endswith(":batchUpdate")
        for request in kwargs["data"]["requests"]:
            deleted_start_indexes.append(request["deleteDimension"]["range"]["startIndex"])
        return {}

    monkeypatch.setattr(service, "_sheet_request_json", capture_request)

    deleted_count = service._delete_stale_sheet_rows_for_period(
        spreadsheet_id="sheet_123",
        sheet_title="Media",
        headers={},
        start_date="2026-07-12",
        end_date="2026-07-18",
        expected_dedupe_keys={
            "2026-07-12:2026-07-18:VXV001_VXV002_VXV004:media:649228828282105_4236666629977775"
        },
        delete_missing_media_rows=True,
    )

    assert deleted_count == 4
    assert deleted_start_indexes == [5, 4, 3, 2]


def test_empty_media_bucket_falls_back_to_story_permalink(tmp_path: Path) -> None:
    service = MediaPerformanceService(
        settings=_settings(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )

    bucket = service._empty_media_bucket(
        media_key="649228828282105_122179615958934207",
        story_id="649228828282105_122179615958934207",
        post_meta={},
    )

    assert bucket["permalink_url"] == (
        "https://www.facebook.com/permalink.php?story_fbid=122179615958934207&id=649228828282105"
    )


def test_format_sheet_data_rows_alternates_weekly_period_background(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service = MediaPerformanceService(
        settings=_settings_with_sheet(tmp_path),
        logger=logging.getLogger("test"),
        meta_client=FakeMeta(),  # type: ignore[arg-type]
        pancake_client=FakePancake(),  # type: ignore[arg-type]
    )
    payloads: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service,
        "_sheet_values_get",
        lambda **_: {
            "values": [
                ["2026-07-01", "2026-07-07"],
                ["2026-07-01", "2026-07-07"],
                ["2026-07-08", "2026-07-14"],
                ["2026-07-08", "2026-07-14"],
                ["2026-07-15", "2026-07-21"],
            ]
        },
    )

    def capture_request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "POST"
        assert url.endswith(":batchUpdate")
        payloads.append(kwargs.get("data", {}))
        return {}

    monkeypatch.setattr(service, "_sheet_request_json", capture_request)

    service._format_sheet_data_rows(
        spreadsheet_id="sheet_123",
        sheet_title="Media",
        headers={},
        row_numbers=[],
        start_date="",
        end_date="",
    )

    requests_payload = payloads[0]["requests"]
    assert [item["repeatCell"]["range"]["startRowIndex"] for item in requests_payload] == [1, 3, 5]
    assert [item["repeatCell"]["range"]["endRowIndex"] for item in requests_payload] == [3, 5, 6]
    assert requests_payload[0]["repeatCell"]["range"]["endColumnIndex"] == len(MEDIA_PERFORMANCE_SHEET_HEADERS)
    assert requests_payload[0]["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"] == {
        "red": 1.0,
        "green": 1.0,
        "blue": 1.0,
    }
    assert requests_payload[1]["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"] == {
        "red": 252 / 255,
        "green": 229 / 255,
        "blue": 205 / 255,
    }
    assert requests_payload[2]["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"] == {
        "red": 1.0,
        "green": 1.0,
        "blue": 1.0,
    }
