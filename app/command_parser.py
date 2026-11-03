from __future__ import annotations

from datetime import date, datetime, timedelta
import re
import unicodedata
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.exceptions import CommandParseError, ValidationError
from app.ads_sku_catalog import AdsSkuCatalogError, extract_catalog_codes
from app.models import AdsCommand, MediaPerformanceCommand
from app.utils import is_supported_facebook_url


_URL_PATTERN = re.compile(
    r"<?(?P<link>https?://[^\s<>]+)>?",
    re.IGNORECASE,
)
_EXISTING_MODE_PATTERN = re.compile(
    r"(?:lên\s*(?:(?:campaign|camp)\s*)?cũ|len\s*(?:(?:campaign|camp)\s*)?cu)"
    r"(?:\s+camp\s+(?P<campaign_hint>[^.!?]+?))?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_EXISTING_POST_INTENT_PATTERN = re.compile(
    r"(?:bài(?:\s+viết)?\s+có\s+sẵn|bai(?:\s+viet)?\s+co\s+san)",
    re.IGNORECASE,
)
_NEW_MODE_FLAG_PATTERN = re.compile(r"(?:lên\s*mới|len\s*moi)\s*[.!?]*\s*$", re.IGNORECASE)
_MANUAL_SKU_PATTERN = re.compile(r"(?<![0-9A-Z])(?:JC|VXV)[0-9A-Z]+(?![0-9A-Z])", re.IGNORECASE)
_VXV_CODE_PATTERN = re.compile(r"(?<![0-9A-Z])VXV[0-9A-Z]+(?![0-9A-Z])", re.IGNORECASE)
_LEADING_ADS_PREFIX_PATTERN = re.compile(r"^\s*/ads\b", re.IGNORECASE)
_WEB_ADS_MODE_PATTERN = re.compile(
    r"^\s*(?:/adsweb(?:@[a-z0-9_]+)?\b|(?:lên\s*mới|len\s*moi)\s+web\b)",
    re.IGNORECASE,
)
_WEB_MEDIA_URL_FIELD_PATTERN = re.compile(
    r"^\s*(?P<field>media|media\s*url|media_url)\s*[:=]\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_WEB_MEDIA_CAPTION_FIELD_PATTERN = re.compile(
    r"^\s*(?:caption|nội\s*dung|noi\s*dung)\s*[:=]\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_WEB_MEDIA_ALLOWED_HOSTS = frozenset(
    {
        "drive.google.com",
        "drive.usercontent.google.com",
        "docs.google.com",
        "www.googleapis.com",
    }
)
_WEB_FIELD_PATTERN = re.compile(
    r"^\s*(?P<field>caption|nội\s*dung|noi\s*dung|tiêu\s*đề|tieu\s*de|title|headline|"
    r"nút|nut|cta|url|link|ngân\s*sách|ngan\s*sach|budget|mã\s*sản\s*phẩm|"
    r"ma\s*san\s*pham|sku)\s*[:=]\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_DEFAULT_WEB_CTA_TYPES = {
    "mua ngay": "SHOP_NOW",
    "shop now": "SHOP_NOW",
    "xem them": "LEARN_MORE",
    "tim hieu them": "LEARN_MORE",
    "learn more": "LEARN_MORE",
    "dang ky": "SIGN_UP",
    "sign up": "SIGN_UP",
    "lien he voi chung toi": "CONTACT_US",
    "contact us": "CONTACT_US",
}
_WEB_DESTINATION_SCHEMES = frozenset({"http", "https"})
_MEDIA_PERF_SLASH_PATTERN = re.compile(r"^\s*/media_perf(?:@[a-z0-9_]+)?\b", re.IGNORECASE)
_MEDIA_PERF_DURATION_PATTERN = re.compile(
    r"(?<![0-9A-Z])(?P<days>\d{1,3})\s*(?:d|day|days|ngày|ngay)(?![0-9A-Z])",
    re.IGNORECASE,
)
_MEDIA_PERF_DATE_TOKEN = (
    r"(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|hôm nay|hom nay|hôm qua|hom qua)"
)
_MEDIA_PERF_DATE_RANGE_PATTERN = re.compile(
    rf"(?:từ|tu|from)\s+(?P<start>{_MEDIA_PERF_DATE_TOKEN})\s+(?:đến|den|tới|toi|to|-)\s+"
    rf"(?P<end>{_MEDIA_PERF_DATE_TOKEN})",
    re.IGNORECASE,
)
_BUDGET_PATTERNS = [
    re.compile(r"(?:budget)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
    re.compile(r"(?:ngân\s*sách)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
    re.compile(r"(?:ngan\s*sach)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
]


def normalize_web_caption(text: str) -> str:
    """Keep the submitted layout and restore the fixed JC caption sections.

    Telegram/Meta can receive a caption as one physical line when a retry is
    reconstructed from stored job data. These markers are the stable sections
    used in JC captions, so separating them here prevents the ad preview from
    silently flattening the layout again.
    """

    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return ""

    value = _separate_web_product_hashtag_run(value)
    value = re.sub(
        r"(?<!\n)[ \t]+—[ \t]*(?=(?:Instagram:|Order online via inbox Facebook|Website:))",
        "\n— ",
        value,
        flags=re.IGNORECASE,
    )
    brand_marker = r"\S+\s+\S+\s*\|\s*Made\s+in\s+Vietnam\b"
    value = _insert_web_caption_break_nfkd(value, brand_marker)
    value = _ensure_web_caption_blank_before_nfkd(value, brand_marker)
    value = _insert_web_caption_break_nfkd(
        value,
        r"Forever\s+be\s+an\s+extra[- ]ordinary\s+princess\b",
    )
    value = re.sub(r"(?<!\n)\n[ \t]*(?=— Instagram:)", "\n\n", value, flags=re.IGNORECASE)
    return value.strip()


def _separate_web_product_hashtag_run(value: str) -> str:
    """Put the full consecutive #JC hashtag run on one dedicated line."""

    match = re.search(r"#JC[0-9A-Z]+\b", value, flags=re.IGNORECASE)
    if not match:
        return value
    line_start = value.rfind("\n", 0, match.start()) + 1
    prefix = value[line_start : match.start()]
    if not prefix.strip():
        return value
    return value[: match.start()].rstrip() + "\n" + value[match.start() :].lstrip()


def _insert_web_caption_break_nfkd(value: str, marker_pattern: str) -> str:
    """Find styled Unicode markers while preserving the original characters."""

    normalized_chars: list[str] = []
    original_indexes: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKD", character)
        normalized_chars.extend(normalized)
        original_indexes.extend([index] * len(normalized))

    normalized_value = "".join(normalized_chars)
    match = re.search(marker_pattern, normalized_value, flags=re.IGNORECASE)
    if not match:
        return value
    original_index = original_indexes[match.start()]
    if original_index <= 0 or value[original_index - 1] == "\n":
        return value
    return value[:original_index].rstrip() + "\n" + value[original_index:].lstrip()


def _ensure_web_caption_blank_before_nfkd(value: str, marker_pattern: str) -> str:
    """Ensure a section marker is separated from the previous block by one blank line."""

    normalized_chars: list[str] = []
    original_indexes: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKD", character)
        normalized_chars.extend(normalized)
        original_indexes.extend([index] * len(normalized))

    match = re.search(marker_pattern, "".join(normalized_chars), flags=re.IGNORECASE)
    if not match:
        return value
    original_index = original_indexes[match.start()]
    if original_index <= 0 or value[original_index - 1] != "\n":
        return value
    if original_index >= 2 and value[original_index - 2] == "\n":
        return value
    return value[:original_index].rstrip() + "\n\n" + value[original_index:].lstrip()


def _extract_budget_optional(raw: str) -> int | None:
    for pattern in _BUDGET_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        budget_text = match.group("budget")
        digits = re.sub(r"[^\d]", "", budget_text)
        if not digits:
            raise CommandParseError("Ngân sách chưa hợp lệ. Anh nhập số tiền, ví dụ: ngân sách 300000.")
        return int(digits)
    return None


def _extract_budget(raw: str) -> int:
    value = _extract_budget_optional(raw)
    if value is not None:
        return value
    raise CommandParseError(
        "Em chưa thấy ngân sách. Anh gửi như sau:\n"
        "<link_bài_viết_facebook> ngân sách 300000 lên mới\n"
        "Hoặc: <link_bài_viết_facebook> JCV140 lên cũ\n"
        "Hoặc: <link_bài_viết_facebook> lên cũ camp video"
    )


def _extract_manual_sku_keywords(
    raw: str,
    allowed_sku_codes: tuple[str, ...] = (),
    *,
    ignore_unknown_sku_codes: bool = False,
) -> list[str]:
    cleaned = _URL_PATTERN.sub(" ", raw)
    for pattern in _BUDGET_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _EXISTING_MODE_PATTERN.sub(" ", cleaned)
    cleaned = _NEW_MODE_FLAG_PATTERN.sub(" ", cleaned)
    cleaned = _LEADING_ADS_PREFIX_PATTERN.sub(" ", cleaned)

    if allowed_sku_codes:
        try:
            return extract_catalog_codes(
                cleaned,
                allowed_sku_codes,
                strict=not ignore_unknown_sku_codes,
            )
        except AdsSkuCatalogError as exc:
            raise CommandParseError(str(exc)) from exc

    keywords: list[str] = []
    seen: set[str] = set()
    for match in _MANUAL_SKU_PATTERN.finditer(cleaned):
        code = str(match.group(0)).strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        keywords.append(code)
    return keywords


def _resolve_campaign_mode(raw: str) -> tuple[bool, str]:
    existing_mode_match = _EXISTING_MODE_PATTERN.search(raw)
    has_existing_mode_flag = bool(existing_mode_match or _EXISTING_POST_INTENT_PATTERN.search(raw))
    has_new_mode_flag = bool(_NEW_MODE_FLAG_PATTERN.search(raw))
    if has_existing_mode_flag and has_new_mode_flag:
        raise CommandParseError(
            "Em thấy cả hai ý định `bài có sẵn/lên cũ` và `lên mới` trong cùng một lệnh.\n"
            "Anh chỉ dùng một cờ ở cuối câu:\n"
            "- Chèn vào campaign cũ: <link> JCV140 lên cũ\n"
            "- Chèn bài có sẵn: <link> bài có sẵn\n"
            "- Lên campaign mới: <link> ngân sách 300000 lên mới"
        )
    if not has_existing_mode_flag and not has_new_mode_flag:
        raise CommandParseError(
            "Để không tạo nhầm campaign mới, anh ghi rõ một trong hai cách:\n"
            "- Chèn vào campaign cũ: <link> JCV140 lên cũ\n"
            "- Chèn bài có sẵn: <link> bài có sẵn\n"
            "- Lên campaign mới: <link> ngân sách 300000 lên mới"
        )
    campaign_hint = ""
    if existing_mode_match:
        campaign_hint = re.sub(r"\s+", " ", str(existing_mode_match.group("campaign_hint") or "").strip())
    return has_existing_mode_flag, campaign_hint


def parse_ads_command(
    text: str,
    allowed_sku_codes: tuple[str, ...] = (),
    allow_new_sku_override: bool = False,
    ignore_unknown_sku_codes: bool = False,
) -> AdsCommand:
    raw = (text or "").strip()
    if not raw:
        raise CommandParseError(
            "Tin nhắn đang trống. Anh gửi:\n"
            "- <link_bài_viết_facebook> ngân sách 300000 lên mới\n"
            "- <link_bài_viết_facebook> JCV140 lên cũ\n"
            "- <link_bài_viết_facebook> lên cũ camp video"
        )

    link_match = _URL_PATTERN.search(raw)
    if not link_match:
        raise CommandParseError(
            "Em chưa thấy link bài viết Facebook.\n"
            "Anh gửi:\n"
            "- <link_bài_viết_facebook> ngân sách 300000 lên mới\n"
            "- <link_bài_viết_facebook> JCV140 lên cũ\n"
            "- <link_bài_viết_facebook> lên cũ camp video"
        )

    link = link_match.group("link").strip()
    use_existing_campaign, existing_campaign_hint = _resolve_campaign_mode(raw)
    budget = _extract_budget_optional(raw) if use_existing_campaign else _extract_budget(raw)
    if budget is None:
        budget = 0
    manual_sku_keywords = (
        _extract_manual_sku_keywords(
            raw,
            allowed_sku_codes,
            ignore_unknown_sku_codes=ignore_unknown_sku_codes,
        )
        if use_existing_campaign or allow_new_sku_override
        else []
    )

    if (not use_existing_campaign) and budget <= 0:
        raise ValidationError("Ngân sách phải lớn hơn 0 VND.")

    if not is_supported_facebook_url(link):
        raise ValidationError("Link không phải bài post Facebook hợp lệ.")

    return AdsCommand(
        post_url=link,
        budget_daily_vnd=budget,
        use_existing_campaign=use_existing_campaign,
        manual_sku_keywords=manual_sku_keywords,
        existing_campaign_hint=existing_campaign_hint,
    )


def try_parse_web_ads_form(
    text: str,
    cta_config: dict | None = None,
) -> tuple[bool, dict[str, str] | None]:
    """Parse the second Telegram message for a new Web Sales ad."""

    raw = (text or "").strip()
    if not _WEB_ADS_MODE_PATTERN.search(raw):
        return False, None

    field_aliases = {
        "caption": "caption",
        "nội dung": "caption",
        "noi dung": "caption",
        "tiêu đề": "headline",
        "tieu de": "headline",
        "title": "headline",
        "headline": "headline",
        "nút": "cta",
        "nut": "cta",
        "cta": "cta",
        "url": "url",
        "link": "url",
        "ngân sách": "budget",
        "ngan sach": "budget",
        "budget": "budget",
        "mã sản phẩm": "sku",
        "ma san pham": "sku",
        "sku": "sku",
    }
    values: dict[str, str] = {}
    current_field = ""
    mode_match = _WEB_ADS_MODE_PATTERN.search(raw)
    body = raw[mode_match.end() :] if mode_match else raw
    for line in body.splitlines():
        match = _WEB_FIELD_PATTERN.match(line)
        if match:
            raw_field = re.sub(r"\s+", " ", str(match.group("field") or "").strip().lower())
            current_field = field_aliases.get(raw_field, "")
            if current_field:
                values[current_field] = str(match.group("value") or "").strip()
            continue
        if current_field:
            existing = values.get(current_field, "")
            continuation = line.rstrip("\r")
            values[current_field] = (
                f"{existing}\n{continuation}" if existing else continuation
            )

    missing = [label for key, label in (("caption", "caption"), ("headline", "tiêu đề"), ("cta", "nút"), ("url", "url"), ("budget", "ngân sách")) if not values.get(key)]
    if missing:
        raise CommandParseError(
            "Mẫu camp Web còn thiếu: " + ", ".join(missing) + ".\n"
            "Anh gửi media trước, rồi gửi:\n"
            "lên mới web\n"
            "caption: Nội dung #JCA250\n"
            "tiêu đề: Tên quảng cáo\n"
            "nút: Mua ngay\n"
            "url: https://example.com\n"
            "ngân sách: 300000\n"
            "mã sản phẩm: JCA250"
        )

    budget_digits = re.sub(r"[^\d]", "", values["budget"])
    if not budget_digits or int(budget_digits) <= 0:
        raise ValidationError("Ngân sách Web phải lớn hơn 0 VND.")

    parsed_url = urlparse(values["url"].strip())
    if parsed_url.scheme.lower() not in _WEB_DESTINATION_SCHEMES or not parsed_url.netloc:
        raise ValidationError(
            "URL Web phải là đường dẫn HTTP/HTTPS hợp lệ, ví dụ: https://example.com hoặc https://example.com/san-pham"
        )

    cta_label, cta_type = parse_web_cta(values["cta"], cta_config)

    return True, {
        "caption": normalize_web_caption(values["caption"]),
        "headline": values["headline"].strip(),
        "cta_label": cta_label,
        "cta_type": cta_type,
        "destination_url": values["url"].strip(),
        "budget_daily_vnd": str(int(budget_digits)),
        "sku_override": values.get("sku", "").strip(),
    }


def parse_web_media_url(text: str) -> str:
    """Validate a public Google Drive media URL used by the Web flow."""

    value = str(text or "").strip().strip("<>")
    parsed_url = urlparse(value)
    host = (parsed_url.hostname or "").lower().rstrip(".")
    if parsed_url.scheme.lower() != "https" or not parsed_url.netloc or any(char.isspace() for char in value):
        raise ValidationError(
            "Link media phải là đường dẫn HTTPS hợp lệ từ Google Drive "
            "(ví dụ: https://drive.google.com/file/d/...)."
        )
    if parsed_url.username or parsed_url.password or host not in _WEB_MEDIA_ALLOWED_HOSTS:
        raise ValidationError(
            "Link media hiện chỉ hỗ trợ Google Drive. Anh bật chia sẻ "
            "Anyone with the link / Viewer rồi gửi lại link HTTPS."
        )
    return value


def try_parse_web_media_url_message(text: str) -> tuple[bool, dict[str, str] | None]:
    """Parse a first Web message containing a Google Drive link and optional caption."""

    raw = (text or "").strip()
    if not raw:
        return False, None

    lines = raw.splitlines()
    first_line = lines[0].strip() if lines else ""
    media_value = ""
    matched = False
    field_match = _WEB_MEDIA_URL_FIELD_PATTERN.match(first_line)
    if field_match:
        matched = True
        media_value = str(field_match.group("value") or "").strip()
    else:
        parsed_first = urlparse(first_line.strip("<>").strip())
        host = (parsed_first.hostname or "").lower().rstrip(".")
        if parsed_first.scheme.lower() == "https" and host in _WEB_MEDIA_ALLOWED_HOSTS:
            matched = True
            media_value = first_line

    if not matched:
        return False, None
    if not media_value:
        raise CommandParseError(
            "Em chưa thấy link media. Anh gửi: media: https://drive.google.com/file/d/.../view"
        )

    caption = ""
    inline_caption_match = re.search(
        r"\s+(?:caption|nội\s*dung|noi\s*dung)\s*[:=]\s*",
        media_value,
        flags=re.IGNORECASE,
    )
    if inline_caption_match:
        caption = media_value[inline_caption_match.end() :].strip()
        media_value = media_value[: inline_caption_match.start()].strip()
    current_caption = False
    for line in lines[1:]:
        caption_match = _WEB_MEDIA_CAPTION_FIELD_PATTERN.match(line)
        if caption_match:
            caption = str(caption_match.group("value") or "").strip()
            current_caption = True
            continue
        if current_caption and line.strip():
            caption = f"{caption}\n{line.strip()}".strip()

    return True, {"media_url": parse_web_media_url(media_value), "caption": caption}


def parse_web_cta(text: str, cta_config: dict | None = None) -> tuple[str, str]:
    label = str(text or "").strip()
    configured_ctas = _configured_web_ctas(cta_config)
    cta_type = configured_ctas.get(_normalize_text(label), "")
    if not cta_type:
        supported = ", ".join(sorted(configured_ctas.keys()))
        raise ValidationError(f"Nút hành động chưa được hỗ trợ: {label}. Nút hợp lệ: {supported}.")
    return label, cta_type


def parse_web_url(text: str) -> str:
    value = re.sub(r"^\s*(?:url|link)\s*[:=]?\s*", "", str(text or "").strip(), flags=re.IGNORECASE)
    parsed_url = urlparse(value)
    if parsed_url.scheme.lower() not in _WEB_DESTINATION_SCHEMES or not parsed_url.netloc:
        raise ValidationError(
            "URL Web phải là đường dẫn HTTP/HTTPS hợp lệ, ví dụ: https://example.com hoặc https://example.com/san-pham"
        )
    return value


def parse_web_budget(text: str) -> int:
    value = re.sub(
        r"^\s*(?:ngân\s*sách|ngan\s*sach|budget)\s*[:=]?\s*",
        "",
        str(text or "").strip(),
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s*(?:vnd|vnđ|đ|d)\s*$", "", value, flags=re.IGNORECASE).strip()
    if not re.fullmatch(r"[\d\s.,]+", value):
        raise ValidationError("Ngân sách Web phải là số VND lớn hơn 0, ví dụ: 300000.")
    digits = re.sub(r"[^\d]", "", value)
    if not digits or int(digits) <= 0:
        raise ValidationError("Ngân sách Web phải lớn hơn 0 VND.")
    return int(digits)


def _configured_web_ctas(cta_config: dict | None = None) -> dict[str, str]:
    configured_ctas = _DEFAULT_WEB_CTA_TYPES.copy()
    if isinstance(cta_config, dict):
        raw_ctas = cta_config.get("cta_types", cta_config)
        if isinstance(raw_ctas, dict):
            for label, value in raw_ctas.items():
                normalized_label = _normalize_text(str(label))
                normalized_value = str(value or "").strip().upper()
                if normalized_label and normalized_value:
                    configured_ctas[normalized_label] = normalized_value
    return configured_ctas


def try_parse_media_performance_command(
    text: str,
    timezone_name: str,
) -> tuple[bool, MediaPerformanceCommand | None]:
    raw = (text or "").strip()
    if not raw:
        return False, None

    normalized = _normalize_text(raw)
    slash_match = _MEDIA_PERF_SLASH_PATTERN.match(raw)
    is_natural = normalized.startswith(
        (
            "phan tich media",
            "phan tich ma",
            "phan tich camp",
            "phan tich campaign",
            "media perf",
            "media performance",
        )
    )
    if not slash_match and not is_natural:
        return False, None

    today_local = datetime.now(_resolve_timezone(timezone_name)).date()
    days = 7
    duration_match = _MEDIA_PERF_DURATION_PATTERN.search(raw)
    if duration_match:
        days = int(duration_match.group("days"))
        if days < 1 or days > 90:
            raise CommandParseError("Khoảng phân tích media chỉ hỗ trợ từ 1 đến 90 ngày.")

    start_date = today_local - timedelta(days=days - 1)
    end_date = today_local
    date_range_match = _MEDIA_PERF_DATE_RANGE_PATTERN.search(raw)
    if not date_range_match:
        date_range_match = re.search(
            rf"(?P<start>{_MEDIA_PERF_DATE_TOKEN})\s+(?:đến|den|tới|toi|to|-)\s+"
            rf"(?P<end>{_MEDIA_PERF_DATE_TOKEN})",
            raw,
            flags=re.IGNORECASE,
        )
    if date_range_match:
        parsed_start = _parse_human_date(date_range_match.group("start"), timezone_name)
        parsed_end = _parse_human_date(date_range_match.group("end"), timezone_name)
        if parsed_start is None or parsed_end is None:
            raise CommandParseError("Khoảng ngày phân tích media chưa hợp lệ.")
        if parsed_end < parsed_start:
            parsed_start, parsed_end = parsed_end, parsed_start
        if (parsed_end - parsed_start).days > 90:
            raise CommandParseError("Khoảng phân tích media chỉ hỗ trợ tối đa 90 ngày.")
        start_date = parsed_start
        end_date = parsed_end
        days = (end_date - start_date).days + 1

    codes: list[str] = []
    seen_codes: set[str] = set()
    for match in _VXV_CODE_PATTERN.finditer(raw):
        code = str(match.group(0)).strip().upper()
        if code in seen_codes:
            continue
        seen_codes.add(code)
        codes.append(code)

    campaign_query = _extract_media_performance_campaign_query(raw)
    return True, MediaPerformanceCommand(
        codes=codes,
        campaign_query=campaign_query,
        start_date=start_date,
        end_date=end_date,
        days=days,
        raw_text=raw,
    )


def _extract_media_performance_campaign_query(raw: str) -> str:
    match = re.search(r"\b(?:camp|campaign)\s+(?P<campaign>.+)$", raw, re.IGNORECASE)
    if not match:
        return ""
    value = str(match.group("campaign") or "").strip()
    value = _MEDIA_PERF_DATE_RANGE_PATTERN.sub(" ", value)
    value = _MEDIA_PERF_DURATION_PATTERN.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" .,!?:;-")
    return value


def parse_report_date_argument(text: str) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None

    parts = raw.split()
    if parts and parts[0].lower().startswith("/report"):
        parts = parts[1:]

    if not parts:
        return None
    if len(parts) != 1:
        raise CommandParseError(
            "Cú pháp chưa đúng. Anh dùng:\n"
            "/report\n"
            "hoặc /report YYYY-MM-DD"
        )

    value = parts[0].strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandParseError(
            "Ngày chưa đúng định dạng.\n"
            "Anh dùng: /report YYYY-MM-DD (ví dụ: /report 2026-05-15)"
        ) from exc


def try_parse_report_command(text: str, timezone_name: str) -> tuple[bool, date | None]:
    raw = (text or "").strip()
    if not raw:
        return False, None

    if raw.lower().startswith("/report"):
        return True, parse_report_date_argument(raw)

    normalized = _normalize_text(raw)
    prefix_match = re.match(r"^bao\s*cao\b", normalized)
    if not prefix_match:
        return False, None

    remainder = normalized[prefix_match.end() :].strip()
    if not remainder:
        return True, None

    if remainder in {"hom nay", "ngay hom nay"}:
        today_local = datetime.now(_resolve_timezone(timezone_name)).date()
        return True, today_local

    if remainder in {"hom qua", "ngay hom qua"}:
        today_local = datetime.now(_resolve_timezone(timezone_name)).date()
        return True, today_local - date.resolution

    date_match = re.match(
        r"^(?:ngay\s+)?(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{2,4}))?$",
        remainder,
    )
    if date_match:
        day = int(date_match.group("day"))
        month = int(date_match.group("month"))
        raw_year = date_match.group("year")
        if raw_year:
            year = int(raw_year)
            if len(raw_year) == 2:
                year = 2000 + year
            try:
                return True, date(year, month, day)
            except ValueError as exc:
                raise CommandParseError("Ngày báo cáo không hợp lệ.") from exc

        today_local = datetime.now(_resolve_timezone(timezone_name)).date()
        year = today_local.year
        try:
            candidate = date(year, month, day)
        except ValueError as exc:
            raise CommandParseError("Ngày báo cáo không hợp lệ.") from exc
        if candidate > today_local:
            candidate = date(year - 1, month, day)
        return True, candidate

    raise CommandParseError(
        "Cú pháp báo cáo chưa đúng. Anh dùng:\n"
        "Báo cáo\n"
        "Báo cáo hôm nay\n"
        "Báo cáo ngày hôm qua\n"
        "Báo cáo 15/6\n"
        "Hoặc: /report YYYY-MM-DD"
    )


def parse_reconcile_cod_date_argument(text: str, timezone_name: str) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split()
    if not parts:
        raise CommandParseError("Cú pháp chưa đúng. Anh dùng: /reconcile cod hoặc /reconcile cod YYYY-MM-DD")
    command_token = str(parts[0]).strip().lower()
    if not command_token.startswith("/reconcile"):
        raise CommandParseError("Cú pháp chưa đúng. Anh dùng: /reconcile cod hoặc /reconcile cod YYYY-MM-DD")
    if not re.match(r"^/reconcile(?:@[a-z0-9_]+)?$", command_token):
        raise CommandParseError("Cú pháp chưa đúng. Anh dùng: /reconcile cod hoặc /reconcile cod YYYY-MM-DD")
    parts = parts[1:]
    if not parts:
        raise CommandParseError("Thiếu chế độ đối soát. Anh dùng: /reconcile cod")
    if str(parts[0]).lower() != "cod":
        raise CommandParseError("Hiện tại bot chỉ hỗ trợ: /reconcile cod")
    parts = parts[1:]
    if not parts:
        return None
    remainder = " ".join(parts).strip()
    parsed = _parse_human_date(remainder, timezone_name)
    if parsed is None:
        raise CommandParseError(
            "Ngày đối soát chưa đúng. Anh dùng:\n"
            "/reconcile cod\n"
            "/reconcile cod YYYY-MM-DD\n"
            "/reconcile cod hôm qua"
        )
    return parsed


def try_parse_reconcile_cod_command(text: str, timezone_name: str) -> tuple[bool, date | None]:
    raw = (text or "").strip()
    if not raw:
        return False, None
    lower = raw.lower()
    if lower.startswith("/reconcile"):
        return True, parse_reconcile_cod_date_argument(raw, timezone_name)

    normalized = _normalize_text(raw)
    prefix_match = re.match(r"^doi\s*soat(?:\s*cod)?\b", normalized)
    if not prefix_match:
        return False, None
    remainder = normalized[prefix_match.end() :].strip()
    if not remainder:
        return True, None
    parsed = _parse_human_date(remainder, timezone_name)
    if parsed is None:
        raise CommandParseError(
            "Cú pháp đối soát COD chưa đúng. Anh dùng:\n"
            "đối soát\n"
            "đối soát hôm nay\n"
            "đối soát hôm qua\n"
            "đối soát cod\n"
            "đối soát cod hôm qua\n"
            "đối soát cod ngày 9/5\n"
            "đối soát cod 2026-05-09"
        )
    return True, parsed


def try_parse_pancake_td_sync_command(text: str) -> tuple[bool, str | None]:
    raw = (text or "").strip()
    if not raw:
        return False, None

    normalized = _normalize_text(raw)
    prefix_match = re.match(r"^len\s*don(?=\s|$|#|[a-z]{2,}\d)", normalized)
    if not prefix_match:
        return False, None

    remainder = normalized[prefix_match.end() :].strip()
    if not remainder or remainder in {"hom nay", "ngay hom nay"}:
        return True, None

    order_match = re.match(r"^(?:ma\s*)?(?P<code>#?[a-z0-9][a-z0-9\-_]*)$", remainder)
    if order_match:
        raw_code = str(order_match.group("code") or "").strip()
        sanitized = re.sub(r"[^a-z0-9]", "", raw_code.lower())
        if re.fullmatch(r"[a-z]{2,}\d{2,}", sanitized):
            return True, sanitized.upper()
        if re.fullmatch(r"\d{1,}", sanitized):
            return True, sanitized

    raise CommandParseError(
        "Cú pháp lên đơn chưa đúng. Anh dùng:\n"
        "lên đơn hôm nay\n"
        "hoặc: lên đơn JCT310 / lên đơn 121"
    )


def _normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFD", text)
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accents.lower().replace("đ", "d")
    return re.sub(r"\s+", " ", lowered).strip()


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Ho_Chi_Minh")


def _parse_human_date(raw: str, timezone_name: str) -> date | None:
    value = _normalize_text(str(raw or ""))
    if not value:
        return None
    today_local = datetime.now(_resolve_timezone(timezone_name)).date()
    if value in {"hom nay", "ngay hom nay"}:
        return today_local
    if value in {"hom qua", "ngay hom qua"}:
        return today_local - date.resolution

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        pass

    date_match = re.match(
        r"^(?:ngay\s+)?(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{2,4}))?$",
        value,
    )
    if not date_match:
        return None
    day = int(date_match.group("day"))
    month = int(date_match.group("month"))
    raw_year = date_match.group("year")
    if raw_year:
        year = int(raw_year)
        if len(raw_year) == 2:
            year = 2000 + year
        try:
            return date(year, month, day)
        except ValueError:
            return None

    year = today_local.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate > today_local:
        candidate = date(year - 1, month, day)
    return candidate
