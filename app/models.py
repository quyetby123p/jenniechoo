from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class AdsCommand:
    post_url: str
    budget_daily_vnd: int
    use_existing_campaign: bool = False
    manual_sku_keywords: list[str] = field(default_factory=list)
    existing_campaign_hint: str = ""
    media_owner: str = ""


@dataclass
class WebAdsInput:
    """Validated fields for a new unpublished website-sales creative."""

    media_file_id: str
    media_unique_id: str
    media_kind: str
    media_filename: str
    caption: str
    headline: str
    cta_label: str
    cta_type: str
    destination_url: str
    budget_daily_vnd: int
    sku_override: str = ""
    sku_source: str = ""
    media_sha256: str = ""
    chat_id: int = 0
    user_id: int = 0
    media_source: str = "telegram"
    media_url: str = ""
    customer_lifecycle_strategy: str = ""
    media_owner: str = ""


@dataclass
class MessengerMediaAdsInput:
    """Validated fields for a new unpublished Messenger-sales creative."""

    media_file_id: str
    media_unique_id: str
    media_kind: str
    media_filename: str
    caption: str
    headline: str
    budget_daily_vnd: int
    media_sha256: str = ""
    chat_id: int = 0
    user_id: int = 0
    media_source: str = "telegram"
    media_url: str = ""
    customer_lifecycle_strategy: str = ""
    media_owner: str = ""


@dataclass
class MediaPerformanceCommand:
    codes: list[str] = field(default_factory=list)
    campaign_query: str = ""
    start_date: date | None = None
    end_date: date | None = None
    days: int = 7
    raw_text: str = ""


@dataclass
class ResolvedPost:
    post_id: str
    page_id: str
    permalink_url: str
    object_story_id: str
    strategy: str = "direct"
    message_text: str = ""
    media_label: str = "Anh"
    video_id: str = ""
    video_source_url: str = ""
    video_thumbnail_url: str = ""
    video_attachment_type: str = ""


@dataclass
class AudienceSlot:
    key: str
    label: str
    suffix: str
    saved_audience_id: str
    adset_name: str
    ad_name: str
    targeting_override: dict[str, Any] | None = None


@dataclass
class PlannedCampaign:
    version: int
    campaign_name: str
    sku_code_text: str
    media_label: str
    post_url: str
    post_fingerprint: str
    budget_daily_vnd: int
    objective: str
    conversion_location: str
    result_goal: str
    message_template_name: str
    audiences: list[AudienceSlot] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    campaign_variant: str = "message"
    media_kind: str = ""
    caption: str = ""
    headline: str = ""
    cta_type: str = ""
    destination_url: str = ""
    media_asset_id: str = ""


@dataclass
class CreationResult:
    campaign_id: str
    adset_ids: list[str]
    ad_ids: list[str]
    creative_ids: list[str]
