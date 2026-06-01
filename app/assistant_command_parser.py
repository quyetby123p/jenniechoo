from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

from app.assistant_models import AssistantIntent, ParsedAssistantCommand
from app.exceptions import CommandParseError


def parse_assistant_command(text: str, timezone_name: str) -> ParsedAssistantCommand:
    raw = (text or "").strip()
    if not raw:
        return ParsedAssistantCommand(intent=AssistantIntent.UNKNOWN, raw_text=raw)

    lowered = raw.lower()
    normalized = _normalize_text(raw)

    if lowered.startswith("/task"):
        task_action, task_args = _parse_task_command_payload(raw)
        return ParsedAssistantCommand(
            intent=AssistantIntent.TASK,
            raw_text=raw,
            task_action=task_action,
            task_args=task_args,
        )

    if lowered.startswith("/ask"):
        question = raw[4:].strip()
        if not question:
            raise CommandParseError("Anh dùng: /ask <câu hỏi>")
        return ParsedAssistantCommand(
            intent=AssistantIntent.GENERAL_QA,
            raw_text=raw,
            question_text=question,
        )

    if lowered.startswith("/run"):
        payload = raw[4:].strip()
        if not payload:
            raise CommandParseError(
                "Anh dùng:\n"
                "/run report [YYYY-MM-DD|hôm qua]\n"
                "/run reconcile cod [YYYY-MM-DD|hôm qua]\n"
                "/run reconcile sheet <run_id>\n"
                "/run media sheet <run_id>"
            )
        action_name, action_args = _parse_action_payload(payload, timezone_name)
        return ParsedAssistantCommand(
            intent=AssistantIntent.ACTION,
            raw_text=raw,
            action_name=action_name,
            action_args=action_args,
        )

    if lowered.startswith("/agenda"):
        payload = raw[7:].strip()
        parsed = _parse_human_date(payload, timezone_name) if payload else None
        if payload and parsed is None:
            raise CommandParseError("Ngày lịch chưa đúng. Ví dụ: /agenda 2026-05-19 hoặc /agenda hôm nay")
        return ParsedAssistantCommand(
            intent=AssistantIntent.AGENDA,
            raw_text=raw,
            date_value=parsed,
        )

    if lowered.startswith("/plan"):
        payload = raw[5:].strip()
        payload_norm = _normalize_text(payload)
        if payload_norm in {"tuan nay", "week"}:
            return ParsedAssistantCommand(
                intent=AssistantIntent.PLAN,
                raw_text=raw,
                week_mode=True,
            )
        parsed = _parse_human_date(payload, timezone_name) if payload else None
        if payload and parsed is None:
            raise CommandParseError("Ngày kế hoạch chưa đúng. Ví dụ: /plan tuần này hoặc /plan 2026-05-19")
        return ParsedAssistantCommand(
            intent=AssistantIntent.PLAN,
            raw_text=raw,
            date_value=parsed,
            week_mode=False,
        )

    if lowered.startswith("/result"):
        payload = raw[7:].strip()
        parsed = _parse_human_date(payload, timezone_name) if payload else None
        if payload and parsed is None:
            raise CommandParseError("Ngày kết quả chưa đúng. Ví dụ: /result hôm qua hoặc /result 2026-05-18")
        return ParsedAssistantCommand(
            intent=AssistantIntent.RESULT,
            raw_text=raw,
            date_value=parsed,
        )

    if normalized.startswith("lich"):
        remainder = normalized[len("lich") :].strip()
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Câu lệnh lịch chưa đúng. Ví dụ: lịch hôm nay | lịch ngày mai | lịch 2026-05-19")
        return ParsedAssistantCommand(
            intent=AssistantIntent.AGENDA,
            raw_text=raw,
            date_value=parsed,
        )

    if normalized.startswith("ke hoach"):
        remainder = normalized[len("ke hoach") :].strip()
        if remainder in {"tuan nay", "week"}:
            return ParsedAssistantCommand(
                intent=AssistantIntent.PLAN,
                raw_text=raw,
                week_mode=True,
            )
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError(
                "Câu lệnh kế hoạch chưa đúng. Ví dụ: kế hoạch hôm nay | kế hoạch tuần này | kế hoạch 2026-05-19"
            )
        return ParsedAssistantCommand(
            intent=AssistantIntent.PLAN,
            raw_text=raw,
            date_value=parsed,
            week_mode=False,
        )

    if normalized.startswith("ket qua"):
        remainder = normalized[len("ket qua") :].strip()
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Câu lệnh kết quả chưa đúng. Ví dụ: kết quả hôm nay | kết quả hôm qua")
        return ParsedAssistantCommand(
            intent=AssistantIntent.RESULT,
            raw_text=raw,
            date_value=parsed,
        )

    if normalized.startswith("hoi "):
        question = raw.split(maxsplit=1)[1].strip()
        if not question:
            raise CommandParseError("Anh nhập thêm nội dung sau từ 'hỏi'.")
        return ParsedAssistantCommand(
            intent=AssistantIntent.GENERAL_QA,
            raw_text=raw,
            question_text=question,
        )

    action_match = _match_natural_action(raw, timezone_name)
    if action_match is not None:
        action_name, action_args = action_match
        return ParsedAssistantCommand(
            intent=AssistantIntent.ACTION,
            raw_text=raw,
            action_name=action_name,
            action_args=action_args,
        )

    task_natural = _parse_natural_task_query(normalized)
    if task_natural is not None:
        action_name, action_args = task_natural
        return ParsedAssistantCommand(
            intent=AssistantIntent.TASK,
            raw_text=raw,
            task_action=action_name,
            task_args=action_args,
        )

    return ParsedAssistantCommand(
        intent=AssistantIntent.GENERAL_QA,
        raw_text=raw,
        question_text=raw,
    )


