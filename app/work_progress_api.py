from __future__ import annotations

from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.work_progress_service import WorkProgressService


class WorkProgressApiServer:
    def __init__(
        self,
        *,
        service: WorkProgressService,
        host: str,
        port: int,
        logger: logging.Logger,
    ) -> None:
        self.service = service
        self.host = host
        self.port = int(port)
        self.logger = logger
        handler_type = self._build_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler_type)

    def serve_forever(self) -> None:
        self.logger.info("Work progress API dang chay tai http://%s:%s", self.host, self.port)
        self._server.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self._server.shutdown()

    def _build_handler(self):
        service = self.service
        logger = self.logger

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    path = parsed.path.rstrip("/")
                    query = parse_qs(parsed.query)

                    if path == "/health":
                        self._write_json(HTTPStatus.OK, {"ok": True, "service": "work_progress"})
                        return

                    if path == "/review/pending":
                        limit = _to_int(query.get("limit", ["20"])[0], fallback=20)
                        rows = service.list_pending_updates(limit=limit)
                        self._write_json(HTTPStatus.OK, {"ok": True, "items": rows})
                        return

                    if path == "/events/pending-identity":
                        limit = _to_int(query.get("limit", ["20"])[0], fallback=20)
                        rows = service.list_pending_identity_events(limit=limit)
                        self._write_json(HTTPStatus.OK, {"ok": True, "items": rows})
                        return

                    if path == "/members":
                        limit = _to_int(query.get("limit", ["200"])[0], fallback=200)
                        rows = service.list_member_identities(limit=limit)
                        self._write_json(HTTPStatus.OK, {"ok": True, "items": rows})
                        return

                    if path.startswith("/reports/"):
                        report_type = path.split("/")[-1].strip().lower()
                        date_raw = str(query.get("date", [""])[0] or "").strip()
                        anchor_date = None
                        if date_raw:
                            try:
                                anchor_date = date.fromisoformat(date_raw)
                            except ValueError:
                                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "date phai la YYYY-MM-DD"})
                                return
                        payload = service.build_report(report_type, anchor_date=anchor_date)
                        self._write_json(HTTPStatus.OK, payload)
                        return

                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint khong ton tai."})
                except Exception as exc:  # noqa: BLE001
                    logger.exception("GET /work-progress API that bai")
                    self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

            def do_POST(self):  # noqa: N802
                try:
                    parsed = urlparse(self.path)
                    path = parsed.path.rstrip("/")
                    body = self._read_json_body()

                    ingest_routes = {
                        "/ingest/telegram": "telegram",
                        "/ingest/zalo": "zalo",
                        "/ingest/pancake-work": "pancake-work",
                        "/ingest/forwarded": "forwarded",
                    }
                    if path in ingest_routes:
                        platform = ingest_routes[path]
                        normalized_body = body
                        if platform == "zalo":
                            normalized_body = _normalize_zalo_payload(body)
                        elif platform == "pancake-work":
                            normalized_body = _normalize_pancake_work_payload(body)
                        payload = service.ingest_event(platform, normalized_body)
                        self._write_json(HTTPStatus.OK, payload)
                        return

                    if path == "/members/map":
                        payload = service.upsert_member_identity(
                            member_id=str(body.get("member_id", "")),
                            platform=str(body.get("platform", "")),
                            platform_user_id=str(body.get("platform_user_id", "")),
                            display_name=str(body.get("display_name", "")),
                        )
                        self._write_json(HTTPStatus.OK, {"ok": True, "item": payload})
                        return

                    if path.startswith("/review/"):
                        parts = [item for item in path.split("/") if item]
                        if len(parts) != 3:
                            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Review endpoint khong hop le."})
                            return
                        _root, update_id, action = parts
                        reviewer_id = str(body.get("reviewer_id", "")).strip()
                        note = str(body.get("note", "")).strip()
                        if action == "approve":
                            payload = service.approve_update(update_id=update_id, reviewer_id=reviewer_id, note=note)
                        elif action == "reject":
                            payload = service.reject_update(update_id=update_id, reviewer_id=reviewer_id, note=note)
                        elif action == "edit":
                            patch = body.get("patch", {})
                            if not isinstance(patch, dict):
                                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "patch phai la object."})
                                return
                            approve_after_edit = bool(body.get("approve_after_edit", True))
                            payload = service.edit_update(
                                update_id=update_id,
                                reviewer_id=reviewer_id,
                                patch=patch,
                                note=note,
                                approve_after_edit=approve_after_edit,
                            )
                        else:
                            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Action review khong ho tro."})
                            return
                        self._write_json(HTTPStatus.OK, {"ok": True, "item": payload})
                        return

                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint khong ton tai."})
                except ValueError as exc:
                    self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except KeyError as exc:
                    self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    logger.exception("POST /work-progress API that bai")
                    self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                logger.info("work-progress-api | " + str(format), *args)

            def _read_json_body(self) -> dict[str, Any]:
                raw_len = self.headers.get("Content-Length", "0")
                content_len = _to_int(raw_len, fallback=0)
                raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
                if not raw.strip():
                    return {}
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body phai la object.")
                return payload

            def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


