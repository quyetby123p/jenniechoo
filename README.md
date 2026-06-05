# FB Ads Automation (Telegram -> Meta Ads)

Tool local tren Windows de:
1. Nhan link bai post Facebook qua Telegram.
2. Tao campaign nhap theo bo cuc 1-3-3 (1 campaign, 3 ad set, 3 ad).
3. Hoac len ads vao campaign ACTIVE co san theo SKU (`len cu`).
4. Duyet/Huy bang nut bam Telegram.
5. Publish chi khi duoc duyet.
6. Tu dong kiem tra token moi sang va gui bao cao qua Telegram.
7. Gui bao cao kinh doanh moi sang (POS Pancake + chi phi Ads + top san pham).

## Quy tac nghiep vu da khoa

1. Muc tieu campaign: `OUTCOME_ENGAGEMENT` (tool tu map `ENGAGEMENT` -> `OUTCOME_ENGAGEMENT` neu cau hinh cu).
2. Vi tri chuyen doi: `MESSAGING_DESTINATION`.
3. Muc tieu ket qua: toi da hoa mua qua tin nhan.
4. Message template mac dinh: `Chao JC`.
5. CBO: ngan sach cap campaign (VND/ngay).
6. Trung link: canh bao, xac nhan, tao version moi (v2, v3...).

## Cau truc thu muc chinh

- `app/`: source code bot Telegram + Meta API.
- `config/`: objective, audiences, message templates.
- `scripts/`: script ho tro backup/artifact/run + service.
- `logs/`: log runtime.
- `state/`: lock/pending requests.
- `storage/`: du lieu job va artifact.

## Cai dat

1. Cai Python 3.10+.
2. Tao virtualenv, cai dependencies:

```powershell
cd .\projects\fb-ads-automation
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Tao `.env` tu `.env.example`, dien:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_ID`
- `META_ACCESS_TOKEN`
- `META_PAGE_ACCESS_TOKEN` (khuyen nghi bat buoc neu link dang `.../posts/pfbid...`)
- `META_AD_ACCOUNT_ID`
- `META_PAGE_ID`
- `TOKEN_HEALTHCHECK_ENABLED` (mac dinh `1`)
- `TOKEN_HEALTHCHECK_HOUR` (mac dinh `9`)
- `TOKEN_HEALTHCHECK_MINUTE` (mac dinh `0`)
- `TOKEN_HEALTHCHECK_STARTUP_ALERT_ONLY_ON_FAILURE` (mac dinh `1`)
- `DAILY_REPORT_ENABLED` (mac dinh `1`)
- `DAILY_REPORT_HOUR` (mac dinh `8`, moc bao cao buoi sang cho ngay hom truoc)
- `DAILY_REPORT_MINUTE` (mac dinh `0`)
- `DAILY_REPORT_HISTORY_DAYS` (mac dinh `90`)
- `DAILY_REPORT_STARTUP_ALERT_ONLY_ON_FAILURE` (giu tuong thich env cu, hien khong gui bao cao ngay luc khoi dong bot)
- `DAILY_REPORT_NOTIFY_CHAT_ID` (chat id nhan **them** daily report; de trong hoac `0` thi chi gui ve `TELEGRAM_ALLOWED_USER_ID`, group/supergroup thuong la so am `-100...`)
- `DAILY_REPORT_TASK_SUMMARY_ENABLED` (mac dinh `1`, noi them tong ket task cong viec vao bao cao tu dong buoi toi)
- `DAILY_REPORT_TASK_SUMMARY_MAX_ITEMS` (mac dinh `5`, so task toi da hien trong moi khoi danh sach)
- `DAILY_REPORT_TASK_DB_PATH` (mac dinh `storage/assistant_bot/tasks.db`, dung chung DB task cua Bot 3)
- `PANCAKE_API_BASE_URL` (mac dinh `https://pos.pancake.vn/api/v1`)
- `PANCAKE_ACCESS_TOKEN` (neu anh dang duoc Pancake cap theo dang access token)
- `PANCAKE_API_KEY` (neu anh dang dung API key trong CRM > Cau hinh ung dung > Webhook va API Key)
- `PANCAKE_SHOP_ID` (id cua shop Pancake POS)
- `PANCAKE_PAGE_SIZE` (mac dinh `200`)
- `REPORT_THB_TO_VND_RATE` (ty gia quy doi THB -> VND trong bao cao, mac dinh `810`)
- `REPORT_THB_MINOR_UNIT_FACTOR` (he so don vi tien THB tu API -> THB hien thi, mac dinh `100`)
- `RECONCILE_COD_ENABLED` (bat/tat doi soat COD, mac dinh `0`)
- `RECONCILE_COD_AUTO_ENABLED` (bat/tat lich doi soat COD tu dong, mac dinh `0`)
- `RECONCILE_COD_HOUR` (gio chay tu dong, mac dinh `15`)
- `RECONCILE_COD_MINUTE` (phut chay tu dong, mac dinh `0`)
- `RECONCILE_COD_AUTO_WEEKDAYS` (danh sach thu chay bao cao tien ve, format `0-6` voi `0=T2`, `6=CN`, mac dinh `0,4` = T2,T6)
- `RECONCILE_COD_WEEKLY_SUMMARY_ENABLED` (bat/tat bao cao tong tien nhan theo tuan, mac dinh `1`)
- `RECONCILE_COD_WEEKLY_SUMMARY_WEEKDAY` (thu gui tong tien nhan tuan, `0=T2` ... `6=CN`, mac dinh `5` = T7)
- `RECONCILE_COD_NOTIFY_CHAT_ID` (chat id nhan bao cao doi soat COD tu dong; de trong hoac `0` thi fallback ve `DAILY_REPORT_NOTIFY_CHAT_ID`, neu van trong thi ve `TELEGRAM_ALLOWED_USER_ID`)
- `RECONCILE_COD_BATCH_LIMIT` (gioi han cap nhat moi batch, mac dinh `100`)
- `RECONCILE_COD_UPDATE_ENABLED` (cho phep cap nhat status Pancake, mac dinh `0`)
- `RECONCILE_COD_STATUS_MAP_PATH` (duong dan file map trang thai)
- `RECONCILE_COD_PANCAKE_LOOKBACK_DAYS` (so ngay lookback khi tim don Pancake, mac dinh `3650`)
- `RECONCILE_COD_SHEET_ENABLED` (bat/tat dong bo ket qua doi soat COD len Google Sheet, mac dinh `0`)
- `RECONCILE_COD_SHEET_MODE` (`apps_script`, `oauth_user` hoac `service_account`, mac dinh `apps_script`)
- `RECONCILE_COD_SHEET_WEBHOOK_URL` (URL Web App cua Google Apps Script khi dung mode `apps_script`)
- `RECONCILE_COD_SHEET_WEBHOOK_SECRET` (secret de xac thuc request webhook, khuyen nghi bat buoc)
- `RECONCILE_COD_SHEET_WEBHOOK_TIMEOUT_SECONDS` (timeout goi webhook, mac dinh `30`)
- `RECONCILE_COD_SHEET_SPREADSHEET_ID` (id file Google Sheet)
- `RECONCILE_COD_SHEET_GID` (gid tab can ghi, mac dinh `1034910254`)
- `RECONCILE_COD_SHEET_CREDENTIALS_PATH` (chi dung khi mode `service_account`)
- `RECONCILE_COD_SHEET_OAUTH_CLIENT_ID` (chi dung khi mode `oauth_user`)
- `RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET` (chi dung khi mode `oauth_user`)
- `RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN` (chi dung khi mode `oauth_user`)
- `RECONCILE_COD_SHEET_OAUTH_TOKEN_URI` (token endpoint OAuth, mac dinh `https://oauth2.googleapis.com/token`)
- `PANCAKE_TD_SYNC_ENABLED` (bat/tat dong bo tao don Pancake -> Thai Duong, mac dinh `0`)
- `PANCAKE_TD_SYNC_POLL_SECONDS` (chu ky poll don moi, mac dinh `30`)
- `PANCAKE_TD_SYNC_BATCH_LIMIT` (so don toi da xu ly moi lan poll, mac dinh `50`)
- `PANCAKE_TD_SYNC_NOTIFY_CHAT_ID` (chat id nhan thong bao tong ket; de trong thi dung `TELEGRAM_ALLOWED_USER_ID`)
- `PANCAKE_TD_SYNC_PRODUCT_REFRESH_MINUTES` (chu ky refresh danh muc san pham Thai Duong, mac dinh `30`)
- `PANCAKE_TD_SYNC_STATE_PATH` (duong dan file state cursor/idempotency local)
- `THAI_DUONG_API_BASE_URL` (neu dung API Thai Duong)
- `THAI_DUONG_API_TOKEN` (neu dung API Thai Duong)
- `THAI_DUONG_AUTO_AUTH_ENABLED` (bat/tat tu gia han token Thai Duong, mac dinh `0`)
- `THAI_DUONG_AUTO_REFRESH_THRESHOLD_MINUTES` (moc phut truoc han de bot tu renew, mac dinh `120`)
- `THAI_DUONG_AUTO_AUTH_MIN_RETRY_SECONDS` (khoang cach toi thieu giua 2 lan thu renew, mac dinh `120`)
- `THAI_DUONG_AUTH_LOGIN_PATH` (endpoint login, mac dinh `/api/v1/auth/login`)
- `THAI_DUONG_AUTH_REFRESH_PATH` (endpoint refresh, mac dinh `/api/v1/auth/refresh`)
- `THAI_DUONG_AUTH_EMAIL` (tai khoan dang nhap Thai Duong de fallback khi refresh that bai)
- `THAI_DUONG_AUTH_PASSWORD` (mat khau dang nhap Thai Duong)
- `THAI_DUONG_AUTH_USERNAME` (tuy chon, neu API login can userName)
- `THAI_DUONG_CUSTOMER_CODE` (tuy chon, ep ma KH khi tao don Thai Duong; neu bo trong bot thu tu lay tu username/token)
- `THAI_DUONG_AUTH_REFRESH_TOKEN` (tuy chon, bot tu cap nhat lai neu API tra ve token moi)
- `THAI_DUONG_AUTO_AUTH_STATE_PATH` (noi luu token/auth state local de khoi phuc sau restart)

