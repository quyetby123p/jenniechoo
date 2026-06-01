from __future__ import annotations

import base64
from datetime import date
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from app.settings import Settings
from app.exceptions import ValidationError
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import dump_json
import os
import requests


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


def test_fetch_history_fallbacks_to_csv_when_api_fails(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.reconcile_cod_import_history_dir.mkdir(parents=True, exist_ok=True)
    settings.reconcile_cod_import_detail_dir.mkdir(parents=True, exist_ok=True)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    csv_path = settings.reconcile_cod_import_history_dir / "history_cod_2026-05.csv"
    csv_path.write_text("Ngay tra tien COD,Ma khach hang\n2026-05-09,THA356\n", encoding="utf-8-sig")

    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "base_url": "https://example.com",
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {
                "enabled": True,
                "history_glob": "storage/reconcile_cod/imports/history/*.csv",
                "detail_glob": "storage/reconcile_cod/imports/detail/*.csv",
                "history_date_field": "Ngay tra tien COD",
                "detail_date_field": "Ngay doi soat",
            },
        },
    )

    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_history_api = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("api fail"))  # type: ignore[method-assign]
    rows, source = client.fetch_settlement_history(date(2026, 5, 1), date(2026, 5, 31))
    assert source == "csv"
    assert len(rows) == 1
    assert rows[0]["Ma khach hang"] == "THA356"


def test_fetch_history_prefers_api_when_available(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "base_url": "https://example.com",
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {
                "enabled": True,
                "history_glob": "storage/reconcile_cod/imports/history/*.csv",
                "detail_glob": "storage/reconcile_cod/imports/detail/*.csv",
            },
        },
    )
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_history_api = lambda *_args, **_kwargs: [{"settlement_date": "2026-05-09"}]  # type: ignore[method-assign]

    rows, source = client.fetch_settlement_history(date(2026, 5, 1), date(2026, 5, 31))
    assert source == "api"
    assert rows[0]["settlement_date"] == "2026-05-09"


def test_fetch_detail_filters_by_settlement_date_from_csv(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.reconcile_cod_import_history_dir.mkdir(parents=True, exist_ok=True)
    settings.reconcile_cod_import_detail_dir.mkdir(parents=True, exist_ok=True)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    detail_csv = settings.reconcile_cod_import_detail_dir / "detail_cod_2026-05-09.csv"
    detail_csv.write_text(
        "Ngay doi soat,Ma van don,Trang thai\n2026-05-09,TH1,Giao hang thanh cong\n2026-05-04,TH2,Hoan hang\n",
        encoding="utf-8-sig",
    )
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": False,
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {
                "enabled": True,
                "history_glob": "storage/reconcile_cod/imports/history/*.csv",
                "detail_glob": "storage/reconcile_cod/imports/detail/*.csv",
                "history_date_field": "Ngay tra tien COD",
                "detail_date_field": "Ngay doi soat",
            },
        },
    )

    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    rows, source = client.fetch_settlement_details(date(2026, 5, 9))
    assert source == "csv"
    assert len(rows) == 1
    assert rows[0]["Ma van don"] == "TH1"


def test_fetch_detail_fallbacks_to_csv_when_api_fails(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.reconcile_cod_import_detail_dir.mkdir(parents=True, exist_ok=True)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    detail_csv = settings.reconcile_cod_import_detail_dir / "detail_cod_2026-05-09.csv"
    detail_csv.write_text(
        "Ngay doi soat,Ma van don,Trang thai\n2026-05-09,TH1,Giao hang thanh cong\n",
        encoding="utf-8-sig",
    )
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "base_url": "https://example.com",
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {
                "enabled": True,
                "history_glob": "storage/reconcile_cod/imports/history/*.csv",
                "detail_glob": "storage/reconcile_cod/imports/detail/*.csv",
                "history_date_field": "Ngay tra tien COD",
                "detail_date_field": "Ngay doi soat",
            },
        },
    )
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_detail_api = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("api fail"))  # type: ignore[method-assign]

    rows, source = client.fetch_settlement_details(date(2026, 5, 9))
    assert source == "csv"
    assert len(rows) == 1
    assert rows[0]["Ma van don"] == "TH1"


