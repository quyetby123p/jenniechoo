from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.settings import Settings


def _dummy_settings(tmp_path: Path, **overrides: Any) -> Settings:
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
        reconcile_cod_sheet_enabled=True,
        reconcile_cod_sheet_mode="service_account",
        reconcile_cod_sheet_webhook_url="",
        reconcile_cod_sheet_webhook_secret="",
        reconcile_cod_sheet_webhook_timeout_seconds=30,
        reconcile_cod_sheet_spreadsheet_id="sheet_123",
        reconcile_cod_sheet_gid=1034910254,
        reconcile_cod_sheet_credentials_path="config/gsheet-sa.json",
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


def test_sync_report_maps_b_to_ak_and_skips_existing(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(tmp_path)
    credentials_file = settings.reconcile_cod_sheet_credentials_file
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    credentials_file.write_text("{}", encoding="utf-8")

    report = {
        "settlement_date": "2026-05-09",
        "records": [
            {
                "td_awb": "TH35028N4TCP6B",
                "td_send_date": "2026-04-27T00:00:00.000Z",
                "td_detail_settlement_date": "2026-05-09T00:00:00.000Z",
                "td_status": "SUCCESS",
                "td_cod_minor": 280000,
                "td_sheet_cod_minor": 260000,
                "td_fee_minor": 3400,
                "td_remote_fee": 0,
                "td_refund_fee": 0,
                "td_cod_fee": 44.8,
                "td_insurance_fee": 0,
                "td_account_fee": 33.6,
                "td_hard_goods_fee": 0,
                "td_ffm_fee": 30,
                "td_confirm_trend_order_fee": 0,
                "td_confirm_hard_order_fee": 0,
                "td_mess_fee": 0,
                "td_mess_care_fee": 0,
                "td_telesale_care_fee": 0,
                "td_fulfillment_other_fee": 0,
                "td_ship_discount_fee": 0,
                "td_delivery_total": 78.8,
                "td_service_other_total": 63.6,
                "pancake_order_id": "9001",
                "pancake_display_id": "JCT001",
            },
            {
                "td_awb": "TH01288ND0802B1",
                "td_send_date": "2026-04-30",
                "td_detail_settlement_date": "2026-05-09",
                "td_status": "RETURNED",
                "td_cod_minor": 424000,
                "td_sheet_cod_minor": 0,
                "td_fee_minor": 2400,
                "pancake_order_id": "10853425701",
                "pancake_display_id": "JCT888",
            },
            {
                "td_awb": "TH33028N4TCR1C",
                "td_send_date": "2026-04-27",
                "td_detail_settlement_date": "2026-05-09",
                "td_status": "RETURNING",
                "td_cod_minor": 240000,
                "td_sheet_cod_minor": 0,
                "td_fee_minor": 3400,
                "pancake_order_id": "",
            },
        ],
    }

    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    captured_rows: list[list[Any]] = []
    captured_start_rows: list[int] = []

    monkeypatch.setattr(service, "_build_auth_headers", lambda *_args, **_kwargs: {"Authorization": "Bearer t"})
    monkeypatch.setattr(service, "_resolve_sheet_title", lambda *_args, **_kwargs: "COD")
    monkeypatch.setattr(service, "_load_existing_keys", lambda **_kwargs: {"TH01288ND0802B1|2026-05-09"})
    def _fake_find_row(**kwargs):  # noqa: ANN001
        assert kwargs["anchor_column"] == "Q"
        return 297

    monkeypatch.setattr(service, "_find_next_blank_row_from_anchor_column", _fake_find_row)
    def _capture_append(**kwargs):  # noqa: ANN001
        captured_rows.extend(kwargs["rows"])
        captured_start_rows.append(int(kwargs["start_row"]))

    monkeypatch.setattr(service, "_append_rows", _capture_append)

    result = service.sync_report(report)

    assert result["ok"] is True
    assert result["attempted"] == 3
    assert result["inserted"] == 2
    assert result["skipped_existing"] == 1
    assert len(captured_rows) == 2
    assert captured_start_rows == [297]

    first = captured_rows[0]
    assert len(first) == 36
    assert first[0] == "DA-TL.JE"  # B
    assert first[2] == "JCT001"  # D
    assert first[13] == "TH35028N4TCP6B"  # O
    assert first[14] == "2026-04-27"  # P
    assert first[15] == "2026-05-09"  # Q
    assert first[16] == "Giao hàng thành công"  # R
    assert first[17] == 2600  # S (COD theo bảng đối soát, không phải COD đơn hàng)
    assert first[18] == 34  # T
    assert first[19] == 0  # U
    assert first[20] == 0  # V
    assert first[21] == 44.8  # W
    assert first[22] == 0  # X
    assert first[23] == 33.6  # Y
    assert first[24] == 0  # Z
    assert first[25] == 30  # AA
    assert first[26] == 0  # AB
    assert first[27] == 0  # AC
    assert first[28] == 0  # AD
    assert first[29] == 0  # AE
    assert first[30] == 0  # AF
    assert first[31] == 0  # AG
    assert first[32] == 0  # AH
    assert first[33] == 78.8  # AI
    assert first[34] == 63.6  # AJ
    assert first[35] == 30  # AK

    second = captured_rows[1]
    assert second[0] == ""
    assert second[2] == ""
    assert second[16] == "Đang hoàn hàng"
    assert second[17] == 0


def test_sync_report_returns_structured_error_when_sync_fails(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(tmp_path)
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))

    def raise_impl(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_sync_report_impl", raise_impl)
    result = service.sync_report({"records": []})
    assert result["enabled"] is True
    assert result["ok"] is False
    assert result["errors"] == ["boom"]


def test_sync_report_disabled_returns_noop(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, reconcile_cod_sheet_enabled=False)
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    result = service.sync_report({"records": [{"td_awb": "A"}]})
    assert result["enabled"] is False
    assert result["ok"] is True
    assert result["inserted"] == 0


def test_sync_report_apps_script_mode(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(
        tmp_path,
        reconcile_cod_sheet_mode="apps_script",
        reconcile_cod_sheet_webhook_url="https://script.google.com/macros/s/abc/exec",
        reconcile_cod_sheet_webhook_secret="secret1",
    )
    report = {
        "run_id": "run_1",
        "settlement_date": "2026-05-09",
        "records": [
            {
                "td_awb": "THX",
                "td_send_date": "2026-05-01",
                "td_detail_settlement_date": "2026-05-09",
                "td_status": "SUCCESS",
                "td_cod_minor": 10000,
                "td_fee_minor": 1200,
                "pancake_order_id": "9001",
                "pancake_display_id": "JCT101",
            }
        ],
    }
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))

    def fake_webhook(url: str, *, headers: dict[str, str], data: dict[str, Any], timeout_seconds: int):  # noqa: ANN001
        assert url == "https://script.google.com/macros/s/abc/exec"
        assert headers["X-Reconcile-Secret"] == "secret1"
        assert timeout_seconds == 30
        rows = data.get("rows", [])
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["values"][0] == "DA-TL.JE"
        return {"ok": True, "inserted": 1, "skipped_existing": 0, "sheet_title": "COD"}

    monkeypatch.setattr(service, "_request_webhook_json", fake_webhook)
    result = service.sync_report(report)
    assert result["ok"] is True
    assert result["mode"] == "apps_script"
    assert result["inserted"] == 1
    assert result["skipped_existing"] == 0


