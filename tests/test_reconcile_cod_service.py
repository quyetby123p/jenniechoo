from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from typing import Any

from app.reconcile_cod_service import ReconcileCodService
from app.settings import Settings
from app.utils import dump_json


class _FakeThaiDuongClient:
    def __init__(self, history_rows: list[dict[str, Any]], detail_rows: list[dict[str, Any]]) -> None:
        self.history_rows = history_rows
        self.detail_rows = detail_rows
        self.requested_settlement_dates: list[date] = []

    def fetch_settlement_history(self, start_date: date, end_date: date):  # noqa: ANN001
        del start_date, end_date
        return list(self.history_rows), "api"

    def fetch_settlement_details(self, settlement_date: date, settlement=None):  # noqa: ANN001
        del settlement
        self.requested_settlement_dates.append(settlement_date)
        return list(self.detail_rows), "api"


class _FakePancakeClient:
    def __init__(
        self,
        orders: list[dict[str, Any]],
        *,
        fail_once: dict[tuple[str, int], Exception] | None = None,
    ) -> None:
        self.orders = orders
        self.update_calls: list[tuple[str, int, dict[str, Any]]] = []
        self.fail_once = dict(fail_once or {})

    def fetch_all_orders_for_range(self, start_date: date, end_date: date, timezone_name: str):  # noqa: ANN001
        del start_date, end_date, timezone_name
        return list(self.orders)

    def update_order_status(self, order_id: str, status: int, *, update_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        self.update_calls.append((order_id, status, update_cfg or {}))
        key = (str(order_id), int(status))
        if key in self.fail_once:
            error = self.fail_once.pop(key)
            raise error
        return {"success": True}


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
        token_healthcheck_enabled=False,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=False,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="",
        pancake_access_token="token_dummy",
        pancake_shop_id=123,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
        reconcile_cod_enabled=True,
        reconcile_cod_auto_enabled=False,
        reconcile_cod_hour=9,
        reconcile_cod_minute=30,
        reconcile_cod_batch_limit=100,
        reconcile_cod_update_enabled=False,
        reconcile_cod_status_map_path="config/reconcile_cod_status_map.json",
        reconcile_cod_pancake_lookback_days=3650,
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


def _write_reconcile_configs(settings: Settings, *, mapped_status: int | None = 2) -> None:
    match_cfg = {
        "thai_duong": {
            "settlement_date_paths": ["settlement_date"],
            "detail_settlement_date_paths": ["settlement_date"],
            "send_date_paths": ["send_date"],
            "awb_paths": ["awb"],
            "status_paths": ["status_text"],
            "phone_paths": ["phone"],
            "customer_name_paths": ["customer_name"],
            "amount_paths": ["cod"],
            "fee_paths": ["fee"],
            "conclusion_thb_paths": ["cod_remain", "cod"],
            "conclusion_vnd_paths": ["cod_vnd"],
            "exchange_rate_paths": ["exchange_rate"],
            "amount_minor_unit_factor": 100,
            "fee_minor_unit_factor": 100,
        },
        "pancake": {
            "awb_paths": ["third_party_id"],
            "phone_paths": ["bill_phone_number"],
            "customer_name_paths": ["bill_full_name"],
            "amount_paths": ["total_price"],
            "original_amount_paths": ["items[].variation_info.retail_price"],
            "status_paths": ["status"],
            "display_id_paths": ["custom_id", "display_id"],
            "order_id_paths": ["id"],
            "amount_minor_unit_factor": 1,
        },
    }
    dump_json(settings.reconcile_cod_match_config_path, match_cfg)
    dump_json(
        settings.reconcile_cod_status_map_config_path,
        {
            "enabled": True,
            "mapping": {
                "giao hang thanh cong": {"status": mapped_status},
            },
            "update_endpoint": {
                "method": "POST",
                "path": "/shops/{shop_id}/orders/{order_id}",
                "status_field": "status",
            },
        },
    )


def test_reconcile_cod_match_unique_by_awb(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH35028N4TCP6B",
                "status_text": "Giao hàng thành công",
                "phone": "0809199218",
                "customer_name": "May Foster Thirakon",
                "cod": "2800",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "180157094927073",
                "display_id": "327",
                "third_party_id": "TH35028N4TCP6B",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster Thirakon",
                "total_price": 280000,
                "status": 1,
            }
        ]
    )

    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))

    assert report["summary"]["matched_unique"] == 1
    assert report["summary"]["already_correct"] == 0
    assert report["summary"]["ambiguous"] == 0
    assert report["summary"]["not_found"] == 0
    assert report["summary"]["unmapped_status"] == 0
    assert Path(str(report["csv_path"])).exists()


