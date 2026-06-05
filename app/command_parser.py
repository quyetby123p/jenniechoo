from __future__ import annotations

from datetime import date, datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from app.exceptions import CommandParseError, ValidationError
from app.models import AdsCommand
from app.utils import is_supported_facebook_url


_URL_PATTERN = re.compile(
    r"<?(?P<link>https?://[^\s<>]+)>?",
    re.IGNORECASE,
)
_EXISTING_MODE_PATTERN = re.compile(
    r"(?:lên\s*cũ|len\s*cu)(?:\s+camp\s+(?P<campaign_hint>[^.!?]+?))?\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_NEW_MODE_FLAG_PATTERN = re.compile(r"(?:lên\s*mới|len\s*moi)\s*[.!?]*\s*$", re.IGNORECASE)
_MANUAL_SKU_PATTERN = re.compile(r"(?<![0-9A-Z])JC[0-9A-Z]+(?![0-9A-Z])", re.IGNORECASE)
_LEADING_ADS_PREFIX_PATTERN = re.compile(r"^\s*/ads\b", re.IGNORECASE)
_BUDGET_PATTERNS = [
    re.compile(r"(?:budget)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
    re.compile(r"(?:ngân\s*sách)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
    re.compile(r"(?:ngan\s*sach)\s*[:=]?\s*<?\s*(?P<budget>[\d\.,\s]+)\s*>?", re.IGNORECASE),
]


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


def _extract_manual_sku_keywords(raw: str) -> list[str]:
    cleaned = _URL_PATTERN.sub(" ", raw)
    for pattern in _BUDGET_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _EXISTING_MODE_PATTERN.sub(" ", cleaned)
    cleaned = _NEW_MODE_FLAG_PATTERN.sub(" ", cleaned)
    cleaned = _LEADING_ADS_PREFIX_PATTERN.sub(" ", cleaned)

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
    has_existing_mode_flag = bool(existing_mode_match)
    has_new_mode_flag = bool(_NEW_MODE_FLAG_PATTERN.search(raw))
    if has_existing_mode_flag and has_new_mode_flag:
        raise CommandParseError(
            "Em thấy cả hai cờ `lên cũ` và `lên mới` trong cùng một lệnh.\n"
            "Anh chỉ dùng một cờ ở cuối câu:\n"
            "- Lên campaign cũ: <link> JCV140 lên cũ\n"
            "- Lên campaign cũ theo hint: <link> lên cũ camp video\n"
            "- Lên campaign mới: <link> ngân sách 300000 lên mới"
        )
    if not has_existing_mode_flag and not has_new_mode_flag:
        raise CommandParseError(
            "Để phân biệt luồng chạy, anh thêm cờ ở cuối câu:\n"
            "- Lên campaign cũ: <link> JCV140 lên cũ\n"
            "- Lên campaign cũ theo hint: <link> lên cũ camp video\n"
            "- Lên campaign mới: <link> ngân sách 300000 lên mới"
        )
    campaign_hint = ""
    if existing_mode_match:
        campaign_hint = re.sub(r"\s+", " ", str(existing_mode_match.group("campaign_hint") or "").strip())
    return has_existing_mode_flag, campaign_hint


def parse_ads_command(text: str) -> AdsCommand:
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
    manual_sku_keywords = _extract_manual_sku_keywords(raw) if use_existing_campaign else []

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
        if re.fullmatch(r"\d{6,}", sanitized):
            return True, sanitized

    raise CommandParseError(
        "Cú pháp lên đơn chưa đúng. Anh dùng:\n"
        "lên đơn hôm nay\n"
        "hoặc: lên đơn JCT310"
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
