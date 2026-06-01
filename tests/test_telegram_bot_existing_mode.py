from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.approval_service import ApprovalService
from app.exceptions import MetaApiError, ValidationError
from app.models import AdsCommand, AudienceSlot, PlannedCampaign, ResolvedPost
from app.settings import Settings
from app.telegram_bot import TelegramAdsBot
import app.telegram_bot as telegram_bot_module


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
    )


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


class FakeStorage:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}
        self.saved_jobs: list[tuple[str, dict[str, Any]]] = []
        self.jobs: dict[str, tuple[str, dict[str, Any]]] = {}
        self.pending_seq = 0

    def create_pending_request(self, payload: dict[str, Any], request_type: str = "duplicate_confirm") -> str:
        self.pending_seq += 1
        request_id = f"req_{self.pending_seq}"
        record = dict(payload)
        record["request_type"] = request_type
        record["request_id"] = request_id
        self.pending[request_id] = record
        return request_id

    def delete_pending_request(self, request_id: str) -> None:
        self.pending.pop(request_id, None)

    def get_pending_request(self, request_id: str) -> dict[str, Any] | None:
        return self.pending.get(request_id)

    def generate_job_id(self) -> str:
        return "job_test_1"

    def save_job(self, payload: dict[str, Any], status: str = "pending") -> None:
        saved = dict(payload)
        saved["status"] = status
        self.saved_jobs.append((status, saved))
        self.jobs[str(saved["job_id"])] = (status, saved)

    def find_job(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        return self.jobs.get(job_id)

    def move_job_status(
        self,
        job_id: str,
        from_status: str,  # noqa: ARG002
        to_status: str,
        extra_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, payload = self.jobs[job_id]
        updated = dict(payload)
        updated["status"] = to_status
        if extra_updates:
            updated.update(extra_updates)
        self.jobs[job_id] = (to_status, updated)
        return updated


class FakeMeta:
    def __init__(self) -> None:
        self.publish_ads_calls: list[list[str]] = []
        self.publish_tree_calls: list[tuple[str, list[str], list[str]]] = []
        self.campaign_candidates: list[dict[str, str]] = []
        self.find_active_campaigns_calls: list[list[str]] = []

    def resolve_post(self, post_url: str) -> ResolvedPost:
        return ResolvedPost(
            post_id="123",
            page_id="61581440236157",
            permalink_url=post_url,
            object_story_id="61581440236157_123",
            strategy="direct",
            message_text="Noi dung #JCV140",
            media_label="Anh",
        )

    def ensure_ads_token_can_access_page(self) -> None:
        return None

    def find_active_campaigns_by_keywords(self, keywords: list[str]) -> list[dict[str, str]]:  # noqa: ARG002
        self.find_active_campaigns_calls.append(list(keywords))
        return list(self.campaign_candidates)

    def publish_ads(self, ad_ids: list[str]) -> None:
        self.publish_ads_calls.append(list(ad_ids))

    def publish_tree(self, campaign_id: str, adset_ids: list[str], ad_ids: list[str]) -> None:
        self.publish_tree_calls.append((campaign_id, list(adset_ids), list(ad_ids)))

    @staticmethod
    def is_auto_destination_error(error_message: str) -> bool:
        return "không tương thích với mục tiêu của chiến dịch" in str(error_message).lower()

    @staticmethod
    def is_instagram_media_requirement_error(error_message: str) -> bool:
        return "không có hình ảnh hoặc video" in str(error_message).lower()

    @staticmethod
    def is_link_ad_cta_locked_error(error_message: str) -> bool:
        message = str(error_message).lower()
        return "đang chạy quảng cáo liên kết" in message or "running link ads" in message

    @staticmethod
    def is_post_not_advertisable_error(error_message: str) -> bool:
        message = str(error_message).lower()
        return (
            "bài viết này không thể đưa vào quảng cáo được" in message
            or "quảng cáo bài viết không hợp lệ" in message
            or "this post cannot be used for an ad" in message
        )

    def find_latest_ad_by_story_ids(
        self,
        story_ids: list[str],  # noqa: ARG002
        *,
        adset_id: str | None = None,  # noqa: ARG002
        max_ads_scan: int = 800,  # noqa: ARG002
    ) -> dict[str, str] | None:
        return None

    def duplicate_ad_from_source(
        self,
        source_ad_id: str,  # noqa: ARG002
        target_ad_name: str | None = None,  # noqa: ARG002
        *,
        target_adset_id: str | None = None,  # noqa: ARG002
        status_option: str = "PAUSED",  # noqa: ARG002
    ) -> str:
        raise MetaApiError("FakeMeta chua cau hinh duplicate fallback.")


class FakePancakeTdSync:
    def __init__(self, report: dict[str, Any] | None = None) -> None:
        self.report = report or {"ok": True, "created": 0, "failed": 0, "notify": False}
        self.sync_today_calls = 0
        self.sync_order_code_calls: list[str] = []
        self.build_calls: list[tuple[dict[str, Any], str]] = []

    def sync_today_manual(self) -> dict[str, Any]:
        self.sync_today_calls += 1
        return dict(self.report)

    def sync_order_code_manual(self, order_code: str) -> dict[str, Any]:
        self.sync_order_code_calls.append(str(order_code))
        report = dict(self.report)
        report["manual_order_code"] = str(order_code)
        return report

    def build_message(self, report: dict[str, Any], trigger_label: str = "") -> str:
        self.build_calls.append((dict(report), trigger_label))
        return f"{trigger_label}\nTổng quan: {'OK' if report.get('ok') else 'LỖI'}"


class FakeRollback:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[str], list[str], list[str] | None]] = []

    def rollback(
        self,
        campaign_id: str | None,
        adset_ids: list[str],
        ad_ids: list[str],
        creative_ids: list[str] | None = None,
    ) -> None:
        self.calls.append((campaign_id, list(adset_ids), list(ad_ids), list(creative_ids or [])))


class FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=1)
        self.reply_markup = "x"
        self.answers: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:  # noqa: ANN001
        self.reply_markup = reply_markup


class FakeQuery:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=1)
        self.message = FakeMessage()
        self.answer_calls: list[str] = []
        self.data = ""

    async def answer(self, text: str = "", show_alert: bool = False) -> None:  # noqa: ARG002
        self.answer_calls.append(text)


def _build_bot(meta: FakeMeta, storage: FakeStorage, rollback: FakeRollback) -> TelegramAdsBot:
    settings = replace(_dummy_settings(), telegram_allowed_user_id=1)
    logger = logging.getLogger("telegram_bot_test")
    dedup = SimpleNamespace(inspect=lambda *_args, **_kwargs: {})
    reports = SimpleNamespace(generate_report=lambda *_args, **_kwargs: {}, default_report_date=lambda: None, build_message=lambda *_a, **_k: "")
    bot = TelegramAdsBot(
        settings=settings,
        logger=logger,
        storage=storage,
        dedup=dedup,
        meta_client=meta,
        daily_report_service=reports,
        approval_service=ApprovalService(),
        rollback_service=rollback,
    )
    bot._bot = FakeBot()
    bot._bot_username = "testbot"
    return bot


