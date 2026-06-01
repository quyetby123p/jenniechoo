from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from app.pancake_td_sync_service import PancakeToThaiDuongSyncService
from app.settings import Settings
from app.utils import dump_json, load_json


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
        pancake_api_key="api_key_dummy",
        pancake_access_token="",
        pancake_shop_id=123,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
        pancake_td_sync_enabled=True,
        pancake_td_sync_batch_limit=50,
        pancake_td_sync_poll_seconds=30,
        pancake_td_sync_product_refresh_minutes=30,
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


class FakePancakeClient:
    def __init__(
        self,
        orders: list[dict],
        *,
        fetch_error: Exception | None = None,
        note_update_error: Exception | None = None,
    ) -> None:
        self.orders = orders
        self.fetch_error = fetch_error
        self.note_update_error = note_update_error
        self.calls: list[tuple[int, int]] = []
        self.note_updates: list[dict[str, Any]] = []

    def fetch_orders_by_timestamp_range(self, start_ts: int, end_ts: int) -> list[dict]:
        self.calls.append((start_ts, end_ts))
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.orders)

    def update_order_note_print(
        self,
        order_id: str,
        note_text: str,
        *,
        update_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.note_update_error is not None:
            raise self.note_update_error
        self.note_updates.append(
            {
                "order_id": order_id,
                "note_text": note_text,
                "update_cfg": dict(update_cfg or {}),
            }
        )
        return {"success": True}


class FakeThaiDuongClient:
    def __init__(
        self,
        *,
        product_rows: list[dict] | None = None,
        existing_refs: set[str] | None = None,
        lookup_rows_by_reference: dict[str, list[dict[str, Any]]] | None = None,
        lookup_miss_counts: dict[str, int] | None = None,
        fail_create_attempts: int = 0,
        lookup_error: Exception | None = None,
        create_response: dict[str, Any] | None = None,
        status_update_error: Exception | None = None,
    ) -> None:
        self.product_rows = product_rows or []
        self.existing_refs = set(existing_refs or set())
        self.lookup_rows_by_reference = {
            str(key): [dict(row) for row in rows]
            for key, rows in (lookup_rows_by_reference or {}).items()
        }
        self.lookup_miss_counts = {
            str(key): max(0, int(value))
            for key, value in (lookup_miss_counts or {}).items()
        }
        self.fail_create_attempts = fail_create_attempts
        self.lookup_error = lookup_error
        self.status_update_error = status_update_error
        self.create_response = create_response or {"ok": True, "data": {"id": "td_1"}}
        self.create_calls: list[dict] = []
        self.lookup_calls: list[str] = []
        self.status_update_calls: list[dict[str, Any]] = []

    def fetch_products_for_sync(self, endpoint_cfg: dict) -> list[dict]:  # noqa: ARG002
        return list(self.product_rows)

    def find_orders_by_reference_for_sync(
        self,
        *,
        endpoint_cfg: dict,  # noqa: ARG002
        reference_value: str,
        reference_filter_field: str = "",  # noqa: ARG002
        extra_filters: dict | None = None,  # noqa: ARG002
    ) -> list[dict]:
        self.lookup_calls.append(reference_value)
        if self.lookup_error is not None:
            raise self.lookup_error
        miss_count = self.lookup_miss_counts.get(reference_value, 0)
        if miss_count > 0:
            self.lookup_miss_counts[reference_value] = miss_count - 1
            return []
        mapped_rows = self.lookup_rows_by_reference.get(reference_value)
        if isinstance(mapped_rows, list):
            return [dict(row) for row in mapped_rows]
        if reference_value in self.existing_refs:
            return [{"orderUID": reference_value}]
        return []

    def create_order_for_sync(self, payload: dict, endpoint_cfg: dict | None = None) -> dict:  # noqa: ARG002
        self.create_calls.append(payload)
        if self.fail_create_attempts > 0:
            self.fail_create_attempts -= 1
            raise RuntimeError("temporary error")
        return dict(self.create_response)

    def update_order_status_for_sync(
        self,
        *,
        order_id: str,
        payload: dict[str, Any] | None = None,
        endpoint_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.status_update_error is not None:
            raise self.status_update_error
        self.status_update_calls.append(
            {
                "order_id": order_id,
                "payload": dict(payload or {}),
                "endpoint_cfg": dict(endpoint_cfg or {}),
            }
        )
        return {"success": True}


def _write_basic_sync_config(settings: Settings, **override: dict) -> None:
    settings.config_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "poll": {
            "cursor_overlap_seconds": 120,
            "state_retention_days": 60,
            "max_lookback_hours": 0,
            "today_only_local": False,
            "auth_error_pause_minutes": 20,
            "failed_order_retry_minutes": 5,
            "pancake_fetch_error_pause_seconds": 300,
            "post_create_lookup_retry_attempts": 3,
            "post_create_lookup_retry_delay_seconds": [0, 0, 0],
        },
        "retry": {"max_attempts": 3, "backoff_seconds": [0, 0, 0]},
        "notify": {
            "notify_on_created": True,
            "notify_on_error": True,
            "notify_on_empty_poll": False,
            "max_error_lines": 10,
            "pancake_fetch_error_notify_cooldown_minutes": 15,
        },
        "pancake": {
            "order_id_paths": ["id"],
            "order_code_paths": ["custom_id"],
            "order_created_ts_paths": ["inserted_at_timestamp"],
            "items_paths": ["items[]"],
            "item_code_paths": ["variation_info.sku"],
            "item_color_paths": ["variation_info.color"],
            "item_quantity_paths": ["quantity"],
            "item_price_paths": ["variation_info.retail_price"],
            "item_name_paths": ["variation_info.name"],
            "payment_method_paths": ["payment_method"],
            "transferred_amount_paths": ["transferred_amount", "transfer_money"],
            "deposit_amount_paths": ["deposit_amount"],
            "total_amount_paths": ["total_price"],
            "money_minor_unit_factor": 100,
            "payment_method_transfer_keywords": ["chuyen khoan", "transfer", "prepaid"],
            "payment_method_deposit_keywords": ["coc", "deposit"],
            "payment_method_cod_keywords": ["cod", "thu ho", "cash on delivery"],
        },
        "thai_duong": {
            "product_endpoint": {
                "base_url_env": "THAI_DUONG_API_BASE_URL",
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
                "method": "GET",
                "path": "/api/v1/products",
                "request_mode": "query",
                "page_field": "page",
                "page_size_field": "limit",
                "page_size": 200,
                "result_path": "data.data",
                "total_pages_path": "data.total",
                "has_next_page_path": "data.hasNextPage",
                "search_field": "searchText",
                "filters_field": "filters",
                "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
            },
            "order_lookup_endpoint": {
                "base_url_env": "THAI_DUONG_API_BASE_URL",
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
                "method": "POST",
                "path": "/api/v1/orders/list",
                "request_mode": "json_body",
                "page_field": "page",
                "page_size_field": "limit",
                "page_size": 50,
                "result_path": "data.data",
                "total_pages_path": "data.total",
                "has_next_page_path": "data.hasNextPage",
                "search_field": "searchText",
                "filters_field": "filters",
                "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
            },
            "create_order_endpoint": {
                "base_url_env": "THAI_DUONG_API_BASE_URL",
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
                "method": "POST",
                "path": "/api/v1/orders",
            },
            "reference_filter_field": "",
            "order_reference_paths": ["orderUID", "pancakeOrderId", "note"],
            "product_mapping": {
                "product_code_paths": ["sku"],
                "product_color_paths": ["color"],
                "product_name_paths": ["name"],
                "product_id_paths": ["id"],
                "variant_list_paths": [],
                "variant_code_paths": ["sku"],
                "variant_color_paths": ["color"],
                "variant_id_paths": ["id"],
                "variant_name_paths": ["name"],
            },
            "payload_fields": {
                "reference_order_id_path": "orderUID",
                "pancake_order_id_path": "pancakeOrderId",
                "customer_code_path": "customerCode",
                "payment_type_path": "paymentType",
                "deposit_amount_path": "codTransferred",
                "cod_amount_path": "cod",
                "need_sale_confirm_path": "isNeedSale",
                "need_sale_confirm_value": False,
                "order_status_path": "orderStatus",
                "order_status_value": "SALE_CONFIRM",
                "items_path": "products",
                "note_path": "note",
                "copy_from_order": {}
            },
            "item_payload_keys": {
                "quantity": "quantity",
                "sku": "sku"
            }
        }
    }
    payload.update(override)
    dump_json(settings.pancake_td_sync_config_path, payload)
    dump_json(settings.pancake_td_color_alias_config_path, {"kem": "trắng"})
    dump_json(
        settings.thai_duong_order_payload_template_path,
        {
            "orderUID": "",
            "pancakeOrderId": "",
            "customerCode": "",
            "paymentType": "",
            "orderStatus": "",
            "cod": 0,
            "codTransferred": 0,
            "isNeedSale": False,
            "products": [],
        },
    )


def _enable_print_note_sync(settings: Settings) -> None:
    cfg = load_json(settings.pancake_td_sync_config_path)
    if not isinstance(cfg, dict):
        cfg = {}
    pancake_cfg = cfg.get("pancake", {})
    if not isinstance(pancake_cfg, dict):
        pancake_cfg = {}
        cfg["pancake"] = pancake_cfg
    print_cfg = pancake_cfg.get("print_note_sync", {})
    if not isinstance(print_cfg, dict):
        print_cfg = {}
    print_cfg["enabled"] = True
    pancake_cfg["print_note_sync"] = print_cfg
    dump_json(settings.pancake_td_sync_config_path, cfg)


def test_sync_transfer_order_sets_transfer_and_cod_zero(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_1",
                "custom_id": "JCT001",
                "inserted_at_timestamp": 1_714_000_001,
                "payment_method": "chuyển khoản",
                "transferred_amount": 500000,
                "total_price": 500000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "SP-01",
                            "color": "kem",
                            "retail_price": 500000,
                            "name": "Ao thun",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 101, "sku": "SP01", "color": "trắng", "name": "Ao thun TD"}]
    )
    service = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )

    report = service.sync_once()

    assert report["created"] == 1
    assert len(thai_duong.create_calls) == 1
    payload = thai_duong.create_calls[0]
    assert payload["paymentType"] == "TRANSFER"
    assert payload["codTransferred"] == 5000
    assert payload["cod"] == 5000
    assert payload["isNeedSale"] is False
    assert payload["orderStatus"] == "SALE_CONFIRM"
    assert payload["products"][0]["sku"] == "SP01"
    assert payload["products"][0]["quantity"] == 1
    assert len(thai_duong.status_update_calls) == 1
    assert thai_duong.status_update_calls[0]["order_id"] == "td_1"
    assert thai_duong.status_update_calls[0]["payload"]["orderStatus"] == "SALE_CONFIRM"
    assert thai_duong.status_update_calls[0]["payload"]["isNeedSale"] is False


