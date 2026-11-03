import pytest

from app.ads_sku_catalog import AdsSkuCatalogError, build_snapshot, extract_catalog_codes
from app.campaign_planner import build_campaign_plan
from app.command_parser import parse_ads_command
from app.models import AdsCommand, ResolvedPost


def _ruza_objective() -> dict:
    return {
        "campaign_objective": "OUTCOME_ENGAGEMENT",
        "conversion_location": "MESSAGING_DESTINATION",
        "result_goal": "MAXIMIZE_PURCHASES_VIA_MESSAGE",
        "message_template_name": "Chào Ruza",
        "sku_prefix": "RUZA",
        "market_name": "Thailand",
        "campaign_name_suffix": "",
        "sku_catalog": build_snapshot(
            ["KVF273", "QLF191", "ADB485.C2608"],
            spreadsheet_id="sheet",
            sheet_name="Sản phẩm và giá",
        ),
        "sku_catalog_ignore_unknown": True,
        "campaign_payload_overrides": {},
        "adset_payload_overrides": {},
        "creative_payload_overrides": {},
        "ad_payload_overrides": {},
    }


def _ruza_audiences() -> dict:
    return {
        "slots": [
            {"key": "office", "label": "Công sở", "suffix": "CS", "saved_audience_id": "aud-cs"},
            {"key": "fashion", "label": "Thời trang", "suffix": "TT", "saved_audience_id": "aud-tt"},
        ]
    }


def _ruza_templates() -> dict:
    return {
        "templates": {
            "Chào Ruza": {
                "creative_patch": {
                    "page_welcome_message": {
                        "template_id": "1789046865760268",
                        "template_version": 0,
                    }
                },
                "ad_patch": {},
                "adset_patch": {},
            }
        }
    }


def test_ruza_catalog_normalizes_suffix_and_deduplicates() -> None:
    snapshot = build_snapshot(
        ["ADB485.C2608", "ADB485", "VBC016 "],
        spreadsheet_id="sheet",
        sheet_name="Sản phẩm và giá",
    )
    assert snapshot["sku_codes"] == ["ADB485", "VBC016"]
    assert extract_catalog_codes("#ADB485.C2608 KVF273_QLF191 ADB485", ("ADB485", "KVF273", "QLF191")) == [
        "ADB485",
        "KVF273",
        "QLF191",
    ]


def test_ruza_catalog_rejects_unknown_sku() -> None:
    with pytest.raises(AdsSkuCatalogError, match="không có trong catalog"):
        extract_catalog_codes("#NOT999", ("ADB485",), strict=True)


def test_ruza_campaign_has_two_audiences_and_expected_names() -> None:
    command = AdsCommand(
        post_url="https://www.facebook.com/ruza.th/posts/1",
        budget_daily_vnd=300000,
    )
    resolved = ResolvedPost(
        post_id="1",
        page_id="ruza-page",
        permalink_url="https://www.facebook.com/ruza.th/posts/1",
        object_story_id="ruza-page_1",
        strategy="test",
        message_text="Set mới #KVF273_QLF191",
        media_label="Video",
    )
    plan = build_campaign_plan(
        command=command,
        resolved_post=resolved,
        post_fingerprint="ruza-test",
        version=1,
        timezone_name="Asia/Bangkok",
        audiences_config=_ruza_audiences(),
        objective_config=_ruza_objective(),
        template_config=_ruza_templates(),
    )
    assert plan.campaign_name == "ADS:QUYET|MK:Thailand|KVF273_QLF191"
    assert [slot.label for slot in plan.audiences] == ["Công sở", "Thời trang"]
    assert [slot.adset_name for slot in plan.audiences] == [
        "ADS:QUYET|MK:Thailand|KVF273_QLF191 - Công sở",
        "ADS:QUYET|MK:Thailand|KVF273_QLF191 - Thời trang",
    ]
    assert all(
        slot.ad_name == "ADS:QUYET|MK:Thailand|SKU:KVF273_QLF191|MED:Video"
        for slot in plan.audiences
    )
    assert plan.raw["creative_payload_overrides"]["page_welcome_message"] == {
        "template_id": "1789046865760268",
        "template_version": 0,
    }


