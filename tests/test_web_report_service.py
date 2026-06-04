from datetime import date
import logging
from pathlib import Path

from app.settings import Settings
from app.utils import dump_json
from app.web_report_service import WebReportService


class _FakePancakeClient:
    def __init__(self, orders: list[dict], aggs: dict | None = None, details: dict[str, dict] | None = None):
        self._orders = orders
        self._aggs = aggs or {}
        self._details = details or {}
        self.fetch_count = 0
        self.detail_calls: list[str] = []

    def fetch_all_orders_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        self.fetch_count += 1
        return self._orders

    def fetch_orders_snapshot_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        self.fetch_count += 1
        return {
            "orders": self._orders,
            "aggs": self._aggs,
        }

    def get_order_detail(self, order_id: str):  # noqa: ANN001
        self.detail_calls.append(str(order_id))
        return self._details.get(str(order_id), {})


class _FakeMetaClient:
    def __init__(self, spend_vnd: int = 0, *, error: Exception | None = None):
        self.spend_vnd = spend_vnd
        self.error = error
        self.calls: list[tuple[date, date, str]] = []

    def get_spend_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        self.calls.append((start_date, end_date, timezone_name))
        if self.error is not None:
            raise self.error
        return {"spend_vnd": self.spend_vnd}


class _FakeThaiDuongClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.calls: list[dict] = []

    def fetch_orders_for_sync(self, endpoint_cfg: dict, *, search_text: str = "", extra_filters: dict | None = None):
        self.calls.append(
            {
                "endpoint_cfg": endpoint_cfg,
                "search_text": search_text,
                "extra_filters": extra_filters or {},
            }
        )
        return self._rows


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
    assert snapshot["metrics"]["shipping_orders"] == 0
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
                    "pancake_display_id": "JC-C1",
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
    assert snapshot["metrics"]["shipping_orders"] == 0
    assert snapshot["metrics"]["reconcile_received_orders"] == 1
    assert snapshot["metrics"]["pending_reconcile_orders"] == 1
    assert len(snapshot["status_lists"]["returning"]) == 1
    assert len(snapshot["status_lists"]["reconcile-received"]) == 1


def test_returning_excludes_partial_return_status_15(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-R-PARTIAL",
                    "status": 15,
                    "total_price": 110_000,
                    "inserted_at": "2026-06-01T09:00:00+07:00",
                    "items": [],
                },
                {
                    "display_id": "JC-R-FULL",
                    "status": 5,
                    "total_price": 120_000,
                    "inserted_at": "2026-06-01T10:00:00+07:00",
                    "items": [],
                },
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["returning_orders"] == 1
    assert len(snapshot["status_lists"]["returning"]) == 1
    assert snapshot["status_lists"]["returning"][0]["order_ref"] == "JC-R-FULL"


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
    assert "THB" in snapshot["metrics"]["revenue_total_text"]
    assert "VNĐ" in snapshot["metrics"]["revenue_total_text"]


def test_snapshot_includes_ads_spend_for_selected_range(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    meta = _FakeMetaClient(spend_vnd=1_630_000)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
        meta_client=meta,
    )

    snapshot = service.get_snapshot(date(2026, 5, 1), date(2026, 5, 31))

    assert snapshot["metrics"]["ads_spend_vnd"] == 1_630_000
    assert snapshot["metrics"]["ads_spend_vnd_text"] == "1,630,000"
    assert snapshot["metrics"]["roas"] == 0.0
    assert snapshot["metrics"]["roas_text"] == "0.00x"
    assert meta.calls == [(date(2026, 5, 1), date(2026, 5, 31), "Asia/Ho_Chi_Minh")]


def test_snapshot_calculates_roas_from_vnd_revenue_and_ads_spend(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [],
            aggs={
                "cod": {"value": 500_000},
                "prepaid": {"value": 500_000},
            },
        ),
        meta_client=_FakeMetaClient(spend_vnd=1_630_000),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["revenue_total_vnd"] == 8_150_000
    assert snapshot["metrics"]["roas"] == 5.0
    assert snapshot["metrics"]["roas_text"] == "5.00x"


def test_snapshot_ads_spend_failure_falls_back_to_zero(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
        meta_client=_FakeMetaClient(error=RuntimeError("meta down")),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["ads_spend_vnd"] == 0
    assert snapshot["metrics"]["ads_spend_vnd_text"] == "0"


def test_shipping_status_metric_and_list(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-SHIP",
                    "status": 2,
                    "total_price": 350_000,
                    "items": [],
                },
                {
                    "display_id": "JC-WAIT",
                    "status": 11,
                    "total_price": 120_000,
                    "items": [],
                },
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["shipping_orders"] == 1
    assert len(snapshot["status_lists"]["shipping"]) == 1
    assert snapshot["status_lists"]["shipping"][0]["order_ref"] == "JC-SHIP"


def test_reconcile_received_td_status_only_mode_counts_td_rows(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
        {
            "reconcile_received_mode": "td_status_only",
            "reconcile_received_td_statuses": ["SUCCESS"],
            "pending_reconcile_match_results": ["not_found", "ambiguous", "unmapped_status"],
            "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT901",
                },
                {
                    "match_result": "ambiguous",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT902",
                },
            ],
        },
    )

    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
    )
    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["reconcile_received_orders"] == 2
    assert snapshot["metrics"]["pending_reconcile_orders"] == 1


