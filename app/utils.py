from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_ALLOWED_POST_QUERY_KEYS = {"story_fbid", "id", "fbid", "v"}
_FACEBOOK_HOST_SUFFIXES = ("facebook.com", "fb.watch")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(value: str, show_tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= show_tail:
        return "*" * len(value)
    return "*" * (len(value) - show_tail) + value[-show_tail:]


def is_supported_facebook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    if not any(host.endswith(suffix) for suffix in _FACEBOOK_HOST_SUFFIXES):
        return False

    path = (parsed.path or "").lower()
    post_markers = ("/posts/", "/permalink.php", "/photo.php", "/videos/", "/reel/")
    if any(marker in path for marker in post_markers):
        return True

    query = parse_qs(parsed.query)
    return "story_fbid" in query and "id" in query


def normalize_facebook_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")

    query = parse_qs(parsed.query, keep_blank_values=False)
    normalized_q = {
        key: query[key][0]
        for key in sorted(query.keys())
        if key in _ALLOWED_POST_QUERY_KEYS and query[key]
    }
    query_string = urlencode(normalized_q, doseq=False)
    return urlunparse((parsed.scheme.lower(), host, path, "", query_string, ""))


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_post_slug(url: str, max_length: int = 24) -> str:
    normalized = normalize_facebook_url(url)
    parsed = urlparse(normalized)
    query = parse_qs(parsed.query)

    candidate = ""
    if "story_fbid" in query:
        candidate = query["story_fbid"][0]
    elif "fbid" in query:
        candidate = query["fbid"][0]
    else:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments:
            candidate = segments[-1]

    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", candidate).strip("-").lower()
    if not cleaned:
        cleaned = "post"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip("-")
    return cleaned


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def replace_placeholders(payload: Any, replacements: dict[str, str]) -> Any:
    if isinstance(payload, str):
        result = payload
        for key, value in replacements.items():
            result = result.replace("${" + key + "}", value)
        return result
    if isinstance(payload, list):
        return [replace_placeholders(item, replacements) for item in payload]
    if isinstance(payload, dict):
        return {
            key: replace_placeholders(value, replacements)
            for key, value in payload.items()
        }
    return payload


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
