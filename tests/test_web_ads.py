from dataclasses import replace
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from app.campaign_planner import build_messenger_media_campaign_plan, build_web_campaign_plan
from app.command_parser import (
    parse_web_budget,
    parse_web_cta,
    parse_web_media_url,
    parse_web_url,
    normalize_web_caption,
    try_parse_web_ads_form,
    try_parse_web_media_url_message,
)
from app.exceptions import ValidationError
from app.meta_ads_client import MetaAdsClient
from app.models import AudienceSlot, MessengerMediaAdsInput, PlannedCampaign, WebAdsInput
from app.approval_service import ApprovalService
from app.settings import Settings
from app.telegram_bot import TelegramAdsBot
from app.web_lifecycle import web_lifecycle_label


def _settings() -> Settings:
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
        pancake_access_token="",
        pancake_shop_id=0,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
        meta_pixel_id="pixel_1",
        new_web_ads_enabled=True,
    )


def _web_input(**updates: object) -> WebAdsInput:
    values: dict[str, object] = {
        "media_file_id": "file_1",
        "media_unique_id": "unique_1",
        "media_kind": "image",
        "media_filename": "creative.jpg",
        "caption": "Nội dung quảng cáo #JCA250 #JCQ221 #summer",
        "headline": "Bộ sưu tập mới",
        "cta_label": "Mua ngay",
        "cta_type": "SHOP_NOW",
        "destination_url": "https://example.com/san-pham",
        "budget_daily_vnd": 300000,
        "sku_override": "",
        "customer_lifecycle_strategy": "ALL_AUDIENCES",
    }
    values.update(updates)
    return WebAdsInput(**values)


def _audiences() -> dict[str, str]:
    return {
        "thoi_trang_saved_audience_id": "aud_1",
        "du_lich_saved_audience_id": "aud_2",
        "tiec_saved_audience_id": "aud_3",
    }


def _web_objective() -> dict[str, object]:
    return {
        "campaign_objective": "OUTCOME_SALES",
        "conversion_location": "WEBSITE",
        "result_goal": "MAXIMIZE_CONVERSIONS",
        "customer_lifecycle_options": {
            "ALL_AUDIENCES": "Thu hút lượt chuyển đổi từ tất cả đối tượng",
            "NEW_CUSTOMERS_ONLY": "Chinh phục khách hàng mới",
        },
        "billing_event": "IMPRESSIONS",
        "optimization_goal": "OFFSITE_CONVERSIONS",
        "campaign_payload_overrides": {},
        "adset_payload_overrides": {"destination_type": "UNDEFINED"},
        "creative_payload_overrides": {},
        "ad_payload_overrides": {},
    }


def test_web_form_parses_labeled_multiline_message() -> None:
    matched, form = try_parse_web_ads_form(
        """lên mới web
caption: Nội dung quảng cáo #JCA250 #JCQ221
tiêu đề: Bộ sưu tập mới
nút: Mua ngay
url: https://example.com/san-pham
ngân sách: 300.000
mã sản phẩm: JCA250_JCQ221""",
        {"cta_types": {"Mua ngay": "SHOP_NOW"}},
    )

    assert matched is True
    assert form is not None
    assert form["budget_daily_vnd"] == "300000"
    assert form["cta_type"] == "SHOP_NOW"
    assert form["sku_override"] == "JCA250_JCQ221"


def test_web_plan_appends_media_owner_at_end_of_ad_name() -> None:
    plan = build_web_campaign_plan(
        web_input=_web_input(media_owner="Studio JC"),
        post_fingerprint="web123",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )
    assert plan.audiences[0].ad_name.endswith("|summer|Studio JC")


def test_web_caption_preserves_blank_lines_and_normalizes_line_endings() -> None:
    matched, form = try_parse_web_ads_form(
        """lên mới web
caption: Dòng đầu\r\n\r\nDòng cuối #JCA250
tiêu đề: Tiêu đề
nút: Mua ngay
url: https://example.com
ngân sách: 300000"""
    )

    assert matched is True
    assert form is not None
    assert form["caption"] == "Dòng đầu\n\nDòng cuối\n#JCA250"
    assert normalize_web_caption("a\r\nb\r\nc") == "a\nb\nc"


