"""Sync the Ruza product-code snapshot from Google Sheets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import quote

import requests

from app.ads_sku_catalog import build_snapshot, write_snapshot
from app.settings import load_settings


SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


def _oauth_value(settings, field: str, *env_names: str) -> str:
    value = str(getattr(settings, field, "") or "").strip()
    if value:
        return value
    for name in env_names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _refresh_access_token(settings) -> str:
    token_uri = _oauth_value(
        settings,
        "media_analytics_sheet_oauth_token_uri",
        "ADS2_SKU_CATALOG_OAUTH_TOKEN_URI",
        "MEDIA_ANALYTICS_SHEET_OAUTH_TOKEN_URI",
    ) or "https://oauth2.googleapis.com/token"
    client_id = _oauth_value(
        settings,
        "media_analytics_sheet_oauth_client_id",
        "ADS2_SKU_CATALOG_OAUTH_CLIENT_ID",
        "MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_ID",
    )
    client_secret = _oauth_value(
        settings,
        "media_analytics_sheet_oauth_client_secret",
        "ADS2_SKU_CATALOG_OAUTH_CLIENT_SECRET",
        "MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_SECRET",
    )
    refresh_token = _oauth_value(
        settings,
        "media_analytics_sheet_oauth_refresh_token",
        "ADS2_SKU_CATALOG_OAUTH_REFRESH_TOKEN",
        "MEDIA_ANALYTICS_SHEET_OAUTH_REFRESH_TOKEN",
    )
    missing = [
        label
        for label, value in (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("refresh_token", refresh_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Thiếu Google OAuth cho sync catalog: " + ", ".join(missing))
    response = requests.post(
        token_uri,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google OAuth không trả access_token.")
    return token


def sync_catalog(
    *,
    profile: str = "ads2",
    output: str | Path | None = None,
    spreadsheet_id: str = "",
    sheet_name: str = "",
    source_range: str = "",
) -> Path:
    settings = load_settings(profile=profile)
    resolved_spreadsheet_id = str(spreadsheet_id or settings.sku_catalog_spreadsheet_id).strip()
    resolved_sheet_name = str(sheet_name or settings.sku_catalog_sheet_name).strip()
    resolved_range = str(source_range or settings.sku_catalog_range).strip()
    if not resolved_spreadsheet_id or not resolved_sheet_name or not resolved_range:
        raise RuntimeError("Thiếu cấu hình Google Sheet catalog SKU.")

    token = _refresh_access_token(settings)
    a1_range = f"'{resolved_sheet_name.replace(chr(39), chr(39) * 2)}'!{resolved_range}"
    url = f"{SHEETS_API_BASE}/{resolved_spreadsheet_id}/values/{quote(a1_range, safe='')}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"majorDimension": "COLUMNS"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    columns = payload.get("values", []) if isinstance(payload, dict) else []
    values = columns[0] if columns and isinstance(columns[0], list) else []
    snapshot = build_snapshot(
        values,
        spreadsheet_id=resolved_spreadsheet_id,
        sheet_name=resolved_sheet_name,
        column="J",
        source_range=resolved_range,
    )
    destination = Path(output or settings.sku_catalog_path)
    return write_snapshot(destination, snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Ruza SKU catalog from Google Sheets.")
    parser.add_argument("--profile", default="ads2")
    parser.add_argument("--output", default="")
    parser.add_argument("--spreadsheet-id", default="")
    parser.add_argument("--sheet-name", default="")
    parser.add_argument("--range", dest="source_range", default="")
    args = parser.parse_args()
    destination = sync_catalog(
        profile=args.profile,
        output=args.output or None,
        spreadsheet_id=args.spreadsheet_id,
        sheet_name=args.sheet_name,
        source_range=args.source_range,
    )
    print(f"Đã đồng bộ catalog SKU: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
