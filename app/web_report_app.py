from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request

from app.logger import configure_logger
from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings, load_settings
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.web_report_service import WebReportService


REPORT_BASELINE_DATE = date(2026, 2, 1)


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
        meta_client=MetaAdsClient(settings=current_settings, logger=current_logger),
        thai_duong_client=ThaiDuongCodClient(settings=current_settings, logger=current_logger),
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
        missing_overview = _build_missing_overview(snapshot)
        today = datetime.now(_resolve_timezone(current_settings.app_timezone)).date()
        today_snapshot = current_report_service.get_snapshot(today, today)
        overall_start = min(REPORT_BASELINE_DATE, today)
        overall_snapshot = current_report_service.get_snapshot(overall_start, today)
        summary_cards = {
            "closed_today": int((today_snapshot.get("metrics") or {}).get("closed_orders") or 0),
            "revenue_today_thb_text": str((today_snapshot.get("metrics") or {}).get("revenue_total_thb_text") or "0"),
            "revenue_today_vnd_text": str((today_snapshot.get("metrics") or {}).get("revenue_total_vnd_text") or "0"),
            "ads_spend_today_vnd_text": str((today_snapshot.get("metrics") or {}).get("ads_spend_vnd_text") or "0"),
            "roas_today_text": str((today_snapshot.get("metrics") or {}).get("roas_text") or "0.00x"),
            "waiting_total": int((overall_snapshot.get("metrics") or {}).get("waiting_orders") or 0),
            "shipping_total": int((overall_snapshot.get("metrics") or {}).get("shipping_orders") or 0),
            "pending_reconcile_total": int((overall_snapshot.get("metrics") or {}).get("pending_reconcile_orders") or 0),
        }
        return render_template(
            "web_report/dashboard.html",
            snapshot=snapshot,
            missing_overview=missing_overview,
            summary_cards=summary_cards,
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
        metrics = snapshot.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        title_map = {
            "waiting": "Đơn chờ hàng",
            "shipping": "Đơn đang gửi",
            "pending-reconcile": "Đơn chờ đối soát",
            "reconcile-received": "Đơn đối soát đã nhận",
            "returning": "Đơn hoàn / đang hoàn",
        }
        summary_map = {
            "waiting": {
                "orders": int(metrics.get("waiting_orders") or 0),
                "value_thb_text": str(metrics.get("waiting_value_thb_text") or "0"),
                "value_vnd_text": str(metrics.get("waiting_value_vnd_text") or "0"),
            },
            "shipping": {
                "orders": int(metrics.get("shipping_orders") or 0),
                "value_thb_text": str(metrics.get("shipping_value_thb_text") or "0"),
                "value_vnd_text": str(metrics.get("shipping_value_vnd_text") or "0"),
            },
            "reconcile-received": {
                "orders": int(metrics.get("reconcile_received_orders") or 0),
                "value_thb_text": str(metrics.get("reconcile_received_value_thb_text") or "0"),
                "value_vnd_text": str(metrics.get("reconcile_received_value_vnd_text") or "0"),
            },
            "returning": {
                "orders": int(metrics.get("returning_orders") or 0),
                "value_thb_text": str(metrics.get("returning_value_thb_text") or "0"),
                "value_vnd_text": str(metrics.get("returning_value_vnd_text") or "0"),
            },
            "pending-reconcile": {
                "orders": int(metrics.get("pending_reconcile_orders") or 0),
                "value_thb_text": str(metrics.get("pending_reconcile_value_thb_text") or "0"),
                "value_vnd_text": str(metrics.get("pending_reconcile_value_vnd_text") or "0"),
            },
        }
        status_summary = summary_map.get(
            status_key,
            {
                "orders": len(rows),
                "value_thb_text": "0",
                "value_vnd_text": "0",
            },
        )
        return render_template(
            "web_report/status_detail.html",
            snapshot=snapshot,
            status_key=status_key,
            status_title=title_map.get(status_key, status_key),
            status_summary=status_summary,
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


def _build_missing_product_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    brand_detail = snapshot.get("brand_detail")
    if not isinstance(brand_detail, dict):
        return []

    quantities_by_key: dict[tuple[str, str, str], int] = {}
    for brand_data in brand_detail.values():
        if not isinstance(brand_data, dict):
            continue
        sku_rows = brand_data.get("sku_rows")
        if not isinstance(sku_rows, list):
            continue
        for sku_row in sku_rows:
            if not isinstance(sku_row, dict):
                continue
            sku = str(sku_row.get("sku", "")).strip() or "UNKNOWN"
            color = str(sku_row.get("color", "")).strip() or "Khác"
            sizes = sku_row.get("sizes")
            if isinstance(sizes, dict) and sizes:
                for raw_size, raw_quantity in sizes.items():
                    size = str(raw_size).strip() or "Khác"
                    quantity = _to_positive_int(raw_quantity)
                    if quantity <= 0:
                        continue
                    key = (sku, color, size)
                    quantities_by_key[key] = quantities_by_key.get(key, 0) + quantity
                continue
            quantity = _to_positive_int(sku_row.get("missing_quantity"))
            if quantity <= 0:
                continue
            key = (sku, color, "Khác")
            quantities_by_key[key] = quantities_by_key.get(key, 0) + quantity

    rows = [
        {
            "sku": sku,
            "color": color,
            "size": size,
            "quantity": quantity,
        }
        for (sku, color, size), quantity in quantities_by_key.items()
        if quantity > 0
    ]
    rows.sort(key=lambda item: (-int(item.get("quantity") or 0), str(item.get("sku", "")), str(item.get("size", ""))))
    return rows


def _build_missing_overview(snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    size_summary = snapshot.get("size_summary")
    if not isinstance(size_summary, list):
        size_summary = []
    product_rows = _build_missing_product_rows(snapshot)

    missing_skus = sorted(
        {
            str(item.get("sku", "")).strip()
            for item in product_rows
            if isinstance(item, dict) and str(item.get("sku", "")).strip()
        }
    )
    normalized_size_rows = [
        {
            "size": str(item.get("size", "")).strip() or "Khác",
            "quantity": _to_positive_int(item.get("quantity")),
        }
        for item in size_summary
        if isinstance(item, dict)
    ]
    normalized_size_rows = [item for item in normalized_size_rows if item["quantity"] > 0]
    normalized_size_rows.sort(key=lambda item: (-item["quantity"], item["size"]))
    size_total_quantity = sum(item["quantity"] for item in normalized_size_rows)

    return {
        "missing_order_count": _to_positive_int(metrics.get("waiting_orders")),
        "missing_skus": missing_skus,
        "missing_sku_count": len(missing_skus),
        "size_rows": normalized_size_rows,
        "size_count": len(normalized_size_rows),
        "size_total_quantity": size_total_quantity,
    }


def _to_positive_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        number = int(float(str(value).replace(",", "").strip()))
        return max(0, number)
    except (TypeError, ValueError):
        return 0


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")
