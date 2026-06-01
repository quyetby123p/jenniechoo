import copy
from datetime import date
import logging
from pathlib import Path

import pytest

from app.exceptions import ValidationError
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings


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
        pancake_page_size=2,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


def test_fetch_all_orders_for_date_paginates_and_filters(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    calls: list[int] = []

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/shops/123/orders"
        page = int(params.get("page_number", 1))
        calls.append(page)
        if page == 1:
            return {
                "success": True,
                "total_pages": 2,
                "data": [
                    {"id": "o1", "inserted_at": "2026-05-15T10:00:00+07:00"},
                    {"id": "o2", "inserted_at": "2026-05-14T23:59:59+07:00"},
                ],
            }
        return {
            "success": True,
            "total_pages": 2,
            "data": [
                {"id": "o3", "inserted_at": "2026-05-15T22:30:00+07:00"},
            ],
        }

    client._request = fake_request  # type: ignore[method-assign]
    orders = client.fetch_all_orders_for_date(date(2026, 5, 15), "Asia/Ho_Chi_Minh")
    assert [order["id"] for order in orders] == ["o1", "o3"]
    assert calls == [1, 2]


def test_fetch_all_orders_requires_shop_id(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, pancake_shop_id=0)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    with pytest.raises(ValidationError):
        client.fetch_all_orders_for_date(date(2026, 5, 15), "Asia/Ho_Chi_Minh")


def test_request_uses_access_token_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _dummy_settings(tmp_path, pancake_api_key="", pancake_access_token="token_dummy")
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))

    captured_params: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        text = '{"success": true, "shops": []}'

    def fake_http_request(method: str, url: str, *, params=None, json=None, timeout=None):  # noqa: ANN001
        del method, url, json, timeout
        captured_params.update(params or {})
        return _FakeResponse()

    monkeypatch.setattr("app.pancake_pos_client.requests.request", fake_http_request)
    shops = client.list_shops()
    assert shops == []
    assert captured_params.get("access_token") == "token_dummy"


def test_fetch_all_orders_for_range_returns_all_pages(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/shops/123/orders"
        page = int(params.get("page_number", 1))
        if page == 1:
            return {
                "success": True,
                "total_pages": 2,
                "data": [{"id": "o1"}, {"id": "o2"}],
            }
        return {
            "success": True,
            "total_pages": 2,
            "data": [{"id": "o3"}],
        }

    client._request = fake_request  # type: ignore[method-assign]
    orders = client.fetch_all_orders_for_range(date(2026, 5, 1), date(2026, 5, 15), "Asia/Ho_Chi_Minh")
    assert [item["id"] for item in orders] == ["o1", "o2", "o3"]


def test_update_order_status_uses_default_endpoint(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    calls: list[tuple[str, str, dict[str, int]]] = []

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        del params
        calls.append((method, path, data or {}))
        return {"success": True}

    client._request = fake_request  # type: ignore[method-assign]
    payload = client.update_order_status("180157094927073", 2)
    assert payload["success"] is True
    assert calls == [("POST", "/shops/123/orders/180157094927073", {"status": 2})]


def test_fetch_orders_by_timestamp_range_uses_internal_fetch(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    captured: list[tuple[int, int]] = []

    def fake_fetch(start_ts: int, end_ts: int):  # noqa: ANN202
        captured.append((start_ts, end_ts))
        return [{"id": "o1"}]

    client._fetch_orders_by_timestamp_range = fake_fetch  # type: ignore[method-assign]
    rows = client.fetch_orders_by_timestamp_range(100, 200)
    assert rows == [{"id": "o1"}]
    assert captured == [(100, 200)]


def test_update_order_note_print_uses_default_endpoint(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    calls: list[tuple[str, str, dict[str, object]]] = []
    source_order = {
        "id": 180157094927073,
        "status": 1,
        "total_price": 190000,
        "total_quantity": 1,
        "is_empty_cart": False,
        "items": [{"product_id": "p1", "quantity": 1}],
        "note_print": "",
    }
    updated_order = copy.deepcopy(source_order)
    updated_order["note_print"] = "THA356_20260526_2"

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        del params
        calls.append((method, path, data or {}))
        if method == "GET":
            if len([call for call in calls if call[0] == "GET"]) == 1:
                return {"success": True, "order": copy.deepcopy(source_order)}
            return {"success": True, "order": copy.deepcopy(updated_order)}
        return {"success": True}

    client._request = fake_request  # type: ignore[method-assign]
    payload = client.update_order_note_print("180157094927073", "THA356_20260526_2")
    assert payload["success"] is True
    assert calls[0] == ("GET", "/shops/123/orders/180157094927073", {})
    assert calls[1][0] == "PUT"
    assert calls[1][1] == "/shops/123/orders/180157094927073"
    assert calls[1][2]["note_print"] == "THA356_20260526_2"
    assert calls[1][2]["items"] == source_order["items"]
    assert calls[2] == ("GET", "/shops/123/orders/180157094927073", {})


def test_update_order_note_print_can_disable_safe_mode(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        del params
        calls.append((method, path, data or {}))
        return {"success": True}

    client._request = fake_request  # type: ignore[method-assign]
    payload = client.update_order_note_print(
        "180157094927073",
        "THA356_20260526_2",
        update_cfg={"safe_full_order_update": False},
    )
    assert payload["success"] is True
    assert calls == [
        (
            "PUT",
            "/shops/123/orders/180157094927073",
            {"note_print": "THA356_20260526_2"},
        )
    ]


def test_update_order_note_print_blocks_unsafe_extra_payload(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = PancakePosClient(settings=settings, logger=logging.getLogger("test"))

    def fake_request(method: str, path: str, *, params=None, data=None):  # noqa: ANN001
        del method, path, params, data
        return {"success": True, "order": {"id": 1, "items": [{"sku": "A", "quantity": 1}]}}

    client._request = fake_request  # type: ignore[method-assign]
    with pytest.raises(ValidationError):
        client.update_order_note_print(
            "180157094927073",
            "THA356_20260526_2",
            update_cfg={"extra_payload": {"status": 2}},
        )
