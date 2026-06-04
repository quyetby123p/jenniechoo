from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import csv
import logging
from pathlib import Path
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import dump_json, fingerprint, load_json, now_utc_iso


class ReconcileCodService:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        pancake_client: PancakePosClient,
        thai_duong_client: ThaiDuongCodClient,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.pancake = pancake_client
        self.thai_duong = thai_duong_client
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.reconcile_cod_runs_dir,
            self.settings.reconcile_cod_reports_dir,
            self.settings.reconcile_cod_applied_dir,
            self.settings.reconcile_cod_import_history_dir,
            self.settings.reconcile_cod_import_detail_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def generate_report(self, settlement_date: date | None = None) -> dict[str, Any]:
        errors: dict[str, str] = {}
        warnings: list[str] = []
        match_cfg = self._load_match_config()
        target_date = settlement_date or self._resolve_default_settlement_date(match_cfg, warnings)

        history_rows: list[dict[str, Any]] = []
        settlement: dict[str, Any] | None = None
        source_mode = "unknown"
        try:
            history_rows, history_source = self.thai_duong.fetch_settlement_history(
                target_date - timedelta(days=60),
                target_date + timedelta(days=1),
            )
            source_mode = history_source
            settlement = self._pick_settlement_entry(history_rows, target_date, match_cfg)
            if not settlement:
                warnings.append(
                    f"Không tìm thấy kỳ đối soát COD đúng ngày {target_date.isoformat()} từ lịch sử; dùng lọc theo ngày trực tiếp."
                )
        except Exception as exc:  # noqa: BLE001
            errors["history"] = str(exc)
            self.logger.exception("Lay lich su doi soat COD that bai")

        detail_rows: list[dict[str, Any]] = []
        try:
            detail_rows, detail_source = self.thai_duong.fetch_settlement_details(target_date, settlement=settlement)
            source_mode = detail_source if source_mode == "unknown" else source_mode
        except Exception as exc:  # noqa: BLE001
            errors["detail"] = str(exc)
            self.logger.exception("Lay chi tiet doi soat COD that bai")

        pancake_orders: list[dict[str, Any]] = []
        try:
            pancake_orders = self._fetch_pancake_orders(match_cfg, target_date)
        except Exception as exc:  # noqa: BLE001
            errors["pancake"] = str(exc)
            self.logger.exception("Lay du lieu don Pancake de doi soat that bai")

        status_map_cfg = self._load_status_map_config()
        records = self._reconcile_rows(
            settlement_date=target_date,
            detail_rows=detail_rows,
            pancake_orders=pancake_orders,
            match_cfg=match_cfg,
            status_map_cfg=status_map_cfg,
        )
        summary = self._build_summary(records)
        conclusion_totals = self._build_conclusion_totals(
            records,
            settlement=settlement,
            match_cfg=match_cfg,
        )

        report: dict[str, Any] = {
            "ok": not errors,
            "partial": bool(errors) and bool(records),
            "settlement_date": target_date.isoformat(),
            "generated_at": now_utc_iso(),
            "timezone": self.settings.app_timezone,
            "source_mode": source_mode,
            "history_count": len(history_rows),
            "detail_count": len(detail_rows),
            "pancake_order_count": len(pancake_orders),
            "summary": summary,
            "conclusion_totals": conclusion_totals,
            "records": records,
            "warnings": warnings,
            "errors": errors,
        }

        csv_path = self._save_csv(report)
        report["csv_path"] = str(csv_path)
        run_path = self._save_run(report)
        report["run_id"] = run_path.stem
        report["run_path"] = str(run_path)
        return report

    def generate_report_if_settlement_exists(self, settlement_date: date | None = None) -> dict[str, Any] | None:
        target_date = settlement_date or datetime.now(self._resolve_timezone()).date()
        match_cfg = self._load_match_config()
        history_rows, _history_source = self.thai_duong.fetch_settlement_history(
            target_date,
            target_date + timedelta(days=1),
        )
        settlement = self._pick_settlement_entry(history_rows, target_date, match_cfg)
        if not settlement:
            self.logger.info("Khong co ky doi soat COD Thai Duong ngay %s; bo qua bao cao tien ve.", target_date)
            return None
        return self.generate_report(target_date)

    def build_message(self, report: dict[str, Any], trigger_label: str = "") -> str:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        conclusion_totals = report.get("conclusion_totals", {}) if isinstance(report.get("conclusion_totals"), dict) else {}
        settlement_raw = str(report.get("settlement_date", "")).strip()
        settlement_date = self._parse_date(settlement_raw) or datetime.now(self._resolve_timezone()).date()
        status_text = "OK" if report.get("ok") else ("CẢNH BÁO" if report.get("partial") else "LỖI")
        source_mode_raw = str(report.get("source_mode", "unknown")).strip().lower()
        source_mode_label = {
            "api": "API",
            "csv": "CSV",
            "unknown": "Không xác định",
        }.get(source_mode_raw, source_mode_raw or "Không xác định")
        lines: list[str] = []
        if trigger_label:
            lines.append(trigger_label)
        lines.extend(
            [
                f"Đối soát COD ngày {settlement_date.strftime('%d/%m/%Y')} ({self.settings.app_timezone})",
                f"Tổng quan: {status_text}",
                f"Nguồn dữ liệu: {source_mode_label}",
                "",
                f"Tổng bản ghi Thái Dương: {self._to_int(report.get('detail_count')):,}",
                f"Kết luận đối soát (THB): {self._format_thb(conclusion_totals.get('thb_total', 0.0))}",
                f"Kết luận đối soát (VNĐ): {self._format_vnd(conclusion_totals.get('vnd_total', 0))}",
                f"Khớp duy nhất: {self._to_int(summary.get('matched_unique')):,}",
                f"Đã đúng trạng thái: {self._to_int(summary.get('already_correct')):,}",
                f"Khớp mơ hồ: {self._to_int(summary.get('ambiguous')):,}",
                f"Không tìm thấy: {self._to_int(summary.get('not_found')):,}",
                f"Chưa map trạng thái: {self._to_int(summary.get('unmapped_status')):,}",
                f"Đủ điều kiện cập nhật: {self._to_int(summary.get('update_candidates')):,}",
            ]
        )
        converted_count = self._to_int(conclusion_totals.get("vnd_converted_count"), fallback=0)
        if converted_count > 0:
            rate = float(self.settings.report_thb_to_vnd_rate)
            lines.append(
                f"(Đã quy đổi {converted_count:,} đơn thiếu VNĐ theo tỷ giá {self._format_rate(rate)})"
            )
        sheet_sync = report.get("sheet_sync", {})
        if isinstance(sheet_sync, dict) and sheet_sync.get("enabled"):
            sheet_status = "OK" if sheet_sync.get("ok") else "LỖI"
            lines.extend(
                [
                    "",
                    f"Đồng bộ Google Sheet: {sheet_status}",
                    f"- Tổng ghi thử: {self._to_int(sheet_sync.get('attempted')):,}",
                    f"- Ghi mới: {self._to_int(sheet_sync.get('inserted')):,}",
                    f"- Bỏ qua trùng: {self._to_int(sheet_sync.get('skipped_existing')):,}",
                ]
            )
        lines.append(f"Tệp CSV: {report.get('csv_path', '')}")
        warnings = report.get("warnings", [])
        if isinstance(warnings, list) and warnings:
            lines.append("")
            lines.append("Cảnh báo:")
            for warning in warnings:
                lines.append(f"- {warning}")
        errors = report.get("errors", {})
        if isinstance(errors, dict) and errors:
            lines.append("")
            lines.append("Lỗi nguồn dữ liệu:")
            for source, err in errors.items():
                lines.append(f"- {source}: {self._short_text(err, limit=260)}")
        return "\n".join(lines)

    def summarize_cash_in_from_report(self, report: dict[str, Any]) -> dict[str, Any]:
        settlement_raw = str(report.get("settlement_date", "")).strip()
        settlement_date = self._parse_date(settlement_raw) or self.default_settlement_date()
        totals = report.get("conclusion_totals", {})
        if not isinstance(totals, dict):
            totals = {}
        thb_total = self._to_optional_float(totals.get("thb_total"))
        vnd_total = self._to_optional_float(totals.get("vnd_total"))
        vnd_converted_count = self._to_int(totals.get("vnd_converted_count"), fallback=0)
        source = str(totals.get("source", "")).strip() or "unknown"
        if thb_total is None:
            thb_total = 0.0
        if vnd_total is None or (float(vnd_total) <= 0 and float(thb_total) > 0):
            vnd_total = float(thb_total) * float(self.settings.report_thb_to_vnd_rate)
            if abs(float(thb_total)) > 1e-9:
                vnd_converted_count = max(vnd_converted_count, 1)
        return {
            "ok": bool(report.get("ok")),
            "partial": bool(report.get("partial")),
            "settlement_date": settlement_date.isoformat(),
            "thb_total": round(float(thb_total), 2),
            "vnd_total": round(float(vnd_total), 2),
            "vnd_converted_count": max(0, int(vnd_converted_count)),
            "source": source,
        }

    def build_weekly_cash_in_summary(self, anchor_date: date | None = None) -> dict[str, Any]:
        anchor = anchor_date or datetime.now(self._resolve_timezone()).date()
        week_start = anchor - timedelta(days=anchor.weekday())
        week_end = week_start + timedelta(days=4)
        if anchor < week_end:
            week_end = anchor
        if week_end < week_start:
            week_end = week_start

        match_cfg = self._load_match_config()
        td_cfg = match_cfg.get("thai_duong", {}) if isinstance(match_cfg.get("thai_duong"), dict) else {}
        settlement_paths = self._as_list(td_cfg.get("settlement_date_paths"))

        try:
            history_rows, history_source = self.thai_duong.fetch_settlement_history(
                week_start,
                week_end + timedelta(days=1),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "history_source": "unknown",
                "days": [],
                "thb_total": 0.0,
                "vnd_total": 0.0,
                "vnd_converted_count": 0,
                "error": str(exc),
            }

        history_by_date: dict[date, dict[str, Any]] = {}
        for row in history_rows:
            if not isinstance(row, dict):
                continue
            raw = self._extract_first_value(row, settlement_paths)
            parsed = self._parse_date(str(raw))
            if not parsed:
                continue
            if parsed < week_start or parsed > week_end:
                continue
            if parsed not in history_by_date:
                history_by_date[parsed] = row

        thb_total = 0.0
        vnd_total = 0.0
        converted_count = 0
        day_items: list[dict[str, Any]] = []
        day = week_start
        while day <= week_end:
            row = history_by_date.get(day)
            if row is not None:
                totals = self._build_conclusion_totals(
                    records=[],
                    settlement=row,
                    match_cfg=match_cfg,
                )
                day_thb = self._to_optional_float(totals.get("thb_total"))
                day_vnd = self._to_optional_float(totals.get("vnd_total"))
                day_converted = self._to_int(totals.get("vnd_converted_count"), fallback=0)
                if day_thb is None:
                    day_thb = 0.0
                if day_vnd is None or (float(day_vnd) <= 0 and float(day_thb) > 0):
                    day_vnd = float(day_thb) * float(self.settings.report_thb_to_vnd_rate)
                    if abs(float(day_thb)) > 1e-9:
                        day_converted = max(day_converted, 1)
                thb_total += float(day_thb)
                vnd_total += float(day_vnd)
                converted_count += max(0, int(day_converted))
                day_items.append(
                    {
                        "settlement_date": day.isoformat(),
                        "thb_total": round(float(day_thb), 2),
                        "vnd_total": round(float(day_vnd), 2),
                        "vnd_converted_count": max(0, int(day_converted)),
                        "source": str(totals.get("source", "")).strip() or "unknown",
                    }
                )
            day = day + timedelta(days=1)

        return {
            "ok": True,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "history_source": history_source,
            "days": day_items,
            "thb_total": round(thb_total, 2),
            "vnd_total": round(vnd_total, 2),
            "vnd_converted_count": converted_count,
        }

    def default_settlement_date(self) -> date:
        now_local = datetime.now(self._resolve_timezone())
        return (now_local - timedelta(days=1)).date()

    def apply_updates(self, run_id: str) -> dict[str, Any]:
        run_path = self.settings.reconcile_cod_runs_dir / f"{run_id}.json"
        if not run_path.exists():
            raise FileNotFoundError(f"Khong tim thay run doi soat: {run_id}")
        payload = load_json(run_path)
        if not isinstance(payload, dict):
            raise ValueError("Run doi soat COD khong hop le.")
        records = payload.get("records", [])
        if not isinstance(records, list):
            records = []
        settlement_date_raw = str(payload.get("settlement_date", "")).strip()
        settlement_date = self._parse_date(settlement_date_raw)
        if not settlement_date:
            raise ValueError("Run doi soat COD thieu settlement_date hop le.")

        if not self.settings.reconcile_cod_update_enabled:
            return {
                "ok": False,
                "reason": "RECONCILE_COD_UPDATE_ENABLED=0",
                "updated": 0,
                "skipped": len(records),
                "failed": 0,
            }

        status_map_cfg = self._load_status_map_config()
        update_cfg = status_map_cfg.get("update_endpoint", {})
        if not isinstance(update_cfg, dict):
            update_cfg = {}
        transition_cfg = status_map_cfg.get("pre_update_transition", {})
        if not isinstance(transition_cfg, dict):
            transition_cfg = {}

        limit = max(1, int(self.settings.reconcile_cod_batch_limit))
        applied_keys = self._load_applied_fingerprints(settlement_date)
        updated = 0
        failed = 0
        skipped = 0
        errors: list[str] = []
        failed_orders: list[dict[str, Any]] = []
        newly_applied: list[str] = []
        transitioned = 0

        for record in records:
            if not isinstance(record, dict):
                skipped += 1
                continue
            if str(record.get("match_result", "")).strip() != "matched_unique":
                skipped += 1
                continue
            target_status = self._to_optional_int(record.get("target_status"))
            order_id = str(record.get("pancake_order_id", "")).strip()
            key = str(record.get("fingerprint", "")).strip()
            if not order_id or target_status is None:
                skipped += 1
                continue
            if key and key in applied_keys:
                skipped += 1
                continue
            if updated >= limit:
                skipped += 1
                continue
            try:
                self.pancake.update_order_status(order_id, target_status, update_cfg=update_cfg)
                updated += 1
                if key:
                    newly_applied.append(key)
            except Exception as exc:  # noqa: BLE001
                recovered = self._try_apply_with_transition_fallback(
                    order_id=order_id,
                    target_status=target_status,
                    record=record,
                    error=exc,
                    update_cfg=update_cfg,
                    transition_cfg=transition_cfg,
                )
                if recovered:
                    transitioned += 1
                    updated += 1
                    if key:
                        newly_applied.append(key)
                    continue
                failed += 1
                display_id = str(record.get("pancake_display_id", "")).strip()
                current_status = self._to_optional_int(record.get("pancake_status"))
                error_text = str(exc)
                if display_id:
                    errors.append(f"{order_id} ({display_id}): {error_text}")
                else:
                    errors.append(f"{order_id}: {error_text}")
                failed_orders.append(
                    {
                        "order_id": order_id,
                        "display_id": display_id,
                        "awb": str(record.get("td_awb", "")).strip(),
                        "current_status": current_status,
                        "target_status": target_status,
                        "error": error_text,
                    }
                )

        if newly_applied:
            self._save_applied_fingerprints(settlement_date, applied_keys.union(newly_applied))

        apply_summary = {
            "ok": failed == 0,
            "updated": updated,
            "failed": failed,
            "skipped": skipped,
            "transitioned": transitioned,
            "errors": errors,
            "failed_orders": failed_orders,
            "applied_at": now_utc_iso(),
        }
        payload["apply_summary"] = apply_summary
        dump_json(run_path, payload)
        return apply_summary

    def _resolve_default_settlement_date(self, match_cfg: dict[str, Any], warnings: list[str]) -> date:
        fallback = self.default_settlement_date()
        try:
            history_rows, _ = self.thai_duong.fetch_settlement_history(
                fallback - timedelta(days=90),
                fallback + timedelta(days=1),
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Không lấy được lịch sử đối soát COD để chọn kỳ mặc định: {exc}")
            return fallback

        td_cfg = match_cfg.get("thai_duong", {}) if isinstance(match_cfg.get("thai_duong"), dict) else {}
        settlement_paths = self._as_list(td_cfg.get("settlement_date_paths"))
        dates: list[date] = []
        for row in history_rows:
            if not isinstance(row, dict):
                continue
            raw = self._extract_first_value(row, settlement_paths)
            parsed = self._parse_date(str(raw))
            if parsed:
                dates.append(parsed)
        if not dates:
            return fallback

        seen_run_dates = self._load_existing_run_dates()
        candidates = sorted(set(dates), reverse=True)
        for item in candidates:
            if item not in seen_run_dates:
                return item
        return candidates[0]

    def _load_existing_run_dates(self) -> set[date]:
        values: set[date] = set()
        for path in self.settings.reconcile_cod_runs_dir.glob("*.json"):
            try:
                payload = load_json(path)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, dict):
                continue
            parsed = self._parse_date(str(payload.get("settlement_date", "")).strip())
            if parsed:
                values.add(parsed)
        return values

    def _pick_settlement_entry(
        self,
        history_rows: list[dict[str, Any]],
        target_date: date,
        match_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        td_cfg = match_cfg.get("thai_duong", {}) if isinstance(match_cfg.get("thai_duong"), dict) else {}
        paths = self._as_list(td_cfg.get("settlement_date_paths"))
        for row in history_rows:
            if not isinstance(row, dict):
                continue
            raw = self._extract_first_value(row, paths)
            parsed = self._parse_date(str(raw))
            if parsed == target_date:
                return row
        return None

    def _fetch_pancake_orders(self, match_cfg: dict[str, Any], target_date: date) -> list[dict[str, Any]]:
        lookback_days = max(1, int(self.settings.reconcile_cod_pancake_lookback_days))
        start_date = target_date - timedelta(days=lookback_days)
        end_date = datetime.now(self._resolve_timezone()).date()
        return self.pancake.fetch_all_orders_for_range(start_date, end_date, self.settings.app_timezone)

    def _reconcile_rows(
        self,
        *,
        settlement_date: date,
        detail_rows: list[dict[str, Any]],
        pancake_orders: list[dict[str, Any]],
        match_cfg: dict[str, Any],
        status_map_cfg: dict[str, Any],
    ) -> list[dict[str, Any]]:
        td_cfg = match_cfg.get("thai_duong", {}) if isinstance(match_cfg.get("thai_duong"), dict) else {}
        pc_cfg = match_cfg.get("pancake", {}) if isinstance(match_cfg.get("pancake"), dict) else {}
        td_amount_factor = max(1, self._to_int(td_cfg.get("amount_minor_unit_factor"), fallback=100))
        pc_amount_factor = max(1, self._to_int(pc_cfg.get("amount_minor_unit_factor"), fallback=1))

        awb_paths_td = self._as_list(td_cfg.get("awb_paths"))
        status_paths_td = self._as_list(td_cfg.get("status_paths"))
        phone_paths_td = self._as_list(td_cfg.get("phone_paths"))
        name_paths_td = self._as_list(td_cfg.get("customer_name_paths"))
        amount_paths_td = self._as_list(td_cfg.get("amount_paths"))
        sheet_cod_paths_td = self._as_list(td_cfg.get("sheet_cod_paths"))
        if not sheet_cod_paths_td:
            sheet_cod_paths_td = [
                "codFromCustomer",
                "cod_transfered",
                "codTransferred",
                "collectedCod",
                "collected_cod",
            ]
        detail_settlement_paths_td = self._as_list(td_cfg.get("detail_settlement_date_paths"))
        send_date_paths_td = self._as_list(td_cfg.get("send_date_paths"))
        fee_paths_td = self._as_list(td_cfg.get("fee_paths"))
        conclusion_thb_paths_td = self._as_list(td_cfg.get("conclusion_thb_paths"))
        conclusion_vnd_paths_td = self._as_list(td_cfg.get("conclusion_vnd_paths"))
        exchange_rate_paths_td = self._as_list(td_cfg.get("exchange_rate_paths"))
        td_fee_factor = max(1, self._to_int(td_cfg.get("fee_minor_unit_factor"), fallback=td_amount_factor))

        awb_paths_pc = self._as_list(pc_cfg.get("awb_paths"))
        phone_paths_pc = self._as_list(pc_cfg.get("phone_paths"))
        name_paths_pc = self._as_list(pc_cfg.get("customer_name_paths"))
        amount_paths_pc = self._as_list(pc_cfg.get("amount_paths"))
        original_amount_paths_pc = self._as_list(pc_cfg.get("original_amount_paths"))
        status_paths_pc = self._as_list(pc_cfg.get("status_paths"))
        display_id_paths_pc = self._as_list(pc_cfg.get("display_id_paths"))
        id_paths_pc = self._as_list(pc_cfg.get("order_id_paths"))

        awb_index: dict[str, list[dict[str, Any]]] = {}
        identity_index: dict[str, list[dict[str, Any]]] = {}
        identity_amount_index: dict[str, list[dict[str, Any]]] = {}
        identity_original_amount_index: dict[str, list[dict[str, Any]]] = {}
        pancake_meta: dict[int, dict[str, Any]] = {}

        for order in pancake_orders:
            if not isinstance(order, dict):
                continue
            awb_values = self._extract_values(order, awb_paths_pc)
            phone_value = self._normalize_phone(self._extract_first_value(order, phone_paths_pc))
            name_value = self._normalize_name(self._extract_first_value(order, name_paths_pc))
            amount_minor = self._to_minor_amount(
                self._extract_first_value(order, amount_paths_pc),
                factor=pc_amount_factor,
            )
            original_amount_values = self._extract_minor_amount_values(
                order,
                original_amount_paths_pc,
                factor=pc_amount_factor,
            )
            status_value = self._to_optional_int(self._extract_first_value(order, status_paths_pc))
            display_id = str(self._extract_first_value(order, display_id_paths_pc)).strip()
            order_id = str(self._extract_first_value(order, id_paths_pc)).strip()

            info = {
                "order": order,
                "order_id": order_id,
                "display_id": display_id,
                "status": status_value,
            }
            pancake_meta[id(order)] = info

            for awb_raw in awb_values:
                awb = self._normalize_awb(awb_raw)
                if not awb:
                    continue
                awb_index.setdefault(awb, []).append(order)

            phone_name_key = self._phone_name_key(phone_value, name_value)
            if phone_name_key:
                identity_index.setdefault(phone_name_key, []).append(order)
                if amount_minor is not None:
                    amount_key = self._identity_amount_key(phone_name_key, amount_minor)
                    identity_amount_index.setdefault(amount_key, []).append(order)
                for original_amount in original_amount_values:
                    original_amount_key = self._identity_amount_key(phone_name_key, original_amount)
                    identity_original_amount_index.setdefault(original_amount_key, []).append(order)

        records: list[dict[str, Any]] = []
        for row in detail_rows:
            if not isinstance(row, dict):
                continue
            td_awb = self._normalize_awb(self._extract_first_value(row, awb_paths_td))
            td_status_raw = str(self._extract_first_value(row, status_paths_td)).strip()
            td_status_key = self._normalize_name(td_status_raw)
            td_phone = self._normalize_phone(self._extract_first_value(row, phone_paths_td))
            td_name = self._normalize_name(self._extract_first_value(row, name_paths_td))
            td_amount_minor = self._to_minor_amount(
                self._extract_first_value(row, amount_paths_td),
                factor=td_amount_factor,
            )
            td_sheet_cod_minor = self._to_minor_amount(
                self._extract_first_value(row, sheet_cod_paths_td),
                factor=td_amount_factor,
            )
            if td_sheet_cod_minor is None:
                success_status_keys = {"success", "delivered", "giao hang thanh cong"}
                if td_status_key in success_status_keys and td_amount_minor is not None:
                    td_sheet_cod_minor = td_amount_minor
                else:
                    td_sheet_cod_minor = 0
            fee_map = self._extract_fee_map(row)
            td_delivery_fee = self._to_optional_float(self._extract_first_value(row, fee_paths_td)) or 0.0
            td_remote_fee = self._to_optional_float(row.get("remoteFee")) or self._to_optional_float(row.get("remote_fee")) or 0.0
            td_refund_fee = fee_map.get("refund_fee", 0.0)
            td_cod_fee = fee_map.get("cod_fee", 0.0)
            td_insurance_fee = fee_map.get("insurance_fee", 0.0)
            td_account_fee = fee_map.get("account_fee", 0.0)
            td_hard_goods_fee = fee_map.get("hard_goods_fee", 0.0)
            td_ffm_fee = fee_map.get("ffm_fee", 0.0)
            td_confirm_trend_order_fee = fee_map.get("confirm_trend_order_fee", 0.0)
            td_confirm_hard_order_fee = fee_map.get("confirm_hard_order_fee", 0.0)
            td_mess_fee = fee_map.get("mess_fee", 0.0)
            td_mess_care_fee = fee_map.get("mess_care_fee", 0.0)
            td_telesale_care_fee = fee_map.get("telesale_care_fee", 0.0)
            td_fulfillment_other_fee = fee_map.get("fulfillment_other_fee", 0.0)
            td_ship_discount_fee = fee_map.get("ship_discount_fee", 0.0)
            td_delivery_total = (
                td_delivery_fee
                + td_remote_fee
                + td_refund_fee
                + td_cod_fee
                + td_insurance_fee
            )
            td_service_other_total = (
                td_account_fee
                + td_hard_goods_fee
                + td_ffm_fee
                + td_confirm_trend_order_fee
                + td_confirm_hard_order_fee
                + td_mess_fee
                + td_mess_care_fee
                + td_telesale_care_fee
                + td_ship_discount_fee
            )
            td_conclusion_thb_minor = self._to_minor_amount(
                self._extract_first_value(row, conclusion_thb_paths_td),
                factor=td_amount_factor,
            )
            if td_conclusion_thb_minor is None:
                td_conclusion_thb_minor = td_amount_minor
            td_conclusion_vnd = self._to_minor_amount(
                self._extract_first_value(row, conclusion_vnd_paths_td),
                factor=1,
            )
            td_exchange_rate = self._to_optional_float(self._extract_first_value(row, exchange_rate_paths_td))
            if td_exchange_rate is None or td_exchange_rate <= 0:
                td_exchange_rate = float(self.settings.report_thb_to_vnd_rate)
            conclusion_vnd_is_estimated = False
            if td_conclusion_vnd is None and td_conclusion_thb_minor is not None:
                thb_major = float(td_conclusion_thb_minor) / float(td_amount_factor)
                td_conclusion_vnd = int(round(thb_major * td_exchange_rate))
                conclusion_vnd_is_estimated = True
            td_fee_minor = self._to_minor_amount(
                self._extract_first_value(row, fee_paths_td),
                factor=td_fee_factor,
            )
            td_detail_settlement_date = str(self._extract_first_value(row, detail_settlement_paths_td) or "").strip()
            td_send_date = str(self._extract_first_value(row, send_date_paths_td) or "").strip()
            phone_name_key = self._phone_name_key(td_phone, td_name)
            amount_key = self._identity_amount_key(phone_name_key, td_amount_minor)

            awb_candidates = list(awb_index.get(td_awb, [])) if td_awb else []
            identity_candidates = list(identity_index.get(phone_name_key, [])) if phone_name_key else []
            identity_amount_candidates = list(identity_amount_index.get(amount_key, [])) if amount_key else []
            identity_original_amount_candidates = (
                list(identity_original_amount_index.get(amount_key, [])) if amount_key else []
            )
            match_tier = ""
            if len(awb_candidates) == 1:
                candidates = awb_candidates
                match_tier = "awb"
            else:
                if len(identity_candidates) == 1:
                    candidates = identity_candidates
                    match_tier = "identity"
                elif len(identity_candidates) > 1:
                    if identity_amount_candidates:
                        candidates = identity_amount_candidates
                        match_tier = "identity"
                    elif identity_original_amount_candidates:
                        candidates = identity_original_amount_candidates
                        match_tier = "identity_original_amount"
                    else:
                        candidates = []
                        match_tier = "identity"
                else:
                    candidates = awb_candidates if len(awb_candidates) > 1 else []
                    match_tier = "awb" if awb_candidates else ""

            target_status = self._resolve_target_status(status_map_cfg, td_status_key)
            target_status_int = self._to_optional_int(target_status)
            fingerprint_key = td_awb or phone_name_key or str(row)
            item_fingerprint = fingerprint(f"{settlement_date.isoformat()}|{fingerprint_key}")

            record: dict[str, Any] = {
                "fingerprint": item_fingerprint,
                "settlement_date": settlement_date.isoformat(),
                "td_awb": td_awb,
                "td_status": td_status_raw,
                "td_phone": td_phone,
                "td_customer_name": td_name,
                "td_detail_settlement_date": td_detail_settlement_date,
                "td_send_date": td_send_date,
                "td_cod_minor": td_amount_minor,
                "td_sheet_cod_minor": td_sheet_cod_minor,
                "td_amount_minor": td_amount_minor,
                "td_fee_minor": td_fee_minor,
                "td_delivery_fee": td_delivery_fee,
                "td_remote_fee": td_remote_fee,
                "td_refund_fee": td_refund_fee,
                "td_cod_fee": td_cod_fee,
                "td_insurance_fee": td_insurance_fee,
                "td_account_fee": td_account_fee,
                "td_hard_goods_fee": td_hard_goods_fee,
                "td_ffm_fee": td_ffm_fee,
                "td_confirm_trend_order_fee": td_confirm_trend_order_fee,
                "td_confirm_hard_order_fee": td_confirm_hard_order_fee,
                "td_mess_fee": td_mess_fee,
                "td_mess_care_fee": td_mess_care_fee,
                "td_telesale_care_fee": td_telesale_care_fee,
                "td_fulfillment_other_fee": td_fulfillment_other_fee,
                "td_ship_discount_fee": td_ship_discount_fee,
                "td_delivery_total": td_delivery_total,
                "td_service_other_total": td_service_other_total,
                "td_conclusion_thb_minor": td_conclusion_thb_minor,
                "td_conclusion_vnd": td_conclusion_vnd,
                "td_conclusion_vnd_is_estimated": conclusion_vnd_is_estimated,
                "td_exchange_rate": td_exchange_rate,
                "td_thb_minor_factor": td_amount_factor,
                "target_status": target_status_int,
                "match_result": "",
                "match_tier": match_tier,
                "reason": "",
                "pancake_order_id": "",
                "pancake_display_id": "",
                "pancake_status": None,
            }

            if not candidates:
                if len(identity_candidates) > 1:
                    record["match_result"] = "ambiguous"
                    record["reason"] = "Trùng phone+tên nhưng không chốt được theo giá trị đơn."
                    records.append(record)
                    continue
                if len(awb_candidates) > 1:
                    record["match_result"] = "ambiguous"
                    record["reason"] = "Khớp nhiều hơn 1 đơn Pancake theo AWB."
                    records.append(record)
                    continue
                record["match_result"] = "not_found"
                record["reason"] = "Không tìm thấy đơn Pancake tương ứng."
                records.append(record)
                continue

            if len(candidates) > 1:
                record["match_result"] = "ambiguous"
                tier_label = "AWB"
                if match_tier == "identity":
                    tier_label = "identity"
                elif match_tier == "identity_original_amount":
                    tier_label = "identity+gia_tri_goc"
                record["reason"] = f"Khớp nhiều hơn 1 đơn Pancake ở tầng {tier_label}."
                records.append(record)
                continue

            matched = candidates[0]
            meta = pancake_meta.get(id(matched), {})
            record["pancake_order_id"] = str(meta.get("order_id", "")).strip()
            record["pancake_display_id"] = str(meta.get("display_id", "")).strip()
            record["pancake_status"] = meta.get("status")

            if target_status_int is None:
                record["match_result"] = "unmapped_status"
                record["reason"] = "Trạng thái Thái Dương chưa map sang status Pancake."
                records.append(record)
                continue

            if self._to_optional_int(meta.get("status")) == target_status_int:
                record["match_result"] = "already_correct"
                record["reason"] = "Đơn Pancake đã ở đúng trạng thái."
                records.append(record)
                continue

            record["match_result"] = "matched_unique"
            if match_tier == "identity_original_amount":
                record["reason"] = "Khớp fallback identity theo giá trị gốc đơn và sẵn sàng cập nhật."
            elif match_tier == "identity":
                record["reason"] = "Khớp fallback identity và sẵn sàng cập nhật."
            else:
                record["reason"] = "Khớp duy nhất và sẵn sàng cập nhật."
            records.append(record)

        return records

    @staticmethod
    def _build_summary(records: list[dict[str, Any]]) -> dict[str, int]:
        counters = {
            "matched_unique": 0,
            "already_correct": 0,
            "ambiguous": 0,
            "not_found": 0,
            "unmapped_status": 0,
            "update_candidates": 0,
            "total": len(records),
        }
        for item in records:
            if not isinstance(item, dict):
                continue
            key = str(item.get("match_result", "")).strip()
            if key in counters:
                counters[key] += 1
            if key == "matched_unique" and item.get("target_status") is not None:
                counters["update_candidates"] += 1
        return counters

    def _build_conclusion_totals(
        self,
        records: list[dict[str, Any]],
        *,
        settlement: dict[str, Any] | None,
        match_cfg: dict[str, Any] | None,
    ) -> dict[str, Any]:
        td_cfg = match_cfg.get("thai_duong", {}) if isinstance(match_cfg, dict) and isinstance(match_cfg.get("thai_duong"), dict) else {}
        summary_thb_paths = self._as_list(td_cfg.get("summary_conclusion_thb_paths"))
        if not summary_thb_paths:
            summary_thb_paths = ["conclusion", "ket_luan_doi_soat_thb", "summary.conclusion"]
        summary_vnd_paths = self._as_list(td_cfg.get("summary_conclusion_vnd_paths"))
        if not summary_vnd_paths:
            summary_vnd_paths = ["conclusionVND", "conclusionVnd", "ket_luan_doi_soat_vnd", "summary.conclusionVND"]
        summary_rate_paths = self._as_list(td_cfg.get("summary_exchange_rate_paths"))
        if not summary_rate_paths:
            summary_rate_paths = ["currencyRate", "exchangeRate", "rate", "summary.currencyRate"]

        if isinstance(settlement, dict) and settlement:
            summary_thb = self._to_optional_float(self._extract_first_value(settlement, summary_thb_paths))
            summary_vnd = self._to_optional_float(self._extract_first_value(settlement, summary_vnd_paths))
            summary_rate = self._to_optional_float(self._extract_first_value(settlement, summary_rate_paths))
            if summary_rate is None or summary_rate <= 0:
                summary_rate = float(self.settings.report_thb_to_vnd_rate)

            if summary_thb is None and summary_vnd is not None and summary_rate > 0:
                summary_thb = summary_vnd / summary_rate
            converted_count = 0
            if (summary_vnd is None or (float(summary_vnd) <= 0 and float(summary_thb or 0) > 0)) and summary_thb is not None:
                summary_vnd = summary_thb * summary_rate
                converted_count = 1
            if summary_thb is not None and summary_vnd is not None:
                return {
                    "thb_total": round(summary_thb, 2),
                    "vnd_total": round(summary_vnd, 2),
                    "vnd_converted_count": int(converted_count),
                    "source": "settlement_summary",
                }

        thb_total = 0.0
        vnd_total = 0.0
        vnd_converted_count = 0
        for item in records:
            if not isinstance(item, dict):
                continue
            thb_minor = self._to_optional_int(item.get("td_conclusion_thb_minor"))
            thb_factor = max(
                1,
                self._to_int(
                    item.get("td_thb_minor_factor"),
                    fallback=self.settings.report_thb_minor_unit_factor,
                ),
            )
            if thb_minor is not None:
                thb_total += float(thb_minor) / float(thb_factor)
            vnd = self._to_optional_int(item.get("td_conclusion_vnd"))
            if vnd is not None:
                vnd_total += float(vnd)
                if bool(item.get("td_conclusion_vnd_is_estimated")):
                    vnd_converted_count += 1
        return {
            "thb_total": round(thb_total, 2),
            "vnd_total": round(vnd_total, 2),
            "vnd_converted_count": int(vnd_converted_count),
            "source": "detail_rows",
        }

    def _save_csv(self, report: dict[str, Any]) -> Path:
        settlement_date = str(report.get("settlement_date", "")).strip() or "unknown"
        generated = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.reconcile_cod_reports_dir / f"reconcile_cod_{settlement_date}_{generated}.csv"
        records = report.get("records", [])
        fieldnames = [
            "fingerprint",
            "settlement_date",
            "td_awb",
            "td_status",
            "td_phone",
            "td_customer_name",
            "td_detail_settlement_date",
            "td_send_date",
            "td_cod_minor",
            "td_sheet_cod_minor",
            "td_amount_minor",
            "td_fee_minor",
            "td_delivery_fee",
            "td_remote_fee",
            "td_refund_fee",
            "td_cod_fee",
            "td_insurance_fee",
            "td_account_fee",
            "td_hard_goods_fee",
            "td_ffm_fee",
            "td_confirm_trend_order_fee",
            "td_confirm_hard_order_fee",
            "td_mess_fee",
            "td_mess_care_fee",
            "td_telesale_care_fee",
            "td_fulfillment_other_fee",
            "td_ship_discount_fee",
            "td_delivery_total",
            "td_service_other_total",
            "td_conclusion_thb_minor",
            "td_conclusion_vnd",
            "td_conclusion_vnd_is_estimated",
            "td_exchange_rate",
            "match_result",
            "match_tier",
            "reason",
            "pancake_order_id",
            "pancake_display_id",
            "pancake_status",
            "target_status",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            if isinstance(records, list):
                for item in records:
                    if isinstance(item, dict):
                        writer.writerow(item)
        return path

    def _save_run(self, report: dict[str, Any]) -> Path:
        settlement_date = str(report.get("settlement_date", "")).strip() or "unknown"
        generated = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"run_{settlement_date}_{generated}"
        path = self.settings.reconcile_cod_runs_dir / f"{run_id}.json"
        dump_json(path, report)
        return path

    def _load_applied_fingerprints(self, settlement_date: date) -> set[str]:
        path = self.settings.reconcile_cod_applied_dir / f"{settlement_date.isoformat()}.json"
        if not path.exists():
            return set()
        payload = load_json(path)
        if not isinstance(payload, list):
            return set()
        return {str(item).strip() for item in payload if str(item).strip()}

    def _save_applied_fingerprints(self, settlement_date: date, values: set[str]) -> None:
        path = self.settings.reconcile_cod_applied_dir / f"{settlement_date.isoformat()}.json"
        dump_json(path, sorted(values))

    def _load_match_config(self) -> dict[str, Any]:
        path = self.settings.reconcile_cod_match_config_path
        if path.exists():
            payload = load_json(path)
            if isinstance(payload, dict):
                return payload
        return self._default_match_config()

    def _load_status_map_config(self) -> dict[str, Any]:
        path = self.settings.reconcile_cod_status_map_config_path
        if path.exists():
            payload = load_json(path)
            if isinstance(payload, dict):
                return payload
        return {"enabled": False, "mapping": {}, "update_endpoint": {}}

    @staticmethod
    def _default_match_config() -> dict[str, Any]:
        return {
            "thai_duong": {
                "settlement_date_paths": ["Ngày trả tiền COD", "settlement_date"],
                "detail_settlement_date_paths": ["Ngày đối soát", "settlement_date"],
                "send_date_paths": ["Ngày gửi đơn", "send_date", "created_at"],
                "awb_paths": ["Mã vận đơn", "awb", "tracking_code", "tracking_number"],
                "status_paths": ["Trạng thái", "status", "delivery_status"],
                "phone_paths": ["Số điện thoại", "phone", "bill_phone_number", "customer_phone"],
                "customer_name_paths": ["Tên khách hàng", "customer_name", "receiver_name"],
                "amount_paths": ["COD", "Tiền COD", "cod_amount", "amount"],
                "sheet_cod_paths": [
                    "codFromCustomer",
                    "cod_transfered",
                    "codTransferred",
                    "collectedCod",
                    "collected_cod",
                ],
                "fee_paths": ["Phí", "fee", "shipping_fee", "delivery_fee"],
                "conclusion_thb_paths": ["codRemain", "COD con lai", "ket_luan_doi_soat_thb", "cod"],
                "conclusion_vnd_paths": ["codVnd", "Kết luận đối soát (VNĐ)", "ket_luan_doi_soat_vnd"],
                "exchange_rate_paths": ["exchangeRate", "ty_gia", "rate"],
                "summary_conclusion_thb_paths": ["conclusion", "ket_luan_doi_soat_thb"],
                "summary_conclusion_vnd_paths": ["conclusionVND", "conclusionVnd", "ket_luan_doi_soat_vnd"],
                "summary_exchange_rate_paths": ["currencyRate", "exchangeRate", "rate"],
                "amount_minor_unit_factor": 100,
                "fee_minor_unit_factor": 100,
            },
            "pancake": {
                "awb_paths": [
                    "shippingOrderCode",
                    "shipping_order_code",
                    "shipments[].tracking_number",
                    "shipments[].bill_no",
                    "shipments[].tracking_code",
                    "third_party_id",
                    "third_party_infomation.tracking_number",
                    "additional_info.awb",
                    "additional_info.tracking_number",
                    "custom_id",
                ],
                "phone_paths": [
                    "bill_phone_number",
                    "shipping_address.phone_number",
                    "customer.shop_customer.phone_numbers[]",
                    "customer.shop_customer.shop_customer_addresses[].phone_number",
                ],
                "customer_name_paths": [
                    "bill_full_name",
                    "shipping_address.full_name",
                    "customer.name",
                    "customer.shop_customer.shop_customer_addresses[].full_name",
                ],
                "amount_paths": ["cod", "total_price"],
                "original_amount_paths": [
                    "items[].variation_info.retail_price_currency_original",
                    "items[].variation_info.retail_price_wholesale_original",
                    "items[].variation_info.retail_price",
                    "items[].variation_info.retail_price_by_weight",
                    "items[].variation_info.retail_price_original",
                ],
                "status_paths": ["status"],
                "display_id_paths": ["custom_id", "display_id"],
                "order_id_paths": ["id"],
                "amount_minor_unit_factor": 1,
            },
        }

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _extract_first_value(self, payload: dict[str, Any], paths: list[str]) -> Any:
        for path in paths:
            values = self._extract_values(payload, [path])
            for value in values:
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
        return None

    def _extract_values(self, payload: Any, paths: list[str]) -> list[Any]:
        results: list[Any] = []
        for path in paths:
            tokens = [token.strip() for token in str(path).split(".") if token.strip()]
            results.extend(self._walk_path(payload, tokens))
        compact: list[Any] = []
        for value in results:
            if isinstance(value, list):
                compact.extend(value)
            else:
                compact.append(value)
        return compact

    def _walk_path(self, current: Any, tokens: list[str]) -> list[Any]:
        if not tokens:
            return [current]
        token = tokens[0]
        is_list = token.endswith("[]")
        key = token[:-2] if is_list else token

        if isinstance(current, dict):
            if key not in current:
                return []
            next_value = current.get(key)
            if is_list:
                if not isinstance(next_value, list):
                    return []
                values: list[Any] = []
                for item in next_value:
                    values.extend(self._walk_path(item, tokens[1:]))
                return values
            return self._walk_path(next_value, tokens[1:])

        if isinstance(current, list):
            values: list[Any] = []
            for item in current:
                values.extend(self._walk_path(item, tokens))
            return values
        return []

    @staticmethod
    def _normalize_awb(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            return ""
        return re.sub(r"[^A-Z0-9]", "", raw)

    @staticmethod
    def _normalize_phone(value: Any) -> str:
        digits = re.sub(r"[^\d]", "", str(value or ""))
        if not digits:
            return ""
        if digits.startswith("84") and len(digits) >= 10:
            digits = digits[2:]
        if digits.startswith("66") and len(digits) >= 9:
            digits = digits[2:]
        if digits.startswith("0"):
            digits = digits[1:]
        return digits

    @staticmethod
    def _normalize_name(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        folded = unicodedata.normalize("NFD", raw)
        no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
        lowered = no_accents.lower().replace("đ", "d")
        normalized = re.sub(r"\s+", " ", lowered).strip()
        return normalized

    @staticmethod
    def _identity_key(phone: str, name: str, amount_minor: int | None) -> str:
        if not phone or not name or amount_minor is None:
            return ""
        return f"{phone}|{name}|{amount_minor}"

    @staticmethod
    def _phone_name_key(phone: str, name: str) -> str:
        if not phone or not name:
            return ""
        return f"{phone}|{name}"

    @staticmethod
    def _identity_amount_key(phone_name_key: str, amount_minor: int | None) -> str:
        if not phone_name_key or amount_minor is None:
            return ""
        return f"{phone_name_key}|{amount_minor}"

    @staticmethod
    def _to_minor_amount(value: Any, factor: int) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        cleaned = text.replace(",", "").replace(" ", "")
        cleaned = re.sub(r"[^\d\.-]", "", cleaned)
        if not cleaned:
            return None
        try:
            numeric = float(cleaned)
        except ValueError:
            return None
        return int(round(numeric * max(1, factor)))

    def _extract_minor_amount_values(self, payload: dict[str, Any], paths: list[str], factor: int) -> set[int]:
        values: set[int] = set()
        if not paths:
            return values
        for raw in self._extract_values(payload, paths):
            amount = self._to_minor_amount(raw, factor=factor)
            if amount is not None:
                values.add(amount)
        return values

    def _extract_fee_map(self, payload: dict[str, Any]) -> dict[str, float]:
        result: dict[str, float] = {}
        fees = payload.get("fees", [])
        if not isinstance(fees, list):
            return result
        for item in fees:
            if not isinstance(item, dict):
                continue
            key = str(item.get("fieldName", "")).strip()
            if not key:
                continue
            value = self._to_optional_float(item.get("value"))
            if value is None:
                continue
            result[key] = result.get(key, 0.0) + float(value)
        return result

    @staticmethod
    def _resolve_target_status(status_map_cfg: dict[str, Any], normalized_status: str) -> int | None:
        mapping = status_map_cfg.get("mapping", {}) if isinstance(status_map_cfg.get("mapping"), dict) else {}
        if not normalized_status:
            return None
        candidate = mapping.get(normalized_status)
        if isinstance(candidate, dict):
            return ReconcileCodService._to_optional_int(candidate.get("status"))
        return ReconcileCodService._to_optional_int(candidate)

    def _try_apply_with_transition_fallback(
        self,
        *,
        order_id: str,
        target_status: int,
        record: dict[str, Any],
        error: Exception,
        update_cfg: dict[str, Any],
        transition_cfg: dict[str, Any],
    ) -> bool:
        if not self._can_apply_transition_fallback(record=record, error=error, transition_cfg=transition_cfg):
            return False

        intermediate_status = self._to_optional_int(transition_cfg.get("intermediate_status"))
        if intermediate_status is None:
            intermediate_status = 2
        if intermediate_status is None or intermediate_status == target_status:
            return False

        display_id = str(record.get("pancake_display_id", "")).strip() or order_id
        try:
            self.pancake.update_order_status(order_id, intermediate_status, update_cfg=update_cfg)
            self.pancake.update_order_status(order_id, target_status, update_cfg=update_cfg)
            self.logger.info(
                "Fallback transition thanh cong cho don %s: %s -> %s",
                display_id,
                intermediate_status,
                target_status,
            )
            return True
        except Exception as fallback_exc:  # noqa: BLE001
            self.logger.warning(
                "Fallback transition that bai cho don %s: %s",
                display_id,
                fallback_exc,
            )
            return False

    def _can_apply_transition_fallback(
        self,
        *,
        record: dict[str, Any],
        error: Exception,
        transition_cfg: dict[str, Any],
    ) -> bool:
        enabled = transition_cfg.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "off", "no"}
        if not bool(enabled):
            return False

        current_status = self._to_optional_int(record.get("pancake_status"))
        from_statuses = self._to_status_set(transition_cfg.get("from_statuses"))
        if not from_statuses:
            from_statuses = {1, 13}
        if current_status is None or current_status not in from_statuses:
            return False

        error_keys = self._to_normalized_keywords(transition_cfg.get("retry_if_error_contains"))
        if not error_keys:
            error_keys = {
                self._normalize_compare_text("chưa có thông tin sản phẩm"),
                self._normalize_compare_text("chua co thong tin san pham"),
            }
        error_text = self._normalize_compare_text(str(error))
        return any(keyword in error_text for keyword in error_keys)

    @staticmethod
    def _to_status_set(value: Any) -> set[int]:
        result: set[int] = set()
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif value is None:
            items = []
        else:
            items = [value]
        for item in items:
            normalized = ReconcileCodService._to_optional_int(item)
            if normalized is not None:
                result.add(normalized)
        return result

    @staticmethod
    def _to_normalized_keywords(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif value is None:
            items = []
        else:
            items = [value]
        for item in items:
            text = ReconcileCodService._normalize_compare_text(str(item))
            if text:
                result.add(text)
        return result

    @staticmethod
    def _normalize_compare_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(text or "").lower())
        stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", stripped).strip()

    @staticmethod
    def _to_optional_int(value: Any) -> int | None:
        try:
            if value is None or str(value).strip() == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_optional_float(value: Any) -> float | None:
        try:
            if value is None or str(value).strip() == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _format_vnd(value: Any) -> str:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 0.0
        if abs(amount - round(amount)) < 1e-9:
            return f"{amount:,.0f}"
        return f"{amount:,.2f}"

    @staticmethod
    def _format_thb(value: Any) -> str:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = 0.0
        if abs(amount - round(amount)) < 1e-9:
            return f"{amount:,.0f}"
        return f"{amount:,.2f}"

    @staticmethod
    def _format_rate(value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return f"{value:,.0f}"
        return f"{value:,.2f}"

    @staticmethod
    def _short_text(value: Any, limit: int = 260) -> str:
        normalized = " ".join(str(value or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    @staticmethod
    def _parse_date(raw: str) -> date | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if "T" in value and len(value) >= 10:
            value = value[:10]
        patterns = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y")
        for pattern in patterns:
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                continue
        return None