def test_fetch_history_api_supports_has_next_page_path(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))

    calls: list[int] = []

    def fake_request_json(*, method, url, headers, params, data):  # noqa: ANN001
        del method, url, headers, data
        calls.append(int(params["page"]))
        page = int(params["page"])
        if page == 1:
            return {"data": {"data": [{"id": 1}], "hasNextPage": True}}
        return {"data": {"data": [{"id": 2}], "hasNextPage": False}}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    cfg = {
        "api": {
            "enabled": True,
            "base_url": "https://example.com",
            "history_endpoint": {
                "method": "GET",
                "path": "/api/v1/cash-flows/cod-histories",
                "result_path": "data.data",
                "start_date_param": "filters[validFrom]",
                "end_date_param": "filters[validTo]",
                "page_param": "page",
                "page_size_param": "limit",
                "page_size": 1,
                "has_next_page_path": "data.hasNextPage",
            },
            "detail_endpoint": {"path": "/unused"},
        }
    }

    rows = client._fetch_history_api(cfg, date(2026, 5, 1), date(2026, 5, 31))
    assert rows == [{"id": 1}, {"id": 2}]
    assert calls == [1, 2]


def test_fetch_detail_api_supports_json_body_request_mode(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))

    captured: dict[str, object] = {}

    def fake_request_json(*, method, url, headers, params, data):  # noqa: ANN001
        del headers
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["data"] = data
        return {"data": {"data": [{"shippingOrderCode": "TH123"}], "hasNextPage": False}}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    cfg = {
        "api": {
            "enabled": True,
            "base_url": "https://example.com",
            "history_endpoint": {"path": "/unused"},
            "detail_endpoint": {
                "method": "POST",
                "path": "/api/v1/orders/list",
                "request_mode": "json_body",
                "result_path": "data.data",
                "settlement_date_param": "filters.paymentCodDateFrom",
                "settlement_date_to_field": "filters.paymentCodDateTo",
                "page_field": "page",
                "page_size_field": "limit",
                "page_size": 200,
                "has_next_page_path": "data.hasNextPage",
                "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
            },
        }
    }

    rows = client._fetch_detail_api(cfg, date(2026, 5, 9), settlement={})
    assert rows == [{"shippingOrderCode": "TH123"}]
    assert captured["method"] == "POST"
    assert captured["params"] is None
    payload = captured["data"]
    assert isinstance(payload, dict)
    assert payload["page"] == 1
    assert payload["limit"] == 200
    assert payload["filters"]["paymentCodDateFrom"] == "2026-05-09"
    assert payload["filters"]["paymentCodDateTo"] == "2026-05-09"


def test_build_api_headers_keeps_bearer_spacing(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    old = os.environ.get("THAI_DUONG_API_TOKEN")
    os.environ["THAI_DUONG_API_TOKEN"] = "abc123"
    try:
        headers = client._build_api_headers(  # noqa: SLF001
            {
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
            }
        )
    finally:
        if old is None:
            del os.environ["THAI_DUONG_API_TOKEN"]
        else:
            os.environ["THAI_DUONG_API_TOKEN"] = old
    assert headers["Authorization"] == "Bearer abc123"


def test_fetch_history_raises_combined_error_when_api_and_csv_both_fail(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "base_url": "https://example.com",
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {
                "enabled": True,
                "history_glob": "storage/reconcile_cod/imports/history/*.csv",
                "detail_glob": "storage/reconcile_cod/imports/detail/*.csv",
            },
        },
    )
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_history_api = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValidationError("API Thai Duong loi (401): Unauthorized"))  # type: ignore[method-assign]

    try:
        client.fetch_settlement_history(date(2026, 5, 22), date(2026, 5, 22))
        assert False, "expected ValidationError"
    except ValidationError as exc:
        message = str(exc)
        assert "Khong co nguon du lieu lich su doi soat COD kha dung" in message
        assert "API: API Thai Duong loi (401): Unauthorized" in message
        assert "CSV: Khong tim thay file CSV doi soat theo pattern" in message


