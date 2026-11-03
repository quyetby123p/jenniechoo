from __future__ import annotations

import re

from app.command_parser import normalize_web_caption
from app.ads_sku_catalog import AdsSkuCatalogError, catalog_codes, extract_catalog_codes
from app.models import AdsCommand, AudienceSlot, MessengerMediaAdsInput, PlannedCampaign, ResolvedPost, WebAdsInput
from app.media_owner import normalize_media_owner
from app.utils import deep_merge
from app.web_lifecycle import normalize_web_lifecycle_strategy


_LEGACY_AUDIENCE_LAYOUT = [
    ("thoi_trang_saved_audience_id", "Thời trang", "TS"),
    ("du_lich_saved_audience_id", "Du lịch", "DL"),
    ("tiec_saved_audience_id", "Tiệc", "TIEC"),
]
DEFAULT_AUDIENCE_LAYOUT = _LEGACY_AUDIENCE_LAYOUT
_DEFAULT_SKU_PREFIX = "JC"
_MAX_META_NAME_LENGTH = 400
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


def extract_sku_codes(
    message_text: str,
    sku_prefix: str = _DEFAULT_SKU_PREFIX,
    allowed_codes: tuple[str, ...] = (),
    *,
    strict_catalog: bool = True,
) -> list[str]:
    if allowed_codes:
        try:
            return extract_catalog_codes(message_text, allowed_codes, strict=strict_catalog)
        except AdsSkuCatalogError as exc:
            raise ValueError(str(exc)) from exc
    codes: list[str] = []
    seen: set[str] = set()
    for match in _sku_code_pattern(sku_prefix).finditer(message_text or ""):
        code = match.group("code").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def resolve_sku_catalog(objective_config: dict | None) -> tuple[str, ...]:
    raw_catalog = (objective_config or {}).get("sku_catalog", {})
    if isinstance(raw_catalog, dict):
        try:
            codes = catalog_codes(raw_catalog)
        except AdsSkuCatalogError as exc:
            raise ValueError(str(exc)) from exc
        if codes:
            return codes
    raw_codes = (objective_config or {}).get("sku_codes", [])
    if isinstance(raw_codes, list) and raw_codes:
        try:
            return catalog_codes({"sku_codes": raw_codes})
        except AdsSkuCatalogError as exc:
            raise ValueError(str(exc)) from exc
    return ()


def _extract_configured_sku_codes(message_text: str, objective_config: dict) -> list[str]:
    allowed_codes = resolve_sku_catalog(objective_config)
    if allowed_codes:
        try:
            return extract_catalog_codes(message_text, allowed_codes, strict=True)
        except AdsSkuCatalogError as exc:
            raise ValueError(str(exc)) from exc
    return extract_sku_codes(
        message_text,
        resolve_sku_prefix(objective_config),
        resolve_sku_catalog(objective_config),
    )


def _normalize_sku_overrides(raw_values: list[str], objective_config: dict) -> list[str]:
    """Normalize explicit command SKU overrides using the active profile catalog."""

    allowed_codes = resolve_sku_catalog(objective_config)
    strict_catalog = not _config_bool(objective_config.get("sku_catalog_ignore_unknown"), default=False)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if allowed_codes:
            try:
                values = extract_catalog_codes(str(value), allowed_codes, strict=strict_catalog)
            except AdsSkuCatalogError as exc:
                raise ValueError(str(exc)) from exc
        else:
            values = extract_sku_codes(str(value), resolve_sku_prefix(objective_config))
        for code in values:
            if code not in seen:
                seen.add(code)
                normalized.append(code)
    return normalized


def resolve_audience_layout(audiences_config: dict) -> list[tuple[str, str, str, str]]:
    """Return configured audience slots, with legacy three-slot fallback."""

    raw_slots = audiences_config.get("slots") if isinstance(audiences_config, dict) else None
    if isinstance(raw_slots, list) and raw_slots:
        result: list[tuple[str, str, str, str]] = []
        for index, raw in enumerate(raw_slots, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"audiences.slots[{index}] phải là object.")
            key = str(raw.get("key") or f"slot_{index}").strip()
            label = str(raw.get("label") or "").strip()
            suffix = str(raw.get("suffix") or key.upper()).strip()
            audience_id = str(raw.get("saved_audience_id") or "").strip()
            if not label:
                raise ValueError(f"audiences.slots[{index}] thiếu label.")
            result.append((key, label, suffix, audience_id))
        return result
    return [
        (key, label, suffix, str(audiences_config.get(key, "")).strip())
        for key, label, suffix in _LEGACY_AUDIENCE_LAYOUT
    ]