def test_reconcile_received_includes_success_rows_without_pancake_ref(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
        {
            "reconcile_received_mode": "td_status_only",
            "reconcile_received_td_statuses": ["SUCCESS"],
            "pending_reconcile_match_results": [],
            "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "already_correct",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT901",
                    "td_awb": "AWB-MAPPED",
                    "td_cod_minor": 550_000,
                },
                {
                    "match_result": "not_found",
                    "td_status": "SUCCESS",
                    "td_awb": "AWB-NO-PANCAKE",
                    "td_cod_minor": 1_200_000,
                },
                {
                    "match_result": "ambiguous",
                    "td_status": "SUCCESS",
                    "td_awb": "AWB-ZERO-COD",
                    "td_cod_minor": 0,
                },
                {
                    "match_result": "already_correct",
                    "td_status": "RETURNED",
                    "td_awb": "AWB-RETURNED",
                    "td_cod_minor": 990_000,
                },
            ],
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))
    rows = snapshot["status_lists"]["reconcile-received"]

    assert snapshot["metrics"]["reconcile_received_orders"] == 3
    assert snapshot["metrics"]["reconcile_received_value_minor"] == 1_750_000
    assert snapshot["metrics"]["pending_reconcile_orders"] == 0
    assert {row["display_ref"] for row in rows} == {"JCT901", "AWB-NO-PANCAKE", "AWB-ZERO-COD"}


def test_pending_reconcile_requires_cashflow_missing_and_pancake_status_unmapped(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
        {
            "pending_reconcile_mode": "td_success_not_in_cashflow",
            "pending_reconcile_td_success_statuses": ["SUCCESS", "BEING_RETURNED"],
            "pending_reconcile_pancake_status_codes": [2],
            "pending_reconcile_td_to_pancake_status_codes": {
                "SUCCESS": [3],
                "BEING_RETURNED": [3, 4, 5],
                "RETURNED": [3, 4, 5],
            },
            "brand_rules": [],
        },
    )
    settings = _dummy_settings(tmp_path, web_report_status_map_path=str(status_map_path))
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 350_000,
                    "pancake_display_id": "JCT-SUCCESS-PENDING",
                    "pancake_status": 2,
                    "reason": "Chưa lên dòng tiền và Pancake chưa đổi trạng thái.",
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "BEING_RETURNED",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 240_000,
                    "pancake_display_id": "JCT-RETURNING-PENDING",
                    "pancake_status": 2,
                    "reason": "Đơn đang hoàn nhưng Pancake vẫn đang gửi.",
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 180_000,
                    "pancake_display_id": "JCT-NOT-SHIPPING",
                    "pancake_status": 11,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 350_000,
                    "td_cod_minor": 350_000,
                    "pancake_display_id": "JCT-CASHFLOW-DONE",
                    "pancake_status": 11,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 350_000,
                    "pancake_display_id": "JCT-STATUS-ALIGNED",
                    "pancake_status": 3,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "RETURNED",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 240_000,
                    "pancake_display_id": "JCT-RETURNED-SKIPPED",
                    "pancake_status": 2,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "RETURNED",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 240_000,
                    "pancake_display_id": "JCT-RETURN-ALIGNED",
                    "pancake_status": 4,
                },
                {
                    "match_result": "not_found",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 180_000,
                    "pancake_display_id": "",
                    "pancake_status": None,
                },
            ],
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))
    pending_refs = {row["pancake_order_ref"] for row in snapshot["status_lists"]["pending-reconcile"]}

    assert snapshot["metrics"]["pending_reconcile_orders"] == 2
    assert snapshot["metrics"]["pending_reconcile_value_minor"] == 590_000
    assert pending_refs == {"JCT-SUCCESS-PENDING", "JCT-RETURNING-PENDING"}


