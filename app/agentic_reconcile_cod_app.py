from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import html
import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from app.reconcile_cod_service import ReconcileCodService
from app.reconcile_cod_sheet_service import ReconcileCodSheetService
from app.settings import Settings
from app.utils import now_utc_iso


_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass(frozen=True)
class AgenticReconcileCodRequest:
    settlement_date: date | None = None
    trigger_label: str = "Agentic Reconcile COD"
    apply_updates_policy: str = "auto"  # auto|always|never
    sync_sheet_policy: str = "auto"  # auto|always|never
    llm_judge_mode: str = "auto"  # auto|force|off
    llm_model: str = "gpt-4.1-mini"
    include_records_limit: int = 30
    html_report_path: Path | None = None


class AgenticReconcileCodApp:
    def __init__(
        self,
        *,
        settings: Settings,
        logger,
        reconcile_service: ReconcileCodService,
        reconcile_sheet_service: ReconcileCodSheetService | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.reconcile = reconcile_service
        self.reconcile_sheet = reconcile_sheet_service

    def run(self, request: AgenticReconcileCodRequest) -> dict[str, Any]:
        report = self.reconcile.generate_report(request.settlement_date)
        apply_summary: dict[str, Any] | None = None
        sheet_sync: dict[str, Any] | None = None

        run_id = str(report.get("run_id", "")).strip()
        if self._should_apply_updates(request=request, report=report):
            if run_id:
                apply_summary = self.reconcile.apply_updates(run_id)
            else:
                apply_summary = {
                    "ok": False,
                    "updated": 0,
                    "failed": 0,
                    "skipped": 0,
                    "transitioned": 0,
                    "errors": ["Thiếu run_id nên không thể apply cập nhật."],
                    "failed_orders": [],
                    "applied_at": now_utc_iso(),
                }

        if self._should_sync_sheet(request=request, report=report):
            if self.reconcile_sheet:
                sheet_sync = self.reconcile_sheet.sync_report(report)
            else:
                sheet_sync = {
                    "enabled": bool(self.settings.reconcile_cod_sheet_enabled),
                    "ok": False,
                    "attempted": 0,
                    "inserted": 0,
                    "skipped_existing": 0,
                    "errors": ["Luồng sheet chưa được khởi tạo."],
                }

        judge_payload = self._build_judge_payload(
            report=report,
            apply_summary=apply_summary,
            sheet_sync=sheet_sync,
            include_records_limit=max(1, int(request.include_records_limit)),
        )
        judgment = self._judge_outcome(
            mode=str(request.llm_judge_mode).strip().lower() or "auto",
            model=str(request.llm_model).strip() or "gpt-4.1-mini",
            payload=judge_payload,
        )
        failed_order_codes = self._extract_failed_order_codes(apply_summary)

        result: dict[str, Any] = {
            "ok": bool(report.get("ok", False)),
            "generated_at": now_utc_iso(),
            "trigger_label": request.trigger_label,
            "report": report,
            "apply_summary": apply_summary,
            "sheet_sync": sheet_sync,
            "judgment": judgment,
            "failed_order_codes": failed_order_codes,
            "next_actions": judgment.get("actions", []),
        }

        html_path = request.html_report_path
        if html_path:
            html_path = Path(html_path)
            self._write_html_report(
                path=html_path,
                result=result,
                include_records_limit=max(1, int(request.include_records_limit)),
            )
            result["html_report_path"] = str(html_path)

        if judgment.get("verdict") == "blocked":
            result["ok"] = False
        elif self._to_int((apply_summary or {}).get("failed")) > 0:
            result["ok"] = False
        elif sheet_sync and sheet_sync.get("ok") is False:
            result["ok"] = False
        return result

    def _should_apply_updates(self, *, request: AgenticReconcileCodRequest, report: dict[str, Any]) -> bool:
        policy = str(request.apply_updates_policy).strip().lower()
        if policy == "never":
            return False
        if policy == "always":
            return True
        if not self.settings.reconcile_cod_update_enabled:
            return False
        summary = report.get("summary", {})
        if not isinstance(summary, dict):
            return False
        return self._to_int(summary.get("update_candidates")) > 0

    def _should_sync_sheet(self, *, request: AgenticReconcileCodRequest, report: dict[str, Any]) -> bool:
        policy = str(request.sync_sheet_policy).strip().lower()
        if policy == "never":
            return False
        if policy == "always":
            return True
        if not self.settings.reconcile_cod_sheet_enabled:
            return False
        if not bool(report.get("ok", False)):
            return False
        records = report.get("records", [])
        return isinstance(records, list) and len(records) > 0

    def _build_judge_payload(
        self,
        *,
        report: dict[str, Any],
        apply_summary: dict[str, Any] | None,
        sheet_sync: dict[str, Any] | None,
        include_records_limit: int,
    ) -> dict[str, Any]:
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        errors = report.get("errors", {}) if isinstance(report.get("errors"), dict) else {}
        warnings = report.get("warnings", []) if isinstance(report.get("warnings"), list) else []
        records = report.get("records", []) if isinstance(report.get("records"), list) else []

        sampled_records: list[dict[str, Any]] = []
        for item in records[:include_records_limit]:
            if not isinstance(item, dict):
                continue
            sampled_records.append(
                {
                    "td_awb": str(item.get("td_awb", "")).strip(),
                    "td_status": str(item.get("td_status", "")).strip(),
                    "match_result": str(item.get("match_result", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                    "pancake_display_id": str(item.get("pancake_display_id", "")).strip(),
                    "pancake_order_id": str(item.get("pancake_order_id", "")).strip(),
                    "target_status": item.get("target_status"),
                }
            )

        return {
            "settlement_date": str(report.get("settlement_date", "")).strip(),
            "source_mode": str(report.get("source_mode", "")).strip(),
            "detail_count": self._to_int(report.get("detail_count")),
            "summary": summary,
            "errors": errors,
            "warnings": warnings[:20],
            "apply_summary": apply_summary or {},
            "sheet_sync": sheet_sync or {},
            "sampled_records": sampled_records,
            "failed_order_codes": self._extract_failed_order_codes(apply_summary),
        }

    def _judge_outcome(self, *, mode: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        mode = mode if mode in {"auto", "force", "off"} else "auto"
        if mode == "off":
            return self._heuristic_judgment(payload)

        api_key = str(
            os.getenv("AGENTIC_JUDGE_OPENAI_API_KEY")
            or os.getenv("BOT3_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        ).strip()
        if not api_key:
            if mode == "force":
                raise RuntimeError("Thiếu API key cho LLM judge (AGENTIC_JUDGE_OPENAI_API_KEY/BOT3_OPENAI_API_KEY).")
            judged = self._heuristic_judgment(payload)
            judged["engine"] = "heuristic_fallback"
            judged["flags"] = list(dict.fromkeys([*judged.get("flags", []), "llm_unavailable"]))
            return judged

        try:
            llm_result = self._judge_by_openai(model=model, api_key=api_key, payload=payload)
            if isinstance(llm_result, dict):
                return llm_result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("LLM judge that bai, fallback heuristic: %s", exc)
            if mode == "force":
                raise

        judged = self._heuristic_judgment(payload)
        judged["engine"] = "heuristic_fallback"
        judged["flags"] = list(dict.fromkeys([*judged.get("flags", []), "llm_parse_fallback"]))
        return judged

    def _judge_by_openai(self, *, model: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        system_prompt = (
            "Bạn là bộ máy phán đoán vận hành đối soát COD. "
            "Trả về DUY NHẤT JSON object theo schema: "
            "{\"engine\":string,\"verdict\":\"ok|warning|blocked\",\"confidence\":number,"
            "\"ops_summary\":string,\"actions\":string[],\"flags\":string[]}. "
            "Không thêm markdown, không thêm text ngoài JSON."
        )
        user_prompt = (
            "Đây là payload đối soát. Hãy phán đoán rủi ro vận hành và đề xuất hành động ngắn gọn tiếng Việt.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        body = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "max_output_tokens": 600,
        }
        response = requests.request(
            method="POST",
            url=_OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI Responses lỗi ({response.status_code}): {self._short_text(response.text)}")
        payload_resp = response.json()
        if not isinstance(payload_resp, dict):
            raise RuntimeError("OpenAI Responses trả dữ liệu không hợp lệ.")
        text = self._extract_output_text(payload_resp)
        if not text:
            raise RuntimeError("LLM judge không trả nội dung.")
        parsed = self._parse_json_object(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("LLM judge trả JSON không hợp lệ.")

        verdict = str(parsed.get("verdict", "")).strip().lower()
        if verdict not in {"ok", "warning", "blocked"}:
            verdict = "warning"
        actions = parsed.get("actions", [])
        flags = parsed.get("flags", [])
        if not isinstance(actions, list):
            actions = []
        if not isinstance(flags, list):
            flags = []
        return {
            "engine": str(parsed.get("engine", "llm")).strip() or "llm",
            "verdict": verdict,
            "confidence": self._to_float(parsed.get("confidence"), fallback=0.7),
            "ops_summary": str(parsed.get("ops_summary", "")).strip(),
            "actions": [str(item).strip() for item in actions if str(item).strip()],
            "flags": [str(item).strip() for item in flags if str(item).strip()],
        }

    def _heuristic_judgment(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
        errors = payload.get("errors", {}) if isinstance(payload.get("errors"), dict) else {}
        apply_summary = payload.get("apply_summary", {}) if isinstance(payload.get("apply_summary"), dict) else {}
        sheet_sync = payload.get("sheet_sync", {}) if isinstance(payload.get("sheet_sync"), dict) else {}
        failed_order_codes = payload.get("failed_order_codes", [])
        if not isinstance(failed_order_codes, list):
            failed_order_codes = []

        flags: list[str] = []
        actions: list[str] = []
        verdict = "ok"
        confidence = 0.93

        if errors:
            verdict = "blocked"
            confidence = 0.98
            flags.append("source_error")
            actions.append("Kiểm tra token/API Thái Dương hoặc file CSV fallback trước khi chạy lại.")

        if self._to_int(summary.get("not_found")) > 0:
            if verdict == "ok":
                verdict = "warning"
                confidence = 0.85
            flags.append("not_found")
            actions.append("Rà soát key match (AWB/phone+tên+tiền) cho nhóm không tìm thấy.")

        if self._to_int(summary.get("unmapped_status")) > 0:
            if verdict == "ok":
                verdict = "warning"
                confidence = 0.82
            flags.append("unmapped_status")
            actions.append("Cập nhật mapping trạng thái trong reconcile_cod_status_map.json.")

        failed_apply = self._to_int(apply_summary.get("failed"))
        if failed_apply > 0:
            if verdict != "blocked":
                verdict = "warning"
                confidence = min(confidence, 0.8)
            flags.append("update_failed")
            actions.append("Xử lý các mã đơn lỗi trên Pancake rồi chạy lại apply theo run_id.")

        if isinstance(sheet_sync, dict) and sheet_sync and sheet_sync.get("ok") is False:
            if verdict != "blocked":
                verdict = "warning"
                confidence = min(confidence, 0.8)
            flags.append("sheet_sync_failed")
            actions.append("Kiểm tra cấu hình Google Sheet/OAuth/Webhook rồi sync lại.")

        if failed_order_codes:
            flags.append("failed_order_codes")
            actions.append(f"Ưu tiên xử lý {len(failed_order_codes)} mã đơn lỗi đã được liệt kê.")

        if not actions:
            actions.append("Không có blocker, có thể tiếp tục vận hành theo lịch bình thường.")

        ops_summary = (
            f"Kỳ {payload.get('settlement_date', '')}: "
            f"matched={self._to_int(summary.get('matched_unique'))}, "
            f"already_correct={self._to_int(summary.get('already_correct'))}, "
            f"failed_update={failed_apply}."
        )
        return {
            "engine": "heuristic",
            "verdict": verdict,
            "confidence": round(confidence, 2),
            "ops_summary": ops_summary,
            "actions": actions,
            "flags": list(dict.fromkeys(flags)),
        }

    def _extract_failed_order_codes(self, apply_summary: dict[str, Any] | None) -> list[str]:
        if not isinstance(apply_summary, dict):
            return []
        values: list[str] = []
        failed_orders = apply_summary.get("failed_orders", [])
        if isinstance(failed_orders, list):
            for item in failed_orders:
                if not isinstance(item, dict):
                    continue
                display_id = str(item.get("display_id", "")).strip()
                order_id = str(item.get("order_id", "")).strip()
                awb = str(item.get("awb", "")).strip()
                if display_id and order_id:
                    label = f"{display_id} ({order_id})"
                else:
                    label = display_id or order_id
                if awb:
                    label = f"{label} | AWB:{awb}" if label else f"AWB:{awb}"
                if label:
                    values.append(label)
        if values:
            return list(dict.fromkeys(values))

        errors = apply_summary.get("errors", [])
        if isinstance(errors, list):
            for item in errors:
                text = str(item).strip()
                if not text:
                    continue
                display_match = re.search(r"\(([A-Z]{2,}\d+)\)", text)
                if display_match:
                    values.append(display_match.group(1))
                    continue
                order_match = re.search(r"^(\d{6,})", text)
                if order_match:
                    values.append(order_match.group(1))
        return list(dict.fromkeys(values))

    def _write_html_report(self, *, path: Path, result: dict[str, Any], include_records_limit: int) -> None:
        report = result.get("report", {}) if isinstance(result.get("report"), dict) else {}
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        apply_summary = result.get("apply_summary", {}) if isinstance(result.get("apply_summary"), dict) else {}
        sheet_sync = result.get("sheet_sync", {}) if isinstance(result.get("sheet_sync"), dict) else {}
        judgment = result.get("judgment", {}) if isinstance(result.get("judgment"), dict) else {}
        records = report.get("records", []) if isinstance(report.get("records"), list) else []
        failed_codes = result.get("failed_order_codes", [])
        if not isinstance(failed_codes, list):
            failed_codes = []

        rows_html = []
        for item in records[:include_records_limit]:
            if not isinstance(item, dict):
                continue
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(str(item.get('td_awb', '')).strip())}</td>"
                f"<td>{html.escape(str(item.get('td_status', '')).strip())}</td>"
                f"<td>{html.escape(str(item.get('match_result', '')).strip())}</td>"
                f"<td>{html.escape(str(item.get('pancake_display_id', '')).strip())}</td>"
                f"<td>{html.escape(str(item.get('reason', '')).strip())}</td>"
                "</tr>"
            )
        rows_content = "\n".join(rows_html) if rows_html else "<tr><td colspan='5'>Không có dữ liệu mẫu</td></tr>"
        failed_codes_html = "".join(
            f"<li>{html.escape(str(item))}</li>" for item in failed_codes if str(item).strip()
        )
        actions = judgment.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        actions_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in actions if str(item).strip())
        if not actions_html:
            actions_html = "<li>Không có hành động đề xuất.</li>"

        html_content = f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Agentic COD Reconcile Report</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card: #ffffff;
      --ink: #14213d;
      --muted: #56637a;
      --ok: #2a9d8f;
      --warn: #f4a261;
      --bad: #e76f51;
      --line: #dce3ef;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top right, #e7eef9 0%, var(--bg) 45%);
      color: var(--ink);
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    .header {{ margin-bottom: 18px; }}
    .muted {{ color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      box-shadow: 0 2px 10px rgba(20, 33, 61, 0.05);
    }}
    .chip {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
    }}
    .ok {{ background: #def7f0; color: var(--ok); }}
    .warning {{ background: #fff1e3; color: #d97706; }}
    .blocked {{ background: #ffe5df; color: var(--bad); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
    }}
    th, td {{
      font-size: 13px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      vertical-align: top;
    }}
    th {{ background: #f1f5fb; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h2>Agentic COD Reconcile Report</h2>
      <div class="muted">Run ID: {html.escape(str(report.get("run_id", "")).strip())} | Kỳ: {html.escape(str(report.get("settlement_date", "")).strip())}</div>
      <div class="muted">Generated at: {html.escape(str(result.get("generated_at", "")).strip())}</div>
    </div>
    <div class="grid">
      <div class="card"><b>Tổng bản ghi</b><div>{self._to_int(summary.get("total")):,}</div></div>
      <div class="card"><b>Khớp duy nhất</b><div>{self._to_int(summary.get("matched_unique")):,}</div></div>
      <div class="card"><b>Đã đúng trạng thái</b><div>{self._to_int(summary.get("already_correct")):,}</div></div>
      <div class="card"><b>Không tìm thấy</b><div>{self._to_int(summary.get("not_found")):,}</div></div>
      <div class="card"><b>Chưa map trạng thái</b><div>{self._to_int(summary.get("unmapped_status")):,}</div></div>
      <div class="card"><b>Lỗi cập nhật</b><div>{self._to_int(apply_summary.get("failed")):,}</div></div>
      <div class="card"><b>Sheet inserted</b><div>{self._to_int(sheet_sync.get("inserted")):,}</div></div>
      <div class="card"><b>Verdict</b><div><span class="chip {html.escape(str(judgment.get("verdict", "warning")))}">{html.escape(str(judgment.get("verdict", "")).upper())}</span></div></div>
    </div>
    <div class="card" style="margin-bottom: 14px;">
      <b>Tóm tắt phán đoán</b>
      <div>{html.escape(str(judgment.get("ops_summary", "")).strip())}</div>
      <b>Hành động gợi ý</b>
      <ul>{actions_html}</ul>
    </div>
    <div class="card" style="margin-bottom: 14px;">
      <b>Mã đơn lỗi</b>
      <ul>{failed_codes_html or "<li>Không có mã lỗi.</li>"}</ul>
    </div>
    <h3>Mẫu bản ghi đối soát</h3>
    <table>
      <thead>
        <tr>
          <th>AWB</th>
          <th>Trạng thái TD</th>
          <th>Kết quả match</th>
          <th>Mã đơn Pancake</th>
          <th>Lý do</th>
        </tr>
      </thead>
      <tbody>
        {rows_content}
      </tbody>
    </table>
  </div>
</body>
</html>
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content, encoding="utf-8")

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        direct_text = str(payload.get("output_text", "")).strip()
        if direct_text:
            return direct_text
        output = payload.get("output", [])
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text_value = str(block.get("text", "")).strip()
                if text_value:
                    parts.append(text_value)
        return "\n".join(parts).strip()

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            candidate = text[first : last + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return None
        return None

    @staticmethod
    def _to_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _to_float(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _short_text(raw: str, limit: int = 320) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
