from dataclasses import replace
from datetime import date
from pathlib import Path
import json
import logging

import pytest

from app.exceptions import MetaApiError, ValidationError
from app.meta_ads_client import MetaAdsClient
from app.models import AudienceSlot, PlannedCampaign, ResolvedPost
from app.settings import Settings


def _dummy_settings() -> Settings:
    root = Path(".")
    return Settings(
        project_root=root,
        storage_root=root / "storage",
        logs_root=root / "logs",
        state_root=root / "state",
        config_root=root / "config",
        telegram_bot_token="dummy",
        telegram_allowed_user_id=1,
        meta_access_token="dummy",
        meta_page_access_token="page_dummy",
        meta_ad_account_id="act_1",
        meta_page_id="61581440236157",
        meta_api_version="v21.0",
        app_timezone="Asia/Ho_Chi_Minh",
        app_currency="VND",
        retry_max=3,
        retry_backoff_seconds=[1, 2, 3],
        token_healthcheck_enabled=True,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=True,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="",
        pancake_access_token="",
        pancake_shop_id=0,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
    )


def test_resolve_post_from_story_fbid_url() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    resolved = client._resolve_post_from_url_patterns(
        "https://www.facebook.com/permalink.php?story_fbid=123456789&id=61581440236157"
    )
    assert resolved is not None
    assert resolved.post_id == "123456789"
    assert resolved.object_story_id == "61581440236157_123456789"


def test_resolve_post_from_numeric_posts_url() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    resolved = client._resolve_post_from_url_patterns(
        "https://www.facebook.com/jenniechoo.bangkok/posts/987654321"
    )
    assert resolved is not None
    assert resolved.post_id == "987654321"
    assert resolved.object_story_id == "61581440236157_987654321"


def test_resolve_post_owner_page_mismatch() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    with pytest.raises(ValidationError):
        client._resolve_post_from_url_patterns(
            "https://www.facebook.com/permalink.php?story_fbid=123&id=11111111111111"
        )


def test_resolve_post_from_pageid_pfbid_path() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    client._validate_page_access_token_owner = lambda: None  # type: ignore[method-assign]

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert path == "/61581440236157_pfbid0abc123"
        return {
            "id": "61581440236157_999888777",
            "permalink_url": "https://www.facebook.com/61581440236157/posts/999888777",
        }

    client._request = fake_request  # type: ignore[method-assign]
    resolved = client._resolve_post_from_pageid_pfbid(
        "https://www.facebook.com/jenniechoo.bangkok/posts/pfbid0abc123"
    )
    assert resolved is not None
    assert resolved.post_id == "999888777"
    assert resolved.page_id == "61581440236157"
    assert resolved.strategy == "direct_pageid_pfbid"


def test_resolve_pfbid_does_not_fallback_to_latest_post_when_unmatched() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    client._validate_page_access_token_owner = lambda: None  # type: ignore[method-assign]

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = method, params, data, access_token
        if path == "/61581440236157/posts":
            return {
                "data": [
                    {
                        "id": "61581440236157_999",
                        "permalink_url": "https://www.facebook.com/61581440236157/posts/999",
                    }
                ]
            }
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    with pytest.raises(ValidationError, match="tránh lên nhầm bài"):
        client._resolve_post_from_page_posts(
            "https://www.facebook.com/jenniechoo.bangkok/posts/pfbid0abc123"
        )


def test_list_page_posts_by_sku_returns_matching_posts_with_media_labels() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        if path == "/me":
            return {"id": "61581440236157", "name": "Page"}
        if path == "/61581440236157/posts":
            assert "attachments" in params["fields"]
            return {
                "data": [
                    {
                        "id": "61581440236157_101",
                        "message": "First angle #VXV002",
                        "permalink_url": "https://www.facebook.com/61581440236157/posts/101",
                        "status_type": "added_photos",
                        "attachments": {"data": [{"media_type": "photo"}]},
                    },
                    {
                        "id": "61581440236157_102",
                        "message": "Video angle #VXV002 #Sleepwears",
                        "permalink_url": "https://www.facebook.com/reel/202/",
                        "status_type": "added_video",
                        "attachments": {"data": [{"media_type": "video"}]},
                    },
                    {
                        "id": "61581440236157_103",
                        "message": "Other sku #VXV003",
                        "permalink_url": "https://www.facebook.com/61581440236157/posts/103",
                        "status_type": "added_photos",
                    },
                ]
            }
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    posts = client.list_page_posts_by_sku(["VXV002"], max_scan=100, max_posts=10)

    assert [(post.object_story_id, post.media_label) for post in posts] == [
        ("61581440236157_101", "Anh"),
        ("61581440236157_102", "Video"),
    ]
    assert posts[0].message_text == "First angle #VXV002"


def test_normalize_campaign_objective_alias() -> None:
    assert MetaAdsClient._normalize_campaign_objective("ENGAGEMENT") == "OUTCOME_ENGAGEMENT"


def test_is_instagram_media_requirement_error_detects_vi_message() -> None:
    assert MetaAdsClient.is_instagram_media_requirement_error(
        "Meta API loi (400): Bài viết của bạn không có hình ảnh hoặc video. "
        "Quảng cáo trên Instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết."
    )


def test_is_instagram_media_requirement_error_detects_en_message() -> None:
    assert MetaAdsClient.is_instagram_media_requirement_error(
        "Meta API error: Your post has no image or video. Instagram ads currently only support video, image and link posts."
    )


def test_is_post_not_advertisable_error_detects_vi_message() -> None:
    assert MetaAdsClient.is_post_not_advertisable_error(
        "Meta API loi (400): Bạn đang sử dụng Post ID: 122134418307048007, bài viết này không thể đưa vào quảng cáo được."
    )


