from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import csv
from glob import glob
import json
import logging
import os
from pathlib import Path
import copy
import threading
import time
from typing import Any

import requests

from app.exceptions import ValidationError
from app.settings import Settings
from app.utils import dump_json, load_json


class ThaiDuongCodClient:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._auto_auth_lock = threading.Lock()
        self._last_auto_auth_attempt_ts = 0.0

    def fetch_settlement_history(self, start_date: date, end_date: date) -> tuple[list[dict[str, Any]], str]:
        cfg = self._load_source_config()
        errors: list[str] = []
        if self._is_api_enabled(cfg):
            try:
                rows = self._fetch_history_api(cfg, start_date, end_date)
                return rows, "api"
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Lay lich su doi soat COD tu API that bai: %s", exc)
                errors.append(f"API: {exc}")

        if self._is_csv_enabled(cfg):
            try:
                rows = self._fetch_history_csv(cfg, start_date, end_date)
                return rows, "csv"
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Lay lich su doi soat COD tu CSV that bai: %s", exc)
                errors.append(f"CSV: {exc}")

        detail = "; ".join(errors).strip()
        hint = f" Chi tiet: {detail}" if detail else ""
        raise ValidationError(
            "Khong co nguon du lieu lich su doi soat COD kha dung (API/CSV)."
            + hint
        )

    def fetch_settlement_details(
        self,
        settlement_date: date,
        settlement: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        cfg = self._load_source_config()
        errors: list[str] = []
        if self._is_api_enabled(cfg):
            try:
                rows = self._fetch_detail_api(cfg, settlement_date, settlement or {})
                return rows, "api"
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Lay chi tiet doi soat COD tu API that bai: %s", exc)
                errors.append(f"API: {exc}")

        if self._is_csv_enabled(cfg):
            try:
                rows = self._fetch_detail_csv(cfg, settlement_date)
                return rows, "csv"
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Lay chi tiet doi soat COD tu CSV that bai: %s", exc)
                errors.append(f"CSV: {exc}")

        detail = "; ".join(errors).strip()
        hint = f" Chi tiet: {detail}" if detail else ""
        raise ValidationError("Khong co nguon du lieu chi tiet doi soat COD kha dung (API/CSV)." + hint)

    def fetch_products_for_sync(self, endpoint_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self._fetch_endpoint_rows(endpoint_cfg=endpoint_cfg, search_text="")
        return [item for item in rows if isinstance(item, dict)]

    def find_orders_by_reference_for_sync(
        self,
        *,
        endpoint_cfg: dict[str, Any],
        reference_value: str,
        reference_filter_field: str = "",
        extra_filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_reference = str(reference_value).strip()
        if not normalized_reference:
            return []

        filter_values: dict[str, Any] = {}
        if isinstance(extra_filters, dict):
            filter_values.update(extra_filters)
        if reference_filter_field:
            filter_values[reference_filter_field] = normalized_reference

        rows = self._fetch_endpoint_rows(
            endpoint_cfg=endpoint_cfg,
            search_text=normalized_reference,
            filter_values=filter_values,
        )
        return [item for item in rows if isinstance(item, dict)]

    def create_order_for_sync(
        self,
        payload: dict[str, Any],
        endpoint_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = endpoint_cfg if isinstance(endpoint_cfg, dict) else {}
        method = str(cfg.get("method", "POST")).strip().upper() or "POST"
        path = str(cfg.get("path", "/api/v1/orders")).strip() or "/api/v1/orders"
        if not path.startswith("/"):
            path = "/" + path

        base_url = self._resolve_api_base_url(cfg)
        headers = self._build_api_headers(cfg)
        return self._request_json(
            method=method,
            url=f"{base_url}{path}",
            headers=headers,
            params=None,
            data=payload if isinstance(payload, dict) else {},
        )

    def update_order_status_for_sync(
        self,
        *,
        order_id: str,
        payload: dict[str, Any] | None = None,
        endpoint_cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = endpoint_cfg if isinstance(endpoint_cfg, dict) else {}
        method = str(cfg.get("method", "PUT")).strip().upper() or "PUT"
        path_template = str(cfg.get("path", "/api/v1/orders/update-status-order/{order_id}")).strip()
        if not path_template:
            path_template = "/api/v1/orders/update-status-order/{order_id}"
        if not path_template.startswith("/"):
            path_template = "/" + path_template
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise ValidationError("Thiếu order_id Thái Dương để cập nhật trạng thái.")
        try:
            path = path_template.format(order_id=normalized_order_id)
        except KeyError as exc:
            raise ValidationError(f"Cấu hình update trạng thái Thái Dương thiếu placeholder: {exc}") from exc

        base_url = self._resolve_api_base_url(cfg)
        request_payload = payload if isinstance(payload, dict) else {}
        use_session_login = bool(cfg.get("use_session_login", True))
        if use_session_login:
            session = self._build_authenticated_session(cfg)
            try:
                return self._request_json_with_session(
                    session=session,
                    method=method,
                    url=f"{base_url}{path}",
                    params=None,
                    data=request_payload,
                )
            finally:
                session.close()

        headers = self._build_api_headers(cfg)
        return self._request_json(
            method=method,
            url=f"{base_url}{path}",
            headers=headers,
            params=None,
            data=request_payload,
        )

    def ensure_api_token_fresh(self, *, force: bool = False) -> dict[str, Any]:
        self._hydrate_token_from_state()
        auto_auth_enabled = self._env_bool("THAI_DUONG_AUTO_AUTH_ENABLED", default=False)
        threshold_seconds = max(
            60,
            self._env_int("THAI_DUONG_AUTO_REFRESH_THRESHOLD_MINUTES", default=120) * 60,
        )
        min_retry_seconds = max(10, self._env_int("THAI_DUONG_AUTO_AUTH_MIN_RETRY_SECONDS", default=120))

        current_token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
        remaining_seconds = self._token_remaining_seconds(current_token)
        needs_renew = force or (not current_token) or (remaining_seconds <= threshold_seconds)

        report: dict[str, Any] = {
            "ok": True,
            "enabled": auto_auth_enabled,
            "rotated": False,
            "method": "",
            "remaining_seconds": remaining_seconds,
            "needs_renew": needs_renew,
        }
        if not needs_renew:
            report["reason"] = "token_con_han"
            return report
        if not auto_auth_enabled:
            report["ok"] = False
            report["reason"] = "auto_auth_tat"
            return report

        now_ts = time.time()
        if (now_ts - self._last_auto_auth_attempt_ts) < float(min_retry_seconds) and not force:
            report["ok"] = False
            report["reason"] = "dang_trong_khoang_retry"
            return report

        with self._auto_auth_lock:
            current_token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
            remaining_seconds = self._token_remaining_seconds(current_token)
            needs_renew = force or (not current_token) or (remaining_seconds <= threshold_seconds)
            if not needs_renew:
                report["remaining_seconds"] = remaining_seconds
                report["needs_renew"] = False
                report["reason"] = "token_da_duoc_lam_moi_tu_instance_khac"
                return report

            self._last_auto_auth_attempt_ts = time.time()

            refreshed = self._try_refresh_token(current_token=current_token)
            if refreshed:
                access_token, refresh_token = refreshed
                self._apply_tokens(access_token=access_token, refresh_token=refresh_token, source="refresh")
                report["rotated"] = True
                report["method"] = "refresh"
                report["remaining_seconds"] = self._token_remaining_seconds(access_token)
                return report

            login_tokens = self._try_login_token()
            if login_tokens:
                access_token, refresh_token = login_tokens
                self._apply_tokens(access_token=access_token, refresh_token=refresh_token, source="login")
                report["rotated"] = True
                report["method"] = "login"
                report["remaining_seconds"] = self._token_remaining_seconds(access_token)
                return report

            report["ok"] = False
            report["reason"] = "khong_lay_duoc_token_moi_tu_refresh_hoac_login"
            return report

    def _try_refresh_token(self, *, current_token: str) -> tuple[str, str] | None:
        path = str(os.getenv("THAI_DUONG_AUTH_REFRESH_PATH", "/api/v1/auth/refresh")).strip() or "/api/v1/auth/refresh"
        if not path.startswith("/"):
            path = "/" + path
        refresh_token = str(os.getenv("THAI_DUONG_AUTH_REFRESH_TOKEN", "")).strip()

        attempts: list[tuple[str, dict[str, Any], str]] = []
        if refresh_token:
            cookie_value = f"refreshToken={refresh_token}"
            attempts.append((refresh_token, {"refreshToken": refresh_token}, cookie_value))
            attempts.append((refresh_token, {}, cookie_value))
        if current_token:
            if refresh_token:
                attempts.append((current_token, {"refreshToken": refresh_token}, f"refreshToken={refresh_token}"))
            attempts.append((current_token, {}, ""))
        if refresh_token:
            attempts.append(("", {"refreshToken": refresh_token}, f"refreshToken={refresh_token}"))

        for bearer_token, body, cookie_header in attempts:
            payload = self._request_auth_json(
                path=path,
                body=body,
                bearer_token=bearer_token,
                cookie_header=cookie_header,
            )
            if not isinstance(payload, dict):
                continue
            access_token, new_refresh_token = self._extract_access_refresh_tokens(payload)
            if access_token:
                return access_token, (new_refresh_token or refresh_token)
        return None

    def _try_login_token(self) -> tuple[str, str] | None:
        path = str(os.getenv("THAI_DUONG_AUTH_LOGIN_PATH", "/api/v1/auth/login")).strip() or "/api/v1/auth/login"
        if not path.startswith("/"):
            path = "/" + path
        email = str(os.getenv("THAI_DUONG_AUTH_EMAIL", "")).strip()
        password = str(os.getenv("THAI_DUONG_AUTH_PASSWORD", "")).strip()
        if not email or not password:
            return None
        body: dict[str, Any] = {"email": email, "password": password}
        username = str(os.getenv("THAI_DUONG_AUTH_USERNAME", "")).strip()
        if username:
            body["userName"] = username
        payload = self._request_auth_json(path=path, body=body, bearer_token="", cookie_header="")
        if not isinstance(payload, dict):
            return None
        access_token, refresh_token = self._extract_access_refresh_tokens(payload)
        if not access_token:
            return None
        return access_token, refresh_token

    def _request_auth_json(
        self,
        *,
        path: str,
        body: dict[str, Any],
        bearer_token: str = "",
        cookie_header: str = "",
    ) -> dict[str, Any]:
        base_url = str(os.getenv("THAI_DUONG_API_BASE_URL", "")).strip().rstrip("/")
        if not base_url:
            return {}
        url = f"{base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = requests.post(url=url, headers=headers, json=body, timeout=20)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Thai Duong auto-auth call that bai (%s): %s", path, exc)
            return {}
        if response.status_code >= 400:
            self.logger.warning(
                "Thai Duong auto-auth tra loi %s (%s): %s",
                response.status_code,
                path,
                self._short_text(response.text, limit=200),
            )
            return {}
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Thai Duong auto-auth tra JSON khong hop le (%s): %s",
                path,
                self._short_text(response.text, limit=200),
            )
            return {}
        if not isinstance(payload, dict):
            return {}
        token_cookie = str(response.cookies.get("token", "")).strip()
        refresh_cookie = str(response.cookies.get("refreshToken", "")).strip()
        if token_cookie and not self._extract_first_string(payload, ("token", "access_token", "accessToken")):
            payload["token"] = token_cookie
        if refresh_cookie and not self._extract_first_string(payload, ("refreshToken", "refresh_token")):
            payload["refreshToken"] = refresh_cookie
        return payload

    def _extract_access_refresh_tokens(self, payload: dict[str, Any]) -> tuple[str, str]:
        access_paths = (
            "access_token",
            "accessToken",
            "token",
            "jwt",
            "data.access_token",
            "data.accessToken",
            "data.token",
            "data.jwt",
            "data.access.token",
            "data.session.access_token",
            "data.session.accessToken",
        )
        refresh_paths = (
            "refresh_token",
            "refreshToken",
            "data.refresh_token",
            "data.refreshToken",
            "data.session.refresh_token",
            "data.session.refreshToken",
        )
        access_token = self._extract_first_string(payload, access_paths)
        if not access_token:
            access_token = self._find_first_string_by_keys(
                payload,
                {"access_token", "accesstoken", "token", "jwt"},
            )
        refresh_token = self._extract_first_string(payload, refresh_paths)
        if not refresh_token:
            refresh_token = self._find_first_string_by_keys(payload, {"refresh_token", "refreshtoken"})
        return access_token, refresh_token

    @staticmethod
    def _extract_first_string(payload: dict[str, Any], paths: tuple[str, ...]) -> str:
        for path in paths:
            value = ThaiDuongCodClient._extract_value(payload, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _find_first_string_by_keys(value: Any, keys: set[str], depth: int = 0) -> str:
        if depth > 6:
            return ""
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).replace("-", "").replace("_", "").lower()
                if normalized in keys and isinstance(item, str) and item.strip():
                    return item.strip()
                found = ThaiDuongCodClient._find_first_string_by_keys(item, keys, depth + 1)
                if found:
                    return found
            return ""
        if isinstance(value, list):
            for item in value:
                found = ThaiDuongCodClient._find_first_string_by_keys(item, keys, depth + 1)
                if found:
                    return found
        return ""

    def _apply_tokens(self, *, access_token: str, refresh_token: str, source: str) -> None:
        normalized_access = str(access_token).strip()
        if not normalized_access:
            return
        os.environ["THAI_DUONG_API_TOKEN"] = normalized_access
        if refresh_token:
            os.environ["THAI_DUONG_AUTH_REFRESH_TOKEN"] = str(refresh_token).strip()
        state_payload = {
            "token": normalized_access,
            "refresh_token": str(refresh_token).strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
        path = self._resolve_auto_auth_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(path, state_payload)

    def _hydrate_token_from_state(self) -> None:
        path = self._resolve_auto_auth_state_path()
        if not path.exists():
            return
        payload = load_json(path)
        if not isinstance(payload, dict):
            return
        state_token = str(payload.get("token", "")).strip()
        state_refresh_token = str(payload.get("refresh_token", "")).strip()
        current_token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
        if (not current_token or self._token_remaining_seconds(current_token) <= 0) and state_token:
            os.environ["THAI_DUONG_API_TOKEN"] = state_token
        if state_refresh_token and not str(os.getenv("THAI_DUONG_AUTH_REFRESH_TOKEN", "")).strip():
            os.environ["THAI_DUONG_AUTH_REFRESH_TOKEN"] = state_refresh_token

    def _resolve_auto_auth_state_path(self) -> Path:
        raw = str(os.getenv("THAI_DUONG_AUTO_AUTH_STATE_PATH", "storage/thai_duong_auth/state.json")).strip()
        path = Path(raw) if raw else Path("storage/thai_duong_auth/state.json")
        if path.is_absolute():
            return path
        return self.settings.project_root / path

    @staticmethod
    def _token_remaining_seconds(token: str) -> int:
        payload = ThaiDuongCodClient._decode_jwt_payload(token)
        exp_ts = ThaiDuongCodClient._to_int(payload.get("exp"), fallback=0)
        if exp_ts <= 0:
            return 0
        now_ts = int(datetime.now(timezone.utc).timestamp())
        return exp_ts - now_ts

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        raw = str(os.getenv(key, "")).strip().lower()
        if not raw:
            return default
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
        return default

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        raw = str(os.getenv(key, "")).strip()
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def check_token_health(self) -> dict[str, Any]:
        token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
        now_ts = int(datetime.now(timezone.utc).timestamp())
        auto_auth_enabled = self._env_bool("THAI_DUONG_AUTO_AUTH_ENABLED", default=False)
        report: dict[str, Any] = {
            "ok": True,
            "configured": bool(token),
            "token_env": "THAI_DUONG_API_TOKEN",
            "auto_auth_enabled": auto_auth_enabled,
            "api_probe": {
                "ok": False,
                "skipped": True,
                "reason": "Chua thuc hien.",
            },
        }
        warnings: list[str] = []

        if auto_auth_enabled:
            auto_auth_report = self.ensure_api_token_fresh(force=False)
            report["auto_auth"] = auto_auth_report
            token = str(os.getenv("THAI_DUONG_API_TOKEN", "")).strip()
            report["configured"] = bool(token)

        if not token:
            report["ok"] = False
            report["error"] = "Chua cau hinh THAI_DUONG_API_TOKEN."
            return report

        payload = self._decode_jwt_payload(token)
        if isinstance(payload, dict):
            report["token_iat_ts"] = self._to_int(payload.get("iat"), fallback=0)
            report["token_exp_ts"] = self._to_int(payload.get("exp"), fallback=0)
            exp_ts = self._to_int(payload.get("exp"), fallback=0)
            if exp_ts > 0:
                report["token_exp_utc"] = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat()
                remaining_seconds = exp_ts - now_ts
                report["token_remaining_seconds"] = remaining_seconds
                if remaining_seconds <= 0:
                    report["ok"] = False
                    report["error"] = "Token Thai Duong da het han."
                elif remaining_seconds <= 6 * 3600:
                    warnings.append("Token Thai Duong sap het han trong vong 6 gio.")
            else:
                warnings.append("Khong doc duoc thoi gian het han (exp) tu token Thai Duong.")
        else:
            warnings.append("Token Thai Duong khong dung dinh dang JWT hoac payload khong hop le.")

        try:
            cfg = self._load_source_config()
            if self._is_api_enabled(cfg):
                today = datetime.now(timezone.utc).date()
                self._fetch_history_api(cfg, today, today)
                report["api_probe"] = {
                    "ok": True,
                    "skipped": False,
                    "endpoint": "history_endpoint",
                }
            else:
                report["api_probe"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "API mode dang tat trong reconcile_cod_source.json.",
                }
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            report["api_probe"] = {
                "ok": False,
                "skipped": False,
                "error": str(exc),
            }

        if warnings:
            report["warnings"] = warnings
        return report

    def _load_source_config(self) -> dict[str, Any]:
        path = self.settings.reconcile_cod_source_config_path
        if not path.exists():
            raise ValidationError(f"Khong tim thay config nguon doi soat COD: {path}")
        payload = load_json(path)
        if not isinstance(payload, dict):
            raise ValidationError("Config nguon doi soat COD khong hop le.")
        return payload

    @staticmethod
    def _is_api_enabled(cfg: dict[str, Any]) -> bool:
        api_cfg = cfg.get("api", {})
        return isinstance(api_cfg, dict) and bool(api_cfg.get("enabled", False))

    @staticmethod
    def _is_csv_enabled(cfg: dict[str, Any]) -> bool:
        csv_cfg = cfg.get("csv", {})
        return isinstance(csv_cfg, dict) and bool(csv_cfg.get("enabled", True))

    def _fetch_history_api(self, cfg: dict[str, Any], start_date: date, end_date: date) -> list[dict[str, Any]]:
        api_cfg = cfg.get("api", {})
        if not isinstance(api_cfg, dict):
            raise ValidationError("Config API doi soat COD khong hop le.")
        endpoint = api_cfg.get("history_endpoint", {})
        if not isinstance(endpoint, dict):
            raise ValidationError("Thieu api.history_endpoint trong config doi soat COD.")

        base_url = self._resolve_api_base_url(api_cfg)
        headers = self._build_api_headers(api_cfg)
        method = str(endpoint.get("method", "GET")).strip().upper()
        path = str(endpoint.get("path", "")).strip()
        if not path:
            raise ValidationError("api.history_endpoint.path dang trong.")

        date_format = str(endpoint.get("date_format", "%Y-%m-%d")).strip() or "%Y-%m-%d"
        start_param = str(endpoint.get("start_date_param", "start_date")).strip()
        end_param = str(endpoint.get("end_date_param", "end_date")).strip()
        page_param = str(endpoint.get("page_param", "page_number")).strip()
        page_size_param = str(endpoint.get("page_size_param", "page_size")).strip()
        page_size = self._to_int(endpoint.get("page_size"), fallback=200)
        result_path = str(endpoint.get("result_path", "data")).strip() or "data"
        total_pages_path = str(endpoint.get("total_pages_path", "total_pages")).strip() or "total_pages"
        has_next_page_path = str(endpoint.get("has_next_page_path", "")).strip()

        page = 1
        total_pages = 1
        rows: list[dict[str, Any]] = []
        while page <= max(total_pages, 1):
            params: dict[str, Any] = {
                start_param: start_date.strftime(date_format),
                end_param: end_date.strftime(date_format),
                page_param: page,
                page_size_param: page_size,
            }
            payload = self._request_json(
                method=method,
                url=f"{base_url}{path}",
                headers=headers,
                params=params,
                data=None,
            )
            page_rows = self._extract_list(payload, result_path)
            rows.extend(item for item in page_rows if isinstance(item, dict))
            if has_next_page_path:
                has_next = bool(self._extract_value(payload, has_next_page_path))
                if has_next and page_rows:
                    page += 1
                    total_pages = max(total_pages, page)
                    continue
                break
            total_pages = self._to_int(self._extract_value(payload, total_pages_path), fallback=page)
            if page >= total_pages or not page_rows:
                break
            page += 1

        return rows

    def _fetch_endpoint_rows(
        self,
        *,
        endpoint_cfg: dict[str, Any],
        search_text: str,
        filter_values: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(endpoint_cfg, dict):
            raise ValidationError("Config endpoint Thai Duong khong hop le.")

        base_url = self._resolve_api_base_url(endpoint_cfg)
        headers = self._build_api_headers(endpoint_cfg)
        method = str(endpoint_cfg.get("method", "POST")).strip().upper() or "POST"
        path = str(endpoint_cfg.get("path", "")).strip()
        if not path:
            raise ValidationError("Config endpoint Thai Duong thieu path.")
        if not path.startswith("/"):
            path = "/" + path

        request_mode = str(endpoint_cfg.get("request_mode", "json_body")).strip().lower() or "json_body"
        result_path = str(endpoint_cfg.get("result_path", "data.data")).strip() or "data.data"
        has_next_page_path = str(endpoint_cfg.get("has_next_page_path", "")).strip()
        total_pages_path = str(endpoint_cfg.get("total_pages_path", "data.total")).strip() or "data.total"

        page_field = str(endpoint_cfg.get("page_field", "page")).strip() or "page"
        page_size_field = str(endpoint_cfg.get("page_size_field", "limit")).strip() or "limit"
        page_param = str(endpoint_cfg.get("page_param", "page")).strip() or "page"
        page_size_param = str(endpoint_cfg.get("page_size_param", "limit")).strip() or "limit"
        page_size = self._to_int(endpoint_cfg.get("page_size"), fallback=200)

        search_field = str(endpoint_cfg.get("search_field", "searchText")).strip() or "searchText"
        filters_field = str(endpoint_cfg.get("filters_field", "filters")).strip() or "filters"
        body_template = endpoint_cfg.get("body_template", {})
        if not isinstance(body_template, dict):
            body_template = {}

        rows: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= max(1, total_pages):
            params: dict[str, Any] | None = None
            data: dict[str, Any] | None = None
            if request_mode == "query":
                params = {
                    page_param: page,
                    page_size_param: page_size,
                }
                if search_text and search_field:
                    params[search_field] = search_text
                if isinstance(filter_values, dict):
                    for key, value in filter_values.items():
                        key_name = str(key).strip()
                        if not key_name:
                            continue
                        params[key_name] = value
            else:
                data = copy.deepcopy(body_template)
                self._set_path(data, page_field, page)
                self._set_path(data, page_size_field, page_size)
                if search_text and search_field:
                    self._set_path(data, search_field, search_text)
                if isinstance(filter_values, dict):
                    for key, value in filter_values.items():
                        key_name = str(key).strip()
                        if not key_name:
                            continue
                        if "." in key_name or "[" in key_name:
                            self._set_path(data, key_name, value)
                        elif filters_field:
                            self._set_path(data, f"{filters_field}.{key_name}", value)
                        else:
                            self._set_path(data, key_name, value)

            payload = self._request_json(
                method=method,
                url=f"{base_url}{path}",
                headers=headers,
                params=params,
                data=data,
            )
            page_rows = self._extract_list(payload, result_path)
            rows.extend(item for item in page_rows if isinstance(item, dict))
            if has_next_page_path:
                has_next = bool(self._extract_value(payload, has_next_page_path))
                if has_next and page_rows:
                    page += 1
                    total_pages = max(total_pages, page)
                    continue
                break
            total_pages = self._to_int(self._extract_value(payload, total_pages_path), fallback=page)
            if page >= total_pages or not page_rows:
                break
            page += 1
        return rows

    def _fetch_detail_api(
        self,
        cfg: dict[str, Any],
        settlement_date: date,
        settlement: dict[str, Any],
    ) -> list[dict[str, Any]]:
        api_cfg = cfg.get("api", {})
        if not isinstance(api_cfg, dict):
            raise ValidationError("Config API doi soat COD khong hop le.")
        endpoint = api_cfg.get("detail_endpoint", {})
        if not isinstance(endpoint, dict):
            raise ValidationError("Thieu api.detail_endpoint trong config doi soat COD.")

        base_url = self._resolve_api_base_url(api_cfg)
        headers = self._build_api_headers(api_cfg)
        method = str(endpoint.get("method", "GET")).strip().upper()
        raw_path = str(endpoint.get("path", "")).strip()
        if not raw_path:
            raise ValidationError("api.detail_endpoint.path dang trong.")

        date_format = str(endpoint.get("date_format", "%Y-%m-%d")).strip() or "%Y-%m-%d"
        settlement_id_param = str(endpoint.get("settlement_id_param", "")).strip()
        settlement_date_param = str(endpoint.get("settlement_date_param", "settlement_date")).strip()
        page_param = str(endpoint.get("page_param", "page_number")).strip()
        page_size_param = str(endpoint.get("page_size_param", "page_size")).strip()
        page_size = self._to_int(endpoint.get("page_size"), fallback=200)
        result_path = str(endpoint.get("result_path", "data")).strip() or "data"
        total_pages_path = str(endpoint.get("total_pages_path", "total_pages")).strip() or "total_pages"
        has_next_page_path = str(endpoint.get("has_next_page_path", "")).strip()
        request_mode = str(endpoint.get("request_mode", "query")).strip().lower() or "query"
        body_template = endpoint.get("body_template", {})
        if not isinstance(body_template, dict):
            body_template = {}
        page_field = str(endpoint.get("page_field", "page")).strip() or "page"
        page_size_field = str(endpoint.get("page_size_field", "limit")).strip() or "limit"
        settlement_date_from_field = str(endpoint.get("settlement_date_from_field", "")).strip()
        settlement_date_to_field = str(endpoint.get("settlement_date_to_field", "")).strip()

        path_context = {
            "settlement_date": settlement_date.strftime(date_format),
            "settlement_id": str(settlement.get("settlement_id", settlement.get("id", ""))).strip(),
            "partner_code": str(settlement.get("partner_code", settlement.get("customer_code", ""))).strip(),
        }
        path = raw_path.format(**path_context)

        page = 1
        total_pages = 1
        rows: list[dict[str, Any]] = []
        while page <= max(total_pages, 1):
            params: dict[str, Any] | None = None
            data: dict[str, Any] | None = None
            if request_mode == "json_body":
                data = copy.deepcopy(body_template)
                self._set_path(data, page_field, page)
                self._set_path(data, page_size_field, page_size)
                if settlement_date_from_field:
                    self._set_path(data, settlement_date_from_field, settlement_date.strftime(date_format))
                elif settlement_date_param:
                    self._set_path(data, settlement_date_param, settlement_date.strftime(date_format))
                if settlement_date_to_field:
                    self._set_path(data, settlement_date_to_field, settlement_date.strftime(date_format))
                if settlement_id_param and path_context["settlement_id"]:
                    self._set_path(data, settlement_id_param, path_context["settlement_id"])
            else:
                params = {
                    settlement_date_param: settlement_date.strftime(date_format),
                    page_param: page,
                    page_size_param: page_size,
                }
                if settlement_id_param and path_context["settlement_id"]:
                    params[settlement_id_param] = path_context["settlement_id"]

            payload = self._request_json(
                method=method,
                url=f"{base_url}{path}",
                headers=headers,
                params=params,
                data=data,
            )
            page_rows = self._extract_list(payload, result_path)
            rows.extend(item for item in page_rows if isinstance(item, dict))
            if has_next_page_path:
                has_next = bool(self._extract_value(payload, has_next_page_path))
                if has_next and page_rows:
                    page += 1
                    total_pages = max(total_pages, page)
                    continue
                break
            total_pages = self._to_int(self._extract_value(payload, total_pages_path), fallback=page)
            if page >= total_pages or not page_rows:
                break
            page += 1

        return rows

    def _fetch_history_csv(self, cfg: dict[str, Any], start_date: date, end_date: date) -> list[dict[str, Any]]:
        csv_cfg = cfg.get("csv", {})
        if not isinstance(csv_cfg, dict):
            raise ValidationError("Config CSV doi soat COD khong hop le.")
        pattern = str(csv_cfg.get("history_glob", "storage/reconcile_cod/imports/history/*.csv")).strip()
        encoding = str(csv_cfg.get("history_encoding", "utf-8-sig")).strip() or "utf-8-sig"
        date_field = str(csv_cfg.get("history_date_field", "Ngày trả tiền COD")).strip() or "Ngày trả tiền COD"
        rows = self._read_csv_rows(pattern, encoding)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            value = str(row.get(date_field, "")).strip()
            row_date = self._parse_date(value)
            if row_date and start_date <= row_date <= end_date:
                filtered.append(row)
        return filtered

    def _fetch_detail_csv(self, cfg: dict[str, Any], settlement_date: date) -> list[dict[str, Any]]:
        csv_cfg = cfg.get("csv", {})
        if not isinstance(csv_cfg, dict):
            raise ValidationError("Config CSV doi soat COD khong hop le.")
        pattern = str(csv_cfg.get("detail_glob", "storage/reconcile_cod/imports/detail/*.csv")).strip()
        encoding = str(csv_cfg.get("detail_encoding", "utf-8-sig")).strip() or "utf-8-sig"
        date_field = str(csv_cfg.get("detail_date_field", "Ngày đối soát")).strip() or "Ngày đối soát"
        rows = self._read_csv_rows(pattern, encoding)
        filtered: list[dict[str, Any]] = []
        for row in rows:
            value = str(row.get(date_field, "")).strip()
            row_date = self._parse_date(value)
            if row_date == settlement_date:
                filtered.append(row)
        return filtered

    def _read_csv_rows(self, pattern: str, encoding: str) -> list[dict[str, Any]]:
        path_pattern = self._resolve_pattern_path(pattern)
        matches = sorted(
            (Path(item) for item in glob(path_pattern)),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            raise ValidationError(f"Khong tim thay file CSV doi soat theo pattern: {pattern}")
        target = matches[0]
        rows: list[dict[str, Any]] = []
        with target.open("r", encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                rows.append({str(k).strip(): v for k, v in row.items()})
        if not rows:
            raise ValidationError(f"File CSV doi soat khong co du lieu: {target}")
        return rows

    def _resolve_pattern_path(self, pattern: str) -> str:
        raw = pattern.replace("\\", "/").strip()
        if not raw:
            return "storage/reconcile_cod/imports/detail/*.csv"
        absolute_candidate = Path(raw)
        if absolute_candidate.is_absolute():
            return raw
        project_rel = self.settings.project_root.joinpath(raw).as_posix()
        return project_rel

    def _resolve_api_base_url(self, api_cfg: dict[str, Any]) -> str:
        env_key = str(api_cfg.get("base_url_env", "THAI_DUONG_API_BASE_URL")).strip()
        value = ""
        if env_key:
            value = str(os.getenv(env_key, "")).strip()
        if not value:
            value = str(api_cfg.get("base_url", "")).strip()
        if not value:
            raise ValidationError("Chua co base URL API Thai Duong.")
        return value.rstrip("/")

    def _build_api_headers(self, api_cfg: dict[str, Any]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        env_key = str(api_cfg.get("token_env", "THAI_DUONG_API_TOKEN")).strip()
        if env_key.upper() == "THAI_DUONG_API_TOKEN":
            try:
                self.ensure_api_token_fresh(force=False)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Thai Duong auto-auth gap loi: %s", exc)
        token = ""
        if env_key:
            token = str(os.getenv(env_key, "")).strip()
        if not token:
            token = str(api_cfg.get("token", "")).strip()
        if token:
            header_name = str(api_cfg.get("token_header", "Authorization")).strip() or "Authorization"
            prefix = str(api_cfg.get("token_prefix", "Bearer "))
            headers[header_name] = f"{prefix}{token}" if prefix else token
        return headers

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=data if data else None,
            timeout=30,
        )
        if response.status_code >= 400:
            raise ValidationError(
                f"API Thai Duong loi ({response.status_code}): {self._short_text(response.text)}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"API Thai Duong tra JSON khong hop le: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise ValidationError("API Thai Duong tra du lieu khong hop le.")
        return payload

    def _request_json_with_session(
        self,
        *,
        session: requests.Session,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = session.request(
            method=method,
            url=url,
            params=params,
            json=data if data else None,
            timeout=30,
        )
        if response.status_code >= 400:
            raise ValidationError(
                f"API Thai Duong loi ({response.status_code}): {self._short_text(response.text)}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"API Thai Duong tra JSON khong hop le: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise ValidationError("API Thai Duong tra du lieu khong hop le.")
        return payload

    def _build_authenticated_session(self, api_cfg: dict[str, Any]) -> requests.Session:
        base_url = self._resolve_api_base_url(api_cfg)
        login_path = str(
            api_cfg.get("login_path", os.getenv("THAI_DUONG_AUTH_LOGIN_PATH", "/api/v1/auth/login"))
        ).strip() or "/api/v1/auth/login"
        if not login_path.startswith("/"):
            login_path = "/" + login_path
        email = str(os.getenv("THAI_DUONG_AUTH_EMAIL", "")).strip()
        password = str(os.getenv("THAI_DUONG_AUTH_PASSWORD", "")).strip()
        if not email or not password:
            raise ValidationError(
                "Thiếu THAI_DUONG_AUTH_EMAIL/THAI_DUONG_AUTH_PASSWORD để đăng nhập session cập nhật trạng thái."
            )
        body: dict[str, Any] = {"email": email, "password": password}
        username = str(os.getenv("THAI_DUONG_AUTH_USERNAME", "")).strip()
        if username:
            body["userName"] = username
        session = requests.Session()
        response = session.post(
            url=f"{base_url}{login_path}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=20,
        )
        if response.status_code >= 400:
            session.close()
            raise ValidationError(
                f"Thai Duong login session loi ({response.status_code}): {self._short_text(response.text)}"
            )
        if not session.cookies.get("token"):
            session.close()
            raise ValidationError("Thai Duong login session không nhận được cookie token.")
        return session

    @staticmethod
    def _short_text(value: str, limit: int = 400) -> str:
        normalized = " ".join(str(value).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict[str, Any]:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        payload_part = parts[1]
        padding = "=" * (-len(payload_part) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload_part + padding)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    @staticmethod
    def _extract_value(payload: dict[str, Any], path: str) -> Any:
        current: Any = payload
        for token in str(path).split("."):
            token = token.strip()
            if not token:
                continue
            if not isinstance(current, dict):
                return None
            current = current.get(token)
        return current

    def _extract_list(self, payload: dict[str, Any], path: str) -> list[Any]:
        value = self._extract_value(payload, path)
        if isinstance(value, list):
            return value
        return []

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _parse_date(raw: str) -> date | None:
        value = str(raw).strip()
        if not value:
            return None
        if "T" in value and len(value) >= 10:
            value = value[:10]
        patterns = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y")
        from datetime import datetime

        for pattern in patterns:
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        key = str(path or "").strip()
        if not key:
            return
        if "[" in key and "]" in key and "." not in key:
            # Support legacy query-style keys mapped into JSON body.
            # Example: filters[paymentCodDateFrom] -> {"filters": {"paymentCodDateFrom": value}}
            head = key.split("[", 1)[0]
            tail = key[len(head) :].strip()
            segments = [head] if head else []
            cursor = ""
            for ch in tail:
                if ch in "[]":
                    if cursor:
                        segments.append(cursor)
                        cursor = ""
                    continue
                cursor += ch
            if cursor:
                segments.append(cursor)
        else:
            segments = [item.strip() for item in key.split(".") if item.strip()]
        if not segments:
            return
        current: dict[str, Any] = payload
        for segment in segments[:-1]:
            child = current.get(segment)
            if not isinstance(child, dict):
                child = {}
                current[segment] = child
            current = child
        current[segments[-1]] = value
