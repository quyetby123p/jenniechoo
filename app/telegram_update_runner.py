from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

from aiogram import Dispatcher
from aiogram.types import Update

from app.scheduled_tasks import build_runtime


def _read_text_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _decode_update_payload(args: argparse.Namespace) -> dict[str, Any]:
    sources = [
        bool(str(args.update_json or "").strip()),
        bool(str(args.update_json_file or "").strip()),
        bool(str(args.update_b64 or "").strip()),
        bool(str(args.update_b64_file or "").strip()),
    ]
    if sum(1 for item in sources if item) != 1:
        raise ValueError(
            "Provide exactly one of --update-json, --update-json-file, --update-b64, or --update-b64-file."
        )

    if args.update_json:
        raw = str(args.update_json).strip()
    elif args.update_json_file:
        raw = _read_text_file(str(args.update_json_file))
    elif args.update_b64:
        raw = base64.b64decode(str(args.update_b64).strip()).decode("utf-8")
    else:
        raw = base64.b64decode(_read_text_file(str(args.update_b64_file))).decode("utf-8")

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Telegram update payload must be a JSON object.")
    if "update_id" not in payload:
        raise ValueError("Telegram update payload is missing update_id.")
    return payload


async def run_update(update_payload: dict[str, Any], *, validate_only: bool = False) -> int:
    if validate_only:
        Update.model_validate(update_payload)
        print(f"Telegram update payload valid: {update_payload.get('update_id')}")
        return 0

    runtime = build_runtime()
    dispatcher = Dispatcher()
    dispatcher.include_router(runtime.bot.router)
    try:
        try:
            me = await runtime.telegram.get_me()
            runtime.bot._bot_username = str(getattr(me, "username", "") or "").strip().lstrip("@").lower()
        except Exception:  # noqa: BLE001
            runtime.bot.logger.warning("Khong lay duoc username bot khi xu ly webhook update.")

        update = Update.model_validate(update_payload, context={"bot": runtime.telegram})
        await dispatcher.feed_update(runtime.telegram, update)
        print(f"Telegram update processed: {update_payload.get('update_id')}")
        return 0
    finally:
        await runtime.telegram.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process one Telegram webhook update.")
    parser.add_argument("--update-json", default="", help="Raw Telegram update JSON.")
    parser.add_argument("--update-json-file", default="", help="Path to Telegram update JSON.")
    parser.add_argument("--update-b64", default="", help="Base64-encoded Telegram update JSON.")
    parser.add_argument("--update-b64-file", default="", help="Path to base64-encoded Telegram update JSON.")
    parser.add_argument("--validate-only", action="store_true", help="Only validate update payload shape.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        payload = _decode_update_payload(args)
        return asyncio.run(run_update(payload, validate_only=bool(args.validate_only)))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram update failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