def _resolve_audience_targeting_overrides(audiences_config: dict) -> dict[str, dict]:
    raw_slots = audiences_config.get("slots") if isinstance(audiences_config, dict) else None
    if not isinstance(raw_slots, list):
        return {}

    overrides: dict[str, dict] = {}
    for index, raw in enumerate(raw_slots, start=1):
        if not isinstance(raw, dict) or "targeting_override" not in raw:
            continue
        key = str(raw.get("key") or f"slot_{index}").strip()
        targeting = raw.get("targeting_override")
        if not isinstance(targeting, dict) or not targeting:
            raise ValueError(f"audiences.slots[{index}].targeting_override phải là object không rỗng.")
        overrides[key] = targeting
    return overrides


def _market_name(objective_config: dict) -> str:
    return str(objective_config.get("market_name", "ThaiLan")).strip() or "ThaiLan"


def _campaign_name(sku_code_text: str, objective_config: dict, *, variant_suffix: str = "|Codex") -> str:
    template = objective_config.get("campaign_name_template")
    if template:
        return str(template).format(
            campaign_name="",
            sku_code_text=sku_code_text,
        )
    suffix = str(objective_config.get("campaign_name_suffix", variant_suffix)).strip()
    return f"ADS:QUYET|MK:{_market_name(objective_config)}|{sku_code_text}{suffix}"


def _build_audience_slots(
    audiences_config: dict,
    campaign_name: str,
    ad_name: str,
    *,
    context: str,
    validate_names: bool = False,
) -> list[AudienceSlot]:
    slots: list[AudienceSlot] = []
    targeting_overrides = _resolve_audience_targeting_overrides(audiences_config)
    for key, label, suffix, audience_id in resolve_audience_layout(audiences_config):
        if not audience_id or audience_id == "replace_me":
            if context == "campaign" and len(resolve_audience_layout(audiences_config)) == 1:
                targeting = targeting_overrides.get(key)
                if targeting:
                    audience_id = ""
                else:
                    raise ValueError(
                        f"Saved Audience ID cho '{label}' chưa được cấu hình trong audiences.json."
                    )
            else:
                raise ValueError(
                    f"Saved Audience ID cho '{label}' chưa được cấu hình trong audiences.json."
                )
        adset_name = f"{campaign_name} - {label}"
        if validate_names:
            _validate_meta_name(adset_name, f"nhóm quảng cáo {context} {label}")
        slots.append(
            AudienceSlot(
                key=key,
                label=label,
                suffix=suffix,
                saved_audience_id=audience_id,
                adset_name=adset_name,
                ad_name=ad_name,
                targeting_override=targeting_overrides.get(key),
            )
        )
    if not slots:
        raise ValueError("Chưa cấu hình audience slot nào trong audiences.json.")
    return slots


def resolve_web_sku_codes(
    caption: str,
    sku_override: str,
    objective_config: dict | None = None,
    sku_source: str = "",
) -> list[str]:
    """Resolve the canonical Web SKU and reject conflicting sources."""

    sku_prefix = resolve_sku_prefix(objective_config)
    caption_codes = _extract_configured_sku_codes(caption, objective_config)
    override_codes = _extract_configured_sku_codes(sku_override, objective_config) if sku_override else []
    normalized_source = str(sku_source or "").strip().lower()
    if normalized_source == "caption":
        return caption_codes
    if normalized_source == "override":
        return override_codes
    if caption_codes and override_codes and caption_codes != override_codes:
        raise ValueError(
            "Mã sản phẩm trong caption không khớp dòng mã sản phẩm dự phòng. "
            "Anh sửa lại để hai nơi giống nhau rồi gửi lại."
        )
    return caption_codes or override_codes


