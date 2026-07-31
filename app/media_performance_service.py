from __future__ import annotations

from datetime import date, datetime
import json
import logging
import math
import re
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from app.exceptions import MetaApiError
from app.meta_ads_client import MetaAdsClient
from app.models import MediaPerformanceCommand
from app.pancake_pos_client import PancakePosClient
from app.settings import Settings
from app.utils import dump_json, load_json, now_utc_iso


_VXV_CODE_PATTERN = re.compile(r"(?<![0-9A-Z])VXV[0-9A-Z]+(?![0-9A-Z])", re.IGNORECASE)
_MESSAGE_ACTION_MARKERS = (
    "messaging_conversation",
    "total_messaging",
    "messaging_connection",
    "messaging_first_reply",
    "leadgen.other",
)
_MESSAGE_ACTION_TYPES = (
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
    "onsite_conversion.messaging_first_reply",
    "onsite_conversion.messaging_conversation_replied_7d",
)
_ORDER_ACTION_TYPES = (
    "omni_purchase",
    "onsite_conversion.purchase",
    "onsite_web_purchase",
    "onsite_app_purchase",
    "onsite_web_app_purchase",
    "onsite_conversion.messaging_order_created_v2",
)
_REVENUE_ACTION_TYPES = (
    "omni_purchase",
    "onsite_conversion.purchase",
    "onsite_web_purchase",
    "onsite_app_purchase",
    "onsite_web_app_purchase",
)
_VIEW_ACTION_MARKERS = (
    "video_view",
    "thruplay",
)
_REACTION_ACTION_MARKERS = (
    "post_reaction",
)
_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
MEDIA_PERFORMANCE_SHEET_KEYS = [
    "start_date",
    "end_date",
    "code",
    "level",
    "media_key",
    "story_id",
    "permalink_url",
    "label",
    "score",
    "spend_vnd",
    "messages",
    "cost_per_message_vnd",
    "order_count",
    "cost_per_order_vnd",
    "revenue_vnd",
    "roas",
    "reactions",
    "reach",
    "views",
    "clicks",
    "cpc_vnd",
    "ctr",
    "cpm_vnd",
]
MEDIA_PERFORMANCE_SHEET_HEADERS = [
    "START_DATE / TỪ NGÀY",
    "END_DATE / ĐẾN NGÀY",
    "CODE / MÃ VXV",
    "LEVEL / CẤP",
    "MEDIA_KEY / MÃ MEDIA",
    "STORY_ID / ID BÀI",
    "PERMALINK_URL / LINK BÀI",
    "LABEL / NHÃN",
    "SCORE / ĐIỂM",
    "SPEND / CHI PHÍ ADS",
    "MESS / TIN NHẮN",
    "AVG_MESS_COST / GIÁ MESS TB",
    "ORDER_COUNT / SỐ LƯỢNG ĐƠN",
    "COST_PER_ORDER / CHI PHÍ TB/ĐƠN",
    "REVENUE / DOANH THU",
    "ROAS / TỶ SUẤT DOANH THU",
    "REACTIONS / THẢ CẢM XÚC",
    "REACH / TIẾP CẬN",
    "VIEWS / LƯỢT XEM",
    "CLICKS / LƯỢT CLICK",
    "CPC / CHI PHÍ CLICK",
    "CTR / TỶ LỆ CLICK",
    "CPM / CHI PHÍ 1.000 HIỂN THỊ",
]
_REACTIONS_COLUMN_INDEX = MEDIA_PERFORMANCE_SHEET_KEYS.index("reactions")
_LEGACY_MEDIA_PERFORMANCE_SHEET_KEYS_WITHOUT_REACTIONS = [
    key for key in MEDIA_PERFORMANCE_SHEET_KEYS if key != "reactions"
]
_LEGACY_MEDIA_PERFORMANCE_SHEET_HEADERS_WITHOUT_REACTIONS = [
    header for header in MEDIA_PERFORMANCE_SHEET_HEADERS if header != "REACTIONS / THẢ CẢM XÚC"
]


