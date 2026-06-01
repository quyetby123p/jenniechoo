from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import unicodedata


@dataclass(frozen=True)
class ExtractedProgress:
    task_key: str
    status: str
    progress_pct: int
    blocker: str
    next_step: str
    deadline_date: str
    confidence: float
    summary: str


_STATUS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "done": ("hoan thanh", "xong", "done", "completed", "da xong"),
    "blocked": ("blocked", "vuong", "ket", "bi chan", "chua co quyen", "dang loi"),
    "doing": ("dang lam", "in progress", "doing", "dang xu ly", "dang check"),
    "todo": ("todo", "to do", "chua lam", "chua bat dau", "pending"),
}

_TASK_LABEL_PATTERN = re.compile(r"(?:task|viec|dau viec)\s*[:\-]\s*(.+?)(?:[.;,\n]|$)", re.IGNORECASE)
_HASHTAG_TASK_PATTERN = re.compile(r"#([A-Za-z0-9_-]{3,48})")
_PERCENT_PATTERN = re.compile(r"\b([1-9]?\d|100)\s*%")
_BLOCKER_PATTERN = re.compile(
    r"(?:blocker|vuong|ket|bi chan|ly do)\s*[:\-]\s*(.+?)(?:[.;,\n]|$)",
    re.IGNORECASE,
)
_NEXT_STEP_PATTERN = re.compile(
    r"(?:next step|buoc tiep theo|se|sẽ)\s*[:\-]\s*(.+?)(?:[.;,\n]|$)",
    re.IGNORECASE,
)
_DEADLINE_PATTERN = re.compile(
    r"(?:deadline|han|truoc)\s*[:\-]?\s*([0-3]?\d/[0-1]?\d(?:/[0-9]{2,4})?|[0-9]{4}-[0-1]?\d-[0-3]?\d)",
    re.IGNORECASE,
)


def extract_progress_signal(message_text: str, *, context_text: str = "", today: date | None = None) -> ExtractedProgress | None:
    raw = str(message_text or "").strip()
    if not raw:
        return None

    normalized = _normalize_lookup(raw)
    context_normalized = _normalize_lookup(context_text or "")
    merged = " ".join(part for part in (normalized, context_normalized) if part).strip()
    if not merged:
        return None

    status, status_score = _detect_status(merged)
    progress_pct = _extract_progress_pct(raw)
    task_key, task_score = _extract_task_key(raw, normalized=normalized)
    blocker = _extract_single_field(_BLOCKER_PATTERN, raw)
    next_step = _extract_single_field(_NEXT_STEP_PATTERN, raw)
    deadline_date = _extract_deadline(raw, today=today)

    if not status and progress_pct <= 0:
        return None
    if not task_key:
        task_key = _fallback_task_key(raw)

    if not status:
        status = "doing" if progress_pct > 0 else "todo"
    if progress_pct <= 0:
        progress_pct = 100 if status == "done" else (30 if status == "doing" else 0)
    if status == "done":
        progress_pct = 100
    if status == "todo":
        progress_pct = min(progress_pct, 10)

    confidence = 0.35
    confidence += status_score
    confidence += task_score
    if progress_pct > 0:
        confidence += 0.15
    if blocker:
        confidence += 0.1
    if next_step:
        confidence += 0.1
    if deadline_date:
        confidence += 0.05
    confidence = round(max(0.0, min(0.99, confidence)), 4)

    summary = f"{task_key} | {status} | {progress_pct}%"
    if blocker:
        summary += f" | blocker: {blocker}"
    if next_step:
        summary += f" | next: {next_step}"

    return ExtractedProgress(
        task_key=task_key,
        status=status,
        progress_pct=progress_pct,
        blocker=blocker,
        next_step=next_step,
        deadline_date=deadline_date,
        confidence=confidence,
        summary=summary,
    )


def _normalize_lookup(text: str) -> str:
    folded = unicodedata.normalize("NFD", str(text or "").strip())
    no_accent = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accent.lower().replace("đ", "d")
    lowered = re.sub(r"[^a-z0-9\s:/%-]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _detect_status(text: str) -> tuple[str, float]:
    best_status = ""
    best_score = 0.0
    for status, keywords in _STATUS_KEYWORDS.items():
        for token in keywords:
            if token in text:
                score = 0.25 if status in {"doing", "todo"} else 0.3
                if score > best_score:
                    best_status = status
                    best_score = score
    return best_status, best_score


def _extract_progress_pct(text: str) -> int:
    match = _PERCENT_PATTERN.search(str(text or ""))
    if not match:
        return 0
    try:
        return max(0, min(100, int(match.group(1))))
    except (TypeError, ValueError):
        return 0


def _extract_task_key(text: str, *, normalized: str) -> tuple[str, float]:
    match_hash = _HASHTAG_TASK_PATTERN.search(str(text or ""))
    if match_hash:
        return match_hash.group(1).strip(), 0.2

    match_label = _TASK_LABEL_PATTERN.search(str(text or ""))
    if match_label:
        value = _clean_sentence(match_label.group(1))
        if value:
            return value[:80], 0.15

    if "task" in normalized or "viec" in normalized:
        fallback = _fallback_task_key(text)
        if fallback:
            return fallback, 0.08
    return "", 0.0


def _extract_single_field(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(str(text or ""))
    if not match:
        return ""
    return _clean_sentence(match.group(1))


def _extract_deadline(text: str, *, today: date | None = None) -> str:
    match = _DEADLINE_PATTERN.search(str(text or ""))
    if not match:
        return ""
    token = match.group(1).strip()
    if "-" in token:
        return _normalize_iso_date(token)
    return _normalize_dmy(token, today=today)


def _normalize_iso_date(token: str) -> str:
    parts = str(token).strip().split("-")
    if len(parts) != 3:
        return ""
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        parsed = date(year, month, day)
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat()


def _normalize_dmy(token: str, *, today: date | None = None) -> str:
    raw = str(token).strip()
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if len(parts) not in {2, 3}:
        return ""
    try:
        day = int(parts[0])
        month = int(parts[1])
        year = int(parts[2]) if len(parts) == 3 else (today.year if today else date.today().year)
        if year < 100:
            year += 2000
        parsed = date(year, month, day)
    except (TypeError, ValueError):
        return ""
    return parsed.isoformat()


def _fallback_task_key(text: str) -> str:
    words = [chunk for chunk in re.split(r"\s+", str(text or "").strip()) if chunk]
    if not words:
        return ""
    cleaned = " ".join(words[:8]).strip(".,;:! ")
    return cleaned[:80]


def _clean_sentence(text: str) -> str:
    return " ".join(str(text or "").split()).strip(".,;: ")