def test_is_configured_apps_script_requires_url(tmp_path: Path) -> None:
    settings = _dummy_settings(
        tmp_path,
        reconcile_cod_sheet_mode="apps_script",
        reconcile_cod_sheet_webhook_url="",
    )
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    ok, reason = service.is_configured()
    assert ok is False
    assert "RECONCILE_COD_SHEET_WEBHOOK_URL" in reason


def test_sync_report_oauth_user_mode(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(
        tmp_path,
        reconcile_cod_sheet_mode="oauth_user",
        reconcile_cod_sheet_oauth_client_id="client_id_1",
        reconcile_cod_sheet_oauth_client_secret="client_secret_1",
        reconcile_cod_sheet_oauth_refresh_token="refresh_token_1",
    )
    report = {
        "settlement_date": "2026-05-09",
        "records": [
            {
                "td_awb": "TH-OAUTH-1",
                "td_send_date": "2026-05-01",
                "td_detail_settlement_date": "2026-05-09",
                "td_status": "SUCCESS",
                "td_cod_minor": 10000,
                "td_fee_minor": 1200,
                "pancake_order_id": "9001",
                "pancake_display_id": "JCT9001",
            }
        ],
    }

    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    captured_rows: list[list[Any]] = []

    monkeypatch.setattr(service, "_refresh_oauth_user_access_token", lambda: "oauth_access_token")
    monkeypatch.setattr(service, "_resolve_sheet_title", lambda *_args, **_kwargs: "COD")
    monkeypatch.setattr(service, "_load_existing_keys", lambda **_kwargs: set())
    def _fake_find_row(**kwargs):  # noqa: ANN001
        assert kwargs["anchor_column"] == "Q"
        return 297

    monkeypatch.setattr(service, "_find_next_blank_row_from_anchor_column", _fake_find_row)
    monkeypatch.setattr(service, "_append_rows", lambda **kwargs: captured_rows.extend(kwargs["rows"]))

    result = service.sync_report(report)
    assert result["ok"] is True
    assert result["mode"] == "oauth_user"
    assert result["inserted"] == 1
    assert result["skipped_existing"] == 0
    assert captured_rows and captured_rows[0][2] == "JCT9001"


def test_is_configured_oauth_user_requires_client_fields(tmp_path: Path) -> None:
    settings = _dummy_settings(
        tmp_path,
        reconcile_cod_sheet_mode="oauth_user",
        reconcile_cod_sheet_oauth_client_id="",
        reconcile_cod_sheet_oauth_client_secret="",
        reconcile_cod_sheet_oauth_refresh_token="",
    )
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    ok, reason = service.is_configured()
    assert ok is False
    assert "RECONCILE_COD_SHEET_OAUTH_CLIENT_ID" in reason


def test_find_next_blank_row_from_anchor_column_uses_last_non_empty(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(tmp_path)
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))

    def fake_request_json(*_args, **_kwargs):  # noqa: ANN001
        return {"values": [["x"], ["0"], [], ["ghi_chu"], []]}

    monkeypatch.setattr(service, "_request_json", fake_request_json)
    row = service._find_next_blank_row_from_anchor_column(
        spreadsheet_id="sheet_123",
        sheet_title="COD",
        headers={"Authorization": "Bearer t"},
        anchor_column="H",
        start_row=3,
    )
    assert row == 7