def test_is_post_not_advertisable_error_detects_vi_invalid_post_message() -> None:
    assert MetaAdsClient.is_post_not_advertisable_error(
        "Meta API loi (400): Bạn đang quảng cáo bài viết không hợp lệ nên không tạo được quảng cáo."
    )


def test_is_link_ad_cta_locked_error_detects_vi_message() -> None:
    assert MetaAdsClient.is_link_ad_cta_locked_error(
        "Meta API loi (400): Bài viết này đang chạy quảng cáo liên kết, do đó bạn chưa thể chỉnh sửa nút kêu gọi hành động"
    )


def test_find_latest_ad_by_story_ids_returns_latest_candidate() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, data, access_token
        assert method == "GET"
        assert path == "/adset_1/ads"
        return {
            "data": [
                {
                    "id": "ad_1",
                    "name": "Older",
                    "status": "PAUSED",
                    "effective_status": "PAUSED",
                    "updated_time": "2026-05-25T09:00:00+0700",
                    "creative": {
                        "id": "cr_1",
                        "object_story_id": "853907927797012_122134418307048007",
                        "effective_object_story_id": "853907927797012_122134418307048007",
                    },
                },
                {
                    "id": "ad_2",
                    "name": "Newer",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-05-25T09:30:00+0700",
                    "creative": {
                        "id": "cr_2",
                        "object_story_id": "853907927797012_122134418307048007",
                        "effective_object_story_id": "853907927797012_122134418307048007",
                    },
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    matched = client.find_latest_ad_by_story_ids(
        ["853907927797012_122134418307048007"],
        adset_id="adset_1",
    )
    assert matched is not None
    assert matched["id"] == "ad_2"
    assert matched["creative_id"] == "cr_2"


def test_find_reusable_creative_id_by_story_ids_filters_message_template_mismatch() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    expected_welcome = {
        "reengagement": {
            "message": {"text": "VAYXA welcome"},
        },
        "ice_breakers": {
            "message": {"ice_breakers": []},
        },
        "has_ai_generated_welcome_message": False,
    }

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, data, access_token
        assert method == "GET"
        assert path == "/act_1/ads"
        return {
            "data": [
                {
                    "id": "ad_bad",
                    "updated_time": "2026-06-29T11:46:00+0700",
                    "creative": {
                        "id": "cr_bad",
                        "object_story_id": "649228828282105_122179632500934207",
                        "effective_object_story_id": "649228828282105_122179632500934207",
                        "page_welcome_message": "Mess Cơ bản",
                    },
                },
                {
                    "id": "ad_good",
                    "updated_time": "2026-06-29T11:20:00+0700",
                    "creative": {
                        "id": "cr_good",
                        "object_story_id": "649228828282105_122179632500934207",
                        "effective_object_story_id": "649228828282105_122179632500934207",
                        "page_welcome_message": {
                            "reengagement": {
                                "message": {"text": "VAYXA welcome", "extra": "ok"},
                            },
                            "ice_breakers": {
                                "message": {"ice_breakers": []},
                            },
                            "has_ai_generated_welcome_message": False,
                            "template_version": 0,
                        },
                    },
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    creative_id = client.find_reusable_creative_id_by_story_ids(
        ["649228828282105_122179632500934207"],
        expected_page_welcome_message=expected_welcome,
    )

    assert creative_id == "cr_good"


def test_find_reusable_creative_id_by_story_ids_filters_cta_mismatch() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        assert path == "/act_1/ads"
        assert "call_to_action_type" in params["fields"]
        return {
            "data": [
                {
                    "id": "ad_bad_cta",
                    "updated_time": "2026-06-30T11:28:56+0700",
                    "creative": {
                        "id": "cr_bad_cta",
                        "object_story_id": "649228828282105_122187967838934207",
                        "effective_object_story_id": "649228828282105_122187967838934207",
                        "call_to_action_type": "INSTAGRAM_MESSAGE",
                    },
                },
                {
                    "id": "ad_good_cta",
                    "updated_time": "2026-06-30T11:20:00+0700",
                    "creative": {
                        "id": "cr_good_cta",
                        "object_story_id": "649228828282105_122187967838934207",
                        "effective_object_story_id": "649228828282105_122187967838934207",
                        "call_to_action_type": "MESSAGE_PAGE",
                    },
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    creative_id = client.find_reusable_creative_id_by_story_ids(
        ["649228828282105_122187967838934207"],
        expected_call_to_action_type="MESSAGE_PAGE",
    )

    assert creative_id == "cr_good_cta"


def test_find_latest_ad_by_story_ids_filters_message_template_mismatch() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    expected_welcome = {
        "reengagement": {"message": {"text": "VAYXA welcome"}},
        "has_ai_generated_welcome_message": False,
    }

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, data, access_token
        assert method == "GET"
        assert path == "/adset_1/ads"
        return {
            "data": [
                {
                    "id": "ad_bad",
                    "name": "Bad template",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-06-29T11:46:00+0700",
                    "creative": {
                        "id": "cr_bad",
                        "object_story_id": "649228828282105_122179632500934207",
                        "effective_object_story_id": "649228828282105_122179632500934207",
                        "page_welcome_message": "Mess Cơ bản",
                    },
                },
                {
                    "id": "ad_good",
                    "name": "Good template",
                    "status": "PAUSED",
                    "effective_status": "PAUSED",
                    "updated_time": "2026-06-29T11:20:00+0700",
                    "creative": {
                        "id": "cr_good",
                        "object_story_id": "649228828282105_122179632500934207",
                        "effective_object_story_id": "649228828282105_122179632500934207",
                        "page_welcome_message": json.dumps(
                            {
                                "reengagement": {
                                    "message": {"text": "VAYXA welcome"},
                                },
                                "has_ai_generated_welcome_message": False,
                                "template_id": "saved_template_id",
                            }
                        ),
                    },
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    matched = client.find_latest_ad_by_story_ids(
        ["649228828282105_122179632500934207"],
        adset_id="adset_1",
        expected_page_welcome_message=expected_welcome,
    )

    assert matched is not None
    assert matched["id"] == "ad_good"
    assert matched["creative_id"] == "cr_good"


def test_find_latest_ad_by_story_ids_filters_cta_mismatch() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        assert path == "/adset_1/ads"
        assert "call_to_action_type" in params["fields"]
        return {
            "data": [
                {
                    "id": "ad_bad_cta",
                    "name": "Bad CTA",
                    "status": "ACTIVE",
                    "effective_status": "WITH_ISSUES",
                    "updated_time": "2026-06-30T11:28:56+0700",
                    "creative": {
                        "id": "cr_bad_cta",
                        "object_story_id": "649228828282105_122187967838934207",
                        "effective_object_story_id": "649228828282105_122187967838934207",
                        "call_to_action_type": "INSTAGRAM_MESSAGE",
                    },
                },
                {
                    "id": "ad_good_cta",
                    "name": "Good CTA",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-06-30T11:20:00+0700",
                    "creative": {
                        "id": "cr_good_cta",
                        "object_story_id": "649228828282105_122187967838934207",
                        "effective_object_story_id": "649228828282105_122187967838934207",
                        "call_to_action_type": "MESSAGE_PAGE",
                    },
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    matched = client.find_latest_ad_by_story_ids(
        ["649228828282105_122187967838934207"],
        adset_id="adset_1",
        expected_call_to_action_type="MESSAGE_PAGE",
    )

    assert matched is not None
    assert matched["id"] == "ad_good_cta"
    assert matched["creative_id"] == "cr_good_cta"
    assert matched["call_to_action_type"] == "MESSAGE_PAGE"


def test_create_campaign_normalizes_legacy_objective_in_override() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    sent_payload: dict[str, str] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        if data:
            sent_payload.update(data)
        return {"id": "1234567890"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="FBMSG_20260516_test_v1",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chao JC",
        raw={
            "campaign_payload_overrides": {
                "objective": "engagement",
            }
        },
    )

    campaign_id = client.create_campaign(plan)
    assert campaign_id == "1234567890"
    assert sent_payload["objective"] == "OUTCOME_ENGAGEMENT"
    assert sent_payload["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"


def test_normalize_adset_payload_maps_legacy_result_goal_and_removes_old_fields() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    payload = client._normalize_adset_payload(
        {
            "name": "A",
            "conversion_location": "MESSAGING_DESTINATION",
            "result_goal": "MAXIMIZE_PURCHASES_VIA_MESSAGE",
            "bid_strategy": "lowest_cost_without_cap",
            "bid_amount": 1000,
            "cost_cap": 900,
            "target_cost": 800,
            "bid_constraints": {"roas_average_floor": 1.2},
        }
    )
    assert payload["optimization_goal"] == "MESSAGING_PURCHASE_CONVERSION"
    assert "conversion_location" not in payload
    assert "result_goal" not in payload
    assert payload["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"
    assert "bid_amount" not in payload
    assert "cost_cap" not in payload
    assert "target_cost" not in payload
    assert "bid_constraints" not in payload


def test_create_adset_retries_with_simple_payload_when_bid_amount_required() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    client.get_saved_audience_targeting = lambda _id: {"geo_locations": {"countries": ["TH"]}}  # type: ignore[method-assign]

    sent_payloads: list[dict[str, object]] = []
    call_state = {"count": 0}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        if data:
            sent_payloads.append(dict(data))
        call_state["count"] += 1
        if call_state["count"] == 1:
            raise MetaApiError("Meta API loi (400): bid_amount required for LOWEST_COST_WITH_BID_CAP")
        return {"id": "adset_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="FBMSG_20260516_test_v1",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chao JC",
        raw={
            "adset_payload_overrides": {
                "bid_strategy": "LOWEST_COST_WITH_BID_CAP",
                "optimization_goal": "CONVERSATIONS",
                "destination_type": "MESSENGER",
            }
        },
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="TEST_TS",
        ad_name="TEST_TS_AD1",
    )

    adset_id = client.create_adset(plan=plan, campaign_id="camp_123", slot=slot)
    assert adset_id == "adset_123"
    assert len(sent_payloads) == 2
    assert sent_payloads[0]["bid_strategy"] == "LOWEST_COST_WITH_BID_CAP"
    assert "bid_strategy" not in sent_payloads[1]
    assert sent_payloads[1]["optimization_goal"] == "MESSAGING_PURCHASE_CONVERSION"
    assert sent_payloads[1]["billing_event"] == "IMPRESSIONS"
    assert sent_payloads[1]["destination_type"] == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"


def test_create_adset_uses_targeting_override_without_saved_audience() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, access_token
        assert method == "POST"
        assert path == "/act_1/adsets"
        captured.update(data or {})
        return {"id": "adset_broad_1"}

    client._request = fake_request  # type: ignore[method-assign]
    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|VXV011",
        sku_code_text="VXV011",
        media_label="Anh",
        post_url="https://www.facebook.com/post",
        post_fingerprint="abc",
        budget_daily_vnd=235014,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Mess Cơ bản",
        raw={
            "adset_payload_overrides": {
                "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                "targeting": {
                    "age_min": 20,
                    "age_max": 65,
                    "genders": [2],
                    "geo_locations": {"countries": ["TH"], "location_types": ["home", "recent"]},
                    "targeting_automation": {"advantage_audience": 1},
                },
            }
        },
    )
    slot = AudienceSlot(
        key="new_campaign_single_adset",
        label="Broad",
        suffix="BROAD",
        saved_audience_id="",
        adset_name="ADS:QUYET|MK:ThaiLan|VXV011",
        ad_name="ADS:QUYET|MK:ThaiLan|SKU:VXV011|MED:Anh",
    )

    adset_id = client.create_adset(plan=plan, campaign_id="camp_1", slot=slot)

    assert adset_id == "adset_broad_1"
    assert captured["targeting"] == plan.raw["adset_payload_overrides"]["targeting"]


def test_create_ad_creative_strips_internal_template_keys() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/adcreatives"
        if data:
            captured.update(data)
        return {"id": "creative_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={
            "creative_payload_overrides": {
                "message_template_name": "Chào JC",
                "page_welcome_message_source_creative_id": "2900378033648616",
                "page_welcome_message": {
                    "template_id": "1169691625327338",
                    "template_version": 0,
                },
            }
        },
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )
    resolved = ResolvedPost(
        post_id="123",
        page_id="61581440236157",
        permalink_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        object_story_id="61581440236157_123",
    )

    creative_id = client.create_ad_creative(plan=plan, slot=slot, resolved_post=resolved)
    assert creative_id == "creative_123"
    assert "message_template_name" not in captured
    assert "page_welcome_message_source_creative_id" not in captured
    assert isinstance(captured.get("page_welcome_message"), dict)


def test_create_ad_creative_uses_creative_access_token_when_configured() -> None:
    settings = replace(_dummy_settings(), meta_creative_access_token="creative_token")
    client = MetaAdsClient(settings=settings, logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, data
        assert method == "POST"
        assert path == "/act_1/adcreatives"
        assert access_token == "creative_token"
        return {"id": "creative_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238",
        sku_code_text="JCV238",
        media_label="Video",
        post_url="https://www.facebook.com/reel/123",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={"creative_payload_overrides": {}},
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )
    resolved = ResolvedPost(
        post_id="123",
        page_id="61581440236157",
        permalink_url="https://www.facebook.com/reel/123",
        object_story_id="61581440236157_123",
    )

    assert client.create_ad_creative(plan=plan, slot=slot, resolved_post=resolved) == "creative_123"


def test_create_ad_creative_applies_extra_overrides() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/adcreatives"
        if data:
            captured.update(data)
        return {"id": "creative_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={
            "creative_payload_overrides": {},
        },
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )
    resolved = ResolvedPost(
        post_id="123",
        page_id="61581440236157",
        permalink_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        object_story_id="61581440236157_123",
    )

    creative_id = client.create_ad_creative(
        plan=plan,
        slot=slot,
        resolved_post=resolved,
        destination_type_override="MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        extra_payload_overrides={"asset_feed_spec": {"optimization_type": "DOF_MESSAGING_DESTINATION"}},
    )
    assert creative_id == "creative_123"
    assert isinstance(captured.get("asset_feed_spec"), dict)
    assert captured["asset_feed_spec"]["optimization_type"] == "DOF_MESSAGING_DESTINATION"


def test_create_ad_creative_replaces_dof_with_seed_override() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, access_token
        assert method == "POST"
        assert path == "/act_1/adcreatives"
        if data:
            captured.update(data)
        return {"id": "creative_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|VXV011",
        sku_code_text="VXV011",
        media_label="Anh",
        post_url="https://www.facebook.com/posts/1",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Mess Cơ bản",
        raw={"creative_payload_overrides": {}},
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )
    resolved = ResolvedPost(
        post_id="123",
        page_id="61581440236157",
        permalink_url="https://www.facebook.com/posts/123",
        object_story_id="61581440236157_123",
    )
    seed_dof = {
        "creative_features_spec": {
            "media_order": {"enroll_status": "OPT_IN"},
            "standard_enhancements": {"enroll_status": "OPT_IN"},
        }
    }

    creative_id = client.create_ad_creative(
        plan=plan,
        slot=slot,
        resolved_post=resolved,
        destination_type_override="MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        extra_payload_overrides={
            "degrees_of_freedom_spec": seed_dof,
            "contextual_multi_ads": {"enroll_status": "OPT_OUT"},
            "instagram_user_id": "17841478128229539",
        },
    )

    assert creative_id == "creative_123"
    assert captured["degrees_of_freedom_spec"] == seed_dof
    assert "product_extensions" not in captured["degrees_of_freedom_spec"]["creative_features_spec"]
    assert captured["instagram_user_id"] == "17841478128229539"


def test_create_ad_includes_dof_for_auto_destination() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/ads"
        if data:
            captured.update(data)
        return {"id": "ad_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238|Codex",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={
            "ad_payload_overrides": {},
        },
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )

    ad_id = client.create_ad(plan=plan, slot=slot, adset_id="adset_1", creative_id="creative_1")
    assert ad_id == "ad_123"
    assert "degrees_of_freedom_spec" in captured
    assert captured.get("contextual_multi_ads") == {"enroll_status": "OPT_OUT"}


def test_create_ad_omits_dof_when_destination_is_messenger() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/ads"
        if data:
            captured.update(data)
        return {"id": "ad_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238|Codex",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={
            "adset_payload_overrides": {
                "destination_type": "MESSENGER",
            },
            "ad_payload_overrides": {},
        },
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )

    ad_id = client.create_ad(plan=plan, slot=slot, adset_id="adset_1", creative_id="creative_1")
    assert ad_id == "ad_123"
    assert "degrees_of_freedom_spec" not in captured
    assert "contextual_multi_ads" not in captured


def test_create_ad_creative_override_destination_omits_dof() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/adcreatives"
        if data:
            captured.update(data)
        return {"id": "creative_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238|Codex",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={},
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )
    resolved = ResolvedPost(
        post_id="123",
        page_id="61581440236157",
        permalink_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        object_story_id="61581440236157_123",
    )

    creative_id = client.create_ad_creative(
        plan=plan,
        slot=slot,
        resolved_post=resolved,
        destination_type_override="MESSENGER",
    )
    assert creative_id == "creative_123"
    assert "degrees_of_freedom_spec" not in captured
    assert "contextual_multi_ads" not in captured


def test_create_ad_override_destination_omits_dof() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "POST"
        assert path == "/act_1/ads"
        if data:
            captured.update(data)
        return {"id": "ad_123"}

    client._request = fake_request  # type: ignore[method-assign]

    plan = PlannedCampaign(
        version=1,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCV238|Codex",
        sku_code_text="JCV238",
        media_label="Anh",
        post_url="https://www.facebook.com/permalink.php?story_fbid=1&id=61581440236157",
        post_fingerprint="abc",
        budget_daily_vnd=300000,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        raw={},
    )
    slot = AudienceSlot(
        key="thoi_trang_saved_audience_id",
        label="Thoi trang",
        suffix="TS",
        saved_audience_id="111",
        adset_name="A",
        ad_name="B",
    )

    ad_id = client.create_ad(
        plan=plan,
        slot=slot,
        adset_id="adset_1",
        creative_id="creative_1",
        destination_type_override="MESSENGER",
    )
    assert ad_id == "ad_123"
    assert "degrees_of_freedom_spec" not in captured
    assert "contextual_multi_ads" not in captured


def test_duplicate_ad_from_source_uses_paused_status_and_renames() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, access_token
        calls.append((method, path, dict(data) if isinstance(data, dict) else None))
        if path == "/120249992082570728/copies":
            return {"copied_ad_id": "120300000000000001"}
        if path == "/120300000000000001":
            return {"success": True}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    copied_ad_id = client.duplicate_ad_from_source(
        "120249992082570728",
        "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Video|ADSET:120248804559660728",
    )

    assert copied_ad_id == "120300000000000001"
    assert calls == [
        ("POST", "/120249992082570728/copies", {"status_option": "PAUSED"}),
        (
            "POST",
            "/120300000000000001",
            {"name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Video|ADSET:120248804559660728"},
        ),
    ]


def test_duplicate_ad_from_source_supports_target_adset_override() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, access_token
        calls.append((method, path, dict(data) if isinstance(data, dict) else None))
        if path == "/120249992082570728/copies":
            return {"copied_ad_id": "120300000000000003"}
        return {"success": True}

    client._request = fake_request  # type: ignore[method-assign]

    copied_ad_id = client.duplicate_ad_from_source(
        "120249992082570728",
        target_ad_name="Copy A",
        target_adset_id="120248804559660728",
    )

    assert copied_ad_id == "120300000000000003"
    assert calls[0] == (
        "POST",
        "/120249992082570728/copies",
        {"status_option": "PAUSED", "adset_id": "120248804559660728"},
    )


def test_list_adset_ads_for_copy_filters_issues_and_name() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        assert path == "/adset_source/ads"
        assert "issues_info" in params["fields"]
        return {
            "data": [
                {
                    "id": "ad_active",
                    "name": "ADS:QUYET|MK:ThaiLan|SKU:VXV011|MED:Video",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-07-03T01:00:00+0000",
                    "creative": {
                        "id": "cr_1",
                        "object_story_id": "649_1",
                        "call_to_action_type": "INSTAGRAM_MESSAGE",
                    },
                },
                {
                    "id": "ad_paused",
                    "name": "ADS:QUYET|MK:ThaiLan|SKU:VXV011|MED:Anh",
                    "status": "PAUSED",
                    "effective_status": "PAUSED",
                    "updated_time": "2026-07-03T02:00:00+0000",
                    "creative": {
                        "id": "cr_2",
                        "object_story_id": "649_2",
                        "call_to_action_type": "MESSAGE_PAGE",
                    },
                },
                {
                    "id": "ad_issue",
                    "name": "ADS:QUYET|MK:ThaiLan|SKU:VXV011|MED:Anh",
                    "status": "ACTIVE",
                    "effective_status": "WITH_ISSUES",
                    "issues_info": [{"error_code": 1}],
                    "creative": {"id": "cr_bad"},
                },
                {
                    "id": "ad_other_sku",
                    "name": "ADS:QUYET|MK:ThaiLan|SKU:VXV099|MED:Anh",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "creative": {"id": "cr_other"},
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    ads = client.list_adset_ads_for_copy(
        "adset_source",
        name_contains="SKU:VXV011",
        include_statuses=["ACTIVE", "PAUSED"],
    )

    assert [ad["id"] for ad in ads] == ["ad_active", "ad_paused"]
    assert ads[1]["object_story_id"] == "649_2"


def test_extract_copied_ad_id_supports_list_shape() -> None:
    copied_ad_id = MetaAdsClient._extract_copied_ad_id({"copies": [{"id": "120300000000000002"}]})
    assert copied_ad_id == "120300000000000002"


def test_get_multi_destination_asset_feed_spec_success() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        if path == "/adset_1/ads":
            return {
                "data": [
                    {
                        "id": "ad_1",
                        "updated_time": "2026-05-18T10:00:00+0000",
                        "creative": {"id": "cr_1"},
                    },
                    {
                        "id": "ad_2",
                        "updated_time": "2026-05-18T09:00:00+0000",
                        "creative": {"id": "cr_2"},
                    },
                ]
            }
        if path == "/cr_1":
            return {"asset_feed_spec": {"optimization_type": "DOF_MESSAGING_DESTINATION"}}
        if path == "/cr_2":
            return {"asset_feed_spec": {}}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    spec = client.get_multi_destination_asset_feed_spec("adset_1")
    assert spec["optimization_type"] == "DOF_MESSAGING_DESTINATION"


def test_get_multi_destination_creative_overrides_uses_healthy_message_page_seed() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        if path == "/adset_1/ads":
            return {
                "data": [
                    {
                        "id": "bad_newer",
                        "effective_status": "WITH_ISSUES",
                        "updated_time": "2026-06-30T11:45:00+0700",
                        "creative": {"id": "cr_bad"},
                    },
                    {
                        "id": "good_seed",
                        "effective_status": "ACTIVE",
                        "updated_time": "2026-06-30T11:34:56+0700",
                        "creative": {"id": "cr_good"},
                    },
                ]
            }
        if path == "/cr_good":
            assert "instagram_user_id" in params["fields"]
            assert "page_welcome_message" in params["fields"]
            return {
                "call_to_action_type": "MESSAGE_PAGE",
                "asset_feed_spec": {
                    "optimization_type": "DOF_MESSAGING_DESTINATION",
                    "call_to_actions": [
                        {"type": "INSTAGRAM_MESSAGE"},
                        {"type": "MESSAGE_PAGE"},
                    ],
                    "additional_data": {"seed": True},
                },
                "degrees_of_freedom_spec": {
                    "creative_features_spec": {
                        "media_order": {"enroll_status": "OPT_IN"},
                        "standard_enhancements": {"enroll_status": "OPT_IN"},
                    }
                },
                "contextual_multi_ads": {"enroll_status": "OPT_OUT"},
                "instagram_user_id": "17841478128229539",
                "page_welcome_message": '{"template_id":"962707759488898"}',
            }
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    overrides = client.get_multi_destination_creative_overrides(
        "adset_1",
        expected_call_to_action_type="MESSAGE_PAGE",
    )

    assert overrides == {
        "asset_feed_spec": {
            "optimization_type": "DOF_MESSAGING_DESTINATION",
            "call_to_actions": [
                {"type": "INSTAGRAM_MESSAGE"},
                {"type": "MESSAGE_PAGE"},
            ],
            "additional_data": {"seed": True},
        },
        "call_to_action_type": "MESSAGE_PAGE",
        "degrees_of_freedom_spec": {
            "creative_features_spec": {
                "media_order": {"enroll_status": "OPT_IN"},
                "standard_enhancements": {"enroll_status": "OPT_IN"},
            }
        },
        "contextual_multi_ads": {"enroll_status": "OPT_OUT"},
        "instagram_user_id": "17841478128229539",
        "page_welcome_message": '{"template_id":"962707759488898"}',
    }


def test_get_message_destination_creative_overrides_does_not_require_asset_feed_spec() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = data, access_token
        assert method == "GET"
        if path == "/adset_1/ads":
            return {
                "data": [
                    {
                        "id": "seed_ad",
                        "effective_status": "ACTIVE",
                        "updated_time": "2026-06-30T14:16:24+0700",
                        "creative": {"id": "cr_seed"},
                    }
                ]
            }
        if path == "/cr_seed":
            assert "asset_feed_spec" in params["fields"]
            assert "page_welcome_message" in params["fields"]
            return {
                "call_to_action_type": "MESSAGE_PAGE",
                "degrees_of_freedom_spec": {
                    "creative_features_spec": {
                        "media_order": {"enroll_status": "OPT_OUT"},
                        "product_extensions": {"enroll_status": "OPT_OUT"},
                    }
                },
                "instagram_user_id": "17841478128229539",
                "page_welcome_message": '{"template_id":"962707759488898"}',
            }
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    overrides = client.get_message_destination_creative_overrides(
        "adset_1",
        expected_call_to_action_type="MESSAGE_PAGE",
    )

    assert overrides == {
        "call_to_action_type": "MESSAGE_PAGE",
        "degrees_of_freedom_spec": {
            "creative_features_spec": {
                "media_order": {"enroll_status": "OPT_OUT"},
                "product_extensions": {"enroll_status": "OPT_OUT"},
            }
        },
        "instagram_user_id": "17841478128229539",
        "page_welcome_message": '{"template_id":"962707759488898"}',
    }


def test_get_multi_destination_asset_feed_spec_raises_when_missing() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        if path == "/adset_1/ads":
            return {"data": [{"id": "ad_1", "updated_time": "2026-05-18T10:00:00+0000", "creative": {"id": "cr_1"}}]}
        if path == "/cr_1":
            return {"asset_feed_spec": {}}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    with pytest.raises(ValidationError):
        client.get_multi_destination_asset_feed_spec("adset_1")


def test_get_account_multi_destination_asset_feed_spec_success() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        if path == "/act_1/ads":
            return {
                "data": [
                    {"id": "ad_1", "updated_time": "2026-05-18T10:00:00+0000", "creative": {"id": "cr_1"}},
                    {"id": "ad_2", "updated_time": "2026-05-18T09:00:00+0000", "creative": {"id": "cr_2"}},
                ]
            }
        if path == "/cr_1":
            return {"asset_feed_spec": {"optimization_type": "DOF_MESSAGING_DESTINATION"}}
        if path == "/cr_2":
            return {"asset_feed_spec": {}}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    spec = client.get_account_multi_destination_asset_feed_spec()
    assert spec["optimization_type"] == "DOF_MESSAGING_DESTINATION"


def test_get_account_multi_destination_asset_feed_spec_raises_when_missing() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        if path == "/act_1/ads":
            return {"data": [{"id": "ad_1", "updated_time": "2026-05-18T10:00:00+0000", "creative": {"id": "cr_1"}}]}
        if path == "/cr_1":
            return {"asset_feed_spec": {}}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    with pytest.raises(ValidationError):
        client.get_account_multi_destination_asset_feed_spec()


def test_is_auto_destination_error_recognizes_objective_mismatch_marker() -> None:
    message = "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
    assert MetaAdsClient.is_auto_destination_error(message) is True


def test_diagnose_instagram_post_access_reports_missing_instagram_asset_access() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        if path == "/61581440236157_123":
            return {
                "id": "61581440236157_123",
                "admin_creator": {
                    "name": "Instagram",
                    "namespace": "instapp",
                },
                "instagram_eligibility": "eligible",
                "is_instagram_eligible": True,
            }
        if path == "/me/permissions":
            return {
                "data": [
                    {"permission": "ads_management", "status": "granted"},
                    {"permission": "pages_manage_ads", "status": "granted"},
                ]
            }
        if path == "/act_1/instagram_accounts":
            return {"data": []}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    diagnostics = client.diagnose_instagram_post_access("61581440236157_123")

    assert diagnostics["is_instagram_origin"] is True
    assert diagnostics["instagram_eligibility"] == "eligible"
    assert diagnostics["is_instagram_eligible"] is True
    assert diagnostics["instagram_basic_granted"] is False
    assert diagnostics["instagram_account_count"] == 0
    assert diagnostics["has_instagram_media_access"] is False


def test_diagnose_instagram_post_access_uses_creative_token_for_media_access() -> None:
    settings = replace(_dummy_settings(), meta_creative_access_token="creative_token")
    client = MetaAdsClient(settings=settings, logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = params, data
        assert method == "GET"
        if path == "/61581440236157_123":
            return {
                "id": "61581440236157_123",
                "admin_creator": {"name": "Instagram", "namespace": "instapp"},
                "instagram_eligibility": "eligible",
                "is_instagram_eligible": True,
            }
        if path == "/me/permissions":
            if access_token == "creative_token":
                return {
                    "data": [
                        {"permission": "ads_management", "status": "granted"},
                        {"permission": "instagram_basic", "status": "granted"},
                    ]
                }
            return {"data": [{"permission": "ads_management", "status": "granted"}]}
        if path == "/act_1/instagram_accounts":
            if access_token == "creative_token":
                return {"data": [{"id": "17841443328764553", "username": "jenniechoo.th"}]}
            return {"data": []}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    diagnostics = client.diagnose_instagram_post_access("61581440236157_123")

    assert diagnostics["is_instagram_origin"] is True
    assert diagnostics["creative_token_present"] is True
    assert diagnostics["creative_instagram_basic_granted"] is True
    assert diagnostics["creative_instagram_account_count"] == 1
    assert diagnostics["instagram_basic_granted"] is True
    assert diagnostics["instagram_account_count"] == 1
    assert diagnostics["has_instagram_media_access"] is True


def test_check_token_health_success() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        if path == "/act_1":
            return {
                "id": "act_1",
                "name": "Test Account",
                "account_status": 1,
                "currency": "VND",
            }
        if path == "/me" and access_token == "dummy":
            return {"id": "sys_1", "name": "Bot AI"}
        if path == "/61581440236157" and access_token == "dummy":
            return {"id": "61581440236157", "name": "Test Page"}
        if path == "/me":
            return {"id": "61581440236157", "name": "Test Page"}
        if path == "/61581440236157/posts":
            return {"data": [{"id": "61581440236157_123"}]}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]
    report = client.check_token_health()

    assert report["ok"] is True
    assert report["checks"]["ads_account"]["ok"] is True
    assert report["checks"]["ads_identity"]["name"] == "Bot AI"
    assert report["checks"]["ads_page_access"]["ok"] is True
    assert report["checks"]["page_identity"]["ok"] is True
    assert report["checks"]["page_posts"]["first_post_id"] == "61581440236157_123"


def test_check_token_health_reports_failure_when_page_token_missing() -> None:
    settings = replace(_dummy_settings(), meta_page_access_token="")
    client = MetaAdsClient(settings=settings, logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        if path == "/act_1":
            return {"id": "act_1", "account_status": 1, "currency": "VND", "name": "A"}
        if path == "/me":
            return {"id": "sys_1", "name": "Bot AI"}
        if path == "/61581440236157":
            return {"id": "61581440236157", "name": "Test Page"}
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]
    report = client.check_token_health()
    assert report["ok"] is False
    assert report["checks"]["page_identity"]["ok"] is False


def test_ensure_ads_token_can_access_page_raises_validation_error() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        if path == "/61581440236157":
            raise MetaApiError("Meta API loi (400): Bạn cần có quyền truy cập để quảng cáo cho Trang này.")
        raise AssertionError(f"Unexpected path: {path}")

    client._request = fake_request  # type: ignore[method-assign]

    with pytest.raises(ValidationError) as exc_info:
        client.ensure_ads_token_can_access_page()
    assert "chưa đủ quyền quảng cáo cho Trang" in str(exc_info.value)


def test_get_daily_spend_success() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/act_1/insights"
        assert params["level"] == "account"
        return {
            "data": [
                {
                    "account_id": "act_1",
                    "date_start": "2026-05-15",
                    "date_stop": "2026-05-15",
                    "spend": "300000.0",
                }
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    result = client.get_daily_spend(date(2026, 5, 15), "Asia/Ho_Chi_Minh")

    assert result["spend_vnd"] == 300000
    assert result["date_start"] == "2026-05-15"
    assert result["date_stop"] == "2026-05-15"


def test_get_daily_spend_returns_zero_when_empty_data() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    client._request = lambda *args, **kwargs: {"data": []}  # type: ignore[method-assign]

    result = client.get_daily_spend(date(2026, 5, 15), "Asia/Ho_Chi_Minh")

    assert result["spend_vnd"] == 0
    assert result["date_start"] == "2026-05-15"
    assert result["date_stop"] == "2026-05-15"


def test_get_spend_for_range_uses_account_total_without_time_increment() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/act_1/insights"
        assert params["level"] == "account"
        assert "time_increment" not in params
        assert json.loads(params["time_range"]) == {
            "since": "2026-05-01",
            "until": "2026-05-31",
        }
        return {
            "data": [
                {
                    "account_id": "act_1",
                    "date_start": "2026-05-01",
                    "date_stop": "2026-05-31",
                    "spend": "130929340",
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    result = client.get_spend_for_range(date(2026, 5, 1), date(2026, 5, 31), "Asia/Ho_Chi_Minh")

    assert result["spend_vnd"] == 130929340
    assert result["date_start"] == "2026-05-01"
    assert result["date_stop"] == "2026-05-31"


def test_get_spend_for_range_retries_page_token_when_ads_token_expired() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    used_tokens: list[str | None] = []

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = method, params, data
        used_tokens.append(access_token)
        assert path == "/act_1/insights"
        if access_token == "dummy":
            raise MetaApiError("Meta API loi (400): The token has expired.")
        return {
            "data": [
                {
                    "account_id": "act_1",
                    "date_start": "2026-07-19",
                    "date_stop": "2026-07-19",
                    "spend": "2000000",
                }
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    result = client.get_spend_for_range(date(2026, 7, 19), date(2026, 7, 19), "Asia/Ho_Chi_Minh")

    assert result["spend_vnd"] == 2000000
    assert used_tokens == ["dummy", "page_dummy"]


def test_get_ad_insights_for_range_requests_ad_level_action_values() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        captured["method"] = method
        captured["path"] = path
        captured["params"] = dict(params or {})
        captured["access_token"] = access_token
        return {
            "data": [
                {
                    "ad_id": "ad_1",
                    "ad_name": "ADS:QUYET|SKU:VXV011|MED:Video",
                    "spend": "100000",
                    "actions": [{"action_type": "omni_purchase", "value": "2"}],
                    "action_values": [{"action_type": "omni_purchase", "value": "500000"}],
                }
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]

    rows = client.get_ad_insights_for_range(date(2026, 7, 1), date(2026, 7, 7), "Asia/Ho_Chi_Minh")

    assert rows[0]["ad_id"] == "ad_1"
    assert captured["method"] == "GET"
    assert captured["path"] == "/act_1/insights"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["level"] == "ad"
    assert params["use_unified_attribution_setting"] == "true"
    assert "actions" in str(params["fields"])
    assert "action_values" in str(params["fields"])
    assert captured["access_token"] == "dummy"


def test_get_ad_insights_for_range_retries_page_token_when_ads_token_expired() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    used_tokens: list[str | None] = []

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        _ = method, params, data
        used_tokens.append(access_token)
        assert path == "/act_1/insights"
        if access_token == "dummy":
            raise MetaApiError("Meta API loi (400): The token has expired.")
        return {"data": [{"ad_id": "ad_1", "spend": "100000"}]}

    client._request = fake_request  # type: ignore[method-assign]

    rows = client.get_ad_insights_for_range(date(2026, 7, 1), date(2026, 7, 7), "Asia/Ho_Chi_Minh")

    assert rows == [{"ad_id": "ad_1", "spend": "100000"}]
    assert used_tokens == ["dummy", "page_dummy"]


def test_find_active_campaigns_by_keywords_returns_sorted_matches() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/act_1/campaigns"
        return {
            "data": [
                {
                    "id": "camp_3",
                    "name": "ADS ThaiLan JCV140",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-05-18T09:00:00+0000",
                },
                {
                    "id": "camp_2",
                    "name": "ADS ThaiLan JCV140 JCA158",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-05-18T10:00:00+0000",
                },
                {
                    "id": "camp_1",
                    "name": "ADS ThaiLan JCV140 JCA158 extra",
                    "effective_status": "PAUSED",
                    "updated_time": "2026-05-18T11:00:00+0000",
                },
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    result = client.find_active_campaigns_by_keywords(["JCV140", "JCA158"])

    assert [item["id"] for item in result] == ["camp_2"]


def test_list_eligible_adsets_filters_active_and_paused() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/camp_123/adsets"
        return {
            "data": [
                {
                    "id": "adset_1",
                    "name": "A1",
                    "effective_status": "ACTIVE",
                    "updated_time": "2026-05-18T10:00:00+0000",
                    "destination_type": "MESSENGER",
                },
                {
                    "id": "adset_2",
                    "name": "A2",
                    "effective_status": "PAUSED",
                    "updated_time": "2026-05-18T09:00:00+0000",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                },
                {"id": "adset_3", "name": "A3", "effective_status": "ARCHIVED", "updated_time": "2026-05-18T11:00:00+0000"},
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    result = client.list_eligible_adsets("camp_123", max_count=20)

    assert [item["id"] for item in result] == ["adset_1", "adset_2"]
    assert result[0]["destination_type"] == "MESSENGER"
    assert result[1]["destination_type"] == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"


def test_list_eligible_adsets_raises_when_exceed_limit() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/camp_123/adsets"
        return {
            "data": [
                {"id": f"adset_{idx}", "name": f"A{idx}", "effective_status": "ACTIVE"}
                for idx in range(1, 23)
            ]
        }

    client._request = fake_request  # type: ignore[method-assign]
    with pytest.raises(ValidationError):
        client.list_eligible_adsets("camp_123", max_count=20)


def test_publish_ads_updates_only_ads() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    updated: list[tuple[str, str]] = []
    client.update_status = lambda entity_id, status: updated.append((entity_id, status))  # type: ignore[method-assign]

    client.publish_ads(["ad_1", "ad_2"])

    assert updated == [("ad_1", "ACTIVE"), ("ad_2", "ACTIVE")]


def test_rollback_tree_handles_creatives() -> None:
    client = MetaAdsClient(settings=_dummy_settings(), logger=logging.getLogger("test"))
    rolled_back: list[str] = []
    client._safe_delete_or_pause = lambda entity_id: rolled_back.append(entity_id)  # type: ignore[method-assign]

    client.rollback_tree("camp_1", ["adset_1"], ["ad_1", "ad_2"], ["cr_1", "cr_2"])

    assert rolled_back == ["ad_2", "ad_1", "cr_2", "cr_1", "adset_1", "camp_1"]
