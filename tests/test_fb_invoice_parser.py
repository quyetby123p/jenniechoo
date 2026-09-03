from decimal import Decimal

from app import fb_payment_reconcile_service as reconcile
from app.fb_payment_reconcile_service import parse_invoice_pdf


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