def test_sync_writes_thai_duong_order_uid_to_pancake_print_note(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    _enable_print_note_sync(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_note_1",
                "custom_id": "JCTNOTE01",
                "inserted_at_timestamp": 1_714_000_050,
                "payment_method": "cod",
                "total_price": 500000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "SP-01",
                            "color": "kem",
                            "retail_price": 500000,
                            "name": "Ao note",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 101, "sku": "SP01", "color": "trắng", "name": "Ao thun TD"}],
        create_response={"ok": True, "orderUID": "THA356_20260526_2"},
    )
    service = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )

    report = service.sync_once()

    assert report["created"] == 1
    assert report["print_note_synced"] == 1
    assert report["print_note_failed"] == 0
    assert len(pancake.note_updates) == 1
    assert pancake.note_updates[0]["order_id"] == "pc_note_1"
    assert pancake.note_updates[0]["note_text"] == "THA356_20260526_2"


def test_sync_print_note_update_error_does_not_fail_create(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    _enable_print_note_sync(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_note_2",
                "custom_id": "JCTNOTE02",
                "inserted_at_timestamp": 1_714_000_051,
                "payment_method": "cod",
                "total_price": 500000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "SP-01",
                            "color": "kem",
                            "retail_price": 500000,
                            "name": "Ao note",
                        },
                    }
                ],
            }
        ],
        note_update_error=RuntimeError("Pancake update error"),
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 101, "sku": "SP01", "color": "trắng", "name": "Ao thun TD"}],
        create_response={"ok": True, "orderUID": "THA356_20260526_3"},
    )
    service = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )

    report = service.sync_once()

    assert report["created"] == 1
    assert report["failed"] == 0
    assert report["print_note_synced"] == 0
    assert report["print_note_failed"] == 1
    assert any("Ghi chú in Pancake lỗi" in str(item) for item in report.get("errors", []))