def test_ruza_campaign_uses_all_when_caption_has_no_known_sku() -> None:
    command = AdsCommand(post_url="https://www.facebook.com/ruza.th/posts/1", budget_daily_vnd=1)
    resolved = ResolvedPost(
        post_id="1",
        page_id="ruza-page",
        permalink_url="https://www.facebook.com/ruza.th/posts/1",
        object_story_id="ruza-page_1",
        strategy="test",
        message_text="Không có mã",
        media_label="Video",
    )
    plan = build_campaign_plan(
        command=command,
        resolved_post=resolved,
        post_fingerprint="ruza-test",
        version=1,
        timezone_name="Asia/Bangkok",
        audiences_config=_ruza_audiences(),
        objective_config=_ruza_objective(),
        template_config=_ruza_templates(),
    )
    assert plan.sku_code_text == "ALL"
    assert plan.campaign_name == "ADS:QUYET|MK:Thailand|ALL"
    assert all(
        slot.ad_name == "ADS:QUYET|MK:Thailand|SKU:ALL|MED:Video"
        for slot in plan.audiences
    )

    resolved.message_text = "#NOT999"
    plan = build_campaign_plan(
        command=command,
        resolved_post=resolved,
        post_fingerprint="ruza-test",
        version=1,
        timezone_name="Asia/Bangkok",
        audiences_config=_ruza_audiences(),
        objective_config=_ruza_objective(),
        template_config=_ruza_templates(),
    )
    assert plan.sku_code_text == "ALL"

    resolved.message_text = "#NOT999 #KVF273"
    plan = build_campaign_plan(
        command=command,
        resolved_post=resolved,
        post_fingerprint="ruza-test",
        version=1,
        timezone_name="Asia/Bangkok",
        audiences_config=_ruza_audiences(),
        objective_config=_ruza_objective(),
        template_config=_ruza_templates(),
    )
    assert plan.sku_code_text == "KVF273"
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:Thailand|SKU:KVF273|MED:Video"


def test_ruza_campaign_rejects_missing_saved_audience_id() -> None:
    command = AdsCommand(post_url="https://www.facebook.com/ruza.th/posts/1", budget_daily_vnd=1)
    resolved = ResolvedPost(
        post_id="1",
        page_id="ruza-page",
        permalink_url="https://www.facebook.com/ruza.th/posts/1",
        object_story_id="ruza-page_1",
        strategy="test",
        message_text="#KVF273",
        media_label="Video",
    )
    audiences = _ruza_audiences()
    audiences["slots"][1]["saved_audience_id"] = "replace_me"
    with pytest.raises(ValueError, match="Saved Audience ID"):
        build_campaign_plan(
            command=command,
            resolved_post=resolved,
            post_fingerprint="ruza-test",
            version=1,
            timezone_name="Asia/Bangkok",
            audiences_config=audiences,
            objective_config=_ruza_objective(),
            template_config=_ruza_templates(),
        )

def test_ruza_existing_command_parses_catalog_skus() -> None:
    command = parse_ads_command(
        "https://www.facebook.com/ruza.th/posts/1 KVF273_QLF191 lên cũ",
        allowed_sku_codes=("KVF273", "QLF191"),
    )
    assert command.use_existing_campaign is True
    assert command.manual_sku_keywords == ["KVF273", "QLF191"]


def test_ruza_new_command_accepts_catalog_sku_override() -> None:
    command = parse_ads_command(
        "https://www.facebook.com/ruza.th/posts/1 KVF273_QLF191 ngân sách 200000 lên mới",
        allowed_sku_codes=("KVF273", "QLF191"),
        allow_new_sku_override=True,
    )
    assert command.use_existing_campaign is False
    assert command.manual_sku_keywords == ["KVF273", "QLF191"]


def test_ruza_new_command_ignores_unknown_sku_override() -> None:
    command = parse_ads_command(
        "https://www.facebook.com/ruza.th/posts/1 RQF168 ADB452 ngân sách 200000 lên mới",
        allowed_sku_codes=("ADB452",),
        allow_new_sku_override=True,
        ignore_unknown_sku_codes=True,
    )
    assert command.manual_sku_keywords == ["ADB452"]


def test_ruza_new_plan_uses_explicit_sku_when_caption_is_empty() -> None:
    command = AdsCommand(
        post_url="https://www.facebook.com/ruza.th/posts/1",
        budget_daily_vnd=200000,
        manual_sku_keywords=["KVF273", "QLF191"],
    )
    resolved = ResolvedPost(
        post_id="1",
        page_id="ruza-page",
        permalink_url="https://www.facebook.com/ruza.th/posts/1",
        object_story_id="ruza-page_1",
        strategy="test",
        message_text="Caption khong co ma",
        media_label="Video",
    )
    plan = build_campaign_plan(
        command=command,
        resolved_post=resolved,
        post_fingerprint="ruza-override",
        version=1,
        timezone_name="Asia/Bangkok",
        audiences_config=_ruza_audiences(),
        objective_config=_ruza_objective(),
        template_config=_ruza_templates(),
    )
    assert plan.sku_code_text == "KVF273_QLF191"