def extract_non_jc_hashtags(message_text: str) -> list[str]:
    return extract_non_sku_hashtags(message_text, _DEFAULT_SKU_PREFIX)


def extract_non_sku_hashtags(
    message_text: str,
    sku_prefix: str = _DEFAULT_SKU_PREFIX,
    allowed_codes: tuple[str, ...] = (),
    *,
    strict_catalog: bool = True,
    ignore_unknown_sku_codes: bool = False,
) -> list[str]:
    hashtags: list[str] = []
    seen: set[str] = set()
    sku_only_pattern = _sku_code_only_pattern(sku_prefix)
    allowed = {str(code).strip().upper() for code in allowed_codes if str(code).strip()}
    for match in _HASHTAG_PATTERN.finditer(message_text or ""):
        tag = match.group("tag").strip()
        tag_parts = [part for part in re.split(r"[_/,\s-]+", tag.upper()) if part]
        is_allowed_catalog_tag = bool(tag_parts) and all(
            part.split(".", 1)[0] in allowed for part in tag_parts
        )
        if (
            ignore_unknown_sku_codes
            and not is_allowed_catalog_tag
            and re.fullmatch(r"[A-Z]{2,}[0-9]{2,}(?:\.[A-Z0-9]+)?", tag, flags=re.IGNORECASE)
        ):
            continue
        if not tag or sku_only_pattern.fullmatch(tag) or is_allowed_catalog_tag:
            continue
        dedup_key = tag.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        hashtags.append(tag)
    return hashtags


def build_non_jc_hashtag_suffix(
    message_text: str,
    sku_prefix: str = _DEFAULT_SKU_PREFIX,
    allowed_codes: tuple[str, ...] = (),
    *,
    strict_catalog: bool = True,
    ignore_unknown_sku_codes: bool = False,
) -> str:
    hashtags = extract_non_sku_hashtags(
        message_text,
        sku_prefix,
        allowed_codes,
        strict_catalog=strict_catalog,
        ignore_unknown_sku_codes=ignore_unknown_sku_codes,
    )
    if not hashtags:
        return ""
    return f"|{'_'.join(hashtags)}"