def test_web_caption_restores_fixed_jc_sections_when_stored_as_one_line() -> None:
    caption = (
        "Main copy 🤍 #JCA146 #JCCV148 — Instagram: instagram.com/jenniechoo.th "
        "— Order online via inbox Facebook — Website: th.jenniechoo.com "
        "JENNIE CHOO | Made in Vietnam Forever be an extra-ordinary princess"
    )

    normalized = normalize_web_caption(caption)
    assert normalized == normalize_web_caption(normalized)
    assert normalized == (
        "Main copy 🤍\n"
        "#JCA146 #JCCV148\n\n"
        "— Instagram: instagram.com/jenniechoo.th\n"
        "— Order online via inbox Facebook\n"
        "— Website: th.jenniechoo.com\n\n"
        "JENNIE CHOO | Made in Vietnam\n"
        "Forever be an extra-ordinary princess"
    )


def test_web_caption_restores_styled_brand_signature_and_is_idempotent() -> None:
    caption = (
        "Main copy #JCA146 — Website: th.jenniechoo.com "
        "𝐉𝐄𝐍𝐍𝐈𝐄 𝐂𝐇𝐎𝐎 | Made in Vietnam "
        "𝑭𝒐𝒓𝒆𝒗𝒆𝒓 𝒃𝒆 𝒂𝒏 𝒆𝒙𝒕𝒓𝒂-𝒐𝒓𝒅𝒊𝒏𝒂𝒓𝒚 𝒑𝒓𝒊𝒏𝒄𝒆𝒔𝒔"
    )

    normalized = normalize_web_caption(caption)
    assert normalized == normalize_web_caption(normalized)
    assert normalized.endswith(
        "\n\n𝐉𝐄𝐍𝐍𝐈𝐄 𝐂𝐇𝐎𝐎 | Made in Vietnam\n"
        "𝑭𝒐𝒓𝒆𝒗𝒆𝒓 𝒃𝒆 𝒂𝒏 𝒆𝒙𝒕𝒓𝒂-𝒐𝒓𝒅𝒊𝒏𝒂𝒓𝒚 𝒑𝒓𝒊𝒏𝒄𝒆𝒔𝒔"
    )


def test_web_form_rejects_unsupported_cta() -> None:
    with pytest.raises(ValidationError, match="chưa được hỗ trợ"):
        try_parse_web_ads_form(
            """lên mới web
caption: #JCA250
tiêu đề: Mới
nút: Gọi ngay
url: https://example.com
ngân sách: 300000""",
            {"cta_types": {"Mua ngay": "SHOP_NOW"}},
        )


def test_web_conversation_parsers_accept_plain_answers_and_any_site_path() -> None:
    assert parse_web_cta("xem thêm", {"cta_types": {"Xem thêm": "LEARN_MORE"}}) == (
        "xem thêm",
        "LEARN_MORE",
    )
    assert parse_web_url("https://example.com/san-pham") == "https://example.com/san-pham"
    assert parse_web_url("http://example.com") == "http://example.com"
    assert parse_web_budget("300.000 VND") == 300000

    with pytest.raises(ValidationError, match="HTTP/HTTPS"):
        try_parse_web_ads_form(
            """lên mới web
caption: #JCA250
tiêu đề: Mới
nút: Mua ngay
url: ftp://example.com
ngân sách: 300000""",
            {"cta_types": {"Mua ngay": "SHOP_NOW"}},
        )


def test_web_media_size_error_handles_telegram_download_limit() -> None:
    assert TelegramAdsBot._is_telegram_file_too_large_error(RuntimeError("Bad Request: file is too big"))
    assert not TelegramAdsBot._is_telegram_file_too_large_error(RuntimeError("network timeout"))
    assert "20 MB" in TelegramAdsBot._web_media_size_error(20 * 1024 * 1024)