4. Dien `config/audiences.json` bang 3 saved audience id that.

## Kiem tra config

```powershell
python -m app.main --check-config
```

## Chay bot

```powershell
python -m app.main
```

## Web report don hang (V1)

Web report dung cho dashboard team, refresh cache moi `10` phut va lay du lieu tu Pancake + run doi soat COD moi nhat theo ngay.

### Chay local

```powershell
python -m app.web_report_main
```

Mac dinh web chay `http://127.0.0.1:8000`.

### Route chinh

- `/` dashboard tong quan.
- `/brand/<brand_slug>` chi tiet thieu hang theo brand/size.
- `/status/waiting` danh sach don cho hang.
- `/status/pending-reconcile` danh sach don cho doi soat.
- `/api/v1/snapshot?date=YYYY-MM-DD` tra JSON snapshot.
- `/healthz` healthcheck deploy.

### Bien moi truong web report

- `WEB_REPORT_HOST` (mac dinh `0.0.0.0`)
- `WEB_REPORT_PORT` (mac dinh `8000`)
- `WEB_REPORT_REFRESH_SECONDS` (mac dinh `600`)
- `WEB_REPORT_STATUS_MAP_PATH` (mac dinh `config/web_report_status_map.json`)

File map status/brand: `config/web_report_status_map.json`.

### Deploy free: Render web + GitHub Actions

Phuong an free:
- Render free chi chay `fb-ops-web-report` (web report).
- Cloudflare Worker chay webhook Telegram va cron dung gio.
- GitHub Actions chi la executor Python duoc Cloudflare goi bang `workflow_dispatch`.
- Telegram polling realtime (`len camp` ngay khi nhan tin) khong can may local neu da bat Cloudflare Worker webhook, nhung se cham hon worker/VM tra phi vi phai doi GitHub Actions khoi dong job.

1. Kiem tra nhanh truoc khi push:

```powershell
.\scripts\deploy\render-preflight.ps1
```

2. Push GitHub bang 1 lenh (tu init git -> commit -> set remote -> push):

```powershell
.\scripts\deploy\bootstrap-github-render.ps1 -GitHubRepoUrl "https://github.com/<org-or-user>/<repo>.git"
```

3. Tao/cap nhat Render web report tu dong bang API:

```powershell
.\scripts\deploy\render-create-service.ps1 -RenderApiKey "<RENDER_API_KEY>" -RepoUrl "https://github.com/<org-or-user>/<repo>"
```

4. Dong bo `.env` len GitHub Actions Secrets de job dinh ky chay online:

```powershell
.\scripts\deploy\github-actions-secrets.ps1 -Repo "quyetby123p/jenniechoo"
```

Script tren can GitHub CLI (`gh`) va `gh auth login` truoc khi chay.

5. Cloudflare Worker cron tu dispatch GitHub Actions theo lich:
- Local bot la scheduler chinh neu may dang bat. Khi local chay thanh cong, local goi Worker `/schedule/mark` de danh dau slot da xong.
- Cloudflare Worker la backup +5 phut: neu khong thay marker local thi moi dispatch GitHub Actions.
- Moi 30 phut: local sync truoc; cloud backup tai phut `:05/:35` cho batch `Pancake -> Thai Duong`.
- 08:00 (Asia/Ho_Chi_Minh): local gui daily report ngay hom qua; cloud backup luc 08:05.
- 09:00: local kiem tra token Meta/Thai Duong va Bot 3 hoi task trong ngay; cloud backup luc 09:05.
- 17:00: Bot 3 hoi tien do task trong ngay; cloud backup luc 17:05.
- 15:00 thu 2 va thu 6: local gui bao cao tien ve Thai Duong neu ngay do co ky doi soat that; cloud backup luc 15:05.
- 15:00 thu 7: local gui tong ket tien ve theo tuan; cloud backup luc 15:05.
- 21:00: local gui daily report ngay hom nay; cloud backup luc 21:05.
- Workflow co cache `state/` + storage runtime lien quan de giu cursor/lich su giua cac lan chay.
- GitHub native `on.schedule` da tat de tranh delayed run cua GitHub gui bao cao sai khung gio.
- Guard marker duoc luu vao GitHub Actions repository variable `LOCAL_SCHEDULE_MARKS`; neu Worker khong doc/ghi duoc variable thi fail-open de cloud van chay backup.

Co the chay thu cong tren GitHub:
- Vao `Actions` -> `free scheduled tasks` -> `Run workflow`.
- Chon task: `token-health`, `daily-report`, `reconcile-cash-in`, `reconcile-weekly`, `pancake-td-sync`, `bot3-daily-checkin`.

### Telegram online mien phi khi tat may

Phuong an free on dinh nhat hien tai:
- Cloudflare Worker nhan webhook Telegram.
- Worker goi GitHub Actions task `telegram-update`.
- GitHub Actions chay code Python trong repo de xu ly lenh va gui lai Telegram.

Tradeoff:
- Mien phi va khong can may local bat.
- Cham hon bot polling/worker tra phi, thuong can cho GitHub Actions khoi dong job.
- Phu hop cho lenh len camp, report, doi soat, nut duyet/callback; khong phu hop neu can chat realtime toc do cao.

Deploy Cloudflare Worker:

```powershell
.\scripts\deploy\cloudflare-telegram-webhook.ps1
```

Yeu cau:
- Node.js co `npx`.
- Da login Cloudflare Wrangler (`npx wrangler login`).
- `.env` local da co Telegram/Meta/Pancake keys.
- Git credential local co quyen repo GitHub.

Sau khi deploy, script se:
- set Cloudflare Worker secrets;
- deploy worker `fb-ads-telegram-dispatcher`;
- set Telegram webhook den `/telegram/webhook`;
- neu co `BOT3_TELEGRAM_TOKEN`, set them Bot 3 webhook den `/telegram/webhook/bot3`;
- luu `CLOUD_SCHEDULE_GUARD_MARK_URL` va `CLOUD_SCHEDULE_GUARD_SECRET` vao `.env` local;
- worker se dispatch update Telegram va cron schedule sang GitHub Actions `free-scheduled-tasks.yml`.

Tuy chon:
- Muon bo qua test nhanh khi push: them `-SkipTests`.
- Muon push branch rieng: them `-Branch feature/web-report-v1`.
- Muon chi init/commit/remote, chua push ngay: them `-NoPush`.
- Neu may chua config Git user: them `-GitUserName "Ten cua anh" -GitUserEmail "email@domain.com"`.

Ghi chu:
- `render-sync-stack.ps1` van ton tai cho phuong an Render worker tra phi, nhung khong dung trong phuong an free.
- Cloudflare Worker webhook/cron giu phan online mien phi khi may local tat.
- GitHub Actions van co do tre khoi dong job, nhung khong con dung native GitHub schedule cho cac moc gio nghiep vu.

## Cu phap Telegram

Khuyen nghi (don gian nhat):

```text
<link_facebook_post> ngan sach 300000 len moi
```

Mode len campaign cu:

```text
<link_facebook_post> [SKU...] len cu
<link_facebook_post> len cu camp video
```

Vi du:

```text
https://www.facebook.com/yourpage/posts/123456 JCV140 len cu
```

Quy tac mode `len cu`:
- Neu co `camp <hint>` o cuoi lenh (vi du `camp video`): bot tim campaign `ACTIVE` theo keyword trong hint.
- Neu len theo `camp <hint>`: ten ad se dong co dinh `SKU:ALL`.
- Neu khong co `camp <hint>`: bot tim campaign `ACTIVE` chua du tat ca SKU.
- SKU uu tien lay tu cau lenh (vi du `JCV140`); neu khong co se lay hashtag `#JC...` trong noi dung post.
- Khong bat buoc nhap ngan sach vi campaign cu da co budget san.
- Neu khop nhieu campaign: bot gui danh sach de bam chon.
- Bot chi len ads vao adset `ACTIVE/PAUSED` san co cua campaign da chon.
- Gioi han an toan: toi da `20` adset moi lan chay.
- Khi bam `Duyet`, bot chi bat `ACTIVE` cho ads moi tao (khong doi status campaign/adset cu).

Mode len campaign moi:

```text
<link_facebook_post> ngan sach 300000 len moi
```

Hoac /ads:

```text
/ads <facebook_post_url> budget=<so_tien_vnd> len moi
```

Vi du:

```text
/ads https://www.facebook.com/yourpage/posts/123456 budget=300000 len moi
```

Kiem tra token thu cong:

```text
/token
```

Chay doi soat COD theo ngay doi soat Thai Duong:

```text
/reconcile cod
```

```text
/reconcile cod 2026-05-09
```

Lenh tu nhien:

```text
doi soat cod
```

```text
doi soat cod hom qua
```

### Lich bao cao dong tien Thai Duong (tu dong)

- Bot chay theo moc `RECONCILE_COD_HOUR:RECONCILE_COD_MINUTE` trong `APP_TIMEZONE`.
- Slot bao cao tien ve:
  - Chay theo `RECONCILE_COD_AUTO_WEEKDAYS`.
  - Moi lan chay se lay ky doi soat mac dinh moi nhat va gui tong tien ve THB/VND (ban ngan gon).
- Slot tong tuan:
  - Bat bang `RECONCILE_COD_WEEKLY_SUMMARY_ENABLED=1`.
  - Gui vao thu `RECONCILE_COD_WEEKLY_SUMMARY_WEEKDAY`.
  - Tong hop khung T2-T6 cua tuan hien tai.
- Chat nhan:
  - Uu tien `RECONCILE_COD_NOTIFY_CHAT_ID`.
  - Neu trong/0 -> fallback `DAILY_REPORT_NOTIFY_CHAT_ID`.
  - Neu van trong/0 -> fallback `TELEGRAM_ALLOWED_USER_ID`.

## Dong bo Google Sheet cho doi soat COD (mode OAuth user - khong can key service account)

Neu org cua anh chan tao service account key, dung mode `oauth_user`:

1. Tao OAuth Client ID (type `Desktop app`) trong `Google Cloud` -> `APIs & Services` -> `Credentials`.
2. Lay `Client ID` + `Client Secret`.
3. Chay script lay `refresh_token`:

```powershell
.\.venv\Scripts\python.exe .\scripts\google_oauth_sheet_token.py --client-id "<CLIENT_ID>" --client-secret "<CLIENT_SECRET>"
```

4. Dien `.env`:

```text
RECONCILE_COD_SHEET_ENABLED=1
RECONCILE_COD_SHEET_MODE=oauth_user
RECONCILE_COD_SHEET_SPREADSHEET_ID=<spreadsheet_id>
RECONCILE_COD_SHEET_GID=1034910254
RECONCILE_COD_SHEET_OAUTH_CLIENT_ID=<client_id>
RECONCILE_COD_SHEET_OAUTH_CLIENT_SECRET=<client_secret>
RECONCILE_COD_SHEET_OAUTH_REFRESH_TOKEN=<refresh_token>
```

Bot se:
- Sau khi đối soát xong, tự gửi nút `Duyệt ghi Google Sheet` / `Hủy`.
- Chỉ khi bấm `Duyệt` mới ghi.
- Ghi trực tiếp qua Google Sheets API bằng OAuth của anh (không cần key JSON).

## Dong bo Google Sheet cho doi soat COD (cach don gian: Apps Script)

Mode mac dinh: `apps_script` (khong can key JSON service account).

1. Mo Google Sheet cua anh -> `Extensions` -> `Apps Script`.
2. Dan script Web App (doPost) theo mau em cung cap.
3. `Deploy` -> `New deployment` -> `Web app`:
   - Execute as: `Me`
   - Who has access: `Anyone with the link`
4. Copy URL Web App va dien `.env`:

```text
RECONCILE_COD_SHEET_ENABLED=1
RECONCILE_COD_SHEET_MODE=apps_script
RECONCILE_COD_SHEET_WEBHOOK_URL=<web_app_url>
RECONCILE_COD_SHEET_WEBHOOK_SECRET=<secret_tu_dat>
RECONCILE_COD_SHEET_GID=1034910254
```

Bot se:
- Sau khi đối soát xong, tự gửi nút `Duyệt ghi Google Sheet` / `Hủy` trên Telegram.
- Chỉ khi anh bấm `Duyệt` mới gọi webhook để ghi.
- Ghi dữ liệu vào cột `B:AK`.
- Dedupe theo khóa `Mã vận đơn (O) + Ngày đối soát (Q)` được xử lý phía Apps Script.
- Nếu webhook lỗi, bot vẫn giữ kết quả đối soát/CSV và báo lỗi ghi sheet riêng.

Mau Apps Script toi thieu (dan vao `Code.gs`):

```javascript
const WEBHOOK_SECRET = 'thay_secret_cua_anh';

function doPost(e) {
  try {
    const body = JSON.parse((e.postData && e.postData.contents) || '{}');
    const secret = String(body.secret || '');
    if (WEBHOOK_SECRET && secret !== WEBHOOK_SECRET) {
      return _json({ ok: false, error: 'Unauthorized secret' });
    }

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Sheet1');
    if (!sheet) {
      return _json({ ok: false, error: 'Sheet not found' });
    }

    const rows = Array.isArray(body.rows) ? body.rows : [];
    const lastRow = Math.max(3, sheet.getLastRow());
    const existing = sheet.getRange(3, 15, Math.max(0, lastRow - 2), 3).getValues(); // O:Q
    const seen = new Set();
    existing.forEach(r => {
      const awb = String(r[0] || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
      const d = _toYmd(r[2]);
      if (awb && d) seen.add(`${awb}|${d}`);
    });

    const valuesToAppend = [];
    let skipped = 0;
    rows.forEach(item => {
      const key = String(item.key || '');
      const values = Array.isArray(item.values) ? item.values : null;
      if (!values) return;
      if (key && seen.has(key)) { skipped += 1; return; }
      valuesToAppend.push(values);
      if (key) seen.add(key);
    });

    if (valuesToAppend.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 2, valuesToAppend.length, 36).setValues(valuesToAppend); // B:AK
    }
    return _json({
      ok: true,
      inserted: valuesToAppend.length,
      skipped_existing: skipped,
      sheet_title: sheet.getName(),
    });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function _toYmd(v) {
  if (!v) return '';
  const d = new Date(v);
  if (isNaN(d.getTime())) return '';
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
```

Chay bao cao doanh thu/chi phi:

```text
/report
```

Backfill ngay cu:

```text
/report 2026-05-15
```

Lenh tu nhien (khong can dau `/`):

```text
Báo cáo
```

```text
báo cáo ngày hôm qua
```

```text
báo cáo hôm nay
```

```text
báo cáo 15/6
```

## Dong bo tu dong don Pancake -> Thai Duong (polling)

