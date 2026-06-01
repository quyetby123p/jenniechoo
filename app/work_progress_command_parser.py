from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.exceptions import CommandParseError


@dataclass(frozen=True)
class WorkProgressCommand:
    action: str
    args: dict[str, Any] = field(default_factory=dict)


def is_work_progress_command(text: str) -> bool:
    raw = str(text or "").strip().lower()
    return raw.startswith("/progress")


def parse_work_progress_command(text: str) -> WorkProgressCommand:
    raw = str(text or "").strip()
    if not raw:
        raise CommandParseError("Lệnh progress đang trống.")

    tokens = raw.split()
    head = tokens[0].strip().lower()
    if not head.startswith("/progress"):
        raise CommandParseError("Lệnh progress phải bắt đầu bằng /progress.")

    action = tokens[1].strip().lower() if len(tokens) > 1 else "help"
    rest = raw.split(None, 2)[2].strip() if len(raw.split(None, 2)) >= 3 else ""

    if action in {"help", "h"}:
        return WorkProgressCommand(action="help")

    if action in {"pending", "queue"}:
        limit = 20
        if rest:
            try:
                limit = int(rest)
            except ValueError:
                raise CommandParseError("Limit của /progress pending phải là số nguyên.") from None
        return WorkProgressCommand(action="pending", args={"limit": max(1, min(100, limit))})

    if action in {"unmapped", "identity", "pending-identity"}:
        limit = 20
        if rest:
            try:
                limit = int(rest)
            except ValueError:
                raise CommandParseError("Limit của /progress unmapped phải là số nguyên.") from None
        return WorkProgressCommand(action="unmapped", args={"limit": max(1, min(100, limit))})

    if action in {"approve", "ok"}:
        if len(tokens) < 3:
            raise CommandParseError("Anh dùng: /progress approve <update_id> [ghi chú]")
        update_id = tokens[2].strip()
        note = raw.split(None, 3)[3].strip() if len(raw.split(None, 3)) >= 4 else ""
        return WorkProgressCommand(action="approve", args={"update_id": update_id, "note": note})

    if action in {"reject", "deny"}:
        if len(tokens) < 3:
            raise CommandParseError("Anh dùng: /progress reject <update_id> [ghi chú]")
        update_id = tokens[2].strip()
        note = raw.split(None, 3)[3].strip() if len(raw.split(None, 3)) >= 4 else ""
        return WorkProgressCommand(action="reject", args={"update_id": update_id, "note": note})

    if action == "report":
        if len(tokens) < 3:
            raise CommandParseError("Anh dùng: /progress report <daily|weekly|monthly> [YYYY-MM-DD]")
        report_type = tokens[2].strip().lower()
        if report_type not in {"daily", "weekly", "monthly"}:
            raise CommandParseError("report_type chỉ nhận: daily|weekly|monthly.")
        anchor_date = None
        if len(tokens) >= 4:
            date_raw = tokens[3].strip()
            try:
                anchor_date = date.fromisoformat(date_raw)
            except ValueError:
                raise CommandParseError("Ngày report phải theo định dạng YYYY-MM-DD.") from None
        return WorkProgressCommand(action="report", args={"report_type": report_type, "anchor_date": anchor_date})

    if action == "map":
        segments = [item.strip() for item in rest.split("|")]
        if len(segments) < 3:
            raise CommandParseError(
                "Anh dùng: /progress map <member_id> | <platform> | <platform_user_id> | [display_name]"
            )
        member_id = segments[0]
        platform = segments[1].lower()
        platform_user_id = segments[2]
        display_name = segments[3] if len(segments) >= 4 else ""
        if not member_id or not platform or not platform_user_id:
            raise CommandParseError("map cần đủ member_id, platform, platform_user_id.")
        return WorkProgressCommand(
            action="map",
            args={
                "member_id": member_id,
                "platform": platform,
                "platform_user_id": platform_user_id,
                "display_name": display_name,
            },
        )

    if action == "edit":
        segments = [item.strip() for item in rest.split("|")]
        if len(segments) < 3:
            raise CommandParseError(
                "Anh dùng: /progress edit <update_id> | <status> | <progress_pct> | [blocker] | [next_step] | [deadline]"
            )
        update_id = segments[0]
        status = segments[1].lower()
        try:
            progress_pct = int(segments[2])
        except ValueError:
            raise CommandParseError("progress_pct trong /progress edit phải là số nguyên.") from None
        if status not in {"todo", "doing", "blocked", "done"}:
            raise CommandParseError("status edit phải là todo|doing|blocked|done.")
        patch: dict[str, Any] = {"status": status, "progress_pct": max(0, min(100, progress_pct))}
        if len(segments) >= 4 and segments[3]:
            patch["blocker"] = segments[3]
        if len(segments) >= 5 and segments[4]:
            patch["next_step"] = segments[4]
        if len(segments) >= 6 and segments[5]:
            patch["deadline_date"] = segments[5]
        return WorkProgressCommand(action="edit", args={"update_id": update_id, "patch": patch})

    raise CommandParseError(
        "Lệnh progress chưa hỗ trợ. Dùng: /progress help | pending | unmapped | approve | reject | edit | map | report"
    )