def test_web_media_url_parser_accepts_drive_link_and_caption() -> None:
    matched, payload = try_parse_web_media_url_message(
        """media: https://drive.google.com/file/d/drive_file_123/view?usp=sharing
caption: Nội dung quảng cáo #JCA250
"""
    )

    assert matched is True
    assert payload == {
        "media_url": "https://drive.google.com/file/d/drive_file_123/view?usp=sharing",
        "caption": "Nội dung quảng cáo #JCA250",
    }
    assert parse_web_media_url("https://drive.google.com/open?id=drive_file_123")

    inline_matched, inline_payload = try_parse_web_media_url_message(
        "media: https://drive.google.com/file/d/drive_file_123/view Caption: #JCA250"
    )
    assert inline_matched is True
    assert inline_payload == {
        "media_url": "https://drive.google.com/file/d/drive_file_123/view",
        "caption": "#JCA250",
    }


def test_web_media_url_parser_accepts_bare_drive_link() -> None:
    matched, payload = try_parse_web_media_url_message(
        "https://drive.google.com/file/d/1CNiXB7CU4SoVJk50Mueyr7yFuct84dWI/view?usp=drive_link"
    )

    assert matched is True
    assert payload == {
        "media_url": "https://drive.google.com/file/d/1CNiXB7CU4SoVJk50Mueyr7yFuct84dWI/view?usp=drive_link",
        "caption": "",
    }


def test_web_media_url_parser_rejects_non_drive_and_non_https_links() -> None:
    with pytest.raises(ValidationError, match="Google Drive"):
        parse_web_media_url("https://example.com/creative.mp4")
    with pytest.raises(ValidationError, match="HTTPS"):
        parse_web_media_url("http://drive.google.com/file/d/drive_file_123/view")
    assert TelegramAdsBot._build_web_video_thumbnail_url(
        "https://drive.google.com/file/d/drive_file_123/view"
    ) == "https://drive.google.com/thumbnail?id=drive_file_123&sz=w1200"


