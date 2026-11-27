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

## Where to get each credential

On the current machine, the existing values are stored in the local project
file below. Read the values locally and enter them into the target machine;
do not paste the file into chat or commit it:

`D:\clawagent-main\clawagent-main\codex\projects\fb-ads-automation\.env`

| Variables | Source | Required for web report |
| --- | --- | --- |
| `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `META_PAGE_ACCESS_TOKEN`, `META_PAGE_ID` | JC Meta Business account / Ads Manager. The current values are in the local `.env`; create a new Meta system-user or long-lived token if the old token is expired. | Yes for JC Ads cost |
| `ADS2_META_ACCESS_TOKEN`, `ADS2_META_AD_ACCOUNT_ID`, `ADS2_META_PAGE_ACCESS_TOKEN`, `ADS2_META_PAGE_ID` | Ruza Thailand Meta Ads account (ADS2), not Vayxa. The current values are in the local `.env`. | Yes for Ruza Ads cost |
| `PANCAKE_ACCESS_TOKEN`, `PANCAKE_API_KEY` | Pancake POS account for JC. Use the API/access-token area in Pancake POS; only the credential type used by the current integration is needed. | Yes for JC orders/revenue |
| `ADS2_PANCAKE_ACCESS_TOKEN`, `ADS2_PANCAKE_API_KEY` | Pancake POS shop Ruza Thailand, shop ID `408116196`. Do not use Vayxa credentials. | Yes for Ruza orders/revenue |
| `THAI_DUONG_API_TOKEN`, `THAI_DUONG_AUTH_REFRESH_TOKEN` | Thai Duong COD API/auth flow. Current values are in the local `.env`; login fallback uses the `THAI_DUONG_AUTH_*` fields. | Yes for JC reconciliation/P&L |
| `RECONCILE_COD_SHEET_OAUTH_CLIENT_ID`, `RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET`, `RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN` | Google Cloud OAuth client and the refresh token authorized for the reconciliation spreadsheet. Current values are in the local `.env`. | Yes for JC P&L fee columns H/J |
| `WEB_REPORT_API_TOKEN` | Generate a new read-only random token locally with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Put the result in `.env`; give only the Bearer token to the third-party consumer. | Only for API export |
| `RENDER_API_KEY` | Render Dashboard -> Account Settings -> API Keys. This is only for deployment/management, not for dashboard runtime. Create a new key on the target machine. | No |

The fixed non-secret identifiers are already filled in: JC Pancake shop
`407330767`, Ruza Thailand Pancake shop `408116196`, and THB/VND rate `750`.
The Telegram bot token is not required by the web-only entry point.

### Local credential checks

To list only credential variable names from the current local `.env` without
printing their values:

```powershell
$envFile = 'D:\clawagent-main\clawagent-main\codex\projects\fb-ads-automation\.env'
Get-Content $envFile | Where-Object { $_ -match '^\\s*[A-Z0-9_]+\\s*=' } | ForEach-Object {
  if ($_ -match '^\\s*([A-Z0-9_]+)\\s*=') { $Matches[1] }
} | Sort-Object -Unique
```

For the report API token specifically, the current local token file is
`storage/web_report_api_token.txt`. Treat it as a secret and generate a new
one before sharing access with a third party.

## Render deploy

Use Render Secret Files or environment variables. Do not put a real Render
API key in this folder. A Render key previously pasted into chat should be
revoked and replaced before sharing this package.