def test_existing_mode_no_campaign_match_sends_error() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
    )
    asyncio.run(
        bot._start_existing_campaign_draft_flow(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
        )
    )

    assert bot._bot is not None
    assert "Không tìm thấy campaign ACTIVE" in bot._bot.messages[-1]["text"]
    assert storage.saved_jobs[-1][1]["campaign_mode"] == "existing"


def test_existing_mode_campaign_hint_uses_hint_keywords_and_fail_fast() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._start_existing_campaign_draft_flow(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
        )
    )

    assert meta.find_active_campaigns_calls == [["VIDEO"]]
    assert bot._bot is not None
    assert "camp video" in bot._bot.messages[-1]["text"]


def test_existing_mode_multiple_campaign_match_creates_selection_request() -> None:
    meta = FakeMeta()
    meta.campaign_candidates = [
        {"id": "camp_1", "name": "Camp JCV140 1", "updated_time": "2026-05-18T10:00:00+0000"},
        {"id": "camp_2", "name": "Camp JCV140 2", "updated_time": "2026-05-18T09:00:00+0000"},
    ]
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
    )
    asyncio.run(
        bot._start_existing_campaign_draft_flow(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
        )
    )

    assert len(storage.pending) == 1
    pending = next(iter(storage.pending.values()))
    assert pending["request_type"] == "existing_campaign_select"
    assert len(pending["campaign_candidates"]) == 2
    assert bot._bot is not None
    assert "Tìm thấy nhiều campaign ACTIVE" in bot._bot.messages[-1]["text"]


def test_existing_mode_multiple_campaign_match_persists_campaign_hint() -> None:
    meta = FakeMeta()
    meta.campaign_candidates = [
        {"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video", "updated_time": "2026-05-18T10:00:00+0000"},
        {"id": "camp_2", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video V2", "updated_time": "2026-05-18T09:00:00+0000"},
    ]
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._start_existing_campaign_draft_flow(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
        )
    )

    pending = next(iter(storage.pending.values()))
    assert pending["existing_campaign_hint"] == "video"
    assert pending["campaign_match_keywords"] == ["VIDEO"]


def test_campaign_pick_restores_campaign_hint_from_pending_request() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    captured: dict[str, Any] = {}

    async def _capture_create_existing(  # noqa: ANN202
        chat_id: int,
        command: AdsCommand,
        post_fingerprint: str,
        version: int,
        selected_campaign: dict[str, str],
        campaign_keywords: list[str],
        request_id: str | None = None,
    ) -> None:
        captured["chat_id"] = chat_id
        captured["hint"] = command.existing_campaign_hint
        captured["keywords"] = list(campaign_keywords)
        captured["campaign_id"] = selected_campaign.get("id")
        captured["request_id"] = request_id
        _ = post_fingerprint, version

    bot._create_existing_campaign_draft_and_send_review = _capture_create_existing  # type: ignore[method-assign]

    request_id = storage.create_pending_request(
        {
            "post_url": "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
            "budget_daily_vnd": 300000,
            "post_fingerprint": "fp_1",
            "version": 2,
            "use_existing_campaign": True,
            "manual_sku_keywords": [],
            "existing_campaign_hint": "video",
            "campaign_match_keywords": ["VIDEO"],
            "campaign_candidates": [
                {"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video", "updated_time": "2026-05-18T10:00:00+0000"}
            ],
            "campaign_candidate_total": 1,
        },
        "existing_campaign_select",
    )

    query = FakeQuery()
    asyncio.run(bot._on_campaign_pick(query, request_id, 0))

    assert captured["hint"] == "video"
    assert captured["keywords"] == ["VIDEO"]
    assert captured["campaign_id"] == "camp_1"
    assert captured["request_id"] == request_id


def test_existing_mode_campaign_hint_uses_sku_all_in_ad_name() -> None:
    class HintSkuMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.ad_names: list[str] = []

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Video",
                    "destination_type": "MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override, extra_payload_overrides
            return "cr_1"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, destination_type_override
            self.ad_names.append(str(slot.ad_name))
            return "ad_1"

    meta = HintSkuMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.ad_names == ["ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Anh|ADSET:adset_1"]
    assert storage.saved_jobs[-1][0] == "pending"
    assert storage.saved_jobs[-1][1]["sku_code_text"] == "ALL"


def test_existing_mode_draft_never_mutates_campaign_or_adset() -> None:
    class NonMutatingMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.created_ads = 0
            self.created_creatives = 0

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Existing",
                    "destination_type": "MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def create_adset(self, *args, **kwargs):  # noqa: ANN001, ANN002, D401
            raise AssertionError("Existing mode draft khong duoc tao adset moi.")

        def update_status(self, *args, **kwargs):  # noqa: ANN001, ANN002, D401
            raise AssertionError("Existing mode draft khong duoc doi status campaign/adset.")

        def publish_tree(self, campaign_id: str, adset_ids: list[str], ad_ids: list[str]) -> None:  # noqa: ARG002
            raise AssertionError("Existing mode khong duoc publish tree.")

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override, extra_payload_overrides
            self.created_creatives += 1
            return "cr_1"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, destination_type_override
            self.created_ads += 1
            return "ad_1"

    meta = NonMutatingMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "Camp Existing"},
            campaign_keywords=["JCV140"],
        )
    )

    assert meta.created_creatives == 1
    assert meta.created_ads == 1
    assert storage.saved_jobs[-1][0] == "pending"
    assert storage.saved_jobs[-1][1]["campaign_mode"] == "existing"
    assert storage.saved_jobs[-1][1]["publish_scope"] == "ads_only"
    assert storage.saved_jobs[-1][1]["adset_ids"] == []


def test_approve_existing_mode_publishes_ads_only() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    storage.jobs["job_1"] = (
        "pending",
        {
            "job_id": "job_1",
            "status": "pending",
            "campaign_mode": "existing",
            "publish_scope": "ads_only",
            "campaign_id": "camp_1",
            "adset_ids": [],
            "ad_ids": ["ad_1", "ad_2"],
            "creative_ids": ["cr_1", "cr_2"],
            "ads_manager_url": "https://adsmanager.facebook.com",
        },
    )

    query = FakeQuery()
    asyncio.run(bot._on_approve(query, "job_1"))

    assert meta.publish_ads_calls == [["ad_1", "ad_2"]]
    assert meta.publish_tree_calls == []


def test_reject_existing_mode_rolls_back_ads_and_creatives_only() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    storage.jobs["job_1"] = (
        "pending",
        {
            "job_id": "job_1",
            "status": "pending",
            "campaign_mode": "existing",
            "publish_scope": "ads_only",
            "campaign_id": "camp_1",
            "adset_ids": [],
            "ad_ids": ["ad_1"],
            "creative_ids": ["cr_1"],
            "ads_manager_url": "https://adsmanager.facebook.com",
        },
    )

    query = FakeQuery()
    asyncio.run(bot._on_reject(query, "job_1"))

    assert rollback.calls == [(None, [], ["ad_1"], ["cr_1"])]


