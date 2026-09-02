"""Chạy cầu nối Dropo -> Pancake.

Jennie Choo (mặc định AN TOÀN, chỉ in payload, không tạo đơn):
    .venv\\Scripts\\python.exe scripts\\run_dropo_pancake_bridge.py --profile main

Tạo đơn thật:
    .venv\\Scripts\\python.exe scripts\\run_dropo_pancake_bridge.py --profile main --live

Chạy vòng lặp (nếu muốn giữ máy chạy nền thay vì dùng GitHub Actions):
    .venv\\Scripts\\python.exe scripts\\run_dropo_pancake_bridge.py --profile main --live --loop

VAYXA vẫn dùng profile ADS2:
    .venv\\Scripts\\python.exe scripts\\run_dropo_pancake_bridge.py --profile ADS2 --live

Thoát với mã 0 nếu không có đơn nào lỗi, mã 1 nếu có — để GitHub Actions
báo đỏ khi cần người nhìn vào.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Console Windows mặc định là cp1252. Log của cầu nối có tiếng Việt có dấu,
# gặp cp1252 là logging ném UnicodeEncodeError và NUỐT MẤT thông báo lỗi thật
# — đúng lúc cần đọc lỗi nhất thì không còn gì để đọc.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

try:  # nạp .env khi chạy local; trên CI thì biến đã có sẵn trong môi trường
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _apply_config_defaults(profile: str) -> list[str]:
    """Đổ cấu hình không bí mật từ config/dropo_pancake_bridge.json vào môi trường.

    Id Sheet, id kho, đường dẫn bảng SKU đều không phải bí mật nên để trong
    repo, khỏi phụ thuộc vào việc .env có đúng dòng nào hay không. Chỉ đặt khi
    biến CHƯA có giá trị, nên .env vẫn được quyền ghi đè.

    Riêng token Pancake và OAuth Google thì KHÔNG bao giờ đọc từ đây — chúng
    chỉ được phép nằm trong .env hoặc GitHub Secrets.
    """
    # Jennie dùng shop main và tab riêng; VAYXA vẫn giữ config cũ.
    if not profile:
        candidates = [REPO_ROOT / "config" / "dropo_pancake_bridge_jennie.json"]
    else:
        candidates = [REPO_ROOT / "config" / f"dropo_pancake_bridge_{profile.lower()}.json"]
    candidates.append(REPO_ROOT / "config" / "dropo_pancake_bridge.json")
    path = next((candidate for candidate in candidates if candidate.exists()), candidates[-1])
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    prefix = (profile or "").strip().upper()
    applied: list[str] = []
    for key, value in data.items():
        name = str(key).strip().upper()
        if not name or "TOKEN" in name or "SECRET" in name or "API_KEY" in name:
            continue
        scoped = f"{prefix}_{name}" if prefix else name
        if not (os.getenv(scoped) or "").strip():
            os.environ[scoped] = str(value)
            applied.append(scoped)
    return applied

from app.dropo_pancake_bridge import BridgeConfig, DropoPancakeBridge  # noqa: E402
from app.exceptions import ValidationError  # noqa: E402
from app.instance_lock import single_instance_lock  # noqa: E402
from app.settings import load_settings  # noqa: E402


def build_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("dropo_pancake_bridge")
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def _run_bridge_loop(bridge: DropoPancakeBridge, config: BridgeConfig, args, logger: logging.Logger) -> int:
    """Chạy bridge trong phiên duy nhất đã được khóa ở cấp process."""

    exit_code = 0
    while True:
        try:
            report = bridge.run_once()
        except ValidationError as exc:
            logger.error("Cấu hình sai: %s", exc)
            return 2
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chạy thất bại: %s", exc)
            exit_code = 1
            if not args.loop:
                return exit_code
            time.sleep(config.poll_seconds)
            continue

        logger.info(
            "scanned=%s pending=%s created=%s dry_run=%s skipped=%s failed=%s",
            report.scanned,
            report.pending,
            report.created,
            report.dry_run,
            report.skipped,
            report.failed,
        )
        for row in report.rows:
            if row.status == "dry_run":
                logger.info("[DRY] dòng %s -> %s", row.row_index, row.message)
            elif row.status == "created":
                logger.info("dòng %s -> đơn Pancake %s", row.row_index, row.order_id)
            else:
                logger.warning("dòng %s -> %s: %s", row.row_index, row.status, row.message)

        fail_on_row_errors = os.getenv("DROPO_PANCAKE_BRIDGE_FAIL_ON_ROW_ERRORS", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        if report.failed and fail_on_row_errors:
            exit_code = 1

        if os.getenv("GITHUB_STEP_SUMMARY"):
            _write_actions_summary(report.as_dict(), dry_run=config.dry_run)

        if not args.loop:
            return exit_code
        time.sleep(config.poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Đồng bộ lead Dropo sang đơn Pancake POS.")
    parser.add_argument("--profile", default="ADS2", help="Tiền tố biến môi trường, vd ADS2.")
    parser.add_argument("--live", action="store_true", help="Tạo đơn THẬT (mặc định là dry-run).")
    parser.add_argument("--loop", action="store_true", help="Chạy lặp theo poll_seconds.")
    parser.add_argument("--once", action="store_true", help="Chạy đúng 1 lần rồi thoát (mặc định).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logger = build_logger(args.verbose)
    profile = args.profile.strip()
    # settings dùng tên profile viết thường ("ads2"); BridgeConfig dùng tiền tố
    # viết hoa không kèm gạch dưới ("ADS2") — cùng một profile, hai cách gọi.
    prefix = "" if profile.lower() in {"", "main", "default"} else profile.upper().replace("-", "_")
    os.environ.setdefault("ADS_PROFILE_PREFIX", prefix)

    applied = _apply_config_defaults(prefix)
    if applied:
        logger.info("Lay cau hinh bridge tu file config theo profile: %s", ", ".join(applied))

    # Jennie dùng bộ biến không tiền tố và config mặc định; "main" chỉ là
    # tên dễ hiểu ở CLI, không phải một profile cấu hình riêng.  Bridge
    # không dùng Telegram/Meta nên không được buộc CI phải khai báo credential
    # của các dịch vụ đó.
    settings_profile = None if profile.lower() in {"", "main", "default"} else profile
    settings = load_settings(
        profile=settings_profile,
        require_app_credentials=False,
    )
    config = BridgeConfig.from_env(prefix)
    if args.live:
        config.dry_run = False

    bridge = DropoPancakeBridge(settings, logger, config=config)
    ok, reason = bridge.is_configured()
    if not ok:
        logger.error("Chưa chạy được: %s", reason)
        return 2

    logger.info(
        "Profile=%s shop=%s dry_run=%s sheet=%s tab=%s",
        args.profile,
        settings.pancake_shop_id,
        config.dry_run,
        config.spreadsheet_id[:12] + "...",
        config.sheet_tab,
    )

    lock_file = Path(
        os.getenv("DROPO_PANCAKE_BRIDGE_LOCK_FILE", "").strip()
        or (REPO_ROOT / "storage" / "dropo_pancake_bridge.lock")
    )
    try:
        with single_instance_lock(lock_file):
            logger.info("Đã khóa phiên bridge: %s", lock_file)
            return _run_bridge_loop(bridge, config, args, logger)
    except RuntimeError as exc:
        logger.error("Không chạy phiên mới: bridge đang có phiên khác giữ khóa (%s).", exc)
        return 3


def _write_actions_summary(report: dict, *, dry_run: bool) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Dropo → Pancake",
        "",
        f"- Chế độ: `{'DRY RUN' if dry_run else 'LIVE'}`",
        f"- Quét: `{report['scanned']}` dòng · chờ xử lý `{report['pending']}`",
        f"- Tạo đơn: `{report['created']}` · bỏ qua `{report['skipped']}` · lỗi `{report['failed']}`",
    ]
    if report["rows"]:
        lines += ["", "| Dòng | Trạng thái | Order ID | Ghi chú |", "| --- | --- | --- | --- |"]
        for row in report["rows"][:40]:
            note = str(row["message"]).replace("|", "/")[:160]
            lines.append(f"| {row['row']} | {row['status']} | {row['order_id']} | {note} |")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