class MediaPerformanceService:
    def __init__(
        self,
        *,
        settings: Settings,
        logger: logging.Logger,
        meta_client: MetaAdsClient,
        pancake_client: PancakePosClient,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.meta = meta_client
        self.pancake = pancake_client
        self.output_dir = self.settings.storage_root / "media_analytics"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(self, command: MediaPerformanceCommand | None = None) -> dict[str, Any]:
        command = command or self._default_command()
        start_date = command.start_date or self._default_command().start_date
        end_date = command.end_date or self._default_command().end_date
        if start_date is None or end_date is None:
            raise ValueError("Media performance command thiếu khoảng ngày.")
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        report: dict[str, Any] = {
            "ok": True,
            "profile": self._settings_profile_name(),
            "account_id": self.meta.ad_account_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": self.settings.app_timezone,
            "generated_at": now_utc_iso(),
            "requested_codes": list(command.codes),
            "campaign_query": command.campaign_query,
            "summary": {
                "code_count": 0,
                "media_count": 0,
                "source_ad_count": 0,
                "unmatched_source_ad_count": 0,
            },
            "codes": [],
            "warnings": [],
            "errors": {},
        }

        try:
            insight_rows = self.meta.get_ad_insights_for_range(start_date, end_date, self.settings.app_timezone)
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            report["errors"]["meta"] = str(exc)
            self.logger.exception("Lay Meta ad insights that bai cho media performance")
            self._save_report(report)
            return report

        ad_ids = sorted({str(row.get("ad_id", "")).strip() for row in insight_rows if str(row.get("ad_id", "")).strip()})
        metadata = self.meta.get_ads_metadata(ad_ids) if ad_ids else {}
        story_ids = self._extract_story_ids(metadata)
        post_metadata = self.meta.get_post_metadata_for_story_ids(sorted(story_ids)) if story_ids else {}
        job_index = self._load_job_index()
        pancake_revenue_by_code: dict[str, dict[str, Any]] = {}
        pancake_warnings: list[str] = []

        grouped: dict[str, dict[str, Any]] = {}
        requested_codes = {code.upper() for code in command.codes}
        unmatched_ad_count = 0
        included_ad_count = 0

        for row in insight_rows:
            if not isinstance(row, dict):
                continue
            ad_id = str(row.get("ad_id", "")).strip()
            if not ad_id:
                continue
            ad_meta = metadata.get(ad_id, {})
            if not self._matches_campaign_filter(row=row, ad_meta=ad_meta, campaign_query=command.campaign_query):
                continue

            metrics = self._metrics_from_insight(row)
            story_id = self._story_id_from_ad_meta(ad_meta)
            post_meta = post_metadata.get(story_id, {}) if story_id else {}
            job_payloads = self._jobs_for_ad(ad_id=ad_id, story_id=story_id, job_index=job_index)
            codes = self._normalize_code_group(
                self._extract_codes_for_ad(row=row, ad_meta=ad_meta, post_meta=post_meta, jobs=job_payloads)
            )
            if requested_codes and not (set(codes) & requested_codes):
                unmatched_ad_count += 1
                continue
            if not codes:
                unmatched_ad_count += 1
                continue

            included_ad_count += 1
            code = self._code_group_label(codes)
            code_bucket = grouped.setdefault(code, self._empty_code_bucket(code, codes=codes))
            self._add_metrics(code_bucket["totals"], metrics)
            code_bucket["ad_count"] += 1
            if len(codes) > 1:
                code_bucket["multi_code_ad_count"] += 1

            media_key = story_id or str(ad_meta.get("creative_id", "")).strip() or ad_id
            media_bucket = code_bucket["media"].setdefault(
                media_key,
                self._empty_media_bucket(media_key=media_key, story_id=story_id, post_meta=post_meta),
            )
            self._add_metrics(media_bucket["totals"], metrics)
            if len(codes) > 1:
                media_bucket["multi_code"] = True
            media_bucket["ads"].append(
                {
                    "ad_id": ad_id,
                    "ad_name": self._first_text(row.get("ad_name"), ad_meta.get("ad_name")),
                    "adset_id": self._first_text(row.get("adset_id"), ad_meta.get("adset_id")),
                    "adset_name": self._first_text(row.get("adset_name"), ad_meta.get("adset_name")),
                    "campaign_id": self._first_text(row.get("campaign_id"), ad_meta.get("campaign_id")),
                    "campaign_name": self._first_text(row.get("campaign_name"), ad_meta.get("campaign_name")),
                    "creative_id": str(ad_meta.get("creative_id", "")).strip(),
                    "codes": codes,
                    "metrics": metrics,
                }
            )

        code_rows: list[dict[str, Any]] = []
        meta_revenue_available = any(
            self._to_int(bucket.get("totals", {}).get("order_count"))
            or self._to_int(bucket.get("totals", {}).get("revenue_vnd"))
            for bucket in grouped.values()
        )
        if not meta_revenue_available:
            pancake_revenue_by_code, pancake_warnings = self._build_revenue_by_code(
                start_date=start_date,
                end_date=end_date,
            )
            report["warnings"].extend(pancake_warnings)
        revenue_available = meta_revenue_available or bool(pancake_revenue_by_code)
        for code, bucket in grouped.items():
            if meta_revenue_available:
                revenue = {
                    "code": code,
                    "revenue_vnd": self._to_int(bucket["totals"].get("revenue_vnd")),
                    "order_count": self._to_int(bucket["totals"].get("order_count")),
                    "multi_code_order_count": 0,
                    "source": "meta_insights",
                }
            else:
                revenue = self._revenue_for_code_group(
                    code=code,
                    codes=bucket.get("codes") if isinstance(bucket.get("codes"), list) else [code],
                    revenue_by_code=pancake_revenue_by_code,
                )
                revenue["source"] = "pancake_fallback" if pancake_revenue_by_code else "none"
            bucket["revenue"] = {
                "revenue_vnd": self._to_int(revenue.get("revenue_vnd")),
                "order_count": self._to_int(revenue.get("order_count")),
                "multi_code_order_count": self._to_int(revenue.get("multi_code_order_count")),
                "source": str(revenue.get("source", "")).strip(),
            }
            spend_vnd = self._to_int(bucket["totals"].get("spend_vnd"))
            revenue_vnd = self._to_int(bucket["revenue"].get("revenue_vnd"))
            bucket["roas"] = round(revenue_vnd / spend_vnd, 2) if spend_vnd > 0 and revenue_vnd > 0 else None
            bucket["media"] = self._score_media_rows(
                list(bucket["media"].values()),
                code_roas=bucket["roas"],
                revenue_available=revenue_available,
            )
            bucket["media_count"] = len(bucket["media"])
            code_rows.append(bucket)

        code_rows.sort(
            key=lambda item: (
                self._to_int(item.get("revenue", {}).get("revenue_vnd")),
                self._to_int(item.get("totals", {}).get("spend_vnd")),
            ),
            reverse=True,
        )
        report["codes"] = code_rows
        report["summary"] = {
            "code_count": len(code_rows),
            "media_count": sum(self._to_int(item.get("media_count")) for item in code_rows),
            "source_ad_count": included_ad_count,
            "unmatched_source_ad_count": unmatched_ad_count,
        }
        if requested_codes:
            present_codes: set[str] = set()
            for item in code_rows:
                if not isinstance(item, dict):
                    continue
                codes = item.get("codes") if isinstance(item.get("codes"), list) else []
                present_codes.update(self._normalize_code_group(codes))
                present_codes.update(self._extract_codes_from_any(item.get("code")))
            missing_codes = sorted(requested_codes - present_codes)
            if missing_codes:
                report["warnings"].append("Không thấy dữ liệu ads cho mã: " + ", ".join(missing_codes))
        report["sheet_sync"] = self.sync_report_to_sheet(report)
        self._save_report(report)
        return report

    def sync_report_to_sheet(self, report: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": bool(self.settings.media_analytics_sheet_enabled),
            "ok": True,
            "spreadsheet_id": str(self.settings.media_analytics_sheet_spreadsheet_id).strip(),
            "gid": int(self.settings.media_analytics_sheet_gid),
            "sheet_title": "",
            "sheet_url": self._sheet_url(),
            "attempted": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "deleted_stale_ad_rows": 0,
            "deleted_stale_rows": 0,
            "sorted_rows": 0,
            "errors": [],
        }
        if not self.settings.media_analytics_sheet_enabled:
            return result

        ok, reason = self._sheet_config_status()
        if not ok:
            result["ok"] = False
            result["errors"] = [reason]
            return result

        rows = self._report_to_sheet_rows(report)
        result["attempted"] = len(rows)
        if not rows:
            return result

        try:
            sync_result = self._sync_sheet_rows(
                rows,
                delete_missing_media_rows=not bool(report.get("requested_codes")) and not bool(report.get("campaign_query")),
            )
            result.update(sync_result)
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Ghi media performance vao Google Sheet that bai")
            result["ok"] = False
            result["errors"] = [str(exc)]
            return result

    def build_messages(self, report: dict[str, Any]) -> list[str]:
        start_text = self._format_date(str(report.get("start_date", "")))
        end_text = self._format_date(str(report.get("end_date", "")))
        header = [
            "Phân tích media ADS2 theo mã VXV",
            f"Kỳ: {start_text} - {end_text}",
            f"Tổng quan: {self._status_text(report)}",
        ]
        campaign_query = str(report.get("campaign_query", "")).strip()
        if campaign_query:
            header.append(f"Lọc camp: {campaign_query}")

        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        header.append(
            "Dữ liệu: "
            f"{self._to_int(summary.get('code_count'))} mã | "
            f"{self._to_int(summary.get('media_count'))} media"
        )

        warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
        for warning in warnings[:3]:
            header.append(f"Cảnh báo: {warning}")
        sheet_sync = report.get("sheet_sync") if isinstance(report.get("sheet_sync"), dict) else {}
        if sheet_sync:
            if sheet_sync.get("enabled") and sheet_sync.get("ok"):
                header.append(
                    "Google Sheet: "
                    f"{sheet_sync.get('sheet_url')} "
                    f"(upsert {self._to_int(sheet_sync.get('inserted')) + self._to_int(sheet_sync.get('updated')):,} dòng)"
                )
            elif sheet_sync.get("enabled"):
                errors_text = "; ".join(str(item) for item in sheet_sync.get("errors", [])[:2])
                header.append(f"Google Sheet lỗi: {errors_text or 'không rõ lỗi'}")
        errors = report.get("errors") if isinstance(report.get("errors"), dict) else {}
        if errors:
            for key, value in errors.items():
                header.append(f"Lỗi {key}: {value}")

        messages: list[str] = []
        current = "\n".join(header).strip()
        codes = report.get("codes") if isinstance(report.get("codes"), list) else []
        if not codes:
            current += "\n\nChưa có dữ liệu media theo mã trong kỳ này."
            return [current]

        for code in codes:
            if not isinstance(code, dict):
                continue
            block = self._build_code_block(code)
            if len(current) + len(block) + 2 > 3600:
                messages.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            messages.append(current)
        return messages

    def _default_command(self) -> MediaPerformanceCommand:
        end_date = datetime.now(self._resolve_timezone()).date()
        days = max(1, int(self.settings.media_analytics_history_days))
        start_date = end_date.fromordinal(end_date.toordinal() - days + 1)
        return MediaPerformanceCommand(start_date=start_date, end_date=end_date, days=days)

    def _settings_profile_name(self) -> str:
        return str(
            getattr(self.settings, "profile_name", None)
            or getattr(self.settings, "profile", None)
            or "default"
        ).strip() or "default"

    def _build_code_block(self, code: dict[str, Any]) -> str:
        totals = code.get("totals", {}) if isinstance(code.get("totals"), dict) else {}
        revenue = code.get("revenue", {}) if isinstance(code.get("revenue"), dict) else {}
        roas = code.get("roas")
        lines = [
            f"Mã {code.get('code', 'UNKNOWN')}",
            (
                f"- Spend: {self._fmt_vnd(self._to_int(totals.get('spend_vnd')))} | "
                f"DT: {self._fmt_vnd(self._to_int(revenue.get('revenue_vnd')))} | "
                f"ROAS: {self._fmt_ratio(roas)}"
            ),
            (
                f"- Mess: {self._to_int(totals.get('messages')):,} | "
                f"Clicks: {self._to_int(totals.get('clicks')):,} | "
                f"Views: {self._to_int(totals.get('views')):,} | "
                f"CTR: {self._fmt_percent(totals.get('ctr'))} | "
                f"CPC: {self._fmt_vnd(self._to_int(totals.get('cpc_vnd')))}"
            ),
        ]
        if self._to_int(code.get("multi_code_ad_count")) > 0:
            lines.append("- Có nguồn media chứa nhiều mã; spend được gắn vào từng mã.")

        media_rows = code.get("media") if isinstance(code.get("media"), list) else []
        if media_rows:
            lines.append("- Media nên scale/theo dõi:")
            for index, media in enumerate(media_rows[:5], start=1):
                totals = media.get("totals", {}) if isinstance(media.get("totals"), dict) else {}
                lines.append(
                    f"  {index}) {media.get('label')} {self._to_float(media.get('score')):.0f}đ | "
                    f"{self._fmt_vnd(self._to_int(totals.get('spend_vnd')))} | "
                    f"mess {self._to_int(totals.get('messages')):,} | "
                    f"CTR {self._fmt_percent(totals.get('ctr'))} | "
                    f"CPC {self._fmt_vnd(self._to_int(totals.get('cpc_vnd')))}"
                )
        cut_rows = [item for item in media_rows if str(item.get("label", "")).upper() == "CUT"]
        if cut_rows:
            lines.append("- Media yếu cần xem xét:")
            for media in cut_rows[:3]:
                totals = media.get("totals", {}) if isinstance(media.get("totals"), dict) else {}
                lines.append(
                    f"  - {media.get('media_key')}: {self._fmt_vnd(self._to_int(totals.get('spend_vnd')))}, "
                    f"mess {self._to_int(totals.get('messages')):,}, CTR {self._fmt_percent(totals.get('ctr'))}"
                )
        return "\n".join(lines)

    def _score_media_rows(
        self,
        media_rows: list[dict[str, Any]],
        *,
        code_roas: float | None,
        revenue_available: bool,
    ) -> list[dict[str, Any]]:
        max_messages = max((self._to_int(item["totals"].get("messages")) for item in media_rows), default=0)
        max_clicks = max((self._to_int(item["totals"].get("clicks")) for item in media_rows), default=0)
        max_views = max((self._to_int(item["totals"].get("views")) for item in media_rows), default=0)
        message_costs = [
            self._to_int(item["totals"].get("cost_per_message_vnd"))
            for item in media_rows
            if self._to_int(item["totals"].get("messages")) > 0 and self._to_int(item["totals"].get("cost_per_message_vnd")) > 0
        ]
        click_costs = [
            self._to_int(item["totals"].get("cpc_vnd"))
            for item in media_rows
            if self._to_int(item["totals"].get("clicks")) > 0 and self._to_int(item["totals"].get("cpc_vnd")) > 0
        ]
        best_message_cost = min(message_costs) if message_costs else None
        best_click_cost = min(click_costs) if click_costs else None

        for item in media_rows:
            totals = item["totals"]
            spend = self._to_int(totals.get("spend_vnd"))
            messages = self._to_int(totals.get("messages"))
            clicks = self._to_int(totals.get("clicks"))
            views = self._to_int(totals.get("views"))
            ctr = self._to_float(totals.get("ctr"))
            cost_per_message = self._to_int(totals.get("cost_per_message_vnd"))
            cpc = self._to_int(totals.get("cpc_vnd"))
            revenue_vnd = self._to_int(totals.get("revenue_vnd"))
            media_roas = round(revenue_vnd / spend, 2) if spend > 0 and revenue_vnd > 0 else None
            item["roas"] = media_roas

            result_component = 0.0
            if messages > 0 and best_message_cost and cost_per_message > 0:
                result_component = min(1.0, best_message_cost / cost_per_message)
            elif spend < self.settings.media_analytics_min_spend_vnd:
                result_component = 0.35

            ctr_component = min(1.0, ctr / 2.0) if ctr > 0 else 0.0
            cpc_component = min(1.0, best_click_cost / cpc) if best_click_cost and cpc > 0 else 0.0
            click_component = (ctr_component + cpc_component) / 2

            volume_values = []
            if max_messages > 0:
                volume_values.append(messages / max_messages)
            if max_clicks > 0:
                volume_values.append(clicks / max_clicks)
            if max_views > 0:
                volume_values.append(views / max_views)
            volume_component = sum(volume_values) / len(volume_values) if volume_values else 0.0

            sample_component = min(1.0, spend / max(1, self.settings.media_analytics_min_spend_vnd))
            roas_for_score = media_roas if media_roas is not None else code_roas
            roas_component = min(1.0, float(roas_for_score or 0) / 2.5) if revenue_available else 0.5

            score = (
                roas_component * 30
                + result_component * 25
                + click_component * 20
                + volume_component * 15
                + sample_component * 10
            )
            item["score"] = round(score, 1)
            item["label"] = self._label_media(score=score, totals=totals)
            item["ads"].sort(
                key=lambda ad: self._to_int(ad.get("metrics", {}).get("spend_vnd")) if isinstance(ad.get("metrics"), dict) else 0,
                reverse=True,
            )

        media_rows.sort(
            key=lambda item: (
                str(item.get("label", "")) == "SCALE",
                self._to_float(item.get("score")),
                self._to_int(item["totals"].get("spend_vnd")),
            ),
            reverse=True,
        )
        return media_rows

    def _label_media(self, *, score: float, totals: dict[str, Any]) -> str:
        spend = self._to_int(totals.get("spend_vnd"))
        impressions = self._to_int(totals.get("impressions"))
        messages = self._to_int(totals.get("messages"))
        clicks = self._to_int(totals.get("clicks"))
        if spend <= 0 and impressions <= 0:
            return "NO_DATA"
        if spend >= self.settings.media_analytics_min_spend_vnd and messages <= 0 and clicks <= 0:
            return "CUT"
        if score >= 70 and spend >= self.settings.media_analytics_min_spend_vnd:
            return "SCALE"
        if score < 40 and spend >= self.settings.media_analytics_min_spend_vnd:
            return "CUT"
        return "HOLD"

    def _metrics_from_insight(self, row: dict[str, Any]) -> dict[str, Any]:
        spend_vnd = self._to_int_money(row.get("spend"))
        impressions = self._to_int(row.get("impressions"))
        clicks = self._to_int(row.get("clicks")) or self._to_int(row.get("inline_link_clicks"))
        inline_link_clicks = self._to_int(row.get("inline_link_clicks"))
        messages = self._action_count_by_priority(row.get("actions"), _MESSAGE_ACTION_TYPES)
        order_count = self._action_count_by_priority(row.get("actions"), _ORDER_ACTION_TYPES)
        revenue_vnd = self._action_money_by_priority(row.get("action_values"), _REVENUE_ACTION_TYPES)
        views = self._sum_actions(row.get("actions"), _VIEW_ACTION_MARKERS)
        views += self._sum_actions(row.get("video_play_actions"), _VIEW_ACTION_MARKERS)
        views += self._sum_actions(row.get("video_thruplay_watched_actions"), _VIEW_ACTION_MARKERS)
        reactions = self._sum_actions(row.get("actions"), _REACTION_ACTION_MARKERS)
        ctr = round((clicks / impressions) * 100, 2) if impressions > 0 and clicks > 0 else 0.0
        cpc_vnd = int(round(spend_vnd / clicks)) if clicks > 0 else 0
        cpm_vnd = int(round((spend_vnd / impressions) * 1000)) if impressions > 0 else 0
        cost_per_message_vnd = int(round(spend_vnd / messages)) if messages > 0 else 0
        return {
            "spend_vnd": spend_vnd,
            "impressions": impressions,
            "reach": self._to_int(row.get("reach")),
            "clicks": clicks,
            "inline_link_clicks": inline_link_clicks,
            "messages": messages,
            "order_count": order_count,
            "revenue_vnd": revenue_vnd,
            "views": views,
            "reactions": reactions,
            "ctr": ctr,
            "cpc_vnd": cpc_vnd,
            "cpm_vnd": cpm_vnd,
            "cost_per_message_vnd": cost_per_message_vnd,
        }

    def _add_metrics(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key in (
            "spend_vnd",
            "impressions",
            "reach",
            "clicks",
            "inline_link_clicks",
            "messages",
            "order_count",
            "revenue_vnd",
            "views",
            "reactions",
        ):
            target[key] = self._to_int(target.get(key)) + self._to_int(source.get(key))
        impressions = self._to_int(target.get("impressions"))
        clicks = self._to_int(target.get("clicks"))
        spend = self._to_int(target.get("spend_vnd"))
        messages = self._to_int(target.get("messages"))
        target["ctr"] = round((clicks / impressions) * 100, 2) if impressions > 0 and clicks > 0 else 0.0
        target["cpc_vnd"] = int(round(spend / clicks)) if clicks > 0 else 0
        target["cpm_vnd"] = int(round((spend / impressions) * 1000)) if impressions > 0 else 0
        target["cost_per_message_vnd"] = int(round(spend / messages)) if messages > 0 else 0

    def _empty_metrics(self) -> dict[str, Any]:
        return {
            "spend_vnd": 0,
            "impressions": 0,
            "reach": 0,
            "clicks": 0,
            "inline_link_clicks": 0,
            "messages": 0,
            "order_count": 0,
            "revenue_vnd": 0,
            "views": 0,
            "reactions": 0,
            "ctr": 0.0,
            "cpc_vnd": 0,
            "cpm_vnd": 0,
            "cost_per_message_vnd": 0,
        }

    def _empty_code_bucket(self, code: str, *, codes: list[str] | None = None) -> dict[str, Any]:
        code_group = self._normalize_code_group(codes or self._extract_codes_from_any(code))
        return {
            "code": code,
            "codes": code_group or [code],
            "totals": self._empty_metrics(),
            "revenue": self._empty_revenue_bucket(code),
            "roas": None,
            "ad_count": 0,
            "media_count": 0,
            "multi_code_ad_count": 0,
            "media": {},
        }

    def _empty_media_bucket(self, *, media_key: str, story_id: str, post_meta: dict[str, Any]) -> dict[str, Any]:
        permalink = self._first_text(post_meta.get("permalink_url"), self._permalink_from_story_id(story_id))
        message = str(post_meta.get("message", "")).strip()
        return {
            "media_key": media_key,
            "story_id": story_id,
            "permalink_url": permalink,
            "message_preview": self._short(message, 160),
            "totals": self._empty_metrics(),
            "score": 0.0,
            "label": "NO_DATA",
            "multi_code": False,
            "ads": [],
        }

    def _empty_revenue_bucket(self, code: str) -> dict[str, Any]:
        return {
            "code": code,
            "revenue_vnd": 0,
            "order_count": 0,
            "multi_code_order_count": 0,
        }

    def _build_revenue_by_code(self, *, start_date: date, end_date: date) -> tuple[dict[str, dict[str, Any]], list[str]]:
        warnings: list[str] = []
        if not self.pancake.is_configured() or self.settings.pancake_shop_id <= 0:
            return {}, ["Chưa cấu hình Pancake cho ADS2, nên ROAS chỉ hiển thị khi có dữ liệu doanh thu."]
        try:
            snapshot = self.pancake.fetch_orders_snapshot_for_range(start_date, end_date, self.settings.app_timezone)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Lay Pancake orders that bai cho media performance")
            return {}, [f"Không lấy được doanh thu Pancake: {exc}"]

        orders = snapshot.get("orders", []) if isinstance(snapshot, dict) else []
        if not isinstance(orders, list):
            return {}, ["Pancake trả dữ liệu orders không hợp lệ."]

        buckets: dict[str, dict[str, Any]] = {}
        order_ids_by_code: dict[str, set[str]] = {}
        multi_order_ids_by_code: dict[str, set[str]] = {}
        for index, order in enumerate(orders):
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("id") or order.get("order_id") or order.get("bill_full_name") or index).strip()
            order_codes = self._extract_codes_from_any(order)
            item_codes_found = False
            items = order.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_codes = self._extract_codes_from_any(item)
                    if not item_codes:
                        continue
                    item_codes_found = True
                    revenue_vnd = self._item_revenue_vnd(item=item, order=order)
                    for code in item_codes:
                        bucket = buckets.setdefault(code, self._empty_revenue_bucket(code))
                        bucket["revenue_vnd"] = self._to_int(bucket.get("revenue_vnd")) + revenue_vnd
                        order_ids_by_code.setdefault(code, set()).add(order_id)
                        if len(item_codes) > 1 or len(order_codes) > 1:
                            multi_order_ids_by_code.setdefault(code, set()).add(order_id)

            if item_codes_found or not order_codes:
                continue
            order_revenue_vnd = self._order_total_vnd(order)
            for code in order_codes:
                bucket = buckets.setdefault(code, self._empty_revenue_bucket(code))
                bucket["revenue_vnd"] = self._to_int(bucket.get("revenue_vnd")) + order_revenue_vnd
                order_ids_by_code.setdefault(code, set()).add(order_id)
                if len(order_codes) > 1:
                    multi_order_ids_by_code.setdefault(code, set()).add(order_id)

        for code, bucket in buckets.items():
            order_ids = order_ids_by_code.get(code, set())
            multi_order_ids = multi_order_ids_by_code.get(code, set())
            bucket["order_count"] = len(order_ids)
            bucket["multi_code_order_count"] = len(multi_order_ids)
            bucket["_order_ids"] = sorted(order_ids)
            bucket["_multi_order_ids"] = sorted(multi_order_ids)
        return buckets, warnings

    def _revenue_for_code_group(
        self,
        *,
        code: str,
        codes: list[Any],
        revenue_by_code: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        code_group = self._normalize_code_group(codes or [code])
        if not code_group:
            return self._empty_revenue_bucket(code)
        if len(code_group) == 1:
            return dict(revenue_by_code.get(code_group[0], self._empty_revenue_bucket(code)))

        revenue_vnd = 0
        order_count = 0
        multi_code_order_count = 0
        order_ids: set[str] = set()
        multi_order_ids: set[str] = set()
        has_order_ids = False
        has_multi_order_ids = False
        for item_code in code_group:
            bucket = revenue_by_code.get(item_code, {})
            if not isinstance(bucket, dict):
                continue
            revenue_vnd += self._to_int(bucket.get("revenue_vnd"))
            bucket_order_ids = {
                str(item).strip()
                for item in bucket.get("_order_ids", [])
                if str(item).strip()
            } if isinstance(bucket.get("_order_ids"), list) else set()
            bucket_multi_order_ids = {
                str(item).strip()
                for item in bucket.get("_multi_order_ids", [])
                if str(item).strip()
            } if isinstance(bucket.get("_multi_order_ids"), list) else set()
            if bucket_order_ids:
                has_order_ids = True
                order_ids.update(bucket_order_ids)
            else:
                order_count += self._to_int(bucket.get("order_count"))
            if bucket_multi_order_ids:
                has_multi_order_ids = True
                multi_order_ids.update(bucket_multi_order_ids)
            else:
                multi_code_order_count += self._to_int(bucket.get("multi_code_order_count"))

        return {
            "code": code,
            "revenue_vnd": revenue_vnd,
            "order_count": len(order_ids) if has_order_ids else order_count,
            "multi_code_order_count": len(multi_order_ids) if has_multi_order_ids else multi_code_order_count,
        }

    def _load_job_index(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        index: dict[str, dict[str, list[dict[str, Any]]]] = {
            "ad_id": {},
            "story_id": {},
        }
        for root in (
            self.settings.jobs_pending_dir,
            self.settings.jobs_published_dir,
            self.settings.jobs_cancelled_dir,
            self.settings.jobs_failed_dir,
        ):
            if not root.exists():
                continue
            for path in root.glob("*.json"):
                try:
                    payload = load_json(path)
                except Exception:  # noqa: BLE001
                    self.logger.warning("Khong doc duoc job history %s", path)
                    continue
                if not isinstance(payload, dict):
                    continue
                for ad_id in payload.get("ad_ids", []) if isinstance(payload.get("ad_ids"), list) else []:
                    normalized_ad_id = str(ad_id).strip()
                    if normalized_ad_id:
                        index["ad_id"].setdefault(normalized_ad_id, []).append(payload)
                story_id = str(payload.get("object_story_id", "")).strip()
                if story_id:
                    index["story_id"].setdefault(story_id, []).append(payload)
        return index

    def _jobs_for_ad(
        self,
        *,
        ad_id: str,
        story_id: str,
        job_index: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        seen: set[int] = set()
        for job in job_index.get("ad_id", {}).get(ad_id, []):
            marker = id(job)
            if marker not in seen:
                seen.add(marker)
                jobs.append(job)
        if story_id:
            for job in job_index.get("story_id", {}).get(story_id, []):
                marker = id(job)
                if marker not in seen:
                    seen.add(marker)
                    jobs.append(job)
        return jobs

    def _extract_codes_for_ad(
        self,
        *,
        row: dict[str, Any],
        ad_meta: dict[str, Any],
        post_meta: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> list[str]:
        fragments: list[Any] = [
            row.get("campaign_name"),
            row.get("adset_name"),
            row.get("ad_name"),
            ad_meta.get("campaign_name"),
            ad_meta.get("adset_name"),
            ad_meta.get("ad_name"),
            post_meta.get("message"),
            post_meta.get("permalink_url"),
        ]
        for job in jobs:
            fragments.extend(
                [
                    job.get("campaign_name"),
                    job.get("sku_code_text"),
                    job.get("selected_campaign_name"),
                    job.get("campaign_match_keywords"),
                    job.get("post_url"),
                    job.get("resolved_permalink_url"),
                ]
            )
        codes: list[str] = []
        seen: set[str] = set()
        for fragment in fragments:
            for code in self._extract_codes_from_any(fragment):
                if code in seen:
                    continue
                seen.add(code)
                codes.append(code)
        return codes

    def _matches_campaign_filter(self, *, row: dict[str, Any], ad_meta: dict[str, Any], campaign_query: str) -> bool:
        query = str(campaign_query or "").strip()
        if not query:
            return True
        candidates = [
            row.get("campaign_id"),
            row.get("campaign_name"),
            ad_meta.get("campaign_id"),
            ad_meta.get("campaign_name"),
        ]
        normalized_query = self._normalize(query)
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            if text == query:
                return True
            if normalized_query in self._normalize(text):
                return True
        return False

    def _extract_story_ids(self, metadata: dict[str, dict[str, Any]]) -> set[str]:
        story_ids: set[str] = set()
        for item in metadata.values():
            story_id = self._story_id_from_ad_meta(item)
            if story_id:
                story_ids.add(story_id)
        return story_ids

    def _story_id_from_ad_meta(self, ad_meta: dict[str, Any]) -> str:
        return self._first_text(ad_meta.get("object_story_id"), ad_meta.get("effective_object_story_id"))

    @staticmethod
    def _permalink_from_story_id(story_id: str) -> str:
        normalized = str(story_id or "").strip()
        if "_" not in normalized:
            return ""
        page_id, post_id = normalized.split("_", 1)
        if not page_id or not post_id or not page_id.isdigit() or not post_id.isdigit():
            return ""
        return f"https://www.facebook.com/permalink.php?story_fbid={post_id}&id={page_id}"

    def _extract_codes_from_any(self, value: Any) -> list[str]:
        text = " ".join(self._iter_text_fragments(value))
        codes: list[str] = []
        seen: set[str] = set()
        for match in _VXV_CODE_PATTERN.finditer(text):
            code = match.group(0).upper()
            if code in seen:
                continue
            seen.add(code)
            codes.append(code)
        return codes

    @staticmethod
    def _normalize_code_group(codes: Any) -> list[str]:
        if not isinstance(codes, list | tuple | set):
            codes = [codes]
        normalized: list[str] = []
        seen: set[str] = set()
        for code in codes:
            text = str(code or "").strip().upper()
            if not text or not _VXV_CODE_PATTERN.fullmatch(text) or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return sorted(normalized)

    @staticmethod
    def _code_group_label(codes: list[str]) -> str:
        return "_".join(codes)

    def _iter_text_fragments(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, int, float)):
            return [str(value)]
        if isinstance(value, list):
            fragments: list[str] = []
            for item in value:
                fragments.extend(self._iter_text_fragments(item))
            return fragments
        if isinstance(value, dict):
            fragments = []
            for item in value.values():
                fragments.extend(self._iter_text_fragments(item))
            return fragments
        return [str(value)]

    def _item_revenue_vnd(self, *, item: dict[str, Any], order: dict[str, Any]) -> int:
        quantity = max(0, self._to_int(item.get("quantity")))
        variation_info = item.get("variation_info") if isinstance(item.get("variation_info"), dict) else {}
        unit_price_minor = max(0, self._to_int(variation_info.get("retail_price")))
        currency = str(order.get("order_currency", "")).strip().upper()
        return self._minor_to_vnd(unit_price_minor * quantity, currency)

    def _order_total_vnd(self, order: dict[str, Any]) -> int:
        currency = str(order.get("order_currency", "")).strip().upper()
        return self._minor_to_vnd(max(0, self._to_int(order.get("total_price"))), currency)

    def _minor_to_vnd(self, value: int, currency: str) -> int:
        if currency == "THB":
            thb = value / max(1, self.settings.report_thb_minor_unit_factor)
            return int(round(thb * max(0.0, self.settings.report_thb_to_vnd_rate)))
        if currency == "VND":
            return value
        return 0

    def _sum_actions(self, actions: Any, markers: tuple[str, ...]) -> int:
        if not isinstance(actions, list):
            return 0
        total = 0
        for item in actions:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("action_type", "")).strip().lower()
            if not any(marker in action_type for marker in markers):
                continue
            total += self._to_int(item.get("value"))
        return total

    def _action_count_by_priority(self, actions: Any, action_types: tuple[str, ...]) -> int:
        values = self._action_values_by_type(actions)
        for action_type in action_types:
            value = self._to_int(values.get(action_type))
            if value:
                return value
        return 0

    def _action_money_by_priority(self, actions: Any, action_types: tuple[str, ...]) -> int:
        values = self._action_values_by_type(actions)
        for action_type in action_types:
            value = self._to_int_money(values.get(action_type))
            if value:
                return value
        return 0

    def _action_values_by_type(self, actions: Any) -> dict[str, Any]:
        if not isinstance(actions, list):
            return {}
        values: dict[str, Any] = {}
        for item in actions:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("action_type", "")).strip().lower()
            if not action_type:
                continue
            values[action_type] = item.get("value")
        return values

    def _save_report(self, report: dict[str, Any]) -> None:
        start_date = str(report.get("start_date", "unknown")).replace("/", "-")
        end_date = str(report.get("end_date", "unknown")).replace("/", "-")
        stamp = datetime.now(self._resolve_timezone()).strftime("%Y%m%dT%H%M%S")
        path = self.output_dir / f"report_{start_date}_{end_date}_{stamp}.json"
        report["report_path"] = str(path)
        dump_json(path, report)

    def _sheet_config_status(self) -> tuple[bool, str]:
        if not str(self.settings.media_analytics_sheet_spreadsheet_id).strip():
            return False, "Thiếu MEDIA_ANALYTICS_SHEET_SPREADSHEET_ID."
        if int(self.settings.media_analytics_sheet_gid) <= 0:
            return False, "Thiếu MEDIA_ANALYTICS_SHEET_GID."
        if not str(self.settings.media_analytics_sheet_oauth_client_id).strip():
            return False, "Thiếu MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_ID."
        if not str(self.settings.media_analytics_sheet_oauth_client_secret).strip():
            return False, "Thiếu MEDIA_ANALYTICS_SHEET_OAUTH_CLIENT_SECRET."
        if not str(self.settings.media_analytics_sheet_oauth_refresh_token).strip():
            return False, "Thiếu MEDIA_ANALYTICS_SHEET_OAUTH_REFRESH_TOKEN."
        return True, ""

    def _report_to_sheet_rows(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        generated_at = str(report.get("generated_at", "")).strip()
        start_date = str(report.get("start_date", "")).strip()
        end_date = str(report.get("end_date", "")).strip()
        rows: list[dict[str, Any]] = []
        for code in report.get("codes", []) if isinstance(report.get("codes"), list) else []:
            if not isinstance(code, dict):
                continue
            code_name = str(code.get("code", "")).strip().upper()
            if not code_name:
                continue
            totals = code.get("totals", {}) if isinstance(code.get("totals"), dict) else {}
            revenue = code.get("revenue", {}) if isinstance(code.get("revenue"), dict) else {}
            for media in code.get("media", []) if isinstance(code.get("media"), list) else []:
                if not isinstance(media, dict):
                    continue
                media_key = str(media.get("media_key", "")).strip()
                media_totals = media.get("totals", {}) if isinstance(media.get("totals"), dict) else {}
                media_row_key = f"{start_date}:{end_date}:{code_name}:media:{media_key}"
                rows.append(
                    self._sheet_row(
                        dedupe_key=media_row_key,
                        generated_at=generated_at,
                        start_date=start_date,
                        end_date=end_date,
                        code=code_name,
                        level="media",
                        totals=media_totals,
                        revenue_vnd=self._to_int(media_totals.get("revenue_vnd")),
                        roas=media.get("roas"),
                        order_count=self._to_int(media_totals.get("order_count")),
                        media_key=media_key,
                        story_id=str(media.get("story_id", "")).strip(),
                        permalink_url=str(media.get("permalink_url", "")).strip(),
                        label=str(media.get("label", "")).strip(),
                        score=media.get("score"),
                    )
                )
        return rows

    def _sheet_row(
        self,
        *,
        dedupe_key: str,
        generated_at: str,
        start_date: str,
        end_date: str,
        code: str,
        level: str,
        totals: dict[str, Any],
        revenue_vnd: Any,
        roas: Any,
        order_count: Any,
        media_key: str,
        story_id: str,
        permalink_url: str,
        label: str,
        score: Any,
    ) -> dict[str, Any]:
        spend_vnd = self._to_int(totals.get("spend_vnd"))
        impressions = self._to_int(totals.get("impressions"))
        order_count_int = self._to_int(order_count)
        return {
            "dedupe_key": dedupe_key,
            "generated_at": generated_at,
            "start_date": start_date,
            "end_date": end_date,
            "code": code,
            "level": level,
            "media_key": media_key,
            "story_id": story_id,
            "permalink_url": permalink_url,
            "label": label,
            "score": score,
            "spend_vnd": spend_vnd,
            "messages": self._to_int(totals.get("messages")),
            "cost_per_message_vnd": self._to_int(totals.get("cost_per_message_vnd")),
            "order_count": order_count_int,
            "cost_per_order_vnd": int(round(spend_vnd / order_count_int)) if order_count_int > 0 else 0,
            "revenue_vnd": revenue_vnd,
            "roas": roas,
            "reactions": self._to_int(totals.get("reactions")),
            "reach": self._to_int(totals.get("reach")),
            "views": self._to_int(totals.get("views")),
            "clicks": self._to_int(totals.get("clicks")),
            "cpc_vnd": self._to_int(totals.get("cpc_vnd")),
            "ctr": totals.get("ctr", 0),
            "cpm_vnd": self._to_int(totals.get("cpm_vnd"))
            or (int(round((spend_vnd / impressions) * 1000)) if impressions > 0 else 0),
        }

    def _sync_sheet_rows(self, rows: list[dict[str, Any]], *, delete_missing_media_rows: bool = True) -> dict[str, Any]:
        access_token = self._refresh_sheet_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        spreadsheet_id = str(self.settings.media_analytics_sheet_spreadsheet_id).strip()
        gid = int(self.settings.media_analytics_sheet_gid)
        sheet_title = self._resolve_sheet_title(spreadsheet_id=spreadsheet_id, gid=gid, headers=headers)
        self._ensure_sheet_header(spreadsheet_id=spreadsheet_id, sheet_title=sheet_title, headers=headers)
        self._format_sheet_header(spreadsheet_id=spreadsheet_id, headers=headers)

        unique_rows: dict[str, dict[str, Any]] = {}
        skipped = 0
        for row in rows:
            key = str(row.get("dedupe_key", "")).strip()
            if not key:
                skipped += 1
                continue
            if key in unique_rows:
                skipped += 1
                continue
            unique_rows[key] = row

        deleted_stale_rows = self._delete_stale_sheet_rows_for_period(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            start_date=str(rows[0].get("start_date", "")).strip(),
            end_date=str(rows[0].get("end_date", "")).strip(),
            expected_dedupe_keys=set(unique_rows),
            delete_missing_media_rows=delete_missing_media_rows,
        )
        existing_map = self._load_existing_sheet_map(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )

        updates: list[tuple[int, list[Any]]] = []
        appends: list[list[Any]] = []
        for dedupe_key, row in unique_rows.items():
            values = self._sheet_row_values(row)
            row_number = existing_map.get(dedupe_key)
            if row_number:
                updates.append((row_number, values))
            else:
                appends.append(values)
        if updates:
            self._batch_update_sheet_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                updates=updates,
            )
        if appends:
            self._append_sheet_rows(
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
                headers=headers,
                rows=appends,
            )
        sorted_rows = self._sort_sheet_rows_by_period(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        row_map_after_sync = self._load_existing_sheet_map(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
        )
        synced_row_numbers = [
            row_map_after_sync[dedupe_key] for dedupe_key in unique_rows if dedupe_key in row_map_after_sync
        ]
        self._format_sheet_data_rows(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            row_numbers=synced_row_numbers,
            start_date=str(rows[0].get("start_date", "")).strip(),
            end_date=str(rows[0].get("end_date", "")).strip(),
        )

        return {
            "ok": True,
            "sheet_title": sheet_title,
            "inserted": len(appends),
            "updated": len(updates),
            "skipped": skipped,
            "deleted_stale_ad_rows": deleted_stale_rows,
            "deleted_stale_rows": deleted_stale_rows,
            "sorted_rows": sorted_rows,
        }

    def _refresh_sheet_access_token(self) -> str:
        response = requests.request(
            method="POST",
            url=self.settings.media_analytics_sheet_oauth_token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "client_id": self.settings.media_analytics_sheet_oauth_client_id,
                "client_secret": self.settings.media_analytics_sheet_oauth_client_secret,
                "refresh_token": self.settings.media_analytics_sheet_oauth_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OAuth token endpoint lỗi ({response.status_code}): {self._short_text(response.text)}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OAuth token endpoint trả JSON không hợp lệ: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OAuth token endpoint trả dữ liệu không hợp lệ.")
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("Không lấy được access_token từ OAuth token endpoint.")
        return access_token

    def _resolve_sheet_title(self, *, spreadsheet_id: str, gid: int, headers: dict[str, str]) -> str:
        payload = self._sheet_request_json(
            "GET",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}",
            headers=headers,
            params={"fields": "sheets(properties(sheetId,title))"},
        )
        sheets = payload.get("sheets", [])
        if not isinstance(sheets, list):
            raise RuntimeError("Google Sheets API không trả danh sách sheets.")
        for item in sheets:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            if not isinstance(props, dict):
                continue
            if self._to_int(props.get("sheetId")) == gid:
                title = str(props.get("title", "")).strip()
                if title:
                    return title
        raise RuntimeError(f"Không tìm thấy tab Google Sheet có gid={gid}.")

    def _ensure_sheet_header(self, *, spreadsheet_id: str, sheet_title: str, headers: dict[str, str]) -> None:
        read_range = f"'{self._escape_sheet_title(sheet_title)}'!A1:AE1"
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=read_range,
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        first_row = values[0] if isinstance(values, list) and values else []
        header_last_column = self._sheet_column_label(len(MEDIA_PERFORMANCE_SHEET_HEADERS))
        if not first_row or not any(str(cell).strip() for cell in first_row if cell is not None):
            self._sheet_values_update(
                spreadsheet_id=spreadsheet_id,
                write_range=f"'{self._escape_sheet_title(sheet_title)}'!A1:{header_last_column}1",
                headers=headers,
                values=[list(MEDIA_PERFORMANCE_SHEET_HEADERS)],
            )
            return
        first_cell = str(first_row[0] if isinstance(first_row, list) and first_row else "").strip()
        legacy_metadata_first_cells = {"dedupe_key", "DEDUPE_KEY / KHÓA DÒNG"}
        valid_first_cells = {*legacy_metadata_first_cells, "start_date", MEDIA_PERFORMANCE_SHEET_HEADERS[0]}
        if first_cell not in valid_first_cells:
            raise RuntimeError(
                "Tab Google Sheet đang có dữ liệu/header khác ở dòng 1. "
                "Anh tạo tab trống hoặc đặt dòng 1 theo schema media analyzer trước khi sync."
            )
        if self._has_legacy_metadata_columns(first_row):
            self._delete_sheet_columns(
                spreadsheet_id=spreadsheet_id,
                headers=headers,
                start_index=0,
                end_index=2,
            )
            first_row = list(first_row[2:])
        legacy_ad_start = self._legacy_ad_columns_start(first_row)
        if legacy_ad_start is not None:
            self._delete_sheet_columns(
                spreadsheet_id=spreadsheet_id,
                headers=headers,
                start_index=legacy_ad_start,
                end_index=legacy_ad_start + 6,
            )
            first_row = list(first_row[:legacy_ad_start]) + list(first_row[legacy_ad_start + 6 :])
        if self._missing_reactions_sheet_column(first_row) or self._has_shifted_reactions_sheet_data(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            first_row=first_row,
        ):
            self._insert_sheet_columns(
                spreadsheet_id=spreadsheet_id,
                headers=headers,
                start_index=_REACTIONS_COLUMN_INDEX,
                end_index=_REACTIONS_COLUMN_INDEX + 1,
            )
            first_row = (
                list(first_row[:_REACTIONS_COLUMN_INDEX])
                + [""]
                + list(first_row[_REACTIONS_COLUMN_INDEX:])
            )
        if len(first_row) > len(MEDIA_PERFORMANCE_SHEET_HEADERS):
            self._delete_trailing_sheet_columns(
                spreadsheet_id=spreadsheet_id,
                headers=headers,
                start_index=len(MEDIA_PERFORMANCE_SHEET_HEADERS),
                end_index=len(first_row),
            )
            first_row = list(first_row[: len(MEDIA_PERFORMANCE_SHEET_HEADERS)])
        current_header = [str(cell or "").strip() for cell in first_row[: len(MEDIA_PERFORMANCE_SHEET_HEADERS)]]
        if current_header != MEDIA_PERFORMANCE_SHEET_HEADERS:
            self._sheet_values_update(
                spreadsheet_id=spreadsheet_id,
                write_range=f"'{self._escape_sheet_title(sheet_title)}'!A1:{header_last_column}1",
                headers=headers,
                values=[list(MEDIA_PERFORMANCE_SHEET_HEADERS)],
            )

    def _format_sheet_header(self, *, spreadsheet_id: str, headers: dict[str, str]) -> None:
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": int(self.settings.media_analytics_sheet_gid),
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(MEDIA_PERFORMANCE_SHEET_HEADERS),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "horizontalAlignment": "CENTER",
                                    "wrapStrategy": "WRAP",
                                    "textFormat": {"bold": True},
                                }
                            },
                            "fields": "userEnteredFormat(horizontalAlignment,wrapStrategy,textFormat.bold)",
                        }
                    }
                ]
            },
        )

    def _format_sheet_data_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        row_numbers: list[int],
        start_date: str,
        end_date: str,
    ) -> None:
        del row_numbers, start_date, end_date
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=f"'{self._escape_sheet_title(sheet_title)}'!A2:B",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return
        period_rows: list[tuple[int, tuple[str, str]]] = []
        for index, row in enumerate(values):
            if not isinstance(row, list):
                continue
            period_key = self._period_key_from_visible_sheet_row(row)
            if period_key:
                period_rows.append((index + 2, period_key))

        requests_payload = self._build_weekly_period_format_requests(period_rows)
        if not requests_payload:
            return
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={"requests": requests_payload},
        )

    def _load_existing_sheet_map(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> dict[str, int]:
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=f"'{self._escape_sheet_title(sheet_title)}'!A2:E",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return {}
        result: dict[str, int] = {}
        for index, row in enumerate(values):
            if not isinstance(row, list):
                continue
            dedupe_key = self._dedupe_key_from_visible_sheet_row(row)
            if dedupe_key:
                result[dedupe_key] = index + 2
        return result

    def _delete_stale_sheet_rows_for_period(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        start_date: str,
        end_date: str,
        expected_dedupe_keys: set[str],
        delete_missing_media_rows: bool,
    ) -> int:
        if not start_date or not end_date:
            return 0
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=f"'{self._escape_sheet_title(sheet_title)}'!A2:E",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return 0
        row_numbers: list[int] = []
        for index, row in enumerate(values):
            if not isinstance(row, list):
                continue
            row_start = str(row[0] if len(row) > 0 else "").strip()
            row_end = str(row[1] if len(row) > 1 else "").strip()
            level = str(row[3] if len(row) > 3 else "").strip().lower()
            is_same_period = row_start == start_date and row_end == end_date
            if not is_same_period:
                continue
            dedupe_key = self._dedupe_key_from_visible_sheet_row(row)
            if level in {"ad", "code"}:
                row_numbers.append(index + 2)
                continue
            if delete_missing_media_rows and level == "media" and dedupe_key not in expected_dedupe_keys:
                row_numbers.append(index + 2)
        if not row_numbers:
            return 0
        requests_payload = []
        for row_number in sorted(row_numbers, reverse=True):
            requests_payload.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": int(self.settings.media_analytics_sheet_gid),
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        }
                    }
                }
            )
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={"requests": requests_payload},
        )
        return len(row_numbers)

    def _delete_stale_ad_rows_for_period(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        start_date: str,
        end_date: str,
    ) -> int:
        return self._delete_stale_sheet_rows_for_period(
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            headers=headers,
            start_date=start_date,
            end_date=end_date,
            expected_dedupe_keys=set(),
            delete_missing_media_rows=False,
        )

    def _batch_update_sheet_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        updates: list[tuple[int, list[Any]]],
    ) -> None:
        data = [
            {
                "range": (
                    f"'{self._escape_sheet_title(sheet_title)}'!"
                    f"A{row_number}:{self._sheet_column_label(len(MEDIA_PERFORMANCE_SHEET_KEYS))}{row_number}"
                ),
                "majorDimension": "ROWS",
                "values": [values],
            }
            for row_number, values in updates
        ]
        if not data:
            return
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}/values:batchUpdate",
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            data={"data": data},
        )

    def _append_sheet_rows(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        rows: list[list[Any]],
    ) -> None:
        if not rows:
            return
        write_range = (
            f"'{self._escape_sheet_title(sheet_title)}'!"
            f"A:{self._sheet_column_label(len(MEDIA_PERFORMANCE_SHEET_KEYS))}"
        )
        encoded_range = quote(write_range, safe="")
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}:append",
            headers=headers,
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            data={"majorDimension": "ROWS", "values": rows},
        )

    def _sort_sheet_rows_by_period(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
    ) -> int:
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=f"'{self._escape_sheet_title(sheet_title)}'!A2:B",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return 0

        last_populated_row = 0
        populated_rows = 0
        for index, row in enumerate(values):
            if not isinstance(row, list):
                continue
            if not self._period_key_from_visible_sheet_row(row):
                continue
            populated_rows += 1
            last_populated_row = index + 2

        if populated_rows < 2 or last_populated_row < 3:
            return 0

        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={
                "requests": [
                    {
                        "sortRange": {
                            "range": {
                                "sheetId": int(self.settings.media_analytics_sheet_gid),
                                "startRowIndex": 1,
                                "endRowIndex": last_populated_row,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(MEDIA_PERFORMANCE_SHEET_KEYS),
                            },
                            "sortSpecs": [
                                {"dimensionIndex": 0, "sortOrder": "ASCENDING"},
                                {"dimensionIndex": 1, "sortOrder": "ASCENDING"},
                            ],
                        }
                    }
                ]
            },
        )
        return populated_rows

    def _sheet_values_get(
        self,
        *,
        spreadsheet_id: str,
        read_range: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        encoded_range = quote(read_range, safe="")
        return self._sheet_request_json(
            "GET",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}",
            headers=headers,
            params=params,
        )

    def _sheet_values_update(
        self,
        *,
        spreadsheet_id: str,
        write_range: str,
        headers: dict[str, str],
        values: list[list[Any]],
    ) -> None:
        encoded_range = quote(write_range, safe="")
        self._sheet_request_json(
            "PUT",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}/values/{encoded_range}",
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            data={"majorDimension": "ROWS", "values": values},
        )

    def _sheet_request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params or None,
            json=data if data is not None else None,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Google Sheets API lỗi ({response.status_code}): {self._short_text(response.text)}")
        try:
            payload = response.json() if str(response.text or "").strip() else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Google Sheets API trả JSON không hợp lệ: {self._short_text(response.text)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Google Sheets API trả dữ liệu không hợp lệ.")
        return payload

    @staticmethod
    def _sheet_row_values(row: dict[str, Any]) -> list[Any]:
        return ["" if row.get(key) is None else row.get(key, "") for key in MEDIA_PERFORMANCE_SHEET_KEYS]

    def _sheet_url(self) -> str:
        spreadsheet_id = str(self.settings.media_analytics_sheet_spreadsheet_id).strip()
        gid = int(self.settings.media_analytics_sheet_gid)
        if not spreadsheet_id:
            return ""
        return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit?gid={gid}#gid={gid}"

    @staticmethod
    def _has_legacy_metadata_columns(header_row: list[Any]) -> bool:
        first_two = [str(cell or "").strip() for cell in header_row[:2]]
        return first_two in (
            ["dedupe_key", "generated_at"],
            ["DEDUPE_KEY / KHÓA DÒNG", "GENERATED_AT / THỜI GIAN TẠO"],
        )

    @staticmethod
    def _legacy_ad_columns_start(header_row: list[Any]) -> int | None:
        legacy = ["campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name"]
        normalized = [str(cell or "").strip() for cell in header_row]
        for start_index in (11, 9):
            if normalized[start_index : start_index + 6] == legacy:
                return start_index
        return None

    @staticmethod
    def _missing_reactions_sheet_column(header_row: list[Any]) -> bool:
        normalized = [str(cell or "").strip() for cell in header_row]
        return normalized[: len(_LEGACY_MEDIA_PERFORMANCE_SHEET_HEADERS_WITHOUT_REACTIONS)] in (
            _LEGACY_MEDIA_PERFORMANCE_SHEET_HEADERS_WITHOUT_REACTIONS,
            _LEGACY_MEDIA_PERFORMANCE_SHEET_KEYS_WITHOUT_REACTIONS,
        )

    def _has_shifted_reactions_sheet_data(
        self,
        *,
        spreadsheet_id: str,
        sheet_title: str,
        headers: dict[str, str],
        first_row: list[Any],
    ) -> bool:
        current_header = [str(cell or "").strip() for cell in first_row[: len(MEDIA_PERFORMANCE_SHEET_HEADERS)]]
        if current_header != MEDIA_PERFORMANCE_SHEET_HEADERS:
            return False
        payload = self._sheet_values_get(
            spreadsheet_id=spreadsheet_id,
            read_range=f"'{self._escape_sheet_title(sheet_title)}'!A2:{self._sheet_column_label(len(MEDIA_PERFORMANCE_SHEET_HEADERS))}21",
            headers=headers,
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values", []) if isinstance(payload, dict) else []
        if not isinstance(values, list):
            return False
        return self._has_shifted_reactions_metric_rows(values)

    @staticmethod
    def _has_shifted_reactions_metric_rows(rows: list[Any]) -> bool:
        for row in rows:
            if not isinstance(row, list):
                continue
            level = str(row[3] if len(row) > 3 else "").strip().lower()
            if level not in {"code", "media", "ad"}:
                continue
            reactions_value = str(row[_REACTIONS_COLUMN_INDEX] if len(row) > _REACTIONS_COLUMN_INDEX else "").strip()
            reach_value = str(row[_REACTIONS_COLUMN_INDEX + 1] if len(row) > _REACTIONS_COLUMN_INDEX + 1 else "").strip()
            shifted_clicks_value = str(
                row[_REACTIONS_COLUMN_INDEX + 3] if len(row) > _REACTIONS_COLUMN_INDEX + 3 else ""
            ).strip()
            if (
                reactions_value
                and reach_value
                and MediaPerformanceService._looks_like_integer_cell(reactions_value)
                and MediaPerformanceService._looks_like_integer_cell(reach_value)
                and MediaPerformanceService._looks_like_currency_cell(shifted_clicks_value)
            ):
                return True
        return False

    @staticmethod
    def _looks_like_integer_cell(value: str) -> bool:
        text = str(value or "").strip().replace(".", "").replace(",", "")
        return text.isdigit()

    @staticmethod
    def _looks_like_currency_cell(value: str) -> bool:
        text = str(value or "").strip().lower()
        return "đ" in text or "vnd" in text

    @staticmethod
    def _dedupe_key_from_visible_sheet_row(row: list[Any]) -> str:
        start_date = str(row[0] if len(row) > 0 else "").strip()
        end_date = str(row[1] if len(row) > 1 else "").strip()
        code = str(row[2] if len(row) > 2 else "").strip().upper()
        level = str(row[3] if len(row) > 3 else "").strip().lower()
        media_key = str(row[4] if len(row) > 4 else "").strip()
        if not start_date or not end_date or not code or not level:
            return ""
        if level == "code":
            return f"{start_date}:{end_date}:{code}:code"
        if level == "media" and media_key:
            return f"{start_date}:{end_date}:{code}:media:{media_key}"
        if level == "ad" and media_key:
            return f"{start_date}:{end_date}:{code}:ad:{media_key}"
        return ""

    @staticmethod
    def _period_key_from_visible_sheet_row(row: list[Any]) -> tuple[str, str] | None:
        start_date = str(row[0] if len(row) > 0 else "").strip()
        end_date = str(row[1] if len(row) > 1 else "").strip()
        if not start_date or not end_date:
            return None
        return start_date, end_date

    @staticmethod
    def _background_color_for_period_index(period_index: int) -> dict[str, float]:
        if period_index % 2 == 1:
            return {"red": 252 / 255, "green": 229 / 255, "blue": 205 / 255}
        return {"red": 1.0, "green": 1.0, "blue": 1.0}

    def _build_weekly_period_format_requests(
        self,
        period_rows: list[tuple[int, tuple[str, str]]],
    ) -> list[dict[str, Any]]:
        period_order: dict[tuple[str, str], int] = {}
        row_colors: list[tuple[int, int]] = []
        for row_number, period_key in period_rows:
            if row_number < 2:
                continue
            if period_key not in period_order:
                period_order[period_key] = len(period_order)
            row_colors.append((row_number, period_order[period_key]))

        if not row_colors:
            return []

        requests_payload: list[dict[str, Any]] = []
        range_start = row_colors[0][0]
        previous_row = row_colors[0][0]
        previous_color_index = row_colors[0][1]

        for row_number, color_index in row_colors[1:]:
            if row_number == previous_row + 1 and color_index == previous_color_index:
                previous_row = row_number
                continue
            requests_payload.append(
                self._sheet_background_request(
                    start_row=range_start,
                    end_row=previous_row,
                    color=self._background_color_for_period_index(previous_color_index),
                )
            )
            range_start = previous_row = row_number
            previous_color_index = color_index

        requests_payload.append(
            self._sheet_background_request(
                start_row=range_start,
                end_row=previous_row,
                color=self._background_color_for_period_index(previous_color_index),
            )
        )
        return requests_payload

    def _sheet_background_request(
        self,
        *,
        start_row: int,
        end_row: int,
        color: dict[str, float],
    ) -> dict[str, Any]:
        return {
            "repeatCell": {
                "range": {
                    "sheetId": int(self.settings.media_analytics_sheet_gid),
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(MEDIA_PERFORMANCE_SHEET_HEADERS),
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }

    @staticmethod
    def _contiguous_row_ranges(row_numbers: list[int]) -> list[tuple[int, int]]:
        if not row_numbers:
            return []
        ranges: list[tuple[int, int]] = []
        start = previous = row_numbers[0]
        for row_number in row_numbers[1:]:
            if row_number == previous + 1:
                previous = row_number
                continue
            ranges.append((start, previous))
            start = previous = row_number
        ranges.append((start, previous))
        return ranges

    def _delete_sheet_columns(
        self,
        *,
        spreadsheet_id: str,
        headers: dict[str, str],
        start_index: int,
        end_index: int,
    ) -> None:
        if end_index <= start_index:
            return
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": int(self.settings.media_analytics_sheet_gid),
                                "dimension": "COLUMNS",
                                "startIndex": start_index,
                                "endIndex": end_index,
                            }
                        }
                    }
                ]
            },
        )

    def _insert_sheet_columns(
        self,
        *,
        spreadsheet_id: str,
        headers: dict[str, str],
        start_index: int,
        end_index: int,
    ) -> None:
        if end_index <= start_index:
            return
        self._sheet_request_json(
            "POST",
            f"{_SHEETS_API_BASE}/{spreadsheet_id}:batchUpdate",
            headers=headers,
            data={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": int(self.settings.media_analytics_sheet_gid),
                                "dimension": "COLUMNS",
                                "startIndex": start_index,
                                "endIndex": end_index,
                            },
                            "inheritFromBefore": True,
                        }
                    }
                ]
            },
        )

    def _delete_trailing_sheet_columns(
        self,
        *,
        spreadsheet_id: str,
        headers: dict[str, str],
        start_index: int,
        end_index: int,
    ) -> None:
        self._delete_sheet_columns(
            spreadsheet_id=spreadsheet_id,
            headers=headers,
            start_index=start_index,
            end_index=end_index,
        )

    @staticmethod
    def _sheet_column_label(index: int) -> str:
        if index <= 0:
            return "A"
        value = int(index)
        label = ""
        while value > 0:
            value, remainder = divmod(value - 1, 26)
            label = chr(ord("A") + remainder) + label
        return label

    @staticmethod
    def _escape_sheet_title(title: str) -> str:
        return str(title).replace("'", "''")

    def _status_text(self, report: dict[str, Any]) -> str:
        if report.get("ok"):
            return "OK"
        return "LỖI"

    def _resolve_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("Asia/Ho_Chi_Minh")

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    @staticmethod
    def _first_text(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            if value is None or value == "":
                return 0
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            if value is None or value == "":
                return 0.0
            number = float(str(value).replace(",", "").strip())
            if math.isnan(number) or math.isinf(number):
                return 0.0
            return number
        except (TypeError, ValueError):
            return 0.0

    def _to_int_money(self, value: Any) -> int:
        return int(round(self._to_float(value)))

    @staticmethod
    def _fmt_vnd(value: int) -> str:
        return f"{max(0, int(value)):,}đ"

    @staticmethod
    def _fmt_percent(value: Any) -> str:
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return "0.00%"

    @staticmethod
    def _fmt_ratio(value: Any) -> str:
        try:
            if value is None:
                return "N/A"
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "N/A"

    @staticmethod
    def _format_date(value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return value

    @staticmethod
    def _short(value: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _short_text(raw: str, limit: int = 360) -> str:
        normalized = " ".join(str(raw).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."