def test_approve_new_mode_still_publishes_tree() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    storage.jobs["job_2"] = (
        "pending",
        {
            "job_id": "job_2",
            "status": "pending",
            "campaign_mode": "new",
            "publish_scope": "tree",
            "campaign_id": "camp_2",
            "adset_ids": ["adset_1"],
            "ad_ids": ["ad_1"],
            "creative_ids": ["cr_1"],
            "ads_manager_url": "https://adsmanager.facebook.com",
        },
    )

    query = FakeQuery()
    asyncio.run(bot._on_approve(query, "job_2"))

    assert meta.publish_tree_calls == [("camp_2", ["adset_1"], ["ad_1"])]


def test_existing_mode_fallbacks_to_account_asset_feed_spec_when_adset_spec_missing() -> None:
    class FallbackMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.account_spec_calls = 0
            self.creative_overrides: list[dict[str, Any] | None] = []
            self.account_spec = {"optimization_type": "DOF_MESSAGING_DESTINATION", "source": "account"}

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            raise ValidationError("Khong co asset_feed_spec trong adset")

        def get_account_multi_destination_asset_feed_spec(self, max_ads_scan: int = 200) -> dict[str, Any]:  # noqa: ARG002
            self.account_spec_calls += 1
            return dict(self.account_spec)

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override
            self.creative_overrides.append(extra_payload_overrides)
            return "cr_1"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, destination_type_override
            return "ad_1"

    meta = FallbackMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "Camp JCV140"},
            campaign_keywords=["JCV140"],
        )
    )

    assert meta.account_spec_calls == 1
    assert len(meta.creative_overrides) == 1
    assert meta.creative_overrides[0] == {"asset_feed_spec": meta.account_spec}
    assert storage.saved_jobs[-1][0] == "pending"
    assert storage.saved_jobs[-1][1]["ad_ids"] == ["ad_1"]


def test_existing_mode_retries_with_account_asset_feed_spec_after_objective_mismatch() -> None:
    class RetryMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.create_ad_calls = 0
            self.account_spec_calls = 0
            self.creative_overrides: list[dict[str, Any] | None] = []
            self.adset_spec = {"optimization_type": "DOF_MESSAGING_DESTINATION", "source": "adset"}
            self.account_spec = {"optimization_type": "DOF_MESSAGING_DESTINATION", "source": "account"}

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            return dict(self.adset_spec)

        def get_account_multi_destination_asset_feed_spec(self, max_ads_scan: int = 200) -> dict[str, Any]:  # noqa: ARG002
            self.account_spec_calls += 1
            return dict(self.account_spec)

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override
            self.creative_overrides.append(extra_payload_overrides)
            return f"cr_{len(self.creative_overrides)}"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, destination_type_override
            self.create_ad_calls += 1
            if self.create_ad_calls == 1:
                raise MetaApiError(
                    "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
                )
            return "ad_2"

    meta = RetryMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/permalink.php?story_fbid=123&id=61581440236157",
        budget_daily_vnd=300000,
        use_existing_campaign=True,
        manual_sku_keywords=["JCV140"],
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "Camp JCV140"},
            campaign_keywords=["JCV140"],
        )
    )

    assert meta.account_spec_calls == 1
    assert meta.create_ad_calls == 2
    assert meta.creative_overrides == [
        {"asset_feed_spec": meta.adset_spec},
        {"asset_feed_spec": meta.account_spec},
    ]
    assert rollback.calls == [(None, [], [], ["cr_1"])]
    assert storage.saved_jobs[-1][0] == "pending"
    assert storage.saved_jobs[-1][1]["ad_ids"] == ["ad_2"]


def test_existing_mode_fallbacks_to_messenger_when_instagram_media_requirement_fails() -> None:
    class MediaFallbackMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.creative_calls = 0
            self.creative_destinations: list[str | None] = []
            self.creative_overrides: list[dict[str, Any] | None] = []
            self.ad_destinations: list[str | None] = []
            self.adset_spec = {"optimization_type": "DOF_MESSAGING_DESTINATION", "source": "adset"}

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            return dict(self.adset_spec)

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post
            self.creative_calls += 1
            self.creative_destinations.append(destination_type_override)
            self.creative_overrides.append(extra_payload_overrides)
            if self.creative_calls == 1:
                raise MetaApiError(
                    "Meta API loi (400): Bài viết của bạn không có hình ảnh hoặc video. "
                    "Quảng cáo trên Instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết."
                )
            return "cr_2"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot
            self.ad_destinations.append(destination_type_override)
            if destination_type_override == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                raise MetaApiError(
                    "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
                )
            return "ad_2"

    meta = MediaFallbackMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.creative_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert meta.creative_overrides == [{"asset_feed_spec": meta.adset_spec}, None]
    assert meta.ad_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert rollback.calls == []
    assert storage.saved_jobs[-1][0] == "pending"
    assert storage.saved_jobs[-1][1]["active_destination_type"] == "MESSENGER"
    assert "không có hình ảnh hoặc video" in storage.saved_jobs[-1][1]["destination_fallback_reason"]


def test_existing_mode_fallbacks_to_messenger_when_creative_post_invalid_on_multi_destination() -> None:
    class PostInvalidFallbackMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.creative_destinations: list[str] = []
            self.creative_overrides: list[dict[str, Any] | None] = []
            self.ad_destinations: list[str] = []

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            return {"optimization_type": "DOF_MESSAGING_DESTINATION"}

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post
            destination = str(destination_type_override or "MESSAGING_INSTAGRAM_DIRECT_MESSENGER").strip().upper()
            self.creative_destinations.append(destination)
            self.creative_overrides.append(extra_payload_overrides if extra_payload_overrides else None)
            if destination == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                raise MetaApiError(
                    "Meta API loi (400): Bạn đang quảng cáo bài viết không hợp lệ nên không tạo được quảng cáo."
                )
            return "cr_post_invalid_fallback"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot
            destination = str(destination_type_override or "MESSAGING_INSTAGRAM_DIRECT_MESSENGER").strip().upper()
            self.ad_destinations.append(destination)
            if destination == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                raise MetaApiError(
                    "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
                )
            return "ad_post_invalid_fallback"

    meta = PostInvalidFallbackMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.creative_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert meta.creative_overrides == [{"asset_feed_spec": {"optimization_type": "DOF_MESSAGING_DESTINATION"}}, None]
    assert meta.ad_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert rollback.calls == []
    assert storage.saved_jobs[-1][0] == "pending"
    payload = storage.saved_jobs[-1][1]
    assert payload["active_destination_type"] == "MESSENGER"
    assert "quảng cáo bài viết không hợp lệ" in payload["destination_fallback_reason"].lower()


