from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdsCommand:
    post_url: str
    budget_daily_vnd: int
    use_existing_campaign: bool = False
    manual_sku_keywords: list[str] = field(default_factory=list)
    existing_campaign_hint: str = ""


@dataclass
class ResolvedPost:
    post_id: str
    page_id: str
    permalink_url: str
    object_story_id: str
    strategy: str = "direct"
    message_text: str = ""
    media_label: str = "Anh"


@dataclass
class AudienceSlot:
    key: str
    label: str
    suffix: str
    saved_audience_id: str
    adset_name: str
    ad_name: str


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


@dataclass
class CreationResult:
    campaign_id: str
    adset_ids: list[str]
    ad_ids: list[str]
    creative_ids: list[str]