def _parse_task_command_payload(raw: str) -> tuple[str, dict[str, Any]]:
    payload = (raw or "")[5:].strip()
    if not payload:
        return "list", {"status": ""}

    parts = payload.split(maxsplit=1)
    head = _normalize_text(parts[0])
    tail = parts[1].strip() if len(parts) > 1 else ""

    if head == "add":
        if not tail:
            raise CommandParseError("Anh dùng: /task add <tieu de> hoặc /task add self|manager | <tieu de> | <ghi chú>")
        fields = [_clean_field(item) for item in tail.split("|")]
        if not fields:
            raise CommandParseError("Thiếu tiêu đề công việc.")

        source_type = "manager"
        note = ""
        title = fields[0]
        if len(fields) >= 2 and _normalize_text(fields[0]) in {"self", "manager", "sep", "boss"}:
            source_type = "self" if _normalize_text(fields[0]) == "self" else "manager"
            title = fields[1]
            note = fields[2] if len(fields) >= 3 else ""
        else:
            note = fields[1] if len(fields) >= 2 else ""
        if not title:
            raise CommandParseError("Thiếu tiêu đề công việc.")
        return "add", {"title": title, "source_type": source_type, "note": note}

    if head == "update":
        fields = [_clean_field(item) for item in tail.split("|")]
        if len(fields) < 4:
            raise CommandParseError(
                "Anh dùng: /task update <ten viec> | <status> | <percent> | <ghi chú> | <ly do blocked> | <buoc tiep theo>"
            )
        title = fields[0]
        status = fields[1]
        percent_raw = fields[2]
        note = fields[3]
        blocked_reason = fields[4] if len(fields) >= 5 else ""
        next_step = fields[5] if len(fields) >= 6 else ""
        if not title:
            raise CommandParseError("Thiếu tên việc cần cập nhật.")
        try:
            percent = int(percent_raw)
        except ValueError:
            raise CommandParseError("Tiến độ phải là số nguyên 0-100.") from None
        if percent < 0 or percent > 100:
            raise CommandParseError("Tiến độ phải nằm trong khoảng 0-100.")
        return "update", {
            "title": title,
            "status": status,
            "progress_percent": percent,
            "note": note,
            "blocked_reason": blocked_reason,
            "next_step": next_step,
        }

    if head == "done":
        if not tail:
            return "done_report", {}
        normalized_tail = _normalize_text(tail)
        if normalized_tail in {"report", "list", "all"}:
            return "done_report", {}
        fields = [_clean_field(item) for item in tail.split("|")]
        title = fields[0] if fields else ""
        if not title:
            raise CommandParseError("Thiếu tên việc cần chốt done.")
        note = fields[1] if len(fields) >= 2 else ""
        return "done", {"title": title, "note": note}

    if head == "list":
        normalized_status = _normalize_text(tail)
        if normalized_status in {"all", "tat ca", ""}:
            return "list", {"status": ""}
        if normalized_status in {"pending", "open", "chua xong", "chua hoan thanh"}:
            return "list", {"status": "pending"}
        if normalized_status not in {"todo", "doing", "blocked", "done"}:
            raise CommandParseError("Task list hỗ trợ: all|todo|doing|blocked|done|pending.")
        return "list", {"status": normalized_status}

    if head == "report":
        return "report", {}

    if head == "week":
        return "week", {}

    if head == "pending":
        return "pending_report", {}

    if head == "pick":
        if not tail:
            raise CommandParseError("Anh dùng: /task pick <request_id> <index>")
        pick_parts = tail.split()
        if len(pick_parts) < 2:
            raise CommandParseError("Anh dùng: /task pick <request_id> <index>")
        request_id = _clean_field(pick_parts[0])
        try:
            candidate_index = int(str(pick_parts[1]).strip())
        except ValueError:
            raise CommandParseError("Index chọn task phải là số nguyên.") from None
        return "pick", {"request_id": request_id, "candidate_index": candidate_index}

    raise CommandParseError(
        "Task command chưa hỗ trợ. Mẫu dùng:\n"
        "/task add <tieu de>\n"
        "/task update <ten viec> | <status> | <percent> | <ghi chú> | <ly do blocked> | <buoc tiep theo>\n"
        "/task done <ten viec> | <ghi chú>\n"
        "/task list [all|todo|doing|blocked|done|pending]\n"
        "/task report | /task week | /task pending"
    )