def test_existing_mode_retries_without_asset_feed_spec_when_link_ad_cta_locked() -> None:
    class LinkAdCtaLockedMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.creative_destinations: list[str] = []
            self.creative_overrides: list[dict[str, Any] | None] = []
            self.ad_destinations: list[str] = []

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            return {
                "call_to_actions": [
                    {"type": "MESSAGE_PAGE"},
                    {"type": "INSTAGRAM_MESSAGE"},
                ],
                "optimization_type": "DOF_MESSAGING_DESTINATION",
            }

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post
            destination = str(destination_type_override or "MESSAGING_INSTAGRAM_DIRECT_MESSENGER").strip().upper()
            self.creative_destinations.append(destination)
            if isinstance(extra_payload_overrides, dict):
                self.creative_overrides.append(dict(extra_payload_overrides))
            else:
                self.creative_overrides.append(None)
            if isinstance(extra_payload_overrides, dict) and "asset_feed_spec" in extra_payload_overrides:
                raise MetaApiError(
                    "Meta API loi (400): Bài viết này đang chạy quảng cáo liên kết, do đó bạn chưa thể chỉnh sửa nút kêu gọi hành động"
                )
            return "cr_retry_no_asset_spec"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot
            destination = str(destination_type_override or "MESSAGING_INSTAGRAM_DIRECT_MESSENGER").strip().upper()
            self.ad_destinations.append(destination)
            return "ad_retry_no_asset_spec"

    meta = LinkAdCtaLockedMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.creative_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
    ]
    assert meta.creative_overrides == [
        {
            "asset_feed_spec": {
                "call_to_actions": [
                    {"type": "MESSAGE_PAGE"},
                    {"type": "INSTAGRAM_MESSAGE"},
                ],
                "optimization_type": "DOF_MESSAGING_DESTINATION",
            }
        },
        None,
    ]
    assert meta.ad_destinations == ["MESSAGING_INSTAGRAM_DIRECT_MESSENGER"]
    assert rollback.calls == []
    assert storage.saved_jobs[-1][0] == "pending"
    payload = storage.saved_jobs[-1][1]
    assert payload["active_destination_type"] == "INHERIT_ADSET"
    assert payload["ad_ids"] == ["ad_retry_no_asset_spec"]


def test_existing_mode_fallbacks_to_duplicate_source_ad_when_messenger_retry_still_fails() -> None:
    class DuplicateFallbackMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.creative_calls = 0
            self.ad_calls = 0
            self.duplicate_calls: list[tuple[str, str | None, str | None, str]] = []

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Multi",
                    "destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def get_multi_destination_asset_feed_spec(self, adset_id: str, max_ads_scan: int = 20) -> dict[str, Any]:  # noqa: ARG002
            return {"optimization_type": "DOF_MESSAGING_DESTINATION"}

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override, extra_payload_overrides
            self.creative_calls += 1
            if self.creative_calls == 1:
                raise MetaApiError(
                    "Meta API loi (400): Bài viết của bạn không có hình ảnh hoặc video. "
                    "Quảng cáo trên Instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết."
                )
            return "cr_2"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, destination_type_override
            self.ad_calls += 1
            raise MetaApiError(
                "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
            )

        def find_latest_ad_by_story_ids(
            self,
            story_ids: list[str],  # noqa: ARG002
            *,
            adset_id: str | None = None,  # noqa: ARG002
            max_ads_scan: int = 800,  # noqa: ARG002
        ) -> dict[str, str] | None:
            return {
                "id": "120249992082570728",
                "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Video|ADSET:adset_1",
            }

        def duplicate_ad_from_source(
            self,
            source_ad_id: str,
            target_ad_name: str | None = None,
            *,
            target_adset_id: str | None = None,
            status_option: str = "PAUSED",
        ) -> str:
            self.duplicate_calls.append((source_ad_id, target_ad_name, target_adset_id, status_option))
            return "ad_copy_1"

    meta = DuplicateFallbackMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.ad_calls == 2
    assert meta.duplicate_calls == [
        ("120249992082570728", "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Anh|ADSET:adset_1", "adset_1", "PAUSED")
    ]
    assert rollback.calls == [(None, [], [], ["cr_2"])]
    assert storage.saved_jobs[-1][0] == "pending"
    payload = storage.saved_jobs[-1][1]
    assert payload["ad_ids"] == ["ad_copy_1"]
    assert payload["creative_ids"] == []
    assert payload["active_destination_type"] == "MESSENGER"


def test_existing_mode_creative_post_not_advertisable_uses_duplicate_fallback() -> None:
    class CreativePostBlockedMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.duplicate_calls: list[tuple[str, str | None, str | None, str]] = []

        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Existing",
                    "destination_type": "MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override, extra_payload_overrides
            raise MetaApiError(
                "Meta API loi (400): Bạn đang quảng cáo bài viết không hợp lệ nên không tạo được quảng cáo."
            )

        def find_latest_ad_by_story_ids(
            self,
            story_ids: list[str],  # noqa: ARG002
            *,
            adset_id: str | None = None,  # noqa: ARG002
            max_ads_scan: int = 800,  # noqa: ARG002
        ) -> dict[str, str] | None:
            return {
                "id": "120249992082570728",
                "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Anh|ADSET:adset_1",
            }

        def duplicate_ad_from_source(
            self,
            source_ad_id: str,
            target_ad_name: str | None = None,
            *,
            target_adset_id: str | None = None,
            status_option: str = "PAUSED",
        ) -> str:
            self.duplicate_calls.append((source_ad_id, target_ad_name, target_adset_id, status_option))
            return "ad_copy_creative_blocked"

    meta = CreativePostBlockedMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert meta.duplicate_calls == [
        ("120249992082570728", "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Anh|ADSET:adset_1", "adset_1", "PAUSED")
    ]
    assert rollback.calls == []
    assert storage.saved_jobs[-1][0] == "pending"
    payload = storage.saved_jobs[-1][1]
    assert payload["ad_ids"] == ["ad_copy_creative_blocked"]
    assert payload["creative_ids"] == []


def test_new_mode_fallbacks_to_messenger_when_instagram_media_requirement_fails(monkeypatch) -> None:  # noqa: ANN001
    class NewModeFallbackMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.adset_destinations: list[str] = []
            self.creative_destinations: list[str] = []
            self.ad_destinations: list[str] = []
            self.ad_destination_overrides: list[str | None] = []
            self.created_adset_seq = 0

        @staticmethod
        def effective_destination_type(plan: PlannedCampaign) -> str:
            overrides = plan.raw.get("adset_payload_overrides", {}) if isinstance(plan.raw, dict) else {}
            if isinstance(overrides, dict):
                value = str(overrides.get("destination_type", "")).strip().upper()
                if value:
                    return value
            return "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"

        def get_account_multi_destination_asset_feed_spec(self, max_ads_scan: int = 200) -> dict[str, Any]:  # noqa: ARG002
            return {"optimization_type": "DOF_MESSAGING_DESTINATION"}

        def create_campaign(self, plan: PlannedCampaign) -> str:  # noqa: ARG002
            return "camp_new_1"

        def create_adset(self, plan: PlannedCampaign, campaign_id: str, slot: AudienceSlot) -> str:  # noqa: ARG002
            self.created_adset_seq += 1
            self.adset_destinations.append(self.effective_destination_type(plan))
            return f"adset_{self.created_adset_seq}"

        def create_ad_creative(
            self,
            plan: PlannedCampaign,
            slot: AudienceSlot,  # noqa: ARG002
            resolved_post: ResolvedPost,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001, ARG002
        ) -> str:
            destination = str(destination_type_override or self.effective_destination_type(plan)).strip().upper()
            self.creative_destinations.append(destination)
            if destination == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                raise MetaApiError(
                    "Meta API loi (400): Bài viết của bạn không có hình ảnh hoặc video. "
                    "Quảng cáo trên Instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết."
                )
            return "cr_new_1"

        def create_ad(
            self,
            plan: PlannedCampaign,
            slot: AudienceSlot,  # noqa: ARG002
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            destination = str(destination_type_override or self.effective_destination_type(plan)).strip().upper()
            self.ad_destinations.append(destination)
            self.ad_destination_overrides.append(None if destination_type_override is None else str(destination_type_override))
            if destination == "MESSENGER" and destination_type_override is None:
                raise MetaApiError(
                    "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
                )
            return "ad_new_1"

    meta = NewModeFallbackMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    fake_plan = PlannedCampaign(
        version=5,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCA240_JCCV241|Codex",
        sku_code_text="JCA240_JCCV241",
        media_label="Video",
        post_url="https://www.facebook.com/reel/2992172994319258",
        post_fingerprint="fp_new_mode",
        budget_daily_vnd=125479,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        audiences=[
            AudienceSlot(
                key="slot_1",
                label="Thoi trang",
                suffix="TT",
                saved_audience_id="aud_1",
                adset_name="Adset New 1",
                ad_name="Ad New 1",
            )
        ],
        raw={"adset_payload_overrides": {"destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"}},
    )

    monkeypatch.setattr(telegram_bot_module, "load_json", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(telegram_bot_module, "build_campaign_plan", lambda **_kwargs: fake_plan)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/2992172994319258",
        budget_daily_vnd=125479,
    )

    asyncio.run(
        bot._create_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_new_mode",
            version=5,
        )
    )

    assert meta.adset_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert meta.creative_destinations == [
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSENGER",
    ]
    assert meta.ad_destinations == ["MESSENGER", "MESSENGER"]
    assert meta.ad_destination_overrides == [None, "MESSENGER"]
    assert rollback.calls == [(None, ["adset_1"], [], [])]
    assert storage.saved_jobs[-1][0] == "pending"
    payload = storage.saved_jobs[-1][1]
    assert payload["campaign_mode"] == "new"
    assert payload["active_destination_type"] == "MESSENGER"
    assert "không có hình ảnh hoặc video" in payload["destination_fallback_reason"]