def test_reconcile_cod_apply_updates_is_idempotent(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, reconcile_cod_update_enabled=True)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH35028N4TCP6B",
                "status_text": "Giao hàng thành công",
                "phone": "0809199218",
                "customer_name": "May Foster Thirakon",
                "cod": "2800",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "180157094927073",
                "display_id": "327",
                "third_party_id": "TH35028N4TCP6B",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster Thirakon",
                "total_price": 280000,
                "status": 1,
            }
        ]
    )

    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))
    run_id = str(report["run_id"])

    first = service.apply_updates(run_id)
    second = service.apply_updates(run_id)

    assert first["updated"] == 1
    assert first["failed"] == 0
    assert second["updated"] == 0
    assert len(pancake.update_calls) == 1


def test_reconcile_cod_apply_updates_auto_transition_from_printing_then_retry(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, reconcile_cod_update_enabled=True)
    _write_reconcile_configs(settings, mapped_status=3)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH35028N4TCP6B",
                "status_text": "Giao hàng thành công",
                "phone": "0809199218",
                "customer_name": "May Foster Thirakon",
                "cod": "2800",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "360300986571957",
                "display_id": "JCT315",
                "third_party_id": "TH35028N4TCP6B",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster Thirakon",
                "total_price": 280000,
                "status": 13,
            }
        ],
        fail_once={
            (
                "360300986571957",
                3,
            ): RuntimeError('Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm","success":false}'),
        },
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))
    run_id = str(report["run_id"])

    apply_summary = service.apply_updates(run_id)

    assert apply_summary["updated"] == 1
    assert apply_summary["failed"] == 0
    assert apply_summary["transitioned"] == 1
    assert apply_summary["failed_orders"] == []
    assert [call[1] for call in pancake.update_calls] == [3, 2, 3]


