from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass
class CallbackAction:
    action: str
    value: str
    index: int | None = None


class ApprovalService:
    DUPLICATE_CONFIRM_PREFIX = "dup_ok:"
    DUPLICATE_CANCEL_PREFIX = "dup_no:"
    APPROVE_PREFIX = "ap_ok:"
    REJECT_PREFIX = "ap_no:"
    CAMPAIGN_PICK_PREFIX = "camp_pick:"
    CAMPAIGN_CANCEL_PREFIX = "camp_cancel:"
    RECONCILE_APPLY_PREFIX = "rc_apply:"
    RECONCILE_CANCEL_PREFIX = "rc_cancel:"
    RECONCILE_SHEET_APPLY_PREFIX = "rc_sheet_apply:"
    RECONCILE_SHEET_CANCEL_PREFIX = "rc_sheet_cancel:"

    def duplicate_keyboard(self, request_id: str, version: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Tạo v{version}",
                        callback_data=f"{self.DUPLICATE_CONFIRM_PREFIX}{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.DUPLICATE_CANCEL_PREFIX}{request_id}",
                    ),
                ]
            ]
        )

    def review_keyboard(self, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Duyệt",
                        callback_data=f"{self.APPROVE_PREFIX}{job_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.REJECT_PREFIX}{job_id}",
                    ),
                ]
            ]
        )

    def existing_campaign_select_keyboard(self, request_id: str, campaign_options: list[str]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for index, option in enumerate(campaign_options):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=option,
                        callback_data=f"{self.CAMPAIGN_PICK_PREFIX}{request_id}:{index}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Hủy",
                    callback_data=f"{self.CAMPAIGN_CANCEL_PREFIX}{request_id}",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def reconcile_update_keyboard(self, request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Duyệt cập nhật COD",
                        callback_data=f"{self.RECONCILE_APPLY_PREFIX}{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.RECONCILE_CANCEL_PREFIX}{request_id}",
                    ),
                ]
            ]
        )

    def reconcile_sheet_sync_keyboard(self, request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Duyệt ghi Google Sheet",
                        callback_data=f"{self.RECONCILE_SHEET_APPLY_PREFIX}{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.RECONCILE_SHEET_CANCEL_PREFIX}{request_id}",
                    ),
                ]
            ]
        )

    def parse_callback(self, callback_data: str | None) -> CallbackAction | None:
        if not callback_data:
            return None

        if callback_data.startswith(self.CAMPAIGN_PICK_PREFIX):
            payload = callback_data[len(self.CAMPAIGN_PICK_PREFIX) :].strip()
            if ":" not in payload:
                return None
            request_id, raw_index = payload.rsplit(":", 1)
            if not request_id or not raw_index.isdigit():
                return None
            return CallbackAction(action="campaign_pick", value=request_id, index=int(raw_index))

        if callback_data.startswith(self.CAMPAIGN_CANCEL_PREFIX):
            return CallbackAction(
                action="campaign_cancel",
                value=callback_data[len(self.CAMPAIGN_CANCEL_PREFIX) :],
            )

        if callback_data.startswith(self.RECONCILE_APPLY_PREFIX):
            return CallbackAction(
                action="reconcile_apply",
                value=callback_data[len(self.RECONCILE_APPLY_PREFIX) :],
            )

        if callback_data.startswith(self.RECONCILE_CANCEL_PREFIX):
            return CallbackAction(
                action="reconcile_cancel",
                value=callback_data[len(self.RECONCILE_CANCEL_PREFIX) :],
            )

        if callback_data.startswith(self.RECONCILE_SHEET_APPLY_PREFIX):
            return CallbackAction(
                action="reconcile_sheet_apply",
                value=callback_data[len(self.RECONCILE_SHEET_APPLY_PREFIX) :],
            )

        if callback_data.startswith(self.RECONCILE_SHEET_CANCEL_PREFIX):
            return CallbackAction(
                action="reconcile_sheet_cancel",
                value=callback_data[len(self.RECONCILE_SHEET_CANCEL_PREFIX) :],
            )

        for prefix, action_name in (
            (self.DUPLICATE_CONFIRM_PREFIX, "duplicate_confirm"),
            (self.DUPLICATE_CANCEL_PREFIX, "duplicate_cancel"),
            (self.APPROVE_PREFIX, "approve"),
            (self.REJECT_PREFIX, "reject"),
        ):
            if callback_data.startswith(prefix):
                return CallbackAction(action=action_name, value=callback_data[len(prefix):])
        return None
