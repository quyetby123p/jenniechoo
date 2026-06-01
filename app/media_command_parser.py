from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from app.exceptions import CommandParseError


_SLASH_MEDIA_COMMAND_PATTERN = re.compile(
    r"^\s*/media(?:@\w+)?\s+(?P<product_code>[^\s]+)(?:\s+(?P<keywords>.*))?$",
    re.IGNORECASE,
)
_NATURAL_MEDIA_PREFIX_PATTERN = re.compile(
    r"^\s*(?:tim|tìm)\s+media\b",
    re.IGNORECASE,
)
_LABELLED_PRODUCT_CODE_PATTERN = re.compile(
    r"(?:\b(?:sku|ma|mã|code)\b)\s*[:=#-]?\s*(?P<product_code>[A-Za-z0-9][A-Za-z0-9_\-\.]{1,63})",
    re.IGNORECASE,
)
_PRODUCT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{1,63}$")


@dataclass(frozen=True)
class MediaCommand:
    product_code: str
    keyword_text: str



def parse_media_caption(caption: str) -> MediaCommand:
    raw = str(caption or "").strip()
    if not raw:
        raise CommandParseError(
            "Caption đang trống. Anh gửi ảnh kèm caption theo mẫu:\n"
            "Tìm media\n"
            "Hoặc: /media SKU123 váy hoa nữ"
        )

    slash_match = _SLASH_MEDIA_COMMAND_PATTERN.match(raw)
    if slash_match:
        product_code = str(slash_match.group("product_code") or "").strip().upper()
        if not _PRODUCT_CODE_PATTERN.match(product_code):
            raise CommandParseError(
                "Mã sản phẩm chưa hợp lệ. Chỉ dùng chữ/số và ký tự _ - . (tối đa 64 ký tự)."
            )
        keyword_text = str(slash_match.group("keywords") or "").strip()
        return MediaCommand(product_code=product_code, keyword_text=keyword_text)

    natural_match = _NATURAL_MEDIA_PREFIX_PATTERN.match(raw)
    if natural_match:
        rest = raw[natural_match.end() :].strip()
        product_code, keyword_text = _extract_natural_product_code_and_keywords(rest)
        return MediaCommand(product_code=product_code, keyword_text=keyword_text)

    raise CommandParseError(
        "Cú pháp chưa đúng. Khi gửi ảnh, anh có thể ghi:\n"
        "- Tìm media\n"
        "- Tìm media JC123 váy hoa\n"
        "- /media SKU123 váy hoa nữ"
    )



def _extract_natural_product_code_and_keywords(rest: str) -> tuple[str, str]:
    raw = str(rest or "").strip()
    if not raw:
        return "", ""

    labelled = _LABELLED_PRODUCT_CODE_PATTERN.search(raw)
    if labelled:
        product_code = str(labelled.group("product_code") or "").strip().upper()
        if not _PRODUCT_CODE_PATTERN.match(product_code):
            raise CommandParseError("Mã sản phẩm trong caption chưa hợp lệ.")
        keyword_text = (raw[: labelled.start()] + " " + raw[labelled.end() :]).strip()
        keyword_text = re.sub(r"\s+", " ", keyword_text).strip()
        return product_code, keyword_text

    tokens = raw.split()
    first = tokens[0].strip()
    if _looks_like_product_code(first):
        product_code = first.upper()
        keyword_text = " ".join(tokens[1:]).strip()
        return product_code, keyword_text
    return "", raw



def _looks_like_product_code(token: str) -> bool:
    value = str(token or "").strip()
    if not _PRODUCT_CODE_PATTERN.match(value):
        return False
    upper = value.upper()
    has_digit = any(ch.isdigit() for ch in value)
    has_symbol = any(ch in {"-", "_", "."} for ch in value)
    has_prefix = upper.startswith("JC") or upper.startswith("SKU")
    return has_digit or has_symbol or has_prefix



def is_media_command_text(text: str) -> bool:
    raw = str(text or "").strip()
    if raw.startswith("/"):
        return raw.lower().startswith("/media")
    return _is_natural_media_text(raw)



def _is_natural_media_text(text: str) -> bool:
    normalized = _normalize_text(text)
    return normalized.startswith("tim media")



def _normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFD", str(text or ""))
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accents.lower().replace("đ", "d")
    return re.sub(r"\s+", " ", lowered).strip()