def test_reconcile_cod_auto_pick_latest_unprocessed_settlement_date(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=None)
    settings.reconcile_cod_runs_dir.mkdir(parents=True, exist_ok=True)
    dump_json(
        settings.reconcile_cod_runs_dir / "run_2026-05-09_20260510T000000Z.json",
        {"settlement_date": "2026-05-09"},
    )

    thai_duong = _FakeThaiDuongClient(
        history_rows=[
            {"settlement_date": "2026-05-09"},
            {"settlement_date": "2026-05-04"},
        ],
        detail_rows=[
            {
                "settlement_date": "2026-05-04",
                "awb": "TH0001",
                "status_text": "Giao hàng thành công",
                "phone": "0800000001",
                "customer_name": "A",
                "cod": "100",
            }
        ],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(None)
    assert report["settlement_date"] == "2026-05-04"
    assert thai_duong.requested_settlement_dates[-1] == date(2026, 5, 4)


def test_reconcile_cod_fallback_identity_when_awb_not_unique(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "send_date": "2026-05-08",
                "awb": "TH-DUP-01",
                "status_text": "Giao hàng thành công",
                "phone": "0809 199 218",
                "customer_name": "May Foster",
                "cod": "2800",
                "fee": "120",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "327",
                "third_party_id": "THDUP01",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster",
                "total_price": 280000,
                "status": 1,
            },
            {
                "id": "order-2",
                "display_id": "328",
                "third_party_id": "THDUP01",
                "bill_phone_number": "0809000000",
                "bill_full_name": "Nguoi Khac",
                "total_price": 99900,
                "status": 1,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["matched_unique"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity"
    assert record["pancake_order_id"] == "order-1"


def test_reconcile_cod_ambiguous_after_identity_fallback(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH_DUP_02",
                "status_text": "Giao hàng thành công",
                "phone": "0809199218",
                "customer_name": "May Foster",
                "cod": "2800",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "327",
                "third_party_id": "THDUP02",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster",
                "total_price": 280000,
                "status": 1,
            },
            {
                "id": "order-2",
                "display_id": "328",
                "third_party_id": "THDUP02",
                "bill_phone_number": "0809199218",
                "bill_full_name": "May Foster",
                "total_price": 280000,
                "status": 1,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["ambiguous"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity"
    assert record["match_result"] == "ambiguous"


def test_reconcile_cod_summary_counts_and_audit_columns(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "send_date": "2026-05-08",
                "awb": "A1",
                "status_text": "Giao hàng thành công",
                "phone": "0800000001",
                "customer_name": "Ten A",
                "cod": "100",
                "fee": "5",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "B1",
                "status_text": "Giao hàng thành công",
                "phone": "0800000002",
                "customer_name": "Ten B",
                "cod": "200",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "C-DUP",
                "status_text": "Giao hàng thành công",
                "phone": "0800000003",
                "customer_name": "Ten C",
                "cod": "300",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "D404",
                "status_text": "Giao hàng thành công",
                "phone": "0800000004",
                "customer_name": "Ten D",
                "cod": "400",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "E1",
                "status_text": "Dang van chuyen",
                "phone": "0800000005",
                "customer_name": "Ten E",
                "cod": "500",
            },
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-a",
                "display_id": "1",
                "third_party_id": "A1",
                "bill_phone_number": "0800000001",
                "bill_full_name": "Ten A",
                "total_price": 10000,
                "status": 1,
            },
            {
                "id": "order-b",
                "display_id": "2",
                "third_party_id": "B1",
                "bill_phone_number": "0800000002",
                "bill_full_name": "Ten B",
                "total_price": 20000,
                "status": 2,
            },
            {
                "id": "order-c1",
                "display_id": "3",
                "third_party_id": "CDUP",
                "bill_phone_number": "0800000003",
                "bill_full_name": "Ten C",
                "total_price": 30000,
                "status": 1,
            },
            {
                "id": "order-c2",
                "display_id": "4",
                "third_party_id": "CDUP",
                "bill_phone_number": "0800000003",
                "bill_full_name": "Ten C",
                "total_price": 30000,
                "status": 1,
            },
            {
                "id": "order-e",
                "display_id": "5",
                "third_party_id": "E1",
                "bill_phone_number": "0800000005",
                "bill_full_name": "Ten E",
                "total_price": 50000,
                "status": 1,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    summary = report["summary"]
    assert summary["matched_unique"] == 1
    assert summary["already_correct"] == 1
    assert summary["ambiguous"] == 1
    assert summary["not_found"] == 1
    assert summary["unmapped_status"] == 1
    assert summary["total"] == 5

    first_record = report["records"][0]
    assert "td_send_date" in first_record
    assert "td_detail_settlement_date" in first_record
    assert "td_fee_minor" in first_record
    assert "td_sheet_cod_minor" in first_record
    assert "match_tier" in first_record


def test_reconcile_cod_apply_updates_respects_batch_limit(tmp_path: Path) -> None:
    settings = _dummy_settings(
        tmp_path,
        reconcile_cod_update_enabled=True,
        reconcile_cod_batch_limit=1,
    )
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "A1",
                "status_text": "Giao hàng thành công",
                "phone": "0800000001",
                "customer_name": "Ten A",
                "cod": "100",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "B1",
                "status_text": "Giao hàng thành công",
                "phone": "0800000002",
                "customer_name": "Ten B",
                "cod": "200",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "C1",
                "status_text": "Giao hàng thành công",
                "phone": "0800000003",
                "customer_name": "Ten C",
                "cod": "300",
            },
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-a",
                "display_id": "1",
                "third_party_id": "A1",
                "bill_phone_number": "0800000001",
                "bill_full_name": "Ten A",
                "total_price": 10000,
                "status": 1,
            },
            {
                "id": "order-b",
                "display_id": "2",
                "third_party_id": "B1",
                "bill_phone_number": "0800000002",
                "bill_full_name": "Ten B",
                "total_price": 20000,
                "status": 1,
            },
            {
                "id": "order-c",
                "display_id": "3",
                "third_party_id": "C1",
                "bill_phone_number": "0800000003",
                "bill_full_name": "Ten C",
                "total_price": 30000,
                "status": 2,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))
    run_id = str(report["run_id"])

    apply_summary = service.apply_updates(run_id)
    assert apply_summary["updated"] == 1
    assert apply_summary["skipped"] == 2
    assert len(pancake.update_calls) == 1


def test_reconcile_cod_identity_unique_does_not_require_amount_match(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "NO-AWB-1",
                "status_text": "Giao hàng thành công",
                "phone": "0809111222",
                "customer_name": "Khach A",
                "cod": "3700",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "301",
                "third_party_id": "",
                "bill_phone_number": "0809 111 222",
                "bill_full_name": "Khach A",
                "total_price": 520000,
                "status": 1,
            }
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["matched_unique"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity"
    assert record["pancake_order_id"] == "order-1"


def test_reconcile_cod_identity_duplicates_use_amount_to_disambiguate(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "NO-AWB-2",
                "status_text": "Giao hàng thành công",
                "phone": "0809222333",
                "customer_name": "Khach B",
                "cod": "3700",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "302",
                "third_party_id": "",
                "bill_phone_number": "0809222333",
                "bill_full_name": "Khach B",
                "total_price": 370000,
                "status": 1,
            },
            {
                "id": "order-2",
                "display_id": "303",
                "third_party_id": "",
                "bill_phone_number": "0809222333",
                "bill_full_name": "Khach B",
                "total_price": 520000,
                "status": 1,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["matched_unique"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity"
    assert record["pancake_order_id"] == "order-1"


def test_reconcile_cod_identity_duplicates_without_amount_match_are_ambiguous(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "NO-AWB-3",
                "status_text": "Giao hàng thành công",
                "phone": "0809333444",
                "customer_name": "Khach C",
                "cod": "3700",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "304",
                "third_party_id": "",
                "bill_phone_number": "0809333444",
                "bill_full_name": "Khach C",
                "total_price": 520000,
                "status": 1,
            },
            {
                "id": "order-2",
                "display_id": "305",
                "third_party_id": "",
                "bill_phone_number": "0809333444",
                "bill_full_name": "Khach C",
                "total_price": 815000,
                "status": 1,
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["ambiguous"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity"
    assert record["match_result"] == "ambiguous"


def test_reconcile_cod_identity_duplicates_fallback_to_original_order_value(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "NO-AWB-4",
                "status_text": "Giao hàng thành công",
                "phone": "0809444555",
                "customer_name": "Khach D",
                "cod": "3700",
            }
        ],
    )
    pancake = _FakePancakeClient(
        orders=[
            {
                "id": "order-1",
                "display_id": "306",
                "third_party_id": "",
                "bill_phone_number": "0809444555",
                "bill_full_name": "Khach D",
                "total_price": 815000,
                "status": 1,
                "items": [
                    {"variation_info": {"retail_price": 370000}},
                    {"variation_info": {"retail_price": 445000}},
                ],
            },
            {
                "id": "order-2",
                "display_id": "307",
                "third_party_id": "",
                "bill_phone_number": "0809444555",
                "bill_full_name": "Khach D",
                "total_price": 520000,
                "status": 1,
                "items": [
                    {"variation_info": {"retail_price": 520000}},
                ],
            },
        ]
    )
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report(date(2026, 5, 9))
    assert report["summary"]["matched_unique"] == 1
    record = report["records"][0]
    assert record["match_tier"] == "identity_original_amount"
    assert record["pancake_order_id"] == "order-1"


def test_reconcile_cod_report_includes_conclusion_totals_and_vnd_fallback(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, report_thb_to_vnd_rate=800.0)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-05-09"}],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH-A",
                "status_text": "Giao hàng thành công",
                "phone": "0809000111",
                "customer_name": "Khach A",
                "cod": "5300",
                "cod_remain": "5300",
                "cod_vnd": None,
                "exchange_rate": "820",
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "TH-B",
                "status_text": "Giao hàng thành công",
                "phone": "0809000222",
                "customer_name": "Khach B",
                "cod": "2400",
                "cod_remain": "2400",
                "cod_vnd": "2000000",
                "exchange_rate": None,
            },
        ],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))
    totals = report.get("conclusion_totals", {})
    assert isinstance(totals, dict)
    assert totals.get("thb_total") == 7700.0
    assert totals.get("vnd_total") == 6346000
    assert totals.get("vnd_converted_count") == 1

    message = service.build_message(report, trigger_label="Đối soát COD thủ công")
    assert "Kết luận đối soát (THB): 7,700" in message
    assert "Kết luận đối soát (VNĐ): 6,346,000" in message
    assert "Đã quy đổi 1 đơn thiếu VNĐ theo tỷ giá 800" in message


def test_reconcile_cod_report_prefers_settlement_summary_conclusion(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, report_thb_to_vnd_rate=815.0)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[
            {
                "settlement_date": "2026-05-09",
                "conclusion": 4339.16,
                "conclusionVND": 3510380.44,
                "currencyRate": 809,
            }
        ],
        detail_rows=[
            {
                "settlement_date": "2026-05-09",
                "awb": "TH-A",
                "status_text": "Giao hàng thành công",
                "phone": "0809000111",
                "customer_name": "Khach A",
                "cod": "5300",
                "cod_remain": "5300",
                "cod_vnd": None,
                "exchange_rate": None,
            },
            {
                "settlement_date": "2026-05-09",
                "awb": "TH-B",
                "status_text": "Giao hàng thành công",
                "phone": "0809000222",
                "customer_name": "Khach B",
                "cod": "2400",
                "cod_remain": "2400",
                "cod_vnd": None,
                "exchange_rate": None,
            },
        ],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )
    report = service.generate_report(date(2026, 5, 9))
    totals = report.get("conclusion_totals", {})
    assert isinstance(totals, dict)
    assert totals.get("source") == "settlement_summary"
    assert totals.get("thb_total") == 4339.16
    assert totals.get("vnd_total") == 3510380.44
    assert totals.get("vnd_converted_count") == 0

    message = service.build_message(report, trigger_label="Đối soát COD thủ công")
    assert "Kết luận đối soát (THB): 4,339.16" in message
    assert "Kết luận đối soát (VNĐ): 3,510,380.44" in message