def _to_int(value: Any, *, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _normalize_zalo_payload(raw_body: dict[str, Any]) -> dict[str, Any]:
    body = dict(raw_body or {})
    direct_text = _pick_first(body, ["message_text", "text", "content"])
    nested_text = _pick_first(
        body,
        [
            "message.text",
            "message.msg",
            "data.message.text",
            "data.message.msg",
            "data.content.text",
            "data.text",
            "payload.message.text",
        ],
    )
    message_text = str(direct_text or nested_text or "").strip()

    event_id = str(
        _pick_first(
            body,
            [
                "event_id",
                "message_id",
                "msg_id",
                "data.msg_id",
                "data.message.msg_id",
                "message.msg_id",
                "payload.message.msg_id",
            ],
        )
        or ""
    ).strip()
    sender_id = str(
        _pick_first(
            body,
            [
                "sender_id",
                "user_id",
                "from_id",
                "sender.id",
                "sender.uid",
                "from.id",
                "data.sender.id",
                "data.user_id",
                "data.from.id",
            ],
        )
        or ""
    ).strip()
    channel_id = str(
        _pick_first(
            body,
            [
                "channel_id",
                "conversation_id",
                "thread_id",
                "group_id",
                "oa_id",
                "recipient.id",
                "data.recipient.id",
                "data.oa_id",
                "data.group_id",
                "payload.thread_id",
            ],
        )
        or ""
    ).strip()
    event_time = _pick_first(
        body,
        [
            "event_time",
            "timestamp",
            "created_time",
            "message.timestamp",
            "data.timestamp",
            "data.message.timestamp",
            "payload.timestamp",
        ],
    )

    normalized: dict[str, Any] = {
        "event_id": event_id,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "message_text": message_text,
        "event_time": event_time if event_time is not None else "",
        "raw_payload": raw_body,
    }
    # Keep optional platform-specific fields for troubleshooting.
    event_name = str(_pick_first(body, ["event_name", "data.event_name", "payload.event_name"]) or "").strip()
    if event_name:
        normalized["event_name"] = event_name
    if not normalized["message_text"]:
        # Build fallback text from status-like events to keep traceability.
        action = str(_pick_first(body, ["action", "data.action", "event_name"]) or "").strip()
        task_name = str(
            _pick_first(body, ["task_name", "task.title", "data.task_name", "data.task.title", "data.message.title"])
            or ""
        ).strip()
        if action or task_name:
            normalized["message_text"] = f"task: {task_name or 'unknown'} {action}".strip()
    return normalized


def _normalize_pancake_work_payload(raw_body: dict[str, Any]) -> dict[str, Any]:
    body = dict(raw_body or {})
    message_text = str(
        _pick_first(
            body,
            [
                "message_text",
                "text",
                "content",
                "comment.text",
                "comment.body",
                "comment.content",
                "data.comment.text",
                "data.comment.body",
                "data.comment.content",
                "task.note",
                "task.description",
                "data.task.note",
                "data.task.description",
                "data.activity.message",
            ],
        )
        or ""
    ).strip()
    task_name = str(
        _pick_first(
            body,
            [
                "task_name",
                "task.title",
                "task.name",
                "data.task_name",
                "data.task.title",
                "data.task.name",
                "activity.task_name",
            ],
        )
        or ""
    ).strip()
    status = str(
        _pick_first(
            body,
            [
                "status",
                "task.status",
                "data.status",
                "data.task.status",
                "event.action",
                "data.event.action",
                "activity.action",
            ],
        )
        or ""
    ).strip()
    progress = str(
        _pick_first(
            body,
            [
                "progress",
                "task.progress",
                "task.progress_percent",
                "data.progress",
                "data.task.progress",
                "data.task.progress_percent",
                "activity.progress_percent",
            ],
        )
        or ""
    ).strip()
    task_code = str(
        _pick_first(
            body,
            [
                "task_code",
                "task.code",
                "task.id",
                "data.task_code",
                "data.task.code",
                "data.task.id",
                "activity.task_code",
            ],
        )
        or ""
    ).strip()
    if not message_text and (task_name or status or progress):
        tokens = ["task:"]
        if task_name:
            tokens.append(task_name)
        if task_code:
            tokens.append(f"#{task_code}")
        if status:
            tokens.append(status)
        if progress:
            tokens.append(f"{progress}%")
        message_text = " ".join(tokens).strip()

    event_time = _pick_first(
        body,
        ["event_time", "timestamp", "updated_at", "data.timestamp", "data.updated_at", "task.updated_at"],
    )
    normalized = {
        "event_id": str(
            _pick_first(
                body,
                [
                    "event_id",
                    "message_id",
                    "task_event_id",
                    "activity_id",
                    "id",
                    "data.event_id",
                    "data.activity_id",
                    "data.id",
                ],
            )
            or ""
        ).strip(),
        "channel_id": str(
            _pick_first(
                body,
                [
                    "channel_id",
                    "workspace_id",
                    "project_id",
                    "board_id",
                    "team_id",
                    "data.workspace_id",
                    "data.project_id",
                    "data.board_id",
                    "data.team_id",
                    "activity.workspace_id",
                    "activity.project_id",
                ],
            )
            or ""
        ).strip(),
        "sender_id": str(
            _pick_first(
                body,
                [
                    "sender_id",
                    "user_id",
                    "assignee_id",
                    "actor_id",
                    "actor.id",
                    "task.assignee_id",
                    "task.assignee.id",
                    "data.user_id",
                    "data.assignee_id",
                    "data.actor_id",
                    "data.actor.id",
                    "data.task.assignee_id",
                    "data.task.assignee.id",
                    "activity.user_id",
                    "activity.actor_id",
                ],
            )
            or ""
        ).strip(),
        "message_text": message_text,
        "event_time": event_time if event_time is not None else "",
        "raw_payload": raw_body,
    }
    return normalized


def _pick_first(payload: dict[str, Any], paths: list[str]) -> Any:
    for path in paths:
        value = _dig(payload, path)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _dig(payload: Any, dotted_path: str) -> Any:
    current = payload
    for token in str(dotted_path).split("."):
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
            continue
        return None
    return current