def test_find_orders_by_reference_for_sync_sets_filter_field(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request_json(*, method, url, headers, params, data):  # noqa: ANN001
        del method, url, headers, params
        captured["data"] = data
        return {"data": {"data": [{"orderUID": "PC001"}], "hasNextPage": False}}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    rows = client.find_orders_by_reference_for_sync(
        endpoint_cfg={
            "base_url": "https://example.com",
            "method": "POST",
            "path": "/api/v1/orders/list",
            "request_mode": "json_body",
            "page_field": "page",
            "page_size_field": "limit",
            "page_size": 50,
            "result_path": "data.data",
            "has_next_page_path": "data.hasNextPage",
            "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
        },
        reference_value="PC001",
        reference_filter_field="orderUID",
    )
    assert rows == [{"orderUID": "PC001"}]
    payload = captured["data"]
    assert isinstance(payload, dict)
    assert payload["filters"]["orderUID"] == "PC001"
    assert payload["searchText"] == "PC001"


def test_create_order_for_sync_uses_default_orders_endpoint(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    def fake_request_json(*, method, url, headers, params, data):  # noqa: ANN001
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["data"] = data
        del headers
        return {"ok": True}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    old_base = os.environ.get("THAI_DUONG_API_BASE_URL")
    old_token = os.environ.get("THAI_DUONG_API_TOKEN")
    os.environ["THAI_DUONG_API_BASE_URL"] = "https://example.com"
    os.environ["THAI_DUONG_API_TOKEN"] = "abc"
    try:
        result = client.create_order_for_sync(
            {"orderUID": "PC001"},
            endpoint_cfg={
                "base_url_env": "THAI_DUONG_API_BASE_URL",
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
                "method": "POST",
                "path": "/api/v1/orders",
            },
        )
    finally:
        if old_base is None:
            del os.environ["THAI_DUONG_API_BASE_URL"]
        else:
            os.environ["THAI_DUONG_API_BASE_URL"] = old_base
        if old_token is None:
            del os.environ["THAI_DUONG_API_TOKEN"]
        else:
            os.environ["THAI_DUONG_API_TOKEN"] = old_token

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.com/api/v1/orders"
    assert captured["params"] is None
    assert captured["data"] == {"orderUID": "PC001"}


def test_update_order_status_for_sync_uses_login_session(tmp_path: Path, monkeypatch) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    captured: dict[str, object] = {}

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self) -> dict[str, Any]:
            return dict(self._payload)

    class FakeCookieJar:
        def __init__(self) -> None:
            self._store = {"token": "session-token"}

        def get(self, key: str, default: str | None = None) -> str | None:
            return self._store.get(key, default)

    class FakeSession:
        def __init__(self) -> None:
            self.cookies = FakeCookieJar()

        def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: int) -> FakeResponse:  # noqa: A002
            captured["login_url"] = url
            captured["login_headers"] = headers
            captured["login_body"] = dict(json)
            captured["login_timeout"] = timeout
            return FakeResponse(200, {"statusCode": 200})

        def request(
            self,
            method: str,
            url: str,
            params: dict[str, Any] | None,
            json: dict[str, Any] | None,  # noqa: A002
            timeout: int,
        ) -> FakeResponse:
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            captured["body"] = dict(json or {})
            captured["timeout"] = timeout
            return FakeResponse(200, {"ok": True})

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(requests, "Session", lambda: FakeSession())
    monkeypatch.setenv("THAI_DUONG_AUTH_EMAIL", "THA356")
    monkeypatch.setenv("THAI_DUONG_AUTH_PASSWORD", "secret")
    monkeypatch.setenv("THAI_DUONG_AUTH_USERNAME", "THA356")

    result = client.update_order_status_for_sync(
        order_id="order_123",
        payload={"orderStatus": "SALE_CONFIRM", "isNeedSale": False},
        endpoint_cfg={
            "base_url": "https://example.com",
            "method": "PUT",
            "path": "/api/v1/orders/update-status-order/{order_id}",
            "use_session_login": True,
            "login_path": "/api/v1/auth/login",
        },
    )

    assert result == {"ok": True}
    assert captured["login_url"] == "https://example.com/api/v1/auth/login"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://example.com/api/v1/orders/update-status-order/order_123"
    assert captured["params"] is None
    assert captured["body"] == {"orderStatus": "SALE_CONFIRM", "isNeedSale": False}
    assert captured["closed"] is True


