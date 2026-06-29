from __future__ import annotations

import re

from app.models import AdsCommand, AudienceSlot, PlannedCampaign, ResolvedPost
from app.utils import deep_merge


_AUDIENCE_LAYOUT = [
    ("thoi_trang_saved_audience_id", "Thời trang", "TS"),
    ("du_lich_saved_audience_id", "Du lịch", "DL"),
    ("tiec_saved_audience_id", "Tiệc", "TIEC"),
]
_DEFAULT_SKU_PREFIX = "JC"
_SKU_PREFIX_PATTERN = re.compile(r"^[A-Z][0-9A-Z]{1,7}$")
_HASHTAG_PATTERN = re.compile(r"#(?P<tag>\w+)", re.UNICODE)


def _normalize_sku_prefix(sku_prefix: str | None) -> str:
    prefix = str(sku_prefix or _DEFAULT_SKU_PREFIX).strip().upper()
    if not _SKU_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("sku_prefix trong objective config chưa hợp lệ.")
    return prefix


def resolve_sku_prefix(objective_config: dict | None) -> str:
    raw_prefix = (objective_config or {}).get("sku_prefix", _DEFAULT_SKU_PREFIX)
    return _normalize_sku_prefix(str(raw_prefix or _DEFAULT_SKU_PREFIX))


def _sku_code_pattern(sku_prefix: str) -> re.Pattern[str]:
    prefix = re.escape(_normalize_sku_prefix(sku_prefix))
    return re.compile(
        rf"#?(?<![0-9A-Z])(?P<code>{prefix}[0-9A-Z]+)(?![0-9A-Z])",
        re.IGNORECASE,
    )


def _sku_code_only_pattern(sku_prefix: str) -> re.Pattern[str]:
    prefix = re.escape(_normalize_sku_prefix(sku_prefix))
    return re.compile(
        rf"^{prefix}[0-9A-Z]+(?:[_\-/,\s]+{prefix}[0-9A-Z]+)*$",
        re.IGNORECASE,
    )


def _sku_example(sku_prefix: str, with_hash: bool = True) -> str:
    prefix = _normalize_sku_prefix(sku_prefix)
    example = "JCV238" if prefix == "JC" else f"{prefix}001"
    return f"#{example}" if with_hash else example


def extract_jc_codes(message_text: str) -> list[str]:
    return extract_sku_codes(message_text, _DEFAULT_SKU_PREFIX)


def extract_sku_codes(message_text: str, sku_prefix: str = _DEFAULT_SKU_PREFIX) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for match in _sku_code_pattern(sku_prefix).finditer(message_text or ""):
        code = match.group("code").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def extract_non_jc_hashtags(message_text: str) -> list[str]:
    return extract_non_sku_hashtags(message_text, _DEFAULT_SKU_PREFIX)


def extract_non_sku_hashtags(message_text: str, sku_prefix: str = _DEFAULT_SKU_PREFIX) -> list[str]:
    hashtags: list[str] = []
    seen: set[str] = set()
    sku_only_pattern = _sku_code_only_pattern(sku_prefix)
    for match in _HASHTAG_PATTERN.finditer(message_text or ""):
        tag = match.group("tag").strip()
        if not tag or sku_only_pattern.fullmatch(tag):
            continue
        dedup_key = tag.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        hashtags.append(tag)
    return hashtags


def build_non_jc_hashtag_suffix(message_text: str, sku_prefix: str = _DEFAULT_SKU_PREFIX) -> str:
    hashtags = extract_non_sku_hashtags(message_text, sku_prefix)
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

    sku_prefix = resolve_sku_prefix(objective_config)
    sku_codes = extract_sku_codes(resolved_post.message_text, sku_prefix)
    if not sku_codes:
        raise ValueError(
            f"Không tìm thấy mã sản phẩm dạng #{sku_prefix}... trong nội dung bài viết.\n"
            f"Anh thêm hashtag mã (ví dụ {_sku_example(sku_prefix)}) vào bài viết rồi gửi lại link giúp em."
        )
    sku_code_text = "_".join(sku_codes)

    campaign_name = f"ADS:QUYET|MK:ThaiLan|{sku_code_text}|Codex"
    media_label = (resolved_post.media_label or "Anh").strip() or "Anh"
    non_sku_suffix = build_non_jc_hashtag_suffix(resolved_post.message_text, sku_prefix)
    ad_name = f"ADS:QUYET|MK:ThaiLan|SKU:{sku_code_text}|MED:{media_label}{non_sku_suffix}"

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
    sku_prefix = resolve_sku_prefix(objective_config)

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
            f"Anh gửi theo cú pháp: <link> {_sku_example(sku_prefix, with_hash=False)} lên cũ\n"
            f"hoặc đảm bảo bài viết có hashtag #{sku_prefix}... ."
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