Khi `PANCAKE_TD_SYNC_ENABLED=1`, bot chay poll don Pancake theo chu ky `PANCAKE_TD_SYNC_POLL_SECONDS` (mac dinh 30 giay) va tao don ben Thai Duong theo mode `create-only`:
- Moi `pancake_order_id` chi dong bo 1 lan (idempotency local qua state + remote qua tra cuu `orders/list`).
- Neu map SKU/mau that bai, don se bao loi 1 lan theo fingerprint du lieu don; sau do bot retry im lang theo cua so `poll.failed_order_retry_minutes` (mac dinh 5 phut) de tranh spam.
- Neu Pancake API loi lap lai (500/DNS/SSL), bot se:
  - tam backoff lay don theo `poll.pancake_fetch_error_pause_seconds` (mac dinh 300s),
  - chi gui lai thong bao loi Pancake sau `notify.pancake_fetch_error_notify_cooldown_minutes` (mac dinh 15 phut) neu loi van giong nhau.
- Sau khi tao don, bot set truong `isNeedSale=true` (Sale xac nhan).
- Ghi chu in (`pancake.print_note_sync`): bot lay `orderUID` Thai Duong va ghi vao `note_print` cua don Pancake theo che do an toan:
  - Doc full payload don hien tai tu Pancake.
  - Chi thay doi field ghi chu in (`note_print`), khong cho phep `extra_payload` sua field khac.
  - Verify lai cac field quan trong (`__items_signature__`, `total_price`, `total_quantity`, ...) sau update de canh bao neu co thay doi ngoai y muon.
- Thong bao Telegram chi gui tong ket + loi (khong spam moi poll rong).
- Neu bat `THAI_DUONG_AUTO_AUTH_ENABLED=1`, bot se tu thu gia han token Thai Duong khi token sap het han:
  - Thu `POST /api/v1/auth/refresh` truoc.
  - Neu refresh that bai, fallback `POST /api/v1/auth/login` (can `THAI_DUONG_AUTH_EMAIL` + `THAI_DUONG_AUTH_PASSWORD`).

Lenh thu cong:
- `len don hom nay` (ho tro co dau: `lên đơn hôm nay`) de quet va dong bo don trong ngay ngay lap tuc, van loc trung local + remote.
- `len don JCT310` (ho tro co dau: `lên đơn JCT310`) de dong bo mot don cu the theo ma Pancake, van loc trung local + remote.

Config chinh:
- `config/pancake_td_sync.json`: field path Pancake, endpoint Thai Duong, retry/notify policy, map payload field.
- `config/pancake_td_color_alias.json`: alias mau (mac dinh co `kem -> trắng`).
- `config/thai_duong_order_payload_template.json`: payload mau tao don Thai Duong (cap nhat theo payload capture thuc te cua anh).
  - Trong `poll`, key `manual_order_lookup_hours` quy dinh so gio lookback khi chay lenh theo ma don (`len don JCT...`), mac dinh `720` (30 ngay).

Rule tinh tien/thanh toan:
- Chuyen khoan/tra truoc: `paymentType=TRANSFER`, `cod=tong don`, `codTransferred=tong da chuyen`.
- Don coc: `cod=tong don - tien coc`, `codTransferred=tien coc`.
- Don COD thuong: `cod=tong don`, `codTransferred=0`.
- Neu thieu du lieu coc/thanh toan: fallback `cod=tong don`.
- Neu tien tu Pancake la minor unit, dat `pancake.money_minor_unit_factor` (vd `100`) de quy doi dung truoc khi day qua Thai Duong.

## Agentic App Skill cho DOI SOAT COD

Dong goi nay dung cho agent/LLM khac tai su dung nhanh luong doi soat COD theo chuan da chot:
- Script lam IO va orchestration.
- LLM (neu co key) chi lam phan judgment.
- Output JSON + HTML de audit va chain automation.

### Thanh phan

- Skill guide: `docs/skills/cod-reconcile-agentic-app/SKILL.md`
- Script app: `scripts/agentic_cod_skill_app.py`
- Input profile mau: `scripts/profiles/agentic_cod_input.example.json`

### Chay nhanh (1 lenh)

```powershell
.\.venv\Scripts\python.exe .\scripts\agentic_cod_skill_app.py `
  --request-text "doi soat cod hom qua" `
  --apply-updates auto `
  --sync-sheet auto `
  --llm-judge auto `
  --html-report `
  --output-json ".\storage\reconcile_cod\reports\agentic_latest.json"
```

### Adaptive input/output

- Input:
  - `--request-text` (lenh tu nhien),
  - `--settlement-date`,
  - `--input-json` (profile co dinh),
  - `--interactive` (prompt khi thieu input).
- Output:
  - JSON stdout (machine-friendly),
  - file JSON (`--output-json`),
  - file HTML (`--html-report`/`--output-html`), mo tu dong bang `--open-html`.

### LLM judgment mode

- `--llm-judge auto` (khuyen dung): co key thi goi LLM, khong co thi fallback heuristic.
- `--llm-judge force`: bat buoc phai co key.
- `--llm-judge off`: chi dung heuristic deterministic.

## Media Research Bot (Bot 2)

Bot 2 la process Telegram rieng de:
- Nhan anh san pham + caption `/media <SKU> [keyword]`.
- Tim media thi truong (anh + video) theo huong API-first (SerpApi + Cloudinary).
- Gui preview ket qua va cho bam `Duyet/Huy` truoc khi ghi Google Sheet.
- Ghi sheet theo tung dong media (one link per row) va upsert theo `dedupe_key`.

### Bien moi truong cho Bot 2