def test_reconcile_cod_weekly_cash_in_summary_t2_t6_and_vnd_fallback(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, report_thb_to_vnd_rate=800.0)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[
            {"settlement_date": "2026-05-25", "conclusion": 1000, "conclusionVND": 800000},
            {"settlement_date": "2026-05-26", "conclusion": 2000, "conclusionVND": 0},
            {"settlement_date": "2026-05-30", "conclusion": 9999, "conclusionVND": 9999000},
        ],
        detail_rows=[],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    summary = service.build_weekly_cash_in_summary(date(2026, 5, 31))
    assert summary["ok"] is True
    assert summary["week_start"] == "2026-05-25"
    assert summary["week_end"] == "2026-05-29"
    assert summary["thb_total"] == 3000.0
    assert summary["vnd_total"] == 2400000.0
    assert summary["vnd_converted_count"] == 1

    days = summary["days"]
    assert isinstance(days, list)
    assert [item["settlement_date"] for item in days] == ["2026-05-25", "2026-05-26"]
    assert days[1]["vnd_converted_count"] == 1


def test_reconcile_cod_cash_in_report_skips_day_without_settlement(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-06-01", "conclusion": 1000, "conclusionVND": 810000}],
        detail_rows=[
            {
                "settlement_date": "2026-06-01",
                "awb": "TH-A",
                "status_text": "Giao hàng thành công",
                "phone": "0809000111",
                "customer_name": "Khach A",
                "cod": "1000",
            }
        ],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report_if_settlement_exists(date(2026, 6, 5))

    assert report is None
    assert thai_duong.requested_settlement_dates == []


def test_reconcile_cod_cash_in_report_uses_exact_settlement_day(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(
        history_rows=[{"settlement_date": "2026-06-01", "conclusion": 1000, "conclusionVND": 810000}],
        detail_rows=[
            {
                "settlement_date": "2026-06-01",
                "awb": "TH-A",
                "status_text": "Giao hàng thành công",
                "phone": "0809000111",
                "customer_name": "Khach A",
                "cod": "1000",
            }
        ],
    )
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    report = service.generate_report_if_settlement_exists(date(2026, 6, 1))

    assert isinstance(report, dict)
    assert report["settlement_date"] == "2026-06-01"
    assert thai_duong.requested_settlement_dates == [date(2026, 6, 1)]


def test_reconcile_cod_summarize_cash_in_from_report_uses_conclusion_totals(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, report_thb_to_vnd_rate=815.0)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(history_rows=[], detail_rows=[])
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    summary = service.summarize_cash_in_from_report(
        {
            "ok": True,
            "partial": False,
            "settlement_date": "2026-05-25",
            "conclusion_totals": {
                "thb_total": 1234.56,
                "vnd_total": 1000000,
                "vnd_converted_count": 0,
                "source": "settlement_summary",
            },
        }
    )
    assert summary["ok"] is True
    assert summary["settlement_date"] == "2026-05-25"
    assert summary["thb_total"] == 1234.56
    assert summary["vnd_total"] == 1000000.0
    assert summary["vnd_converted_count"] == 0


def test_reconcile_cod_summarize_cash_in_from_report_converts_when_vnd_zero(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, report_thb_to_vnd_rate=810.0)
    _write_reconcile_configs(settings, mapped_status=2)
    thai_duong = _FakeThaiDuongClient(history_rows=[], detail_rows=[])
    pancake = _FakePancakeClient(orders=[])
    service = ReconcileCodService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,  # type: ignore[arg-type]
        thai_duong_client=thai_duong,  # type: ignore[arg-type]
    )

    summary = service.summarize_cash_in_from_report(
        {
            "ok": True,
            "partial": False,
            "settlement_date": "2026-06-01",
            "conclusion_totals": {
                "thb_total": 1221.74,
                "vnd_total": 0,
                "vnd_converted_count": 0,
                "source": "settlement_summary",
            },
        }
    )
    assert summary["thb_total"] == 1221.74
    assert summary["vnd_total"] == 989609.4
    assert summary["vnd_converted_count"] == 1
