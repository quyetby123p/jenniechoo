from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
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
        period = _parse_query_period(request.args, timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(period["start_date"], period["end_date"])
        return jsonify(snapshot)

    @app.get("/")
    def dashboard():  # type: ignore[no-untyped-def]
        period = _parse_query_period(request.args, timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(period["start_date"], period["end_date"])
        return render_template(
            "web_report/dashboard.html",
            snapshot=snapshot,
            active_path="/",
            selected_mode=period["mode"],
            selected_date=period["date_text"],
            selected_start_date=period["start_text"],
            selected_end_date=period["end_text"],
            query_string=period["query_string"],
        )

    @app.get("/brand/<brand_slug>")
    def brand_detail(brand_slug: str):  # type: ignore[no-untyped-def]
        period = _parse_query_period(request.args, timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(period["start_date"], period["end_date"])
        brand = snapshot.get("brand_detail", {}).get(brand_slug)
        if not isinstance(brand, dict):
            abort(404)
        return render_template(
            "web_report/brand_detail.html",
            snapshot=snapshot,
            brand=brand,
            active_path=f"/brand/{brand_slug}",
            selected_mode=period["mode"],
            selected_date=period["date_text"],
            selected_start_date=period["start_text"],
            selected_end_date=period["end_text"],
            query_string=period["query_string"],
        )

    @app.get("/status/<status_key>")
    def status_detail(status_key: str):  # type: ignore[no-untyped-def]
        period = _parse_query_period(request.args, timezone_name=current_settings.app_timezone)
        snapshot = current_report_service.get_snapshot(period["start_date"], period["end_date"])
        status_map = snapshot.get("status_lists", {})
        if not isinstance(status_map, dict):
            abort(404)
        rows = status_map.get(status_key)
        if not isinstance(rows, list):
            abort(404)
        title_map = {
            "waiting": "Đơn chờ hàng",
            "shipping": "Đơn đang gửi",
            "pending-reconcile": "Đơn chờ đối soát",
            "reconcile-received": "Đơn đối soát đã nhận",
            "returning": "Đơn hoàn / đang hoàn",
        }
        return render_template(
            "web_report/status_detail.html",
            snapshot=snapshot,
            status_key=status_key,
            status_title=title_map.get(status_key, status_key),
            rows=rows,
            active_path=f"/status/{status_key}",
            selected_mode=period["mode"],
            selected_date=period["date_text"],
            selected_start_date=period["start_text"],
            selected_end_date=period["end_text"],
            query_string=period["query_string"],
        )

    @app.context_processor
    def inject_common() -> dict[str, Any]:
        return {
            "timezone_name": current_settings.app_timezone,
            "thb_to_vnd_rate": current_settings.report_thb_to_vnd_rate,
        }

    return app


def _parse_query_period(args: Any, *, timezone_name: str) -> dict[str, Any]:
    tz = _resolve_timezone(timezone_name)
    today = datetime.now(tz).date()
    mode = str(args.get("mode", "")).strip().lower()
    date_raw = str(args.get("date", "")).strip()
    start_raw = str(args.get("start_date", "")).strip()
    end_raw = str(args.get("end_date", "")).strip()

    if mode == "today":
        start_date = today
        end_date = today
        mode = "today"
    elif mode == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
        mode = "yesterday"
    elif mode == "last7":
        start_date = today - timedelta(days=6)
        end_date = today
        mode = "last7"
    elif mode == "last30":
        start_date = today - timedelta(days=29)
        end_date = today
        mode = "last30"
    elif mode == "last90":
        start_date = today - timedelta(days=89)
        end_date = today
        mode = "last90"
    elif mode == "lastmonth":
        month_start = today.replace(day=1)
        end_date = month_start - timedelta(days=1)
        start_date = end_date.replace(day=1)
        mode = "lastmonth"
    elif mode == "week_to_date":
        start_date = today - timedelta(days=today.weekday())
        end_date = today
        mode = "week_to_date"
    elif mode == "month_to_date":
        start_date = today.replace(day=1)
        end_date = today
        mode = "month_to_date"
    elif mode == "range" or (not mode and start_raw and end_raw):
        start_date = _parse_iso_date(start_raw, fallback=today)
        end_date = _parse_iso_date(end_raw, fallback=start_date)
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        mode = "range"
    else:
        start_date = _parse_iso_date(date_raw, fallback=today)
        end_date = start_date
        mode = "date"

    query_params = {
        "mode": mode,
        "date": start_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    return {
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "date_text": start_date.isoformat(),
        "start_text": start_date.isoformat(),
        "end_text": end_date.isoformat(),
        "query_string": urlencode(query_params),
    }


def _parse_iso_date(raw_value: str | None, *, fallback: date) -> date:
    value = str(raw_value or "").strip()
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")
