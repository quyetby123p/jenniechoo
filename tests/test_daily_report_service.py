from datetime import date
import logging
from pathlib import Path

from app.daily_report_service import DailyReportService
from app.settings import Settings
from app.utils import dump_json


class _FakePancakeClient:
    def __init__(self, orders=None, error: Exception | None = None, aggs=None):  # noqa: ANN001
        self._orders = orders or []
        self._error = error
        self._aggs = aggs or {}

    def fetch_daily_orders_snapshot(self, report_date: date, timezone_name: str):  # noqa: ANN001
        if self._error:
            raise self._error
        return {
            "orders": self._orders,
            "aggs": self._aggs,
        }

    def fetch_all_orders_for_date(self, report_date: date, timezone_name: str):  # noqa: ANN001
        if self._error:
            raise self._error
        return self._orders


class _FakeMetaClient:
    def __init__(self, summary=None, error: Exception | None = None):  # noqa: ANN001
        self._summary = summary or {}
        self._error = error

    def get_daily_spend(self, report_date: date, timezone_name: str):  # noqa: ANN001
        if self._error:
            raise self._error
        return self._summary


def _dummy_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        telegram_bot_token="dummy",
        telegram_allowed_user_id=1,
        meta_access_token="dummy",
        meta_page_access_token="page_dummy",
        meta_ad_account_id="act_1",
        meta_page_id="61581440236157",
        meta_api_version="v21.0",
        app_timezone="Asia/Ho_Chi_Minh",
        app_currency="VND",
        retry_max=3,
        retry_backoff_seconds=[1, 2, 3],
        token_healthcheck_enabled=True,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=True,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="api_key_dummy",
        pancake_access_token="",
        pancake_shop_id=123,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


def test_generate_report_success_and_cleanup_old_file(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    old_file = settings.reports_daily_dir / "report_2025-12-31.json"
    dump_json(old_file, {"ok": True})

    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "o1",
                "order_currency": "THB",
                "total_price": 120_000,
                "items": [
                    {
                        "quantity": 2,
                        "variation_id": "v1",
                        "variation_info": {
                            "name": "Sandal A",
                            "retail_price": 30_000,
                            "product_id": "p1",
                            "display_id": "SKU1",
                        },
                    },
                    {
                        "quantity": 1,
                        "variation_id": "v2",
                        "variation_info": {
                            "name": "Heel B",
                            "retail_price": 60_000,
                            "product_id": "p2",
                            "display_id": "SKU2",
                        },
                    },
                ],
            }
        ]
    )
    meta = _FakeMetaClient(
        summary={
            "report_date": "2026-05-15",
            "spend_vnd": 300_000,
            "currency": "VND",
        }
    )

    service = DailyReportService(settings=settings, logger=logging.getLogger("test"), pancake_client=pancake, meta_client=meta)
    report = service.generate_report(date(2026, 5, 15))

    assert report["ok"] is True
    assert report["pos"]["revenue_total_thb"] == 1_200
    assert report["pos"]["revenue_total_vnd"] == 978_000
    assert report["pos"]["thb_minor_unit_factor"] == 100
    assert report["ads"]["spend_vnd"] == 300_000
    assert report["roas"] == 3.26
    assert report["top_products"][0]["name"] == "Sandal A"
    assert report["top_products"][0]["revenue_thb"] == 600
    assert (settings.reports_daily_dir / "report_2026-05-15.json").exists()
    assert not old_file.exists()


def test_generate_report_partial_when_ads_fail(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "o1",
                "order_currency": "THB",
                "total_price": 50_000,
                "items": [],
            }
        ]
    )
    meta = _FakeMetaClient(error=RuntimeError("Meta timeout"))
    service = DailyReportService(settings=settings, logger=logging.getLogger("test"), pancake_client=pancake, meta_client=meta)
    report = service.generate_report(date(2026, 5, 15))

    assert report["ok"] is False
    assert report["partial"] is True
    assert report["pos"]["revenue_total_thb"] == 500
    assert report["pos"]["revenue_total_vnd"] == 407_500
    assert report["ads"] is None
    text = service.build_message(report, trigger_label="Báo cáo thủ công")
    assert "CẢNH BÁO" in text
    assert "Doanh thu POS: 500 THB" in text
    assert "Quy đổi VND: 407,500 VND" in text
    assert "Chi phí Ads: chưa có dữ liệu." in text


def test_generate_report_prefers_aggs_revenue_over_order_total(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "o1",
                "order_currency": "THB",
                "total_price": 340_000,
                "items": [],
            },
            {
                "id": "o2",
                "order_currency": "THB",
                "total_price": 250_000,
                "items": [],
            },
            {
                "id": "o3",
                "order_currency": "THB",
                "total_price": 410_000,
                "items": [],
            },
        ],
        aggs={
            "cod": {"value": 410_000.0},
            "prepaid": {"value": 250_000.0},
        },
    )
    meta = _FakeMetaClient(summary={"spend_vnd": 300_000, "currency": "VND"})
    service = DailyReportService(settings=settings, logger=logging.getLogger("test"), pancake_client=pancake, meta_client=meta)
    report = service.generate_report(date(2026, 5, 22))

    assert report["ok"] is True
    assert report["pos"]["revenue_total_thb"] == 6_600
    assert report["pos"]["revenue_total_vnd"] == 5_379_000
    assert report["pos"]["order_count"] == 3
