from __future__ import annotations

import re

from app.models import AdsCommand, AudienceSlot, PlannedCampaign, ResolvedPost
from app.utils import deep_merge


_AUDIENCE_LAYOUT = [
    ("thoi_trang_saved_audience_id", "Thời trang", "TS"),
    ("du_lich_saved_audience_id", "Du lịch", "DL"),
    ("tiec_saved_audience_id", "Tiệc", "TIEC"),
]
_JC_CODE_PATTERN = re.compile(r"#?(?<![0-9A-Z])(?P<code>JC[0-9A-Z]+)(?![0-9A-Z])", re.IGNORECASE)
_HASHTAG_PATTERN = re.compile(r"#(?P<tag>\w+)", re.UNICODE)
_JC_CODE_ONLY_PATTERN = re.compile(r"^JC[0-9A-Z]+(?:[_\-/,\s]+JC[0-9A-Z]+)*$", re.IGNORECASE)


def extract_jc_codes(message_text: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for match in _JC_CODE_PATTERN.finditer(message_text or ""):
        code = match.group("code").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def extract_non_jc_hashtags(message_text: str) -> list[str]:
    hashtags: list[str] = []
    seen: set[str] = set()
    for match in _HASHTAG_PATTERN.finditer(message_text or ""):
        tag = match.group("tag").strip()
        if not tag or _JC_CODE_ONLY_PATTERN.fullmatch(tag):
            continue
        dedup_key = tag.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        hashtags.append(tag)
    return hashtags


def build_non_jc_hashtag_suffix(message_text: str) -> str:
    hashtags = extract_non_jc_hashtags(message_text)
    if not hashtags:
        return ""
    return f"|{'_'.join(hashtags)}"


def build_campaign_plan(
    command: AdsCommand,
    resolved_post: ResolvedPost,
    post_fingerprint: str,
    version: int,
    timezone_name: str,
    audiences_config: dict,
    objective_config: dict,
    template_config: dict,
) -> PlannedCampaign:
    _ = timezone_name

    sku_codes = extract_jc_codes(resolved_post.message_text)
    if not sku_codes:
        raise ValueError(
            "Không tìm thấy mã sản phẩm dạng #JC... trong nội dung bài viết.\n"
            "Anh thêm hashtag mã (ví dụ #JCV238) vào bài viết rồi gửi lại link giúp em."
        )
    sku_code_text = "_".join(sku_codes)

    campaign_name = f"ADS:QUYET|MK:ThaiLan|{sku_code_text}|Codex"
    media_label = (resolved_post.media_label or "Anh").strip() or "Anh"
    non_jc_suffix = build_non_jc_hashtag_suffix(resolved_post.message_text)
    ad_name = f"ADS:QUYET|MK:ThaiLan|SKU:{sku_code_text}|MED:{media_label}{non_jc_suffix}"

    template_name, templates = _resolve_template_name(objective_config, template_config)

    slots: list[AudienceSlot] = []
    for key, label, suffix in _AUDIENCE_LAYOUT:
        audience_id = str(audiences_config.get(key, "")).strip()
        if not audience_id or audience_id == "replace_me":
            raise ValueError(
                f"Saved Audience ID cho '{label}' chua duoc cau hinh trong audiences.json."
            )
        adset_name = f"{campaign_name} - {label}"
        slots.append(
            AudienceSlot(
                key=key,
                label=label,
                suffix=suffix,
                saved_audience_id=audience_id,
                adset_name=adset_name,
                ad_name=ad_name,
            )
        )

    objective, conversion_location, result_goal = _resolve_objective_meta(objective_config)
    raw = _build_payload_overrides(objective_config, templates[template_name])

    return PlannedCampaign(
        version=version,
        campaign_name=campaign_name,
        sku_code_text=sku_code_text,
        media_label=media_label,
        post_url=command.post_url,
        post_fingerprint=post_fingerprint,
        budget_daily_vnd=command.budget_daily_vnd,
        objective=objective,
        conversion_location=conversion_location,
        result_goal=result_goal,
        message_template_name=template_name,
        audiences=slots,
        raw=raw,
    )


def build_existing_campaign_plan(
    command: AdsCommand,
    resolved_post: ResolvedPost,
    post_fingerprint: str,
    version: int,
    timezone_name: str,
    objective_config: dict,
    template_config: dict,
    sku_keywords: list[str],
) -> PlannedCampaign:
    _ = timezone_name

    normalized_codes: list[str] = []
    seen: set[str] = set()
    for value in sku_keywords:
        code = str(value).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        normalized_codes.append(code)
    if not normalized_codes:
        raise ValueError(
            "Không tìm thấy mã SKU để map campaign cũ.\n"
            "Anh gửi theo cú pháp: <link> JCV140 lên cũ\n"
            "hoặc đảm bảo bài viết có hashtag #JC... ."
        )
    sku_code_text = "_".join(normalized_codes)

    campaign_name = f"ADS:QUYET|MK:ThaiLan|{sku_code_text}|Codex"
    media_label = (resolved_post.media_label or "Anh").strip() or "Anh"

    template_name, templates = _resolve_template_name(objective_config, template_config)
    objective, conversion_location, result_goal = _resolve_objective_meta(objective_config)
    raw = _build_payload_overrides(objective_config, templates[template_name])

    return PlannedCampaign(
        version=version,
        campaign_name=campaign_name,
        sku_code_text=sku_code_text,
        media_label=media_label,
        post_url=command.post_url,
        post_fingerprint=post_fingerprint,
        budget_daily_vnd=command.budget_daily_vnd,
        objective=objective,
        conversion_location=conversion_location,
        result_goal=result_goal,
        message_template_name=template_name,
        audiences=[],
        raw=raw,
    )


def _resolve_template_name(objective_config: dict, template_config: dict) -> tuple[str, dict]:
    template_name = objective_config.get("message_template_name", "").strip()
    templates = template_config.get("templates", {})
    if template_name not in templates:
        raise ValueError(
            f"Khong tim thay message template '{template_name}'. "
            "Hay cap nhat config/message_templates.json."
        )
    if template_name != "Chào JC":
        raise ValueError(
            "Template đang cấu hình không đúng yêu cầu. Vui lòng đặt `message_template_name` là 'Chào JC'."
        )
    return template_name, templates


def _resolve_objective_meta(objective_config: dict) -> tuple[str, str, str]:
    objective = str(objective_config.get("campaign_objective", "OUTCOME_ENGAGEMENT")).strip().upper()
    conversion_location = str(objective_config.get("conversion_location", "MESSAGING_DESTINATION"))
    result_goal = str(objective_config.get("result_goal", "MAXIMIZE_PURCHASES_VIA_MESSAGE"))
    return objective, conversion_location, result_goal


def _build_payload_overrides(objective_config: dict, template_meta: dict) -> dict:
    return {
        "campaign_payload_overrides": objective_config.get("campaign_payload_overrides", {}),
        "adset_payload_overrides": deep_merge(
            objective_config.get("adset_payload_overrides", {}),
            template_meta.get("adset_patch", {}),
        ),
        "creative_payload_overrides": deep_merge(
            objective_config.get("creative_payload_overrides", {}),
            template_meta.get("creative_patch", {}),
        ),
        "ad_payload_overrides": deep_merge(
            objective_config.get("ad_payload_overrides", {}),
            template_meta.get("ad_patch", {}),
        ),
    }
