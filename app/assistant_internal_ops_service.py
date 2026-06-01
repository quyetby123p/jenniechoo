from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from pathlib import Path
from typing import Any

from app.daily_report_service import DailyReportService
from app.media_settings import load_media_settings
from app.media_sheet_service import MediaSheetService
from app.media_storage_service import MediaStorageService
from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.settings import load_settings
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import load_json, now_utc_iso


class AssistantInternalOpsService:
    def __init__(self, project_root: Path, logger: logging.Logger) -> None:
        self.project_root = project_root
        self.logger = logger

    def generate_daily_report(self, report_date: date | None = None) -> dict[str, Any]:
        settings = load_settings(project_root=self.project_root)
        meta = MetaAdsClient(settings=settings, logger=self.logger)
        pancake = PancakePosClient(settings=settings, logger=self.logger)
        service = DailyReportService(
            settings=settings,
            logger=self.logger,
            pancake_client=pancake,
            meta_client=meta,
        )
        payload = service.generate_report(report_date=report_date)
        return payload if isinstance(payload, dict) else {"ok": False, "errors": {"runtime": "payload invalid"}}

    def generate_reconcile_cod_report(self, settlement_date: date | None = None) -> dict[str, Any]:
        settings = load_settings(project_root=self.project_root)
        pancake = PancakePosClient(settings=settings, logger=self.logger)
        thai_duong = ThaiDuongCodClient(settings=settings, logger=self.logger)
        service = ReconcileCodService(
            settings=settings,
            logger=self.logger,
            pancake_client=pancake,
            thai_duong_client=thai_duong,
        )
        payload = service.generate_report(settlement_date=settlement_date)
        return payload if isinstance(payload, dict) else {"ok": False, "errors": {"runtime": "payload invalid"}}

    def sync_reconcile_sheet(self, run_id: str) -> dict[str, Any]:
        settings = load_settings(project_root=self.project_root)
        run_path = settings.reconcile_cod_runs_dir / f"{run_id}.json"
        if not run_path.exists():
            return {"ok": False, "errors": [f"Không tìm thấy run đối soát: {run_id}"]}
        payload = load_json(run_path)
        if not isinstance(payload, dict):
            return {"ok": False, "errors": [f"Dữ liệu run {run_id} không hợp lệ."]}
        service = ReconcileCodSheetService(settings=settings, logger=self.logger)
        result = service.sync_report(payload)
        return result if isinstance(result, dict) else {"ok": False, "errors": ["sync_report trả dữ liệu không hợp lệ."]}

    def sync_media_sheet(self, run_id: str) -> dict[str, Any]:
        settings = load_media_settings(project_root=self.project_root)
        storage = MediaStorageService(settings=settings, logger=self.logger)
        run_payload = storage.load_run(run_id)
        if not run_payload:
            return {"ok": False, "errors": [f"Không tìm thấy media run: {run_id}"]}
        rows = run_payload.get("items", []) if isinstance(run_payload.get("items"), list) else []
        service = MediaSheetService(settings=settings, logger=self.logger)
        result = service.sync_rows(rows)
        return result if isinstance(result, dict) else {"ok": False, "errors": ["sync_rows trả dữ liệu không hợp lệ."]}

    def collect_result_snapshot(self, target_date: date | None = None) -> dict[str, Any]:
        day = target_date or datetime.now(timezone.utc).date()
        day_key = day.isoformat()
        payload: dict[str, Any] = {
            "report_date": day_key,
            "generated_at": now_utc_iso(),
            "daily_report": self._load_daily_report(day_key),
            "reconcile": self._load_reconcile_reports(day_key),
            "media": self._load_media_runs(day_key),
        }
        return payload

    def _load_daily_report(self, day_key: str) -> dict[str, Any]:
        root = self.project_root / "storage" / "reports"
        candidates = [
            root / "daily" / f"report_{day_key}.json",
            root / "errors" / f"report_{day_key}.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            payload = load_json(path)
            if isinstance(payload, dict):
                return payload
        return {}

    def _load_reconcile_reports(self, day_key: str) -> dict[str, Any]:
        root = self.project_root / "storage" / "reconcile_cod"
        reports_dir = root / "reports"
        if not reports_dir.exists():
            return {}
        target_prefix = f"reconcile_{day_key}_"
        matches = sorted(reports_dir.glob(f"{target_prefix}*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not matches:
            return {}
        payload = load_json(matches[0])
        return payload if isinstance(payload, dict) else {}

    def _load_media_runs(self, day_key: str) -> dict[str, Any]:
        runs_dir = self.project_root / "storage" / "media_research" / "runs"
        if not runs_dir.exists():
            return {"count": 0, "latest": []}

        latest: list[dict[str, Any]] = []
        for path in sorted(runs_dir.glob("run_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            payload = load_json(path)
            if not isinstance(payload, dict):
                continue
            created_at = str(payload.get("created_at", "")).strip()
            if created_at and not created_at.startswith(day_key):
                # Keep scanning: run timestamp is UTC; allow date mismatch around midnight by checking fallback.
                try:
                    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date().isoformat()
                except ValueError:
                    parsed = ""
                if parsed and parsed != day_key:
                    continue
            latest.append(
                {
                    "run_id": str(payload.get("run_id", "")),
                    "status": str(payload.get("status", "")),
                    "selected_count": _to_int(payload.get("summary", {}).get("selected_count") if isinstance(payload.get("summary"), dict) else 0),
                    "product_code": str(payload.get("product_code", "")),
                }
            )
            if len(latest) >= 10:
                break
        return {"count": len(latest), "latest": latest}


def _to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
