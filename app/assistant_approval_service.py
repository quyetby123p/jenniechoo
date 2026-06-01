from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass(frozen=True)
class AssistantCallbackAction:
    action: str
    value: str


class AssistantApprovalService:
    CONFIRM_PREFIX = "asst_ok:"
    CANCEL_PREFIX = "asst_no:"

    def action_confirm_keyboard(self, request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Xác nhận chạy",
                        callback_data=f"{self.CONFIRM_PREFIX}{request_id}",
                    ),
                    InlineKeyboardButton(
                        text="Hủy",
                        callback_data=f"{self.CANCEL_PREFIX}{request_id}",
                    ),
                ]
            ]
        )

    def parse_callback(self, callback_data: str | None) -> AssistantCallbackAction | None:
        if not callback_data:
            return None
        if callback_data.startswith(self.CONFIRM_PREFIX):
            return AssistantCallbackAction(
                action="confirm_action",
                value=callback_data[len(self.CONFIRM_PREFIX) :],
            )
        if callback_data.startswith(self.CANCEL_PREFIX):
            return AssistantCallbackAction(
                action="cancel_action",
                value=callback_data[len(self.CANCEL_PREFIX) :],
            )
        return None