def test_web_media_url_download_uses_content_bytes_and_detects_image(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        url = "https://drive.usercontent.google.com/download?id=drive_file_123"
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": "3"}

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            assert chunk_size == 1024 * 1024
            return iter((b"\xff\xd8\xff",))

        def close(self) -> None:
            return None

    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append(url)
        assert kwargs["allow_redirects"] is False
        return FakeResponse()

    monkeypatch.setattr("app.telegram_bot.requests.get", fake_get)
    bot = TelegramAdsBot.__new__(TelegramAdsBot)
    bot.settings = _settings()

    content, media_kind, filename = bot._download_web_media_url(
        "https://drive.google.com/file/d/drive_file_123/view"
    )

    assert content == b"\xff\xd8\xff"
    assert media_kind == "image"
    assert filename == "creative.jpg"
    assert calls == [
        "https://drive.usercontent.google.com/download?id=drive_file_123&export=download&confirm=t"
    ]


def test_web_plan_uses_effective_sku_and_web_naming() -> None:
    plan = build_web_campaign_plan(
        web_input=_web_input(),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    assert plan.campaign_variant == "web"
    assert plan.campaign_name == "ADS:QUYET|MK:ThaiLan|JCA250_JCQ221|Web|Codex"
    assert len(plan.audiences) == 3
    assert all(slot.adset_name.endswith((" - Thời trang", " - Du lịch", " - Tiệc")) for slot in plan.audiences)
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:ThaiLan|SKU:JCA250_JCQ221|MED:Anh|summer"
    assert plan.message_template_name == ""


def test_web_plan_without_sku_uses_all() -> None:
    plan = build_web_campaign_plan(
        web_input=_web_input(caption="Free shipping on all orders."),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    assert plan.sku_code_text == "ALL"
    assert plan.campaign_name == "ADS:QUYET|MK:ThaiLan|ALL|Web|Codex"
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Anh"


def test_web_plan_rejects_mismatched_fallback_sku() -> None:
    with pytest.raises(ValueError, match="không khớp"):
        build_web_campaign_plan(
            web_input=_web_input(sku_override="JCV999"),
            post_fingerprint="fp",
            version=1,
            audiences_config=_audiences(),
            objective_config=_web_objective(),
        )


def test_web_plan_accepts_explicit_sku_source_after_confirmation() -> None:
    plan = build_web_campaign_plan(
        web_input=_web_input(sku_override="JCV999", sku_source="override"),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    assert plan.sku_code_text == "JCV999"
    assert plan.campaign_name == "ADS:QUYET|MK:ThaiLan|JCV999|Web|Codex"


def test_web_sku_confirmation_callbacks_are_parseable() -> None:
    approval = ApprovalService()

    assert approval.parse_callback("web_sku_caption:req_1").action == "web_sku_caption"
    assert approval.parse_callback("web_sku_override:req_1").action == "web_sku_override"
    assert approval.parse_callback("web_sku_cancel:req_1").action == "web_sku_cancel"


def test_web_lifecycle_callbacks_are_parseable() -> None:
    approval = ApprovalService()

    assert approval.parse_callback("web_lifecycle_all:req_1").action == "web_lifecycle_all"
    assert approval.parse_callback("web_lifecycle_new:req_1").action == "web_lifecycle_new"
    assert approval.parse_callback("web_lifecycle_cancel:req_1").action == "web_lifecycle_cancel"
    assert web_lifecycle_label("ALL_AUDIENCES") == "Thu hút lượt chuyển đổi từ tất cả đối tượng"


def test_web_creative_uses_image_hash_url_headline_and_cta() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "creative_1"}
    )
    plan = build_web_campaign_plan(
        web_input=_web_input(),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    creative_id = client.create_web_ad_creative(plan, plan.audiences[0], "hash_1")

    assert creative_id == "creative_1"
    assert captured["path"] == "/act_1/adcreatives"
    data = captured["data"]
    assert isinstance(data, dict)
    story = data["object_story_spec"]
    assert story["page_id"] == "61581440236157"
    assert story["link_data"]["image_hash"] == "hash_1"
    assert story["link_data"]["name"] == "Bộ sưu tập mới"
    assert story["link_data"]["call_to_action"] == {
        "type": "SHOP_NOW",
        "value": {"link": "https://example.com/san-pham"},
    }
    assert data["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}


def test_web_video_creative_uses_video_id_and_title() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "creative_2"}
    )
    web_input = _web_input(media_kind="video", media_filename="creative.mp4")
    plan = build_web_campaign_plan(
        web_input=web_input,
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    client.create_web_ad_creative(
        plan,
        plan.audiences[0],
        "video_1",
        "https://drive.google.com/thumbnail?id=drive_file_123&sz=w1200",
    )

    data = captured["data"]
    assert isinstance(data, dict)
    assert data["object_story_spec"]["video_data"]["video_id"] == "video_1"
    assert data["object_story_spec"]["video_data"]["title"] == "Bộ sưu tập mới"
    assert data["object_story_spec"]["video_data"]["image_url"] == (
        "https://drive.google.com/thumbnail?id=drive_file_123&sz=w1200"
    )

    client.create_web_ad_creative(
        plan,
        plan.audiences[0],
        "video_1",
        "https://drive.google.com/thumbnail?id=drive_file_123&sz=w1200",
        "thumbnail_hash_1",
    )
    data = captured["data"]
    assert isinstance(data, dict)
    assert data["object_story_spec"]["video_data"]["image_hash"] == "thumbnail_hash_1"
    assert "image_url" not in data["object_story_spec"]["video_data"]
    assert data["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}


def test_web_ad_disables_contextual_multi_ads() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "ad_1"}
    )
    plan = build_web_campaign_plan(
        web_input=_web_input(),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    ad_id = client.create_web_ad(plan, plan.audiences[0], "adset_1", "creative_1")

    assert ad_id == "ad_1"
    data = captured["data"]
    assert isinstance(data, dict)
    assert data["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}


def test_web_creative_includes_configured_instagram_identity() -> None:
    settings = replace(_settings(), meta_instagram_user_id="17841443328764553")
    client = MetaAdsClient(settings=settings, logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "creative_ig"}
    )
    plan = build_web_campaign_plan(
        web_input=_web_input(media_kind="video"),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    client.create_web_ad_creative(plan, plan.audiences[0], "video_1", thumbnail_image_hash="hash_1")

    data = captured["data"]
    assert isinstance(data, dict)
    story = data["object_story_spec"]
    assert story["page_id"] == "61581440236157"
    assert story["instagram_user_id"] == "17841443328764553"
    assert story["video_data"]["image_hash"] == "hash_1"


def test_update_ad_creative_only_updates_creative_reference() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {}
    )

    client.update_ad_creative("ad_1", "creative_2")

    assert captured == {
        "method": "POST",
        "path": "/ad_1",
        "data": {"creative": {"creative_id": "creative_2"}},
    }


def test_web_adset_matches_manual_sales_campaign_payload() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    client.get_saved_audience_targeting = lambda _id: {  # type: ignore[method-assign]
        "age_min": 20,
        "age_max": 65,
        "age_range": [20, 45],
        "genders": [2],
        "geo_locations": {"countries": ["TH"], "location_types": ["home", "recent"]},
    }
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "adset_1"}
    )
    plan = build_web_campaign_plan(
        web_input=_web_input(),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    adset_id = client.create_web_adset(plan, "campaign_1", plan.audiences[0])

    assert adset_id == "adset_1"
    data = captured["data"]
    assert isinstance(data, dict)
    assert data["status"] == "PAUSED"
    assert data["billing_event"] == "IMPRESSIONS"
    assert data["optimization_goal"] == "OFFSITE_CONVERSIONS"
    assert data["destination_type"] == "UNDEFINED"
    assert "existing_customer_budget_percentage" not in data
    assert "customer_lifecycle_strategy" not in data
    assert data["promoted_object"] == {"pixel_id": "pixel_1", "custom_event_type": "PURCHASE"}


def test_web_adset_does_not_misrepresent_all_audiences_as_customer_cap() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    client.get_saved_audience_targeting = lambda _id: {"geo_locations": {"countries": ["TH"]}}  # type: ignore[method-assign]
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "adset_1"}
    )
    objective = _web_objective()
    objective["adset_payload_overrides"] = {
        "destination_type": "UNDEFINED",
        "existing_customer_budget_percentage": 100,
        "customer_lifecycle_strategy": "NEW_CUSTOMERS_ONLY",
    }
    plan = build_web_campaign_plan(
        web_input=_web_input(customer_lifecycle_strategy="ALL_AUDIENCES"),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=objective,
    )

    client.create_web_adset(plan, "campaign_1", plan.audiences[0])

    data = captured["data"]
    assert isinstance(data, dict)
    assert "existing_customer_budget_percentage" not in data
    assert "customer_lifecycle_strategy" not in data


