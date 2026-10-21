from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
import uuid

from app.media_settings import MediaSettings
from app.utils import dump_json, load_json, now_utc_iso


CLONE_STATUSES = {
    "draft",
    "awaiting_platform",
    "ready_for_codex",
    "extracting",
    "ready_to_publish",
    "published",
    "partial",
    "failed",
    "cancelled",
}

_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "instagr.am"}
_PRODUCT_CLONE_PATTERN = re.compile(
    r"^\s*clone(?:@\w+)?\s+(?P<product_code>[A-Za-z0-9][A-Za-z0-9_.-]{1,63})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CloneSource:
    url: str
    platform: str


def parse_clone_product_command(text: str) -> str | None:
    match = _PRODUCT_CLONE_PATTERN.match(str(text or ""))
    if not match:
        return None
    return str(match.group("product_code") or "").strip().upper()


def extract_url(text: str) -> str:
    match = _URL_PATTERN.search(str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}>")


def parse_clone_source_url(text: str) -> CloneSource | None:
    raw_url = extract_url(text)
    if not raw_url:
        return None
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in _FACEBOOK_HOSTS:
        platform = "facebook"
    elif host in _INSTAGRAM_HOSTS:
        platform = "instagram"
    else:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.path.strip("/"):
        return None
    normalized = normalize_clone_source_url(raw_url)
    return CloneSource(url=normalized, platform=platform)


def normalize_clone_source_url(url: str) -> str:
    """Normalize a post URL so tracking params cannot defeat duplicate checks."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not parsed.scheme or not host or not parsed.path.strip("/"):
        return ""
    path = parsed.path.rstrip("/") or "/"
    # A post identity is in the path for supported FB/IG post URLs. Keep a
    # query only for unknown URL shapes; fbclid/utm parameters are never useful.
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def clone_dedupe_key(
    *,
    source_url: str,
    caption: str,
    targets: Iterable[str],
    profile_id: str,
) -> str:
    target_text = ",".join(sorted({str(item).strip().lower() for item in targets if str(item).strip()}))
    raw = "\n".join([str(source_url).strip(), str(caption), target_text, str(profile_id).strip().lower()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def media_hash_fingerprint(media_hashes: Iterable[str]) -> str:
    normalized = [str(item).strip().lower() for item in media_hashes if str(item).strip()]
    return hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest() if normalized else ""


def clone_source_fingerprint(
    *,
    source_url: str = "",
    source_post_id: str = "",
    media_hashes: Iterable[str] = (),
) -> str:
    normalized_hashes = [str(item).strip().lower() for item in media_hashes if str(item).strip()]
    raw = "\n".join(
        [normalize_clone_source_url(source_url), str(source_post_id).strip(), "|".join(normalized_hashes)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw.strip() else ""


def load_clone_profiles(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = load_json(path)
    return payload if isinstance(payload, dict) else {}


def get_clone_profile(path: Path, profile_id: str) -> dict[str, Any]:
    profiles = load_clone_profiles(path)
    raw_profiles = profiles.get("profiles", profiles)
    profile = raw_profiles.get(profile_id) if isinstance(raw_profiles, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f"Không tìm thấy profile clone: {profile_id}")
    return dict(profile)


def split_instagram_media(media: list[dict[str, Any]], *, max_carousel: int = 10) -> list[dict[str, Any]]:
    """Create image carousel/video batches while retaining source order."""
    if max_carousel < 1:
        raise ValueError("max_carousel phải lớn hơn 0")
    batches: list[dict[str, Any]] = []
    image_buffer: list[dict[str, Any]] = []

    def flush_images() -> None:
        nonlocal image_buffer
        for start in range(0, len(image_buffer), max_carousel):
            chunk = image_buffer[start : start + max_carousel]
            batches.append({"kind": "carousel", "media": chunk})
        image_buffer = []

    for index, item in enumerate(media):
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized.setdefault("order", index)
        media_type = str(normalized.get("media_type", normalized.get("type", "image"))).lower()
        if media_type in {"video", "reel", "movie"}:
            flush_images()
            batches.append({"kind": "video", "media": [normalized]})
        else:
            image_buffer.append(normalized)
    flush_images()
    return batches


def build_part_caption(caption: str, part: int, total: int, template: str = "\n\nPart {part}/{total}") -> str:
    return f"{caption}{template.format(part=part, total=total)}"


def build_ruza_th_caption(
    translated_caption: str,
    *,
    product_codes: Iterable[str],
    profile: dict[str, Any],
) -> str:
    """Clean the translated source caption and append the fixed RUZA footer."""
    remove_lines = {
        str(item).strip().casefold()
        for item in profile.get("caption_remove_lines", ["Nhắn tin ngay cho RUZA Nàng nhé!"])
        if str(item).strip()
    }
    body_lines = [
        line.rstrip()
        for line in str(translated_caption or "").splitlines()
        if line.strip().casefold() not in remove_lines
    ]
    body = "\n".join(body_lines).strip()
    unique_codes: list[str] = []
    for code in product_codes:
        normalized = str(code or "").strip().upper()
        if normalized and normalized not in unique_codes:
            unique_codes.append(normalized)
    code_text = ", ".join(unique_codes)
    footer_template = str(profile.get("caption_footer_template", "")).strip()
    footer = footer_template.format(
        product_codes=code_text,
        instagram_url=str(profile.get("instagram_url", "https://www.instagram.com/ruza.thailand/")),
        facebook_url=str(profile.get("facebook_url", "https://www.facebook.com/ruza.th")),
    ).strip()
    return f"{body}\n\n{footer}".strip() if body else footer


def remove_ruza_caption_lines(text: str, profile: dict[str, Any]) -> str:
    """Remove configured source-caption CTA lines before translation."""
    remove_lines = {
        str(item).strip().casefold()
        for item in profile.get("caption_remove_lines", ["Nhắn tin ngay cho RUZA Nàng nhé!"])
        if str(item).strip()
    }
    return "\n".join(
        line.rstrip()
        for line in str(text or "").splitlines()
        if line.strip().casefold() not in remove_lines
    ).strip()


def new_clone_manifest(
    *,
    job_id: str,
    source: CloneSource,
    caption: str,
    creator_id: int,
    chat_id: int,
    profile_id: str,
    profile: dict[str, Any],
    message_id: int | None = None,
) -> dict[str, Any]:
    timestamp = now_utc_iso()
    return {
        "schema_version": 1,
        "job_id": job_id,
        "status": "draft",
        "source": {"url": source.url, "platform": source.platform},
        "source_url": source.url,
        "source_platform": source.platform,
        "source_post_id": "",
        "source_fingerprint": "",
        "caption": caption,
        "targets": [],
        "profile_id": profile_id,
        "destination_profile": profile,
        "created_by": {"telegram_user_id": creator_id, "chat_id": chat_id, "message_id": message_id},
        "created_at": timestamp,
        "updated_at": timestamp,
        "dedupe_key": "",
        "duplicate_filter": {"product_code": "", "exclude_sources": [], "target_page_history_check": True},
        "media": [],
        "media_hashes": [],
        "batches": [],
        "platform_results": {},
        "errors": [],
        "result_path": "",
        "codex_instruction": f"đăng job {job_id}",
    }


def new_product_clone_manifest(
    *,
    job_id: str,
    product_code: str,
    creator_id: int,
    chat_id: int,
    profile_id: str,
    profile: dict[str, Any],
    duplicate_filter: dict[str, Any] | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    timestamp = now_utc_iso()
    normalized_code = str(product_code).strip().upper()
    selector = {
        "type": "product_code",
        "value": normalized_code,
        "page_url": str(profile.get("source_facebook_page_url", "https://www.facebook.com/ruza.vn")),
    }
    return {
        "schema_version": 1,
        "job_id": job_id,
        "status": "awaiting_platform",
        "source": {"url": "", "platform": "facebook", "selector": selector},
        "source_url": "",
        "source_platform": "facebook",
        "source_post_id": "",
        "source_fingerprint": "",
        "source_selector": selector,
        "requested_product_code": normalized_code,
        "caption": "",
        "caption_mode": "translate_source_to_thai",
        "caption_policy": {
            "remove_lines": profile.get("caption_remove_lines", ["Nhắn tin ngay cho RUZA Nàng nhé!"]),
            "footer_template": profile.get("caption_footer_template", ""),
            "extract_product_codes_from_source": True,
        },
        "targets": [],
        "profile_id": profile_id,
        "destination_profile": profile,
        "created_by": {"telegram_user_id": creator_id, "chat_id": chat_id, "message_id": message_id},
        "created_at": timestamp,
        "updated_at": timestamp,
        "dedupe_key": "",
        "duplicate_filter": dict(
            duplicate_filter
            or {"product_code": normalized_code, "exclude_sources": [], "target_page_history_check": True}
        ),
        "media": [],
        "media_hashes": [],
        "batches": [],
        "platform_results": {},
        "errors": [],
        "result_path": "",
        "codex_instruction": f"đăng job {job_id}",
    }


class MediaCloneStorageService:
    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings
        self.jobs_dir = settings.clone_jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def generate_job_id(self) -> str:
        return f"clone_{uuid.uuid4().hex[:12]}"

    def job_dir(self, job_id: str) -> Path:
        self._validate_job_id(job_id)
        return self.jobs_dir / job_id

    def manifest_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "manifest.json"

    def result_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "result.json"

    def save_job(self, payload: dict[str, Any]) -> Path:
        job_id = str(payload.get("job_id", "")).strip()
        self._validate_job_id(job_id)
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        record = dict(payload)
        record.setdefault("updated_at", now_utc_iso())
        dump_json(self.manifest_path(job_id), record)
        return self.manifest_path(job_id)

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        path = self.manifest_path(job_id)
        if not path.exists():
            return None
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.load_job(job_id)
        if not current:
            raise FileNotFoundError(f"Không tìm thấy clone job: {job_id}")
        payload = dict(current)
        payload.update(dict(updates))
        status = str(payload.get("status", "")).strip()
        if status not in CLONE_STATUSES:
            raise ValueError(f"Trạng thái clone không hợp lệ: {status}")
        payload["updated_at"] = now_utc_iso()
        self.save_job(payload)
        return payload

    def save_result(self, job_id: str, result: dict[str, Any]) -> Path:
        path = self.result_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(path, dict(result))
        return path

    def load_result(self, job_id: str) -> dict[str, Any] | None:
        path = self.result_path(job_id)
        if not path.exists():
            return None
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None

    def find_active_draft(self, *, chat_id: int, creator_id: int) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("*/manifest.json"):
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            owner = payload.get("created_by", {})
            if not isinstance(owner, dict):
                continue
            try:
                owner_matches = int(owner.get("chat_id", -1)) == int(chat_id) and int(owner.get("telegram_user_id", -1)) == int(creator_id)
            except (TypeError, ValueError):
                owner_matches = False
            if not owner_matches:
                continue
            if str(payload.get("status", "")) in {"draft", "awaiting_platform"}:
                candidates.append(payload)
        candidates.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return candidates[0] if candidates else None

    def find_duplicate(self, dedupe_key: str) -> dict[str, Any] | None:
        needle = str(dedupe_key or "").strip()
        if not needle:
            return None
        found: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("*/manifest.json"):
            payload = load_json(path)
            if isinstance(payload, dict) and str(payload.get("dedupe_key", "")) == needle:
                found.append(payload)
        found.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return found[0] if found else None

    def find_source_duplicate(
        self,
        *,
        source_url: str,
        targets: Iterable[str],
        profile_id: str,
        exclude_job_id: str = "",
    ) -> dict[str, Any] | None:
        normalized_url = normalize_clone_source_url(source_url)
        if not normalized_url:
            return None
        target_set = {str(item).strip().lower() for item in targets if str(item).strip()}
        for path in self.jobs_dir.glob("*/manifest.json"):
            payload = load_json(path)
            if not isinstance(payload, dict) or str(payload.get("job_id", "")) == exclude_job_id:
                continue
            if str(payload.get("profile_id", "")) != str(profile_id):
                continue
            if str(payload.get("status", "")) == "cancelled":
                continue
            source = payload.get("source", {}) if isinstance(payload.get("source"), dict) else {}
            existing_url = normalize_clone_source_url(str(source.get("url", "") or payload.get("source_url", "")))
            existing_targets = {str(item).strip().lower() for item in payload.get("targets", []) if str(item).strip()}
            if existing_url == normalized_url and target_set.intersection(existing_targets):
                return payload
        return None

    def build_product_duplicate_filter(self, product_code: str) -> dict[str, Any]:
        normalized_code = str(product_code or "").strip().upper()
        entries: list[dict[str, Any]] = []
        for path in self.jobs_dir.glob("*/manifest.json"):
            payload = load_json(path)
            if not isinstance(payload, dict) or str(payload.get("status", "")) == "cancelled":
                continue
            selector = payload.get("source_selector", {}) if isinstance(payload.get("source_selector"), dict) else {}
            requested = str(payload.get("requested_product_code", selector.get("value", ""))).strip().upper()
            source = payload.get("source", {}) if isinstance(payload.get("source"), dict) else {}
            source_text = " ".join(
                [
                    str(payload.get("caption", "")),
                    str(payload.get("source_url", "")),
                    str(source.get("url", "")),
                    " ".join(str(item) for item in payload.get("product_codes", []) if item),
                ]
            ).upper()
            if requested != normalized_code and not re.search(
                rf"(?<![A-Z0-9]){re.escape(normalized_code)}(?![A-Z0-9])", source_text
            ):
                continue
            source_url = normalize_clone_source_url(str(source.get("url", "") or payload.get("source_url", "")))
            source_post_id = str(payload.get("source_post_id", "")).strip()
            media_hashes = [str(item).strip().lower() for item in payload.get("media_hashes", []) if str(item).strip()]
            if isinstance(payload.get("media"), list):
                media_hashes.extend(
                    str(item.get("sha256", item.get("hash", ""))).strip().lower()
                    for item in payload["media"]
                    if isinstance(item, dict) and str(item.get("sha256", item.get("hash", ""))).strip()
                )
            target_platforms = self._published_or_reserved_targets(payload)
            if source_url or source_post_id or media_hashes:
                entries.append(
                    {
                        "job_id": str(payload.get("job_id", "")),
                        "source_url": source_url,
                        "source_post_id": source_post_id,
                        "media_hashes": sorted(set(media_hashes)),
                        "target_platforms": sorted(target_platforms),
                        "status": str(payload.get("status", "")),
                    }
                )
        return {"product_code": normalized_code, "exclude_sources": entries, "generated_at": now_utc_iso()}

    @staticmethod
    def _published_or_reserved_targets(payload: dict[str, Any]) -> set[str]:
        targets = {str(item).strip().lower() for item in payload.get("targets", []) if str(item).strip()}
        status = str(payload.get("status", "")).strip()
        if status in {"published", "ready_for_codex", "extracting", "ready_to_publish"}:
            return targets
        results = payload.get("platform_results", {}) if isinstance(payload.get("platform_results"), dict) else {}
        published: set[str] = set()
        for platform, result in results.items():
            if isinstance(result, dict) and (result.get("url") or str(result.get("status", "")).lower() in {"published", "posted", "success"}):
                published.add(str(platform).strip().lower())
        return published

    @staticmethod
    def candidate_matches_duplicate_filter(
        candidate: dict[str, Any],
        duplicate_filter: dict[str, Any],
        target_platforms: Iterable[str],
    ) -> bool:
        source_url = normalize_clone_source_url(str(candidate.get("source_url", "")))
        source_post_id = str(candidate.get("source_post_id", "")).strip()
        candidate_hashes = {str(item).strip().lower() for item in candidate.get("media_hashes", []) if str(item).strip()}
        targets = {str(item).strip().lower() for item in target_platforms if str(item).strip()}
        for entry in duplicate_filter.get("exclude_sources", []) if isinstance(duplicate_filter, dict) else []:
            if not isinstance(entry, dict):
                continue
            excluded_targets = {str(item).strip().lower() for item in entry.get("target_platforms", []) if str(item).strip()}
            if not targets.intersection(excluded_targets):
                continue
            if source_url and source_url == normalize_clone_source_url(str(entry.get("source_url", ""))):
                return True
            if source_post_id and source_post_id == str(entry.get("source_post_id", "")).strip():
                return True
            excluded_hashes = {str(item).strip().lower() for item in entry.get("media_hashes", []) if str(item).strip()}
            if candidate_hashes and excluded_hashes.intersection(candidate_hashes):
                return True
        return False

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not re.fullmatch(r"clone_[a-f0-9]{6,32}", str(job_id or "")):
            raise ValueError(f"job_id không hợp lệ: {job_id}")
