from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from zoneinfo import ZoneInfo

from app.assistant_settings import AssistantSettings


class AssistantGoogleService:
    _CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
    _GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
    _SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, settings: AssistantSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._cached_access_token = ""
        self._token_expire_epoch = 0.0

    def is_configured(self) -> tuple[bool, str]:
        if not self.settings.google_oauth_client_id:
            return False, "Thiếu BOT3_GOOGLE_OAUTH_CLIENT_ID."
        if not self.settings.google_oauth_client_secret:
            return False, "Thiếu BOT3_GOOGLE_OAUTH_CLIENT_SECRET."
        if not self.settings.google_oauth_refresh_token:
            return False, "Thiếu BOT3_GOOGLE_OAUTH_REFRESH_TOKEN."
        return True, ""

    def fetch_agenda(self, target_date: date | None = None) -> dict[str, Any]:
        local_tz = self._resolve_timezone()
        day = target_date or datetime.now(local_tz).date()
        start_local = datetime.combine(day, time.min, tzinfo=local_tz)
        end_local = start_local + timedelta(days=1)
        events = self.fetch_events_between(start_local, end_local, max_per_calendar=50)
        return {
            "date": day.isoformat(),
            "timezone": self.settings.timezone_name,
            "events": events,
            "count": len(events),
        }

    def fetch_week_plan(self, anchor_date: date | None = None) -> dict[str, Any]:
        local_tz = self._resolve_timezone()
        day = anchor_date or datetime.now(local_tz).date()
        start_day = day - timedelta(days=day.weekday())
        end_day = start_day + timedelta(days=7)
        start_local = datetime.combine(start_day, time.min, tzinfo=local_tz)
        end_local = datetime.combine(end_day, time.min, tzinfo=local_tz)
        events = self.fetch_events_between(start_local, end_local, max_per_calendar=120)
        return {
            "week_start": start_day.isoformat(),
            "week_end": (end_day - timedelta(days=1)).isoformat(),
            "timezone": self.settings.timezone_name,
            "events": events,
            "count": len(events),
        }

    def fetch_events_between(
        self,
        start_local: datetime,
        end_local: datetime,
        *,
        max_per_calendar: int = 50,
    ) -> list[dict[str, Any]]:
        if end_local <= start_local:
            return []
        access_token = self._get_access_token()
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        all_events: list[dict[str, Any]] = []
        for calendar_id in self.settings.google_calendar_ids:
            safe_id = quote(calendar_id, safe="")
            url = f"{self._CALENDAR_API_BASE}/calendars/{safe_id}/events"
            payload = self._request_json(
                "GET",
                url,
                headers=headers,
                params={
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeMin": start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "timeMax": end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "maxResults": max(1, max_per_calendar),
                },
            )
            items = payload.get("items", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                all_events.append(self._normalize_event(item, calendar_id=calendar_id))
        all_events.sort(key=lambda event: str(event.get("start_iso", "")))
        return all_events

    def fetch_gmail_summary(self, *, query: str | None = None, max_items: int = 8) -> dict[str, Any]:
        access_token = self._get_access_token()
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        q = str(query or self.settings.gmail_query_default).strip() or self.settings.gmail_query_default

        list_payload = self._request_json(
            "GET",
            f"{self._GMAIL_API_BASE}/users/me/messages",
            headers=headers,
            params={"q": q, "maxResults": max(1, min(max_items, 25))},
        )
        message_refs = list_payload.get("messages", [])
        if not isinstance(message_refs, list):
            message_refs = []
        details: list[dict[str, Any]] = []
        for item in message_refs[: max(1, max_items)]:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id", "")).strip()
            if not message_id:
                continue
            payload = self._request_json(
                "GET",
                f"{self._GMAIL_API_BASE}/users/me/messages/{message_id}",
                headers=headers,
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            details.append(self._normalize_gmail_message(payload))

        return {
            "query": q,
            "count": len(details),
            "messages": details,
            "estimate_total": _to_int(list_payload.get("resultSizeEstimate"), fallback=len(details)),
        }

    def fetch_sheet_snapshot(self, *, max_rows: int = 30, max_cols: str = "J") -> dict[str, Any]:
        spreadsheet_id = str(self.settings.sheets_spreadsheet_id).strip()
        if not spreadsheet_id:
            return {"ok": False, "error": "Thiếu BOT3_SHEETS_SPREADSHEET_ID.", "rows": []}

        access_token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        sheet_title = self._resolve_sheet_title(spreadsheet_id, self.settings.sheets_gid, headers=headers)
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:{max_cols.upper()}{max(1, max_rows)}"
        encoded_range = quote(read_range, safe="")
        payload = self._request_json(
            "GET",
            f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            values = []
        return {
            "ok": True,
            "spreadsheet_id": spreadsheet_id,
            "gid": self.settings.sheets_gid,
            "sheet_title": sheet_title,
            "rows": values,
            "row_count": len(values),
        }

    def _get_access_token(self) -> str:
        now_epoch = datetime.now(timezone.utc).timestamp()
        if self._cached_access_token and now_epoch < (self._token_expire_epoch - 60):
            return self._cached_access_token

        response = requests.request(
            method="POST",
            url=self.settings.google_oauth_token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "client_id": self.settings.google_oauth_client_id,
                "client_secret": self.settings.google_oauth_client_secret,
                "refresh_token": self.settings.google_oauth_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Google OAuth token lỗi ({response.status_code}): {self._short_text(response.text)}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Google OAuth token trả JSON không hợp lệ: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Google OAuth token trả dữ liệu không hợp lệ.")
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise RuntimeError("Không lấy được access_token từ Google OAuth.")
        expires_in = _to_int(payload.get("expires_in"), fallback=3500)
        self._cached_access_token = token
        self._token_expire_epoch = now_epoch + max(300, expires_in)
        return token

    def _resolve_sheet_title(self, spreadsheet_id: str, gid: int, *, headers: dict[str, str]) -> str:
        payload = self._request_json(
            "GET",
            f"{self._SHEETS_API_BASE}/{spreadsheet_id}",
            headers=headers,
            params={"fields": "sheets(properties(sheetId,title))"},
        )
        sheets = payload.get("sheets", [])
        if not isinstance(sheets, list):
            raise RuntimeError("Google Sheets metadata không hợp lệ.")
        for item in sheets:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            if not isinstance(props, dict):
                continue
            if _to_int(props.get("sheetId"), fallback=-1) == gid:
                title = str(props.get("title", "")).strip()
                if title:
                    return title
        raise RuntimeError(f"Không tìm thấy tab Google Sheet có gid={gid}.")

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params or None,
            json=data if data is not None else None,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Google API lỗi ({response.status_code}): {self._short_text(response.text)}")
        text = str(response.text or "").strip()
        if not text:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Google API trả JSON không hợp lệ: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Google API trả dữ liệu không hợp lệ.")
        return payload

    def _normalize_event(self, payload: dict[str, Any], *, calendar_id: str) -> dict[str, Any]:
        event_id = str(payload.get("id", "")).strip()
        summary = str(payload.get("summary", "")).strip() or "(Không tiêu đề)"
        html_link = str(payload.get("htmlLink", "")).strip()
        start_payload = payload.get("start", {})
        end_payload = payload.get("end", {})
        start_iso, all_day = self._extract_event_time(start_payload)
        end_iso, _ = self._extract_event_time(end_payload)
        return {
            "event_id": event_id,
            "calendar_id": calendar_id,
            "summary": summary,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "all_day": all_day,
            "html_link": html_link,
            "location": str(payload.get("location", "")).strip(),
        }

    def _extract_event_time(self, payload: Any) -> tuple[str, bool]:
        if not isinstance(payload, dict):
            return "", False
        raw_dt = str(payload.get("dateTime", "")).strip()
        if raw_dt:
            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                local = dt.astimezone(self._resolve_timezone())
                return local.isoformat(), False
            except ValueError:
                return raw_dt, False
        raw_date = str(payload.get("date", "")).strip()
        if raw_date:
            return raw_date, True
        return "", False

    def _normalize_gmail_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = payload.get("payload", {}).get("headers", [])
        header_map: dict[str, str] = {}
        if isinstance(headers, list):
            for item in headers:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("name", "")).strip().lower()
                value = str(item.get("value", "")).strip()
                if key and value:
                    header_map[key] = value
        return {
            "id": str(payload.get("id", "")).strip(),
            "thread_id": str(payload.get("threadId", "")).strip(),
            "from": header_map.get("from", ""),
            "subject": header_map.get("subject", "(Không tiêu đề)"),
            "date": header_map.get("date", ""),
            "snippet": str(payload.get("snippet", "")).strip(),
            "internal_date": str(payload.get("internalDate", "")).strip(),
        }

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.timezone_name)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    @staticmethod
    def _escape_sheet_title(title: str) -> str:
        return str(title).replace("'", "''")

    @staticmethod
    def _short_text(raw: str, limit: int = 320) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."


def _to_int(value: Any, *, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
