from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request

from app.logger import configure_logger
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings, load_settings
from app.web_report_service import WebReportService


def create_app(
    *,
    settings: Settings | None = None,
    logger: logging.Logger | None = None,
    report_service: WebReportService | None = None,
) -> Flask:
    current_settings = settings or load_settings(project_root=Path(__file__).resolve().parents[1])
    current_logger = logger or configure_logger(
        current_settings.app_logs_dir,
        secrets=[
            current_settings.telegram_bot_token,
            current_settings.meta_access_token,
            current_settings.meta_page_access_token,
            current_settings.pancake_api_key,
            current_settings.pancake_access_token,
        ],
    )
    current_report_service = report_service or WebReportService(
        settings=current_settings,
        logger=current_logger,
        pancake_client=PancakePosClient(settings=current_settings, logger=current_logger),
    )

    app = Flask(
        __name__,
        template_folder=str(current_settings.project_root / "templates"),
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["REPORT_SETTINGS"] = current_settings
    app.config["REPORT_SERVICE"] = current_report_service
    app.config["REPORT_LOGGER"] = current_logger

    @app.get("/healthz")
    def healthz() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get("/api/v1/snapshot")
    def snapshot_api():  # type: ignore[no-untyped-def]
        target_date = _parse_query_date(request.args.get("date"), timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(target_date)
        return jsonify(snapshot)

    @app.get("/")
    def dashboard():  # type: ignore[no-untyped-def]
        target_date = _parse_query_date(request.args.get("date"), timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(target_date)
        return render_template(
            "web_report/dashboard.html",
            snapshot=snapshot,
            active_path="/",
            selected_date=target_date.isoformat(),
        )

    @app.get("/brand/<brand_slug>")
    def brand_detail(brand_slug: str):  # type: ignore[no-untyped-def]
        target_date = _parse_query_date(request.args.get("date"), timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(target_date)
        brand = snapshot.get("brand_detail", {}).get(brand_slug)
        if not isinstance(brand, dict):
            abort(404)
        return render_template(
            "web_report/brand_detail.html",
            snapshot=snapshot,
            brand=brand,
            active_path=f"/brand/{brand_slug}",
            selected_date=target_date.isoformat(),
        )

    @app.get("/status/<status_key>")
    def status_detail(status_key: str):  # type: ignore[no-untyped-def]
        target_date = _parse_query_date(request.args.get("date"), timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(target_date)
        status_map = snapshot.get("status_lists", {})
        if not isinstance(status_map, dict):
            abort(404)
        rows = status_map.get(status_key)
        if not isinstance(rows, list):
            abort(404)
        title_map = {
            "waiting": "Đơn chờ hàng",
            "pending-reconcile": "Đơn chờ đối soát",
        }
        return render_template(
            "web_report/status_detail.html",
            snapshot=snapshot,
            status_key=status_key,
            status_title=title_map.get(status_key, status_key),
            rows=rows,
            active_path=f"/status/{status_key}",
            selected_date=target_date.isoformat(),
        )

    @app.context_processor
    def inject_common() -> dict[str, Any]:
        return {
            "timezone_name": current_settings.app_timezone,
        }

    return app


def _parse_query_date(raw_value: str | None, *, timezone_name: str) -> date:
    if not raw_value:
        return datetime.now(_resolve_timezone(timezone_name)).date()
    value = str(raw_value).strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return datetime.now(_resolve_timezone(timezone_name)).date()


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")