def _config_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        return normalized in {"1", "true", "yes", "y", "on"}
    return bool(value)


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
    allowed_codes = resolve_sku_catalog(objective_config)
    ignore_unknown_sku_codes = _config_bool(objective_config.get("sku_catalog_ignore_unknown"), default=False)
    strict_catalog = not ignore_unknown_sku_codes
    caption_sku_codes = extract_sku_codes(
        resolved_post.message_text,
        resolve_sku_prefix(objective_config),
        allowed_codes,
        strict_catalog=strict_catalog,
    )
    override_sku_codes = _normalize_sku_overrides(command.manual_sku_keywords, objective_config)
    if caption_sku_codes and override_sku_codes and caption_sku_codes != override_sku_codes:
        raise ValueError("SKU trong lệnh không khớp SKU trong caption bài viết.")
    sku_codes = override_sku_codes or caption_sku_codes
    if not sku_codes and not (objective_config.get("sku_catalog_required") or allowed_codes):
        raise ValueError(
            f"Không tìm thấy mã sản phẩm dạng #{resolve_sku_prefix(objective_config)}... trong nội dung bài viết.\n"
            f"Anh thêm hashtag mã (ví dụ {_sku_example(resolve_sku_prefix(objective_config))}) vào bài viết rồi gửi lại link giúp em."
        )
    sku_code_text = "_".join(sku_codes) or "ALL"

    campaign_name = _campaign_name(sku_code_text, objective_config, variant_suffix="|Codex")
    media_label = (resolved_post.media_label or "Anh").strip() or "Anh"
    non_sku_suffix = build_non_jc_hashtag_suffix(
        resolved_post.message_text,
        sku_prefix,
        allowed_codes,
        strict_catalog=strict_catalog,
        ignore_unknown_sku_codes=ignore_unknown_sku_codes,
    )
    media_owner_suffix = f"|{normalize_media_owner(command.media_owner)}" if command.media_owner else ""
    ad_name = (
        f"ADS:QUYET|MK:{_market_name(objective_config)}|SKU:{sku_code_text}|MED:{media_label}"
        f"{non_sku_suffix}{media_owner_suffix}"
    )

    template_name, templates = _resolve_template_name(objective_config, template_config)

    if _config_bool(objective_config.get("new_campaign_single_adset"), default=False):
        audience_id = str(
            objective_config.get("new_campaign_single_adset_saved_audience_id", "")
            or audiences_config.get("new_campaign_single_adset_saved_audience_id", "")
        ).strip()
        targeting_overrides = _resolve_audience_targeting_overrides(audiences_config)
        default_targeting = objective_config.get("adset_payload_overrides", {}).get("targeting")
        if (not audience_id or audience_id == "replace_me") and not isinstance(default_targeting, dict):
            raise ValueError(
                "new_campaign_single_adset đang bật nhưng chưa có saved audience hoặc targeting "
                "trong objective_config.adset_payload_overrides."
            )
        adset_name = str(
            objective_config.get("new_campaign_adset_name_template", "{campaign_name}")
        ).format(campaign_name=campaign_name, sku_code_text=sku_code_text)
        slots = [
            AudienceSlot(
                key="new_campaign_single_adset",
                label=str(objective_config.get("new_campaign_single_adset_label", "Broad") or "Broad"),
                suffix=str(objective_config.get("new_campaign_single_adset_suffix", "BROAD") or "BROAD"),
                saved_audience_id=audience_id,
                adset_name=adset_name,
                ad_name=ad_name,
            )
        ]
    else:
        slots = _build_audience_slots(audiences_config, campaign_name, ad_name, context="campaign")

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

    allowed_codes = resolve_sku_catalog(objective_config)
    normalized_codes: list[str] = []
    seen: set[str] = set()
    for value in sku_keywords:
        if allowed_codes:
            try:
                values = extract_catalog_codes(str(value), allowed_codes, strict=True)
            except AdsSkuCatalogError as exc:
                raise ValueError(str(exc)) from exc
        else:
            values = [str(value).strip().upper()]
        for code in values:
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

    campaign_name = _campaign_name(sku_code_text, objective_config, variant_suffix="|Codex")
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


def build_web_campaign_plan(
    web_input: WebAdsInput,
    post_fingerprint: str,
    version: int,
    audiences_config: dict,
    objective_config: dict,
) -> PlannedCampaign:
    """Build the fixed 1-3-3 unpublished Website Sales campaign layout."""

    sku_prefix = resolve_sku_prefix(objective_config)
    sku_codes = resolve_web_sku_codes(
        web_input.caption,
        web_input.sku_override,
        objective_config,
        web_input.sku_source,
    )
    sku_code_text = "_".join(sku_codes) or "ALL"
    campaign_name = _campaign_name(sku_code_text, objective_config, variant_suffix="|Web|Codex")
    media_label = "Video" if str(web_input.media_kind).strip().lower() == "video" else "Anh"
    non_sku_suffix = build_non_jc_hashtag_suffix(
        web_input.caption,
        sku_prefix,
        resolve_sku_catalog(objective_config),
    )
    media_owner_suffix = f"|{normalize_media_owner(web_input.media_owner)}" if web_input.media_owner else ""
    ad_name = (
        f"ADS:QUYET|MK:{_market_name(objective_config)}|SKU:{sku_code_text}|MED:{media_label}"
        f"{non_sku_suffix}{media_owner_suffix}"
    )

    _validate_meta_name(campaign_name, "campaign Web")
    _validate_meta_name(ad_name, "quảng cáo Web")

    slots = _build_audience_slots(
        audiences_config,
        campaign_name,
        ad_name,
        context="Web",
        validate_names=True,
    )

    lifecycle_strategy = normalize_web_lifecycle_strategy(
        web_input.customer_lifecycle_strategy or objective_config.get("customer_lifecycle_strategy", "")
    )
    objective = str(objective_config.get("campaign_objective", "OUTCOME_SALES")).strip().upper()
    conversion_location = str(objective_config.get("conversion_location", "WEBSITE")).strip().upper()
    result_goal = str(objective_config.get("result_goal", "MAXIMIZE_CONVERSIONS")).strip().upper()
    raw = {
        "customer_lifecycle_strategy": lifecycle_strategy,
        "campaign_payload_overrides": objective_config.get("campaign_payload_overrides", {}),
        "adset_payload_overrides": objective_config.get("adset_payload_overrides", {}),
        "creative_payload_overrides": objective_config.get("creative_payload_overrides", {}),
        "ad_payload_overrides": objective_config.get("ad_payload_overrides", {}),
    }

    return PlannedCampaign(
        version=version,
        campaign_name=campaign_name,
        sku_code_text=sku_code_text,
        media_label=media_label,
        post_url="",
        post_fingerprint=post_fingerprint,
        budget_daily_vnd=web_input.budget_daily_vnd,
        objective=objective,
        conversion_location=conversion_location,
        result_goal=result_goal,
        message_template_name="",
        audiences=slots,
        raw=raw,
        campaign_variant="web",
        media_kind=str(web_input.media_kind).strip().lower(),
        caption=normalize_web_caption(web_input.caption),
        headline=web_input.headline,
        cta_type=web_input.cta_type,
        destination_url=web_input.destination_url,
    )