def _build_fake_jwt(exp_ts: int, iat_ts: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_ts, "iat": iat_ts}).encode("utf-8")
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_check_token_health_reports_ok_when_token_valid_and_probe_pass(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {"enabled": False},
        },
    )
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_history_api = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

    now_ts = int(datetime.now(timezone.utc).timestamp())
    old_token = os.environ.get("THAI_DUONG_API_TOKEN")
    os.environ["THAI_DUONG_API_TOKEN"] = _build_fake_jwt(exp_ts=now_ts + 24 * 3600, iat_ts=now_ts - 60)
    try:
        report = client.check_token_health()
    finally:
        if old_token is None:
            del os.environ["THAI_DUONG_API_TOKEN"]
        else:
            os.environ["THAI_DUONG_API_TOKEN"] = old_token

    assert report["ok"] is True
    assert report["configured"] is True
    assert int(report["token_exp_ts"]) > now_ts
    assert int(report["token_remaining_seconds"]) > 0
    assert report["api_probe"]["ok"] is True
    assert report["api_probe"]["skipped"] is False


def test_check_token_health_reports_fail_when_api_unauthorized(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    settings.config_root.mkdir(parents=True, exist_ok=True)
    dump_json(
        settings.reconcile_cod_source_config_path,
        {
            "api": {
                "enabled": True,
                "history_endpoint": {"path": "/history", "result_path": "data"},
                "detail_endpoint": {"path": "/detail", "result_path": "data"},
            },
            "csv": {"enabled": False},
        },
    )
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))
    client._fetch_history_api = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ValidationError("API Thai Duong loi (401): Unauthorized")
    )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    old_token = os.environ.get("THAI_DUONG_API_TOKEN")
    os.environ["THAI_DUONG_API_TOKEN"] = _build_fake_jwt(exp_ts=now_ts + 24 * 3600, iat_ts=now_ts - 60)
    try:
        report = client.check_token_health()
    finally:
        if old_token is None:
            del os.environ["THAI_DUONG_API_TOKEN"]
        else:
            os.environ["THAI_DUONG_API_TOKEN"] = old_token

    assert report["ok"] is False
    assert report["api_probe"]["ok"] is False
    assert "401" in str(report["api_probe"]["error"])