def test_web_adset_rejects_new_customer_mode_before_meta_request() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    client.get_saved_audience_targeting = lambda _id: {"geo_locations": {"countries": ["TH"]}}  # type: ignore[method-assign]
    request_called = False

    def fake_request(*args: object, **kwargs: object):
        nonlocal request_called
        request_called = True
        return {"id": "adset_1"}

    client._request = fake_request  # type: ignore[method-assign]
    plan = build_web_campaign_plan(
        web_input=_web_input(customer_lifecycle_strategy="NEW_CUSTOMERS_ONLY"),
        post_fingerprint="fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_web_objective(),
    )

    with pytest.raises(ValidationError, match="chưa hỗ trợ công khai"):
        client.create_web_adset(plan, "campaign_1", plan.audiences[0])
    assert request_called is False


def test_media_upload_extracts_image_hash_and_video_id() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    responses = iter(({"images": {"creative.jpg": {"hash": "hash_1"}}}, {"id": "video_1"}))
    client._multipart_request = lambda *args, **kwargs: next(responses)  # type: ignore[method-assign]

    assert client.upload_image_asset(b"\xff\xd8\xff") == "hash_1"
    assert client.upload_video_asset(b"\x00") == "video_1"


def test_large_video_upload_uses_resumable_meta_session() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    client._VIDEO_RESUMABLE_UPLOAD_THRESHOLD = 3
    request_calls: list[dict[str, object]] = []
    transfer_calls: list[dict[str, object]] = []

    def fake_request(method: str, path: str, *, params=None, data=None, access_token=None):
        request_calls.append({"method": method, "path": path, "data": data or {}})
        if data and data.get("upload_phase") == "start":
            return {
                "upload_session_id": "session_1",
                "video_id": "video_resumable_1",
                "start_offset": "0",
                "end_offset": "3",
            }
        return {"success": True}

    def fake_multipart(method: str, path: str, *, files, data=None, timeout_seconds=120, access_token=None):
        transfer_calls.append({"method": method, "path": path, "data": data or {}, "files": files})
        return {"start_offset": "6", "end_offset": "6"}

    client._request = fake_request  # type: ignore[method-assign]
    client._multipart_request = fake_multipart  # type: ignore[method-assign]

    assert client.upload_video_asset(b"abcdef", "large.mp4") == "video_resumable_1"
    assert request_calls[0]["data"] == {"upload_phase": "start", "file_size": 6}
    assert request_calls[-1]["data"] == {
        "upload_phase": "finish",
        "upload_session_id": "session_1",
        "title": "large.mp4",
    }
    assert transfer_calls[0]["data"] == {
        "upload_phase": "transfer",
        "upload_session_id": "session_1",
        "start_offset": 0,
    }


