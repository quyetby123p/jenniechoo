from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

import requests

from app.settings import Settings


class ReconcileCodSheetService:
    _SHEET_SCOPE = ("https://www.googleapis.com/auth/spreadsheets",)
    _SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def sync_report(self, report: dict[str, Any]) -> dict[str, Any]:
        spreadsheet_id = str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip()
        mode = self._resolve_mode()
        result: dict[str, Any] = {
            "enabled": bool(self.settings.reconcile_cod_sheet_enabled),
            "ok": True,
            "mode": mode,
            "spreadsheet_id": spreadsheet_id,
            "gid": int(self.settings.reconcile_cod_sheet_gid),
            "sheet_title": "",
            "attempted": 0,
            "inserted": 0,
            "skipped_existing": 0,
            "errors": [],
        }
        if not self.settings.reconcile_cod_sheet_enabled:
            return result
        try:
            return self._sync_report_impl(report, result=result)
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["errors"] = [str(exc)]
            self.logger.exception("Dong bo reconcile COD sang Google Sheet that bai")
            return result

    def is_configured(self) -> tuple[bool, str]:
        if not self.settings.reconcile_cod_sheet_enabled:
            return False, "RECONCILE_COD_SHEET_ENABLED=0"
        mode = self._resolve_mode()
        if mode == "apps_script":
            if not str(self.settings.reconcile_cod_sheet_webhook_url).strip():
                return False, "Thiếu RECONCILE_COD_SHEET_WEBHOOK_URL."
            return True, ""
        if mode == "oauth_user":
            if not str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip():
                return False, "Thiếu RECONCILE_COD_SHEET_SPREADSHEET_ID."
            if not str(self.settings.reconcile_cod_sheet_oauth_client_id).strip():
                return False, "Thiếu RECONCILE_COD_SHEET_OAUTH_CLIENT_ID."
            if not str(self.settings.reconcile_cod_sheet_oauth_client_secret).strip():
                return False, "Thiếu RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET."
            if not str(self.settings.reconcile_cod_sheet_oauth_refresh_token).strip():
                return False, "Thiếu RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN."
            return True, ""
        if not str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip():
            return False, "Thiếu RECONCILE_COD_SHEET_SPREADSHEET_ID."
        if not self.settings.reconcile_cod_sheet_credentials_file.exists():
            return False, f"Không tìm thấy file credentials: {self.settings.reconcile_cod_sheet_credentials_file}"
        return True, ""

    def _sync_report_impl(self, report: dict[str, Any], *, result: dict[str, Any]) -> dict[str, Any]:
        records = report.get("records", [])
        if not isinstance(records, list):
            records = []
        record_items = [item for item in records if isinstance(item, dict)]
        result["attempted"] = len(record_items)
        if not record_items:
            return result

        settlement_date = str(report.get("settlement_date", "")).strip()
        row_payloads = self._build_row_payloads(record_items, settlement_date=settlement_date)
        mode = self._resolve_mode()
        if mode == "oauth_user":
            return self._sync_via_oauth_user(row_payloads=row_payloads, result=result)
        if mode == "service_account":
            return self._sync_via_google_api(row_payloads=row_payloads, result=result)
        return self._sync_via_apps_script(report=report, row_payloads=row_payloads, result=result)

    def _sync_via_apps_script(
        self,
        *,
        report: dict[str, Any],
        row_payloads: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        webhook_url = str(self.settings.reconcile_cod_sheet_webhook_url).strip()
        if not webhook_url:
            raise ValueError("Thieu RECONCILE_COD_SHEET_WEBHOOK_URL.")

        payload = {
            "secret": str(self.settings.reconcile_cod_sheet_webhook_secret).strip(),
            "meta": {
                "source": "fb_ads_automation",
                "mode": "apps_script",
                "run_id": str(report.get("run_id", "")).strip(),
                "settlement_date": str(report.get("settlement_date", "")).strip(),
                "generated_at": str(report.get("generated_at", "")).strip(),
                "timezone": str(report.get("timezone", "")).strip(),
            },
            "sheet": {
                "spreadsheet_id": str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip(),
                "gid": int(self.settings.reconcile_cod_sheet_gid),
                "target_range": "B:AK",
                "dedupe_columns": {"awb_col": "O", "settlement_col": "Q", "start_row": 3},
            },
            "rows": self._sanitize_row_payloads_for_webhook(row_payloads),
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        secret = str(self.settings.reconcile_cod_sheet_webhook_secret).strip()
        if secret:
            headers["X-Reconcile-Secret"] = secret

        response_payload = self._request_webhook_json(
            webhook_url,
            headers=headers,
            data=payload,
            timeout_seconds=int(self.settings.reconcile_cod_sheet_webhook_timeout_seconds),
        )

        inserted = self._to_int(response_payload.get("inserted"), fallback=len(row_payloads))
        skipped = self._to_int(response_payload.get("skipped_existing"), fallback=max(0, len(row_payloads) - inserted))
        if inserted + skipped > len(row_payloads):
            skipped = max(0, len(row_payloads) - inserted)
        result["inserted"] = inserted
        result["skipped_existing"] = skipped
        result["sheet_title"] = str(response_payload.get("sheet_title", "")).strip()
        return result

    def _sanitize_row_payloads_for_webhook(self, row_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for item in row_payloads:
            if not isinstance(item, dict):
                continue
            clone = dict(item)
            raw_values = clone.get("values", [])
            if isinstance(raw_values, list):
                clone["values"] = [("" if value is None else value) for value in raw_values]
            sanitized.append(clone)
        return sanitized

    def _sync_via_google_api(self, *, row_payloads: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
        spreadsheet_id = str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip()
        if not spreadsheet_id:
            raise ValueError("Thieu RECONCILE_COD_SHEET_SPREADSHEET_ID.")

        credentials_file = self.settings.reconcile_cod_sheet_credentials_file
        if not credentials_file.exists():
            raise FileNotFoundError(f"Khong tim thay file credentials Google Sheet: {credentials_file}")

        headers = self._build_auth_headers(credentials_file)
        gid = int(self.settings.reconcile_cod_sheet_gid)
        sheet_title = self._resolve_sheet_title(spreadsheet_id, gid, headers=headers)
        result["sheet_title"] = sheet_title

        existing_keys = self._load_existing_keys(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        rows_to_append: list[list[Any]] = []
        skipped = 0
        for item in row_payloads:
            key = str(item.get("key", "")).strip()
            values = item.get("values", [])
            if key and key in existing_keys:
                skipped += 1
                continue
            if isinstance(values, list):
                rows_to_append.append(values)
            if key:
                existing_keys.add(key)

        if rows_to_append:
            start_row = self._find_next_blank_row_from_anchor_column(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                anchor_column="Q",
                start_row=3,
            )
            self._append_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=rows_to_append,
                start_row=start_row,
            )
        result["inserted"] = len(rows_to_append)
        result["skipped_existing"] = skipped
        return result

    def _sync_via_oauth_user(self, *, row_payloads: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
        spreadsheet_id = str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip()
        if not spreadsheet_id:
            raise ValueError("Thieu RECONCILE_COD_SHEET_SPREADSHEET_ID.")

        access_token = self._refresh_oauth_user_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        gid = int(self.settings.reconcile_cod_sheet_gid)
        sheet_title = self._resolve_sheet_title(spreadsheet_id, gid, headers=headers)
        result["sheet_title"] = sheet_title

        existing_keys = self._load_existing_keys(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        rows_to_append: list[list[Any]] = []
        skipped = 0
        for item in row_payloads:
            key = str(item.get("key", "")).strip()
            values = item.get("values", [])
            if key and key in existing_keys:
                skipped += 1
                continue
            if isinstance(values, list):
                rows_to_append.append(values)
            if key:
                existing_keys.add(key)

        if rows_to_append:
            start_row = self._find_next_blank_row_from_anchor_column(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                anchor_column="Q",
                start_row=3,
            )
            self._append_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=rows_to_append,
                start_row=start_row,
            )
        result["inserted"] = len(rows_to_append)
        result["skipped_existing"] = skipped
        return result

    def _build_row_payloads(self, record_items: list[dict[str, Any]], *, settlement_date: str) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for record in record_items:
            key = self._build_row_key(
                record.get("td_awb"),
                record.get("td_detail_settlement_date") or settlement_date,
            )
            payloads.append(
                {
                    "key": key,
                    "awb": str(record.get("td_awb", "")).strip(),
                    "settlement_date": self._format_date(record.get("td_detail_settlement_date") or settlement_date),
                    "values": self._build_row_values(record, settlement_date=settlement_date),
                }
            )
        return payloads

    def _request_webhook_json(
        self,
        webhook_url: str,
        *,
        headers: dict[str, str],
        data: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        response = requests.request(
            method="POST",
            url=webhook_url,
            headers=headers,
            json=data,
            timeout=max(5, timeout_seconds),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Apps Script webhook loi ({response.status_code}): {self._short_text(response.text)}"
            )
        text = str(response.text or "").strip()
        payload: dict[str, Any] = {}
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {"message": text}
        if payload.get("ok") is False:
            message = str(payload.get("error", payload.get("message", "Webhook bao loi khong ro."))).strip()
            raise RuntimeError(message or "Webhook bao loi khong ro.")
        return payload

    def _build_auth_headers(self, credentials_file: Path) -> dict[str, str]:
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2.service_account import Credentials
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Chua cai dat google-auth. Anh chay: pip install google-auth") from exc

        credentials = Credentials.from_service_account_file(
            str(credentials_file),
            scopes=self._SHEET_SCOPE,
        )
        credentials.refresh(GoogleRequest())
        token = str(getattr(credentials, "token", "")).strip()
        if not token:
            raise RuntimeError("Khong lay duoc access token Google Sheet tu service account.")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _refresh_oauth_user_access_token(self) -> str:
        token_uri = str(self.settings.reconcile_cod_sheet_oauth_token_uri).strip() or "https://oauth2.googleapis.com/token"
        client_id = str(self.settings.reconcile_cod_sheet_oauth_client_id).strip()
        client_secret = str(self.settings.reconcile_cod_sheet_oauth_client_secret).strip()
        refresh_token = str(self.settings.reconcile_cod_sheet_oauth_refresh_token).strip()
        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError(
                "Thieu OAuth credentials. Can RECONCILE_COD_SHEET_OAUTH_CLIENT_ID, "
                "RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET, RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN."
            )
        response = requests.request(
            method="POST",
            url=token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"OAuth token endpoint loi ({response.status_code}): {self._short_text(response.text)}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"OAuth token endpoint tra JSON khong hop le: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OAuth token endpoint tra du lieu khong hop le.")
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("Khong lay duoc access_token tu OAuth token endpoint.")
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
            raise RuntimeError("Du lieu Sheets API khong hop le: thieu danh sach sheets.")
        for item in sheets:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            if not isinstance(props, dict):
                continue
            sheet_id = self._to_int(props.get("sheetId"), fallback=-1)
            if sheet_id == gid:
                title = str(props.get("title", "")).strip()
                if title:
                    return title
        raise RuntimeError(f"Khong tim thay tab Google Sheet co gid={gid}.")

    def _load_existing_keys(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> set[str]:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!O3:Q"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return set()
        keys: set[str] = set()
        for row in values:
            if not isinstance(row, list):
                continue
            awb_raw = row[0] if len(row) > 0 else ""
            settlement_raw = row[2] if len(row) > 2 else ""
            key = self._build_row_key(awb_raw, settlement_raw)
            if key:
                keys.add(key)
        return keys

    def _append_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        rows: list[list[Any]],
        start_row: int,
    ) -> None:
        if not rows:
            return
        first_row = max(1, int(start_row))
        last_row = first_row + len(rows) - 1
        existing_rows = self._load_existing_rows_for_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=first_row,
            last_row=last_row,
        )
        merged_rows = self._merge_rows_with_existing(rows, existing_rows=existing_rows)
        # Do not touch column L because this column contains sheet formula managed directly on Google Sheet.
        rows_b_to_k = [row[:10] for row in merged_rows]
        rows_m_to_ak = [row[11:36] for row in merged_rows]
        self._write_rows_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=first_row,
            last_row=last_row,
            start_col="B",
            end_col="K",
            rows=rows_b_to_k,
        )
        self._write_rows_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=first_row,
            last_row=last_row,
            start_col="M",
            end_col="AK",
            rows=rows_m_to_ak,
        )

    def _write_rows_block(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        first_row: int,
        last_row: int,
        start_col: str,
        end_col: str,
        rows: list[list[Any]],
    ) -> None:
        if not rows:
            return
        write_range = f"'{self._escape_sheet_title(sheet_title)}'!{start_col}{first_row}:{end_col}{last_row}"
        encoded_range = quote(write_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        self._request_json(
            "PUT",
            url,
            headers=headers,
            params={
                "valueInputOption": "USER_ENTERED",
            },
            data={
                "majorDimension": "ROWS",
                "values": rows,
            },
        )

    def _load_existing_rows_for_block(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        first_row: int,
        last_row: int,
    ) -> list[list[Any]]:
        if last_row < first_row:
            return []
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!B{first_row}:AK{last_row}"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={
                "majorDimension": "ROWS",
                "valueRenderOption": "FORMULA",
                "dateTimeRenderOption": "FORMATTED_STRING",
            },
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return []
        return [row for row in values if isinstance(row, list)]

    def _merge_rows_with_existing(self, rows: list[list[Any]], *, existing_rows: list[list[Any]]) -> list[list[Any]]:
        merged_rows: list[list[Any]] = []
        width = 36
        for idx, row in enumerate(rows):
            current = row if isinstance(row, list) else []
            existing = existing_rows[idx] if idx < len(existing_rows) else []
            merged: list[Any] = []
            for col in range(width):
                new_value = current[col] if col < len(current) else None
                if new_value is None:
                    existing_value = existing[col] if col < len(existing) else ""
                    merged.append(existing_value)
                else:
                    merged.append(new_value)
            merged_rows.append(merged)
        return merged_rows

    def _find_next_blank_row_from_anchor_column(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        anchor_column: str,
        start_row: int,
    ) -> int:
        anchor = str(anchor_column or "").strip().upper() or "H"
        safe_start = max(1, int(start_row))
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!{anchor}{safe_start}:{anchor}"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return safe_start

        last_non_empty_row = safe_start - 1
        for offset, row in enumerate(values):
            row_number = safe_start + offset
            if not isinstance(row, list) or not row:
                continue
            cell_value = str(row[0]).strip()
            if cell_value:
                last_non_empty_row = row_number
        return last_non_empty_row + 1

    def _build_row_values(self, record: dict[str, Any], *, settlement_date: str) -> list[Any]:
        pancake_display_id = str(record.get("pancake_display_id", "")).strip()
        pancake_order_id = str(record.get("pancake_order_id", "")).strip()
        pos_order_code = pancake_display_id or pancake_order_id
        col_b = "DA-TL.JE" if pos_order_code.upper().startswith("JC") else ""
        td_awb = str(record.get("td_awb", "")).strip()
        send_date = self._format_date(record.get("td_send_date"))
        detail_settlement = self._format_date(record.get("td_detail_settlement_date") or settlement_date)
        status_vi = self._humanize_status_vi(record.get("td_status"))
        sheet_cod_minor = record.get("td_sheet_cod_minor")
        if sheet_cod_minor is None or str(sheet_cod_minor).strip() == "":
            sheet_cod_minor = record.get("td_cod_minor")
        cod_value = self._minor_to_major(sheet_cod_minor)
        fee_value = self._minor_to_major(record.get("td_fee_minor"))
        delivery_fee = self._to_sheet_number(record.get("td_delivery_fee"), default=fee_value)
        remote_fee = self._to_sheet_number(record.get("td_remote_fee"))
        refund_fee = self._to_sheet_number(record.get("td_refund_fee"))
        cod_fee = self._to_sheet_number(record.get("td_cod_fee"))
        insurance_fee = self._to_sheet_number(record.get("td_insurance_fee"))
        account_fee = self._to_sheet_number(record.get("td_account_fee"))
        hard_goods_fee = self._to_sheet_number(record.get("td_hard_goods_fee"))
        ffm_fee = self._to_sheet_number(record.get("td_ffm_fee"))
        confirm_trend_order_fee = self._to_sheet_number(record.get("td_confirm_trend_order_fee"))
        confirm_hard_order_fee = self._to_sheet_number(record.get("td_confirm_hard_order_fee"))
        mess_fee = self._to_sheet_number(record.get("td_mess_fee"))
        mess_care_fee = self._to_sheet_number(record.get("td_mess_care_fee"))
        telesale_care_fee = self._to_sheet_number(record.get("td_telesale_care_fee"))
        fulfillment_other_fee = self._to_sheet_number(record.get("td_fulfillment_other_fee"))
        ship_discount_fee = self._to_sheet_number(record.get("td_ship_discount_fee"))
        delivery_total = self._to_sheet_number(record.get("td_delivery_total"))
        service_other_total = self._to_sheet_number(record.get("td_service_other_total"))

        cols_b_to_ak: list[Any] = [None] * 36
        cols_b_to_ak[0] = col_b  # B
        cols_b_to_ak[2] = pos_order_code  # D (Mã đơn POS Pancake)
        cols_b_to_ak[13] = td_awb  # O
        cols_b_to_ak[14] = send_date  # P
        cols_b_to_ak[15] = detail_settlement  # Q
        cols_b_to_ak[16] = status_vi  # R
        cols_b_to_ak[17] = cod_value  # S
        cols_b_to_ak[18] = delivery_fee  # T
        cols_b_to_ak[19] = remote_fee  # U
        cols_b_to_ak[20] = refund_fee  # V
        cols_b_to_ak[21] = cod_fee  # W
        cols_b_to_ak[22] = insurance_fee  # X
        cols_b_to_ak[23] = account_fee  # Y
        cols_b_to_ak[24] = hard_goods_fee  # Z
        cols_b_to_ak[25] = ffm_fee  # AA
        cols_b_to_ak[26] = confirm_trend_order_fee  # AB
        cols_b_to_ak[27] = confirm_hard_order_fee  # AC
        cols_b_to_ak[28] = mess_fee  # AD
        cols_b_to_ak[29] = mess_care_fee  # AE
        cols_b_to_ak[30] = telesale_care_fee  # AF
        cols_b_to_ak[31] = fulfillment_other_fee  # AG
        cols_b_to_ak[32] = ship_discount_fee  # AH
        cols_b_to_ak[33] = delivery_total  # AI
        cols_b_to_ak[34] = service_other_total  # AJ
        cols_b_to_ak[35] = self._to_sheet_number(
            (self._to_optional_float(record.get("td_fulfillment_other_fee")) or 0.0)
            + (self._to_optional_float(record.get("td_ffm_fee")) or 0.0)
        )  # AK
        return cols_b_to_ak

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
            json=data if data else None,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Google Sheets API loi ({response.status_code}): {self._short_text(response.text)}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Google Sheets API tra JSON khong hop le: {self._short_text(response.text)}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Google Sheets API tra du lieu khong hop le.")
        return payload

    def _resolve_mode(self) -> str:
        mode = str(self.settings.reconcile_cod_sheet_mode).strip().lower()
        if mode in {"oauth_user", "oauth"}:
            return "oauth_user"
        if mode in {"service_account", "google_api"}:
            return "service_account"
        return "apps_script"

    @staticmethod
    def _escape_sheet_title(title: str) -> str:
        return str(title).replace("'", "''")

    @staticmethod
    def _short_text(raw: str, limit: int = 360) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    @staticmethod
    def _to_int(value: Any, *, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _build_row_key(self, awb_raw: Any, settlement_raw: Any) -> str:
        awb = self._normalize_awb(awb_raw)
        settlement = self._normalize_date_key(settlement_raw)
        if not awb or not settlement:
            return ""
        return f"{awb}|{settlement}"

    @staticmethod
    def _normalize_awb(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            return ""
        return re.sub(r"[^A-Z0-9]", "", raw)

    def _normalize_date_key(self, value: Any) -> str:
        parsed = self._parse_date(value)
        if not parsed:
            return ""
        return parsed.strftime("%Y-%m-%d")

    def _format_date(self, value: Any) -> str:
        parsed = self._parse_date(value)
        if not parsed:
            return str(value or "").strip()
        return parsed.strftime("%Y-%m-%d")

    @staticmethod
    def _parse_date(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if "T" in text and len(text) >= 10:
            text = text[:10]
        patterns = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y")
        for pattern in patterns:
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
        return None

    @staticmethod
    def _minor_to_major(value: Any) -> int:
        try:
            amount = int(value)
        except (TypeError, ValueError):
            return 0
        return int(round(amount / 100))

    @staticmethod
    def _to_optional_float(value: Any) -> float | None:
        try:
            if value is None or str(value).strip() == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _to_sheet_number(cls, value: Any, *, default: Any = 0) -> int | float:
        numeric = cls._to_optional_float(value)
        if numeric is None:
            numeric = cls._to_optional_float(default)
        if numeric is None:
            numeric = 0.0
        if abs(numeric - round(numeric)) < 1e-9:
            return int(round(numeric))
        return round(float(numeric), 2)

    @staticmethod
    def _humanize_status_vi(raw: Any) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        normalized = text.strip().upper()
        if normalized == "SUCCESS":
            return "Giao hàng thành công"
        if normalized == "RETURNED":
            return "Hoàn hàng thành công"
        if normalized == "RETURNING":
            return "Đang hoàn hàng"
        return text