Can dien cac bien trong `.env`:
- `MEDIA_BOT_TELEGRAM_TOKEN`
- `MEDIA_BOT_ALLOWED_USER_ID`
- `MEDIA_BOT_DAILY_RUN_CAP` (mac dinh `30`)
- `MEDIA_BOT_TIMEZONE` (mac dinh `Asia/Ho_Chi_Minh`)
- `MEDIA_RESEARCH_SERPAPI_API_KEY`
- `MEDIA_RESEARCH_MAX_IMAGE_RESULTS` (mac dinh `20`)
- `MEDIA_RESEARCH_MAX_VIDEO_RESULTS` (mac dinh `20`)
- `MEDIA_RESEARCH_MAX_API_CALLS_PER_RUN` (mac dinh `5`)
- `MEDIA_RESEARCH_PLATFORM_ALLOWLIST`
- `MEDIA_RESEARCH_MARKET_SCOPE` (mac dinh `VN+TH+GLOBAL`)
- `MEDIA_RESEARCH_CLOUDINARY_CLOUD_NAME`
- `MEDIA_RESEARCH_CLOUDINARY_UPLOAD_PRESET` (unsigned upload preset)
- `MEDIA_RESEARCH_SHEET_ENABLED` (mac dinh `1`)
- `MEDIA_RESEARCH_SHEET_MODE=oauth_user`
- `MEDIA_RESEARCH_SHEET_SPREADSHEET_ID=1udcDbgZG9Oe7lWtMbvJCqhhK8gd9YcgC5mREfkeNiRg`
- `MEDIA_RESEARCH_SHEET_GID=844064194`
- `MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_ID`
- `MEDIA_RESEARCH_SHEET_OAUTH_CLIENT_SECRET`
- `MEDIA_RESEARCH_SHEET_OAUTH_REFRESH_TOKEN`
- `MEDIA_RESEARCH_SHEET_OAUTH_TOKEN_URI` (mac dinh `https://oauth2.googleapis.com/token`)

### Kiem tra config Bot 2

```powershell
python -m app.media_main --check-config
```

### Chay Bot 2 local

```powershell
python -m app.media_main
```

### Cu phap su dung Bot 2

1. Gui 1 anh san pham.
2. Dat caption:

```text
Tìm media
```

Hoac:

```text
Tìm media JC123 vay hoa nu
```

Van ho tro cu phap cu:

```text
/media SKU123 vay hoa nu
```

Lenh tro giup:

```text
/media_help
```

```text
/media_status
```

### Dinh dang sheet Bot 2

Neu tab `Reseach media` rong, bot tu tao header:
- `created_at`
- `run_id`
- `product_code`
- `query_text`
- `market_scope`
- `media_type`
- `platform`
- `title`
- `source_url`
- `direct_media_url`
- `thumbnail_url`
- `snippet`
- `engine`
- `engine_query`
- `score`
- `status`
- `dedupe_key`

## Personal Assistant Bot (Bot 3)

Bot 3 la process Telegram rieng de:
- Tong hop lich, ke hoach, ket qua tu Google + du lieu noi bo.
- Tra loi cau hoi ngoai nghiep vu qua OpenAI (co redaction du lieu nhay cam).
- Trigger tac vu nhay cam qua luong `Xac nhan/Huy` callback.
- Gui nhac chu dong theo lich:
  - 08:00 agenda
  - truoc su kien 30 phut
  - 21:00 tong ket

### Bien moi truong cho Bot 3

Can dien cac bien trong `.env`:
- `BOT3_TELEGRAM_TOKEN`
- `BOT3_ALLOWED_USER_ID`
- `BOT3_TIMEZONE` (mac dinh `Asia/Ho_Chi_Minh`)
- `BOT3_PROACTIVE_ENABLED` (mac dinh `1`)
- `BOT3_AGENDA_HOUR` (mac dinh `8`)
- `BOT3_EVENT_REMINDER_LEAD_MINUTES` (mac dinh `30`)
- `BOT3_EOD_HOUR` (mac dinh `21`)
- `BOT3_RATE_LIMIT_PER_MINUTE` (mac dinh `20`)
- `BOT3_MEMORY_ROOT` (mac dinh `memory`)
- `BOT3_MEMORY_INDEX_PATH` (mac dinh `storage/assistant_bot/memory.db`)
- `BOT3_REDACTION_ENABLED` (mac dinh `1`)
- `BOT3_OPENAI_ENABLED` (mac dinh `1`, dat `0` neu muon tat ChatGPT/OpenAI va chi dung du lieu noi bo)
- `BOT3_OPENAI_API_KEY`
- `BOT3_OPENAI_MODEL` (mac dinh `gpt-4.1-mini`)
- `BOT3_OPENAI_TIMEOUT_SECONDS` (mac dinh `45`)
- `BOT3_OPENAI_MAX_TOKENS` (mac dinh `800`)
- `BOT3_OPENAI_RETRY_MAX` (mac dinh `2`)
- `BOT3_OPENAI_RETRY_BACKOFF` (mac dinh `2,5`)
- `BOT3_GOOGLE_OAUTH_CLIENT_ID`
- `BOT3_GOOGLE_OAUTH_CLIENT_SECRET`
- `BOT3_GOOGLE_OAUTH_REFRESH_TOKEN`
- `BOT3_GOOGLE_OAUTH_TOKEN_URI` (mac dinh `https://oauth2.googleapis.com/token`)
- `BOT3_GOOGLE_CALENDAR_IDS` (mac dinh `primary`)
- `BOT3_GMAIL_QUERY_DEFAULT`
- `BOT3_SHEETS_SPREADSHEET_ID`
- `BOT3_SHEETS_GID`
- `BOT3_TASKS_ENABLED` (mac dinh `0`)
- `BOT3_TASK_GROUP_CHAT_ID` (chat id group task, vd `-100...` hoac `-...`)
- `BOT3_MANAGER_USER_IDS` (danh sach user id manager, phan cach boi dau phay)
- `BOT3_TASK_REQUIRE_TAG` (mac dinh `1`, group task bat buoc tag bot)
- `BOT3_TASK_DB_PATH` (mac dinh `storage/assistant_bot/tasks.db`)
- `BOT3_TASK_WEEKLY_SUMMARY_ENABLED` (mac dinh `1`)
- `BOT3_TASK_WEEKLY_SUMMARY_WEEKDAY` (mac dinh `5` = Thu 7, quy uoc `0=Thu 2 ... 6=Chu Nhat`)
- `BOT3_TASK_WEEKLY_SUMMARY_HOUR` (mac dinh `15`)
- `BOT3_TASK_WEEKLY_SUMMARY_MINUTE` (mac dinh `0`)
- `BOT3_TASK_WEEKLY_SUMMARY_MAX_ITEMS` (mac dinh `5`)
- `BOT3_DAILY_TASK_CHECKIN_ENABLED` (mac dinh `0`, dat `1` de bat hoi task 09:00/17:00)
- `BOT3_DAILY_TASK_MORNING_HOUR` / `BOT3_DAILY_TASK_MORNING_MINUTE` (mac dinh `9:00`)
- `BOT3_DAILY_TASK_EVENING_HOUR` / `BOT3_DAILY_TASK_EVENING_MINUTE` (mac dinh `17:00`)
- `BOT3_DAILY_TASK_WEEKDAYS` (mac dinh `0,1,2,3,4,5` = T2-T7)
- `BOT3_DAILY_TASK_MAX_ITEMS` (mac dinh `20`)
- Cloud backup khi may local tat:
- GitHub Actions task `bot3-daily-checkin` gui prompt backup luc 09:05/17:05 neu Worker khong thay local marker.
- Worker route Bot 3 webhook qua `/telegram/webhook/bot3`; script deploy se set webhook nay neu `.env` co `BOT3_TELEGRAM_TOKEN`.
- Script sync GitHub secrets se dua cac bien `BOT3_*` len Actions de cloud co token/config Bot 3.
- GitHub Actions cache them `storage/assistant_bot` de giu `tasks.db` va draft check-in giua cac lan cloud chay.

