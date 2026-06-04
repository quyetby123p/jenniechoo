from datetime import date
from pathlib import Path

from app.settings import Settings
from app.web_report_app import create_app


class _StubReportService:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[date, date]] = []

    def get_snapshot(self, start_date: date, end_date: date | None = None):  # noqa: ANN001
        self.calls.append((start_date, end_date or start_date))
        return self.payload


class _RangeAwareStubReportService:
    def __init__(self, *, today_payload: dict, range_payload: dict):
        self.today_payload = today_payload
        self.range_payload = range_payload
        self.calls: list[tuple[date, date]] = []

    def get_snapshot(self, start_date: date, end_date: date | None = None):  # noqa: ANN001
        effective_end = end_date or start_date
        self.calls.append((start_date, effective_end))
        if start_date == effective_end:
            return self.today_payload
        return self.range_payload


def _dummy_settings(tmp_path: Path) -> Settings:
    project_root = Path(__file__).resolve().parents[1]
    return Settings(
        project_root=project_root,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        config_root=project_root / "config",
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


def _snapshot_payload() -> dict:
    return {
        "report_date": "2026-06-01",
        "period": {
            "start_date": "2026-06-01",
            "end_date": "2026-06-01",
            "is_single_day": True,
            "label": "01-06-2026",
        },
        "timezone": "Asia/Ho_Chi_Minh",
        "currency": {
            "base": "THB",
            "quote": "VND",
            "rate": 810.0,
            "minor_unit_factor": 100,
        },
        "metrics": {
            "total_orders": 2,
            "closed_orders": 2,
            "revenue_total_text": "2,200 THB (~ 1,782,000 VNĐ)",
            "revenue_total_thb_text": "2,200",
            "revenue_total_vnd_text": "1,782,000",
            "ads_spend_vnd": 300_000,
            "ads_spend_vnd_text": "300,000",
            "roas": 5.94,
            "roas_text": "5.94x",
            "waiting_orders": 1,
            "waiting_value_thb_text": "1,200",
            "waiting_value_vnd_text": "972,000",
            "shipping_orders": 1,
            "shipping_value_thb_text": "1,000",
            "shipping_value_vnd_text": "810,000",
            "returning_orders": 0,
            "returning_value_thb_text": "0",
            "returning_value_vnd_text": "0",
            "reconcile_received_orders": 1,
            "reconcile_received_value_thb_text": "2,200",
            "reconcile_received_value_vnd_text": "1,782,000",
            "pending_reconcile_orders": 1,
            "pending_reconcile_value_thb_text": "1,200",
            "pending_reconcile_value_vnd_text": "972,000",
            "missing_line_count": 1,
            "missing_quantity": 1,
            "missing_product_count": 1,
            "waiting_value_text": "1,200 THB (~ 972,000 VNĐ)",
        },
        "size_summary": [{"size": "M", "quantity": 1}],
        "brands": [
            {
                "brand_name": "Jennie Choo",
                "brand_slug": "jennie-choo",
                "total_orders": 2,
                "total_value_text": "2,200 THB (~ 1,782,000 VNĐ)",
                "waiting_orders": 1,
                "missing_line_count": 1,
                "missing_quantity": 1,
                "size_summary": [{"size": "M", "quantity": 1}],
                "waiting_value_text": "1,200 THB (~ 972,000 VNĐ)",
            }
        ],
        "brand_detail": {
            "jennie-choo": {
                "brand_name": "Jennie Choo",
                "brand_slug": "jennie-choo",
                "total_orders": 2,
                "waiting_orders": 1,
                "size_summary": [{"size": "M", "quantity": 1}],
                "sku_rows": [
                    {
                        "sku": "JC-A-100",
                        "color": "Đen",
                        "sizes": {"M": 1},
                        "missing_line_count": 1,
                        "missing_quantity": 1,
                        "value_text": "1,200 THB (~ 972,000 VNĐ)",
                        "order_refs": ["JC001"],
                    }
                ],
                "waiting_value_text": "1,200 THB (~ 972,000 VNĐ)",
            }
        },
        "status_lists": {
            "waiting": [
                {
                    "order_ref": "JC001",
                    "brand_name": "Jennie Choo",
                    "created_at": "09:00 01-06-2026",
                    "status_label": "Chờ hàng",
                    "missing_skus": ["JC-A-100"],
                    "item_count": 1,
                    "order_total_text": "1,200 THB (~ 972,000 VNĐ)",
                }
            ],
            "shipping": [
                {
                    "order_ref": "JC002",
                    "brand_name": "Jennie Choo",
                    "created_at": "10:00 01-06-2026",
                    "status_label": "Đã gửi hàng",
                    "order_total_text": "1,000 THB (~ 810,000 VNĐ)",
                }
            ],
            "pending-reconcile": [
                {
                    "pancake_order_ref": "JC001",
                    "match_result": "not_found",
                    "customer_name": "Test",
                    "td_awb": "AWB1",
                    "td_status": "SUCCESS",
                    "reason": "Not found in reconcile",
                }
            ],
            "reconcile-received": [
                {
                    "pancake_order_ref": "JC001",
                    "match_result": "matched_unique",
                    "customer_name": "Test",
                    "td_awb": "AWB1",
                    "td_status": "SUCCESS",
                    "settlement_date": "2026-06-01",
                    "reason": "",
                }
            ],
            "returning": [],
        },
    }


def test_routes_render_success(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = _StubReportService(_snapshot_payload())
    app = create_app(settings=settings, report_service=service)
    app.testing = True
    client = app.test_client()

    assert client.get("/healthz").status_code == 200
    assert client.get("/?date=2026-06-01").status_code == 200
    assert client.get("/brand/jennie-choo?date=2026-06-01").status_code == 200
    assert client.get("/status/waiting?date=2026-06-01").status_code == 200
    assert client.get("/status/shipping?date=2026-06-01").status_code == 200
    assert client.get("/status/pending-reconcile?date=2026-06-01").status_code == 200
    assert client.get("/status/reconcile-received?mode=today").status_code == 200
    assert client.get("/status/returning?mode=range&start_date=2026-06-01&end_date=2026-06-01").status_code == 200
    assert client.get("/api/v1/snapshot?date=2026-06-01").status_code == 200


def test_unknown_brand_and_status_return_404(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    app = create_app(settings=settings, report_service=_StubReportService(_snapshot_payload()))
    app.testing = True
    client = app.test_client()

    assert client.get("/brand/unknown?date=2026-06-01").status_code == 404
    assert client.get("/status/unknown?date=2026-06-01").status_code == 404


def test_dashboard_daily_revenue_uses_today_snapshot_not_selected_range(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    today_payload = _snapshot_payload()
    today_payload["metrics"]["revenue_total_thb_text"] = "7,400"
    today_payload["metrics"]["revenue_total_vnd_text"] = "5,994,000"
    today_payload["metrics"]["ads_spend_vnd_text"] = "300,000"
    today_payload["metrics"]["roas_text"] = "19.98x"
    range_payload = _snapshot_payload()
    range_payload["metrics"]["revenue_total_thb_text"] = "115,190"
    range_payload["metrics"]["revenue_total_vnd_text"] = "93,303,900"
    range_payload["metrics"]["ads_spend_vnd_text"] = "1,200,000"
    range_payload["metrics"]["roas_text"] = "77.75x"
    service = _RangeAwareStubReportService(today_payload=today_payload, range_payload=range_payload)
    app = create_app(settings=settings, report_service=service)
    app.testing = True
    client = app.test_client()

    response = client.get("/?mode=range&start_date=2026-05-26&end_date=2026-06-01")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "7,400 THB" in html
    assert "115,190 THB" in html
    assert "300,000 VNĐ" in html
    assert "1,200,000 VNĐ" in html
    assert "19.98x" in html
    assert "77.75x" in html
    assert any(start == date(2026, 2, 1) for start, end in service.calls if start != end)


def test_status_page_shows_total_orders_and_total_revenue_summary(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = _StubReportService(_snapshot_payload())
    app = create_app(settings=settings, report_service=service)
    app.testing = True
    client = app.test_client()

    response = client.get("/status/shipping?mode=today")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Tổng đơn" in html
    assert "Tổng doanh số" in html
    assert "1,000 THB" in html
    assert "~ 810,000 VNĐ" in html
    assert "Số dòng hiển thị" in html
    assert 'label text-danger">Tổng đơn' in html
    assert 'label text-danger">Tổng doanh số' in html


def test_dashboard_css_wraps_mobile_metric_values(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    app = create_app(settings=settings, report_service=_StubReportService(_snapshot_payload()))
    app.testing = True
    client = app.test_client()

    response = client.get("/?mode=today")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "overflow-wrap: anywhere" in html
    assert "@media (max-width: 575.98px)" in html
    assert ".kpi-grid-quick { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in html
