import pytest

from app.campaign_planner import (
    build_campaign_plan,
    build_existing_campaign_plan,
    build_non_jc_hashtag_suffix,
    extract_jc_codes,
    extract_non_jc_hashtags,
)
from app.models import AdsCommand, ResolvedPost


def _base_objective() -> dict:
    return {
        "campaign_objective": "ENGAGEMENT",
        "conversion_location": "MESSAGING_DESTINATION",
        "result_goal": "MAXIMIZE_PURCHASES_VIA_MESSAGE",
        "message_template_name": "Chào JC",
        "campaign_payload_overrides": {},
        "adset_payload_overrides": {},
        "creative_payload_overrides": {},
        "ad_payload_overrides": {},
    }


def _base_templates() -> dict:
    return {
        "templates": {
            "Chào JC": {
                "creative_patch": {},
                "ad_patch": {},
                "adset_patch": {},
            }
        }
    }


def _base_audiences() -> dict:
    return {
        "thoi_trang_saved_audience_id": "111",
        "du_lich_saved_audience_id": "222",
        "tiec_saved_audience_id": "333",
    }


def _resolved_post() -> ResolvedPost:
    return ResolvedPost(
        post_id="122133442311048007",
        page_id="853907927797012",
        permalink_url="https://www.facebook.com/122133633243048007/posts/122133442311048007",
        object_story_id="853907927797012_122133442311048007",
        strategy="direct_pageid_pfbid",
        message_text="Noi dung post #JCV238 #JCA158 #JCV238",
        media_label="Anh",
    )


def test_build_campaign_plan_success() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
    )
    plan = build_campaign_plan(
        command=cmd,
        resolved_post=_resolved_post(),
        post_fingerprint="abc123",
        version=2,
        timezone_name="Asia/Ho_Chi_Minh",
        audiences_config=_base_audiences(),
        objective_config=_base_objective(),
        template_config=_base_templates(),
    )
    assert plan.version == 2
    assert plan.campaign_name == "ADS:QUYET|MK:ThaiLan|JCV238_JCA158|Codex"
    assert plan.sku_code_text == "JCV238_JCA158"
    assert plan.media_label == "Anh"
    assert plan.audiences[0].adset_name.endswith(" - Thời trang")
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:ThaiLan|SKU:JCV238_JCA158|MED:Anh"
    assert len(plan.audiences) == 3


def test_build_campaign_plan_template_missing() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
    )
    objective = _base_objective()
    objective["message_template_name"] = "Khong ton tai"
    with pytest.raises(ValueError):
        build_campaign_plan(
            command=cmd,
            resolved_post=_resolved_post(),
            post_fingerprint="abc123",
            version=1,
            timezone_name="Asia/Ho_Chi_Minh",
            audiences_config=_base_audiences(),
            objective_config=objective,
            template_config=_base_templates(),
        )


def test_build_campaign_plan_missing_jc_code() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
    )
    resolved = _resolved_post()
    resolved.message_text = "No hashtag hop le"
    with pytest.raises(ValueError):
        build_campaign_plan(
            command=cmd,
            resolved_post=resolved,
            post_fingerprint="abc123",
            version=1,
            timezone_name="Asia/Ho_Chi_Minh",
            audiences_config=_base_audiences(),
            objective_config=_base_objective(),
            template_config=_base_templates(),
        )


def test_build_campaign_plan_appends_non_jc_hashtag_suffix() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
    )
    resolved = _resolved_post()
    resolved.message_text = "Noi dung #JCV238 #congtruatocmay #Sale #JCV238 #sale"
    plan = build_campaign_plan(
        command=cmd,
        resolved_post=resolved,
        post_fingerprint="abc123",
        version=1,
        timezone_name="Asia/Ho_Chi_Minh",
        audiences_config=_base_audiences(),
        objective_config=_base_objective(),
        template_config=_base_templates(),
    )
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:ThaiLan|SKU:JCV238|MED:Anh|congtruatocmay_Sale"


def test_extract_non_jc_hashtags_and_suffix() -> None:
    message = "Noi dung #JCV238 #congtruatocmay #Sale #sale #JCA158"
    assert extract_non_jc_hashtags(message) == ["congtruatocmay", "Sale"]
    assert build_non_jc_hashtag_suffix(message) == "|congtruatocmay_Sale"


def test_extract_jc_codes_splits_underscore_group() -> None:
    assert extract_jc_codes("#JCA250_JCA248_JCQ211") == ["JCA250", "JCA248", "JCQ211"]
    assert extract_non_jc_hashtags("#JCA250_JCA248_JCQ211 #lamthao") == ["lamthao"]


def test_build_existing_campaign_plan_success() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140", "JCV140", "JCA158"],
    )
    plan = build_existing_campaign_plan(
        command=cmd,
        resolved_post=_resolved_post(),
        post_fingerprint="abc123",
        version=3,
        timezone_name="Asia/Ho_Chi_Minh",
        objective_config=_base_objective(),
        template_config=_base_templates(),
        sku_keywords=["JCV140", "JCV140", "JCA158"],
    )
    assert plan.version == 3
    assert plan.sku_code_text == "JCV140_JCA158"
    assert plan.message_template_name == "Chào JC"
    assert plan.audiences == []


def test_build_existing_campaign_plan_missing_keyword() -> None:
    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=1001&id=1002",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
    )
    with pytest.raises(ValueError):
        build_existing_campaign_plan(
            command=cmd,
            resolved_post=_resolved_post(),
            post_fingerprint="abc123",
            version=1,
            timezone_name="Asia/Ho_Chi_Minh",
            objective_config=_base_objective(),
            template_config=_base_templates(),
            sku_keywords=[],
        )
