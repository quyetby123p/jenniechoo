from datetime import date
from pathlib import Path

from app.settings import Settings
from app.web_report_app import create_app


class _StubReportService:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[date] = []

    def get_snapshot(self, report_date: date):  # noqa: ANN001
        self.calls.append(report_date)
        return self.payload


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
        "timezone": "Asia/Ho_Chi_Minh",
        "metrics": {
            "total_orders": 2,
            "closed_orders": 2,
            "waiting_orders": 1,
            "pending_reconcile_orders": 1,
            "missing_line_count": 1,
            "missing_quantity": 1,
            "missing_product_count": 1,
            "waiting_value_text": "1.200đ",
        },
        "size_summary": [{"size": "M", "quantity": 1}],
        "brands": [
            {
                "brand_name": "Jennie Choo",
                "brand_slug": "jennie-choo",
                "total_orders": 2,
                "waiting_orders": 1,
                "missing_line_count": 1,
                "missing_quantity": 1,
                "size_summary": [{"size": "M", "quantity": 1}],
                "waiting_value_text": "1.200đ",
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
                        "value_text": "1.200đ",
                        "order_refs": ["JC001"],
                    }
                ],
                "waiting_value_text": "1.200đ",
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
                    "order_total_text": "1.200đ",
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
    assert client.get("/status/pending-reconcile?date=2026-06-01").status_code == 200
    assert client.get("/api/v1/snapshot?date=2026-06-01").status_code == 200


def test_unknown_brand_and_status_return_404(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    app = create_app(settings=settings, report_service=_StubReportService(_snapshot_payload()))
    app.testing = True
    client = app.test_client()

    assert client.get("/brand/unknown?date=2026-06-01").status_code == 404
    assert client.get("/status/unknown?date=2026-06-01").status_code == 404
