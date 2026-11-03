from decimal import Decimal
from datetime import date
import logging

from app import fb_payment_reconcile_service as reconcile
from app.fb_payment_reconcile_service import FbPaymentReconcileService, decimal_text, normalize_account, parse_invoice_pdf


def test_account_labels_keep_jc_singapore_separate() -> None:
    assert normalize_account("JC") == "JC"
    assert normalize_account("hoá đơn JC Singapore") == "JC Singapore"
    assert normalize_account("JC SG") == "JC Singapore"
    assert normalize_account("Vayxa") == "VAYXA"
    assert normalize_account("B2B") == "B2B"


def test_ad_account_override_labels_new_invoice_as_b2b(monkeypatch) -> None:  # noqa: ANN001
    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return (
                "Ad account ID\nact_894644562928775\n"
                "Receipt ID\nB2B-20261027-001\n"
                "Date\nOctober 27, 2026\n"
                "Amount paid\nUSD\n123.45"
            )

    class FakeReader:
        pages = [FakePage()]

        def __init__(self, _stream) -> None:  # noqa: ANN001
            pass

    monkeypatch.setattr(reconcile, "PdfReader", FakeReader)
    parsed = parse_invoice_pdf(
        b"pdf",
        file_name="JC_Singapore_2026-10.pdf",
        source_sha256="hash-b2b",
        account_hint="JC Singapore",
        jc_ad_account_id="123456789",
        vayxa_ad_account_id="987654321",
    )
    assert parsed.account == "B2B"
    assert parsed.ad_account_id == "894644562928775"


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
            if kwargs["sheet_name"] == "ADS":
                return {"values": [
                    [], [], [], [], [], [], [], [],
                    [
                    "payment_id", "transaction_date", "posting_date", "amount", "currency",
                    "bank_fee", "fx_rate", "description", "merchant_name", "account_hint", "reference", "note",
                    ], [
                    "P3036", "2026-08-10", "", "100", "USD", "", "", "clear",
                    "FACEBK *FEGZYHM2", "", "532959***3036", "",
                    ], [
                    "", "2026-08-10", "", "", "", "", "", "clear",
                    "FACEBK *OTHER", "", "532959***4472", "",
                    ],
                ]}
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


def test_payment_labels_are_written_to_column_m_only() -> None:
    class Settings:
        fb_reconcile_sheet_id = "sheet"
        fb_reconcile_card_last4 = "3036"

    class FakeGoogle:
        def __init__(self) -> None:
            self.updates: list[dict] = []

        def fetch_spreadsheet_metadata(self, **_kwargs):  # noqa: ANN003
            return {"sheets": [{"properties": {"title": title}} for title in ("ADS", "FB Hóa đơn", "FB Chi tiết", "FB Tổng hợp", "FB Ngoại lệ")]}

        def fetch_sheet_values(self, **kwargs):  # noqa: ANN003
            assert kwargs["cell_range"] == "A1:Z1000"
            return {
                "values": [
                    [], [], [], [], [], [], [], [],
                    [
                        "payment_id", "transaction_date", "posting_date", "amount", "currency",
                        "bank_fee", "fx_rate", "description", "merchant_name", "account_hint",
                        "reference", "note",
                    ],
                    ["P3036", "2026-08-10", "", "100", "USD", "", "", "clear", "FACEBK", "", "", ""],
                ]
            }

        def batch_update_sheet_values(self, **kwargs):  # noqa: ANN003
            self.updates.append(kwargs)
            return {"ok": True}

        def update_sheet_values(self, **kwargs):  # noqa: ANN003
            self.updates.append(kwargs)
            return {"ok": True}

    fake_google = FakeGoogle()
    service = FbPaymentReconcileService(
        settings=Settings(), google=fake_google, storage=None, logger=logging.getLogger("test")
    )
    detail_row = [
        "run", "file", "JC", "", "", "", "", "P3036", "", "", "", "", "", "", "", "", "KHỚP", ""
    ]

    result = service._write_payment_labels({"detail_rows": [detail_row]})

    assert result["labels"] == {"P3036": "JC"}
    assert len(fake_google.updates) == 1
    assert fake_google.updates[0]["data"] == [
        {"range": "'ADS'!M9", "majorDimension": "ROWS", "values": [["FB_label"]]},
        {"range": "'ADS'!M10", "majorDimension": "ROWS", "values": [["JC"]]},
    ]