def test_pending_reconcile_uses_current_pancake_status_for_stale_reconcile_runs(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
            {
                "pending_reconcile_mode": "td_success_not_in_cashflow",
                "pending_reconcile_td_success_statuses": ["RETURNED"],
                "pending_reconcile_pancake_status_codes": [2],
                "pending_reconcile_td_to_pancake_status_codes": {"RETURNED": [3, 4, 5]},
                "brand_rules": [],
            },
    )
    settings = _dummy_settings(tmp_path, web_report_status_map_path=str(status_map_path))
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "RETURNED",
                    "td_sheet_cod_minor": 0,
                    "td_cod_minor": 445_000,
                    "pancake_order_id": "270229017331761",
                    "pancake_display_id": "JCT289",
                    # Stale status from an older reconcile run.
                    "pancake_status": 2,
                }
            ],
        },
    )
    fake_pancake = _FakePancakeClient(
        [],
        details={
            "270229017331761": {
                "id": "270229017331761",
                "display_id": "JCT289",
                "status": 4,
            }
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=fake_pancake,
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["pending_reconcile_orders"] == 0
    assert fake_pancake.detail_calls == ["270229017331761"]


def test_pending_reconcile_uses_thai_duong_order_list_and_pancake_shipping_status(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    dump_json(
        settings.pancake_td_sync_config_path,
        {
            "thai_duong": {
                "order_lookup_endpoint": {
                    "method": "POST",
                    "path": "/api/v1/orders/list",
                    "result_path": "data.data",
                },
                "order_lookup_filters": {"partnerCode": "THA356"},
            }
        },
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-02_20260602T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-02",
            "generated_at": "2026-06-02T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_awb": "AWB-CASHFLOW",
                    "pancake_order_id": "p_cashflow",
                    "td_sheet_cod_minor": 350_000,
                }
            ],
        },
    )
    fake_td = _FakeThaiDuongClient(
        [
            {
                "orderUID": "THA356_PENDING_SUCCESS",
                "pancakeOrderId": "p_success",
                "shippingOrderCode": "AWB-PENDING-SUCCESS",
                "shippingOrderStatus": "SUCCESS",
                "createdAt": "2026-06-01T09:00:00+07:00",
                "buyerName": "Customer Success",
                "cod": 3500,
                "codPaymentDate": None,
                "codStatus": "COD_MONNEY_COLLECTED",
            },
            {
                "orderUID": "THA356_PENDING_RETURN",
                "pancakeOrderId": "p_return",
                "shippingOrderCode": "AWB-PENDING-RETURN",
                "shippingOrderStatus": "RETURNED",
                "createdAt": "2026-06-01T10:00:00+07:00",
                "buyerName": "Customer Return",
                "cod": 2400,
                "codPaymentDate": None,
                "codStatus": "COD_MONNEY_COLLECTED",
            },
            {
                "orderUID": "THA356_ALREADY_IN_CASHFLOW",
                "pancakeOrderId": "p_cashflow",
                "shippingOrderCode": "AWB-CASHFLOW",
                "shippingOrderStatus": "SUCCESS",
                "createdAt": "2026-06-01T11:00:00+07:00",
                "cod": 3500,
            },
            {
                "orderUID": "THA356_PANCAKE_RECEIVED",
                "pancakeOrderId": "p_received",
                "shippingOrderCode": "AWB-RECEIVED",
                "shippingOrderStatus": "SUCCESS",
                "createdAt": "2026-06-01T12:00:00+07:00",
                "cod": 1800,
            },
            {
                "orderUID": "THA356_PAID_TO_SENDER",
                "pancakeOrderId": "p_paid",
                "shippingOrderCode": "AWB-PAID",
                "shippingOrderStatus": "SUCCESS",
                "createdAt": "2026-06-01T13:00:00+07:00",
                "cod": 1800,
                "codStatus": "PAID_TO_SENDER",
            },
            {
                "orderUID": "THA356_STILL_SHIPPING_TD",
                "pancakeOrderId": "p_td_shipping",
                "shippingOrderCode": "AWB-TD-SHIPPING",
                "shippingOrderStatus": "SHIPPING",
                "createdAt": "2026-06-01T14:00:00+07:00",
                "cod": 1800,
            },
            {
                "orderUID": "THA356_OUTSIDE_PERIOD",
                "pancakeOrderId": "p_outside",
                "shippingOrderCode": "AWB-OUTSIDE",
                "shippingOrderStatus": "SUCCESS",
                "createdAt": "2026-06-02T09:00:00+07:00",
                "cod": 1800,
            },
        ]
    )
    fake_pancake = _FakePancakeClient(
        [],
        details={
            "p_success": {"id": "p_success", "status": 2},
            "p_return": {"id": "p_return", "status": 2},
            "p_cashflow": {"id": "p_cashflow", "status": 2},
            "p_received": {"id": "p_received", "status": 3},
            "p_paid": {"id": "p_paid", "status": 2},
            "p_td_shipping": {"id": "p_td_shipping", "status": 2},
            "p_outside": {"id": "p_outside", "status": 2},
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=fake_pancake,
        thai_duong_client=fake_td,  # type: ignore[arg-type]
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))
    rows = snapshot["status_lists"]["pending-reconcile"]

    assert snapshot["metrics"]["pending_reconcile_orders"] == 2
    assert snapshot["metrics"]["pending_reconcile_value_minor"] == 590_000
    assert {row["display_ref"] for row in rows} == {"THA356_PENDING_SUCCESS", "THA356_PENDING_RETURN"}
    assert fake_td.calls[0]["extra_filters"] == {"partnerCode": "THA356"}
    assert fake_pancake.detail_calls == ["p_success", "p_return", "p_received"]


