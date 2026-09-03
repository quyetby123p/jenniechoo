from decimal import Decimal
from datetime import date
import logging

from app import fb_payment_reconcile_service as reconcile
from app.fb_payment_reconcile_service import FbPaymentReconcileService, parse_invoice_pdf


def test_parser_reads_facebook_fields_when_values_are_on_following_lines(monkeypatch) -> None:  # noqa: ANN001
    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return (
                "Ad account ID\nact_123456789\n"
                "Receipt ID\n123456789012345\n"
                "Date\nAugust 10, 2026\n"
                "Amount paid\nUSD\n123.45"
            )

    class FakeReader:
        pages = [FakePage()]

        def __init__(self, _stream) -> None:  # noqa: ANN001
            pass

    monkeypatch.setattr(reconcile, "PdfReader", FakeReader)
    parsed = parse_invoice_pdf(
        b"pdf",
        file_name="JC_2026-08.pdf",
        source_sha256="hash",
        account_hint="",
        jc_ad_account_id="123456789",
        vayxa_ad_account_id="987654321",
    )
    assert parsed.account == "JC"
    assert parsed.invoice_id == "123456789012345"
    assert parsed.invoice_date == "2026-08-10"
    assert parsed.currency == "USD"
    assert parsed.total == Decimal("123.45")
    assert parsed.warning == ""


def test_parser_reads_total_from_compact_facebook_label(monkeypatch) -> None:  # noqa: ANN001
    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return "Receipt number: FB-20260810-123456\nInvoice date: 2026-08-10\nTotal amount: US$ 1,925.31"

    class FakeReader:
        pages = [FakePage()]

        def __init__(self, _stream) -> None:  # noqa: ANN001
            pass

    monkeypatch.setattr(reconcile, "PdfReader", FakeReader)
    parsed = parse_invoice_pdf(
        b"pdf",
        file_name="VAYXA_2026-08.pdf",
        source_sha256="hash-2",
        account_hint="Vayxa",
        jc_ad_account_id="123456789",
        vayxa_ad_account_id="987654321",
    )
    assert parsed.account == "VAYXA"
    assert parsed.invoice_id == "FB-20260810-123456"
    assert parsed.currency == "USD"
    assert parsed.total == Decimal("1925.31")
    assert parsed.warning == ""


def test_payment_reader_keeps_merchant_name_and_reads_sheet_beyond_column_k() -> None:
    class Settings:
        fb_reconcile_sheet_id = "sheet"
        fb_reconcile_card_last4 = "3036"

    class FakeGoogle:
        def fetch_spreadsheet_metadata(self, **_kwargs):  # noqa: ANN003
            return {"sheets": []}

        def fetch_sheet_values(self, **kwargs):  # noqa: ANN003
            if kwargs["sheet_name"] == "Tiền trừ thẻ":
                return {"values": [[
                    "payment_id", "transaction_date", "posting_date", "amount", "currency",
                    "bank_fee", "fx_rate", "description", "merchant_name", "account_hint", "reference", "note",
                ], [
                    "P3036", "2026-08-10", "", "100", "USD", "", "", "clear",
                    "FACEBK *FEGZYHM2", "", "532959***3036", "",
                ]]}
            return {"values": []}

        def update_sheet_values(self, **_kwargs):  # noqa: ANN003
            return {"ok": True}

        def add_sheet_tabs(self, **_kwargs):  # noqa: ANN003
            return {"ok": True}

    service = FbPaymentReconcileService(
        settings=Settings(), google=FakeGoogle(), storage=None, logger=logging.getLogger("test")
    )
    rows, errors = service.read_payments(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31))
    assert errors == []
    assert rows[0].merchant_name == "FACEBK *FEGZYHM2"
