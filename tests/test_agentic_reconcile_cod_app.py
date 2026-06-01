from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
from typing import Any

from app.agentic_reconcile_cod_app import AgenticReconcileCodApp, AgenticReconcileCodRequest
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
        reconcile_cod_update_enabled=True,
        reconcile_cod_status_map_path="config/reconcile_cod_status_map.json",
        reconcile_cod_pancake_lookback_days=3650,
        reconcile_cod_sheet_enabled=True,
        reconcile_cod_sheet_mode="oauth_user",
        reconcile_cod_sheet_webhook_url="",
        reconcile_cod_sheet_webhook_secret="",
        reconcile_cod_sheet_webhook_timeout_seconds=30,
        reconcile_cod_sheet_spreadsheet_id="sheet_123",
        reconcile_cod_sheet_gid=1159924290,
        reconcile_cod_sheet_credentials_path="config/gsheet-sa.json",
        reconcile_cod_sheet_oauth_client_id="client",
        reconcile_cod_sheet_oauth_client_secret="secret",
        reconcile_cod_sheet_oauth_refresh_token="refresh",
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


class _FakeReconcileService:
    def __init__(self, report: dict[str, Any], apply_summary: dict[str, Any] | None = None) -> None:
        self.report = report
        self.apply_summary = apply_summary or {
            "ok": True,
            "updated": 0,
            "failed": 0,
            "skipped": 0,
            "transitioned": 0,
            "errors": [],
            "failed_orders": [],
        }
        self.apply_calls: list[str] = []

    def generate_report(self, settlement_date: date | None = None) -> dict[str, Any]:
        del settlement_date
        return dict(self.report)

    def apply_updates(self, run_id: str) -> dict[str, Any]:
        self.apply_calls.append(run_id)
        return dict(self.apply_summary)


class _FakeSheetService:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def sync_report(self, report: dict[str, Any]) -> dict[str, Any]:
        del report
        self.calls += 1
        return dict(self.result)


def _sample_report() -> dict[str, Any]:
    return {
        "ok": True,
        "settlement_date": "2026-05-22",
        "run_id": "run_2026-05-22_x1",
        "source_mode": "api",
        "detail_count": 4,
        "summary": {
            "matched_unique": 3,
            "already_correct": 1,
            "ambiguous": 0,
            "not_found": 0,
            "unmapped_status": 0,
            "update_candidates": 3,
            "total": 4,
        },
        "errors": {},
        "warnings": [],
        "records": [
            {
                "td_awb": "TH1",
                "td_status": "SUCCESS",
                "match_result": "matched_unique",
                "pancake_display_id": "JCT315",
                "pancake_order_id": "360300986571957",
                "reason": "Khớp duy nhất và sẵn sàng cập nhật.",
            }
        ],
    }


def test_agentic_app_auto_apply_and_sheet_sync_with_failed_codes(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path)
    report = _sample_report()
    apply_summary = {
        "ok": False,
        "updated": 2,
        "failed": 1,
        "skipped": 0,
        "transitioned": 1,
        "errors": ['360300986571957 (JCT315): Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm"}'],
        "failed_orders": [
            {
                "order_id": "360300986571957",
                "display_id": "JCT315",
                "awb": "TH1",
                "error": 'Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm"}',
            }
        ],
    }
    sheet_result = {"enabled": True, "ok": True, "attempted": 4, "inserted": 4, "skipped_existing": 0, "errors": []}
    reconcile = _FakeReconcileService(report=report, apply_summary=apply_summary)
    sheet = _FakeSheetService(result=sheet_result)
    app = AgenticReconcileCodApp(
        settings=settings,
        logger=logging.getLogger("test"),
        reconcile_service=reconcile,  # type: ignore[arg-type]
        reconcile_sheet_service=sheet,  # type: ignore[arg-type]
    )
    req = AgenticReconcileCodRequest(
        settlement_date=date(2026, 5, 22),
        apply_updates_policy="auto",
        sync_sheet_policy="auto",
        llm_judge_mode="off",
    )
    result = app.run(req)

    assert reconcile.apply_calls == ["run_2026-05-22_x1"]
    assert sheet.calls == 1
    assert result["judgment"]["verdict"] == "warning"
    assert result["failed_order_codes"] == ["JCT315 (360300986571957) | AWB:TH1"]
    assert result["ok"] is False


def test_agentic_app_respects_never_policy(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, reconcile_cod_update_enabled=True, reconcile_cod_sheet_enabled=True)
    report = _sample_report()
    reconcile = _FakeReconcileService(report=report)
    sheet = _FakeSheetService(result={"enabled": True, "ok": True})
    app = AgenticReconcileCodApp(
        settings=settings,
        logger=logging.getLogger("test"),
        reconcile_service=reconcile,  # type: ignore[arg-type]
        reconcile_sheet_service=sheet,  # type: ignore[arg-type]
    )
    req = AgenticReconcileCodRequest(apply_updates_policy="never", sync_sheet_policy="never", llm_judge_mode="off")
    result = app.run(req)

    assert reconcile.apply_calls == []
    assert sheet.calls == 0
    assert result["apply_summary"] is None
    assert result["sheet_sync"] is None
    assert result["ok"] is True


def test_agentic_app_writes_html_report(tmp_path: Path) -> None:
    settings = _dummy_settings(tmp_path, reconcile_cod_sheet_enabled=False)
    report = _sample_report()
    reconcile = _FakeReconcileService(report=report)
    app = AgenticReconcileCodApp(
        settings=settings,
        logger=logging.getLogger("test"),
        reconcile_service=reconcile,  # type: ignore[arg-type]
        reconcile_sheet_service=None,
    )
    html_path = tmp_path / "agentic_report.html"
    req = AgenticReconcileCodRequest(
        apply_updates_policy="never",
        sync_sheet_policy="never",
        llm_judge_mode="off",
        html_report_path=html_path,
    )
    result = app.run(req)

    assert result["html_report_path"] == str(html_path)
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "Agentic COD Reconcile Report" in content
    assert "run_2026-05-22_x1" in content