def test_pixel_preflight_rejects_missing_pixel() -> None:
    client = MetaAdsClient(settings=replace(_settings(), meta_pixel_id=""), logger=logging.getLogger("test"))
    with pytest.raises(ValidationError, match="META_PIXEL_ID"):
        client.ensure_pixel_can_access()


def _messenger_media_input(**updates: object) -> MessengerMediaAdsInput:
    values: dict[str, object] = {
        "media_file_id": "file_1",
        "media_unique_id": "unique_1",
        "media_kind": "image",
        "media_filename": "creative.jpg",
        "caption": "Nội dung Messenger #JCA250 #summer",
        "headline": "Bộ sưu tập mới",
        "budget_daily_vnd": 300000,
        "customer_lifecycle_strategy": "ALL_AUDIENCES",
        "media_owner": "Studio JC",
    }
    values.update(updates)
    return MessengerMediaAdsInput(**values)


def _message_objective() -> dict[str, object]:
    return {
        "campaign_objective": "OUTCOME_ENGAGEMENT",
        "conversion_location": "MESSAGING_DESTINATION",
        "result_goal": "MAXIMIZE_PURCHASES_VIA_MESSAGE",
        "message_template_name": "Chào JC",
        "campaign_payload_overrides": {},
        "adset_payload_overrides": {
            "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "MESSAGING_PURCHASE_CONVERSION",
            "promoted_object": {"page_id": "${META_PAGE_ID}", "smart_pse_enabled": False},
        },
        "creative_payload_overrides": {},
        "ad_payload_overrides": {},
    }


def _message_templates() -> dict[str, object]:
    return {"templates": {"Chào JC": {"creative_patch": {}, "adset_patch": {}, "ad_patch": {}}}}


def test_messenger_media_plan_uses_existing_naming_and_owner_suffix() -> None:
    plan = build_messenger_media_campaign_plan(
        media_input=_messenger_media_input(),
        post_fingerprint="messenger_fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_message_objective(),
        template_config=_message_templates(),
    )

    assert plan.campaign_variant == "messenger_media"
    assert plan.campaign_name == "ADS:QUYET|MK:ThaiLan|JCA250|Codex"
    assert plan.audiences[0].ad_name == "ADS:QUYET|MK:ThaiLan|SKU:JCA250|MED:Anh|summer|Studio JC"
    assert plan.cta_type == "MESSAGE_PAGE"
    assert plan.message_template_name == "Chào JC"
    assert len(plan.audiences) == 3


