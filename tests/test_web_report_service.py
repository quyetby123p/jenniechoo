from datetime import date
import logging
from pathlib import Path

from app.settings import Settings
from app.utils import dump_json
from app.web_report_service import WebReportService


class _FakePancakeClient:
    def __init__(self, orders: list[dict], aggs: dict | None = None):
        self._orders = orders
        self._aggs = aggs or {}
        self.fetch_count = 0

    def fetch_all_orders_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        self.fetch_count += 1
        return self._orders

    def fetch_orders_snapshot_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        self.fetch_count += 1
        return {
            "orders": self._orders,
            "aggs": self._aggs,
        }


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


def test_snapshot_aggregates_orders_waiting_and_pending_reconcile(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "not_found",
                    "pancake_display_id": "JCT111",
                    "reason": "not found",
                },
                {
                    "match_result": "already_correct",
                    "pancake_display_id": "JCT112",
                },
            ],
        },
    )

    orders = [
        {
            "display_id": "JC1001",
            "status_name": "Chờ hàng",
            "total_price": 300_000,
            "inserted_at": "2026-06-01T09:10:00+07:00",
            "items": [
                {
                    "quantity": 1,
                    "variation_info": {
                        "product_id": "JC-A-100",
                        "name": "JC-A-100 - Đen - M",
                        "retail_price": 100_000,
                    },
                },
                {
                    "quantity": 2,
                    "variation_info": {
                        "product_id": "JC-A-101",
                        "name": "JC-A-101 - Trắng - L",
                        "retail_price": 100_000,
                    },
                },
            ],
        },
        {
            "display_id": "SA2002",
            "status_name": "Chờ hàng",
            "total_price": 100_000,
            "inserted_at": "2026-06-01T10:00:00+07:00",
            "items": [
                {
                    "quantity": 1,
                    "variation_info": {
                        "product_id": "SA-A-10",
                        "name": "SA-A-10 - Be - S",
                        "retail_price": 100_000,
                    },
                }
            ],
        },
        {
            "display_id": "JC1003",
            "status_name": "Đã nhận",
            "total_price": 200_000,
            "inserted_at": "2026-06-01T11:00:00+07:00",
            "items": [],
        },
    ]
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(orders),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["total_orders"] == 3
    assert snapshot["metrics"]["closed_orders"] == 1
    assert snapshot["metrics"]["waiting_orders"] == 2
    assert snapshot["metrics"]["returning_orders"] == 0
    assert snapshot["metrics"]["reconcile_received_orders"] == 0
    assert snapshot["metrics"]["pending_reconcile_orders"] == 1
    assert snapshot["metrics"]["missing_line_count"] == 3
    assert snapshot["metrics"]["missing_quantity"] == 4
    assert snapshot["metrics"]["missing_product_count"] == 3
    assert snapshot["period"]["is_single_day"] is True
    assert len(snapshot["status_lists"]["waiting"]) == 2
    assert len(snapshot["status_lists"]["pending-reconcile"]) == 1
    assert any(brand["brand_slug"] == "jennie-choo" for brand in snapshot["brands"])
    assert any(brand["brand_slug"] == "say-studios" for brand in snapshot["brands"])


def test_snapshot_supports_date_range_and_new_metrics(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    run_path_1 = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    run_path_2 = settings.reconcile_cod_runs_dir / "run_2026-06-02_20260602T030000Z.json"
    dump_json(
        run_path_1,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT301",
                    "td_awb": "AWB1",
                }
            ],
        },
    )
    dump_json(
        run_path_2,
        {
            "settlement_date": "2026-06-02",
            "generated_at": "2026-06-02T03:00:00+00:00",
            "records": [
                {
                    "match_result": "ambiguous",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT302",
                    "td_awb": "AWB2",
                }
            ],
        },
    )
    orders = [
        {
            "display_id": "JC-R1",
            "status_name": "Đang hoàn",
            "total_price": 110_000,
            "inserted_at": "2026-06-02T09:00:00+07:00",
            "items": [],
        },
        {
            "display_id": "JC-C1",
            "status_name": "Đã nhận",
            "total_price": 120_000,
            "inserted_at": "2026-06-01T09:00:00+07:00",
            "items": [],
        },
    ]
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(orders),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1), date(2026, 6, 2))

    assert snapshot["period"]["start_date"] == "2026-06-01"
    assert snapshot["period"]["end_date"] == "2026-06-02"
    assert snapshot["period"]["is_single_day"] is False
    assert snapshot["metrics"]["total_orders"] == 2
    assert snapshot["metrics"]["returning_orders"] == 1
    assert snapshot["metrics"]["reconcile_received_orders"] == 1
    assert snapshot["metrics"]["pending_reconcile_orders"] == 1
    assert len(snapshot["status_lists"]["returning"]) == 1
    assert len(snapshot["status_lists"]["reconcile-received"]) == 1


def test_snapshot_cache_uses_ttl(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, web_report_refresh_seconds=600)
    fake = _FakePancakeClient(
        [
            {
                "display_id": "JC1001",
                "status_name": "Chờ hàng",
                "total_price": 100_000,
                "items": [],
            }
        ]
    )
    service = WebReportService(settings=settings, logger=logging.getLogger("test"), pancake_client=fake)

    snapshot1 = service.get_snapshot(date(2026, 6, 1))
    snapshot2 = service.get_snapshot(date(2026, 6, 1))

    assert snapshot1 == snapshot2
    assert fake.fetch_count == 1


def test_waiting_status_code_mapping_from_config(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
        {
            "waiting_status_codes": [13],
            "waiting_status_labels": [],
            "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JCX",
                    "status": 13,
                    "total_price": 120_000,
                    "items": [],
                }
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["waiting_orders"] == 1


def test_waiting_status_defaults_to_code_map_when_status_name_missing(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-WAIT",
                    "status": 11,
                    "total_price": 220_000,
                    "items": [],
                }
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["waiting_orders"] == 1


def test_size_uses_variation_fields_instead_of_size_object(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-SIZE",
                    "status": 11,
                    "total_price": 520_000,
                    "items": [
                        {
                            "quantity": 2,
                            "variation_info": {
                                "product_id": "JC-V-239",
                                "size": {
                                    "height": 0,
                                    "id": "abc",
                                    "length": 0,
                                    "width": 0,
                                },
                                "fields": [
                                    {"name": "MÀU", "value": "Nude Beige"},
                                    {"name": "SIZE", "value": "S"},
                                ],
                            },
                        }
                    ],
                }
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["size_summary"] == [{"size": "S", "quantity": 2}]
    row = snapshot["brand_detail"]["jennie-choo"]["sku_rows"][0]
    assert row["sizes"] == {"S": 2}


def test_revenue_total_prefers_aggs_snapshot_values(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-1",
                    "status": 3,
                    "total_price": 900_000,
                    "items": [],
                },
                {
                    "display_id": "JC-2",
                    "status": 11,
                    "total_price": 100_000,
                    "items": [],
                },
            ],
            aggs={
                "cod": {"value": 500_000},
                "prepaid": {"value": 300_000},
            },
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["revenue_total_minor"] == 800_000
