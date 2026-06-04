from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.approval_service import ApprovalService
from app.settings import Settings
from app.telegram_bot import TelegramAdsBot
from app.utils import dump_json


def _dummy_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = Settings(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        logs_root=tmp_path / "logs",
        state_root=tmp_path / "state",
        config_root=tmp_path / "config",
        telegram_bot_token="dummy",
        telegram_allowed_user_id=1,
        meta_access_token="dummy",
        meta_page_access_token="page_dummy",
        meta_ad_account_id="act_1",
        meta_page_id="61581440236157",
        meta_api_version="v21.0",
        app_timezone="Asia/Ho_Chi_Minh",
        app_currency="VND",
        retry_max=3,
        retry_backoff_seconds=[1, 2, 3],
        token_healthcheck_enabled=False,
        token_healthcheck_hour=9,
        token_healthcheck_minute=0,
        token_healthcheck_startup_alert_only_on_failure=True,
        daily_report_enabled=False,
        daily_report_hour=8,
        daily_report_minute=0,
        daily_report_history_days=90,
        daily_report_startup_alert_only_on_failure=True,
        pancake_api_base_url="https://pos.pancake.vn/api/v1",
        pancake_api_key="",
        pancake_access_token="token_dummy",
        pancake_shop_id=123,
        pancake_page_size=200,
        report_thb_to_vnd_rate=815.0,
        report_thb_minor_unit_factor=100,
        reconcile_cod_enabled=True,
        reconcile_cod_auto_enabled=False,
        reconcile_cod_hour=9,
        reconcile_cod_minute=30,
        reconcile_cod_batch_limit=100,
        reconcile_cod_update_enabled=False,
        reconcile_cod_status_map_path="config/reconcile_cod_status_map.json",
        reconcile_cod_pancake_lookback_days=3650,
        reconcile_cod_sheet_enabled=True,
        reconcile_cod_sheet_spreadsheet_id="sheet_123",
        reconcile_cod_sheet_gid=1034910254,
        reconcile_cod_sheet_credentials_path="config/sa.json",
    )
    payload = {**base.__dict__, **overrides}
    return Settings(**payload)


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def send_document(self, chat_id: int, document, caption: str = "") -> None:  # noqa: ANN001
        self.documents.append({"chat_id": chat_id, "caption": caption})


class _FakeStorage:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}
        self.seq = 0

    def create_pending_request(self, payload: dict[str, Any], request_type: str = "duplicate_confirm") -> str:
        self.seq += 1
        req_id = f"req_{self.seq}"
        record = dict(payload)
        record["request_id"] = req_id
        record["request_type"] = request_type
        self.pending[req_id] = record
        return req_id

    def get_pending_request(self, request_id: str) -> dict[str, Any] | None:
        return self.pending.get(request_id)

    def delete_pending_request(self, request_id: str) -> None:
        self.pending.pop(request_id, None)


class _FakeReconcileService:
    def __init__(self, report: dict[str, Any], apply_summary: dict[str, Any] | None = None) -> None:
        self.report = report
        self.apply_summary = apply_summary or {
            "updated": 0,
            "failed": 0,
            "skipped": 0,
            "transitioned": 0,
            "errors": [],
            "failed_orders": [],
        }

    def generate_report(self, settlement_date):  # noqa: ANN001
        del settlement_date
        return dict(self.report)

    def default_settlement_date(self):  # noqa: ANN001
        from datetime import date

        return date(2026, 5, 9)

    def build_message(self, report: dict[str, Any], trigger_label: str = "") -> str:
        del report
        return f"{trigger_label}\nOK"

    def apply_updates(self, run_id: str) -> dict[str, Any]:
        del run_id
        return dict(self.apply_summary)


class _FakeReconcileSheetService:
    def __init__(self) -> None:
        self.calls = 0

    def sync_report(self, report: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            "enabled": True,
            "ok": True,
            "attempted": len(report.get("records", [])),
            "inserted": 1,
            "skipped_existing": 0,
            "errors": [],
        }


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=1)
        self.reply_markup = "x"
        self.answers: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        del reply_markup
        self.answers.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:  # noqa: ANN001
        self.reply_markup = reply_markup


class _FakeQuery:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=1)
        self.message = _FakeMessage()
        self.answer_calls: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:  # noqa: ARG002
        self.answer_calls.append(text)


def _build_bot(
    tmp_path: Path,
    report: dict[str, Any],
    sheet: _FakeReconcileSheetService,
    **settings_overrides: Any,
) -> tuple[TelegramAdsBot, _FakeStorage]:
    settings = _dummy_settings(tmp_path, **settings_overrides)
    logger = logging.getLogger("telegram_reconcile_sheet_test")
    storage = _FakeStorage()
    dedup = SimpleNamespace(inspect=lambda *_args, **_kwargs: {"is_duplicate": False, "next_version": 1, "active_jobs": []})
    meta = SimpleNamespace()
    reports = SimpleNamespace(
        generate_report=lambda *_a, **_k: {},
        default_report_date=lambda: None,
        build_message=lambda *_a, **_k: "",
    )
    bot = TelegramAdsBot(
        settings=settings,
        logger=logger,
        storage=storage,
        dedup=dedup,
        meta_client=meta,
        daily_report_service=reports,
        approval_service=ApprovalService(),
        rollback_service=SimpleNamespace(),
        reconcile_cod_service=_FakeReconcileService(report),
        reconcile_cod_sheet_service=sheet,
    )
    bot._bot = _FakeBot()
    settings.reconcile_cod_reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = settings.reconcile_cod_reports_dir / "x.csv"
    csv_path.write_text("a,b\n", encoding="utf-8")
    return bot, storage