def test_sync_sale_status_update_error_is_reported_but_create_kept(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_sale_status_1",
                "custom_id": "JCTSALE01",
                "inserted_at_timestamp": 1_714_000_052,
                "payment_method": "cod",
                "total_price": 500000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "SP-01",
                            "color": "kem",
                            "retail_price": 500000,
                            "name": "Ao sale",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 101, "sku": "SP01", "color": "trắng", "name": "Ao thun TD"}],
        create_response={"ok": True, "data": {"id": "td_sale_1", "orderUID": "THA356_20260529_99"}},
        status_update_error=RuntimeError("status update unauthorized"),
    )
    service = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )

    report = service.sync_once()

    assert report["created"] == 1
    assert report["failed"] == 0
    assert report["sale_status_synced"] == 0
    assert report["sale_status_failed"] == 1
    assert any("Cập nhật Sale xác nhận lỗi" in str(item) for item in report.get("errors", []))


def test_sync_retries_lookup_after_create_to_update_sale_and_print_note(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    _enable_print_note_sync(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_delay_1",
                "custom_id": "JCTDELAY01",
                "inserted_at_timestamp": 1_714_000_053,
                "payment_method": "chuyển khoản",
                "transfer_money": 250000,
                "total_price": 250000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "SP-01",
                            "color": "kem",
                            "retail_price": 250000,
                            "name": "Ao delay",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 101, "sku": "SP01", "color": "trắng", "name": "Ao thun TD"}],
        create_response={"ok": True},
        lookup_rows_by_reference={
            "JCTDELAY01": [
                {
                    "id": "td_delay_1",
                    "orderUID": "THA356_20260601_1",
                    "pancakeOrderId": "pc_delay_1",
                    "note": "Pancake order JCTDELAY01",
                }
            ]
        },
        lookup_miss_counts={"JCTDELAY01": 1},
    )
    service = PancakeToThaiDuongSyncService(
        settings=settings,
        logger=logging.getLogger("test"),
        pancake_client=pancake,
        thai_duong_client=thai_duong,
    )

    report = service.sync_once()

    assert report["created"] == 1
    assert report["sale_status_synced"] == 1
    assert report["sale_status_failed"] == 0
    assert report["print_note_synced"] == 1
    assert report["print_note_failed"] == 0
    assert len(thai_duong.status_update_calls) == 1
    assert thai_duong.status_update_calls[0]["order_id"] == "td_delay_1"
    assert len(pancake.note_updates) == 1
    assert pancake.note_updates[0]["note_text"] == "THA356_20260601_1"