def test_new_mode_fallback_retry_failure_rolls_back_fallback_adset_and_creative(monkeypatch) -> None:  # noqa: ANN001
    class NewModeFallbackRetryFailMeta(FakeMeta):
        def __init__(self) -> None:
            super().__init__()
            self.created_adset_seq = 0

        @staticmethod
        def effective_destination_type(plan: PlannedCampaign) -> str:
            overrides = plan.raw.get("adset_payload_overrides", {}) if isinstance(plan.raw, dict) else {}
            if isinstance(overrides, dict):
                value = str(overrides.get("destination_type", "")).strip().upper()
                if value:
                    return value
            return "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"

        def create_campaign(self, plan: PlannedCampaign) -> str:  # noqa: ARG002
            return "camp_new_fail_1"

        def get_account_multi_destination_asset_feed_spec(self, max_ads_scan: int = 200) -> dict[str, Any]:  # noqa: ARG002
            return {"optimization_type": "DOF_MESSAGING_DESTINATION"}

        def create_adset(self, plan: PlannedCampaign, campaign_id: str, slot: AudienceSlot) -> str:  # noqa: ARG002
            self.created_adset_seq += 1
            return f"adset_fail_{self.created_adset_seq}"

        def create_ad_creative(
            self,
            plan: PlannedCampaign,
            slot: AudienceSlot,  # noqa: ARG002
            resolved_post: ResolvedPost,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001, ARG002
        ) -> str:
            destination = str(destination_type_override or self.effective_destination_type(plan)).strip().upper()
            if destination == "MESSAGING_INSTAGRAM_DIRECT_MESSENGER":
                raise MetaApiError(
                    "Meta API loi (400): Bài viết của bạn không có hình ảnh hoặc video. "
                    "Quảng cáo trên Instagram hiện chỉ hỗ trợ bài viết video, ảnh và liên kết."
                )
            return "cr_new_fail_1"

        def create_ad(
            self,
            plan: PlannedCampaign,
            slot: AudienceSlot,  # noqa: ARG002
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001, ARG002
        ) -> str:
            _ = plan, destination_type_override
            raise MetaApiError(
                "Meta API loi (400): Nội dung quảng cáo không tương thích với mục tiêu của chiến dịch chứa quảng cáo đó."
            )

    meta = NewModeFallbackRetryFailMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    fake_plan = PlannedCampaign(
        version=5,
        campaign_name="ADS:QUYET|MK:ThaiLan|JCA240_JCCV241|Codex",
        sku_code_text="JCA240_JCCV241",
        media_label="Video",
        post_url="https://www.facebook.com/reel/2992172994319258",
        post_fingerprint="fp_new_mode_fail",
        budget_daily_vnd=125479,
        objective="OUTCOME_ENGAGEMENT",
        conversion_location="MESSAGING_DESTINATION",
        result_goal="MAXIMIZE_PURCHASES_VIA_MESSAGE",
        message_template_name="Chào JC",
        audiences=[
            AudienceSlot(
                key="slot_1",
                label="Thoi trang",
                suffix="TT",
                saved_audience_id="aud_1",
                adset_name="Adset New Fail 1",
                ad_name="Ad New Fail 1",
            )
        ],
        raw={"adset_payload_overrides": {"destination_type": "MESSAGING_INSTAGRAM_DIRECT_MESSENGER"}},
    )

    monkeypatch.setattr(telegram_bot_module, "load_json", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(telegram_bot_module, "build_campaign_plan", lambda **_kwargs: fake_plan)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/2992172994319258",
        budget_daily_vnd=125479,
    )

    asyncio.run(
        bot._create_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_new_mode_fail",
            version=5,
        )
    )

    assert (None, ["adset_fail_1"], [], []) in rollback.calls
    assert (None, [], [], ["cr_new_fail_1"]) in rollback.calls
    assert (None, ["adset_fail_2"], [], []) in rollback.calls
    assert ("camp_new_fail_1", [], [], []) in rollback.calls
    assert storage.saved_jobs[-1][0] == "failed"
    assert "fallback destination tự động nhưng Meta vẫn từ chối" in bot._bot.messages[-1]["text"]


