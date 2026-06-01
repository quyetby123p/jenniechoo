from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
import time
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo

from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import dump_json, load_json, now_utc_iso


class PancakeToThaiDuongSyncService:
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
        self._product_cache_at: datetime | None = None
        self._product_index: dict[str, list[dict[str, Any]]] = {}
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        for path in (
            self.settings.pancake_td_sync_runs_dir,
            self.settings.pancake_td_sync_state_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def sync_once(
        self,
        *,
        force_today_local: bool = False,
        ignore_pause: bool = False,
        max_batch: int | None = None,
        manual_order_code: str | None = None,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        state = self._load_state()
        cfg = self._load_sync_config()
        color_alias = self._load_color_alias()
        payload_template = self._load_payload_template()
        poll_cfg = cfg.get("poll", {}) if isinstance(cfg.get("poll"), dict) else {}
        notify_cfg = cfg.get("notify", {}) if isinstance(cfg.get("notify"), dict) else {}
        overlap_seconds = self._to_int(poll_cfg.get("cursor_overlap_seconds"), fallback=120)
        max_lookback_hours = self._to_int(poll_cfg.get("max_lookback_hours"), fallback=24)
        manual_order_lookup_hours = max(1, self._to_int(poll_cfg.get("manual_order_lookup_hours"), fallback=24 * 30))
        today_only_local = bool(poll_cfg.get("today_only_local", False))
        auth_error_pause_minutes = self._to_int(poll_cfg.get("auth_error_pause_minutes"), fallback=20)
        failed_order_retry_minutes = max(1, self._to_int(poll_cfg.get("failed_order_retry_minutes"), fallback=5))
        pancake_fetch_error_pause_seconds = max(
            30,
            self._to_int(poll_cfg.get("pancake_fetch_error_pause_seconds"), fallback=300),
        )
        pancake_fetch_error_notify_cooldown_minutes = max(
            1,
            self._to_int(notify_cfg.get("pancake_fetch_error_notify_cooldown_minutes"), fallback=15),
        )
        configured_batch = max(1, int(self.settings.pancake_td_sync_batch_limit))
        max_batch = max(1, int(max_batch if max_batch is not None else configured_batch))
        end_ts = int(started_at.timestamp())
        pause_until_ts = self._to_int(state.get("td_auth_pause_until_ts"), fallback=0)
        if (not ignore_pause) and pause_until_ts > end_ts:
            return self._finalize_run(
                {
                    "ok": True,
                    "started_at": started_at.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "cursor_from": self._to_int(state.get("cursor_ts"), fallback=0),
                    "cursor_to": self._to_int(state.get("cursor_ts"), fallback=0),
                    "fetch_start_ts": 0,
                    "fetch_end_ts": end_ts,
                    "fetched": 0,
                    "considered": 0,
                    "created": 0,
                    "skipped_local_duplicate": 0,
                    "skipped_remote_duplicate": 0,
                    "skipped_unmapped": 0,
                    "skipped_repeated_error": 0,
                    "failed": 0,
                    "errors": [],
                    "created_order_ids": [],
                    "processed_order_ids": [],
                    "run_path": "",
                    "notify": False,
                    "notify_max_error_lines": self._to_int(notify_cfg.get("max_error_lines"), fallback=10),
                    "paused_until_ts": pause_until_ts,
                },
                state,
            )
        pancake_pause_until_ts = self._to_int(state.get("pancake_fetch_pause_until_ts"), fallback=0)
        if (not ignore_pause) and pancake_pause_until_ts > end_ts:
            return self._finalize_run(
                {
                    "ok": True,
                    "started_at": started_at.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "cursor_from": self._to_int(state.get("cursor_ts"), fallback=0),
                    "cursor_to": self._to_int(state.get("cursor_ts"), fallback=0),
                    "fetch_start_ts": 0,
                    "fetch_end_ts": end_ts,
                    "fetched": 0,
                    "considered": 0,
                    "created": 0,
                    "skipped_local_duplicate": 0,
                    "skipped_remote_duplicate": 0,
                    "skipped_unmapped": 0,
                    "skipped_repeated_error": 0,
                    "failed": 0,
                    "errors": [],
                    "created_order_ids": [],
                    "processed_order_ids": [],
                    "run_path": "",
                    "notify": False,
                    "notify_max_error_lines": self._to_int(notify_cfg.get("max_error_lines"), fallback=10),
                    "paused_until_ts": pancake_pause_until_ts,
                    "pause_reason": "pancake_fetch_error_backoff",
                },
                state,
            )
        cursor_ts = self._to_int(state.get("cursor_ts"), fallback=0)
        fetch_start_ts = max(0, cursor_ts - max(0, overlap_seconds))
        normalized_manual_order_code = self._normalize_reference(manual_order_code or "")
        if normalized_manual_order_code:
            fetch_start_ts = max(0, end_ts - (manual_order_lookup_hours * 3600))
        elif force_today_local:
            fetch_start_ts = self._day_start_local_unix(started_at)
        else:
            if max_lookback_hours > 0:
                fetch_start_ts = max(fetch_start_ts, end_ts - (max_lookback_hours * 3600))
            if today_only_local:
                fetch_start_ts = max(fetch_start_ts, self._day_start_local_unix(started_at))

        summary: dict[str, Any] = {
            "ok": True,
            "started_at": started_at.isoformat(),
            "finished_at": "",
            "cursor_from": cursor_ts,
            "cursor_to": cursor_ts,
            "fetch_start_ts": fetch_start_ts,
            "fetch_end_ts": end_ts,
            "fetched": 0,
            "considered": 0,
            "created": 0,
            "skipped_local_duplicate": 0,
            "skipped_remote_duplicate": 0,
            "skipped_unmapped": 0,
            "skipped_repeated_error": 0,
            "failed": 0,
            "errors": [],
            "created_order_ids": [],
            "processed_order_ids": [],
            "print_note_synced": 0,
            "print_note_failed": 0,
            "sale_status_synced": 0,
            "sale_status_failed": 0,
            "manual_order_code": normalized_manual_order_code,
            "run_path": "",
            "notify": False,
            "notify_max_error_lines": self._to_int(notify_cfg.get("max_error_lines"), fallback=10),
        }
        auth_error_blocked = False
        auth_error_text = ""

        try:
            orders = self.pancake.fetch_orders_by_timestamp_range(fetch_start_ts, end_ts)
        except Exception as exc:  # noqa: BLE001
            error_text = f"Lấy đơn Pancake thất bại: {exc}"
            fingerprint = self._hash_text(error_text)
            should_notify = self._should_notify_pancake_fetch_error(
                state=state,
                fingerprint=fingerprint,
                now=started_at,
                notify_cooldown_minutes=pancake_fetch_error_notify_cooldown_minutes,
            )
            summary["ok"] = False
            summary["failed"] = 1
            if should_notify:
                summary["errors"] = [error_text]
            else:
                summary["errors"] = []
                summary["skipped_repeated_error"] = 1
            notify_on_error = bool(notify_cfg.get("notify_on_error", True))
            summary["notify"] = bool(should_notify and notify_on_error)
            self._mark_pancake_fetch_error(
                state=state,
                fingerprint=fingerprint,
                error=error_text,
                now=started_at,
                notified=should_notify,
            )
            state["pancake_fetch_pause_until_ts"] = int(end_ts + pancake_fetch_error_pause_seconds)
            summary["pancake_fetch_pause_until_ts"] = state["pancake_fetch_pause_until_ts"]
            return self._finalize_run(summary, state)
        state.pop("pancake_fetch_pause_until_ts", None)
        state.pop("last_pancake_fetch_error", None)

        if not isinstance(orders, list):
            orders = []
        summary["fetched"] = len(orders)
        sorted_orders = self._sort_orders_by_created_at(orders, cfg)
        if normalized_manual_order_code:
            filtered_orders: list[dict[str, Any]] = []
            for order in sorted_orders:
                if not isinstance(order, dict):
                    continue
                order_id = self._normalize_reference(self._extract_order_id(order, cfg))
                order_code = self._normalize_reference(self._extract_order_code(order, cfg))
                if normalized_manual_order_code in {order_id, order_code}:
                    filtered_orders.append(order)
            sorted_orders = filtered_orders
            summary["fetched_matched"] = len(filtered_orders)
            if not filtered_orders:
                summary["ok"] = False
                summary["failed"] = 1
                summary["errors"] = [f"Không tìm thấy đơn Pancake mã {normalized_manual_order_code} trong dữ liệu gần đây."]
                summary["notify"] = True
                return self._finalize_run(summary, state)
        processed_store = state.get("processed_order_ids", {})
        if not isinstance(processed_store, dict):
            processed_store = {}
        failed_store = state.get("failed_order_ids", {})
        if not isinstance(failed_store, dict):
            failed_store = {}
        local_processed_ids = set(str(key).strip() for key in processed_store.keys() if str(key).strip())
        max_seen_ts = cursor_ts
        manual_force_retry_failed = bool(normalized_manual_order_code)

        for order in sorted_orders:
            if summary["considered"] >= max_batch:
                break
            if not isinstance(order, dict):
                continue
            summary["considered"] += 1

            order_id = self._extract_order_id(order, cfg)
            order_code = self._extract_order_code(order, cfg)
            order_created_ts = self._extract_order_created_ts(order, cfg)
            if order_created_ts > max_seen_ts:
                max_seen_ts = order_created_ts

            if not order_id:
                summary["failed"] += 1
                summary["errors"].append("Bỏ qua đơn Pancake thiếu order_id.")
                continue
            if order_id in local_processed_ids:
                summary["skipped_local_duplicate"] += 1
                if manual_force_retry_failed:
                    self._sync_existing_order_metadata_manual(
                        order_id=order_id,
                        order_code=order_code,
                        cfg=cfg,
                        summary=summary,
                    )
                continue
            order_fingerprint = self._order_fingerprint(order, cfg)
            suppress_repeated_error_notify = False
            known_failed = failed_store.get(order_id)
            if isinstance(known_failed, dict) and not manual_force_retry_failed:
                known_fp = str(known_failed.get("fingerprint") or "").strip()
                if known_fp and known_fp == order_fingerprint:
                    if not self._should_retry_failed_order(
                        known_failed,
                        retry_minutes=failed_order_retry_minutes,
                        now=started_at,
                    ):
                        summary["skipped_repeated_error"] += 1
                        continue
                    suppress_repeated_error_notify = True

            try:
                if self._exists_remote_order(order_id=order_id, order_code=order_code, cfg=cfg):
                    summary["skipped_remote_duplicate"] += 1
                    if manual_force_retry_failed:
                        self._sync_existing_order_metadata_manual(
                            order_id=order_id,
                            order_code=order_code,
                            cfg=cfg,
                            summary=summary,
                        )
                    local_processed_ids.add(order_id)
                    processed_store[order_id] = now_utc_iso()
                    failed_store.pop(order_id, None)
                    summary["processed_order_ids"].append(order_id)
                    continue
            except Exception as exc:  # noqa: BLE001
                if self._is_auth_error(exc):
                    auth_error_blocked = True
                    auth_error_text = str(exc)
                    summary["failed"] += 1
                    summary["ok"] = False
                    summary["errors"].append(
                        "Tạm dừng sync vì token Thái Dương không hợp lệ/hết hạn (401 Unauthorized)."
                    )
                    break
                if suppress_repeated_error_notify:
                    summary["skipped_repeated_error"] += 1
                else:
                    summary["failed"] += 1
                    summary["errors"].append(f"Kiểm tra trùng từ Thái Dương thất bại cho đơn {order_id}: {exc}")
                self._mark_failed_order(
                    failed_store=failed_store,
                    order_id=order_id,
                    order_code=order_code,
                    fingerprint=order_fingerprint,
                    error=str(exc),
                )
                continue

            mapped_items: list[dict[str, Any]] = []
            map_error: Exception | None = None
            try:
                mapped_items = self._map_order_items(order, cfg, color_alias)
            except Exception as exc:  # noqa: BLE001
                map_error = exc
                if self._is_sku_not_found_map_error(exc):
                    self.logger.info(
                        "Map SKU that bai cho don %s, thu refresh danh muc Thai Duong va map lai 1 lan.",
                        order_code or order_id,
                    )
                    self._invalidate_product_cache()
                    try:
                        mapped_items = self._map_order_items(order, cfg, color_alias)
                        map_error = None
                    except Exception as retry_exc:  # noqa: BLE001
                        map_error = retry_exc
            if map_error is not None:
                exc = map_error
                if suppress_repeated_error_notify:
                    summary["skipped_repeated_error"] += 1
                else:
                    summary["failed"] += 1
                    summary["skipped_unmapped"] += 1
                    label = order_code or order_id
                    summary["errors"].append(f"Map sản phẩm lỗi cho đơn {label}: {exc}")
                self._mark_failed_order(
                    failed_store=failed_store,
                    order_id=order_id,
                    order_code=order_code,
                    fingerprint=order_fingerprint,
                    error=str(exc),
                )
                continue
            if not mapped_items:
                if suppress_repeated_error_notify:
                    summary["skipped_repeated_error"] += 1
                else:
                    summary["failed"] += 1
                    summary["skipped_unmapped"] += 1
                    label = order_code or order_id
                    summary["errors"].append(f"Không map được sản phẩm nào cho đơn {label}.")
                self._mark_failed_order(
                    failed_store=failed_store,
                    order_id=order_id,
                    order_code=order_code,
                    fingerprint=order_fingerprint,
                    error="Khong map duoc san pham nao",
                )
                continue

            payment = self._resolve_payment(order, cfg, mapped_items)
            payload = self._build_payload(
                order=order,
                order_id=order_id,
                order_code=order_code,
                mapped_items=mapped_items,
                payment=payment,
                cfg=cfg,
                payload_template=payload_template,
            )
            create_cfg = self._thai_duong_create_cfg(cfg)
            retry_cfg = cfg.get("retry", {}) if isinstance(cfg.get("retry"), dict) else {}
            create_result: dict[str, Any] = {}
            try:
                create_result = self._create_remote_order_with_retry(payload, create_cfg, retry_cfg)
            except Exception as exc:  # noqa: BLE001
                if self._is_auth_error(exc):
                    auth_error_blocked = True
                    auth_error_text = str(exc)
                    summary["failed"] += 1
                    summary["ok"] = False
                    label = order_code or order_id
                    summary["errors"].append(
                        f"Tạo đơn Thái Dương bị chặn vì token không hợp lệ/hết hạn cho đơn {label} (401 Unauthorized)."
                    )
                    break
                if suppress_repeated_error_notify:
                    summary["skipped_repeated_error"] += 1
                else:
                    summary["failed"] += 1
                    label = order_code or order_id
                    summary["errors"].append(f"Tạo đơn Thái Dương lỗi cho đơn {label}: {exc}")
                self._mark_failed_order(
                    failed_store=failed_store,
                    order_id=order_id,
                    order_code=order_code,
                    fingerprint=order_fingerprint,
                    error=str(exc),
                )
                continue

            self._sync_sale_status_to_thai_duong(
                order_id=order_id,
                order_code=order_code,
                create_result=create_result,
                cfg=cfg,
                summary=summary,
            )
            self._sync_print_note_to_pancake(
                order_id=order_id,
                order_code=order_code,
                create_result=create_result,
                cfg=cfg,
                summary=summary,
            )
            summary["created"] += 1
            summary["created_order_ids"].append(order_id)
            local_processed_ids.add(order_id)
            processed_store[order_id] = now_utc_iso()
            failed_store.pop(order_id, None)
            summary["processed_order_ids"].append(order_id)

        state_retention_days = self._to_int(
            poll_cfg.get("state_retention_days"),
            fallback=60,
        )
        pruned = self._prune_processed_ids(processed_store, retention_days=state_retention_days)
        state["processed_order_ids"] = pruned
        state["failed_order_ids"] = self._prune_failed_orders(failed_store, retention_days=state_retention_days)
        state["cursor_ts"] = max(cursor_ts, max_seen_ts)
        if auth_error_blocked:
            state["td_auth_pause_until_ts"] = int(end_ts + max(5, auth_error_pause_minutes) * 60)
            summary["auth_pause_until_ts"] = state["td_auth_pause_until_ts"]
            if auth_error_text:
                summary["auth_error"] = self._short_text(auth_error_text, limit=360)
        else:
            state.pop("td_auth_pause_until_ts", None)
        summary["cursor_to"] = int(state["cursor_ts"])
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        notify_on_created = bool(notify_cfg.get("notify_on_created", True))
        notify_on_error = bool(notify_cfg.get("notify_on_error", True))
        notify_on_empty_poll = bool(notify_cfg.get("notify_on_empty_poll", False))
        summary["notify"] = bool(
            (summary["created"] > 0 and notify_on_created)
            or (summary["failed"] > 0 and notify_on_error)
            or (
                summary["created"] <= 0
                and summary["failed"] <= 0
                and notify_on_empty_poll
            )
        )
        return self._finalize_run(summary, state)

    def sync_today_manual(self) -> dict[str, Any]:
        manual_batch = max(2000, int(self.settings.pancake_td_sync_batch_limit))
        return self.sync_once(
            force_today_local=True,
            ignore_pause=True,
            max_batch=manual_batch,
        )

    def sync_order_code_manual(self, order_code: str) -> dict[str, Any]:
        normalized_order_code = self._normalize_reference(order_code or "")
        if not normalized_order_code:
            started_at = datetime.now(timezone.utc)
            state = self._load_state()
            report = {
                "ok": False,
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "cursor_from": self._to_int(state.get("cursor_ts"), fallback=0),
                "cursor_to": self._to_int(state.get("cursor_ts"), fallback=0),
                "fetch_start_ts": 0,
                "fetch_end_ts": int(started_at.timestamp()),
                "fetched": 0,
                "fetched_matched": 0,
                "considered": 0,
                "created": 0,
                "skipped_local_duplicate": 0,
                "skipped_remote_duplicate": 0,
                "skipped_unmapped": 0,
                "skipped_repeated_error": 0,
                "failed": 1,
                "errors": ["Mã đơn Pancake không hợp lệ."],
                "created_order_ids": [],
                "processed_order_ids": [],
                "print_note_synced": 0,
                "print_note_failed": 0,
                "sale_status_synced": 0,
                "sale_status_failed": 0,
                "manual_order_code": "",
                "run_path": "",
                "notify": True,
                "notify_max_error_lines": 10,
            }
            return self._finalize_run(report, state)
        manual_batch = max(2000, int(self.settings.pancake_td_sync_batch_limit))
        return self.sync_once(
            ignore_pause=True,
            max_batch=manual_batch,
            manual_order_code=normalized_order_code,
        )

    def build_message(self, report: dict[str, Any], trigger_label: str = "") -> str:
        lines: list[str] = []
        if trigger_label:
            lines.append(trigger_label)
        lines.extend(
            [
                "Đồng bộ Pancake -> Thái Dương",
                f"Tổng quan: {'OK' if report.get('ok') else 'LỖI'}",
                f"Tạo mới: {self._to_int(report.get('created')):,}",
                f"Lỗi: {self._to_int(report.get('failed')):,}",
                f"Ghi chú in đã cập nhật: {self._to_int(report.get('print_note_synced')):,}",
                f"Ghi chú in lỗi: {self._to_int(report.get('print_note_failed')):,}",
                f"Sale xác nhận đã cập nhật: {self._to_int(report.get('sale_status_synced')):,}",
                f"Sale xác nhận lỗi: {self._to_int(report.get('sale_status_failed')):,}",
                f"Trùng local: {self._to_int(report.get('skipped_local_duplicate')):,}",
                f"Trùng remote: {self._to_int(report.get('skipped_remote_duplicate')):,}",
                f"Không map được: {self._to_int(report.get('skipped_unmapped')):,}",
                f"Lỗi đã báo trước: {self._to_int(report.get('skipped_repeated_error')):,}",
                f"Run: {report.get('run_path', '')}",
            ]
        )
        errors = report.get("errors", [])
        if isinstance(errors, list) and errors:
            lines.append("")
            lines.append("Lỗi chi tiết:")
            max_lines = min(
                max(1, self._to_int(report.get("notify_max_error_lines"), fallback=10)),
                len(errors),
            )
            for item in errors[:max_lines]:
                lines.append(f"- {self._short_text(str(item), limit=260)}")
            if len(errors) > max_lines:
                lines.append(f"- ... và {len(errors) - max_lines} lỗi khác")
        return "\n".join(lines)

    def _sync_existing_order_metadata_manual(
        self,
        *,
        order_id: str,
        order_code: str,
        cfg: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        rows = self._lookup_thai_duong_orders_with_retry(
            order_id=order_id,
            order_code=order_code,
            cfg=cfg,
            reference_candidates=[order_code, order_id],
        )
        if not rows:
            summary["failed"] += 1
            summary["errors"].append(
                f"Không tìm thấy đơn Thái Dương tương ứng để đồng bộ lại cho {order_code or order_id}."
            )
            return

        first_row = rows[0] if isinstance(rows[0], dict) else {}
        if not isinstance(first_row, dict) or not first_row:
            summary["failed"] += 1
            summary["errors"].append(
                f"Dữ liệu đơn Thái Dương không hợp lệ khi đồng bộ lại cho {order_code or order_id}."
            )
            return

        create_result_like = {
            **first_row,
            "data": dict(first_row),
            "result": dict(first_row),
        }
        errors_before = len(summary.get("errors", [])) if isinstance(summary.get("errors"), list) else 0
        self._sync_sale_status_to_thai_duong(
            order_id=order_id,
            order_code=order_code,
            create_result=create_result_like,
            cfg=cfg,
            summary=summary,
        )
        self._sync_print_note_to_pancake(
            order_id=order_id,
            order_code=order_code,
            create_result=create_result_like,
            cfg=cfg,
            summary=summary,
        )
        errors_after = len(summary.get("errors", [])) if isinstance(summary.get("errors"), list) else 0
        if errors_after > errors_before:
            summary["failed"] += 1

    def _finalize_run(self, summary: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        if not summary.get("finished_at"):
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        dump_json(self.settings.pancake_td_sync_state_file, state)
        run_id = datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
        run_path = self.settings.pancake_td_sync_runs_dir / f"{run_id}.json"
        dump_json(run_path, summary)
        summary["run_path"] = str(run_path)
        return summary

    def _extract_order_id(self, order: dict[str, Any], cfg: dict[str, Any]) -> str:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        paths = self._as_list(pancake_cfg.get("order_id_paths"))
        if not paths:
            paths = ["id"]
        value = self._extract_first_value(order, paths)
        return str(value or "").strip()

    def _extract_order_code(self, order: dict[str, Any], cfg: dict[str, Any]) -> str:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        paths = self._as_list(pancake_cfg.get("order_code_paths"))
        if not paths:
            paths = ["custom_id", "display_id", "code"]
        value = self._extract_first_value(order, paths)
        return str(value or "").strip()

    def _extract_order_created_ts(self, order: dict[str, Any], cfg: dict[str, Any]) -> int:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        ts_paths = self._as_list(pancake_cfg.get("order_created_ts_paths"))
        dt_paths = self._as_list(pancake_cfg.get("order_created_at_paths"))
        if not ts_paths:
            ts_paths = ["inserted_at_timestamp", "created_at_timestamp"]
        if not dt_paths:
            dt_paths = ["inserted_at", "created_at"]
        raw_ts = self._extract_first_value(order, ts_paths)
        as_int = self._to_int(raw_ts, fallback=0)
        if as_int > 0:
            return as_int
        raw_dt = str(self._extract_first_value(order, dt_paths) or "").strip()
        dt_value = self._parse_datetime(raw_dt)
        if dt_value is None:
            return 0
        return int(dt_value.timestamp())

    def _sort_orders_by_created_at(self, orders: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
        enriched: list[tuple[int, str, dict[str, Any]]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            created_ts = self._extract_order_created_ts(order, cfg)
            order_id = self._extract_order_id(order, cfg)
            enriched.append((created_ts, order_id, order))
        enriched.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in enriched]

    def _exists_remote_order(self, *, order_id: str, order_code: str, cfg: dict[str, Any]) -> bool:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        lookup_cfg = td_cfg.get("order_lookup_endpoint", {})
        if not isinstance(lookup_cfg, dict):
            return False
        lookup_value = str(order_code or order_id).strip() or str(order_id).strip()
        if not lookup_value:
            return False
        reference_filter_field = str(td_cfg.get("reference_filter_field", "")).strip()
        rows = self.thai_duong.find_orders_by_reference_for_sync(
            endpoint_cfg=lookup_cfg,
            reference_value=lookup_value,
            reference_filter_field=reference_filter_field,
            extra_filters=self._lookup_filters(td_cfg),
        )
        if not rows:
            return False
        reference_paths = self._as_list(td_cfg.get("order_reference_paths"))
        if not reference_paths:
            reference_paths = ["orderUID", "pancakeOrderId", "referenceCode", "reference", "note"]
        normalized_targets = {
            self._normalize_reference(order_id),
            self._normalize_reference(order_code),
            self._normalize_reference(lookup_value),
        }
        normalized_targets.discard("")
        for row in rows:
            for ref in self._extract_values(row, reference_paths):
                if self._normalize_reference(ref) in normalized_targets:
                    return True
        return False

    def _sync_sale_status_to_thai_duong(
        self,
        *,
        order_id: str,
        order_code: str,
        create_result: dict[str, Any],
        cfg: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        sale_cfg = self._resolve_sale_status_sync_cfg(cfg)
        if not sale_cfg.get("enabled"):
            return

        td_order_id = self._extract_thai_duong_order_id(
            create_result=create_result,
            order_id=order_id,
            order_code=order_code,
            cfg=cfg,
            sale_cfg=sale_cfg,
        )
        if not td_order_id:
            summary["sale_status_failed"] = self._to_int(summary.get("sale_status_failed")) + 1
            summary["errors"].append(
                f"Không lấy được ID đơn Thái Dương để chuyển trạng thái Sale xác nhận cho đơn {order_code or order_id}."
            )
            return

        payload: dict[str, Any] = {}
        order_status_field = str(sale_cfg.get("order_status_field", "orderStatus")).strip()
        order_status_value = str(sale_cfg.get("order_status_value", "SALE_CONFIRM")).strip() or "SALE_CONFIRM"
        if order_status_field:
            payload[order_status_field] = order_status_value
        order_confirm_status_field = str(sale_cfg.get("order_confirm_status_field", "orderConfirmStatus")).strip()
        order_confirm_status_value = str(sale_cfg.get("order_confirm_status_value", "")).strip()
        if order_confirm_status_field and order_confirm_status_value:
            payload[order_confirm_status_field] = order_confirm_status_value
        need_sale_field = str(sale_cfg.get("need_sale_field", "isNeedSale")).strip()
        if need_sale_field:
            payload[need_sale_field] = bool(sale_cfg.get("need_sale_value", False))

        endpoint_cfg = sale_cfg.get("update_endpoint")
        if not isinstance(endpoint_cfg, dict):
            endpoint_cfg = {}
        try:
            self.thai_duong.update_order_status_for_sync(
                order_id=td_order_id,
                payload=payload,
                endpoint_cfg=endpoint_cfg,
            )
            summary["sale_status_synced"] = self._to_int(summary.get("sale_status_synced")) + 1
        except Exception as exc:  # noqa: BLE001
            summary["sale_status_failed"] = self._to_int(summary.get("sale_status_failed")) + 1
            summary["errors"].append(
                f"Cập nhật Sale xác nhận lỗi cho đơn {order_code or order_id}: {exc}"
            )

    def _resolve_sale_status_sync_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        raw = td_cfg.get("sale_status_sync", {})
        if not isinstance(raw, dict):
            raw = {}
        endpoint = raw.get("update_endpoint", {})
        if not isinstance(endpoint, dict):
            endpoint = {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "order_id_paths": self._as_list(raw.get("order_id_paths"))
            or ["id", "data.id", "data.data.id", "data.order.id", "result.id"],
            "order_uid_paths": self._as_list(raw.get("order_uid_paths"))
            or ["orderUID", "data.orderUID", "data.data.orderUID", "data.order.orderUID", "result.orderUID"],
            "order_status_field": str(raw.get("order_status_field", "orderStatus")).strip() or "orderStatus",
            "order_status_value": str(raw.get("order_status_value", "SALE_CONFIRM")).strip() or "SALE_CONFIRM",
            "order_confirm_status_field": str(raw.get("order_confirm_status_field", "orderConfirmStatus")).strip(),
            "order_confirm_status_value": str(raw.get("order_confirm_status_value", "")).strip(),
            "need_sale_field": str(raw.get("need_sale_field", "isNeedSale")).strip() or "isNeedSale",
            "need_sale_value": bool(raw.get("need_sale_value", False)),
            "update_endpoint": {
                "base_url_env": str(endpoint.get("base_url_env", "THAI_DUONG_API_BASE_URL")).strip()
                or "THAI_DUONG_API_BASE_URL",
                "method": str(endpoint.get("method", "PUT")).strip().upper() or "PUT",
                "path": str(endpoint.get("path", "/api/v1/orders/update-status-order/{order_id}")).strip()
                or "/api/v1/orders/update-status-order/{order_id}",
                "use_session_login": bool(endpoint.get("use_session_login", True)),
                "login_path": str(endpoint.get("login_path", "")).strip(),
            },
        }

    def _extract_thai_duong_order_id(
        self,
        *,
        create_result: dict[str, Any],
        order_id: str,
        order_code: str,
        cfg: dict[str, Any],
        sale_cfg: dict[str, Any],
    ) -> str:
        order_id_paths = self._as_list(sale_cfg.get("order_id_paths"))
        candidate = str(self._extract_first_value(create_result, order_id_paths) or "").strip()
        if candidate:
            return candidate

        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        reference_paths = self._as_list(td_cfg.get("order_reference_paths"))
        if not reference_paths:
            reference_paths = ["orderUID", "pancakeOrderId", "referenceCode", "reference", "note"]

        td_order_uid = str(
            self._extract_first_value(create_result, self._as_list(sale_cfg.get("order_uid_paths"))) or ""
        ).strip()
        if not td_order_uid:
            note_cfg = self._resolve_print_note_sync_cfg(cfg)
            td_order_uid = self._extract_thai_duong_order_uid(
                create_result=create_result,
                order_id=order_id,
                order_code=order_code,
                cfg=cfg,
                note_cfg=note_cfg,
            )

        rows = self._lookup_thai_duong_orders_with_retry(
            order_id=order_id,
            order_code=order_code,
            cfg=cfg,
            reference_candidates=[td_order_uid, order_code, order_id],
        )
        if not rows:
            return ""

        normalized_targets = {
            self._normalize_reference(td_order_uid),
            self._normalize_reference(order_code),
            self._normalize_reference(order_id),
        }
        normalized_targets.discard("")
        for enforce_match in (True, False):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if enforce_match and normalized_targets:
                    if not any(
                        self._normalize_reference(ref) in normalized_targets
                        for ref in self._extract_values(row, reference_paths)
                    ):
                        continue
                candidate = str(self._extract_first_value(row, order_id_paths) or "").strip()
                if candidate:
                    return candidate
        return ""

    def _sync_print_note_to_pancake(
        self,
        *,
        order_id: str,
        order_code: str,
        create_result: dict[str, Any],
        cfg: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        note_cfg = self._resolve_print_note_sync_cfg(cfg)
        if not note_cfg.get("enabled"):
            return

        td_order_uid = self._extract_thai_duong_order_uid(
            create_result=create_result,
            order_id=order_id,
            order_code=order_code,
            cfg=cfg,
            note_cfg=note_cfg,
        )
        if not td_order_uid:
            summary["print_note_failed"] = self._to_int(summary.get("print_note_failed")) + 1
            summary["errors"].append(
                f"Không lấy được mã đơn Thái Dương để ghi chú in cho đơn {order_code or order_id}."
            )
            return

        note_text = self._render_print_note_text(
            template=str(note_cfg.get("template", "{thai_duong_order_uid}")),
            thai_duong_order_uid=td_order_uid,
            order_id=order_id,
            order_code=order_code,
        )
        update_endpoint_cfg = note_cfg.get("update_endpoint")
        if not isinstance(update_endpoint_cfg, dict):
            update_endpoint_cfg = {}
        try:
            self.pancake.update_order_note_print(
                order_id=order_id,
                note_text=note_text,
                update_cfg=update_endpoint_cfg,
            )
            summary["print_note_synced"] = self._to_int(summary.get("print_note_synced")) + 1
        except Exception as exc:  # noqa: BLE001
            summary["print_note_failed"] = self._to_int(summary.get("print_note_failed")) + 1
            summary["errors"].append(
                f"Ghi chú in Pancake lỗi cho đơn {order_code or order_id}: {exc}"
            )

    def _resolve_print_note_sync_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        raw = pancake_cfg.get("print_note_sync", {})
        if not isinstance(raw, dict):
            raw = {}
        update_endpoint = raw.get("update_endpoint", {})
        if not isinstance(update_endpoint, dict):
            update_endpoint = {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "template": str(raw.get("template", "{thai_duong_order_uid}")),
            "order_uid_paths": self._as_list(raw.get("order_uid_paths"))
            or [
                "orderUID",
                "data.orderUID",
                "data.data.orderUID",
                "data.order.orderUID",
                "result.orderUID",
            ],
            "update_endpoint": {
                "method": str(update_endpoint.get("method", "PUT")).strip().upper() or "PUT",
                "path": str(update_endpoint.get("path", "/shops/{shop_id}/orders/{order_id}")).strip()
                or "/shops/{shop_id}/orders/{order_id}",
                "note_print_field": str(update_endpoint.get("note_print_field", "note_print")).strip()
                or "note_print",
                "mirror_note_field": str(update_endpoint.get("mirror_note_field", "")).strip(),
                "safe_full_order_update": bool(update_endpoint.get("safe_full_order_update", True)),
                "strict_note_only": bool(update_endpoint.get("strict_note_only", True)),
                "verify_after_update": bool(update_endpoint.get("verify_after_update", True)),
                "verify_unchanged_fields": self._as_list(update_endpoint.get("verify_unchanged_fields"))
                or [
                    "__items_signature__",
                    "total_price",
                    "total_quantity",
                    "status",
                    "is_empty_cart",
                ],
                "extra_payload": update_endpoint.get("extra_payload", {})
                if isinstance(update_endpoint.get("extra_payload"), dict)
                else {},
            },
        }

    def _extract_thai_duong_order_uid(
        self,
        *,
        create_result: dict[str, Any],
        order_id: str,
        order_code: str,
        cfg: dict[str, Any],
        note_cfg: dict[str, Any],
    ) -> str:
        order_uid_paths = self._as_list(note_cfg.get("order_uid_paths"))
        candidate = str(self._extract_first_value(create_result, order_uid_paths) or "").strip()
        if candidate:
            return candidate

        rows = self._lookup_thai_duong_orders_with_retry(
            order_id=order_id,
            order_code=order_code,
            cfg=cfg,
            reference_candidates=[order_code, order_id],
        )
        if not rows:
            return ""

        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        reference_paths = self._as_list(td_cfg.get("order_reference_paths"))
        if not reference_paths:
            reference_paths = ["orderUID", "pancakeOrderId", "referenceCode", "reference", "note"]
        normalized_targets = {
            self._normalize_reference(order_code),
            self._normalize_reference(order_id),
        }
        normalized_targets.discard("")
        for enforce_match in (True, False):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if enforce_match and normalized_targets:
                    if not any(
                        self._normalize_reference(ref) in normalized_targets
                        for ref in self._extract_values(row, reference_paths)
                    ):
                        continue
                candidate = str(self._extract_first_value(row, order_uid_paths) or "").strip()
                if candidate:
                    return candidate
        return ""

    def _lookup_thai_duong_orders_with_retry(
        self,
        *,
        order_id: str,
        order_code: str,
        cfg: dict[str, Any],
        reference_candidates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        lookup_cfg = td_cfg.get("order_lookup_endpoint", {})
        if not isinstance(lookup_cfg, dict):
            return []

        references: list[str] = []
        for raw in reference_candidates or []:
            value = str(raw or "").strip()
            if value and value not in references:
                references.append(value)
        for raw in (order_code, order_id):
            value = str(raw or "").strip()
            if value and value not in references:
                references.append(value)
        if not references:
            return []

        reference_filter_field = str(td_cfg.get("reference_filter_field", "")).strip()
        extra_filters = self._lookup_filters(td_cfg)
        attempts, delays = self._resolve_post_create_lookup_retry_cfg(cfg)
        for attempt in range(attempts):
            for reference in references:
                try:
                    rows = self.thai_duong.find_orders_by_reference_for_sync(
                        endpoint_cfg=lookup_cfg,
                        reference_value=reference,
                        reference_filter_field=reference_filter_field,
                        extra_filters=extra_filters,
                    )
                except Exception:  # noqa: BLE001
                    continue
                if rows:
                    return rows
            if attempt >= (attempts - 1):
                break
            delay = delays[min(attempt, len(delays) - 1)] if delays else 0.0
            if delay > 0:
                time.sleep(delay)
        return []

    def _resolve_post_create_lookup_retry_cfg(self, cfg: dict[str, Any]) -> tuple[int, list[float]]:
        poll_cfg = cfg.get("poll", {}) if isinstance(cfg.get("poll"), dict) else {}
        attempts = max(1, self._to_int(poll_cfg.get("post_create_lookup_retry_attempts"), fallback=4))
        delays = self._as_float_list(
            poll_cfg.get("post_create_lookup_retry_delay_seconds", [0.5, 1.0, 2.0]),
        )
        if not delays:
            delays = [0.5, 1.0, 2.0]
        return attempts, delays

    @staticmethod
    def _render_print_note_text(
        *,
        template: str,
        thai_duong_order_uid: str,
        order_id: str,
        order_code: str,
    ) -> str:
        raw_template = str(template or "").strip() or "{thai_duong_order_uid}"
        try:
            rendered = raw_template.format(
                thai_duong_order_uid=str(thai_duong_order_uid or "").strip(),
                pancake_order_id=str(order_id or "").strip(),
                pancake_order_code=str(order_code or "").strip(),
            )
        except Exception:  # noqa: BLE001
            rendered = str(thai_duong_order_uid or "").strip()
        rendered = str(rendered or "").strip()
        if rendered:
            return rendered
        return str(thai_duong_order_uid or "").strip()

    def _map_order_items(
        self,
        order: dict[str, Any],
        cfg: dict[str, Any],
        color_alias: dict[str, str],
    ) -> list[dict[str, Any]]:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        item_paths = self._as_list(pancake_cfg.get("items_paths"))
        if not item_paths:
            item_paths = ["items[]"]
        raw_items = self._extract_values(order, item_paths)
        items = [item for item in raw_items if isinstance(item, dict)]
        if not items:
            return []

        td_index = self._load_product_index(cfg, color_alias)
        code_paths = self._as_list(pancake_cfg.get("item_code_paths"))
        color_paths = self._as_list(pancake_cfg.get("item_color_paths"))
        qty_paths = self._as_list(pancake_cfg.get("item_quantity_paths"))
        price_paths = self._as_list(pancake_cfg.get("item_price_paths"))
        name_paths = self._as_list(pancake_cfg.get("item_name_paths"))
        if not code_paths:
            code_paths = ["variation_info.sku", "variation_info.code", "sku", "code"]
        if not color_paths:
            color_paths = ["variation_info.color", "color", "variant.color"]
        if not qty_paths:
            qty_paths = ["quantity", "qty"]
        if not price_paths:
            price_paths = ["variation_info.retail_price", "price", "sale_price"]
        if not name_paths:
            name_paths = ["variation_info.name", "name", "product_name"]

        mapped_items: list[dict[str, Any]] = []
        for item in items:
            code_raw = str(self._extract_first_value(item, code_paths) or "").strip()
            color_raw = self._extract_item_color(item, color_paths)
            qty = self._to_int(self._extract_first_value(item, qty_paths), fallback=1)
            qty = max(1, qty)
            unit_price = self._to_optional_float(self._extract_first_value(item, price_paths))
            name = str(self._extract_first_value(item, name_paths) or "").strip()

            if not code_raw:
                raise ValueError("Thiếu mã SKU trong item Pancake.")
            code_keys = self._candidate_sku_keys(code_raw)
            if not code_keys:
                raise ValueError("SKU Pancake không hợp lệ.")
            color_keys = self._color_candidate_keys(color_raw, color_alias)

            candidates: list[dict[str, Any]] = []
            for code_key in code_keys:
                for color_key in color_keys:
                    candidates.extend(td_index.get(f"{code_key}|{color_key}", []))
            if not candidates:
                for code_key in code_keys:
                    candidates.extend(td_index.get(f"{code_key}|", []))
            if not candidates:
                raise ValueError(f"Không tìm thấy SKU '{code_raw}' màu '{color_raw or 'trống'}' trên Thái Dương.")
            pick = candidates[0]
            mapped_items.append(
                {
                    "product_id": pick.get("product_id"),
                    "variant_id": pick.get("variant_id"),
                    "sku": pick.get("sku"),
                    "color": pick.get("color"),
                    "quantity": qty,
                    "unit_price": unit_price,
                    "name": name or str(pick.get("name") or ""),
                }
            )
        return mapped_items

    def _extract_item_color(self, item: dict[str, Any], color_paths: list[str]) -> str:
        direct = str(self._extract_first_value(item, color_paths) or "").strip()
        if direct:
            return direct
        for field in self._extract_values(item, ["variation_info.fields[]", "fields[]"]):
            if not isinstance(field, dict):
                continue
            field_name = self._normalize_compare_text(str(field.get("name") or ""))
            if "mau" not in field_name and "color" not in field_name:
                continue
            value = str(field.get("value") or field.get("keyValue") or "").strip()
            if value:
                return value
        return ""

    def _color_candidate_keys(self, color_raw: str, alias_map: dict[str, str]) -> list[str]:
        text = str(color_raw or "").strip()
        if not text:
            return [""]
        result: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            normalized = self._normalize_compare_text(value)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            result.append(normalized)

        _add(text)
        normalized_text = self._normalize_compare_text(text)
        direct_alias = alias_map.get(normalized_text)
        if direct_alias:
            _add(direct_alias)

        for alias_key, alias_value in alias_map.items():
            if alias_key and alias_key in normalized_text:
                _add(alias_value)

        for token in re.split(r"[\s,:;/\-_]+", text):
            normalized_token = self._normalize_compare_text(token)
            if not normalized_token:
                continue
            _add(normalized_token)
            alias_value = alias_map.get(normalized_token)
            if alias_value:
                _add(alias_value)

        if not result:
            return [""]
        return result

    def _resolve_payment(
        self,
        order: dict[str, Any],
        cfg: dict[str, Any],
        mapped_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        method_paths = self._as_list(pancake_cfg.get("payment_method_paths"))
        transferred_paths = self._as_list(pancake_cfg.get("transferred_amount_paths"))
        deposit_paths = self._as_list(pancake_cfg.get("deposit_amount_paths"))
        total_paths = self._as_list(pancake_cfg.get("total_amount_paths"))
        transfer_keywords = self._to_keyword_set(pancake_cfg.get("payment_method_transfer_keywords"))
        deposit_keywords = self._to_keyword_set(pancake_cfg.get("payment_method_deposit_keywords"))
        cod_keywords = self._to_keyword_set(pancake_cfg.get("payment_method_cod_keywords"))
        money_minor_unit_factor = self._to_int(
            pancake_cfg.get("money_minor_unit_factor"),
            fallback=max(1, self._to_int(self.settings.report_thb_minor_unit_factor, fallback=1)),
        )
        if money_minor_unit_factor <= 0:
            money_minor_unit_factor = 1
        if not method_paths:
            method_paths = ["payment_method", "paymentType", "payment.type"]
        if not transferred_paths:
            transferred_paths = ["transferred_amount", "paid_amount", "codTransferred"]
        if not deposit_paths:
            deposit_paths = ["deposit_amount", "deposit", "codTransferred"]
        if not total_paths:
            total_paths = ["total_price", "total", "cod"]
        if not transfer_keywords:
            transfer_keywords = {"transfer", "chuyenkhoan", "prepaid", "thanhtoantruoc"}
        if not deposit_keywords:
            deposit_keywords = {"coc", "deposit"}
        if not cod_keywords:
            cod_keywords = {"cod", "cashondelivery", "thuho"}

        method_text = self._normalize_compare_text(str(self._extract_first_value(order, method_paths) or ""))
        transferred_amount = self._to_optional_float(self._extract_first_value(order, transferred_paths))
        deposit_amount = self._to_optional_float(self._extract_first_value(order, deposit_paths))
        total_amount = self._to_optional_float(self._extract_first_value(order, total_paths))
        transferred_amount = self._scale_money_to_major_unit(transferred_amount, money_minor_unit_factor)
        deposit_amount = self._scale_money_to_major_unit(deposit_amount, money_minor_unit_factor)
        total_amount = self._scale_money_to_major_unit(total_amount, money_minor_unit_factor)
        if total_amount is None:
            total_amount = self._scale_money_to_major_unit(
                self._sum_items(mapped_items),
                money_minor_unit_factor,
            )
        if total_amount is None:
            total_amount = 0.0

        has_transfer_keyword = any(keyword in method_text for keyword in transfer_keywords)
        has_deposit_keyword = any(keyword in method_text for keyword in deposit_keywords)
        has_cod_keyword = any(keyword in method_text for keyword in cod_keywords)
        has_transfer_amount = transferred_amount is not None and transferred_amount > 0
        has_transfer = has_transfer_keyword or (has_transfer_amount and not has_cod_keyword and not has_deposit_keyword)
        has_deposit = has_deposit_keyword
        if has_transfer:
            transferred = transferred_amount if transferred_amount is not None else total_amount
            return {
                "payment_type": "TRANSFER",
                "deposit_amount": self._round_money(transferred),
                "cod_amount": self._round_money(float(total_amount)),
                "rule": "transfer",
            }
        if (deposit_amount is not None and deposit_amount > 0) or has_deposit:
            real_deposit = max(0.0, deposit_amount or 0.0)
            cod_amount = max(0.0, float(total_amount) - real_deposit)
            return {
                "payment_type": "COD",
                "deposit_amount": self._round_money(real_deposit),
                "cod_amount": self._round_money(cod_amount),
                "rule": "deposit",
            }
        return {
            "payment_type": "COD",
            "deposit_amount": 0.0,
            "cod_amount": self._round_money(float(total_amount)),
            "rule": "cod",
        }

    def _build_payload(
        self,
        *,
        order: dict[str, Any],
        order_id: str,
        order_code: str,
        mapped_items: list[dict[str, Any]],
        payment: dict[str, Any],
        cfg: dict[str, Any],
        payload_template: dict[str, Any],
    ) -> dict[str, Any]:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        field_cfg = td_cfg.get("payload_fields", {})
        if not isinstance(field_cfg, dict):
            field_cfg = {}
        item_key_cfg = td_cfg.get("item_payload_keys", {})
        if not isinstance(item_key_cfg, dict):
            item_key_cfg = {}

        payload = copy.deepcopy(payload_template)
        payload_items = [self._build_payload_item(item, item_key_cfg) for item in mapped_items]
        payload_items = [item for item in payload_items if isinstance(item, dict) and item]

        order_label = order_code or order_id
        note = f"Pancake order {order_label}"
        customer_code = self._resolve_td_customer_code()
        need_sale_confirm_value = bool(field_cfg.get("need_sale_confirm_value", False))
        order_status_value = str(field_cfg.get("order_status_value", "SALE_CONFIRM")).strip() or "SALE_CONFIRM"
        fields = {
            "reference_order_id_path": order_id,
            "pancake_order_id_path": order_id,
            "order_code_path": order_label,
            "customer_code_path": customer_code,
            "payment_type_path": payment.get("payment_type"),
            "deposit_amount_path": payment.get("deposit_amount"),
            "cod_amount_path": payment.get("cod_amount"),
            "need_sale_confirm_path": need_sale_confirm_value,
            "order_status_path": order_status_value,
            "items_path": payload_items,
            "note_path": note,
        }
        for cfg_key, value in fields.items():
            path = str(field_cfg.get(cfg_key, "")).strip()
            if path:
                self._set_path(payload, path, value)

        extra_map = field_cfg.get("copy_from_order", {})
        if isinstance(extra_map, dict):
            for target_path, source_path_raw in extra_map.items():
                source_path = str(source_path_raw).strip()
                if not source_path:
                    continue
                value = self._extract_first_value(order, [source_path])
                self._set_path(payload, str(target_path), value)
        return payload

    def _build_payload_item(self, item: dict[str, Any], item_key_cfg: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        mapping = [
            ("sku", "sku", "sku"),
            ("quantity", "quantity", "quantity"),
            ("product_id", "product_id", ""),
            ("variant_id", "variant_id", ""),
            ("unit_price", "unit_price", ""),
            ("color", "color", ""),
            ("name", "name", ""),
        ]
        for cfg_key, source_key, default_key in mapping:
            raw_path = item_key_cfg.get(cfg_key, default_key)
            path = str(raw_path or "").strip()
            if not path:
                continue
            value = item.get(source_key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            result[path] = value
        return result

    def _resolve_td_customer_code(self) -> str:
        raw = str(os.getenv("THAI_DUONG_CUSTOMER_CODE", "")).strip()
        if raw:
            return raw
        raw = str(os.getenv("THAI_DUONG_AUTH_USERNAME", "")).strip()
        if raw:
            return raw
        token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
        claims = self._decode_jwt_payload(token)
        for key in ("userName", "customerCode", "username"):
            value = str(claims.get(key, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload_part = parts[1]
        padding = "=" * (-len(payload_part) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload_part + padding)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def _create_remote_order_with_retry(
        self,
        payload: dict[str, Any],
        create_cfg: dict[str, Any],
        retry_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        max_attempts = self._to_int(retry_cfg.get("max_attempts"), fallback=3)
        backoff = retry_cfg.get("backoff_seconds")
        backoff_seconds = [2, 5, 10]
        if isinstance(backoff, list):
            parsed = [self._to_int(item, fallback=0) for item in backoff]
            cleaned = [item for item in parsed if item >= 0]
            if cleaned:
                backoff_seconds = cleaned
        last_error: Exception | None = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                return self.thai_duong.create_order_for_sync(payload, endpoint_cfg=create_cfg)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= max_attempts:
                    break
                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                if delay > 0:
                    time.sleep(delay)
        if last_error:
            raise last_error
        raise RuntimeError("Tạo đơn Thái Dương thất bại không rõ nguyên nhân.")

    def _load_product_index(
        self,
        cfg: dict[str, Any],
        color_alias: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        if self._product_cache_at and self._product_index:
            age_seconds = (now - self._product_cache_at).total_seconds()
            refresh_seconds = max(60, int(self.settings.pancake_td_sync_product_refresh_minutes) * 60)
            if age_seconds < refresh_seconds:
                return self._product_index

        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        endpoint_cfg = td_cfg.get("product_endpoint", {})
        if not isinstance(endpoint_cfg, dict):
            raise ValueError("Thiếu cấu hình thai_duong.product_endpoint.")
        rows = self.thai_duong.fetch_products_for_sync(endpoint_cfg)
        index = self._build_product_index(rows, cfg, color_alias)
        if not index:
            raise ValueError("Danh mục sản phẩm Thái Dương rỗng hoặc không parse được.")
        self._product_cache_at = now
        self._product_index = index
        return index

    def _invalidate_product_cache(self) -> None:
        self._product_cache_at = None
        self._product_index = {}

    def _build_product_index(
        self,
        rows: list[dict[str, Any]],
        cfg: dict[str, Any],
        color_alias: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        product_cfg = td_cfg.get("product_mapping", {})
        if not isinstance(product_cfg, dict):
            product_cfg = {}
        code_paths = self._as_list(product_cfg.get("product_code_paths"))
        color_paths = self._as_list(product_cfg.get("product_color_paths"))
        name_paths = self._as_list(product_cfg.get("product_name_paths"))
        product_id_paths = self._as_list(product_cfg.get("product_id_paths"))
        variant_list_paths = self._as_list(product_cfg.get("variant_list_paths"))
        variant_code_paths = self._as_list(product_cfg.get("variant_code_paths"))
        variant_color_paths = self._as_list(product_cfg.get("variant_color_paths"))
        variant_id_paths = self._as_list(product_cfg.get("variant_id_paths"))
        variant_name_paths = self._as_list(product_cfg.get("variant_name_paths"))
        if not code_paths:
            code_paths = ["sku", "code", "productCode", "productSKU", "productSku"]
        if not color_paths:
            color_paths = ["color", "colorName"]
        if not name_paths:
            name_paths = ["name", "productName"]
        if not product_id_paths:
            product_id_paths = ["id", "productId"]
        if not variant_list_paths:
            variant_list_paths = ["variants[]", "productVariants[]", "children[]"]
        if not variant_code_paths:
            variant_code_paths = ["sku", "code", "variantCode", "variantSKU", "variantSku"]
        if not variant_color_paths:
            variant_color_paths = ["color", "colorName"]
        if not variant_id_paths:
            variant_id_paths = ["id", "variantId"]
        if not variant_name_paths:
            variant_name_paths = ["name", "variantName"]

        index: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            base_code = str(self._extract_first_value(row, code_paths) or "").strip()
            base_color = str(self._extract_first_value(row, color_paths) or "").strip()
            base_name = str(self._extract_first_value(row, name_paths) or "").strip()
            base_product_id = self._extract_first_value(row, product_id_paths)
            variants = [item for item in self._extract_values(row, variant_list_paths) if isinstance(item, dict)]

            if variants:
                for variant in variants:
                    code = str(self._extract_first_value(variant, variant_code_paths) or base_code).strip()
                    color = str(self._extract_first_value(variant, variant_color_paths) or base_color).strip()
                    name = str(self._extract_first_value(variant, variant_name_paths) or base_name).strip()
                    product_id = self._extract_first_value(variant, product_id_paths)
                    if product_id in (None, ""):
                        product_id = base_product_id
                    variant_id = self._extract_first_value(variant, variant_id_paths)
                    self._index_variant(
                        index=index,
                        sku=code,
                        color=color,
                        color_alias=color_alias,
                        payload={
                            "product_id": product_id,
                            "variant_id": variant_id,
                            "sku": code,
                            "color": color,
                            "name": name,
                        },
                    )
                continue

            self._index_variant(
                index=index,
                sku=base_code,
                color=base_color,
                color_alias=color_alias,
                payload={
                    "product_id": base_product_id,
                    "variant_id": self._extract_first_value(row, variant_id_paths),
                    "sku": base_code,
                    "color": base_color,
                    "name": base_name,
                },
            )
        return index

    def _index_variant(
        self,
        *,
        index: dict[str, list[dict[str, Any]]],
        sku: str,
        color: str,
        color_alias: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        code_keys = self._candidate_sku_keys(sku)
        if not code_keys:
            return
        color_key = self._normalize_color(color, color_alias)
        for code_key in code_keys:
            exact_key = f"{code_key}|{color_key}"
            base_key = f"{code_key}|"
            index.setdefault(exact_key, []).append(payload)
            index.setdefault(base_key, []).append(payload)

    @staticmethod
    def _candidate_sku_keys(sku: str) -> list[str]:
        raw = str(sku or "").strip()
        if not raw:
            return []

        compound = re.sub(r"\s*\|\s*", "|", raw)
        compound = re.sub(r"(?<=[A-Za-z0-9])(?=JC[A-Za-z0-9-])", "|", compound)
        compound_parts = [
            part.strip()
            for part in re.split(r"[|;\r\n]+", compound)
            if str(part or "").strip()
        ]
        if len(compound_parts) > 1:
            merged: list[str] = []
            merged_seen: set[str] = set()
            for part in compound_parts:
                for key in PancakeToThaiDuongSyncService._candidate_sku_keys(part):
                    if key in merged_seen:
                        continue
                    merged_seen.add(key)
                    merged.append(key)
            if merged:
                return merged

        result: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            normalized = PancakeToThaiDuongSyncService._normalize_sku(value)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            result.append(normalized)

        _add(raw)
        tokens = [token.strip() for token in raw.split("-") if token.strip()]
        if len(tokens) >= 2 and re.fullmatch(r"[A-Za-z]{2,}\d+", tokens[0]):
            _add("-".join(tokens[1:]))

        expanded_tokens: list[str] = []
        for token in tokens:
            expanded_tokens.extend(part.strip() for part in re.split(r"[:\s_/]+", token) if part.strip())
        if expanded_tokens:
            _add("".join(expanded_tokens))
            if re.fullmatch(r"[A-Za-z]{2,}\d+", expanded_tokens[0]):
                _add("".join(expanded_tokens[1:]))
                removable_tokens = {"kem", "mau", "color"}
                filtered_tail = [
                    token
                    for token in expanded_tokens[1:]
                    if PancakeToThaiDuongSyncService._normalize_compare_text(token) not in removable_tokens
                ]
                if filtered_tail and len(filtered_tail) != len(expanded_tokens[1:]):
                    _add("".join([expanded_tokens[0], *filtered_tail]))
            removable_desc_tokens = {
                "ao",
                "vay",
                "chan",
                "quan",
                "dam",
                "shirt",
                "skirt",
                "dress",
                "ren",
                "mau",
            }
            removable_color_tokens = {
                "kem",
                "trang",
                "white",
                "nude",
                "beige",
                "nau",
                "den",
                "be",
                "xanh",
                "do",
                "hong",
                "vang",
                "cam",
                "tim",
                "xam",
                "ghi",
                "mint",
                "brown",
                "black",
                "red",
                "pink",
                "yellow",
                "green",
                "blue",
            }
            cleaned = [
                token
                for token in expanded_tokens
                if PancakeToThaiDuongSyncService._normalize_compare_text(token) not in removable_desc_tokens
            ]
            if cleaned and len(cleaned) != len(expanded_tokens):
                _add("".join(cleaned))
                if re.fullmatch(r"[A-Za-z]{2,}\d+", cleaned[0]) and len(cleaned) > 1:
                    _add("".join(cleaned[1:]))
            cleaned_no_desc_or_color = [
                token
                for token in expanded_tokens
                if PancakeToThaiDuongSyncService._normalize_compare_text(token)
                not in (removable_desc_tokens | removable_color_tokens)
            ]
            if cleaned_no_desc_or_color and len(cleaned_no_desc_or_color) != len(expanded_tokens):
                _add("".join(cleaned_no_desc_or_color))
                if re.fullmatch(r"[A-Za-z]{2,}\d+", cleaned_no_desc_or_color[0]) and len(cleaned_no_desc_or_color) > 1:
                    _add("".join(cleaned_no_desc_or_color[1:]))
            color_tokens_for_compact = [
                token.upper()
                for token in removable_color_tokens
                if len(token) >= 3
            ]
            snapshot = list(result)
            for existing in snapshot:
                for color_token in color_tokens_for_compact:
                    if color_token and color_token in existing:
                        _add(existing.replace(color_token, ""))
        return result

    def _load_sync_config(self) -> dict[str, Any]:
        defaults = self._default_sync_config()
        path = self.settings.pancake_td_sync_config_path
        if not path.exists():
            return defaults
        payload = load_json(path)
        if not isinstance(payload, dict):
            return defaults
        return self._deep_merge(defaults, payload)

    def _load_color_alias(self) -> dict[str, str]:
        path = self.settings.pancake_td_color_alias_config_path
        if not path.exists():
            return {"kem": "trắng"}
        payload = load_json(path)
        if not isinstance(payload, dict):
            return {"kem": "trắng"}
        result: dict[str, str] = {}
        for key, value in payload.items():
            normalized_key = self._normalize_compare_text(str(key))
            normalized_value = self._normalize_compare_text(str(value))
            if normalized_key and normalized_value:
                result[normalized_key] = normalized_value
        if "kem" not in result:
            result["kem"] = self._normalize_compare_text("trắng")
        return result

    def _load_payload_template(self) -> dict[str, Any]:
        path = self.settings.thai_duong_order_payload_template_path
        if not path.exists():
            return {}
        payload = load_json(path)
        if isinstance(payload, dict):
            return payload
        return {}

    def _load_state(self) -> dict[str, Any]:
        path = self.settings.pancake_td_sync_state_file
        if not path.exists():
            return {"cursor_ts": 0, "processed_order_ids": {}}
        payload = load_json(path)
        if not isinstance(payload, dict):
            return {"cursor_ts": 0, "processed_order_ids": {}}
        payload.setdefault("cursor_ts", 0)
        payload.setdefault("processed_order_ids", {})
        payload.setdefault("failed_order_ids", {})
        payload.setdefault("pancake_fetch_pause_until_ts", 0)
        payload.setdefault("last_pancake_fetch_error", {})
        return payload

    def _prune_processed_ids(self, values: dict[str, Any], retention_days: int) -> dict[str, Any]:
        if retention_days <= 0:
            return values
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        result: dict[str, Any] = {}
        for key, raw in values.items():
            item_key = str(key).strip()
            if not item_key:
                continue
            parsed = self._parse_datetime(str(raw))
            if parsed is None or parsed >= cutoff:
                result[item_key] = str(raw)
        return result

    def _prune_failed_orders(self, values: dict[str, Any], retention_days: int) -> dict[str, Any]:
        if retention_days <= 0:
            return values
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        result: dict[str, Any] = {}
        for key, raw in values.items():
            order_id = str(key).strip()
            if not order_id:
                continue
            if not isinstance(raw, dict):
                continue
            at_text = str(raw.get("at") or "").strip()
            parsed = self._parse_datetime(at_text)
            if parsed is None or parsed >= cutoff:
                result[order_id] = raw
        return result

    def _should_retry_failed_order(
        self,
        failed_entry: dict[str, Any],
        *,
        retry_minutes: int,
        now: datetime,
    ) -> bool:
        if retry_minutes <= 0:
            return True
        at_text = str(failed_entry.get("at") or "").strip()
        parsed = self._parse_datetime(at_text)
        if parsed is None:
            return True
        elapsed_seconds = (now - parsed).total_seconds()
        return elapsed_seconds >= (retry_minutes * 60)

    def _mark_failed_order(
        self,
        *,
        failed_store: dict[str, Any],
        order_id: str,
        order_code: str,
        fingerprint: str,
        error: str,
    ) -> None:
        if not order_id:
            return
        failed_store[order_id] = {
            "order_code": str(order_code or "").strip(),
            "fingerprint": str(fingerprint or "").strip(),
            "error": self._short_text(str(error or ""), limit=220),
            "at": now_utc_iso(),
        }

    def _should_notify_pancake_fetch_error(
        self,
        *,
        state: dict[str, Any],
        fingerprint: str,
        now: datetime,
        notify_cooldown_minutes: int,
    ) -> bool:
        if not fingerprint:
            return True
        info = state.get("last_pancake_fetch_error")
        if not isinstance(info, dict):
            return True
        if str(info.get("fingerprint") or "").strip() != fingerprint:
            return True
        notified_at = self._parse_datetime(str(info.get("notified_at") or ""))
        if notified_at is None:
            return True
        elapsed_seconds = (now - notified_at).total_seconds()
        return elapsed_seconds >= max(1, notify_cooldown_minutes) * 60

    def _mark_pancake_fetch_error(
        self,
        *,
        state: dict[str, Any],
        fingerprint: str,
        error: str,
        now: datetime,
        notified: bool,
    ) -> None:
        previous = state.get("last_pancake_fetch_error")
        previous_notified_at = ""
        previous_count = 0
        if isinstance(previous, dict):
            previous_notified_at = str(previous.get("notified_at") or "").strip()
            previous_count = self._to_int(previous.get("count"), fallback=0)
        state["last_pancake_fetch_error"] = {
            "fingerprint": str(fingerprint or "").strip(),
            "error": self._short_text(str(error or ""), limit=260),
            "at": now.isoformat(),
            "notified_at": now.isoformat() if notified else previous_notified_at,
            "count": max(0, previous_count) + 1,
        }

    def _order_fingerprint(self, order: dict[str, Any], cfg: dict[str, Any]) -> str:
        pancake_cfg = cfg.get("pancake", {}) if isinstance(cfg.get("pancake"), dict) else {}
        code_paths = self._as_list(pancake_cfg.get("item_code_paths"))
        qty_paths = self._as_list(pancake_cfg.get("item_quantity_paths"))
        item_paths = self._as_list(pancake_cfg.get("items_paths"))
        method_paths = self._as_list(pancake_cfg.get("payment_method_paths"))
        transferred_paths = self._as_list(pancake_cfg.get("transferred_amount_paths"))
        deposit_paths = self._as_list(pancake_cfg.get("deposit_amount_paths"))
        total_paths = self._as_list(pancake_cfg.get("total_amount_paths"))
        updated_paths = ["updated_at", "updatedAt", "last_update_status_at", "inserted_at", "created_at", "createdAt"]

        if not code_paths:
            code_paths = ["variation_info.custom_id", "variation_info.id", "variation_info.product_id", "sku", "code"]
        if not qty_paths:
            qty_paths = ["quantity", "qty"]
        if not item_paths:
            item_paths = ["items[]"]

        item_signatures: list[str] = []
        for item in self._extract_values(order, item_paths):
            if not isinstance(item, dict):
                continue
            code = str(self._extract_first_value(item, code_paths) or "").strip()
            qty = self._to_int(self._extract_first_value(item, qty_paths), fallback=1)
            if not code:
                continue
            item_signatures.append(f"{self._normalize_sku(code)}:{max(1, qty)}")
        item_signatures.sort()

        payload = {
            "updated_at": str(self._extract_first_value(order, updated_paths) or ""),
            "payment_method": str(self._extract_first_value(order, method_paths) or ""),
            "transferred_amount": str(self._extract_first_value(order, transferred_paths) or ""),
            "deposit_amount": str(self._extract_first_value(order, deposit_paths) or ""),
            "total_amount": str(self._extract_first_value(order, total_paths) or ""),
            "items": item_signatures,
        }
        fingerprint_src = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        return hashlib.sha1(fingerprint_src.encode("utf-8")).hexdigest()

    def _lookup_filters(self, td_cfg: dict[str, Any]) -> dict[str, Any]:
        raw = td_cfg.get("order_lookup_filters", {})
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    def _thai_duong_create_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        td_cfg = cfg.get("thai_duong", {}) if isinstance(cfg.get("thai_duong"), dict) else {}
        endpoint_cfg = td_cfg.get("create_order_endpoint", {})
        if isinstance(endpoint_cfg, dict):
            return endpoint_cfg
        return {"method": "POST", "path": "/api/v1/orders"}

    @staticmethod
    def _default_sync_config() -> dict[str, Any]:
        return {
            "poll": {
                "cursor_overlap_seconds": 120,
                "state_retention_days": 60,
                "max_lookback_hours": 24,
                "manual_order_lookup_hours": 720,
                "today_only_local": True,
                "auth_error_pause_minutes": 20,
                "failed_order_retry_minutes": 5,
                "pancake_fetch_error_pause_seconds": 300,
                "post_create_lookup_retry_attempts": 4,
                "post_create_lookup_retry_delay_seconds": [0.5, 1.0, 2.0],
            },
            "retry": {
                "max_attempts": 3,
                "backoff_seconds": [2, 5, 10],
            },
            "notify": {
                "notify_on_created": True,
                "notify_on_error": True,
                "notify_on_empty_poll": False,
                "max_error_lines": 10,
                "pancake_fetch_error_notify_cooldown_minutes": 15,
            },
            "pancake": {
                "order_id_paths": ["id"],
                "order_code_paths": ["custom_id", "display_id", "code"],
                "order_created_ts_paths": ["inserted_at_timestamp", "created_at_timestamp"],
                "order_created_at_paths": ["inserted_at", "created_at"],
                "items_paths": ["items[]"],
                "item_code_paths": [
                    "variation_info.custom_id",
                    "variation_info.id",
                    "variation_info.product_id",
                    "variation_info.sku",
                    "variation_info.code",
                    "sku",
                    "code",
                ],
                "item_color_paths": ["variation_info.color", "color", "variant.color"],
                "item_quantity_paths": ["quantity", "qty"],
                "item_price_paths": ["variation_info.retail_price", "price", "sale_price"],
                "item_name_paths": ["variation_info.name", "name", "product_name"],
                "payment_method_paths": ["payment_method", "paymentType", "payment.type"],
                "transferred_amount_paths": [
                    "transferred_amount",
                    "transfer_money",
                    "paid_amount",
                    "codTransferred",
                ],
                "deposit_amount_paths": ["deposit_amount", "deposit", "codTransferred"],
                "total_amount_paths": ["total_price", "total", "cod"],
                "money_minor_unit_factor": 1,
                "payment_method_transfer_keywords": [
                    "transfer",
                    "chuyen khoan",
                    "thanh toan truoc",
                    "prepaid",
                ],
                "payment_method_deposit_keywords": ["coc", "deposit"],
                "payment_method_cod_keywords": ["cod", "thu ho", "cash on delivery"],
                "print_note_sync": {
                    "enabled": False,
                    "template": "{thai_duong_order_uid}",
                    "order_uid_paths": [
                        "orderUID",
                        "data.orderUID",
                        "data.data.orderUID",
                        "data.order.orderUID",
                        "result.orderUID",
                    ],
                    "update_endpoint": {
                        "method": "PUT",
                        "path": "/shops/{shop_id}/orders/{order_id}",
                        "note_print_field": "note_print",
                        "mirror_note_field": "",
                        "safe_full_order_update": True,
                        "strict_note_only": True,
                        "verify_after_update": True,
                        "verify_unchanged_fields": [
                            "__items_signature__",
                            "total_price",
                            "total_quantity",
                            "status",
                            "is_empty_cart",
                        ],
                        "extra_payload": {},
                    },
                },
            },
            "thai_duong": {
                "product_endpoint": {
                    "base_url_env": "THAI_DUONG_API_BASE_URL",
                    "token_env": "THAI_DUONG_API_TOKEN",
                    "token_header": "Authorization",
                    "token_prefix": "Bearer ",
                    "method": "GET",
                    "path": "/api/v1/products/sku",
                    "request_mode": "query",
                    "page_field": "page",
                    "page_size_field": "limit",
                    "page_size": 200,
                    "result_path": "data.data",
                    "total_pages_path": "data.total",
                    "has_next_page_path": "data.hasNextPage",
                    "search_field": "searchText",
                    "filters_field": "filters",
                    "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
                },
                "order_lookup_endpoint": {
                    "base_url_env": "THAI_DUONG_API_BASE_URL",
                    "token_env": "THAI_DUONG_API_TOKEN",
                    "token_header": "Authorization",
                    "token_prefix": "Bearer ",
                    "method": "POST",
                    "path": "/api/v1/orders/list",
                    "request_mode": "json_body",
                    "page_field": "page",
                    "page_size_field": "limit",
                    "page_size": 50,
                    "result_path": "data.data",
                    "total_pages_path": "data.total",
                    "has_next_page_path": "data.hasNextPage",
                    "search_field": "searchText",
                    "filters_field": "filters",
                    "body_template": {"searchText": "", "filters": {}, "orderBy": {}},
                },
                "create_order_endpoint": {
                    "base_url_env": "THAI_DUONG_API_BASE_URL",
                    "token_env": "THAI_DUONG_API_TOKEN",
                    "token_header": "Authorization",
                    "token_prefix": "Bearer ",
                    "method": "POST",
                    "path": "/api/v1/orders",
                },
                "sale_status_sync": {
                    "enabled": True,
                    "order_id_paths": ["id", "data.id", "data.data.id", "data.order.id", "result.id"],
                    "order_uid_paths": ["orderUID", "data.orderUID", "data.data.orderUID", "data.order.orderUID", "result.orderUID"],
                    "order_status_field": "orderStatus",
                    "order_status_value": "SALE_CONFIRM",
                    "order_confirm_status_field": "",
                    "order_confirm_status_value": "",
                    "need_sale_field": "isNeedSale",
                    "need_sale_value": False,
                    "update_endpoint": {
                        "base_url_env": "THAI_DUONG_API_BASE_URL",
                        "method": "PUT",
                        "path": "/api/v1/orders/update-status-order/{order_id}",
                        "use_session_login": True,
                        "login_path": "/api/v1/auth/login",
                    },
                },
                "reference_filter_field": "",
                "order_reference_paths": ["orderUID", "pancakeOrderId", "referenceCode", "reference", "note"],
                "order_lookup_filters": {},
                "product_mapping": {
                    "product_code_paths": ["sku", "code", "name", "productName", "product.productName"],
                    "product_color_paths": ["color", "colorName"],
                    "product_name_paths": ["name", "productName"],
                    "product_id_paths": ["product.id", "productId", "id"],
                    "variant_list_paths": ["variants[]", "productVariants[]", "children[]"],
                    "variant_code_paths": ["sku", "code", "variantCode", "variantSKU", "variantSku"],
                    "variant_color_paths": ["color", "colorName"],
                    "variant_id_paths": ["id", "variantId"],
                    "variant_name_paths": ["name", "variantName"],
                },
                "payload_fields": {
                    "reference_order_id_path": "orderUID",
                    "pancake_order_id_path": "pancakeOrderId",
                    "order_code_path": "",
                    "customer_code_path": "customerCode",
                    "payment_type_path": "paymentType",
                    "deposit_amount_path": "codTransferred",
                    "cod_amount_path": "cod",
                    "need_sale_confirm_path": "isNeedSale",
                    "need_sale_confirm_value": False,
                    "order_status_path": "orderStatus",
                    "order_status_value": "SALE_CONFIRM",
                    "items_path": "products",
                    "note_path": "note",
                    "copy_from_order": {},
                },
                "item_payload_keys": {
                    "quantity": "quantity",
                    "sku": "sku",
                },
            },
        }

    @staticmethod
    def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in patch.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = PancakeToThaiDuongSyncService._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _round_money(value: float) -> float:
        rounded = round(float(value), 2)
        if abs(rounded - round(rounded)) < 1e-9:
            return float(int(round(rounded)))
        return rounded

    @staticmethod
    def _sum_items(items: list[dict[str, Any]]) -> float | None:
        total = 0.0
        found = False
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = PancakeToThaiDuongSyncService._to_int(item.get("quantity"), fallback=0)
            unit_price = PancakeToThaiDuongSyncService._to_optional_float(item.get("unit_price"))
            if qty <= 0 or unit_price is None:
                continue
            found = True
            total += float(qty) * float(unit_price)
        if not found:
            return None
        return total

    @staticmethod
    def _normalize_reference(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(value or "").strip()).upper()

    @staticmethod
    def _hash_text(value: str) -> str:
        normalized = " ".join(str(value or "").lower().split())
        if not normalized:
            return ""
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_sku(value: Any) -> str:
        folded = PancakeToThaiDuongSyncService._normalize_compare_text(str(value or ""))
        return re.sub(r"[^a-z0-9]", "", folded).upper()

    @staticmethod
    def _normalize_color(value: Any, alias_map: dict[str, str]) -> str:
        normalized = PancakeToThaiDuongSyncService._normalize_compare_text(str(value or ""))
        if not normalized:
            return ""
        return alias_map.get(normalized, normalized)

    @staticmethod
    def _normalize_compare_text(value: str) -> str:
        folded = unicodedata.normalize("NFD", str(value or "").lower())
        no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
        return re.sub(r"[\s\-_]+", "", no_accents).strip()

    @staticmethod
    def _is_sku_not_found_map_error(error: Exception) -> bool:
        text = PancakeToThaiDuongSyncService._normalize_compare_text(str(error or ""))
        return "khongtimthaysku" in text

    @staticmethod
    def _to_keyword_set(value: Any) -> set[str]:
        items: list[Any]
        if isinstance(value, list):
            items = list(value)
        elif value is None:
            items = []
        else:
            items = [value]
        result: set[str] = set()
        for item in items:
            normalized = PancakeToThaiDuongSyncService._normalize_compare_text(str(item))
            if normalized:
                result.add(normalized)
        return result

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _short_text(value: str, limit: int = 260) -> str:
        normalized = " ".join(str(value or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    @staticmethod
    def _to_optional_float(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        cleaned = text.replace(",", "").replace(" ", "")
        cleaned = re.sub(r"[^\d\.\-]", "", cleaned)
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _scale_money_to_major_unit(value: float | None, factor: int) -> float | None:
        if value is None:
            return None
        if factor <= 1:
            return float(value)
        return float(value) / float(factor)

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def _as_float_list(value: Any) -> list[float]:
        items: list[Any]
        if isinstance(value, list):
            items = list(value)
        elif isinstance(value, str) and value.strip():
            items = [part.strip() for part in value.split(",")]
        else:
            items = []
        result: list[float] = []
        for item in items:
            try:
                numeric = float(item)
            except (TypeError, ValueError):
                continue
            result.append(max(0.0, numeric))
        return result

    def _extract_first_value(self, payload: dict[str, Any], paths: list[str]) -> Any:
        for path in paths:
            for value in self._extract_values(payload, [path]):
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
        for item in results:
            if isinstance(item, list):
                compact.extend(item)
            else:
                compact.append(item)
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
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        raw = str(path or "").strip()
        if not raw:
            return
        tokens: list[str] = []
        if "[" in raw and "]" in raw and "." not in raw:
            head = raw.split("[", 1)[0]
            tail = raw[len(head) :]
            if head:
                tokens.append(head)
            chunk = ""
            for ch in tail:
                if ch in "[]":
                    if chunk:
                        tokens.append(chunk)
                        chunk = ""
                    continue
                chunk += ch
            if chunk:
                tokens.append(chunk)
        else:
            tokens = [token.strip() for token in raw.split(".") if token.strip()]
        if not tokens:
            return
        current: dict[str, Any] = payload
        for token in tokens[:-1]:
            child = current.get(token)
            if not isinstance(child, dict):
                child = {}
                current[token] = child
            current = child
        current[tokens[-1]] = value

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _resolve_timezone(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return timezone(timedelta(hours=7))

    def _day_start_local_unix(self, now_utc: datetime) -> int:
        tzinfo = self._resolve_timezone()
        local_now = now_utc.astimezone(tzinfo)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(day_start.timestamp())

    @staticmethod
    def _is_auth_error(error: Exception) -> bool:
        text = str(error or "").lower()
        return "(401)" in text or "unauthorized" in text or "statuscode\":401" in text