def test_payment_labels_write_jc_singapore_to_column_m() -> None:
    class Settings:
        fb_reconcile_sheet_id = "sheet"
        fb_reconcile_card_last4 = "3036"

    class FakeGoogle:
        def __init__(self) -> None:
            self.updates: list[dict] = []

        def fetch_sheet_values(self, **_kwargs):  # noqa: ANN003
            return {
                "values": [
                    [], [], [], [], [], [], [], [],
                    [
                        "payment_id", "transaction_date", "posting_date", "amount", "currency",
                        "bank_fee", "fx_rate", "description", "merchant_name", "account_hint",
                        "reference", "note",
                    ],
                    ["P3036-SG", "2026-09-10", "", "100", "USD", "", "", "clear", "FACEBK", "", "", ""],
                ]
            }

        def batch_update_sheet_values(self, **kwargs):  # noqa: ANN003
            self.updates.append(kwargs)
            return {"ok": True}

    fake_google = FakeGoogle()
    service = FbPaymentReconcileService(
        settings=Settings(), google=fake_google, storage=None, logger=logging.getLogger("test")
    )
    detail_row = [
        "run", "file", "JC Singapore", "", "", "", "", "P3036-SG", "", "", "", "", "", "", "", "", "KHỚP", ""
    ]

    result = service._write_payment_labels({"detail_rows": [detail_row]})

    assert result["labels"] == {"P3036-SG": "JC Singapore"}
    assert fake_google.updates[0]["data"][-1] == {
        "range": "'ADS'!M10", "majorDimension": "ROWS", "values": [["JC Singapore"]]
    }


def test_ads_legacy_layout_uses_header_row_8_and_fb_label_column() -> None:
    class Settings:
        fb_reconcile_sheet_id = "sheet"
        fb_reconcile_card_last4 = "3036"

    class FakeGoogle:
        def __init__(self) -> None:
            self.updates: list[dict] = []

        def fetch_spreadsheet_metadata(self, **_kwargs):  # noqa: ANN003
            return {"sheets": [{"properties": {"title": title}} for title in ("ADS", "FB Hóa đơn", "FB Chi tiết", "FB Tổng hợp", "FB Ngoại lệ")]}

        def fetch_sheet_values(self, **kwargs):  # noqa: ANN003
            if kwargs["sheet_name"] == "ADS":
                return {
                    "values": [
                        [], [], [], [], [], [], [], [],
                        ["Tháng", "Ngày", "", "VNĐ", "", "", "", "", "", "USD", "", "Card", "Merchant Name", "FB_label", "Nhan"],
                        [10, "10/26/2026 14:20", "clear", "259.351", "VND", "", "", "", "", "10.04", "USD", "532959***3036", "FACEBK *CHWARBJYD4", "", ""],
                    ]
                }
            return {"values": []}

        def batch_update_sheet_values(self, **kwargs):  # noqa: ANN003
            self.updates.append(kwargs)
            return {"ok": True}

        def update_sheet_values(self, **_kwargs):  # noqa: ANN003
            return {"ok": True}

    fake_google = FakeGoogle()
    service = FbPaymentReconcileService(
        settings=Settings(), google=fake_google, storage=None, logger=logging.getLogger("test")
    )
    rows, errors = service.read_payments(period_start=date(2026, 9, 26), period_end=date(2026, 10, 26))

    assert errors == []
    assert len(rows) == 1
    assert rows[0].payment_id == "legacy_row_10"
    assert rows[0].amount == Decimal("259351")
    assert rows[0].merchant_name == "FACEBK *CHWARBJYD4"

    result = service._write_payment_labels(
        {"detail_rows": [["run", "file", "B2B", "", "", "", "", "legacy_row_10", "", "", "", "", "", "", "", "", "KHỚP", ""]]}
    )

    assert result["labels"] == {"legacy_row_10": "B2B"}
    assert fake_google.updates[0]["data"] == [
        {"range": "'ADS'!N10", "majorDimension": "ROWS", "values": [["B2B"]]},
        {"range": "'ADS'!O10", "majorDimension": "ROWS", "values": [["Vayxa"]]},
    ]


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