### Kiem tra config Bot 3

```powershell
python -m app.assistant_main --check-config
```

### Chay Bot 3 local

```powershell
python -m app.assistant_main
```

### Cu phap su dung Bot 3

- `/assistant_help`
- `/assistant_status`
- `/agenda [hôm nay|ngày mai|YYYY-MM-DD]`
- `/plan [tuần này|YYYY-MM-DD]`
- `/result [hôm qua|YYYY-MM-DD]`
- `/run report [YYYY-MM-DD|hôm qua]`
- `/run reconcile cod [YYYY-MM-DD|hôm qua]`
- `/run reconcile sheet <run_id>`
- `/run media sheet <run_id>`
- `/ask <cau hoi bat ky>`
- `/task add <tieu de>` (mac dinh nguon manager)
- `/task add self|manager | <tieu de> | <ghi chu>`
- `/task update <ten viec> | <status> | <percent> | <ghi chu> | <ly do blocked> | <buoc tiep theo>`
- `/task done <ten viec> | <ghi chu>`
- `/task list [all|todo|doing|blocked|done|pending]`
- `/task report`
- `/task week`
- `/task pending`
- `/task pick <request_id> <index>` (khi trung ten task)

Lenh tu nhien:
- `lịch hôm nay`
- `kế hoạch tuần này`
- `kết quả hôm qua`
- `chạy đối soát cod hôm qua`
- `hỏi ...`
- Private task wizard:
  - `thêm công việc: <tên task>`
  - bot se hoi lan luot: noi dung -> deadline -> tinh trang (`chưa làm`/`đang làm`/`hoàn thành`)
  - huy wizard: `/cancel`
- Daily task check-in:
  - 09:00 T2-T7 bot hoi viec hom nay trong chat rieng; moi dong anh gui se thanh 1 task deadline hom nay.
  - 17:00 bot gui lai danh sach task hom nay; anh cap nhat bang 1 tin nhieu dong theo so thu tu.
  - `/cancel` huy phien dang cho tra loi.
- Trong group task (co tag bot): `bao cao tien do`, `tong ket tuan`, `viec da hoan thanh`, `viec chua hoan thanh`

## Work Progress Service (Telegram + Zalo + Pancake Work)

Service nay thu thap trao doi cong viec da allowlist, trich xuat cap nhat tien do, cho manager duyet va tong hop report ngay/tuan/thang.

### Chay qua Bot 2 (khuyen nghi)

Bat env:
- `MEDIA_BOT_WORK_PROGRESS_ENABLED=1`
- (neu chi muon dung work progress, tat media research): `MEDIA_BOT_MEDIA_RESEARCH_ENABLED=0`
- `WORK_PROGRESS_MANAGER_TELEGRAM_IDS=<id1,id2,...>`
- `WORK_PROGRESS_TELEGRAM_BOT_TOKEN` (co the bo trong de fallback sang `MEDIA_BOT_TELEGRAM_TOKEN`)

Chay Bot 2:
```powershell
python -m app.media_main
```

Khi bat mode nay, Bot 2 se:
- Tu ingest tin nhan text Telegram trong cac channel allowlist cua work-progress.
- Mo HTTP ingest endpoint de Zalo/Pancake webhook day event vao.
- Chay scheduler report daily/weekly/monthly va gui private cho manager IDs.

Lenh manager tren Bot 2:
- `/progress help`
- `/progress pending [limit]`
- `/progress unmapped [limit]` (xem event thieu map danh tinh)
- `/progress approve <update_id> [ghi chú]`
- `/progress reject <update_id> [ghi chú]`
- `/progress edit <update_id> | <status> | <progress_pct> | [blocker] | [next_step] | [deadline]`
- `/progress map <member_id> | <platform> | <platform_user_id> | [display_name]`
- `/progress report <daily|weekly|monthly> [YYYY-MM-DD]`

### Chay service standalone (tuy chon)

Kiem tra config:
```powershell
python -m app.work_progress_main --check-config
```

Chay API + scheduler:
```powershell
python -m app.work_progress_main
```

Chi chay API:
```powershell
python -m app.work_progress_main --serve-api
```

Chi chay scheduler report private:
```powershell
python -m app.work_progress_main --run-scheduler
```

### HTTP endpoints

- `POST /ingest/telegram`
- `POST /ingest/zalo`
- `POST /ingest/pancake-work`
- `POST /ingest/forwarded` (fallback forwarded flow)
- `POST /members/map` (manual identity mapping)
- `GET /members?limit=200`
- `GET /review/pending?limit=20`
- `POST /review/{update_id}/approve`
- `POST /review/{update_id}/reject`
- `POST /review/{update_id}/edit`
- `GET /reports/daily?date=YYYY-MM-DD`
- `GET /reports/weekly?date=YYYY-MM-DD`
- `GET /reports/monthly?date=YYYY-MM-DD`

### Payload ingest toi thieu

```json
{
  "event_id": "optional-id-from-source",
  "channel_id": "group-or-thread-id",
  "sender_id": "user-id-on-platform",
  "message_text": "task: Bao cao tuan #BC01 dang lam 60%",
  "event_time": "2026-05-28T10:00:00+07:00",
  "raw_payload": {}
}
```