def test_reconcile_report_requires_approval_before_sheet_sync(tmp_path: Path) -> None:
    report = {
        "ok": True,
        "partial": False,
        "settlement_date": "2026-05-09",
        "summary": {
            "matched_unique": 0,
            "already_correct": 1,
            "ambiguous": 0,
            "not_found": 0,
            "unmapped_status": 0,
            "update_candidates": 0,
            "total": 1,
        },
        "csv_path": str((tmp_path / "storage" / "reconcile_cod" / "reports" / "x.csv")),
        "source_mode": "api",
        "detail_count": 1,
        "records": [{"td_awb": "TH1"}],
        "run_id": "run_abc",
    }
    sheet = _FakeReconcileSheetService()
    bot, storage = _build_bot(tmp_path, report, sheet)

    asyncio.run(
        bot._send_reconcile_cod_report(
            chat_id=1,
            trigger_label="Đối soát COD thủ công",
            settlement_date=None,
            notify_success=True,
            allow_update_prompt=False,
            allow_sheet_sync=True,
        )
    )

    assert sheet.calls == 0
    assert len(storage.pending) == 1
    pending = next(iter(storage.pending.values()))
    assert pending["request_type"] == "reconcile_cod_sheet_sync"


def test_reconcile_sheet_apply_callback_runs_sync(tmp_path: Path) -> None:
    report = {
        "settlement_date": "2026-05-09",
        "records": [{"td_awb": "TH1"}],
    }
    sheet = _FakeReconcileSheetService()
    bot, storage = _build_bot(tmp_path, report, sheet)

    run_id = "run_abc"
    run_path = bot.settings.reconcile_cod_runs_dir / f"{run_id}.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(run_path, report)
    req_id = storage.create_pending_request({"run_id": run_id}, "reconcile_cod_sheet_sync")
    query = _FakeQuery()

    asyncio.run(bot._on_reconcile_sheet_apply(query, req_id))

    assert sheet.calls == 1
    assert storage.get_pending_request(req_id) is None


def test_reconcile_auto_apply_reports_failed_order_codes(tmp_path: Path) -> None:
    report = {
        "ok": True,
        "partial": False,
        "settlement_date": "2026-05-09",
        "summary": {
            "matched_unique": 1,
            "already_correct": 0,
            "ambiguous": 0,
            "not_found": 0,
            "unmapped_status": 0,
            "update_candidates": 1,
            "total": 1,
        },
        "csv_path": str((tmp_path / "storage" / "reconcile_cod" / "reports" / "x.csv")),
        "source_mode": "api",
        "detail_count": 1,
        "records": [{"td_awb": "TH35028N4TCP6B"}],
        "run_id": "run_abc",
    }
    apply_summary = {
        "updated": 0,
        "failed": 1,
        "skipped": 0,
        "transitioned": 0,
        "errors": ['360300986571957: Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm"}'],
        "failed_orders": [
            {
                "order_id": "360300986571957",
                "display_id": "JCT315",
                "awb": "TH35028N4TCP6B",
                "error": 'Pancake API lỗi (422): {"message":"[status]: Chưa có thông tin sản phẩm"}',
            }
        ],
    }
    sheet = _FakeReconcileSheetService()
    bot, _storage = _build_bot(tmp_path, report, sheet, reconcile_cod_update_enabled=True)
    bot.reconcile = _FakeReconcileService(report, apply_summary=apply_summary)  # type: ignore[assignment]

    asyncio.run(
        bot._send_reconcile_cod_report(
            chat_id=1,
            trigger_label="Đối soát COD thủ công",
            settlement_date=None,
            notify_success=True,
            allow_update_prompt=True,
            allow_sheet_sync=False,
        )
    )

    texts = [str(item.get("text", "")) for item in bot._bot.messages]  # type: ignore[union-attr]
    assert any("Mã đơn lỗi cần xử lý:" in text for text in texts)
    assert any("JCT315" in text for text in texts)


def test_reconcile_cash_in_auto_skips_when_no_settlement_today(tmp_path: Path) -> None:
    report = {
        "settlement_date": "2026-06-01",
        "records": [],
    }
    sheet = _FakeReconcileSheetService()
    bot, _storage = _build_bot(tmp_path, report, sheet)
    bot.reconcile = SimpleNamespace(
        generate_report_if_settlement_exists=lambda _date: None,
        summarize_cash_in_from_report=lambda _report: {},
    )

    ok = asyncio.run(
        bot._send_reconcile_cod_cash_in_report(
            chat_id=1,
            trigger_label="Báo cáo tiền về tự động Thái Dương",
        )
    )

    assert ok is True
    assert bot._bot is not None
    assert bot._bot.messages == []