def build_messenger_media_campaign_plan(
    media_input: MessengerMediaAdsInput,
    post_fingerprint: str,
    version: int,
    audiences_config: dict,
    objective_config: dict,
    template_config: dict,
) -> PlannedCampaign:
    """Build the fixed 1-3-3 unpublished Messenger purchase campaign."""

    sku_prefix = resolve_sku_prefix(objective_config)
    sku_codes = _extract_configured_sku_codes(media_input.caption, objective_config)
    sku_code_text = "_".join(sku_codes) or "ALL"
    campaign_name = _campaign_name(sku_code_text, objective_config, variant_suffix="|Codex")
    media_label = "Video" if str(media_input.media_kind).strip().lower() == "video" else "Anh"
    non_sku_suffix = build_non_jc_hashtag_suffix(
        media_input.caption,
        sku_prefix,
        resolve_sku_catalog(objective_config),
    )
    media_owner_suffix = f"|{normalize_media_owner(media_input.media_owner)}" if media_input.media_owner else ""
    ad_name = (
        f"ADS:QUYET|MK:{_market_name(objective_config)}|SKU:{sku_code_text}|MED:{media_label}"
        f"{non_sku_suffix}{media_owner_suffix}"
    )
    _validate_meta_name(campaign_name, "campaign Messenger")
    _validate_meta_name(ad_name, "quảng cáo Messenger")

    slots = _build_audience_slots(
        audiences_config,
        campaign_name,
        ad_name,
        context="Messenger",
        validate_names=True,
    )

    template_name, templates = _resolve_template_name(objective_config, template_config)
    objective, conversion_location, result_goal = _resolve_objective_meta(objective_config)
    lifecycle_strategy = normalize_web_lifecycle_strategy(media_input.customer_lifecycle_strategy)
    raw = _build_payload_overrides(objective_config, templates[template_name])
    raw["customer_lifecycle_strategy"] = lifecycle_strategy

    return PlannedCampaign(
        version=version,
        campaign_name=campaign_name,
        sku_code_text=sku_code_text,
        media_label=media_label,
        post_url="",
        post_fingerprint=post_fingerprint,
        budget_daily_vnd=media_input.budget_daily_vnd,
        objective=objective,
        conversion_location=conversion_location,
        result_goal=result_goal,
        message_template_name=template_name,
        audiences=slots,
        raw=raw,
        campaign_variant="messenger_media",
        media_kind=str(media_input.media_kind).strip().lower(),
        caption=normalize_web_caption(media_input.caption),
        headline=media_input.headline,
        cta_type="MESSAGE_PAGE",
    )


def _validate_meta_name(name: str, label: str) -> None:
    if len(name) > _MAX_META_NAME_LENGTH:
        raise ValueError(
            f"Tên {label} dài {len(name)} ký tự, vượt giới hạn Meta {_MAX_META_NAME_LENGTH} ký tự. "
            "Anh rút gọn hashtag hoặc mã sản phẩm rồi gửi lại."
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