def test_build_api_headers_auto_renews_token_via_login(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))

    now_ts = int(datetime.now(timezone.utc).timestamp())
    expired_token = _build_fake_jwt(exp_ts=now_ts - 10, iat_ts=now_ts - 1000)
    old_values = {
        "THAI_DUONG_API_TOKEN": os.environ.get("THAI_DUONG_API_TOKEN"),
        "THAI_DUONG_AUTO_AUTH_ENABLED": os.environ.get("THAI_DUONG_AUTO_AUTH_ENABLED"),
        "THAI_DUONG_AUTH_EMAIL": os.environ.get("THAI_DUONG_AUTH_EMAIL"),
        "THAI_DUONG_AUTH_PASSWORD": os.environ.get("THAI_DUONG_AUTH_PASSWORD"),
        "THAI_DUONG_AUTH_REFRESH_TOKEN": os.environ.get("THAI_DUONG_AUTH_REFRESH_TOKEN"),
        "THAI_DUONG_AUTH_USERNAME": os.environ.get("THAI_DUONG_AUTH_USERNAME"),
        "THAI_DUONG_AUTO_AUTH_STATE_PATH": os.environ.get("THAI_DUONG_AUTO_AUTH_STATE_PATH"),
    }
    os.environ["THAI_DUONG_API_TOKEN"] = expired_token
    os.environ["THAI_DUONG_AUTO_AUTH_ENABLED"] = "1"
    os.environ["THAI_DUONG_AUTH_EMAIL"] = "THA356"
    os.environ["THAI_DUONG_AUTH_PASSWORD"] = "secret"
    os.environ["THAI_DUONG_AUTH_REFRESH_TOKEN"] = ""
    os.environ["THAI_DUONG_AUTH_USERNAME"] = "THA356"
    os.environ["THAI_DUONG_AUTO_AUTH_STATE_PATH"] = "storage/thai_duong_auth/test_state.json"

    def fake_request_auth_json(*, path, body, bearer_token="", cookie_header=""):  # noqa: ANN001
        del body, bearer_token, cookie_header
        if path == "/api/v1/auth/login":
            return {"data": {"accessToken": "token_after_login", "refreshToken": "refresh_after_login"}}
        return {}

    client._request_auth_json = fake_request_auth_json  # type: ignore[method-assign]
    try:
        headers = client._build_api_headers(
            {
                "token_env": "THAI_DUONG_API_TOKEN",
                "token_header": "Authorization",
                "token_prefix": "Bearer ",
            }
        )
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert headers["Authorization"] == "Bearer token_after_login"


def test_ensure_api_token_fresh_prefers_refresh_token(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    client = ThaiDuongCodClient(settings=settings, logger=logging.getLogger("test"))

    now_ts = int(datetime.now(timezone.utc).timestamp())
    soon_expired = _build_fake_jwt(exp_ts=now_ts + 30, iat_ts=now_ts - 1000)
    old_values = {
        "THAI_DUONG_API_TOKEN": os.environ.get("THAI_DUONG_API_TOKEN"),
        "THAI_DUONG_AUTO_AUTH_ENABLED": os.environ.get("THAI_DUONG_AUTO_AUTH_ENABLED"),
        "THAI_DUONG_AUTO_REFRESH_THRESHOLD_MINUTES": os.environ.get("THAI_DUONG_AUTO_REFRESH_THRESHOLD_MINUTES"),
        "THAI_DUONG_AUTH_REFRESH_TOKEN": os.environ.get("THAI_DUONG_AUTH_REFRESH_TOKEN"),
        "THAI_DUONG_AUTH_EMAIL": os.environ.get("THAI_DUONG_AUTH_EMAIL"),
        "THAI_DUONG_AUTH_PASSWORD": os.environ.get("THAI_DUONG_AUTH_PASSWORD"),
    }
    os.environ["THAI_DUONG_API_TOKEN"] = soon_expired
    os.environ["THAI_DUONG_AUTO_AUTH_ENABLED"] = "1"
    os.environ["THAI_DUONG_AUTO_REFRESH_THRESHOLD_MINUTES"] = "120"
    os.environ["THAI_DUONG_AUTH_REFRESH_TOKEN"] = "refresh_old"
    os.environ["THAI_DUONG_AUTH_EMAIL"] = ""
    os.environ["THAI_DUONG_AUTH_PASSWORD"] = ""

    calls: list[str] = []

    def fake_request_auth_json(*, path, body, bearer_token="", cookie_header=""):  # noqa: ANN001
        calls.append(f"{path}|{bearer_token}|{cookie_header}|{body}")
        if path == "/api/v1/auth/refresh":
            return {"access_token": "token_after_refresh", "refresh_token": "refresh_new"}
        return {}

    client._request_auth_json = fake_request_auth_json  # type: ignore[method-assign]
    try:
        report = client.ensure_api_token_fresh(force=True)
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert report["ok"] is True
    assert report["rotated"] is True
    assert report["method"] == "refresh"
    assert any("/api/v1/auth/refresh" in item for item in calls)