def test_existing_mode_post_not_advertisable_shows_specific_guidance_and_rollback_creative() -> None:
    class PostNotAdvertisableMeta(FakeMeta):
        def list_eligible_adsets(self, campaign_id: str, max_count: int) -> list[dict[str, str]]:  # noqa: ARG002
            return [
                {
                    "id": "adset_1",
                    "name": "Adset Existing",
                    "destination_type": "MESSENGER",
                    "effective_status": "ACTIVE",
                }
            ]

        def create_ad_creative(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            resolved_post,  # noqa: ANN001
            destination_type_override=None,  # noqa: ANN001
            extra_payload_overrides=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, resolved_post, destination_type_override, extra_payload_overrides
            return "cr_post_blocked"

        def create_ad(
            self,
            plan,  # noqa: ANN001
            slot,  # noqa: ANN001
            adset_id: str,  # noqa: ARG002
            creative_id: str,  # noqa: ARG002
            destination_type_override=None,  # noqa: ANN001
        ) -> str:
            _ = plan, slot, destination_type_override
            raise MetaApiError(
                "Meta API loi (400): Bạn đang sử dụng Post ID: 122134418307048007, "
                "bài viết này không thể đưa vào quảng cáo được."
            )

        def find_latest_ad_by_story_ids(
            self,
            story_ids: list[str],  # noqa: ARG002
            *,
            adset_id: str | None = None,  # noqa: ARG002
            max_ads_scan: int = 800,  # noqa: ARG002
        ) -> dict[str, str] | None:
            return {
                "id": "120249992082570728",
                "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|MED:Video|ADSET:120248804559660728 - Bản sao",
            }

    meta = PostNotAdvertisableMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    cmd = AdsCommand(
        post_url="https://www.facebook.com/reel/1293883662350463",
        budget_daily_vnd=0,
        use_existing_campaign=True,
        manual_sku_keywords=[],
        existing_campaign_hint="video",
    )
    asyncio.run(
        bot._create_existing_campaign_draft_and_send_review(
            chat_id=1,
            command=cmd,
            post_fingerprint="fp_1",
            version=1,
            selected_campaign={"id": "camp_1", "name": "ADS:QUYET|MK:ThaiLan|SKU:ALL|Video"},
            campaign_keywords=["VIDEO"],
        )
    )

    assert rollback.calls[0] == (None, [], [], ["cr_post_blocked"])
    assert storage.saved_jobs[-1][0] == "failed"
    assert bot._bot is not None
    text = bot._bot.messages[-1]["text"]
    assert "Meta API đang chặn tạo thêm ad mới từ Post ID của reel này" in text
    assert "120249992082570728" in text


def test_daily_report_scheduler_sends_to_personal_and_group_chat() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    captured_calls: list[tuple[int, bool]] = []

    async def _capture_send_daily_report(
        *,
        chat_id: int,
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
        report_payload: dict[str, Any] | None = None,  # noqa: ARG001
        include_recent_rollups: bool = False,
    ) -> dict[str, Any]:
        captured_calls.append((chat_id, include_recent_rollups))
        if len(captured_calls) >= 2:
            raise asyncio.CancelledError()
        return {"ok": True, "partial": False}

    bot._seconds_until_next_daily_report_schedule = lambda: (1, "morning")  # type: ignore[method-assign]
    bot._resolve_daily_report_date_for_slot = lambda _slot: date(2026, 5, 25)  # type: ignore[method-assign]
    bot._send_daily_report = _capture_send_daily_report  # type: ignore[method-assign]
    bot._send_missed_morning_daily_report_on_startup = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: asyncio.sleep(0)
    )

    try:
        asyncio.run(bot._daily_report_monitor_loop())
    except asyncio.CancelledError:
        pass

    assert captured_calls == [(778899, False), (-1001234567890, True)]


def test_daily_report_scheduler_falls_back_to_allowed_user_id() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=0,
    )

    captured_calls: list[tuple[int, bool]] = []

    async def _capture_send_daily_report(
        *,
        chat_id: int,
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
        report_payload: dict[str, Any] | None = None,  # noqa: ARG001
        include_recent_rollups: bool = False,
    ) -> dict[str, Any]:
        captured_calls.append((chat_id, include_recent_rollups))
        raise asyncio.CancelledError()

    bot._seconds_until_next_daily_report_schedule = lambda: (1, "evening")  # type: ignore[method-assign]
    bot._resolve_daily_report_date_for_slot = lambda _slot: date(2026, 5, 25)  # type: ignore[method-assign]
    bot._send_daily_report = _capture_send_daily_report  # type: ignore[method-assign]
    bot._send_missed_morning_daily_report_on_startup = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: asyncio.sleep(0)
    )

    try:
        asyncio.run(bot._daily_report_monitor_loop())
    except asyncio.CancelledError:
        pass

    assert captured_calls == [(778899, False)]


def test_daily_report_scheduler_evening_group_does_not_include_rollups() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    captured_calls: list[tuple[int, bool]] = []

    async def _capture_send_daily_report(
        *,
        chat_id: int,
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
        report_payload: dict[str, Any] | None = None,  # noqa: ARG001
        include_recent_rollups: bool = False,
    ) -> dict[str, Any]:
        captured_calls.append((chat_id, include_recent_rollups))
        if len(captured_calls) >= 2:
            raise asyncio.CancelledError()
        return {"ok": True, "partial": False}

    bot._seconds_until_next_daily_report_schedule = lambda: (1, "evening")  # type: ignore[method-assign]
    bot._resolve_daily_report_date_for_slot = lambda _slot: date(2026, 5, 25)  # type: ignore[method-assign]
    bot._send_daily_report = _capture_send_daily_report  # type: ignore[method-assign]
    bot._send_missed_morning_daily_report_on_startup = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: asyncio.sleep(0)
    )

    try:
        asyncio.run(bot._daily_report_monitor_loop())
    except asyncio.CancelledError:
        pass

    assert captured_calls == [(778899, False), (-1001234567890, False)]


def test_send_missed_morning_daily_report_on_startup_sends_to_personal_and_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    captured_calls: list[tuple[int, bool]] = []
    marked_slots: list[str] = []

    async def _capture_send_daily_report(
        *,
        chat_id: int,
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
        report_payload: dict[str, Any] | None = None,  # noqa: ARG001
        include_recent_rollups: bool = False,
    ) -> dict[str, Any]:
        captured_calls.append((chat_id, include_recent_rollups))
        return {"ok": True, "partial": False}

    bot._send_daily_report = _capture_send_daily_report  # type: ignore[method-assign]
    bot._resolve_daily_report_date_for_slot = lambda _slot: date(2026, 5, 25)  # type: ignore[method-assign]
    bot._should_send_morning_catchup_on_startup = lambda **_kwargs: True  # type: ignore[method-assign]
    bot._mark_daily_report_slot_sent = lambda slot, **_kwargs: marked_slots.append(slot)  # type: ignore[method-assign]

    asyncio.run(bot._send_missed_morning_daily_report_on_startup([778899, -1001234567890]))  # noqa: SLF001

    assert captured_calls == [(778899, False), (-1001234567890, True)]
    assert marked_slots == ["morning"]


def test_should_send_morning_catchup_on_startup_respects_time_and_state() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        daily_report_hour=8,
        daily_report_minute=0,
    )

    now_after_schedule = datetime(2026, 5, 28, 9, 10, 0)
    now_before_schedule = datetime(2026, 5, 28, 7, 59, 0)

    assert bot._should_send_morning_catchup_on_startup(now_local=now_after_schedule, state={}) is True
    assert (
        bot._should_send_morning_catchup_on_startup(
            now_local=now_after_schedule,
            state={"morning_last_sent_run_date": "2026-05-28"},
        )
        is False
    )
    assert bot._should_send_morning_catchup_on_startup(now_local=now_before_schedule, state={}) is False