def test_pending_reconcile_uses_td_success_not_in_cashflow_mode(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
            {
                "pending_reconcile_mode": "td_success_not_in_cashflow",
                "pending_reconcile_td_success_statuses": ["SUCCESS", "BEING_RETURNED", "RETURNED"],
                "pending_reconcile_pancake_status_codes": [2],
                "reconcile_received_mode": "td_status_only",
                "reconcile_received_td_statuses": ["SUCCESS", "BEING_RETURNED", "RETURNED"],
                "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "already_correct",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1001",
                    "pancake_status": 3,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1002",
                    "pancake_status": 2,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "RETURNED",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1003",
                    "pancake_status": 5,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "BEING_RETURNED",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1004",
                    "pancake_status": 4,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "RETURNED",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1005",
                    "pancake_status": 3,
                },
                {
                    "match_result": "not_found",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JCT1006",
                    "pancake_status": 3,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 500_000,
                    "pancake_display_id": "JCT1007",
                    "pancake_status": 2,
                },
            ],
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["reconcile_received_orders"] == 7
    assert snapshot["metrics"]["pending_reconcile_orders"] == 1
    pending_refs = {row["pancake_order_ref"] for row in snapshot["status_lists"]["pending-reconcile"]}
    assert pending_refs == {"JCT1002"}


def test_pending_reconcile_skips_legacy_rows_without_cashflow_signal(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
            {
                "pending_reconcile_mode": "td_success_not_in_cashflow",
                "pending_reconcile_td_success_statuses": ["SUCCESS"],
                "pending_reconcile_pancake_status_codes": [2],
                "pending_reconcile_td_to_pancake_status_codes": {"SUCCESS": [3]},
                "reconcile_received_mode": "td_status_only",
                "reconcile_received_td_statuses": ["SUCCESS"],
                "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "pancake_display_id": "JCT2001",
                    "pancake_status": 5,
                    # Legacy run: no td_sheet_cod_minor field
                }
            ],
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient([]),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))

    assert snapshot["metrics"]["pending_reconcile_orders"] == 0


def test_status_value_metrics_include_shipping_returning_and_reconcile(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    status_map_path = config_root / "custom_status_map.json"
    dump_json(
        status_map_path,
            {
                "pending_reconcile_mode": "td_success_not_in_cashflow",
                "pending_reconcile_td_success_statuses": ["SUCCESS"],
                "pending_reconcile_pancake_status_codes": [2],
                "pending_reconcile_td_to_pancake_status_codes": {"SUCCESS": [3]},
                "reconcile_received_mode": "td_status_only",
                "reconcile_received_td_statuses": ["SUCCESS"],
                "brand_rules": [],
        },
    )
    settings = _dummy_settings(
        tmp_path,
        web_report_status_map_path=str(status_map_path),
    )
    run_path = settings.reconcile_cod_runs_dir / "run_2026-06-01_20260601T030000Z.json"
    dump_json(
        run_path,
        {
            "settlement_date": "2026-06-01",
            "generated_at": "2026-06-01T03:00:00+00:00",
            "records": [
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JC-SHIP",
                    "pancake_status": 2,
                },
                {
                    "match_result": "matched_unique",
                    "td_status": "SUCCESS",
                    "td_sheet_cod_minor": 0,
                    "pancake_display_id": "JC-WAIT",
                    "pancake_status": 2,
                },
            ],
        },
    )
    service = WebReportService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=_FakePancakeClient(
            [
                {
                    "display_id": "JC-SHIP",
                    "status": 2,
                    "total_price": 350_000,
                    "items": [],
                },
                {
                    "display_id": "JC-WAIT",
                    "status": 11,
                    "total_price": 220_000,
                    "items": [],
                },
                {
                    "display_id": "JC-RET",
                    "status": 4,
                    "total_price": 180_000,
                    "items": [],
                },
            ]
        ),
    )

    snapshot = service.get_snapshot(date(2026, 6, 1))
    metrics = snapshot["metrics"]

    assert metrics["shipping_orders"] == 1
    assert metrics["shipping_value_minor"] == 350_000
    assert metrics["returning_orders"] == 1
    assert metrics["returning_value_minor"] == 180_000
    assert metrics["reconcile_received_orders"] == 2
    assert metrics["reconcile_received_value_minor"] == 570_000
    assert metrics["pending_reconcile_orders"] == 1
    assert metrics["pending_reconcile_value_minor"] == 350_000