def test_sync_deposit_and_cod_rules(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    product_rows = [{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}]
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_deposit",
                "custom_id": "JCT002",
                "inserted_at_timestamp": 1_714_000_002,
                "payment_method": "đơn cọc",
                "deposit_amount": 200000,
                "total_price": 700000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 700000, "name": "A"},
                    }
                ],
            },
            {
                "id": "pc_cod",
                "custom_id": "JCT003",
                "inserted_at_timestamp": 1_714_000_003,
                "payment_method": "cod",
                "total_price": 400000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 400000, "name": "A"},
                    }
                ],
            },
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=product_rows)
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 2
    first = thai_duong.create_calls[0]
    second = thai_duong.create_calls[1]
    assert first["paymentType"] == "COD"
    assert first["codTransferred"] == 2000
    assert first["cod"] == 5000
    assert second["paymentType"] == "COD"
    assert second["codTransferred"] == 0
    assert second["cod"] == 4000


def test_sync_partial_transfer_keeps_total_cod_and_sets_deposit(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_transfer_partial",
                "custom_id": "JCTX01",
                "inserted_at_timestamp": 1_714_000_100,
                "payment_method": "chuyển khoản",
                "transfer_money": 1000000,
                "total_price": 3025000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 3025000, "name": "A"},
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["paymentType"] == "TRANSFER"
    assert payload["cod"] == 30250
    assert payload["codTransferred"] == 10000


def test_sync_transfer_amount_without_method_text_still_marks_transfer(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_transfer_signal",
                "custom_id": "JCTX02",
                "inserted_at_timestamp": 1_714_000_110,
                "payment_method": None,
                "transfer_money": 340000,
                "total_price": 340000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 340000, "name": "A"},
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["paymentType"] == "TRANSFER"
    assert payload["cod"] == 3400
    assert payload["codTransferred"] == 3400


