from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class _CodeHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    auth_error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        code = query.get("code", [""])[0].strip()
        error = query.get("error", [""])[0].strip()
        if code:
            _CodeHandler.auth_code = code
            self._send_ok("Đã lấy mã thành công. Anh có thể đóng tab này.")
            return
        if error:
            _CodeHandler.auth_error = error
            self._send_ok(f"OAuth trả lỗi: {error}. Anh có thể đóng tab này.")
            return
        self._send_ok("Không thấy mã OAuth trong URL. Anh đóng tab và chạy lại script.")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        del format, args

    def _send_ok(self, message: str) -> None:
        payload = f"<html><body><h3>{message}</h3></body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _build_auth_url(client_id: str, redirect_uri: str, scope: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _exchange_code_for_tokens(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Token endpoint lỗi ({response.status_code}): {response.text}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Token endpoint trả dữ liệu không hợp lệ.")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Lay refresh token Google OAuth cho Sheets.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    redirect_uri = f"http://{args.host}:{args.port}/callback"
    auth_url = _build_auth_url(args.client_id, redirect_uri, args.scope)

    server = HTTPServer((args.host, args.port), _CodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("1) Mở URL sau để cấp quyền Google Sheets:")
    print(auth_url)
    if not args.no_open:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    print("")
    print(f"2) Chờ callback tại {redirect_uri} ...")
    while _CodeHandler.auth_code is None and _CodeHandler.auth_error is None:
        thread.join(timeout=0.2)

    server.shutdown()
    thread.join(timeout=1)

    if _CodeHandler.auth_error:
        print(f"Lỗi OAuth: {_CodeHandler.auth_error}")
        return 1
    code = _CodeHandler.auth_code or ""
    if not code:
        print("Không nhận được mã OAuth.")
        return 1

    payload = _exchange_code_for_tokens(
        client_id=args.client_id,
        client_secret=args.client_secret,
        redirect_uri=redirect_uri,
        code=code,
    )
    refresh_token = str(payload.get("refresh_token", "")).strip()
    access_token = str(payload.get("access_token", "")).strip()
    print("")
    print("3) Kết quả token:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("")
    if refresh_token:
        print("=> Điền vào .env:")
        print(f"RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN={refresh_token}")
    else:
        print("Không thấy refresh_token. Anh vào Google Account > Security > Third-party access để revoke app rồi chạy lại.")
    if access_token:
        print("Access token đã có (ngắn hạn), bot sẽ tự refresh bằng refresh_token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
