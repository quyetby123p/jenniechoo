from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.meta_ads_client import MetaAdsClient
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.utils import load_json, now_utc_iso


class WebReportService:
    """Aggregate order/reporting data for the web dashboard."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        pancake_client: PancakePosClient,
        meta_client: MetaAdsClient | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.pancake = pancake_client
        self.meta = meta_client
        self._cache_lock = Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def get_snapshot(self, start_date: date, end_date: date | None = None) -> dict[str, Any]:
        period_start, period_end = self._normalize_period(start_date, end_date)
        key = f"{period_start.isoformat()}::{period_end.isoformat()}"
        now_ts = time.time()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_ts, payload = cached
                if now_ts - cached_ts < float(self.settings.web_report_refresh_seconds):
                    return payload

        payload = self._build_snapshot(period_start, period_end)
        with self._cache_lock:
            self._cache[key] = (time.time(), payload)
        return payload

    @staticmethod
    def _normalize_period(start_date: date, end_date: date | None) -> tuple[date, date]:
        if end_date is None:
            return start_date, start_date
        if end_date < start_date:
            return end_date, start_date
        return start_date, end_date

    def _build_snapshot(self, start_date: date, end_date: date) -> dict[str, Any]:
        tz = self._resolve_timezone()
        status_cfg = self._load_status_map_config()
        waiting_codes = self._to_int_set(status_cfg.get("waiting_status_codes", []))
        waiting_labels = self._to_text_set(status_cfg.get("waiting_status_labels", ["chờ hàng", "cho hang"]))
        closed_codes = self._to_int_set(status_cfg.get("closed_status_codes", []))
        closed_labels = self._to_text_set(status_cfg.get("closed_status_labels", []))
        returning_codes = self._to_int_set(status_cfg.get("returning_status_codes", []))
        returning_labels = self._to_text_set(status_cfg.get("returning_status_labels", ["đang hoàn", "hoàn"]))
        shipping_codes = self._to_int_set(status_cfg.get("shipping_status_codes", [2]))
        shipping_labels = self._to_text_set(status_cfg.get("shipping_status_labels", ["đã gửi hàng", "dang giao"]))
        status_code_labels = self._build_status_code_label_map(status_cfg)
        brand_rules = status_cfg.get("brand_rules", [])

        orders, aggs = self._fetch_orders_and_aggs(start_date=start_date, end_date=end_date)
        if not isinstance(orders, list):
            orders = []
        orders = [item for item in orders if isinstance(item, dict)]

        total_orders = 0
        closed_orders = 0
        waiting_orders_count = 0
        returning_orders_count = 0
        shipping_orders_count = 0
        waiting_value_minor = 0
        returning_value_minor = 0
        shipping_value_minor = 0
        order_value_total_minor = 0
        missing_line_count = 0
        missing_quantity = 0
        size_totals: dict[str, int] = {}
        missing_products: set[str] = set()
        order_value_minor_by_ref: dict[str, int] = {}

        waiting_orders: list[dict[str, Any]] = []
        returning_orders: list[dict[str, Any]] = []
        shipping_orders: list[dict[str, Any]] = []
        brands: dict[str, dict[str, Any]] = {}

        for order in orders:
            total_orders += 1

            order_ref = self._extract_order_ref(order)
            order_created_dt = self._extract_order_datetime(order, tz=tz)
            order_status_code = self._extract_status_code(order)
            order_status_label = self._extract_status_label(
                order,
                status_code=order_status_code,
                status_code_labels=status_code_labels,
            )
            order_total_minor = self._extract_order_total_minor(order)
            order_value_total_minor += order_total_minor
            normalized_order_ref = self._normalize_text(order_ref)
            if normalized_order_ref:
                existing_value = self._to_int(order_value_minor_by_ref.get(normalized_order_ref))
                if order_total_minor > existing_value:
                    order_value_minor_by_ref[normalized_order_ref] = order_total_minor
            brand_name, brand_slug = self._extract_brand(order, brand_rules=brand_rules)
            is_waiting = self._is_waiting_status(
                code=order_status_code,
                label=order_status_label,
                waiting_codes=waiting_codes,
                waiting_labels=waiting_labels,
            )
            is_returning = self._is_returning_status(
                code=order_status_code,
                label=order_status_label,
                returning_codes=returning_codes,
                returning_labels=returning_labels,
            )
            is_shipping = self._is_shipping_status(
                code=order_status_code,
                label=order_status_label,
                shipping_codes=shipping_codes,
                shipping_labels=shipping_labels,
            )
            is_closed = self._is_closed_status(
                code=order_status_code,
                label=order_status_label,
                closed_codes=closed_codes,
                closed_labels=closed_labels,
                is_waiting=is_waiting,
                is_returning=is_returning,
            )
            if is_closed:
                closed_orders += 1
            if is_returning:
                returning_orders_count += 1
                returning_value_minor += order_total_minor
                returning_orders.append(
                    {
                        "order_ref": order_ref,
                        "brand_name": brand_name,
                        "brand_slug": brand_slug,
                        "status_code": order_status_code,
                        "status_label": order_status_label,
                        "created_at": self._format_dt(order_created_dt, tz=tz),
                        "created_ts": self._to_ts(order_created_dt),
                        "order_total_minor": order_total_minor,
                        "order_total_text": self._fmt_currency(order_total_minor),
                    }
                )
            if is_shipping:
                shipping_orders_count += 1
                shipping_value_minor += order_total_minor
                shipping_orders.append(
                    {
                        "order_ref": order_ref,
                        "brand_name": brand_name,
                        "brand_slug": brand_slug,
                        "status_code": order_status_code,
                        "status_label": order_status_label,
                        "created_at": self._format_dt(order_created_dt, tz=tz),
                        "created_ts": self._to_ts(order_created_dt),
                        "order_total_minor": order_total_minor,
                        "order_total_text": self._fmt_currency(order_total_minor),
                    }
                )

            brand_bucket = brands.setdefault(
                brand_slug,
                {
                    "brand_name": brand_name,
                    "brand_slug": brand_slug,
                    "total_orders": 0,
                    "closed_orders": 0,
                    "total_value_minor": 0,
                    "waiting_orders": 0,
                    "waiting_value_minor": 0,
                    "missing_line_count": 0,
                    "missing_quantity": 0,
                    "size_totals": {},
                    "sku_rows": {},
                },
            )
            brand_bucket["total_orders"] = self._to_int(brand_bucket.get("total_orders")) + 1
            brand_bucket["total_value_minor"] = self._to_int(brand_bucket.get("total_value_minor")) + order_total_minor
            if is_closed:
                brand_bucket["closed_orders"] = self._to_int(brand_bucket.get("closed_orders")) + 1
            if not is_waiting:
                continue

            waiting_orders_count += 1
            waiting_value_minor += order_total_minor
            brand_bucket["waiting_orders"] = self._to_int(brand_bucket.get("waiting_orders")) + 1
            brand_bucket["waiting_value_minor"] = self._to_int(brand_bucket.get("waiting_value_minor")) + order_total_minor

            order_items = order.get("items", [])
            if not isinstance(order_items, list):
                order_items = []

            line_skus: list[str] = []
            for item in order_items:
                if not isinstance(item, dict):
                    continue
                quantity = max(1, self._to_int(item.get("quantity"), fallback=1))
                size = self._extract_item_size(item)
                color = self._extract_item_color(item)
                sku = self._extract_item_sku(item)
                unit_price_minor = self._extract_item_unit_price_minor(item)
                line_value_minor = max(0, quantity * unit_price_minor)
                normalized_sku = sku or "UNKNOWN"
                line_skus.append(normalized_sku)
                missing_products.add(normalized_sku)

                missing_line_count += 1
                missing_quantity += quantity
                size_totals[size] = self._to_int(size_totals.get(size)) + quantity

                brand_bucket["missing_line_count"] = self._to_int(brand_bucket.get("missing_line_count")) + 1
                brand_bucket["missing_quantity"] = self._to_int(brand_bucket.get("missing_quantity")) + quantity
                brand_sizes = brand_bucket.get("size_totals")
                if not isinstance(brand_sizes, dict):
                    brand_sizes = {}
                    brand_bucket["size_totals"] = brand_sizes
                brand_sizes[size] = self._to_int(brand_sizes.get(size)) + quantity

                row_key = f"{normalized_sku}|{color}"
                sku_rows = brand_bucket.get("sku_rows")
                if not isinstance(sku_rows, dict):
                    sku_rows = {}
                    brand_bucket["sku_rows"] = sku_rows
                row = sku_rows.get(row_key)
                if not isinstance(row, dict):
                    row = {
                        "sku": normalized_sku,
                        "color": color,
                        "sizes": {},
                        "missing_line_count": 0,
                        "missing_quantity": 0,
                        "value_minor": 0,
                        "order_refs": [],
                    }
                    sku_rows[row_key] = row
                row["missing_line_count"] = self._to_int(row.get("missing_line_count")) + 1
                row["missing_quantity"] = self._to_int(row.get("missing_quantity")) + quantity
                row["value_minor"] = self._to_int(row.get("value_minor")) + line_value_minor
                row_sizes = row.get("sizes")
                if not isinstance(row_sizes, dict):
                    row_sizes = {}
                    row["sizes"] = row_sizes
                row_sizes[size] = self._to_int(row_sizes.get(size)) + quantity
                refs = row.get("order_refs")
                if not isinstance(refs, list):
                    refs = []
                    row["order_refs"] = refs
                if order_ref and order_ref not in refs:
                    refs.append(order_ref)

            waiting_orders.append(
                {
                    "order_ref": order_ref,
                    "brand_name": brand_name,
                    "brand_slug": brand_slug,
                    "status_code": order_status_code,
                    "status_label": order_status_label,
                    "created_at": self._format_dt(order_created_dt, tz=tz),
                    "created_ts": self._to_ts(order_created_dt),
                    "order_total_minor": order_total_minor,
                    "order_total_text": self._fmt_currency(order_total_minor),
                    "item_count": len(order_items),
                    "missing_skus": sorted(set(line_skus)),
                }
            )

        waiting_orders.sort(key=lambda item: self._to_int(item.get("created_ts")), reverse=True)
        returning_orders.sort(key=lambda item: self._to_int(item.get("created_ts")), reverse=True)
        shipping_orders.sort(key=lambda item: self._to_int(item.get("created_ts")), reverse=True)
        reconcile_summary = self._load_reconcile_period_summary(
            start_date=start_date,
            end_date=end_date,
            status_cfg=status_cfg,
        )
        pending_reconcile = reconcile_summary.get("pending_rows", [])
        reconcile_received = reconcile_summary.get("received_rows", [])
        pending_reconcile_orders = self._count_unique_reconcile_order_refs(pending_reconcile)
        reconcile_received_orders = self._count_unique_reconcile_order_refs(reconcile_received)
        pending_reconcile_value_minor = self._sum_reconcile_rows_value_minor(
            pending_reconcile,
            order_value_minor_by_ref=order_value_minor_by_ref,
        )
        reconcile_received_value_minor = self._sum_reconcile_rows_value_minor(
            reconcile_received,
            order_value_minor_by_ref=order_value_minor_by_ref,
        )
        revenue_total_minor = self._extract_revenue_minor_from_aggs(aggs, fallback=order_value_total_minor)
        ads_spend_vnd = self._fetch_ads_spend_vnd(start_date=start_date, end_date=end_date)

        brand_overview: list[dict[str, Any]] = []
        brand_detail: dict[str, Any] = {}
        for bucket in sorted(brands.values(), key=lambda item: str(item.get("brand_name", "")).lower()):
            size_summary = self._serialize_size_totals(bucket.get("size_totals"))
            sku_rows_obj = bucket.get("sku_rows")
            sku_rows: list[dict[str, Any]] = []
            if isinstance(sku_rows_obj, dict):
                sku_rows = sorted(
                    (
                        {
                            "sku": str(row.get("sku", "")).strip() or "UNKNOWN",
                            "color": str(row.get("color", "")).strip() or "Khác",
                            "sizes": self._normalize_size_map(row.get("sizes")),
                            "missing_line_count": self._to_int(row.get("missing_line_count")),
                            "missing_quantity": self._to_int(row.get("missing_quantity")),
                            "value_minor": self._to_int(row.get("value_minor")),
                            "value_text": self._fmt_currency(self._to_int(row.get("value_minor"))),
                            "order_refs": sorted(
                                {
                                    str(ref).strip()
                                    for ref in (row.get("order_refs") if isinstance(row.get("order_refs"), list) else [])
                                    if str(ref).strip()
                                }
                            ),
                        }
                        for row in sku_rows_obj.values()
                        if isinstance(row, dict)
                    ),
                    key=lambda row: (self._to_int(row.get("missing_quantity")), str(row.get("sku", ""))),
                    reverse=True,
                )
            brand_record = {
                "brand_name": str(bucket.get("brand_name", "")).strip() or "Khác",
                "brand_slug": str(bucket.get("brand_slug", "")).strip() or "khac",
                "total_orders": self._to_int(bucket.get("total_orders")),
                "closed_orders": self._to_int(bucket.get("closed_orders")),
                "total_value_minor": self._to_int(bucket.get("total_value_minor")),
                "total_value_text": self._fmt_currency(self._to_int(bucket.get("total_value_minor"))),
                "waiting_orders": self._to_int(bucket.get("waiting_orders")),
                "waiting_value_minor": self._to_int(bucket.get("waiting_value_minor")),
                "waiting_value_text": self._fmt_currency(self._to_int(bucket.get("waiting_value_minor"))),
                "missing_line_count": self._to_int(bucket.get("missing_line_count")),
                "missing_quantity": self._to_int(bucket.get("missing_quantity")),
                "size_summary": size_summary,
                "top_skus": sku_rows[:10],
            }
            brand_overview.append(brand_record)
            brand_detail[brand_record["brand_slug"]] = {
                **brand_record,
                "sku_rows": sku_rows,
            }

        is_single_day = start_date == end_date
        period_label = (
            start_date.strftime("%d-%m-%Y")
            if is_single_day
            else f"{start_date.strftime('%d-%m-%Y')} → {end_date.strftime('%d-%m-%Y')}"
        )
        payload = {
            "ok": True,
            "report_date": start_date.isoformat() if is_single_day else f"{start_date.isoformat()}..{end_date.isoformat()}",
            "timezone": self.settings.app_timezone,
            "generated_at": now_utc_iso(),
            "period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "is_single_day": is_single_day,
                "label": period_label,
            },
            "currency": {
                "base": "THB",
                "quote": "VND",
                "rate": float(self.settings.report_thb_to_vnd_rate),
                "minor_unit_factor": int(self.settings.report_thb_minor_unit_factor),
            },
            "metrics": {
                "total_orders": total_orders,
                "closed_orders": closed_orders,
                "revenue_total_minor": revenue_total_minor,
                "revenue_total_text": self._fmt_currency(revenue_total_minor),
                "revenue_total_thb": self._minor_to_thb_major(revenue_total_minor),
                "revenue_total_vnd": self._thb_to_vnd(self._minor_to_thb_major(revenue_total_minor)),
                "revenue_total_thb_text": self._fmt_thb_amount(self._minor_to_thb_major(revenue_total_minor)),
                "revenue_total_vnd_text": self._fmt_vnd_amount(self._thb_to_vnd(self._minor_to_thb_major(revenue_total_minor))),
                "ads_spend_vnd": ads_spend_vnd,
                "ads_spend_vnd_text": self._fmt_vnd_amount(ads_spend_vnd),
                "exchange_rate_thb_to_vnd": float(self.settings.report_thb_to_vnd_rate),
                "waiting_orders": waiting_orders_count,
                "returning_orders": returning_orders_count,
                "shipping_orders": shipping_orders_count,
                "reconcile_received_orders": reconcile_received_orders,
                "pending_reconcile_orders": pending_reconcile_orders,
                "shipping_value_minor": shipping_value_minor,
                "shipping_value_text": self._fmt_currency(shipping_value_minor),
                "shipping_value_thb_text": self._fmt_thb_amount(self._minor_to_thb_major(shipping_value_minor)),
                "shipping_value_vnd_text": self._fmt_vnd_amount(self._thb_to_vnd(self._minor_to_thb_major(shipping_value_minor))),
                "returning_value_minor": returning_value_minor,
                "returning_value_text": self._fmt_currency(returning_value_minor),
                "returning_value_thb_text": self._fmt_thb_amount(self._minor_to_thb_major(returning_value_minor)),
                "returning_value_vnd_text": self._fmt_vnd_amount(self._thb_to_vnd(self._minor_to_thb_major(returning_value_minor))),
                "reconcile_received_value_minor": reconcile_received_value_minor,
                "reconcile_received_value_text": self._fmt_currency(reconcile_received_value_minor),
                "reconcile_received_value_thb_text": self._fmt_thb_amount(
                    self._minor_to_thb_major(reconcile_received_value_minor)
                ),
                "reconcile_received_value_vnd_text": self._fmt_vnd_amount(
                    self._thb_to_vnd(self._minor_to_thb_major(reconcile_received_value_minor))
                ),
                "pending_reconcile_value_minor": pending_reconcile_value_minor,
                "pending_reconcile_value_text": self._fmt_currency(pending_reconcile_value_minor),
                "pending_reconcile_value_thb_text": self._fmt_thb_amount(
                    self._minor_to_thb_major(pending_reconcile_value_minor)
                ),
                "pending_reconcile_value_vnd_text": self._fmt_vnd_amount(
                    self._thb_to_vnd(self._minor_to_thb_major(pending_reconcile_value_minor))
                ),
                "missing_line_count": missing_line_count,
                "missing_quantity": missing_quantity,
                "missing_product_count": len(missing_products),
                "waiting_value_minor": waiting_value_minor,
                "waiting_value_text": self._fmt_currency(waiting_value_minor),
                "waiting_value_thb_text": self._fmt_thb_amount(self._minor_to_thb_major(waiting_value_minor)),
                "waiting_value_vnd_text": self._fmt_vnd_amount(self._thb_to_vnd(self._minor_to_thb_major(waiting_value_minor))),
            },
            "size_summary": self._serialize_size_totals(size_totals),
            "brands": brand_overview,
            "brand_detail": brand_detail,
            "status_lists": {
                "waiting": waiting_orders,
                "shipping": shipping_orders,
                "pending-reconcile": pending_reconcile,
                "reconcile-received": reconcile_received,
                "returning": returning_orders,
            },
        }
        return payload

    def _fetch_orders_and_aggs(self, *, start_date: date, end_date: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if hasattr(self.pancake, "fetch_orders_snapshot_for_range"):
            try:
                payload = self.pancake.fetch_orders_snapshot_for_range(  # type: ignore[attr-defined]
                    start_date,
                    end_date,
                    self.settings.app_timezone,
                )
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                orders = payload.get("orders", [])
                aggs = payload.get("aggs", {})
                if not isinstance(orders, list):
                    orders = []
                if not isinstance(aggs, dict):
                    aggs = {}
                return [item for item in orders if isinstance(item, dict)], aggs

        orders = self.pancake.fetch_all_orders_for_range(start_date, end_date, self.settings.app_timezone)
        if not isinstance(orders, list):
            orders = []
        return [item for item in orders if isinstance(item, dict)], {}

    def _fetch_ads_spend_vnd(self, *, start_date: date, end_date: date) -> int:
        if self.meta is None:
            return 0
        try:
            if hasattr(self.meta, "get_spend_for_range"):
                payload = self.meta.get_spend_for_range(start_date, end_date, self.settings.app_timezone)
                return max(0, self._to_int(payload.get("spend_vnd")))

            total = 0
            for report_date in self._iter_dates(start_date, end_date):
                payload = self.meta.get_daily_spend(report_date, self.settings.app_timezone)
                total += max(0, self._to_int(payload.get("spend_vnd")))
            return total
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Khong lay duoc chi phi Ads cho web report: %s", exc)
            return 0

    @staticmethod
    def _iter_dates(start_date: date, end_date: date):
        cursor = start_date
        while cursor <= end_date:
            yield cursor
            cursor = cursor.fromordinal(cursor.toordinal() + 1)

    def _load_latest_reconcile_run_for_date(self, report_date: date) -> dict[str, Any] | None:
        run_dir = self.settings.reconcile_cod_runs_dir
        if not run_dir.exists():
            return None

        pattern = f"run_{report_date.isoformat()}_*.json"
        run_paths = sorted(run_dir.glob(pattern), reverse=True)
        if not run_paths:
            return None

        latest_payload = None
        latest_ts = -1.0
        for path in run_paths:
            payload = self._safe_read_json(path)
            if not isinstance(payload, dict):
                continue
            generated_ts = self._to_generated_ts(payload)
            if generated_ts > latest_ts:
                latest_ts = generated_ts
                latest_payload = payload
        if isinstance(latest_payload, dict):
            return latest_payload
        return None

    def _load_reconcile_period_summary(
        self,
        *,
        start_date: date,
        end_date: date,
        status_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        pending_states = self._to_text_set(
            status_cfg.get("pending_reconcile_match_results", ["not_found", "ambiguous", "unmapped_status"])
        )
        pending_mode = self._normalize_text(str(status_cfg.get("pending_reconcile_mode", "match_result")))
        if pending_mode not in {"match_result", "td_success_not_in_cashflow"}:
            pending_mode = "match_result"
        pending_td_success_statuses = self._to_text_set(
            status_cfg.get("pending_reconcile_td_success_statuses", ["success"])
        )
        pending_td_to_pancake_status_codes = self._normalize_td_to_pancake_status_map(
            status_cfg.get("pending_reconcile_td_to_pancake_status_codes")
        )
        received_results = self._to_text_set(
            status_cfg.get("reconcile_received_match_results", ["matched_unique", "already_correct"])
        )
        received_td_statuses = self._to_text_set(status_cfg.get("reconcile_received_td_statuses", ["success"]))
        received_mode = self._normalize_text(str(status_cfg.get("reconcile_received_mode", "matched_and_td_status")))
        if received_mode not in {"matched_and_td_status", "td_status_only"}:
            received_mode = "matched_and_td_status"

        pending_rows: list[dict[str, Any]] = []
        received_rows: list[dict[str, Any]] = []
        pending_refs: set[str] = set()
        received_refs: set[str] = set()

        for settlement_date in self._iter_dates(start_date, end_date):
            run_payload = self._load_latest_reconcile_run_for_date(settlement_date)
            if not isinstance(run_payload, dict):
                continue
            records = run_payload.get("records", [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                match_result = self._normalize_text(str(record.get("match_result", "")).strip())
                td_status = self._normalize_text(str(record.get("td_status", "")).strip())
                row = self._build_reconcile_row(record, match_result)
                order_ref = self._normalize_text(str(row.get("pancake_order_ref", "")).strip())
                fingerprint = self._normalize_text(str(record.get("fingerprint", "")).strip())
                dedupe_ref = order_ref or fingerprint or f"row-{len(pending_rows) + len(received_rows)}"

                is_pending = self._is_pending_reconcile_row(
                    record=record,
                    match_result=match_result,
                    td_status=td_status,
                    pending_states=pending_states,
                    pending_mode=pending_mode,
                    pending_td_success_statuses=pending_td_success_statuses,
                    pending_td_to_pancake_status_codes=pending_td_to_pancake_status_codes,
                )
                if is_pending:
                    pending_rows.append(row)
                    pending_refs.add(dedupe_ref)

                is_received_status = not received_td_statuses or td_status in received_td_statuses
                if received_mode == "td_status_only":
                    is_received = is_received_status
                else:
                    is_received_match = not received_results or match_result in received_results
                    is_received = is_received_match and is_received_status
                if is_received:
                    received_rows.append(row)
                    received_refs.add(dedupe_ref)

        pending_rows.sort(
            key=lambda item: (
                str(item.get("settlement_date", "")),
                str(item.get("pancake_order_ref", "")),
            ),
            reverse=True,
        )
        received_rows.sort(
            key=lambda item: (
                str(item.get("settlement_date", "")),
                str(item.get("pancake_order_ref", "")),
            ),
            reverse=True,
        )
        return {
            "pending_rows": pending_rows,
            "received_rows": received_rows,
            "pending_order_count": len(pending_refs),
            "received_order_count": len(received_refs),
        }

    def _is_pending_reconcile_row(
        self,
        *,
        record: dict[str, Any],
        match_result: str,
        td_status: str,
        pending_states: set[str],
        pending_mode: str,
        pending_td_success_statuses: set[str],
        pending_td_to_pancake_status_codes: dict[str, set[int]],
    ) -> bool:
        if pending_mode == "td_success_not_in_cashflow":
            if not self._has_pancake_mapping(record, match_result=match_result):
                return False
            is_success = not pending_td_success_statuses or td_status in pending_td_success_statuses
            if not is_success:
                return False
            # Legacy reconcile runs may miss cashflow columns; skip those rows to avoid false pending.
            if not self._has_cashflow_signal(record):
                return False
            if self._is_cashflow_updated(record, td_status=td_status):
                return False
            if self._is_td_pancake_status_aligned(
                record,
                td_status=td_status,
                td_to_pancake_status_codes=pending_td_to_pancake_status_codes,
            ):
                return False
            return True
        return match_result in pending_states

    def _has_cashflow_signal(self, record: dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        if "td_sheet_cod_minor" not in record:
            return False
        return record.get("td_sheet_cod_minor") is not None

    def _is_cashflow_updated(self, record: dict[str, Any], *, td_status: str = "") -> bool:
        value = record.get("td_sheet_cod_minor")
        if value is None:
            return False
        normalized_status = td_status or self._normalize_text(str(record.get("td_status", "")).strip())
        if normalized_status in {self._normalize_text("BEING_RETURNED"), self._normalize_text("RETURNED")}:
            return True
        return self._to_int(value, fallback=0) > 0

    def _is_td_pancake_status_aligned(
        self,
        record: dict[str, Any],
        *,
        td_status: str,
        td_to_pancake_status_codes: dict[str, set[int]],
    ) -> bool:
        expected_status_codes = td_to_pancake_status_codes.get(td_status, set())
        if not expected_status_codes:
            return False
        pancake_status_code = self._to_optional_int(record.get("pancake_status"))
        if pancake_status_code is None:
            return False
        return pancake_status_code in expected_status_codes

    def _normalize_td_to_pancake_status_map(self, value: Any) -> dict[str, set[int]]:
        default_map: dict[str, set[int]] = {
            self._normalize_text("SUCCESS"): {2, 3},
            self._normalize_text("BEING_RETURNED"): {3, 4, 5},
            self._normalize_text("RETURNED"): {3, 4, 5},
        }
        if not isinstance(value, dict):
            return default_map

        normalized: dict[str, set[int]] = dict(default_map)
        for raw_td_status, raw_codes in value.items():
            td_status_key = self._normalize_text(str(raw_td_status).strip())
            if not td_status_key:
                continue
            codes: set[int] = set()
            if isinstance(raw_codes, list):
                for raw_code in raw_codes:
                    parsed = self._to_optional_int(raw_code)
                    if parsed is not None:
                        codes.add(parsed)
            else:
                parsed = self._to_optional_int(raw_codes)
                if parsed is not None:
                    codes.add(parsed)
            if codes:
                normalized[td_status_key] = codes
        return normalized

    def _has_pancake_mapping(self, record: dict[str, Any], *, match_result: str) -> bool:
        if match_result in {"not_found", "ambiguous", "unmapped_status"}:
            return False
        pancake_display_id = str(record.get("pancake_display_id", "")).strip()
        pancake_order_id = str(record.get("pancake_order_id", "")).strip()
        return bool(pancake_display_id or pancake_order_id)

    def _build_reconcile_row(self, record: dict[str, Any], match_result: str) -> dict[str, Any]:
        ref = str(record.get("pancake_display_id", "")).strip() or str(record.get("pancake_order_id", "")).strip()
        td_cod_minor = max(0, self._to_int(record.get("td_cod_minor"), fallback=0))
        if td_cod_minor <= 0:
            td_cod_minor = max(0, self._to_int(record.get("td_amount_minor"), fallback=0))
        return {
            "pancake_order_ref": ref,
            "match_result": match_result,
            "reason": str(record.get("reason", "")).strip(),
            "td_awb": str(record.get("td_awb", "")).strip(),
            "td_status": str(record.get("td_status", "")).strip(),
            "customer_name": str(record.get("td_customer_name", "")).strip(),
            "settlement_date": str(record.get("settlement_date", "")).strip(),
            "td_cod_minor": td_cod_minor,
        }

    def _count_unique_reconcile_order_refs(self, rows: Any) -> int:
        refs: set[str] = set()
        if not isinstance(rows, list):
            return 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized_ref = self._normalize_text(str(row.get("pancake_order_ref", "")).strip())
            if normalized_ref:
                refs.add(normalized_ref)
        return len(refs)

    def _sum_reconcile_rows_value_minor(self, rows: Any, *, order_value_minor_by_ref: dict[str, int]) -> int:
        if not isinstance(rows, list):
            return 0
        unique_refs: set[str] = set()
        total_minor = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized_ref = self._normalize_text(str(row.get("pancake_order_ref", "")).strip())
            if not normalized_ref or normalized_ref in unique_refs:
                continue
            unique_refs.add(normalized_ref)
            row_minor = max(0, self._to_int(row.get("td_cod_minor"), fallback=0))
            if row_minor <= 0:
                row_minor = max(0, self._to_int(order_value_minor_by_ref.get(normalized_ref)))
            total_minor += row_minor
        return total_minor

    def _extract_brand(self, order: dict[str, Any], *, brand_rules: Any) -> tuple[str, str]:
        candidate_fields = (
            "brand_name",
            "brand",
            "shop_name",
            "page_name",
            "source_name",
            "store_name",
        )
        for field in candidate_fields:
            value = str(order.get(field, "")).strip()
            if value:
                return value, self._slugify(value)

        item_codes: list[str] = []
        items = order.get("items", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                sku = self._extract_item_sku(item)
                if sku:
                    item_codes.append(sku)
        order_ref = self._extract_order_ref(order)
        if order_ref:
            item_codes.append(order_ref)

        normalized_codes = [self._normalize_text(code) for code in item_codes if code]
        if isinstance(brand_rules, list):
            for rule in brand_rules:
                if not isinstance(rule, dict):
                    continue
                pattern = str(rule.get("pattern", "")).strip()
                brand_name = str(rule.get("brand_name", "")).strip()
                if not pattern or not brand_name:
                    continue
                try:
                    regex = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    continue
                for code in normalized_codes:
                    if regex.search(code):
                        slug = str(rule.get("brand_slug", "")).strip() or self._slugify(brand_name)
                        return brand_name, slug

        for code in normalized_codes:
            if code.startswith("jc"):
                return "Jennie Choo", "jennie-choo"
            if code.startswith("lys") or code.startswith("l-") or code.startswith("l "):
                return "Lysilk", "lysilk"
            if code.startswith("sa") or code.startswith("s-") or code.startswith("s "):
                return "Say Studios", "say-studios"
        return "Khác", "khac"

    def _extract_order_ref(self, order: dict[str, Any]) -> str:
        candidates = (
            order.get("display_id"),
            order.get("code"),
            order.get("order_code"),
            order.get("order_id"),
            order.get("id"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def _extract_status_code(self, order: dict[str, Any]) -> int | None:
        raw = order.get("status")
        if isinstance(raw, bool):
            return None
        try:
            if raw is None:
                return None
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None

    def _extract_status_label(
        self,
        order: dict[str, Any],
        *,
        status_code: int | None = None,
        status_code_labels: dict[int, str] | None = None,
    ) -> str:
        for key in ("status_name", "status_text", "status_label", "order_status_name"):
            value = str(order.get(key, "")).strip()
            if value:
                return value
        if status_code is None:
            status_code = self._extract_status_code(order)
        if status_code is not None and isinstance(status_code_labels, dict):
            mapped = str(status_code_labels.get(status_code, "")).strip()
            if mapped:
                return mapped
        raw = order.get("status")
        if isinstance(raw, str):
            return raw.strip()
        return ""

    def _is_waiting_status(
        self,
        *,
        code: int | None,
        label: str,
        waiting_codes: set[int],
        waiting_labels: set[str],
    ) -> bool:
        if code is not None and code in waiting_codes:
            return True
        normalized_label = self._normalize_text(label)
        if normalized_label and normalized_label in waiting_labels:
            return True
        if "cho hang" in normalized_label:
            return True
        return False

    def _is_returning_status(
        self,
        *,
        code: int | None,
        label: str,
        returning_codes: set[int],
        returning_labels: set[str],
    ) -> bool:
        normalized_label = self._normalize_text(label)
        # Always exclude partial return from "đơn hoàn/đang hoàn".
        if "hoan mot phan" in normalized_label or "partial return" in normalized_label:
            return False

        # When explicit status codes are configured, trust code first to avoid label ambiguity.
        if returning_codes:
            if code is not None:
                return code in returning_codes
            if normalized_label and normalized_label in returning_labels:
                return True
            if "dang hoan" in normalized_label or "da hoan" in normalized_label:
                return True
            if "being_returned" in normalized_label or normalized_label == "returned":
                return True
            return False

        if code is not None and code in returning_codes:
            return True
        if normalized_label and normalized_label in returning_labels:
            return True
        if "dang hoan" in normalized_label or "da hoan" in normalized_label:
            return True
        if "being_returned" in normalized_label or normalized_label == "returned":
            return True
        return False

    def _is_shipping_status(
        self,
        *,
        code: int | None,
        label: str,
        shipping_codes: set[int],
        shipping_labels: set[str],
    ) -> bool:
        if code is not None and code in shipping_codes:
            return True
        normalized_label = self._normalize_text(label)
        if normalized_label and normalized_label in shipping_labels:
            return True
        if "gui hang" in normalized_label or "dang giao" in normalized_label or "shipped" in normalized_label:
            return True
        return False

    def _is_closed_status(
        self,
        *,
        code: int | None,
        label: str,
        closed_codes: set[int],
        closed_labels: set[str],
        is_waiting: bool,
        is_returning: bool,
    ) -> bool:
        if code is not None and code in closed_codes:
            return True
        normalized_label = self._normalize_text(label)
        if normalized_label and normalized_label in closed_labels:
            return True

        has_explicit_closed_map = bool(closed_codes or closed_labels)
        if has_explicit_closed_map:
            return False

        return not is_waiting and not is_returning

    def _extract_order_total_minor(self, order: dict[str, Any]) -> int:
        for key in ("total_price", "total", "amount_total"):
            value = order.get(key)
            if value is None:
                continue
            return self._to_int(value)
        return 0

    def _extract_item_sku(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        candidates = (
            item.get("sku"),
            item.get("display_id"),
            variation_info.get("display_id"),
            variation_info.get("product_id"),
            variation_info.get("sku"),
            variation_info.get("name"),
            item.get("name"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return "UNKNOWN"

    def _extract_item_color(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        for key in ("color", "colour"):
            value = str(variation_info.get(key, "")).strip()
            if value:
                return value
        fields = variation_info.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = self._normalize_text(str(field.get("name", "")).strip())
                if "mau" not in name and "color" not in name and "colour" not in name:
                    continue
                value = str(field.get("value", "")).strip()
                if value:
                    return value
        variation_name = str(variation_info.get("name", "")).strip()
        if variation_name:
            parts = [part.strip() for part in re.split(r"[-|/]", variation_name) if part.strip()]
            if len(parts) >= 2:
                return parts[-2]
        return "Khác"

    def _extract_item_size(self, item: dict[str, Any]) -> str:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        raw_size = variation_info.get("size")
        if isinstance(raw_size, str):
            value = raw_size.strip()
            if value:
                return value.upper()
        if isinstance(raw_size, dict):
            for key in ("value", "name", "keyValue", "code"):
                value = str(raw_size.get(key, "")).strip()
                if value:
                    return value.upper()
        fields = variation_info.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                name = self._normalize_text(str(field.get("name", "")).strip())
                if "size" not in name and "kich co" not in name and "kich thuoc" not in name:
                    continue
                value = str(field.get("value") or field.get("keyValue") or "").strip()
                if value:
                    return value.upper()
        candidates = [
            str(item.get("name", "")).strip(),
            str(variation_info.get("name", "")).strip(),
            str(self._extract_item_sku(item)).strip(),
        ]
        size_pattern = re.compile(r"\b(XXXL|XXL|XL|XS|S|M|L|2XL|3XL|4XL)\b", re.IGNORECASE)
        for candidate in candidates:
            match = size_pattern.search(candidate)
            if match:
                return match.group(1).upper()
        return "KHÁC"

    def _extract_item_unit_price_minor(self, item: dict[str, Any]) -> int:
        variation_info = item.get("variation_info")
        if not isinstance(variation_info, dict):
            variation_info = {}
        for key in ("retail_price", "price", "sale_price"):
            raw = variation_info.get(key, item.get(key))
            if raw is None:
                continue
            return max(0, self._to_int(raw))
        return 0

    def _extract_order_datetime(self, order: dict[str, Any], *, tz: timezone | ZoneInfo) -> datetime | None:
        ts_candidates = (
            order.get("inserted_at"),
            order.get("created_at"),
            order.get("created_time"),
            order.get("createdAt"),
            order.get("insertedAt"),
        )
        dt = None
        for raw in ts_candidates:
            dt = self._parse_datetime(raw)
            if dt is not None:
                break
        if dt is None:
            unix_candidates = (
                order.get("inserted_time"),
                order.get("created"),
                order.get("create_time"),
                order.get("created_timestamp"),
            )
            for raw in unix_candidates:
                dt = self._parse_unix_datetime(raw)
                if dt is not None:
                    break
        if dt is None:
            return None
        return dt.astimezone(tz)

    def _load_status_map_config(self) -> dict[str, Any]:
        path = self.settings.web_report_status_map_config_path
        if not path.exists():
            return self._default_status_map_config()
        payload = self._safe_read_json(path)
        if not isinstance(payload, dict):
            return self._default_status_map_config()
        defaults = self._default_status_map_config()
        merged = {**defaults, **payload}
        if not isinstance(merged.get("brand_rules"), list):
            merged["brand_rules"] = defaults.get("brand_rules", [])
        if not isinstance(merged.get("status_code_labels"), dict):
            merged["status_code_labels"] = defaults.get("status_code_labels", {})
        return merged

    def _default_status_map_config(self) -> dict[str, Any]:
        return {
            "waiting_status_codes": [],
            "waiting_status_labels": ["chờ hàng", "cho hang", "waiting"],
            "closed_status_codes": [],
            "closed_status_labels": [],
            "returning_status_codes": [4, 5],
            "returning_status_labels": ["đang hoàn", "đã hoàn", "being_returned", "returned"],
            "shipping_status_codes": [2],
            "shipping_status_labels": ["đã gửi hàng", "dang giao", "shipping"],
            "pending_reconcile_mode": "match_result",
            "pending_reconcile_td_success_statuses": ["success"],
            "pending_reconcile_td_to_pancake_status_codes": {
                "SUCCESS": [2, 3],
                "BEING_RETURNED": [3, 4, 5],
                "RETURNED": [3, 4, 5],
            },
            "status_code_labels": self._default_order_status_code_labels(),
            "pending_reconcile_match_results": ["not_found", "ambiguous", "unmapped_status"],
            "reconcile_received_match_results": ["matched_unique", "already_correct"],
            "reconcile_received_td_statuses": ["success"],
            "reconcile_received_mode": "matched_and_td_status",
            "brand_rules": [
                {"pattern": r"^JC", "brand_name": "Jennie Choo", "brand_slug": "jennie-choo"},
                {"pattern": r"^(LYS|L-)", "brand_name": "Lysilk", "brand_slug": "lysilk"},
                {"pattern": r"^(SA|S-)", "brand_name": "Say Studios", "brand_slug": "say-studios"},
            ],
        }

    @staticmethod
    def _safe_read_json(path: Path) -> Any:
        try:
            return load_json(path)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            if value is None:
                return fallback
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _to_optional_int(value: Any) -> int | None:
        try:
            if value is None:
                return None
            if isinstance(value, bool):
                return None
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_generated_ts(payload: dict[str, Any]) -> float:
        raw = str(payload.get("generated_at", "")).strip()
        dt = WebReportService._parse_datetime(raw)
        if dt is None:
            return 0.0
        return dt.timestamp()

    @staticmethod
    def _to_ts(value: datetime | None) -> int:
        if value is None:
            return 0
        return int(value.timestamp())

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        candidate = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _parse_unix_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            numeric = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if numeric > 2_000_000_000_000:
            numeric = numeric / 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)

    @staticmethod
    def _normalize_text(raw: str) -> str:
        text = str(raw or "").strip().lower()
        if not text:
            return ""
        replacements = {
            "à": "a",
            "á": "a",
            "ạ": "a",
            "ả": "a",
            "ã": "a",
            "â": "a",
            "ầ": "a",
            "ấ": "a",
            "ậ": "a",
            "ẩ": "a",
            "ẫ": "a",
            "ă": "a",
            "ằ": "a",
            "ắ": "a",
            "ặ": "a",
            "ẳ": "a",
            "ẵ": "a",
            "è": "e",
            "é": "e",
            "ẹ": "e",
            "ẻ": "e",
            "ẽ": "e",
            "ê": "e",
            "ề": "e",
            "ế": "e",
            "ệ": "e",
            "ể": "e",
            "ễ": "e",
            "ì": "i",
            "í": "i",
            "ị": "i",
            "ỉ": "i",
            "ĩ": "i",
            "ò": "o",
            "ó": "o",
            "ọ": "o",
            "ỏ": "o",
            "õ": "o",
            "ô": "o",
            "ồ": "o",
            "ố": "o",
            "ộ": "o",
            "ổ": "o",
            "ỗ": "o",
            "ơ": "o",
            "ờ": "o",
            "ớ": "o",
            "ợ": "o",
            "ở": "o",
            "ỡ": "o",
            "ù": "u",
            "ú": "u",
            "ụ": "u",
            "ủ": "u",
            "ũ": "u",
            "ư": "u",
            "ừ": "u",
            "ứ": "u",
            "ự": "u",
            "ử": "u",
            "ữ": "u",
            "ỳ": "y",
            "ý": "y",
            "ỵ": "y",
            "ỷ": "y",
            "ỹ": "y",
            "đ": "d",
        }
        for src, dest in replacements.items():
            text = text.replace(src, dest)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = WebReportService._normalize_text(value)
        normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        return normalized or "khac"

    def _fmt_currency(self, minor_value: int) -> str:
        thb_major = self._minor_to_thb_major(minor_value)
        vnd_value = self._thb_to_vnd(thb_major)
        return f"{self._fmt_thb_amount(thb_major)} THB (~ {self._fmt_vnd_amount(vnd_value)} VNĐ)"

    def _minor_to_thb_major(self, minor_value: int) -> float:
        factor = max(1, int(self.settings.report_thb_minor_unit_factor))
        return float(minor_value) / float(factor)

    def _thb_to_vnd(self, thb_major: float) -> int:
        rate = float(self.settings.report_thb_to_vnd_rate)
        return int(round(thb_major * rate))

    @staticmethod
    def _fmt_thb_amount(thb_major: float) -> str:
        rounded = round(float(thb_major), 2)
        if abs(rounded - round(rounded)) < 1e-9:
            return f"{int(round(rounded)):,}"
        return f"{rounded:,.2f}"

    @staticmethod
    def _fmt_vnd_amount(vnd_value: int) -> str:
        return f"{int(vnd_value):,}"

    @staticmethod
    def _format_dt(value: datetime | None, *, tz: timezone | ZoneInfo) -> str:
        if value is None:
            return ""
        return value.astimezone(tz).strftime("%H:%M %d-%m-%Y")

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone.utc

    @staticmethod
    def _to_int_set(values: Any) -> set[int]:
        result: set[int] = set()
        if not isinstance(values, list):
            return result
        for item in values:
            try:
                result.add(int(str(item).strip()))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _to_text_set(values: Any) -> set[str]:
        result: set[str] = set()
        if not isinstance(values, list):
            return result
        for item in values:
            text = WebReportService._normalize_text(str(item).strip())
            if text:
                result.add(text)
        return result

    @staticmethod
    def _serialize_size_totals(size_map: Any) -> list[dict[str, Any]]:
        normalized = WebReportService._normalize_size_map(size_map)
        return [
            {"size": size, "quantity": quantity}
            for size, quantity in sorted(
                normalized.items(),
                key=lambda item: (item[0] == "KHÁC", item[0]),
            )
        ]

    @staticmethod
    def _normalize_size_map(size_map: Any) -> dict[str, int]:
        if not isinstance(size_map, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in size_map.items():
            size = str(key or "").strip().upper() or "KHÁC"
            result[size] = WebReportService._to_int(result.get(size)) + WebReportService._to_int(value)
        return result

    def _build_status_code_label_map(self, status_cfg: dict[str, Any]) -> dict[int, str]:
        result = self._default_order_status_code_labels()
        custom = status_cfg.get("status_code_labels", {})
        if not isinstance(custom, dict):
            return result
        for raw_code, raw_label in custom.items():
            label = str(raw_label or "").strip()
            if not label:
                continue
            try:
                code = int(str(raw_code).strip())
            except (TypeError, ValueError):
                continue
            result[code] = label
        return result

    @staticmethod
    def _default_order_status_code_labels() -> dict[int, str]:
        # Theo schema OpenAPI Pancake: components.schemas.OrderInfo.properties.status.x-enum-descriptions
        return {
            0: "Mới",
            17: "Chờ xác nhận",
            11: "Chờ hàng",
            12: "Chờ in",
            13: "Đã in",
            20: "Đã đặt hàng",
            1: "Đã xác nhận",
            8: "Đang đóng hàng",
            9: "Chờ chuyển hàng",
            2: "Đã gửi hàng",
            3: "Đã nhận",
            16: "Đã thu tiền",
            4: "Đang hoàn",
            15: "Hoàn một phần",
            5: "Đã hoàn",
            6: "Đã hủy",
            7: "Đã xóa",
        }

    def _extract_revenue_minor_from_aggs(self, aggs: Any, *, fallback: int) -> int:
        if not isinstance(aggs, dict):
            return max(0, fallback)
        cod_minor = self._extract_agg_metric_minor(aggs.get("cod"))
        prepaid_minor = self._extract_agg_metric_minor(aggs.get("prepaid"))
        total = cod_minor + prepaid_minor
        if total > 0:
            return total
        return max(0, fallback)

    def _extract_agg_metric_minor(self, raw_metric: Any) -> int:
        if isinstance(raw_metric, dict):
            raw_metric = raw_metric.get("value")
        return max(0, self._to_int(raw_metric))
