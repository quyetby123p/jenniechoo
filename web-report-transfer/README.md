# Web report transfer package

This folder is a safe configuration package for the JC/Ruza web report.

## Why there are placeholders

Live Meta, Pancake, Thai Duong, Google OAuth, Render, and report API tokens
are credentials. They are not included in a transferable file. The target
machine must receive them through a secret manager or be filled locally by
the owner. Do not commit `.env` or send it in chat.

## Setup on another machine

1. Copy `.env.example` to the project root as `.env`.
2. Replace every `<...>` value with credentials created for that machine.
3. Keep `PANCAKE_SHOP_ID=407330767` for JC and `ADS2_PANCAKE_SHOP_ID=408116196` for Ruza Thailand.
4. Keep `REPORT_THB_TO_VND_RATE=750` unless the accounting rate changes.
5. Run `powershell -ExecutionPolicy Bypass -File web-report-transfer/check-env.ps1 -EnvPath .env`.
6. Start only the web report with `gunicorn app.web_report_main:app --bind 0.0.0.0:$env:PORT --workers 2 --threads 4 --timeout 120`.

The web-only entry point does not require the Telegram bot token. The report
export API is read-only and requires `Authorization: Bearer <WEB_REPORT_API_TOKEN>`.

## Render deploy

Use Render Secret Files or environment variables. Do not put a real Render
API key in this folder. A Render key previously pasted into chat should be
revoked and replaced before sharing this package.