def test_merge_rows_with_existing_preserves_formula_cells(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    rows = [[None] * 36]
    rows[0][0] = "DA-TL.JE"
    rows[0][2] = "JCT001"
    rows[0][13] = "THX"
    rows[0][17] = 2800

    existing = [[f"=COL_{idx}" for idx in range(36)]]
    merged = service._merge_rows_with_existing(rows, existing_rows=existing)
    assert len(merged) == 1
    assert merged[0][0] == "DA-TL.JE"
    assert merged[0][2] == "JCT001"
    assert merged[0][13] == "THX"
    assert merged[0][17] == 2800
    assert merged[0][1] == "=COL_1"
    assert merged[0][35] == "=COL_35"


def test_append_rows_does_not_write_column_l(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    settings = _dummy_settings(tmp_path)
    service = ReconcileCodSheetService(settings=settings, logger=logging.getLogger("test"))
    captured_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        service,
        "_load_existing_rows_for_block",
        lambda **_kwargs: [["" for _ in range(36)]],
    )

    def fake_request_json(method: str, url: str, *, headers: dict[str, str], params=None, data=None):  # noqa: ANN001
        captured_calls.append(
            {
                "method": method,
                "url": url,
                "decoded_url": unquote(url),
                "headers": headers,
                "params": params or {},
                "data": data or {},
            }
        )
        return {}

    monkeypatch.setattr(service, "_request_json", fake_request_json)
    source_row = [f"v{idx}" for idx in range(36)]
    service._append_rows(
        spreadsheet_id="sheet_123",
        sheet_title="COD",
        headers={"Authorization": "Bearer t"},
        rows=[source_row],
        start_row=3,
    )

    assert len(captured_calls) == 2
    first = captured_calls[0]
    second = captured_calls[1]
    assert "B3:K3" in first["decoded_url"]
    assert "M3:AK3" in second["decoded_url"]
    first_values = first["data"]["values"][0]
    second_values = second["data"]["values"][0]
    assert len(first_values) == 10
    assert len(second_values) == 25
    assert first_values == source_row[:10]
    assert second_values == source_row[11:36]
