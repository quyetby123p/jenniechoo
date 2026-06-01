from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import logging
from pathlib import Path
import sys
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from app.agentic_reconcile_cod_app import AgenticReconcileCodApp, AgenticReconcileCodRequest
from app.command_parser import try_parse_reconcile_cod_command
from app.logger import configure_logger
from app.pancake_pos_client import PancakePosClient
from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.settings import load_settings
from app.thai_duong_cod_client import ThaiDuongCodClient
from app.utils import now_utc_iso


def _parse_date_text(raw: str) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    patterns = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y")
    for pattern in patterns:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _resolve_settlement_date(*, request_text: str, settlement_date_text: str, timezone_name: str) -> date | None:
    parsed_direct = _parse_date_text(settlement_date_text)
    if parsed_direct:
        return parsed_direct

    request_text = str(request_text or "").strip()
    if request_text:
        try:
            ok, parsed = try_parse_reconcile_cod_command(request_text, timezone_name)
        except Exception:
            ok, parsed = False, None
        if ok:
            return parsed

    return None


def _build_default_html_path(run_id: str) -> Path:
    stamp = now_utc_iso().replace(":", "").replace("-", "").replace("T", "_").replace("Z", "")
    safe_run_id = run_id or "no_run_id"
    return PROJECT_ROOT / "storage" / "reconcile_cod" / "reports" / f"agentic_{safe_run_id}_{stamp}.html"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agentic Reconcile COD Skill App (script-guided workflow for other agents)."
    )
    parser.add_argument("--input-json", default="", help="Đường dẫn file JSON input profile.")
    parser.add_argument("--request-text", default="", help='Input tự nhiên, ví dụ: "đối soát cod hôm qua".')
    parser.add_argument("--settlement-date", default="", help="Ngày đối soát: YYYY-MM-DD hoặc DD/MM/YYYY.")
    parser.add_argument("--trigger-label", default="Agentic Reconcile COD")
    parser.add_argument("--apply-updates", default="auto", choices=["auto", "always", "never"])
    parser.add_argument("--sync-sheet", default="auto", choices=["auto", "always", "never"])
    parser.add_argument("--llm-judge", default="auto", choices=["auto", "force", "off"])
    parser.add_argument("--llm-model", default="gpt-4.1-mini")
    parser.add_argument("--include-records-limit", type=int, default=30)
    parser.add_argument("--output-json", default="", help="Đường dẫn ghi output JSON.")
    parser.add_argument("--output-html", default="", help="Đường dẫn ghi report HTML.")
    parser.add_argument("--html-report", action="store_true", help="Bật xuất HTML report.")
    parser.add_argument("--open-html", action="store_true", help="Mở HTML report sau khi tạo.")
    parser.add_argument("--interactive", action="store_true", help="Bật prompt tương tác nếu thiếu input.")
    return parser.parse_args()


def _load_input_profile(path_text: str) -> dict[str, str]:
    path = Path(str(path_text or "").strip())
    if not path_text:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy input profile: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Input profile phải là JSON object.")
    normalized: dict[str, str] = {}
    for key, value in payload.items():
        normalized[str(key)] = "" if value is None else str(value)
    return normalized


def _interactive_fill(values: dict[str, str]) -> dict[str, str]:
    updated = dict(values)
    if not updated.get("request_text") and not updated.get("settlement_date"):
        user_text = input("Nhập lệnh tự nhiên (ví dụ: đối soát cod hôm qua, để trống nếu nhập ngày): ").strip()
        updated["request_text"] = user_text
    if not updated.get("settlement_date") and not updated.get("request_text"):
        user_date = input("Nhập ngày đối soát (YYYY-MM-DD hoặc DD/MM/YYYY): ").strip()
        updated["settlement_date"] = user_date
    return updated


def _coalesce(primary: str, secondary: str, fallback: str = "") -> str:
    return primary if str(primary).strip() else (secondary if str(secondary).strip() else fallback)


def main() -> int:
    args = _parse_args()

    profile = _load_input_profile(args.input_json)
    merged = {
        "request_text": _coalesce(args.request_text, profile.get("request_text", "")),
        "settlement_date": _coalesce(args.settlement_date, profile.get("settlement_date", "")),
        "trigger_label": _coalesce(args.trigger_label, profile.get("trigger_label", "Agentic Reconcile COD")),
        "apply_updates": _coalesce(args.apply_updates, profile.get("apply_updates", "auto"), "auto"),
        "sync_sheet": _coalesce(args.sync_sheet, profile.get("sync_sheet", "auto"), "auto"),
        "llm_judge": _coalesce(args.llm_judge, profile.get("llm_judge", "auto"), "auto"),
        "llm_model": _coalesce(args.llm_model, profile.get("llm_model", "gpt-4.1-mini"), "gpt-4.1-mini"),
        "include_records_limit": _coalesce(str(args.include_records_limit), profile.get("include_records_limit", "30"), "30"),
        "output_json": _coalesce(args.output_json, profile.get("output_json", "")),
        "output_html": _coalesce(args.output_html, profile.get("output_html", "")),
    }
    if args.interactive:
        merged = _interactive_fill(merged)

    settings = load_settings(PROJECT_ROOT)
    logger = configure_logger(log_dir=settings.logs_root / "runs" / "agentic_cod_skill")

    settlement_date = _resolve_settlement_date(
        request_text=merged.get("request_text", ""),
        settlement_date_text=merged.get("settlement_date", ""),
        timezone_name=settings.app_timezone,
    )

    html_path: Path | None = None
    if args.html_report or bool(merged.get("output_html", "").strip()):
        if str(merged.get("output_html", "")).strip():
            html_path = Path(str(merged.get("output_html", "")).strip())
        else:
            html_path = _build_default_html_path(str(settlement_date or "latest"))

    request = AgenticReconcileCodRequest(
        settlement_date=settlement_date,
        trigger_label=str(merged.get("trigger_label", "Agentic Reconcile COD")).strip() or "Agentic Reconcile COD",
        apply_updates_policy=str(merged.get("apply_updates", "auto")).strip().lower(),
        sync_sheet_policy=str(merged.get("sync_sheet", "auto")).strip().lower(),
        llm_judge_mode=str(merged.get("llm_judge", "auto")).strip().lower(),
        llm_model=str(merged.get("llm_model", "gpt-4.1-mini")).strip() or "gpt-4.1-mini",
        include_records_limit=max(1, int(str(merged.get("include_records_limit", "30")).strip() or "30")),
        html_report_path=html_path,
    )

    reconcile_service = ReconcileCodService(
        settings=settings,
        logger=logger,
        pancake_client=PancakePosClient(settings=settings, logger=logger),
        thai_duong_client=ThaiDuongCodClient(settings=settings, logger=logger),
    )
    sheet_service = ReconcileCodSheetService(settings=settings, logger=logger)
    app = AgenticReconcileCodApp(
        settings=settings,
        logger=logger,
        reconcile_service=reconcile_service,
        reconcile_sheet_service=sheet_service,
    )
    result = app.run(request)

    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    print(output_text)

    output_json = str(merged.get("output_json", "")).strip()
    if output_json:
        output_path = Path(output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")

    html_report_path = str(result.get("html_report_path", "")).strip()
    if args.open_html and html_report_path:
        try:
            webbrowser.open(Path(html_report_path).resolve().as_uri())
        except Exception:
            pass

    if result.get("ok"):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
