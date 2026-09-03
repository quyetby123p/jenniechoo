from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import logging
import re
from typing import Any

from pypdf import PdfReader

from app.assistant_google_service import AssistantGoogleService
from app.assistant_settings import AssistantSettings
from app.assistant_storage_service import AssistantStorageService
from app.utils import load_json, now_utc_iso


INPUT_SHEET_NAME = "Tiền trừ thẻ"
INVOICE_SHEET_NAME = "FB Hóa đơn"
DETAIL_SHEET_NAME = "FB Chi tiết"
SUMMARY_SHEET_NAME = "FB Tổng hợp"
EXCEPTION_SHEET_NAME = "FB Ngoại lệ"
LEGACY_LABEL_HEADER = "FB_label"

PAYMENT_HEADERS = [
    "payment_id",
    "transaction_date",
    "posting_date",
    "amount",
    "currency",
    "bank_fee",
    "fx_rate",
    "description",
    "account_hint",
    "reference",
    "note",
]
INVOICE_HEADERS = [
    "run_id",
    "source_file",
    "source_sha256",
    "account",
    "ad_account_id",
    "invoice_id",
    "invoice_date",
    "currency",
    "invoice_total",
    "parse_status",
    "warning",
]
DETAIL_HEADERS = [
    "row_key",
    "run_id",
    "account",
    "invoice_id",
    "invoice_date",
    "invoice_currency",
    "invoice_total",
    "payment_id",
    "payment_date",
    "payment_currency",
    "payment_amount",
    "bank_fee",
    "fx_rate",
    "effective_fx_rate",
    "converted_invoice_amount",
    "residual",
    "status",
    "match_reason",
    "confidence",
]
SUMMARY_HEADERS = [
    "row_key",
    "run_id",
    "period_start",
    "period_end",
    "account",
    "invoice_count",
    "matched_count",
    "invoice_total_by_currency",
    "card_total_by_currency",
    "known_bank_fee",
    "residual_card_currency",
    "status",
]
EXCEPTION_HEADERS = [
    "row_key",
    "run_id",
    "type",
    "account",
    "reference",
    "message",
    "source_file",
]


@dataclass(frozen=True)
class ParsedInvoice:
    source_file: str
    source_sha256: str
    account: str
    ad_account_id: str
    invoice_id: str
    invoice_date: str
    currency: str
    total: Decimal | None
    warning: str
    text_excerpt: str


@dataclass(frozen=True)
class PaymentRow:
    payment_id: str
    transaction_date: date | None
    posting_date: date | None
    amount: Decimal
    currency: str
    bank_fee: Decimal
    fx_rate: Decimal | None
    description: str
    account_hint: str
    reference: str
    note: str
    source_row: int

    @property
    def effective_date(self) -> date | None:
        return self.posting_date or self.transaction_date


