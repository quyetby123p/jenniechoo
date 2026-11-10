from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any
import unicodedata
from urllib.parse import quote

import requests

from app.settings import Settings


class ReconcileCodSheetService:
    _SHEET_SCOPE = ("https://www.googleapis.com/auth/spreadsheets",)
    _SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
    _PRODUCT_HEADERS = [
        "Nhãn",
        "Mã đơn POS",
        "Mã sản phẩm",
        "Màu",
        "Size",
        "Số lượng",
        "Giá vốn",
        "Ngày đối soát",
        "Trạng thái",
    ]
    _PRODUCT_PRICE_HEADER = "Giá bán"

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
            "updated_existing": 0,
            "errors": [],
            "product_sync": self._empty_product_sync_result(mode=mode),
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
        if getattr(self.settings, "reconcile_cod_product_sheet_enabled", True) and not self._product_sheet_name():
            return False, "Thiếu RECONCILE_COD_PRODUCT_SHEET_NAME."
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
            result = self._sync_via_oauth_user(row_payloads=row_payloads, result=result)
        elif mode == "service_account":
            result = self._sync_via_google_api(row_payloads=row_payloads, result=result)
        else:
            result = self._sync_via_apps_script(report=report, row_payloads=row_payloads, result=result)
            # Older Apps Script deployments write the financial tab but ignore the
            # product payload. Fall back to the direct API only when that happens.
            product_sync = result.get("product_sync")
            if self._product_sheet_enabled() and isinstance(product_sync, dict) and not product_sync.get("ok"):
                fallback_sync = self._sync_product_sheet_via_google_api(report=report, mode="oauth_user")
                if fallback_sync.get("ok"):
                    result["product_sync"] = fallback_sync
                else:
                    errors = result.setdefault("errors", [])
                    if isinstance(errors, list):
                        errors.extend(
                            f"Tab sản phẩm: {item}"
                            for item in fallback_sync.get("errors", [])
                            if str(item).strip()
                        )
                    result["ok"] = False
            return result

        if self._product_sheet_enabled():
            product_sync = self._sync_product_sheet_via_google_api(report=report, mode=mode)
            result["product_sync"] = product_sync
            if not product_sync.get("ok"):
                result["ok"] = False
                errors = result.setdefault("errors", [])
                if isinstance(errors, list):
                    errors.extend(
                        f"Tab sản phẩm: {item}"
                        for item in product_sync.get("errors", [])
                        if str(item).strip()
                    )
        return result

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

        product_row_payloads = self._build_product_row_payloads(report)
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
                "dedupe_columns": {"awb_col": "O", "settlement_col": "Q", "start_row": 4},
            },
            "rows": self._sanitize_row_payloads_for_webhook(row_payloads),
            "product_sheet": {
                "enabled": self._product_sheet_enabled(),
                "title": self._product_sheet_name(),
                "header_row": 1,
                "start_row": 2,
                "target_range": "A:M",
                "price_column": "M",
                "dedupe_columns": ["A", "B", "C", "D", "E", "H"],
            },
            "product_rows": self._sanitize_product_row_payloads_for_webhook(product_row_payloads),
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
        result["product_sync"] = self._parse_apps_script_product_sync(
            response_payload,
            attempted=len(product_row_payloads),
        )
        return result

    def _sanitize_row_payloads_for_webhook(self, row_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for item in row_payloads:
            if not isinstance(item, dict):
                continue
            clone = dict(item)
            raw_values = clone.get("values", [])
            if isinstance(raw_values, list):
                # Keep null as "do not touch" for the Apps Script adapter.
                clone["values"] = list(raw_values)
            sanitized.append(clone)
        return sanitized

    def _sanitize_product_row_payloads_for_webhook(
        self,
        row_payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for item in row_payloads:
            if not isinstance(item, dict):
                continue
            clone = dict(item)
            values = clone.get("values", [])
            if isinstance(values, list):
                clone["values"] = list(values)
            sanitized.append(clone)
        return sanitized

    def _parse_apps_script_product_sync(self, payload: dict[str, Any], *, attempted: int) -> dict[str, Any]:
        raw = payload.get("product_sync")
        if isinstance(raw, dict):
            result = self._empty_product_sync_result(mode="apps_script")
            result.update(raw)
            result["attempted"] = self._to_int(raw.get("attempted"), fallback=attempted)
            result["inserted"] = self._to_int(raw.get("inserted"), fallback=0)
            result["skipped_existing"] = self._to_int(raw.get("skipped_existing"), fallback=0)
            result["updated_existing"] = self._to_int(raw.get("updated_existing"), fallback=0)
            errors = raw.get("errors", [])
            result["errors"] = errors if isinstance(errors, list) else [str(errors)]
            return result
        if attempted <= 0:
            return self._empty_product_sync_result(mode="apps_script")
        return {
            **self._empty_product_sync_result(mode="apps_script"),
            "ok": False,
            "attempted": attempted,
            "errors": ["Apps Script chưa trả kết quả ghi tab Chi tiet SP COD."],
        }

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

        existing_index = self._load_existing_row_index(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        rows_to_append: list[list[Any]] = []
        repair_updates: list[dict[str, Any]] = []
        repaired_rows: set[int] = set()
        skipped = 0
        for item in row_payloads:
            key = str(item.get("key", "")).strip()
            values = item.get("values", [])
            existing = existing_index.get(key) if key else None
            if existing:
                skipped += 1
                updates = self._build_existing_row_repairs(item, existing=existing)
                if updates:
                    repair_updates.extend(updates)
                    row_number = self._to_int(existing.get("row"), fallback=0)
                    if row_number > 0:
                        repaired_rows.add(row_number)
                continue
            if isinstance(values, list):
                rows_to_append.append(values)
            if key:
                existing_index[key] = {"row": 0, "label": "", "pos": ""}

        if rows_to_append:
            start_row = self._find_next_blank_row_from_anchor_column(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                anchor_column="Q",
                start_row=4,
            )
            self._append_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=rows_to_append,
                start_row=start_row,
            )
        if repair_updates:
            self._write_cell_updates(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                updates=repair_updates,
            )
        result["inserted"] = len(rows_to_append)
        result["skipped_existing"] = skipped
        result["updated_existing"] = len(repaired_rows)
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

        existing_index = self._load_existing_row_index(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        rows_to_append: list[list[Any]] = []
        repair_updates: list[dict[str, Any]] = []
        repaired_rows: set[int] = set()
        skipped = 0
        for item in row_payloads:
            key = str(item.get("key", "")).strip()
            values = item.get("values", [])
            existing = existing_index.get(key) if key else None
            if existing:
                skipped += 1
                updates = self._build_existing_row_repairs(item, existing=existing)
                if updates:
                    repair_updates.extend(updates)
                    row_number = self._to_int(existing.get("row"), fallback=0)
                    if row_number > 0:
                        repaired_rows.add(row_number)
                continue
            if isinstance(values, list):
                rows_to_append.append(values)
            if key:
                existing_index[key] = {"row": 0, "label": "", "pos": ""}

        if rows_to_append:
            start_row = self._find_next_blank_row_from_anchor_column(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                anchor_column="Q",
                start_row=4,
            )
            self._append_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=rows_to_append,
                start_row=start_row,
            )
        if repair_updates:
            self._write_cell_updates(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                updates=repair_updates,
            )
        result["inserted"] = len(rows_to_append)
        result["skipped_existing"] = skipped
        result["updated_existing"] = len(repaired_rows)
        return result

    def _sync_product_sheet_via_google_api(
        self,
        *,
        report: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        product_row_payloads = self._build_product_row_payloads(report)
        result = self._empty_product_sync_result(mode=mode)
        result["attempted"] = len(product_row_payloads)
        if not self._product_sheet_enabled():
            return result

        spreadsheet_id = str(self.settings.reconcile_cod_sheet_spreadsheet_id).strip()
        if not spreadsheet_id:
            result["ok"] = False
            result["errors"] = ["Thiếu RECONCILE_COD_SHEET_SPREADSHEET_ID."]
            return result

        try:
            if mode == "service_account":
                headers = self._build_auth_headers(self.settings.reconcile_cod_sheet_credentials_file)
            else:
                access_token = self._refresh_oauth_user_access_token()
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            sheet_title, sheet_id, created = self._resolve_or_create_product_sheet(
                spreadsheet_id,
                self._product_sheet_name(),
                headers=headers,
            )
            result["sheet_title"] = sheet_title
            result["sheet_id"] = sheet_id
            result["created"] = created
            self._ensure_product_sheet_header(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
            )
            self._ensure_product_return_status_rule(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                sheet_id=sheet_id,
                headers=headers,
            )
            if not product_row_payloads:
                self._merge_and_center_product_order_groups(
                    spreadsheet_id=spreadsheet_id,
                    sheet_title=sheet_title,
                    sheet_id=sheet_id,
                    headers=headers,
                )
                return result

            existing_index = self._load_existing_product_row_index(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
            )
            rows_to_append: list[list[Any]] = []
            repair_updates: list[dict[str, Any]] = []
            repaired_rows: set[int] = set()
            skipped = 0
            for item in product_row_payloads:
                key = str(item.get("key", "")).strip()
                existing = existing_index.get(key) if key else None
                if existing:
                    skipped += 1
                    updates = self._build_existing_product_row_repairs(item, existing=existing)
                    if updates:
                        repair_updates.extend(updates)
                        row_number = self._to_int(existing.get("row"), fallback=0)
                        if row_number > 0:
                            repaired_rows.add(row_number)
                    continue
                values = item.get("values", [])
                if isinstance(values, list):
                    rows_to_append.append(self._build_product_sheet_row(item))
                if key:
                    existing_index[key] = {"row": 0, "values": []}

            if rows_to_append:
                start_row = self._find_first_blank_row_by_anchor_column(
                    spreadsheet_id=spreadsheet_id,
                    sheet_title=sheet_title,
                    headers=headers,
                    anchor_column="C",
                    start_row=2,
                )
                self._write_product_rows(
                    spreadsheet_id=spreadsheet_id,
                    sheet_title=sheet_title,
                    headers=headers,
                    rows=rows_to_append,
                    start_row=start_row,
                )
            if repair_updates:
                self._write_product_cell_updates(
                    spreadsheet_id=spreadsheet_id,
                    sheet_title=sheet_title,
                    headers=headers,
                    updates=repair_updates,
                )
            self._merge_and_center_product_order_groups(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                sheet_id=sheet_id,
                headers=headers,
            )
            result["inserted"] = len(rows_to_append)
            result["skipped_existing"] = skipped
            result["updated_existing"] = len(repaired_rows)
            return result
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["errors"] = [str(exc)]
            self.logger.exception("Ghi tab chi tiet san pham COD that bai")
            return result

    def _merge_and_center_product_order_groups(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        sheet_id: int,
        headers: dict[str, str],
    ) -> None:
        if int(sheet_id) <= 0:
            return
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A2:I"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return

        groups: list[tuple[int, int]] = []
        current_identity = ""
        current_start = 0
        current_end = 0
        context_label = ""
        context_pos = ""
        context_settlement = ""
        for offset, row in enumerate(values):
            row_number = 2 + offset
            if not isinstance(row, list):
                current_identity = ""
                current_start = 0
                current_end = 0
                context_label = ""
                context_pos = ""
                context_settlement = ""
                continue
            sku = str(row[2] if len(row) > 2 else "").strip()
            if not sku:
                if current_start and current_end - current_start + 1 > 1:
                    groups.append((current_start, current_end))
                current_identity = ""
                current_start = 0
                current_end = 0
                context_label = ""
                context_pos = ""
                context_settlement = ""
                continue

            label = str(row[0] if len(row) > 0 else "").strip() or context_label
            pos_order_code = str(row[1] if len(row) > 1 else "").strip() or context_pos
            settlement_date = str(row[7] if len(row) > 7 else "").strip() or context_settlement
            identity = "|".join(
                (
                    self._normalize_product_key(label),
                    self._normalize_product_key(pos_order_code),
                    self._normalize_product_key(settlement_date),
                )
            )
            if not pos_order_code:
                if current_start and current_end - current_start + 1 > 1:
                    groups.append((current_start, current_end))
                current_identity = ""
                current_start = 0
                current_end = 0
                continue
            if identity != current_identity:
                if current_start and current_end - current_start + 1 > 1:
                    groups.append((current_start, current_end))
                current_identity = identity
                current_start = row_number
            current_end = row_number
            context_label = label
            context_pos = pos_order_code
            context_settlement = settlement_date
        if current_start and current_end - current_start + 1 > 1:
            groups.append((current_start, current_end))

        if not groups:
            return
        requests: list[dict[str, Any]] = []
        for start_row, end_row in groups:
            for column_index in (0, 1, 7):
                cell_range = {
                    "sheetId": int(sheet_id),
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": column_index,
                    "endColumnIndex": column_index + 1,
                }
                requests.extend(
                    [
                        {"unmergeCells": {"range": cell_range}},
                        {"mergeCells": {"range": cell_range, "mergeType": "MERGE_ALL"}},
                        {
                            "repeatCell": {
                                "range": cell_range,
                                "cell": {
                                    "userEnteredFormat": {
                                        "horizontalAlignment": "LEFT",
                                        "verticalAlignment": "MIDDLE",
                                    }
                                },
                                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)",
                            }
                        },
                    ]
                )
        self._request_json(
            "POST",
            f"{self._SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={"requests": requests},
        )

    def _ensure_product_return_status_rule(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        sheet_id: int,
        headers: dict[str, str],
    ) -> None:
        if int(sheet_id) <= 0:
            return
        metadata = self._request_json(
            "GET",
            f"{self._SHEETS_API_BASE}/{spreadsheet_id}",
            headers=headers,
            params={"fields": "sheets(properties(sheetId,title),conditionalFormats)"},
        )
        target_sheet: dict[str, Any] | None = None
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {}) if isinstance(sheet, dict) else {}
            if self._to_int(properties.get("sheetId"), fallback=0) == int(sheet_id):
                target_sheet = sheet
                break
        if target_sheet is None:
            raise RuntimeError(f"Khong tim thay tab san pham: {sheet_title}")

        for rule in target_sheet.get("conditionalFormats", []):
            if self._is_product_return_status_rule(rule, sheet_id=int(sheet_id)):
                return

        status_range = {
            "sheetId": int(sheet_id),
            "startRowIndex": 1,
            "startColumnIndex": 8,
            "endColumnIndex": 9,
        }
        self._request_json(
            "POST",
            f"{self._SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={
                "requests": [
                    {
                        "addConditionalFormatRule": {
                            "rule": {
                                "ranges": [status_range],
                                "booleanRule": {
                                    "condition": {
                                        "type": "TEXT_CONTAINS",
                                        "values": [{"userEnteredValue": "Hoàn"}],
                                    },
                                    "format": {
                                        "backgroundColor": {
                                            "red": 0.988235,
                                            "green": 0.894118,
                                            "blue": 0.839216,
                                        }
                                    },
                                },
                            },
                            "index": 0,
                        }
                    }
                ]
            },
        )

    @staticmethod
    def _is_product_return_status_rule(rule: Any, *, sheet_id: int) -> bool:
        if not isinstance(rule, dict):
            return False
        boolean_rule = rule.get("booleanRule", {})
        condition = boolean_rule.get("condition", {}) if isinstance(boolean_rule, dict) else {}
        if condition.get("type") != "TEXT_CONTAINS":
            return False
        values = condition.get("values", [])
        if not any(
            str(item.get("userEnteredValue", "")).strip().casefold() == "hoàn"
            for item in values
            if isinstance(item, dict)
        ):
            return False
        for item in rule.get("ranges", []):
            if not isinstance(item, dict):
                continue
            if (
                ReconcileCodSheetService._to_int(item.get("sheetId"), fallback=0) == int(sheet_id)
                and ReconcileCodSheetService._to_int(item.get("startRowIndex"), fallback=-1) == 1
                and ReconcileCodSheetService._to_int(item.get("startColumnIndex"), fallback=-1) == 8
                and ReconcileCodSheetService._to_int(item.get("endColumnIndex"), fallback=-1) == 9
            ):
                return True
        return False

    def _resolve_or_create_product_sheet(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        *,
        headers: dict[str, str],
    ) -> tuple[str, int, bool]:
        title = str(sheet_title or "").strip()
        if not title:
            raise ValueError("Thiếu tên tab chi tiết sản phẩm COD.")
        metadata = self._request_spreadsheet_metadata(spreadsheet_id, headers=headers)
        existing = self._find_sheet_by_title(metadata, title)
        if existing:
            return title, self._to_int(existing.get("sheetId"), fallback=0), False

        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate"
        payload = self._request_json(
            "POST",
            url,
            headers=headers,
            data={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        )
        replies = payload.get("replies", [])
        if isinstance(replies, list) and replies:
            reply = replies[0]
            if isinstance(reply, dict):
                add_sheet = reply.get("addSheet", {})
                properties = add_sheet.get("properties", {}) if isinstance(add_sheet, dict) else {}
                if isinstance(properties, dict):
                    sheet_id = self._to_int(properties.get("sheetId"), fallback=0)
                    created_title = str(properties.get("title", title)).strip() or title
                    return created_title, sheet_id, True
        metadata = self._request_spreadsheet_metadata(spreadsheet_id, headers=headers)
        existing = self._find_sheet_by_title(metadata, title)
        if existing:
            return title, self._to_int(existing.get("sheetId"), fallback=0), False
        raise RuntimeError(f"Không xác định được tab mới sau khi tạo: {title}")

    def _request_spreadsheet_metadata(
        self,
        spreadsheet_id: str,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"fields": "sheets(properties(sheetId,title))"},
        )
        if not isinstance(payload.get("sheets"), list):
            raise RuntimeError("Google Sheets API thiếu danh sách tab.")
        return payload

    @staticmethod
    def _find_sheet_by_title(metadata: dict[str, Any], title: str) -> dict[str, Any] | None:
        for sheet in metadata.get("sheets", []):
            if not isinstance(sheet, dict):
                continue
            properties = sheet.get("properties", {})
            if not isinstance(properties, dict):
                continue
            if str(properties.get("title", "")).strip() == str(title).strip():
                return properties
        return None

    def _ensure_product_sheet_header(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> None:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:M1"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMULA"},
        )
        values = payload.get("values", [])
        current = values[0] if isinstance(values, list) and values and isinstance(values[0], list) else []
        if not current:
            self._write_product_header(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
            )
            return
        normalized = [str(value or "").strip() for value in current[: len(self._PRODUCT_HEADERS)]]
        if normalized != self._PRODUCT_HEADERS:
            raise RuntimeError(
                f"Tab {sheet_title} có tiêu đề không đúng; không ghi để tránh lệch cột."
            )
        current_price_header = str(current[12] if len(current) > 12 else "").strip()
        if current_price_header and current_price_header != self._PRODUCT_PRICE_HEADER:
            raise RuntimeError(
                f"Tab {sheet_title} có tiêu đề cột M không đúng; không ghi để tránh lệch cột."
            )
        if current_price_header != self._PRODUCT_PRICE_HEADER:
            self._write_product_price_header(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
            )

    def _write_product_header(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> None:
        write_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:I1"
        encoded_range = quote(write_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        self._request_json(
            "PUT",
            url,
            headers=headers,
            params={"valueInputOption": "RAW"},
            data={"majorDimension": "ROWS", "values": [self._PRODUCT_HEADERS]},
        )
        self._write_product_price_header(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )

    def _write_product_price_header(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> None:
        write_range = f"'{self._escape_sheet_title(sheet_title)}'!M1"
        encoded_range = quote(write_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        self._request_json(
            "PUT",
            url,
            headers=headers,
            params={"valueInputOption": "RAW"},
            data={"majorDimension": "ROWS", "values": [[self._PRODUCT_PRICE_HEADER]]},
        )

    def _load_existing_product_row_index(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A2:M"
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
            return {}
        index: dict[str, dict[str, Any]] = {}
        context_label = ""
        context_pos = ""
        context_settlement = ""
        for offset, row in enumerate(values):
            if not isinstance(row, list):
                continue
            sku = str(row[2] if len(row) > 2 else "").strip()
            if not sku:
                context_label = ""
                context_pos = ""
                context_settlement = ""
                continue
            label = str(row[0] if len(row) > 0 else "").strip() or context_label
            pos_order_code = str(row[1] if len(row) > 1 else "").strip() or context_pos
            settlement_date = str(row[7] if len(row) > 7 else "").strip() or context_settlement
            context_label = label
            context_pos = pos_order_code
            context_settlement = settlement_date
            key = self._build_product_key(
                label,
                pos_order_code,
                sku,
                row[3] if len(row) > 3 else "",
                row[4] if len(row) > 4 else "",
                settlement_date,
            )
            if key and key not in index:
                index[key] = {"row": 2 + offset, "values": row}
        return index

    def _find_first_blank_row_by_anchor_column(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        anchor_column: str,
        start_row: int,
    ) -> int:
        anchor = str(anchor_column or "A").strip().upper()
        safe_start = max(1, int(start_row))
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!{anchor}{safe_start}:{anchor}"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMULA"},
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return safe_start
        for offset, row in enumerate(values):
            first = row[0] if isinstance(row, list) and row else ""
            if not str(first or "").strip():
                return safe_start + offset
        return safe_start + len(values)

    def _write_product_rows(
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
        last_row = int(start_row) + len(rows) - 1
        self._write_rows_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=int(start_row),
            last_row=last_row,
            start_col="A",
            end_col="F",
            rows=[row[:6] for row in rows],
        )
        self._write_rows_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=int(start_row),
            last_row=last_row,
            start_col="H",
            end_col="I",
            rows=[row[7:9] for row in rows],
        )
        self._write_rows_block(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=int(start_row),
            last_row=last_row,
            start_col="M",
            end_col="M",
            rows=[[row[12] if len(row) > 12 else ""] for row in rows],
        )

    def _write_product_cell_updates(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        updates: list[dict[str, Any]],
    ) -> None:
        if not updates:
            return
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate"
        data_items: list[dict[str, Any]] = []
        allowed_columns = {"A", "B", "C", "D", "E", "F", "H", "I", "M"}
        for item in updates:
            row = self._to_int(item.get("row"), fallback=0)
            column = str(item.get("column", "")).strip().upper()
            if row < 2 or column not in allowed_columns:
                continue
            data_items.append(
                {
                    "range": f"'{self._escape_sheet_title(sheet_title)}'!{column}{row}",
                    "majorDimension": "ROWS",
                    "values": [[item.get("value", "")]],
                }
            )
        if data_items:
            self._request_json(
                "POST",
                url,
                headers=headers,
                params={"valueInputOption": "USER_ENTERED"},
                data={"data": data_items, "includeValuesInResponse": False},
            )

    def _build_existing_product_row_repairs(
        self,
        item: dict[str, Any],
        *,
        existing: dict[str, Any],
    ) -> list[dict[str, Any]]:
        values = item.get("values", [])
        current = existing.get("values", [])
        if not isinstance(values, list) or not isinstance(current, list):
            return []
        row_number = self._to_int(existing.get("row"), fallback=0)
        if row_number < 2:
            return []
        updates: list[dict[str, Any]] = []
        columns = ((0, "A"), (1, "B"), (2, "C"), (3, "D"), (4, "E"), (5, "F"), (7, "H"), (8, "I"))
        for index, column in columns:
            expected = values[index] if index < len(values) else ""
            current_value = current[index] if index < len(current) else ""
            if not str(expected or "").strip():
                if column in {"A", "B", "H"} and str(current_value or "").strip():
                    updates.append({"row": row_number, "column": column, "value": ""})
                continue
            if str(expected or "").strip() != str(current_value or "").strip():
                updates.append({"row": row_number, "column": column, "value": expected})
        expected_sale_price = item.get("sale_price")
        if expected_sale_price is not None:
            current_sale_price = current[12] if len(current) > 12 else ""
            if str(expected_sale_price).strip() != str(current_sale_price or "").strip():
                updates.append({"row": row_number, "column": "M", "value": expected_sale_price})
        return updates

    def _build_product_sheet_row(self, item: dict[str, Any]) -> list[Any]:
        values = item.get("values", [])
        row = list(values[:9]) if isinstance(values, list) else []
        row.extend([""] * (13 - len(row)))
        sale_price = item.get("sale_price")
        row[12] = sale_price if sale_price is not None else ""
        return row

    def _build_product_row_payloads(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        settlement_date = str(report.get("settlement_date", "")).strip()
        by_key: dict[str, dict[str, Any]] = {}
        records = report.get("records", [])
        if not isinstance(records, list):
            return []
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("match_result", "")).strip() not in {"matched_unique", "already_correct"}:
                continue
            product_lines = record.get("product_lines", [])
            if not isinstance(product_lines, list):
                continue
            pancake_display_id = str(record.get("pancake_display_id", "")).strip()
            pancake_order_id = str(record.get("pancake_order_id", "")).strip()
            pos_order_code = pancake_display_id or pancake_order_id
            label = self._resolve_sheet_label(record, pos_order_code)
            detail_settlement = self._format_date(
                record.get("td_detail_settlement_date") or settlement_date
            )
            status_vi = self._humanize_status_vi(record.get("td_status"))
            wrote_order_header = False
            for product_line in product_lines:
                if not isinstance(product_line, dict):
                    continue
                sku = str(product_line.get("sku", "")).strip()
                color = str(product_line.get("color", "")).strip()
                size = str(product_line.get("size", "")).strip()
                quantity = self._to_int(product_line.get("quantity"), fallback=0)
                if not sku or quantity <= 0 or not pos_order_code:
                    continue
                values: list[Any] = [
                    label if not wrote_order_header else "",
                    pos_order_code if not wrote_order_header else "",
                    sku,
                    color,
                    size,
                    quantity,
                    None,
                    detail_settlement if not wrote_order_header else "",
                    status_vi,
                ]
                key = self._build_product_key(
                    label,
                    pos_order_code,
                    sku,
                    color,
                    size,
                    detail_settlement,
                )
                if not key:
                    continue
                current = by_key.get(key)
                if current is None:
                    item_payload: dict[str, Any] = {"key": key, "values": values}
                    sale_price = product_line.get("sale_price")
                    if sale_price is not None:
                        item_payload["sale_price"] = sale_price
                    by_key[key] = item_payload
                else:
                    current_values = current.get("values", [])
                    if isinstance(current_values, list) and len(current_values) > 5:
                        current_values[5] = self._to_int(current_values[5]) + quantity
                    sale_price = product_line.get("sale_price")
                    incoming_price = self._to_optional_float(sale_price)
                    if incoming_price is not None:
                        existing_price = self._to_optional_float(current.get("sale_price"))
                        if existing_price is None:
                            current["sale_price"] = incoming_price
                        elif abs(existing_price - incoming_price) > 1e-9:
                            current["sale_price"] = None
                wrote_order_header = True
        return list(by_key.values())

    def _build_product_key_from_values(self, values: list[Any]) -> str:
        if not isinstance(values, list):
            return ""
        return self._build_product_key(
            values[0] if len(values) > 0 else "",
            values[1] if len(values) > 1 else "",
            values[2] if len(values) > 2 else "",
            values[3] if len(values) > 3 else "",
            values[4] if len(values) > 4 else "",
            values[7] if len(values) > 7 else "",
        )

    def _build_product_key(
        self,
        label: Any,
        pos_order_code: Any,
        sku: Any,
        color: Any,
        size: Any,
        settlement_date: Any,
    ) -> str:
        parts = [
            self._normalize_product_key(label),
            self._normalize_product_key(pos_order_code),
            self._normalize_product_key(sku),
            self._normalize_product_key(color),
            self._normalize_product_key(size),
            self._normalize_product_key(settlement_date),
        ]
        if not parts[1] or not parts[2]:
            return ""
        return "|".join(parts)

    @staticmethod
    def _normalize_product_key(value: Any) -> str:
        normalized = unicodedata.normalize("NFKD", str(value or "").lower())
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        return re.sub(r"[^a-z0-9]+", "", normalized)

    def _product_sheet_enabled(self) -> bool:
        return bool(
            self.settings.reconcile_cod_sheet_enabled
            and getattr(self.settings, "reconcile_cod_product_sheet_enabled", True)
        )

    def _product_sheet_name(self) -> str:
        return (
            str(getattr(self.settings, "reconcile_cod_product_sheet_name", "Chi tiet SP COD")).strip()
            or "Chi tiet SP COD"
        )

    def _empty_product_sync_result(self, *, mode: str) -> dict[str, Any]:
        return {
            "enabled": self._product_sheet_enabled(),
            "ok": True,
            "mode": mode,
            "sheet_title": self._product_sheet_name(),
            "sheet_id": 0,
            "created": False,
            "attempted": 0,
            "inserted": 0,
            "skipped_existing": 0,
            "updated_existing": 0,
            "errors": [],
        }

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
        return set(
            self._load_existing_row_index(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
            ).keys()
        )

    def _load_existing_row_index(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!B4:Q"
        encoded_range = quote(read_range, safe="")
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}"
        payload = self._request_json(
            "GET",
            url,
            headers=headers,
            params={
                "majorDimension": "ROWS",
                "valueRenderOption": "FORMATTED_VALUE",
                "dateTimeRenderOption": "FORMATTED_STRING",
            },
        )
        values = payload.get("values", [])
        if not isinstance(values, list):
            return {}
        index: dict[str, dict[str, Any]] = {}
        for offset, row in enumerate(values):
            if not isinstance(row, list):
                continue
            awb_raw = row[13] if len(row) > 13 else ""
            settlement_raw = row[15] if len(row) > 15 else ""
            key = self._build_row_key(awb_raw, settlement_raw)
            if not key or key in index:
                continue
            index[key] = {
                "row": 4 + offset,
                "label": row[0] if len(row) > 0 else "",
                "pos": row[2] if len(row) > 2 else "",
            }
        return index

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

    def _write_cell_updates(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        updates: list[dict[str, Any]],
    ) -> None:
        if not updates:
            return
        url = f"{self._SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate"
        data_items: list[dict[str, Any]] = []
        for item in updates:
            row = self._to_int(item.get("row"), fallback=0)
            column = str(item.get("column", "")).strip().upper()
            if row <= 0 or column not in {"B", "D"}:
                continue
            data_items.append(
                {
                    "range": f"'{self._escape_sheet_title(sheet_title)}'!{column}{row}",
                    "majorDimension": "ROWS",
                    "values": [[item.get("value", "")]],
                }
            )
        if not data_items:
            return
        self._request_json(
            "POST",
            url,
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            data={"data": data_items, "includeValuesInResponse": False},
        )

    def _build_existing_row_repairs(
        self,
        item: dict[str, Any],
        *,
        existing: dict[str, Any],
    ) -> list[dict[str, Any]]:
        values = item.get("values", [])
        if not isinstance(values, list):
            return []
        row_number = self._to_int(existing.get("row"), fallback=0)
        if row_number <= 0:
            return []

        updates: list[dict[str, Any]] = []
        expected_label = values[0] if len(values) > 0 else ""
        expected_pos = values[2] if len(values) > 2 else ""
        current_label = existing.get("label", "")
        current_pos = existing.get("pos", "")

        if self._should_repair_cell(expected_label, current_label):
            updates.append({"row": row_number, "column": "B", "value": expected_label})
        if self._should_repair_cell(expected_pos, current_pos):
            updates.append({"row": row_number, "column": "D", "value": expected_pos})
        return updates

    @staticmethod
    def _should_repair_cell(expected: Any, current: Any) -> bool:
        expected_text = str(expected or "").strip()
        if not expected_text:
            return False
        return str(current or "").strip() != expected_text

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

        last_occupied_row = safe_start - 1
        for offset, row in enumerate(values):
            row_number = safe_start + offset
            if isinstance(row, list) and row and str(row[0] or "").strip():
                last_occupied_row = row_number
        return last_occupied_row + 1

    def _build_row_values(self, record: dict[str, Any], *, settlement_date: str) -> list[Any]:
        pancake_display_id = str(record.get("pancake_display_id", "")).strip()
        pancake_order_id = str(record.get("pancake_order_id", "")).strip()
        pos_order_code = pancake_display_id or pancake_order_id
        col_b = self._resolve_sheet_label(record, pos_order_code)
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

    @staticmethod
    def _resolve_sheet_label(record: dict[str, Any], pos_order_code: str) -> str:
        explicit = str(record.get("pancake_sheet_label", "")).strip()
        if explicit:
            return explicit

        profile = str(record.get("pancake_profile", "")).strip().lower()
        if profile in {"ads2", "vayxa", "vx"}:
            return "DA-TL.VX"

        normalized_code = str(pos_order_code or "").strip().upper()
        if normalized_code.startswith("JC"):
            return "DA-TL.JE"
        if normalized_code.startswith(("VX", "VXV")):
            return "DA-TL.VX"
        return ""

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
        folded = unicodedata.normalize("NFD", text)
        normalized = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
        normalized = normalized.lower().replace("đ", "d")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized in {"success", "giao hang thanh cong", "giao thanh cong", "da giao hang"}:
            return "Giao hàng thành công"
        if normalized in {"returned", "da hoan hang", "hoan hang thanh cong", "da tra hang"}:
            return "Hoàn hàng thành công"
        if normalized in {"returning", "dang hoan hang", "dang tra hang"}:
            return "Đang hoàn hàng"
        return text