def test_retry_pending_daily_reports_on_startup_sends_pending_evening() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    captured_calls: list[tuple[int, bool, date | None]] = []
    marked_sent: list[tuple[str, str]] = []

    async def _capture_send_daily_report(
        *,
        chat_id: int,
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,
        notify_success: bool,  # noqa: ARG001
        report_payload: dict[str, Any] | None = None,  # noqa: ARG001
        include_recent_rollups: bool = False,
    ) -> dict[str, Any]:
        captured_calls.append((chat_id, include_recent_rollups, report_date))
        return {"ok": True, "partial": False}

    bot._send_daily_report = _capture_send_daily_report  # type: ignore[method-assign]
    bot._load_daily_report_scheduler_state = lambda: {"evening_pending_run_date": "2026-05-27"}  # type: ignore[method-assign]
    bot._is_daily_report_slot_due_for_retry = lambda **_kwargs: True  # type: ignore[method-assign]
    bot._mark_daily_report_slot_sent = (  # type: ignore[method-assign]
        lambda slot, run_date=None, **_kwargs: marked_sent.append((slot, run_date.isoformat() if run_date else ""))
    )
    bot._mark_daily_report_slot_failed = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    asyncio.run(bot._retry_pending_daily_reports_on_startup([778899, -1001234567890]))  # noqa: SLF001

    assert captured_calls == [
        (778899, False, date(2026, 5, 27)),
        (-1001234567890, False, date(2026, 5, 27)),
    ]
    assert marked_sent == [("evening", "2026-05-27")]


def test_send_daily_report_appends_rollup_text_only_when_enabled() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot._bot = FakeBot()
    base_report = {
        "ok": True,
        "partial": False,
        "report_date": "2026-05-25",
        "generated_at": "2026-05-25T12:00:00+00:00",
        "pos": {},
        "ads": {},
    }
    bot.reports = SimpleNamespace(
        generate_report=lambda *_args, **_kwargs: dict(base_report),
        default_report_date=lambda: date(2026, 5, 25),
        build_message=lambda *_args, **_kwargs: "BASE",
    )
    bot._build_recent_rollup_text_sync = lambda *_args, **_kwargs: "ROLLUP 3D7D"  # type: ignore[method-assign]

    asyncio.run(
        bot._send_daily_report(
            chat_id=-5153224852,
            trigger_label="Báo cáo tự động",
            report_date=date(2026, 5, 25),
            notify_success=True,
            include_recent_rollups=True,
        )
    )
    asyncio.run(
        bot._send_daily_report(
            chat_id=778899,
            trigger_label="Báo cáo tự động",
            report_date=date(2026, 5, 25),
            notify_success=True,
            include_recent_rollups=False,
        )
    )

    assert bot._bot.messages[0]["text"] == "BASE\n\nROLLUP 3D7D"
    assert bot._bot.messages[1]["text"] == "BASE"


def test_daily_report_target_chat_ids_include_source_personal_and_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    assert bot._resolve_daily_report_target_chat_ids(778899) == [778899, -1001234567890]
    assert bot._resolve_daily_report_target_chat_ids(-1001234567890) == [-1001234567890, 778899]


def test_handle_report_command_sends_to_personal_and_group_targets() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-1001234567890,
    )

    captured: dict[str, Any] = {}

    async def _capture_send_to_targets(
        *,
        target_chat_ids: list[int],
        trigger_label: str,
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,
    ) -> None:
        captured["target_chat_ids"] = list(target_chat_ids)
        captured["trigger_label"] = trigger_label
        captured["notify_success"] = notify_success

    class _ManualReportMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=778899)
            self.chat = SimpleNamespace(id=778899)
            self.text = "/report"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._send_daily_report_to_target_chats = _capture_send_to_targets  # type: ignore[method-assign]
    message = _ManualReportMessage()
    asyncio.run(bot.handle_report_command(message))

    assert "Đang tổng hợp báo cáo" in message.answers[-1]
    assert captured["target_chat_ids"] == [778899, -1001234567890]
    assert captured["trigger_label"] == "Báo cáo thủ công"
    assert captured["notify_success"] is True


def test_handle_report_command_allows_any_member_in_report_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
    )

    captured: dict[str, Any] = {}

    async def _capture_send_to_targets(
        *,
        target_chat_ids: list[int],
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
    ) -> None:
        captured["target_chat_ids"] = list(target_chat_ids)

    class _GroupMemberMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)  # not TELEGRAM_ALLOWED_USER_ID
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "/report@testbot"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._send_daily_report_to_target_chats = _capture_send_to_targets  # type: ignore[method-assign]
    message = _GroupMemberMessage()
    asyncio.run(bot.handle_report_command(message))

    assert "Xin lỗi" not in "\n".join(message.answers)
    assert captured["target_chat_ids"] == [-5153224852, 778899]


def test_handle_reconcile_command_allows_any_member_in_report_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
        reconcile_cod_enabled=True,
    )
    bot.reconcile = SimpleNamespace()

    captured: dict[str, Any] = {}

    async def _capture_send_reconcile(
        *,
        chat_id: int,
        trigger_label: str,
        settlement_date: date | None,  # noqa: ARG001
        notify_success: bool,
        allow_update_prompt: bool,
        allow_sheet_sync: bool,
    ) -> None:
        captured["chat_id"] = chat_id
        captured["trigger_label"] = trigger_label
        captured["notify_success"] = notify_success
        captured["allow_update_prompt"] = allow_update_prompt
        captured["allow_sheet_sync"] = allow_sheet_sync

    class _GroupMemberReconcileMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)  # not TELEGRAM_ALLOWED_USER_ID
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "/reconcile@testbot cod"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._send_reconcile_cod_report = _capture_send_reconcile  # type: ignore[method-assign]
    message = _GroupMemberReconcileMessage()
    asyncio.run(bot.handle_reconcile_command(message))

    assert "Xin lỗi" not in "\n".join(message.answers)
    assert captured["chat_id"] == -5153224852
    assert captured["trigger_label"] == "Đối soát COD thủ công"
    assert captured["notify_success"] is True
    assert captured["allow_update_prompt"] is True
    assert captured["allow_sheet_sync"] is True


def test_handle_report_command_in_group_requires_bot_tag() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
    )

    captured: dict[str, Any] = {}

    async def _capture_send_to_targets(
        *,
        target_chat_ids: list[int],
        trigger_label: str,  # noqa: ARG001
        report_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
    ) -> None:
        captured["target_chat_ids"] = list(target_chat_ids)

    class _GroupMemberMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "/report"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._send_daily_report_to_target_chats = _capture_send_to_targets  # type: ignore[method-assign]
    message = _GroupMemberMessage()
    asyncio.run(bot.handle_report_command(message))

    assert captured == {}
    assert message.answers == []


def test_handle_reconcile_command_in_group_requires_bot_tag() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
        reconcile_cod_enabled=True,
    )
    bot.reconcile = SimpleNamespace()

    captured: dict[str, Any] = {}

    async def _capture_send_reconcile(
        *,
        chat_id: int,  # noqa: ARG001
        trigger_label: str,  # noqa: ARG001
        settlement_date: date | None,  # noqa: ARG001
        notify_success: bool,  # noqa: ARG001
        allow_update_prompt: bool,  # noqa: ARG001
        allow_sheet_sync: bool,  # noqa: ARG001
    ) -> None:
        captured["called"] = True

    class _GroupMemberReconcileMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "/reconcile cod"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._send_reconcile_cod_report = _capture_send_reconcile  # type: ignore[method-assign]
    message = _GroupMemberReconcileMessage()
    asyncio.run(bot.handle_reconcile_command(message))

    assert captured == {}
    assert message.answers == []