class FbPaymentReconcileService:
    """Read Facebook billing PDFs, match each invoice to one card payment, and write an audit trail."""

    def __init__(
        self,
        *,
        settings: AssistantSettings,
        google: AssistantGoogleService,
        storage: AssistantStorageService,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.google = google
        self.storage = storage
        self.logger = logger

    def enabled(self) -> bool:
        return bool(self.settings.fb_reconcile_enabled)

    def new_draft(self, *, user_id: int, chat_id: int, period_text: str = "") -> dict[str, Any]:
        start, end = parse_period(period_text)
        return {
            "mode": "fb_reconcile",
            "status": "collecting",
            "user_id": int(user_id),
            "chat_id": int(chat_id),
            "period_start": start.isoformat() if start else "",
            "period_end": end.isoformat() if end else "",
            "files": [],
            "invoices": [],
            "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(),
        }

    def ingest_pdf(
        self,
        *,
        draft: dict[str, Any],
        file_name: str,
        content: bytes,
        account_hint: str = "",
    ) -> tuple[dict[str, Any], ParsedInvoice]:
        digest = hashlib.sha256(content).hexdigest()
        existing = draft.get("files", []) if isinstance(draft.get("files"), list) else []
        duplicate_index = next(
            (index for index, item in enumerate(existing) if isinstance(item, dict) and str(item.get("sha256", "")) == digest),
            None,
        )
        if duplicate_index is not None and not normalize_account(account_hint):
            raise ValueError("PDF này đã được nhận trước đó, bot không tạo bản ghi trùng. Nếu cần gán tài khoản, gửi lại kèm caption JC hoặc Vayxa.")

        parsed = parse_invoice_pdf(
            content,
            file_name=file_name,
            source_sha256=digest,
            account_hint=account_hint,
            jc_ad_account_id=self.settings.fb_reconcile_jc_ad_account_id,
            vayxa_ad_account_id=self.settings.fb_reconcile_vayxa_ad_account_id,
        )
        invoices = [item for item in draft.get("invoices", []) if isinstance(item, dict)]
        if duplicate_index is not None:
            replacement = invoice_to_dict(parsed)
            for index, item in enumerate(invoices):
                if str(item.get("source_sha256", "")) == digest:
                    invoices[index] = replacement
                    break
            files = list(existing)
            files[duplicate_index] = {
                **existing[duplicate_index],
                "account": parsed.account,
                "invoice_id": parsed.invoice_id,
            }
        else:
            files = list(existing)
            files.append(
                {
                    "file_name": file_name,
                    "sha256": digest,
                    "size_bytes": len(content),
                    "account": parsed.account,
                    "invoice_id": parsed.invoice_id,
                    "received_at": now_utc_iso(),
                }
            )
            invoices.append(invoice_to_dict(parsed))
        updated = dict(draft)
        updated["files"] = files
        updated["invoices"] = invoices
        updated["status"] = "ready" if len(files) >= 2 else "collecting"
        updated["updated_at"] = now_utc_iso()
        if not updated.get("period_start") or not updated.get("period_end"):
            inferred_start, inferred_end = infer_period_from_invoices(invoices)
            if inferred_start and inferred_end:
                updated["period_start"] = inferred_start.isoformat()
                updated["period_end"] = inferred_end.isoformat()
        return updated, parsed

    def precheck(self, draft: dict[str, Any]) -> dict[str, Any]:
        invoices = [item for item in draft.get("invoices", []) if isinstance(item, dict)]
        period_start = _parse_iso_date(draft.get("period_start"))
        period_end = _parse_iso_date(draft.get("period_end"))
        if not period_start or not period_end:
            period_start, period_end = infer_period_from_invoices(invoices)
        payments, payment_errors = self.read_payments(
            period_start=period_start,
            period_end=period_end,
        )
        invoice_errors = [
            str(item.get("warning", "")).strip()
            for item in invoices
            if str(item.get("warning", "")).strip()
        ]
        return {
            "invoice_count": len(invoices),
            "file_count": len(draft.get("files", [])) if isinstance(draft.get("files"), list) else 0,
            "payment_count": len(payments),
            "payment_errors": payment_errors,
            "invoice_errors": invoice_errors,
            "period_start": period_start.isoformat() if period_start else "",
            "period_end": period_end.isoformat() if period_end else "",
            "invoice_totals": self._totals_by_account(invoices),
            "card_totals": totals_by_currency(payments),
            "ready": len(invoices) >= 2 and len(draft.get("files", [])) >= 2 and not payment_errors and not invoice_errors,
        }

    def run(self, draft: dict[str, Any]) -> dict[str, Any]:
        if len(draft.get("files", [])) < 2 or len(draft.get("invoices", [])) < 2:
            raise ValueError("Cần đủ 2 file PDF hóa đơn trước khi chạy đối soát.")
        invoices = [item for item in draft.get("invoices", []) if isinstance(item, dict)]
        parse_errors = [str(item.get("warning", "")).strip() for item in invoices if str(item.get("warning", "")).strip()]
        if parse_errors:
            raise ValueError("PDF cần kiểm tra trước khi chạy: " + "; ".join(parse_errors[:4]))
        period_start = _parse_iso_date(draft.get("period_start"))
        period_end = _parse_iso_date(draft.get("period_end"))
        if not period_start or not period_end:
            period_start, period_end = infer_period_from_invoices(invoices)
        payments, payment_errors = self.read_payments(period_start=period_start, period_end=period_end)
        if payment_errors:
            raise ValueError("Dữ liệu tab Tiền trừ thẻ chưa hợp lệ: " + "; ".join(payment_errors[:5]))
        run_id = f"fb_reconcile_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{hashlib.sha1(str(draft).encode()).hexdigest()[:8]}"
        result = self._build_result(
            run_id=run_id,
            invoices=invoices,
            payments=payments,
            period_start=period_start,
            period_end=period_end,
        )
        self.storage.save_run_payload(result)
        return result

    def write_run(self, run_id: str) -> dict[str, Any]:
        path = self.settings.run_logs_dir / f"{str(run_id).strip()}.json"
        payload = load_json(path)
        if not isinstance(payload, dict):
            raise ValueError(f"Không tìm thấy run đối soát: {run_id}")
        self.ensure_sheet_layout()
        written = {}
        for sheet_name, headers, rows in (
            (INVOICE_SHEET_NAME, INVOICE_HEADERS, payload.get("invoice_rows", [])),
            (DETAIL_SHEET_NAME, DETAIL_HEADERS, payload.get("detail_rows", [])),
            (SUMMARY_SHEET_NAME, SUMMARY_HEADERS, payload.get("summary_rows", [])),
            (EXCEPTION_SHEET_NAME, EXCEPTION_HEADERS, payload.get("exception_rows", [])),
        ):
            normalized_rows = [row for row in rows if isinstance(row, list)]
            existing = self.google.fetch_sheet_values(
                spreadsheet_id=self.settings.fb_reconcile_sheet_id,
                sheet_name=sheet_name,
                cell_range="A1:A10000",
            )
            existing_keys = {
                str(row[0]).strip()
                for row in (existing.get("values", []) if isinstance(existing.get("values"), list) else [])
                if isinstance(row, list) and row and str(row[0]).strip()
            }
            if normalized_rows:
                normalized_rows = [row for row in normalized_rows if not row or str(row[0]).strip() not in existing_keys]
            if normalized_rows:
                written[sheet_name] = self.google.append_sheet_values(
                    spreadsheet_id=self.settings.fb_reconcile_sheet_id,
                    sheet_name=sheet_name,
                    values=normalized_rows,
                )
            else:
                written[sheet_name] = {"ok": True, "updated_rows": 0}
        written[INPUT_SHEET_NAME] = self._write_payment_labels(payload)
        payload["written_at"] = now_utc_iso()
        payload["sheet_write"] = written
        self.storage.save_run_payload(payload)
        return {"ok": True, "run_id": run_id, "written": written}

    def _write_payment_labels(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail_rows = payload.get("detail_rows", []) if isinstance(payload.get("detail_rows"), list) else []
        labels_by_payment: dict[str, set[str]] = {}
        for row in detail_rows:
            if not isinstance(row, list) or len(row) < 18:
                continue
            payment_id = str(row[7] or "").strip()
            account = normalize_account(row[2])
            status = str(row[16] or "").strip()
            if payment_id and account and status in {"KHỚP", "KHỚP CÓ LỆCH PHÍ/TỶ GIÁ"}:
                labels_by_payment.setdefault(payment_id, set()).add(account)
        if not labels_by_payment:
            return {"ok": True, "updated_rows": 0, "labels": {}}

        response = self.google.fetch_sheet_values(
            spreadsheet_id=self.settings.fb_reconcile_sheet_id,
            sheet_name=INPUT_SHEET_NAME,
            cell_range="A1:L1000",
        )
        values = response.get("values", []) if isinstance(response.get("values"), list) else []
        if not values:
            return {"ok": False, "updated_rows": 0, "error": "Không đọc được tab Tiền trừ thẻ để ghi nhãn."}
        headers = [_normalize_header(value) for value in values[0]]
        header_index = {name: idx for idx, name in enumerate(headers) if name}
        canonical = "payment_id" in header_index and "account_hint" in header_index
        label_column = header_index.get("account_hint") if canonical else 11
        note_column = header_index.get("note") if canonical else None
        updates = 0
        labels_written: dict[str, str] = {}
        if not canonical and len(values[0]) <= label_column:
            self.google.update_sheet_values(
                spreadsheet_id=self.settings.fb_reconcile_sheet_id,
                sheet_name=INPUT_SHEET_NAME,
                cell_range="L1",
                values=[[LEGACY_LABEL_HEADER]],
            )
        row_lookup: dict[str, tuple[int, list[Any]]] = {}
        data_start = 1
        for offset, row in enumerate(values[data_start:], start=data_start + 1):
            if not isinstance(row, list):
                continue
            payment_id = _cell(row, header_index.get("payment_id")) if canonical else f"legacy_row_{offset}"
            if payment_id:
                row_lookup[payment_id] = (offset, row)
        for payment_id, accounts in labels_by_payment.items():
            located = row_lookup.get(payment_id)
            if not located:
                continue
            row_number, row = located
            label = " + ".join(account for account in ("JC", "VAYXA") if account in accounts)
            if not label:
                continue
            labels_written[payment_id] = label
            existing = _cell(row, label_column)
            if existing != label:
                self.google.update_sheet_values(
                    spreadsheet_id=self.settings.fb_reconcile_sheet_id,
                    sheet_name=INPUT_SHEET_NAME,
                    cell_range=f"{_column_letter(label_column + 1)}{row_number}",
                    values=[[label]],
                )
                updates += 1
            if note_column is not None:
                note = _cell(row, note_column)
                marker = f"FB_label={label}"
                if marker not in note:
                    new_note = f"{note}; {marker}".strip("; ")
                    self.google.update_sheet_values(
                        spreadsheet_id=self.settings.fb_reconcile_sheet_id,
                        sheet_name=INPUT_SHEET_NAME,
                        cell_range=f"{_column_letter(note_column + 1)}{row_number}",
                        values=[[new_note]],
                    )
        return {"ok": True, "updated_rows": updates, "labels": labels_written}

    def ensure_sheet_layout(self) -> dict[str, Any]:
        spreadsheet_id = self.settings.fb_reconcile_sheet_id
        if not spreadsheet_id:
            raise ValueError("Thiếu FB_RECONCILE_SHEET_ID.")
        metadata = self.google.fetch_spreadsheet_metadata(spreadsheet_id=spreadsheet_id)
        sheets = metadata.get("sheets", []) if isinstance(metadata.get("sheets"), list) else []
        titles = {
            str(item.get("properties", {}).get("title", "")).strip()
            for item in sheets
            if isinstance(item, dict) and isinstance(item.get("properties"), dict)
        }
        required = [INPUT_SHEET_NAME, INVOICE_SHEET_NAME, DETAIL_SHEET_NAME, SUMMARY_SHEET_NAME, EXCEPTION_SHEET_NAME]
        missing = [title for title in required if title not in titles]
        if missing:
            self.google.add_sheet_tabs(spreadsheet_id=spreadsheet_id, sheet_names=missing)
        for sheet_name, headers in (
            (INPUT_SHEET_NAME, PAYMENT_HEADERS),
            (INVOICE_SHEET_NAME, INVOICE_HEADERS),
            (DETAIL_SHEET_NAME, DETAIL_HEADERS),
            (SUMMARY_SHEET_NAME, SUMMARY_HEADERS),
            (EXCEPTION_SHEET_NAME, EXCEPTION_HEADERS),
        ):
            current = self.google.fetch_sheet_values(
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                cell_range="A1:Z2",
            )
            values = current.get("values", []) if isinstance(current.get("values"), list) else []
            if not values or not any(str(value).strip() for value in (values[0] if values else [])):
                self.google.update_sheet_values(
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    cell_range=f"A1:{_column_letter(len(headers))}1",
                    values=[headers],
                )
        return {"ok": True, "created_tabs": missing, "tabs": required}

    def read_payments(
        self,
        *,
        period_start: date | None,
        period_end: date | None,
    ) -> tuple[list[PaymentRow], list[str]]:
        self.ensure_sheet_layout()
        response = self.google.fetch_sheet_values(
            spreadsheet_id=self.settings.fb_reconcile_sheet_id,
            sheet_name=INPUT_SHEET_NAME,
            cell_range="A1:K1000",
        )
        values = response.get("values", []) if isinstance(response.get("values"), list) else []
        if not values:
            return [], ["Tab Tiền trừ thẻ chưa có header."]
        headers = [_normalize_header(value) for value in values[0]]
        header_index = {name: idx for idx, name in enumerate(headers) if name}
        legacy_layout = _looks_like_legacy_payment_layout(headers)
        data_rows = values[1:]
        aliases = {
            "payment_id": ["payment_id", "ma_giao_dich", "transaction_id", "id"],
            "transaction_date": ["transaction_date", "ngay_giao_dich", "transactiondate"],
            "posting_date": ["posting_date", "ngay_hach_toan", "postingdate"],
            "amount": ["amount", "so_tien", "so_tien_tru", "card_amount"],
            "currency": ["currency", "tien_te", "don_vi_tien"],
            "bank_fee": ["bank_fee", "phi_ngan_hang", "fee"],
            "fx_rate": ["fx_rate", "ty_gia", "exchange_rate"],
            "description": ["description", "mo_ta", "noi_dung"],
            "account_hint": ["account_hint", "tai_khoan_goi_y", "account"],
            "reference": ["reference", "tham_chieu", "ref"],
            "note": ["note", "ghi_chu"],
        }
        indexes = {key: _first_index(header_index, names) for key, names in aliases.items()}
        if legacy_layout:
            indexes.update({
                "transaction_date": 0,
                "amount": 2,
                "currency": 3,
                "description": 1,
                "reference": 10,
            })
        elif indexes["amount"] is None and indexes["currency"] is None and _looks_like_legacy_payment_row(values[0]):
            legacy_layout = True
            data_rows = values
            indexes.update({
                "transaction_date": 0,
                "amount": 2,
                "currency": 3,
                "description": 1,
                "reference": 10,
            })
        errors: list[str] = []
        required_fields = ("amount", "currency") if legacy_layout else ("payment_id", "amount", "currency")
        for required in required_fields:
            if indexes[required] is None:
                errors.append(f"Thiếu cột {required}.")
        if errors:
            return [], errors

        rows: list[PaymentRow] = []
        for row_number, row in enumerate(data_rows, start=2 if data_rows is not values else 1):
            if not any(str(value).strip() for value in row):
                continue
            payment_id = _cell(row, indexes["payment_id"]) or (f"legacy_row_{row_number}" if legacy_layout else "")
            amount = parse_decimal(_cell(row, indexes["amount"]))
            currency = normalize_currency(_cell(row, indexes["currency"]))
            if legacy_layout and (amount is None or not currency):
                fallback_amount = parse_decimal(_cell(row, 8))
                fallback_currency = normalize_currency(_cell(row, 9))
                if amount is None or not currency:
                    amount, currency = fallback_amount, fallback_currency
            tx_date = parse_date_value(_cell(row, indexes["transaction_date"]))
            posting_date = parse_date_value(_cell(row, indexes["posting_date"]))
            if not payment_id or amount is None or not currency:
                errors.append(f"Dòng {row_number}: cần payment_id, amount và currency.")
                continue
            card_last4 = str(self.settings.fb_reconcile_card_last4 or "").strip()
            reference = _cell(row, indexes["reference"])
            if card_last4 and not re.sub(r"\D", "", reference).endswith(card_last4):
                continue
            effective_date = posting_date or tx_date
            if period_start and period_end and effective_date and not (period_start <= effective_date <= period_end):
                continue
            if period_start and period_end and not effective_date:
                errors.append(f"Dòng {row_number}: thiếu ngày giao dịch/hạch toán.")
                continue
            fx_rate = parse_decimal(_cell(row, indexes["fx_rate"]))
            if legacy_layout and fx_rate is None and currency == "VND":
                usd_amount = parse_decimal(_cell(row, 8))
                usd_currency = normalize_currency(_cell(row, 9))
                if usd_amount and usd_currency == "USD":
                    fx_rate = amount / usd_amount
            rows.append(
                PaymentRow(
                    payment_id=payment_id,
                    transaction_date=tx_date,
                    posting_date=posting_date,
                    amount=abs(amount),
                    currency=currency,
                    bank_fee=abs(parse_decimal(_cell(row, indexes["bank_fee"])) or Decimal("0")),
                    fx_rate=fx_rate,
                    description=_cell(row, indexes["description"]),
                    account_hint=normalize_account(_cell(row, indexes["account_hint"])),
                    reference=reference,
                    note=_cell(row, indexes["note"]),
                    source_row=row_number,
                )
            )
        return rows, errors

    def _build_result(
        self,
        *,
        run_id: str,
        invoices: list[dict[str, Any]],
        payments: list[PaymentRow],
        period_start: date | None,
        period_end: date | None,
    ) -> dict[str, Any]:
        details, exceptions = match_invoices(
            invoices,
            payments,
            date_window_days=self.settings.fb_reconcile_date_window_days,
            rounding_tolerance=self.settings.fb_reconcile_rounding_tolerance,
        )
        for detail in details:
            detail["run_id"] = run_id
        detail_rows = [detail_to_row(item) for item in details]
        exception_rows = [exception_to_row(run_id, item) for item in exceptions]
        invoice_rows = [invoice_to_row(run_id, item) for item in invoices]
        summary_rows = summary_to_rows(
            run_id=run_id,
            invoices=invoices,
            payments=payments,
            details=details,
            period_start=period_start,
            period_end=period_end,
        )
        status_counts: dict[str, int] = {}
        for item in details:
            status = str(item.get("status", "")).strip()
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "ok": not any(item.get("type") == "pdf_error" for item in exceptions),
            "run_id": run_id,
            "period_start": period_start.isoformat() if period_start else "",
            "period_end": period_end.isoformat() if period_end else "",
            "status_counts": status_counts,
            "invoice_rows": invoice_rows,
            "detail_rows": detail_rows,
            "summary_rows": summary_rows,
            "exception_rows": exception_rows,
            "details": [_detail_for_storage(item) for item in details],
            "exceptions": exceptions,
            "created_at": now_utc_iso(),
        }

    def _totals_by_account(self, invoices: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, Decimal]] = {}
        for item in invoices:
            account = normalize_account(item.get("account")) or "CHƯA XÁC ĐỊNH"
            currency = normalize_currency(item.get("currency")) or "UNKNOWN"
            total = parse_decimal(item.get("invoice_total")) or Decimal("0")
            result.setdefault(account, {})[currency] = result.setdefault(account, {}).get(currency, Decimal("0")) + total
        return {account: {currency: decimal_text(value) for currency, value in currencies.items()} for account, currencies in result.items()}