Zalo webhook co the day payload raw (nested) vao `POST /ingest/zalo`, bot se tu normalize cac field pho bien nhu:
- `sender.id` -> `sender_id`
- `recipient.id`/`group_id` -> `channel_id`
- `message.text` -> `message_text`
- `message.msg_id` -> `event_id`
- `timestamp` (seconds/ms) -> `event_time`

### Rule duyet

- `confidence >= WORK_PROGRESS_CONFIDENCE_FAST_TRACK` -> `pending_fast`
- Nho hon nguong -> `pending_manual`
- Chi ban ghi `approved` moi vao report KPI
- Moi thao tac `approve/reject/edit` deu co audit log

### Env chinh

- `WORK_PROGRESS_DATABASE_URL` (ho tro `sqlite:///...` va `postgresql://...`)
- `WORK_PROGRESS_API_HOST`, `WORK_PROGRESS_API_PORT`
- `WORK_PROGRESS_MANAGER_TELEGRAM_IDS`
- `WORK_PROGRESS_TELEGRAM_BOT_TOKEN`
- `WORK_PROGRESS_*_ALLOWLIST_CHANNEL_IDS`

## Bao cao hang ngay

- Lich tu dong 1: `08:00` theo `APP_TIMEZONE`, bao cao du lieu **ngay hom truoc**.
- Lich tu dong 2: `21:00` theo `APP_TIMEZONE`, bao cao du lieu **ngay hom nay**.
- Dich nhan report tu dong:
  - Luon gui ve `TELEGRAM_ALLOWED_USER_ID` (nhu luong cu).
  - Neu `DAILY_REPORT_NOTIFY_CHAT_ID` co gia tri khac `0`, bot gui **them 1 ban** vao chat id nay (co the la group/supergroup, thuong dang `-100...`).
  - Neu de trong hoac `0`, bot chi gui ve `TELEGRAM_ALLOWED_USER_ID`.
- Lenh thu cong (`/report` hoac cau tu nhien `bao cao ...`) cung dung cung co che dich nhan nhu tren.
- Rieng ban report tu dong gui vao group `DAILY_REPORT_NOTIFY_CHAT_ID`:
  - Moc `08:00` se noi them 2 khoi tong hop `3 ngay gan nhat` va `7 ngay gan nhat`.
  - Moc `21:00` khong them `3d/7d`, nhung se noi them khoi `Task cong viec cuoi ngay` neu `DAILY_REPORT_TASK_SUMMARY_ENABLED=1`.
  - Ban gui ve personal va report thu cong van giu cau truc cu.
- Bot khong gui daily report ngay luc khoi dong va khong gui bu slot da lo/pending khi khoi dong lai; bot se cho moc dung gio tiep theo.
- Quyen trong group:
  - Tat ca thanh vien trong group `DAILY_REPORT_NOTIFY_CHAT_ID` deu co the goi lenh report/doi soat, **nhung bat buoc phai tag dung bot** trong tin nhan (vi du: `/report@ten_bot`, `/reconcile@ten_bot cod`, `@ten_bot bao cao hom nay`).
  - Trong group nay, bot chi xu ly report/doi soat; tin nhan khac se im lang (khong bao loi parse link).
  - Trong group nay, khi co thanh vien moi duoc them vao, bot se tu dong gui loi chao mung.
  - Cac lenh nhay cam khac (ads/token) van theo quyen `TELEGRAM_ALLOWED_USER_ID`.
  - Neu muon bot nhan cau tu nhien trong group (vi du: `bao cao hom nay`, `doi soat cod hom qua`), can tat Privacy Mode cua bot trong BotFather (`/setprivacy` -> `Disable`).
- Nguon POS: API cua `pos.pancake.vn` (mac dinh `PANCAKE_API_BASE_URL=https://pos.pancake.vn/api/v1`).
- Doanh thu POS: tong `total_price` theo don vi THB cua don trong ngay.
- Bao cao hien thi ca:
  - Tong doanh thu THB
  - Tong quy doi VND theo `REPORT_THB_TO_VND_RATE`
- Tool tu chia theo `REPORT_THB_MINOR_UNIT_FACTOR` de dua ve THB dung (mac dinh `100`, vi API tra ve dang don vi nho).
- Chi phi Ads: tong `spend` toan ad account trong ngay.
- Top san pham: top 10 theo doanh thu uoc tinh `quantity * retail_price`.
- Khi mot nguon loi, bot van gui phan con lai kem canh bao.

## Van hanh service Windows

Script trong `scripts/service/`:
- `install-service.ps1`
- `start-service.ps1`
- `stop-service.ps1`
- `uninstall-service.ps1`
- `install-watchdog-task.ps1`
- `uninstall-watchdog-task.ps1`
- `install-startup-watchdog.ps1`
- `install-media-service.ps1`
- `run-media-bot.ps1`
- `install-assistant-service.ps1`
- `run-assistant-bot.ps1`

### Chot phuong an "khong dung im"

Neu may khong cho tao Scheduled Task (Access denied), dung startup-watchdog (khuyen nghi):

```powershell
.\scripts\service\install-startup-watchdog.ps1 -StartupFileName start-fb-ads-bot.cmd
```

Watchdog se:
- Tu dong chay khi user dang nhap Windows.
- Kiem tra process `python -m app.main` dinh ky.
- Neu bot bi tat/crash -> tu khoi dong lai.
- Log watchdog: `logs/app/watchdog.log`.

Neu may co quyen tao Scheduled Task, co the dung:

```powershell
.\scripts\service\install-watchdog-task.ps1
```

Service Bot 2 mac dinh: `FBMediaResearchBot`.
Vi du cai Bot 2:

```powershell
.\scripts\service\install-media-service.ps1
.\scripts\service\start-service.ps1 -ServiceName FBMediaResearchBot
```

Service Bot 3 mac dinh: `FBPersonalAssistantBot`.
Vi du cai Bot 3:

```powershell
.\scripts\service\install-assistant-service.ps1
.\scripts\service\start-service.ps1 -ServiceName FBPersonalAssistantBot
```

## Ghi chu API

Meta Marketing API thay doi theo version va account capability.
Neu account cua anh co field/custom setup rieng, cap nhat:
- `config/objective.json` (overrides)
- `config/message_templates.json` (template patch)

Tool da ho tro merge payload override de de tinh chinh ma khong sua code core.