def _parse_natural_task_query(normalized: str) -> tuple[str, dict[str, Any]] | None:
    text = _normalize_text(normalized)
    if not text:
        return None
    if (
        text.startswith("bao cao tien do")
        or text.startswith("tien do cong viec")
        or text.startswith("bao cao cong viec")
        or text.startswith("tien do")
    ):
        return "report", {}
    if text.startswith("tong ket tuan") or text.startswith("bao cao tuan") or text.startswith("tiendo tuan"):
        return "week", {}
    if "chua hoan thanh" in text or text.startswith("task pending") or "viec chua xong" in text:
        return "pending_report", {}
    if "da hoan thanh" in text or text.startswith("task done") or "viec da xong" in text:
        return "done_report", {}
    return None


def _match_natural_action(raw: str, timezone_name: str) -> tuple[str, dict[str, str]] | None:
    normalized = _normalize_text(raw)
    if normalized.startswith("chay bao cao") or normalized.startswith("tao bao cao"):
        remainder = normalized.replace("chay bao cao", "", 1).replace("tao bao cao", "", 1).strip()
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Ngày báo cáo chưa đúng.")
        return "daily_report", {"report_date": parsed.isoformat() if parsed else ""}

    if normalized.startswith("chay doi soat cod") or normalized.startswith("doi soat cod"):
        remainder = normalized.replace("chay doi soat cod", "", 1).replace("doi soat cod", "", 1).strip()
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Ngày đối soát COD chưa đúng.")
        return "reconcile_cod_report", {"settlement_date": parsed.isoformat() if parsed else ""}

    if normalized.startswith("dong bo sheet doi soat") or normalized.startswith("sync sheet doi soat"):
        run_id = _extract_run_id(raw)
        if not run_id:
            raise CommandParseError("Thiếu run_id cho đồng bộ sheet đối soát.")
        return "reconcile_sheet_sync", {"run_id": run_id}

    if normalized.startswith("dong bo sheet media") or normalized.startswith("sync sheet media"):
        run_id = _extract_run_id(raw)
        if not run_id:
            raise CommandParseError("Thiếu run_id cho đồng bộ sheet media.")
        return "media_sheet_sync", {"run_id": run_id}

    return None


def _parse_action_payload(payload: str, timezone_name: str) -> tuple[str, dict[str, str]]:
    normalized = _normalize_text(payload)
    if normalized.startswith("report"):
        remainder = payload.split(maxsplit=1)[1].strip() if len(payload.split()) > 1 else ""
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Ngày báo cáo chưa đúng.")
        return "daily_report", {"report_date": parsed.isoformat() if parsed else ""}

    if normalized.startswith("reconcile cod"):
        remainder = payload.split(maxsplit=2)[2].strip() if len(payload.split()) > 2 else ""
        parsed = _parse_human_date(remainder, timezone_name) if remainder else None
        if remainder and parsed is None:
            raise CommandParseError("Ngày đối soát COD chưa đúng.")
        return "reconcile_cod_report", {"settlement_date": parsed.isoformat() if parsed else ""}

    if normalized.startswith("reconcile sheet"):
        run_id = _extract_run_id(payload)
        if not run_id:
            raise CommandParseError("Thiếu run_id cho reconcile sheet sync.")
        return "reconcile_sheet_sync", {"run_id": run_id}

    if normalized.startswith("media sheet"):
        run_id = _extract_run_id(payload)
        if not run_id:
            raise CommandParseError("Thiếu run_id cho media sheet sync.")
        return "media_sheet_sync", {"run_id": run_id}

    raise CommandParseError(
        "Action chưa hỗ trợ. Anh dùng một trong các mẫu:\n"
        "/run report [YYYY-MM-DD|hôm qua]\n"
        "/run reconcile cod [YYYY-MM-DD|hôm qua]\n"
        "/run reconcile sheet <run_id>\n"
        "/run media sheet <run_id>"
    )


def _extract_run_id(raw: str) -> str:
    match = re.search(r"\b(run_[0-9a-zA-Z]+)\b", raw or "")
    if not match:
        return ""
    return str(match.group(1)).strip()


def _clean_field(raw: str) -> str:
    return " ".join(str(raw or "").split()).strip()


def _normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFD", text or "")
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    lowered = no_accents.lower().replace("đ", "d")
    return re.sub(r"\s+", " ", lowered).strip()


def _parse_human_date(raw: str, timezone_name: str) -> date | None:
    value = _normalize_text(str(raw or ""))
    if not value:
        return None

    today_local = datetime.now(_resolve_timezone(timezone_name)).date()
    if value in {"hom nay", "ngay hom nay"}:
        return today_local
    if value in {"hom qua", "ngay hom qua"}:
        return today_local - timedelta(days=1)
    if value in {"ngay mai", "hom sau"}:
        return today_local + timedelta(days=1)

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
    if candidate > today_local + timedelta(days=30):
        candidate = date(year - 1, month, day)
    return candidate


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Ho_Chi_Minh")