def parse_invoice_pdf(
    content: bytes,
    *,
    file_name: str,
    source_sha256: str,
    account_hint: str,
    jc_ad_account_id: str,
    vayxa_ad_account_id: str,
) -> ParsedInvoice:
    warning_parts: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(str(page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:  # noqa: BLE001
        return ParsedInvoice(file_name, source_sha256, "", "", "", "", "", None, f"Không đọc được PDF: {exc}", "")
    if not text:
        return ParsedInvoice(file_name, source_sha256, normalize_account(account_hint), "", "", "", "", None, "PDF không có lớp text; cần kiểm tra/OCR thủ công.", "")

    ad_account_id = _extract_account_id(text)
    account = normalize_account(account_hint) or account_from_ad_account(ad_account_id, jc_ad_account_id, vayxa_ad_account_id)
    if not account:
        upper_name = file_name.upper()
        if "VAYXA" in upper_name or "ADS2" in upper_name:
            account = "VAYXA"
        elif re.search(r"(^|[^A-Z])JC([^A-Z]|$)", upper_name):
            account = "JC"
    if not account:
        warning_parts.append("Chưa nhận diện được tài khoản JC/Vayxa.")

    invoice_id = _extract_invoice_id(text)
    invoice_date = _extract_invoice_date(text)
    currency = _extract_currency(text)
    total = _extract_total(text, currency)
    if not invoice_id:
        warning_parts.append("Thiếu invoice ID.")
    if not invoice_date:
        warning_parts.append("Thiếu ngày hóa đơn.")
    if not currency:
        warning_parts.append("Thiếu tiền tệ.")
    if total is None:
        warning_parts.append("Thiếu tổng tiền hóa đơn.")
    return ParsedInvoice(
        source_file=file_name,
        source_sha256=source_sha256,
        account=account,
        ad_account_id=ad_account_id,
        invoice_id=invoice_id,
        invoice_date=invoice_date,
        currency=currency,
        total=total,
        warning=" ".join(warning_parts),
        text_excerpt=" ".join(text.split())[:500],
    )


def match_invoices(
    invoices: list[dict[str, Any]],
    payments: list[PaymentRow],
    *,
    date_window_days: int,
    rounding_tolerance: Decimal,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exceptions: list[dict[str, Any]] = []
    usable_invoices = [item for item in invoices if parse_decimal(item.get("invoice_total")) is not None]
    details: list[dict[str, Any]] = []
    for item in invoices:
        if parse_decimal(item.get("invoice_total")) is not None:
            continue
        account = normalize_account(item.get("account")) or "CHƯA XÁC ĐỊNH"
        reference = str(item.get("invoice_id", ""))
        exceptions.append({
            "type": "pdf_error",
            "account": account,
            "reference": reference,
            "message": "PDF thiếu tổng tiền nên chưa thể đối soát.",
            "source_file": str(item.get("source_file", "")),
        })
        details.append({
            "account": account,
            "invoice_id": reference,
            "status": "PDF CẦN KIỂM TRA",
            "match_reason": "PDF thiếu tổng tiền.",
            "confidence": 0,
            "invoice": item,
            "payment": None,
        })
    candidates: dict[int, list[dict[str, Any]]] = {}
    for idx, invoice in enumerate(usable_invoices):
        candidates[idx] = []
        for payment_index, payment in enumerate(payments):
            candidate = _candidate_for(invoice, payment, date_window_days=date_window_days, tolerance=rounding_tolerance)
            if candidate:
                candidate["payment_index"] = payment_index
                candidates[idx].append(candidate)
        candidates[idx].sort(key=lambda item: (-int(item["score"]), int(item["payment_index"])))

    assigned: dict[int, int] = {}
    used_payments: set[int] = set()
    unresolved = set(candidates)

    # First consume uniquely strong one-to-one matches.
    progress = True
    while progress:
        progress = False
        for invoice_index in list(unresolved):
            strong = [item for item in candidates[invoice_index] if item["strong"] and item["payment_index"] not in used_payments]
            if not strong:
                continue
            best_score = int(strong[0]["score"])
            best = [item for item in strong if int(item["score"]) == best_score]
            if len(best) != 1:
                continue
            chosen = best[0]
            assigned[invoice_index] = int(chosen["payment_index"])
            used_payments.add(int(chosen["payment_index"]))
            unresolved.remove(invoice_index)
            progress = True

    for invoice_index in sorted(assigned):
        invoice = usable_invoices[invoice_index]
        payment = payments[assigned[invoice_index]]
        candidate = _candidate_for(invoice, payment, date_window_days=date_window_days, tolerance=rounding_tolerance) or {}
        details.append(_detail_for(invoice, payment, candidate, run_id=""))

    # Foreign-currency pairs without an entered FX rate are reported, never silently marked exact.
    for invoice_index in sorted(unresolved):
        invoice = usable_invoices[invoice_index]
        available = [item for item in candidates[invoice_index] if item["payment_index"] not in used_payments and item["mode"] == "fx_unknown"]
        if len(available) == 1:
            payment_index = int(available[0]["payment_index"])
            used_payments.add(payment_index)
            details.append(_detail_for(invoice, payments[payment_index], available[0], run_id=""))
            unresolved.discard(invoice_index)
        elif len(available) > 1:
            details.append({
                "account": normalize_account(invoice.get("account")) or "CHƯA XÁC ĐỊNH",
                "invoice_id": str(invoice.get("invoice_id", "")),
                "status": "MƠ HỒ",
                "match_reason": "Có nhiều giao dịch thẻ phù hợp khi chưa có tỷ giá.",
                "confidence": 0,
                "invoice": invoice,
                "payment": None,
            })
            unresolved.discard(invoice_index)

    for invoice_index in sorted(unresolved):
        invoice = usable_invoices[invoice_index]
        exceptions.append({
            "type": "invoice_unmatched",
            "account": normalize_account(invoice.get("account")) or "CHƯA XÁC ĐỊNH",
            "reference": str(invoice.get("invoice_id", "")),
            "message": "Không tìm thấy giao dịch thẻ phù hợp trong kỳ.",
            "source_file": str(invoice.get("source_file", "")),
        })
        details.append({
            "account": normalize_account(invoice.get("account")) or "CHƯA XÁC ĐỊNH",
            "invoice_id": str(invoice.get("invoice_id", "")),
            "status": "HÓA ĐƠN CHƯA GHÉP",
            "match_reason": "Không tìm thấy giao dịch thẻ phù hợp trong kỳ.",
            "confidence": 0,
            "invoice": invoice,
            "payment": None,
        })
    for payment_index, payment in enumerate(payments):
        if payment_index not in used_payments:
            exceptions.append({
                "type": "payment_unmatched",
                "account": payment.account_hint or "CHƯA XÁC ĐỊNH",
                "reference": payment.payment_id,
                "message": "Giao dịch thẻ chưa được ghép với hóa đơn.",
                "source_file": "",
            })
    return details, exceptions


def _candidate_for(invoice: dict[str, Any], payment: PaymentRow, *, date_window_days: int, tolerance: Decimal) -> dict[str, Any] | None:
    account = normalize_account(invoice.get("account"))
    if payment.account_hint and account and payment.account_hint != account:
        return None
    invoice_date = parse_date_value(invoice.get("invoice_date"))
    payment_date = payment.effective_date
    if invoice_date and payment_date and abs((payment_date - invoice_date).days) > date_window_days:
        return None
    invoice_id = str(invoice.get("invoice_id", "")).strip().lower()
    searchable = " ".join([payment.payment_id, payment.description, payment.reference, payment.note]).lower()
    ref_hit = bool(invoice_id and invoice_id in searchable)
    expected = _expected_for(invoice, payment)
    if expected is not None:
        residual = payment.amount - expected
        exact = abs(residual) <= tolerance
        if not exact and not ref_hit and normalize_currency(invoice.get("currency")) != payment.currency and payment.fx_rate is None:
            return None
        return {
            "mode": "exact" if exact else ("reference" if ref_hit else "amount_variance"),
            "expected": expected,
            "residual": residual,
            "strong": exact or ref_hit,
            "score": ((1000 if ref_hit else 0) + (500 if exact else 0) + (100 if payment.account_hint == account and account else 0) - abs((payment_date - invoice_date).days)) if payment_date and invoice_date else (1000 if ref_hit else 0),
        }
    if normalize_currency(invoice.get("currency")) != payment.currency and payment_date and invoice_date:
        return {
            "mode": "fx_unknown",
            "expected": None,
            "residual": None,
            "strong": False,
            "score": (100 if payment.account_hint == account and account else 0) - abs((payment_date - invoice_date).days),
        }
    return None


def _expected_for(invoice: dict[str, Any], payment: PaymentRow) -> Decimal | None:
    invoice_currency = normalize_currency(invoice.get("currency"))
    total = parse_decimal(invoice.get("invoice_total"))
    if total is None:
        return None
    fee = payment.bank_fee
    if invoice_currency == payment.currency:
        return total + fee
    if payment.fx_rate is not None:
        return total * payment.fx_rate + fee
    return None


def _detail_for(invoice: dict[str, Any], payment: PaymentRow, candidate: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    invoice_total = parse_decimal(invoice.get("invoice_total")) or Decimal("0")
    expected = candidate.get("expected")
    effective_fx = None
    converted = expected - payment.bank_fee if expected is not None else None
    residual = candidate.get("residual")
    if candidate.get("mode") == "fx_unknown" and invoice_total:
        effective_fx = payment.amount / invoice_total
        converted = payment.amount - payment.bank_fee
        residual = payment.amount - converted
    if candidate.get("mode") == "fx_unknown":
        status = "KHỚP CÓ LỆCH PHÍ/TỶ GIÁ"
        reason = "Ghép theo kỳ/ngày; chưa có tỷ giá ngân hàng để kết luận chính xác."
        confidence = 45
    elif residual is not None and residual == 0:
        status = "KHỚP"
        reason = "Khớp theo số tiền và thông tin giao dịch."
        confidence = 100
    else:
        status = "KHỚP CÓ LỆCH PHÍ/TỶ GIÁ"
        reason = "Đã ghép được nhưng còn phần chênh chưa giải thích."
        confidence = 85 if candidate.get("mode") == "reference" else 70
    return {
        "run_id": run_id,
        "account": normalize_account(invoice.get("account")) or "CHƯA XÁC ĐỊNH",
        "invoice_id": str(invoice.get("invoice_id", "")),
        "invoice_date": str(invoice.get("invoice_date", "")),
        "invoice_currency": normalize_currency(invoice.get("currency")),
        "invoice_total": invoice_total,
        "payment_id": payment.payment_id,
        "payment_date": payment.effective_date.isoformat() if payment.effective_date else "",
        "payment_currency": payment.currency,
        "payment_amount": candidate.get("allocated_payment_amount", payment.amount),
        "bank_fee": payment.bank_fee,
        "fx_rate": payment.fx_rate,
        "effective_fx_rate": effective_fx,
        "converted_invoice_amount": converted,
        "residual": residual,
        "status": status,
        "match_reason": reason,
        "confidence": confidence,
        "invoice": invoice,
        "payment": payment,
    }


def invoice_to_dict(invoice: ParsedInvoice) -> dict[str, Any]:
    return {
        "source_file": invoice.source_file,
        "source_sha256": invoice.source_sha256,
        "account": invoice.account,
        "ad_account_id": invoice.ad_account_id,
        "invoice_id": invoice.invoice_id,
        "invoice_date": invoice.invoice_date,
        "currency": invoice.currency,
        "invoice_total": decimal_text(invoice.total),
        "parse_status": "OK" if not invoice.warning and invoice.total is not None else "CẦN KIỂM TRA",
        "warning": invoice.warning,
        "text_excerpt": invoice.text_excerpt,
    }


def invoice_to_row(run_id: str, invoice: dict[str, Any]) -> list[Any]:
    return [run_id, invoice.get("source_file", ""), invoice.get("source_sha256", ""), invoice.get("account", ""), invoice.get("ad_account_id", ""), invoice.get("invoice_id", ""), invoice.get("invoice_date", ""), invoice.get("currency", ""), invoice.get("invoice_total", ""), invoice.get("parse_status", ""), invoice.get("warning", "")]


def detail_to_row(detail: dict[str, Any]) -> list[Any]:
    row_key = "|".join([str(detail.get("run_id", "")), str(detail.get("invoice_id", "")), str(detail.get("payment_id", ""))])
    return [row_key, detail.get("run_id", ""), detail.get("account", ""), detail.get("invoice_id", ""), detail.get("invoice_date", ""), detail.get("invoice_currency", ""), decimal_text(detail.get("invoice_total")), detail.get("payment_id", ""), detail.get("payment_date", ""), detail.get("payment_currency", ""), decimal_text(detail.get("payment_amount")), decimal_text(detail.get("bank_fee")), decimal_text(detail.get("fx_rate")), decimal_text(detail.get("effective_fx_rate")), decimal_text(detail.get("converted_invoice_amount")), decimal_text(detail.get("residual")), detail.get("status", ""), detail.get("match_reason", ""), detail.get("confidence", 0)]


def _detail_for_storage(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        key: decimal_text(value) if isinstance(value, Decimal) else value
        for key, value in detail.items()
        if key not in {"invoice", "payment"}
    }


def exception_to_row(run_id: str, exception: dict[str, Any]) -> list[Any]:
    row_key = "|".join([run_id, str(exception.get("type", "")), str(exception.get("reference", ""))])
    return [row_key, run_id, exception.get("type", ""), exception.get("account", ""), exception.get("reference", ""), exception.get("message", ""), exception.get("source_file", "")]


def summary_to_rows(*, run_id: str, invoices: list[dict[str, Any]], payments: list[PaymentRow], details: list[dict[str, Any]], period_start: date | None, period_end: date | None) -> list[list[Any]]:
    accounts = sorted({normalize_account(item.get("account")) or "CHƯA XÁC ĐỊNH" for item in invoices} | {item.account_hint for item in payments if item.account_hint})
    rows: list[list[Any]] = []
    for account in accounts:
        account_invoices = [item for item in invoices if (normalize_account(item.get("account")) or "CHƯA XÁC ĐỊNH") == account]
        account_details = [item for item in details if item.get("account") == account]
        account_payments = [item for item in payments if item.account_hint == account]
        statuses = {str(item.get("status", "")) for item in account_details}
        status = "CẦN KIỂM TRA" if any(item not in {"KHỚP"} for item in statuses) else "KHỚP"
        rows.append([
            f"{run_id}|{account}", run_id, period_start.isoformat() if period_start else "", period_end.isoformat() if period_end else "", account,
            len(account_invoices), sum(1 for item in account_details if item.get("status") in {"KHỚP", "KHỚP CÓ LỆCH PHÍ/TỶ GIÁ"}),
            totals_text(account_invoices, amount_key="invoice_total", currency_key="currency"),
            totals_by_currency_text(account_payments),
            decimal_text(sum((item.bank_fee for item in account_payments), Decimal("0"))),
            decimal_text(sum((item.get("residual") or Decimal("0") for item in account_details if item.get("residual") is not None), Decimal("0"))),
            status,
        ])
    all_statuses = {str(item.get("status", "")) for item in details}
    rows.append([
        f"{run_id}|TỔNG JC + VAYXA", run_id, period_start.isoformat() if period_start else "", period_end.isoformat() if period_end else "", "TỔNG JC + VAYXA",
        len(invoices), sum(1 for item in details if item.get("status") in {"KHỚP", "KHỚP CÓ LỆCH PHÍ/TỶ GIÁ"}),
        totals_text(invoices, amount_key="invoice_total", currency_key="currency"),
        totals_by_currency_text(payments),
        decimal_text(sum((item.bank_fee for item in payments), Decimal("0"))),
        decimal_text(sum((item.get("residual") or Decimal("0") for item in details if item.get("residual") is not None), Decimal("0"))),
        "CẦN KIỂM TRA" if any(item not in {"KHỚP"} for item in all_statuses) else "KHỚP",
    ])
    return rows


def totals_by_currency(payments: list[PaymentRow]) -> dict[str, str]:
    totals: dict[str, Decimal] = {}
    for payment in payments:
        totals[payment.currency] = totals.get(payment.currency, Decimal("0")) + payment.amount
    return {key: decimal_text(value) for key, value in totals.items()}


def totals_by_currency_text(payments: list[PaymentRow]) -> str:
    return "; ".join(f"{key}={value}" for key, value in totals_by_currency(payments).items())


def totals_text(items: list[dict[str, Any]], *, amount_key: str, currency_key: str) -> str:
    totals: dict[str, Decimal] = {}
    for item in items:
        currency = normalize_currency(item.get(currency_key)) or "UNKNOWN"
        totals[currency] = totals.get(currency, Decimal("0")) + (parse_decimal(item.get(amount_key)) or Decimal("0"))
    return "; ".join(f"{key}={decimal_text(value)}" for key, value in totals.items())


def parse_period(raw: str) -> tuple[date | None, date | None]:
    value = " ".join(str(raw or "").strip().split())
    if not value:
        return None, None
    match = re.search(r"(20\d{2})[-/](0?[1-9]|1[0-2])", value)
    if not match:
        match = re.search(r"(0?[1-9]|1[0-2])[-/](20\d{2})", value)
        if match:
            year, month = int(match.group(2)), int(match.group(1))
        else:
            return None, None
    else:
        year, month = int(match.group(1)), int(match.group(2))
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return start, end


def infer_period_from_invoices(invoices: list[dict[str, Any]]) -> tuple[date | None, date | None]:
    dates = [parse_date_value(item.get("invoice_date")) for item in invoices]
    dates = [item for item in dates if item]
    if not dates:
        return None, None
    if len({(item.year, item.month) for item in dates}) == 1:
        return parse_period(f"{dates[0].year:04d}-{dates[0].month:02d}")
    return min(dates), max(dates)


def normalize_account(value: Any) -> str:
    text = _fold(str(value or "")).upper()
    if "VAYXA" in text or text in {"ADS2", "VX"}:
        return "VAYXA"
    if text == "JC" or re.search(r"(^|[^A-Z])JC([^A-Z]|$)", text):
        return "JC"
    return ""


def account_from_ad_account(ad_account_id: str, jc_id: str, vayxa_id: str) -> str:
    normalized = re.sub(r"\D", "", str(ad_account_id or ""))
    if normalized and normalized == re.sub(r"\D", "", str(jc_id or "")):
        return "JC"
    if normalized and normalized == re.sub(r"\D", "", str(vayxa_id or "")):
        return "VAYXA"
    return ""


def _extract_account_id(text: str) -> str:
    labels = r"(?:ad\s*account(?:\s*id)?|account\s*id|tai\s*khoan)"
    for candidate_text in (text, " ".join(text.split())):
        match = re.search(rf"{labels}[^\n]{{0,60}}?act[_\s:-]*(\d{{5,}})", candidate_text, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _label_value_candidates(text: str, labels: str, *, lookahead: int = 2) -> list[str]:
    """Return values on a label line and the following non-empty lines.

    Facebook's PDF text layer often places a label and its value in separate
    text boxes. A line-only regex therefore misses fields even though the PDF
    is selectable/searchable.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    candidates: list[str] = []
    for index, line in enumerate(lines):
        match = re.search(labels, line, flags=re.I)
        if not match:
            continue
        suffix = line[match.end():].strip(" :#-\t")
        if suffix:
            candidates.append(suffix)
        for offset in range(1, lookahead + 1):
            if index + offset < len(lines):
                candidates.append(lines[index + offset])
    return candidates


def _extract_invoice_id(text: str) -> str:
    labels = (
        r"(?:invoice\s*(?:number|no\.?|id|reference)|receipt\s*(?:number|no\.?|id)|"
        r"payment\s*(?:id|reference|number)|transaction\s*(?:id|reference|number)|"
        r"billing\s*(?:id|reference|number)|ma\s*hoa\s*don)"
    )
    for candidate in _label_value_candidates(text, labels):
        value = re.sub(r"^(?:no\.?|number|id|reference)\s*[:#-]?\s*", "", candidate, flags=re.I).strip()
        token_match = re.search(r"(?<![A-Za-z0-9])([A-Z0-9][A-Z0-9._/-]{4,})(?![A-Za-z0-9])", value, flags=re.I)
        if not token_match:
            continue
        token = token_match.group(1).strip("._/-")
        if parse_date_value(token) or token.lower() in {"invoice", "receipt", "payment", "transaction", "reference"}:
            continue
        return token
    match = re.search(r"\b(FB[A-Z0-9][A-Z0-9._/-]{5,})\b", text, flags=re.I)
    if match:
        return match.group(1).strip()
    return ""


def _extract_invoice_date(text: str) -> str:
    labels = r"(?:invoice\s*date|billing\s*date|statement\s*date|date|ngay)"
    candidates = _label_value_candidates(text, labels, lookahead=1)
    candidates.extend(re.findall(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b|\b[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}\b", text))
    for candidate in candidates:
        parsed = parse_date_value(candidate)
        if parsed:
            return parsed.isoformat()
    return ""


def _extract_currency(text: str) -> str:
    upper = text.upper()
    if "US$" in upper or "US $" in upper:
        return "USD"
    for token in ("USD", "THB", "VND", "EUR", "SGD", "AUD", "CAD", "GBP"):
        if re.search(rf"\b{token}\b", upper):
            return token
    if "$" in text:
        return "USD"
    return ""


def _extract_total(text: str, currency: str) -> Decimal | None:
    labels = (
        r"(?:amount\s+charged|amount\s+paid|payment\s+amount|paid\s+amount|"
        r"total\s+amount|grand\s+total|balance\s+due|amount\s+due|"
        r"total|paid|tong\s*(?:tien|cong)|so\s*tien)"
    )
    for value in reversed(_label_value_candidates(text, labels)):
        amount = _amount_from_text(value)
        if amount is not None:
            return amount
    # Do not guess from arbitrary numbers such as account IDs, dates, or
    # invoice numbers. A missing labeled total must remain a PDF exception.
    return None


def _amount_from_text(value: str) -> Decimal | None:
    token = re.sub(r"[^0-9,.-]", "", str(value or "")).strip(".,-")
    if not token or not re.search(r"\d", token):
        return None
    try:
        if "," in token and "." in token:
            decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            token = token.replace(thousands_sep, "").replace(decimal_sep, ".")
        elif "," in token:
            tail = token.rsplit(",", 1)[1]
            token = token.replace(",", "." if len(tail) <= 2 else "")
        elif "." in token:
            tail = token.rsplit(".", 1)[1]
            if len(tail) != 2:
                token = token.replace(".", "")
        return Decimal(token)
    except InvalidOperation:
        return None


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, Decimal):
        return value
    return _amount_from_text(str(value))


def parse_date_value(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
        "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
        "%B %d, %Y", "%b %d, %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def normalize_currency(value: Any) -> str:
    text = str(value or "").strip().upper().replace("$", "USD")
    return text if re.fullmatch(r"[A-Z]{3}", text) else ""


def decimal_text(value: Any) -> str:
    parsed = parse_decimal(value)
    if parsed is None:
        return ""
    return format(parsed, "f").rstrip("0").rstrip(".") or "0"


def _parse_iso_date(value: Any) -> date | None:
    return parse_date_value(value)


def _cell(row: list[Any], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _first_index(header_index: dict[str, int], names: list[str]) -> int | None:
    for name in names:
        if name in header_index:
            return header_index[name]
    return None


def _normalize_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _fold(str(value or "")).lower()).strip("_")


def _looks_like_legacy_payment_layout(headers: list[str]) -> bool:
    header_set = set(headers)
    return {"ngay", "vnd", "usd", "card"}.issubset(header_set)


def _looks_like_legacy_payment_row(row: list[Any]) -> bool:
    return len(row) >= 11 and parse_date_value(row[0]) is not None and bool(_cell(row, 10))


def _fold(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").replace("đ", "d").replace("Đ", "D")


def _column_letter(number: int) -> str:
    result = ""
    value = max(1, number)
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result