def test_sync_local_and_remote_idempotency(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    orders = [
        {
            "id": "pc_1",
            "custom_id": "JCT001",
            "inserted_at_timestamp": 1_714_000_001,
            "payment_method": "cod",
            "total_price": 100000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
        },
        {
            "id": "pc_2",
            "custom_id": "JCT002",
            "inserted_at_timestamp": 1_714_000_002,
            "payment_method": "cod",
            "total_price": 100000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
        existing_refs={"JCT002"},
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_once()
    second = service.sync_once()

    assert first["created"] == 1
    assert first["skipped_remote_duplicate"] == 1
    assert second["created"] == 0
    assert second["skipped_local_duplicate"] >= 2
    assert len(thai_duong.create_calls) == 1


def test_sync_retry_create_succeeds_on_third_attempt(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_retry",
                "custom_id": "JCT004",
                "inserted_at_timestamp": 1_714_000_004,
                "payment_method": "cod",
                "total_price": 100000,
                "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
        fail_create_attempts=2,
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    assert len(thai_duong.create_calls) == 3


def test_sync_updates_cursor_and_state(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_1",
                "custom_id": "JCT001",
                "inserted_at_timestamp": 1_714_000_005,
                "payment_method": "cod",
                "total_price": 100000,
                "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()
    state = load_json(settings.pancake_td_sync_state_file)

    assert report["cursor_to"] >= 1_714_000_005
    assert state["cursor_ts"] >= 1_714_000_005
    assert "pc_1" in state["processed_order_ids"]


def test_sync_notify_false_when_no_activity(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient([])
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 0
    assert report["failed"] == 0
    assert report["notify"] is False


def test_sync_auth_error_sets_pause_and_stops_batch(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_auth_1",
                "custom_id": "JCT001",
                "inserted_at_timestamp": 1_714_000_010,
                "payment_method": "cod",
                "total_price": 100000,
                "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
            },
            {
                "id": "pc_auth_2",
                "custom_id": "JCT002",
                "inserted_at_timestamp": 1_714_000_011,
                "payment_method": "cod",
                "total_price": 100000,
                "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
            },
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
        lookup_error=RuntimeError("API Thai Duong loi (401): Unauthorized"),
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()
    state = load_json(settings.pancake_td_sync_state_file)

    assert report["failed"] == 1
    assert report["created"] == 0
    assert report["considered"] == 1
    assert "auth_pause_until_ts" in report
    assert state.get("td_auth_pause_until_ts", 0) > 0
    assert thai_duong.create_calls == []


def test_sync_pause_window_skips_fetch(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    future_pause = int(datetime.now(timezone.utc).timestamp()) + 1200
    dump_json(
        settings.pancake_td_sync_state_file,
        {"cursor_ts": 0, "processed_order_ids": {}, "td_auth_pause_until_ts": future_pause},
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_1",
                "custom_id": "JCT001",
                "inserted_at_timestamp": 1_714_000_001,
                "payment_method": "cod",
                "total_price": 100000,
                "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["notify"] is False
    assert report.get("paused_until_ts", 0) == future_pause
    assert report["fetched"] == 0
    assert pancake.calls == []


def test_sync_pancake_fetch_error_backoff_and_cooldown(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(
        settings,
        poll={
            "cursor_overlap_seconds": 120,
            "state_retention_days": 60,
            "max_lookback_hours": 0,
            "today_only_local": False,
            "auth_error_pause_minutes": 20,
            "failed_order_retry_minutes": 5,
            "pancake_fetch_error_pause_seconds": 60,
        },
        notify={
            "notify_on_created": True,
            "notify_on_error": True,
            "notify_on_empty_poll": False,
            "max_error_lines": 10,
            "pancake_fetch_error_notify_cooldown_minutes": 15,
        },
    )
    pancake = FakePancakeClient([], fetch_error=RuntimeError('Pancake API lỗi (500): "Server internal error"'))
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_once()
    state = load_json(settings.pancake_td_sync_state_file)

    assert first["failed"] == 1
    assert first["notify"] is True
    assert len(first["errors"]) == 1
    assert state.get("pancake_fetch_pause_until_ts", 0) > 0
    assert len(pancake.calls) == 1

    second = service.sync_once()
    assert second["notify"] is False
    assert second["failed"] == 0
    assert second.get("pause_reason") == "pancake_fetch_error_backoff"
    assert len(pancake.calls) == 1

    state = load_json(settings.pancake_td_sync_state_file)
    state["pancake_fetch_pause_until_ts"] = 0
    dump_json(settings.pancake_td_sync_state_file, state)
    third = service.sync_once()

    assert third["failed"] == 1
    assert third["notify"] is False
    assert third["skipped_repeated_error"] == 1
    assert len(third["errors"]) == 0
    assert len(pancake.calls) == 2

    state = load_json(settings.pancake_td_sync_state_file)
    state["pancake_fetch_pause_until_ts"] = 0
    if isinstance(state.get("last_pancake_fetch_error"), dict):
        state["last_pancake_fetch_error"]["notified_at"] = "2020-01-01T00:00:00+00:00"
    dump_json(settings.pancake_td_sync_state_file, state)
    fourth = service.sync_once()

    assert fourth["failed"] == 1
    assert fourth["notify"] is True
    assert len(fourth["errors"]) == 1
    assert len(pancake.calls) == 3


def test_sync_sku_mapping_handles_kem_nude_variant(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_kemnude",
                "custom_id": "JCT341",
                "inserted_at_timestamp": 1_714_000_200,
                "payment_method": "cod",
                "total_price": 780000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JCV237-KEM NUDE-L",
                            "retail_price": 780000,
                            "name": "Selene's Light Dress JCV237",
                            "fields": [
                                {"name": "Màu", "value": "Kem Nude", "keyValue": "KEM NUDE"},
                                {"name": "Size", "value": "L", "keyValue": "L"},
                            ],
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "THA356-JC-V-237-NUDE-L", "color": "", "name": "JCV237"}],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-V-237-NUDE-L"


def test_sync_sku_mapping_handles_kem_ao_kem_tone_alias(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_kem_tone",
                "custom_id": "JCT342",
                "inserted_at_timestamp": 1_714_000_250,
                "payment_method": "cod",
                "total_price": 340000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JC-A-244-KEM: AO KEM-M",
                            "color": "Kem: Áo Kem",
                            "retail_price": 340000,
                            "name": "Ao kem",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "THA356-JC-A-244-Trắng-M", "color": "trắng", "name": "A244"}],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-A-244-Trắng-M"


def test_sync_sku_mapping_handles_be_ao_ren_to_kem_variant(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    dump_json(
        settings.pancake_td_color_alias_config_path,
        {
            "be": "kem",
            "beige": "kem",
            "kem": "trắng",
            "cream": "trắng",
            "trang": "trắng",
        },
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_jca250",
                "custom_id": "JCT346",
                "inserted_at_timestamp": 1_714_000_400,
                "payment_method": "cod",
                "total_price": 190000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JC-A-250-BE: AO REN MAU BE-S",
                            "color": "Be: Áo ren màu be",
                            "retail_price": 190000,
                            "name": "Falling Cloud JCA250",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[
            {"id": 1, "sku": "THA356-JC-A-250-Kem-S", "color": None, "name": "Falling Cloud JCA250"},
            {"id": 2, "sku": "THA356-JC-A-250-Kem-M", "color": None, "name": "Falling Cloud JCA250"},
        ],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-A-250-Kem-S"


def test_sync_sku_mapping_handles_nude_beige_to_hong_variant(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    dump_json(
        settings.pancake_td_color_alias_config_path,
        {
            "nude beige": "hồng",
            "hong": "hồng",
        },
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_jcv239",
                "custom_id": "JCT310",
                "inserted_at_timestamp": 1_714_000_450,
                "payment_method": "cod",
                "total_price": 520000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JC-V-239-NUDE BEIGE-M",
                            "retail_price": 520000,
                            "name": "Artemis Cape Dress JCV239",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[
            {"id": 1, "sku": "THA356-JC-V-239-Hồng-S", "color": "", "name": "Artemis Cape Dress JCV239"},
            {"id": 2, "sku": "THA356-JC-V-239-Hồng-M", "color": "", "name": "Artemis Cape Dress JCV239"},
            {"id": 3, "sku": "THA356-JC-V-239-Hồng-L", "color": "", "name": "Artemis Cape Dress JCV239"},
        ],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-V-239-Hồng-M"


def test_sync_sku_mapping_handles_compound_sku_for_jca250_be_to_kem(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    dump_json(
        settings.pancake_td_color_alias_config_path,
        {
            "be": "kem",
            "beige": "kem",
            "kem": "trắng",
        },
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_jca250_compound",
                "custom_id": "JCT350",
                "inserted_at_timestamp": 1_714_000_451,
                "payment_method": "cod",
                "total_price": 520000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JCV250-BE: AO REN MAU BE-MJC-A-250-BE: AO REN MAU BE-M",
                            "color": "Be: Áo ren màu be",
                            "retail_price": 520000,
                            "name": "Ao ren JCA250",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[
            {"id": 1, "sku": "THA356-JC-A-250-Kem-M", "color": "", "name": "Falling Cloud JCA250"},
            {"id": 2, "sku": "THA356-JC-A-250-Kem-S", "color": "", "name": "Falling Cloud JCA250"},
        ],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-A-250-Kem-M"


def test_sync_sku_mapping_handles_embedded_kem_token_in_sku(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    dump_json(
        settings.pancake_td_color_alias_config_path,
        {
            "kem": "trắng",
            "white": "trắng",
        },
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_jcq221",
                "custom_id": "JCTX221",
                "inserted_at_timestamp": 1_714_000_452,
                "payment_method": "cod",
                "total_price": 385000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JC-Q-221KEMM",
                            "color": "Kem",
                            "retail_price": 385000,
                            "name": "Luminous Ease Trousers JCQ221",
                        },
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[
            {"id": 1, "sku": "THA356-JC-Q-221-White-M", "color": "", "name": "Luminous Ease Trousers JCQ221"},
        ],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-Q-221-White-M"


def test_sync_retries_mapping_after_product_cache_refresh(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_jca246",
                "custom_id": "JCT353",
                "inserted_at_timestamp": 1_714_000_453,
                "payment_method": "cod",
                "total_price": 530000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {
                            "sku": "JC-A-246-KEM: AO KEM-L",
                            "color": "Kem: Áo Kem",
                            "retail_price": 530000,
                            "name": "Venus Sway JCA246",
                        },
                    }
                ],
            }
        ]
    )

    class RefreshOnSecondFetchThaiDuongClient(FakeThaiDuongClient):
        def __init__(self) -> None:
            super().__init__(product_rows=[])
            self.fetch_count = 0

        def fetch_products_for_sync(self, endpoint_cfg: dict) -> list[dict]:  # noqa: ARG002
            self.fetch_count += 1
            if self.fetch_count == 1:
                return [{"id": 1, "sku": "THA356-JC-A-999-White-M", "color": "", "name": "Dummy"}]
            return [{"id": 2, "sku": "THA356-JC-A-246-Kem-L", "color": "", "name": "Venus Sway JCA246"}]

    thai_duong = RefreshOnSecondFetchThaiDuongClient()
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_once()

    assert report["created"] == 1
    assert report["failed"] == 0
    assert thai_duong.fetch_count == 2
    payload = thai_duong.create_calls[0]
    assert payload["products"][0]["sku"] == "THA356-JC-A-246-Kem-L"


def test_sync_unmapped_error_not_spam_until_order_changes(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_unmapped_1",
                "custom_id": "JCTX99",
                "inserted_at_timestamp": 1_714_000_300,
                "updated_at": "2026-05-23T08:00:00",
                "payment_method": "cod",
                "total_price": 340000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "MISS-01", "retail_price": 340000, "name": "Missing SKU"},
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_once()
    second = service.sync_once()

    assert first["failed"] == 1
    assert first["notify"] is True
    assert second["failed"] == 0
    assert second["skipped_repeated_error"] == 1
    assert second["notify"] is False

    pancake.orders[0]["updated_at"] = "2026-05-23T08:05:00"
    pancake.orders[0]["items"][0]["variation_info"]["sku"] = "SP-01"
    third = service.sync_once()

    assert third["created"] == 1


def test_sync_failed_order_is_retried_after_window_without_resending_error(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(
        settings,
        poll={
            "cursor_overlap_seconds": 120,
            "state_retention_days": 60,
            "max_lookback_hours": 0,
            "today_only_local": False,
            "auth_error_pause_minutes": 20,
            "failed_order_retry_minutes": 1,
        },
    )
    pancake = FakePancakeClient(
        [
            {
                "id": "pc_retry_window",
                "custom_id": "JCTX98",
                "inserted_at_timestamp": 1_714_000_350,
                "updated_at": "2026-05-23T08:00:00",
                "payment_method": "cod",
                "total_price": 340000,
                "items": [
                    {
                        "quantity": 1,
                        "variation_info": {"sku": "MISS-02", "retail_price": 340000, "name": "Missing SKU"},
                    }
                ],
            }
        ]
    )
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_once()
    state = load_json(settings.pancake_td_sync_state_file)
    state["failed_order_ids"]["pc_retry_window"]["at"] = "2020-01-01T00:00:00+00:00"
    dump_json(settings.pancake_td_sync_state_file, state)
    second = service.sync_once()

    assert first["failed"] == 1
    assert first["notify"] is True
    assert second["failed"] == 0
    assert second["skipped_repeated_error"] == 1
    assert second["notify"] is False


def test_sync_today_manual_ignores_pause_and_forces_day_start(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    future_pause = int(datetime.now(timezone.utc).timestamp()) + 1200
    dump_json(
        settings.pancake_td_sync_state_file,
        {
            "cursor_ts": 1_900_000_000,
            "processed_order_ids": {},
            "td_auth_pause_until_ts": future_pause,
            "pancake_fetch_pause_until_ts": future_pause,
        },
    )
    pancake = FakePancakeClient([])
    thai_duong = FakeThaiDuongClient(product_rows=[])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_today_manual()

    assert len(pancake.calls) == 1
    fetch_start_ts, fetch_end_ts = pancake.calls[0]
    expected_day_start = service._day_start_local_unix(datetime.fromtimestamp(fetch_end_ts, timezone.utc))
    assert fetch_start_ts == expected_day_start
    assert report["fetch_start_ts"] == expected_day_start


def test_sync_today_manual_keeps_duplicate_filters(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    orders = [
        {
            "id": "pc_manual_1",
            "custom_id": "JCTM001",
            "inserted_at_timestamp": 1_714_000_601,
            "payment_method": "cod",
            "total_price": 100000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
        },
        {
            "id": "pc_manual_2",
            "custom_id": "JCTM002",
            "inserted_at_timestamp": 1_714_000_602,
            "payment_method": "cod",
            "total_price": 100000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 100000}}],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
        existing_refs={"JCTM002"},
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_today_manual()
    second = service.sync_today_manual()

    assert first["created"] == 1
    assert first["skipped_remote_duplicate"] == 1
    assert second["created"] == 0
    assert second["skipped_local_duplicate"] >= 2


def test_sync_order_code_manual_filters_by_order_code(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    orders = [
        {
            "id": "pc_310",
            "custom_id": "JCT310",
            "inserted_at_timestamp": 1_714_000_710,
            "payment_method": "cod",
            "total_price": 120000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 120000}}],
        },
        {
            "id": "pc_311",
            "custom_id": "JCT311",
            "inserted_at_timestamp": 1_714_000_711,
            "payment_method": "cod",
            "total_price": 120000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 120000}}],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_order_code_manual("jct310")

    assert report["created"] == 1
    assert report["failed"] == 0
    assert report["manual_order_code"] == "JCT310"
    assert report["fetched_matched"] == 1
    assert report["created_order_ids"] == ["pc_310"]


def test_sync_order_code_manual_returns_error_when_not_found(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    orders = [
        {
            "id": "pc_311",
            "custom_id": "JCT311",
            "inserted_at_timestamp": 1_714_000_711,
            "payment_method": "cod",
            "total_price": 120000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 120000}}],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_order_code_manual("JCT310")

    assert report["created"] == 0
    assert report["failed"] == 1
    assert report["fetched_matched"] == 0
    assert any("Không tìm thấy đơn Pancake mã JCT310" in str(item) for item in report.get("errors", []))


def test_sync_order_code_manual_retries_failed_order_without_suppressing_error(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    orders = [
        {
            "id": "pc_310",
            "custom_id": "JCT310",
            "inserted_at_timestamp": 1_714_000_710,
            "payment_method": "cod",
            "total_price": 120000,
            "items": [
                {"quantity": 1, "variation_info": {"sku": "MISS-01", "color": "kem", "retail_price": 120000}}
            ],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}])
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    first = service.sync_order_code_manual("JCT310")
    second = service.sync_order_code_manual("JCT310")

    assert first["failed"] == 1
    assert first["skipped_repeated_error"] == 0
    assert second["failed"] == 1
    assert second["skipped_repeated_error"] == 0
    assert any("Map sản phẩm lỗi cho đơn JCT310" in str(item) for item in second.get("errors", []))


def test_sync_order_code_manual_backfills_note_and_sale_for_local_duplicate(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    _write_basic_sync_config(settings)
    _enable_print_note_sync(settings)
    dump_json(
        settings.pancake_td_sync_state_file,
        {
            "cursor_ts": 1_714_000_900,
            "processed_order_ids": {"pc_362": "2026-06-01T02:00:00+00:00"},
            "failed_order_ids": {},
        },
    )
    orders = [
        {
            "id": "pc_362",
            "custom_id": "JCT362",
            "inserted_at_timestamp": 1_714_000_900,
            "payment_method": "cod",
            "total_price": 190000,
            "items": [{"quantity": 1, "variation_info": {"sku": "SP-01", "color": "kem", "retail_price": 190000}}],
        },
    ]
    pancake = FakePancakeClient(orders)
    thai_duong = FakeThaiDuongClient(
        product_rows=[{"id": 1, "sku": "SP01", "color": "trắng", "name": "P1"}],
        lookup_rows_by_reference={
            "JCT362": [
                {
                    "id": "td_362",
                    "orderUID": "THA356_20260531_4",
                    "pancakeOrderId": "pc_362",
                    "note": "Pancake order JCT362",
                }
            ]
        },
    )
    service = PancakeToThaiDuongSyncService(settings, logging.getLogger("test"), pancake, thai_duong)

    report = service.sync_order_code_manual("JCT362")

    assert report["created"] == 0
    assert report["skipped_local_duplicate"] == 1
    assert report["sale_status_synced"] == 1
    assert report["print_note_synced"] == 1
    assert report["failed"] == 0
    assert len(thai_duong.status_update_calls) == 1
    assert thai_duong.status_update_calls[0]["order_id"] == "td_362"
    assert len(pancake.note_updates) == 1
    assert pancake.note_updates[0]["order_id"] == "pc_362"
    assert pancake.note_updates[0]["note_text"] == "THA356_20260531_4"