def test_messenger_media_photo_creative_uses_messenger_link_and_disables_multi_ads() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "creative_1"}
    )
    plan = build_messenger_media_campaign_plan(
        media_input=_messenger_media_input(),
        post_fingerprint="messenger_fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_message_objective(),
        template_config=_message_templates(),
    )

    assert client.create_messenger_media_ad_creative(plan, plan.audiences[0], "hash_1") == "creative_1"
    data = captured["data"]
    assert isinstance(data, dict)
    assert "object_story_id" not in data
    assert data["page_welcome_message"] == "Chào JC"
    assert data["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}
    story = data["object_story_spec"]
    assert story["link_data"] == {
        "image_hash": "hash_1",
        "link": "https://m.me/61581440236157",
        "message": "Nội dung Messenger\n#JCA250 #summer",
        "name": "Bộ sưu tập mới",
        "call_to_action": {"type": "MESSAGE_PAGE"},
    }
    assert "photo_data" not in story


def test_messenger_media_video_creative_uses_video_id_and_title() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    captured: dict[str, object] = {}
    client._request = lambda method, path, *, params=None, data=None, access_token=None: (  # type: ignore[method-assign]
        captured.update({"method": method, "path": path, "data": data}) or {"id": "creative_2"}
    )
    plan = build_messenger_media_campaign_plan(
        media_input=_messenger_media_input(media_kind="video", media_filename="creative.mp4"),
        post_fingerprint="messenger_fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_message_objective(),
        template_config=_message_templates(),
    )

    client.create_messenger_media_ad_creative(plan, plan.audiences[0], "video_1")
    data = captured["data"]
    assert isinstance(data, dict)
    video_data = data["object_story_spec"]["video_data"]
    assert video_data["video_id"] == "video_1"
    assert video_data["title"] == "Bộ sưu tập mới"
    assert video_data["call_to_action"] == {"type": "MESSAGE_PAGE"}
    assert "link" not in video_data


def test_messenger_media_adset_rejects_unsupported_lifecycle_before_request() -> None:
    client = MetaAdsClient(settings=_settings(), logger=logging.getLogger("test"))
    client.get_saved_audience_targeting = lambda _id: {"geo_locations": {"countries": ["TH"]}}  # type: ignore[method-assign]
    request_called = False

    def fake_request(*args: object, **kwargs: object):
        nonlocal request_called
        request_called = True
        return {"id": "adset_1"}

    client._request = fake_request  # type: ignore[method-assign]
    plan = build_messenger_media_campaign_plan(
        media_input=_messenger_media_input(customer_lifecycle_strategy="NEW_CUSTOMERS_ONLY"),
        post_fingerprint="messenger_fp",
        version=1,
        audiences_config=_audiences(),
        objective_config=_message_objective(),
        template_config=_message_templates(),
    )

    with pytest.raises(ValidationError, match="chưa hỗ trợ công khai"):
        client.create_messenger_media_adset(plan, "campaign_1", plan.audiences[0])
    assert request_called is False


def test_media_intake_command_and_flow_callbacks_are_parseable() -> None:
    assert TelegramAdsBot._is_media_intake_start("lên mới media")
    assert TelegramAdsBot._is_media_intake_start("/media_ads")
    assert TelegramAdsBot._is_media_intake_start("/mess")
    assert not TelegramAdsBot._is_media_intake_start("lên mới web")

    approval = ApprovalService()
    keyboard = approval.media_flow_keyboard("req_media")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == ["media_flow_web:req_media", "media_flow_messenger:req_media", "media_flow_cancel:req_media"]
    assert approval.parse_callback("media_flow_web:req_media").action == "media_flow_web"
    assert approval.parse_callback("media_flow_messenger:req_media").action == "media_flow_messenger"
    assert approval.parse_callback("media_flow_cancel:req_media").action == "media_flow_cancel"
