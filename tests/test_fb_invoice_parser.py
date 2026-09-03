from decimal import Decimal
from datetime import date
import logging

from app import fb_payment_reconcile_service as reconcile
from app.fb_payment_reconcile_service import FbPaymentReconcileService, decimal_text, parse_invoice_pdf


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


def test_parser_reads_meta_billing_report_as_transactions_and_filters_card(monkeypatch) -> None:  # noqa: ANN001
    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return (
                "Tài khoản: 2123778324821105\n"
                "Báo cáo lập hóa đơn: 25/7/2026 - 1/9/2026\n"
                "Ngày ID giao dịch Phương thức thanh toán Số tiền Trạng thái thanh toán\n"
                "31/8/2026 28250390024647339-283257447404\n"
                "45203 MasterCard ···· 3036 4.000.000 ₫ (VND) Đã thanh toán\n"
                "25/7/2026 27911319241887762-277565480640\n"
                "02342 Visa ···· 3691 1.135.877 ₫ (VND) Đã thanh toán\n"
                "Tổng số tiền đã lập hóa đơn 5.135.877 ₫ (VND)"
            )

    class FakeReader:
        pages = [FakePage()]

        def __init__(self, _stream) -> None:  # noqa: ANN001
            pass

    monkeypatch.setattr(reconcile, "PdfReader", FakeReader)
    parsed = parse_invoice_pdf(
        b"pdf",
        file_name="Đối soát JC.pdf",
        source_sha256="hash-report",
        account_hint="JC",
        jc_ad_account_id="",
        vayxa_ad_account_id="",
        card_last4="3036",
    )
    assert len(parsed.line_items) == 1
    assert parsed.line_items[0]["invoice_id"] == "28250390024647339-28325744740445203"
    assert parsed.line_items[0]["invoice_total"] == "4000000"
    assert parsed.total == Decimal("4000000")
    assert parsed.currency == "VND"
    assert parsed.warning == ""


def test_parser_reads_meta_report_with_card_in_header(monkeypatch) -> None:  # noqa: ANN001
    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return (
                "Phương thức thanh toán: MasterCard ···· 3036\n"
                "Ngày ID giao dịch Số tiền Trạng thái thanh toán\n"
                "28/8/2026 28536038986087147-28270933995930977 62 ₫ (VND) Đã thanh toán\n"
                "25/7/2026 27862005590157161-27992498120441233 1.925.310 ₫ (VND) Đã thanh toán\n"
                "Tổng số tiền đã lập hóa đơn 1.925.372 ₫ (VND)"
            )

    class FakeReader:
        pages = [FakePage()]

        def __init__(self, _stream) -> None:  # noqa: ANN001
            pass

    monkeypatch.setattr(reconcile, "PdfReader", FakeReader)
    parsed = parse_invoice_pdf(
        b"pdf",
        file_name="Đối soát Vayxa.pdf",
        source_sha256="hash-report-2",
        account_hint="Vayxa",
        jc_ad_account_id="",
        vayxa_ad_account_id="",
        card_last4="3036",
    )
    assert len(parsed.line_items) == 2
    assert parsed.total == Decimal("1925372")
    assert parsed.line_items[0]["invoice_id"] == "28536038986087147-28270933995930977"
    assert parsed.warning == ""


def test_decimal_text_preserves_integer_zeroes() -> None:
    assert decimal_text(Decimal("4000000")) == "4000000"
    assert decimal_text(Decimal("123.4500")) == "123.45"
