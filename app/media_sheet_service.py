from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import requests

from app.media_constants import MEDIA_SHEET_HEADERS
from app.media_settings import MediaSettings


class MediaSheetService:
    _SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, settings: MediaSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def is_configured(self) -> tuple[bool, str]:
        if not self.settings.sheet_enabled:
            return False, "MEDIA_RESEARCH_SHEET_ENABLED=0"
        if self._resolve_mode() != "oauth_user":
            return False, "Hiện tại chỉ hỗ trợ MEDIA_RESEARCH_SHEET_MODE=oauth_user."
        if not str(self.settings.sheet_spreadsheet_id).strip():
            return False, "Thiếu MEDIA_RESEARCH_SHEET_SPREADSHEET_ID."
        if not str(self.settings.sheet_oauth_client_id).strip():
            return False, "Thiếu MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID."
        if not str(self.settings.sheet_oauth_client_secret).strip():
            return False, "Thiếu MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET."
        if not str(self.settings.sheet_oauth_refresh_token).strip():
            return False, "Thiếu MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN."
        return True, ""

    def sync_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": bool(self.settings.sheet_enabled),
            "ok": True,
            "mode": self._resolve_mode(),
            "spreadsheet_id": str(self.settings.sheet_spreadsheet_id).strip(),
            "gid": int(self.settings.sheet_gid),
            "sheet_title": "",
            "attempted": len(rows),
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [],
        }
        if not self.settings.sheet_enabled:
            return result

        ok, reason = self.is_configured()
        if not ok:
            result["ok"] = False
            result["errors"] = [reason]
            return result

        try:
            sync_result = self._sync_rows_impl(rows)
            result.update(sync_result)
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Ghi media research vao Google Sheet that bai")
            result["ok"] = False
            result["errors"] = [str(exc)]
            return result

    def _sync_rows_impl(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        access_token = self._refresh_oauth_user_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        spreadsheet_id = str(self.settings.sheet_spreadsheet_id).strip()
        sheet_title = self._resolve_sheet_title(spreadsheet_id, int(self.settings.sheet_gid), headers=headers)

        self._ensure_header(spreadsheet_id=spreadsheet_id, sheet_title=sheet_title, headers=headers)

        existing_map = self._load_existing_dedupe_map(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )

        unique_rows: dict[str, dict[str, Any]] = {}
        skipped = 0
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            key = str(row.get("dedupe_key", "")).strip()
            if not key:
                skipped += 1
                continue
            if key in unique_rows:
                skipped += 1
                continue
            unique_rows[key] = row

        updates: list[tuple[int, list[Any]]] = []
        appends: list[list[Any]] = []

        for dedupe_key, row in unique_rows.items():
            values = self._row_to_values(row)
            row_number = existing_map.get(dedupe_key)
            if row_number:
                updates.append((row_number, values))
            else:
                appends.append(values)

        if updates:
            self._batch_update_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                updates=updates,
            )
        if appends:
            self._append_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=appends,
            )

        return {
            "ok": True,
            "sheet_title": sheet_title,
            "inserted": len(appends),
            "updated": len(updates),
            "skipped": skipped,
        }

    def _refresh_oauth_user_access_token(self) -> str:
        response = requests.request(
            method="POST",
            url=self.settings.sheet_oauth_token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "client_id": self.settings.sheet_oauth_client_id,
                "client_secret": self.settings.sheet_oauth_client_secret,
                "refresh_token": self.settings.sheet_oauth_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OAuth token endpoint lỗi ({response.status_code}): {self._short_text(response.text)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OAuth token endpoint trả dữ liệu không hợp lệ.")
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("Không lấy được access_token từ OAuth token endpoint.")
        return access_token

    def _resolve_sheet_title(self, spreadsheet_id: str, gid: int, *, headers: dict[str, str]) -> str:
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"fields": "sheets(properties(sheetId,title))"},
        )
        sheets = payload.get("sheets", [])
        if not isinstance(sheets, list):
            raise RuntimeError("Google Sheets API không trả danh sách sheets.")
        for item in sheets:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            if not isinstance(props, dict):
                continue
            if self._to_int(props.get("sheetId"), fallback=-1) == gid:
                title = str(props.get("title", "")).strip()
                if title:
                    return title
        raise RuntimeError(f"Không tìm thấy tab Google Sheet có gid={gid}.")

    def _ensure_header(self, *, spreadsheet_id: str, sheet_title: str, headers: dict[str, str]) -> None:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:Q1"
        payload = self._request_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=read_range,
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        first_row = values[0] if isinstance(values, list) and values else []
        has_header = isinstance(first_row, list) and any(str(cell).strip() for cell in first_row)
        if has_header:
            return

        write_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:Q1"
        self._request_values_update(
            spreadsheet_id=spreadsheet_id,
            write_range=write_range,
            headers=headers,
            values=[list(MEDIA_SHEET_HEADERS)],
        )

    def _load_existing_dedupe_map(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> dict[str, int]:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A2:Q"
        payload = self._request_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=read_range,
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return {}
        result: dict[str, int] = {}
        for index, row in enumerate(values):
            if not isinstance(row, list):
                continue
            dedupe_key = str(row[16] if len(row) > 16 else "").strip()
            if not dedupe_key:
                continue
            result[dedupe_key] = index + 2
        return result

    def _batch_update_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        updates: list[tuple[int, list[Any]]],
    ) -> None:
        data = []
        for row_number, values in updates:
            data.append(
                {
                    "range": f"'{self._escape_sheet_title(sheet_title)}'!A{row_number}:Q{row_number}",
                    "majorDimension": "ROWS",
                    "values": [values],
                }
            )
        if not data:
            return
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate"
        self._request_json(
            "POST",
            url,
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            data={"data": data},
        )

    def _append_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        rows: list[list[Any]],
    ) -> None:
        if not rows:
            return
        write_range = f"'{self._escape_sheet_title(sheet_title)}'!A:Q"
        encoded_range = quote(write_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}:append"
        self._request_json(
            "POST",
            url,
            headers=headers,
            params={
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            data={
                "majorDimension": "ROWS",
                "values": rows,
            },
        )

    def _request_values_get(
        self,
        *,
        spreadsheet_id: str,
        read_range: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        return self._request_json("GET", url, headers=headers, params=params)

    def _request_values_update(
        self,
        *,
        spreadsheet_id: str,
        write_range: str,
        headers: dict[str, str],
        values: list[list[Any]],
    ) -> dict[str, Any]:
        encoded_range = quote(write_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        return self._request_json(
            "PUT",
            url,
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            data={"majorDimension": "ROWS", "values": values},
        )

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
            raise RuntimeError(f"Google Sheets API lỗi ({response.status_code}): {self._short_text(response.text)}")
        try:
            payload = response.json() if str(response.text or "").strip() else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Google Sheets API trả JSON không hợp lệ: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Google Sheets API trả dữ liệu không hợp lệ.")
        return payload

    @staticmethod
    def _row_to_values(row: dict[str, Any]) -> list[Any]:
        values: list[Any] = []
        for key in MEDIA_SHEET_HEADERS:
            value = row.get(key, "")
            if value is None:
                values.append("")
            else:
                values.append(value)
        return values

    def _resolve_mode(self) -> str:
        mode = str(self.settings.sheet_mode).strip().lower()
        if mode in {"oauth_user", "oauth"}:
            return "oauth_user"
        return mode or "unknown"

    @staticmethod
    def _escape_sheet_title(title: str) -> str:
        return str(title).replace("'", "''")

    @staticmethod
    def _to_int(value: Any, *, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _short_text(raw: str, limit: int = 360) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