def test_resolve_reconcile_cod_notify_chat_id_prefers_config_then_group_then_owner() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)

    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
        reconcile_cod_notify_chat_id=-100999111,
    )
    assert bot._resolve_reconcile_cod_notify_chat_id() == -100999111

    bot.settings = replace(
        bot.settings,
        reconcile_cod_notify_chat_id=0,
    )
    assert bot._resolve_reconcile_cod_notify_chat_id() == -5153224852

    bot.settings = replace(
        bot.settings,
        daily_report_notify_chat_id=0,
    )
    assert bot._resolve_reconcile_cod_notify_chat_id() == 778899


def test_reconcile_cod_scheduler_cash_in_slot_sends_to_configured_notify_chat() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
        reconcile_cod_notify_chat_id=-1001234567890,
        reconcile_cod_hour=10,
        reconcile_cod_minute=0,
    )
    bot.reconcile = SimpleNamespace()

    captured_calls: list[tuple[int, str]] = []

    async def _capture_send_cash_in(*, chat_id: int, trigger_label: str) -> None:
        captured_calls.append((chat_id, trigger_label))
        raise asyncio.CancelledError()

    bot._seconds_until_next_reconcile_schedule = lambda: (1, ["cash_in"])  # type: ignore[method-assign]
    bot._send_reconcile_cod_cash_in_report = _capture_send_cash_in  # type: ignore[method-assign]

    try:
        asyncio.run(bot._reconcile_cod_monitor_loop())
    except asyncio.CancelledError:
        pass

    assert captured_calls == [
        (
            -1001234567890,
            "Báo cáo tiền về tự động Thái Dương (10:00)",
        )
    ]


def test_reconcile_cod_scheduler_weekly_summary_slot_sends_to_group_chat_fallback() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
        reconcile_cod_notify_chat_id=0,
        reconcile_cod_hour=10,
        reconcile_cod_minute=0,
    )
    bot.reconcile = SimpleNamespace()

    captured_calls: list[tuple[int, str]] = []

    async def _capture_send_weekly(*, chat_id: int, trigger_label: str) -> None:
        captured_calls.append((chat_id, trigger_label))
        raise asyncio.CancelledError()

    bot._seconds_until_next_reconcile_schedule = lambda: (1, ["weekly_summary"])  # type: ignore[method-assign]
    bot._send_reconcile_cod_weekly_summary_report = _capture_send_weekly  # type: ignore[method-assign]

    try:
        asyncio.run(bot._reconcile_cod_monitor_loop())
    except asyncio.CancelledError:
        pass

    assert captured_calls == [
        (
            -5153224852,
            "Tổng tiền nhận tuần tự động Thái Dương (10:00)",
        )
    ]


def test_handle_text_message_in_report_group_ignores_non_report_non_reconcile_text() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
    )

    captured: dict[str, Any] = {"ads_called": False}

    async def _capture_process_ads_input(message, raw_text: str) -> None:  # noqa: ANN001
        del message, raw_text
        captured["ads_called"] = True

    class _GroupChatTextMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "em thêm được lệnh báo cáo trên nhóm r ạ..."
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    bot._process_ads_input = _capture_process_ads_input  # type: ignore[method-assign]
    message = _GroupChatTextMessage()
    asyncio.run(bot.handle_text_message(message))

    assert captured["ads_called"] is False
    assert message.answers == []


def test_handle_text_message_in_report_group_suppresses_parse_error() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        telegram_allowed_user_id=778899,
        daily_report_notify_chat_id=-5153224852,
    )

    class _InvalidReportPhraseMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)
            self.chat = SimpleNamespace(id=-5153224852)
            self.text = "báo cáo abcxyz"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _InvalidReportPhraseMessage()
    asyncio.run(bot.handle_text_message(message))

    assert message.answers == []


def test_handle_new_chat_members_greets_humans_in_report_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        daily_report_notify_chat_id=-5153224852,
    )

    class _JoinMessage:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(id=-5153224852)
            self.new_chat_members = [
                SimpleNamespace(is_bot=False, full_name="Huy Tổng Đzai", username=""),
                SimpleNamespace(is_bot=True, full_name="Some Bot", username=""),
            ]
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _JoinMessage()
    asyncio.run(bot.handle_new_chat_members(message))

    assert message.answers == ["Chào mừng Huy Tổng Đzai vào nhóm ạ."]


def test_handle_new_chat_members_ignores_non_report_group() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        daily_report_notify_chat_id=-5153224852,
    )

    class _JoinMessage:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(id=-999)
            self.new_chat_members = [
                SimpleNamespace(is_bot=False, full_name="Huy Tổng Đzai", username=""),
            ]
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _JoinMessage()
    asyncio.run(bot.handle_new_chat_members(message))

    assert message.answers == []


def test_handle_text_message_manual_pancake_td_sync_runs_for_authorized_user() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        pancake_td_sync_enabled=True,
    )
    fake_sync = FakePancakeTdSync(report={"ok": True, "created": 2, "failed": 0, "notify": True})
    bot.pancake_td_sync = fake_sync

    class _ManualSyncMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=1)
            self.chat = SimpleNamespace(id=1)
            self.text = "lên đơn hôm nay"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _ManualSyncMessage()
    asyncio.run(bot.handle_text_message(message))

    assert "Đang lên đơn Thái Dương cho đơn hôm nay" in message.answers[-1]
    assert fake_sync.sync_today_calls == 1
    assert bot._bot is not None
    assert "Đồng bộ thủ công Pancake -> Thái Dương (hôm nay)" in bot._bot.messages[-1]["text"]


def test_handle_text_message_manual_pancake_td_sync_denies_unauthorized_user() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        pancake_td_sync_enabled=True,
    )
    fake_sync = FakePancakeTdSync()
    bot.pancake_td_sync = fake_sync

    class _ManualSyncMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123456789)
            self.chat = SimpleNamespace(id=1)
            self.text = "len don hom nay"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _ManualSyncMessage()
    asyncio.run(bot.handle_text_message(message))

    assert message.answers[-1] == "Xin lỗi, anh/chị không có quyền sử dụng bot này."
    assert fake_sync.sync_today_calls == 0


def test_handle_text_message_manual_pancake_td_sync_runs_for_order_code() -> None:
    meta = FakeMeta()
    storage = FakeStorage()
    rollback = FakeRollback()
    bot = _build_bot(meta, storage, rollback)
    bot.settings = replace(
        bot.settings,
        pancake_td_sync_enabled=True,
    )
    fake_sync = FakePancakeTdSync(report={"ok": True, "created": 1, "failed": 0, "notify": True})
    bot.pancake_td_sync = fake_sync

    class _ManualSyncByCodeMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=1)
            self.chat = SimpleNamespace(id=1)
            self.text = "lên đơn JCT310"
            self.answers: list[str] = []

        async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
            del reply_markup
            self.answers.append(text)

    message = _ManualSyncByCodeMessage()
    asyncio.run(bot.handle_text_message(message))

    assert "Đang lên đơn Thái Dương cho mã JCT310" in message.answers[-1]
    assert fake_sync.sync_today_calls == 0
    assert fake_sync.sync_order_code_calls == ["JCT310"]
    assert bot._bot is not None
    assert "Đồng bộ thủ công Pancake -> Thái Dương (mã JCT310)" in bot._bot.messages[-1]["text"]
