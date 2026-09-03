# Cầu nối Dropo → Pancake POS

## Jennie Choo Thailand

Luồng Jennie dùng đúng dữ liệu sản phẩm trong shop Pancake Jennie (`PANCAKE_SHOP_ID=407330767`):

```
Khách đặt trên th.jenniechoo.com
  └─ Dropo ghi lead vào Google Sheet, tab "Jennie Choo Đơn"
       └─ bridge đọc mã SKU + màu + size của toàn bộ giỏ
            └─ đọc catalog Pancake thật để lấy variation_id
                 └─ tạo một đơn gộp trong kho Jennie
                      └─ ghi Pancake Order ID + trạng thái ngược lại Sheet
```

Jennie không dùng SKU map của VAYXA. Mỗi lần chạy bridge đọc catalog Pancake ở chế độ
GET để nếu anh đổi biến thể/giá trên Pancake thì luồng vẫn bám dữ liệu nguồn.

Profile chạy Jennie là `main`; profile `ADS2` bên dưới vẫn dành cho VAYXA.

Tự động biến lead từ landing `vayxath.com` thành đơn trong Pancake shop VAYXA (`714269213`).

## Luồng

```
Khách đặt trên vayxath.com/vxvNNN
  └─ Dropo thu lead (11 page: root + /vxv001…/vxv011)
       └─ Dropo tự đổ sang Google Sheet, tab "Leads"
            └─ bridge đọc dòng chưa có "Pancake Order ID"
                 └─ POST /shops/714269213/orders
                      └─ ghi order id + trạng thái ngược lại Sheet
```

Dropo **không có webhook** — chỉ có Google Sheet và `list_data` qua MCP. Nên kiến trúc
bắt buộc là poll. Chọn Sheet làm nguồn giúp bỏ hẳn nhu cầu gọi API Dropo từ ngoài,
và dùng luôn OAuth Google mà app đã có sẵn.

## Chống tạo đơn trùng

Đây là phần quan trọng nhất: đơn trùng nghĩa là khách nhận hai kiện COD.

| Cơ chế | Vai trò |
|---|---|
| Cột `Pancake Order ID` trong Sheet | Nguồn sự thật duy nhất. Có giá trị → bỏ qua vĩnh viễn. |
| Ghi ngược **ngay sau từng đơn** | Không gom cuối vòng lặp. Tiến trình chết chỉ hở đúng 1 đơn thay vì cả lô. |
| Ghi Sheet lỗi → dừng hẳn | Không im lặng đi tiếp. Log `CRITICAL` kèm order id để điền tay. |
| `custom_id` = `DROPO-<thời gian>-<4 số cuối SĐT>` | Suy ra từ chính dữ liệu lead, ổn định qua mọi lần chạy. Có đơn trùng lọt vào vẫn tra ra được. |
| `concurrency` trong workflow | Hai lần chạy không bao giờ cùng đọc một dòng. |

**Không** dùng file state, vì GitHub Actions và Render đều có ổ đĩa ephemeral —
file sẽ mất sau mỗi lần chạy và đơn sẽ bị tạo lại.

## Chạy local trước

```powershell
cd D:\clawagent-main\clawagent-main\codex\projects\fb-ads-automation

# 1. Dry run — chỉ in payload, KHÔNG tạo đơn
.venv\Scripts\python.exe scripts\run_dropo_pancake_bridge.py --profile main

# 2. Ưng payload rồi thì tạo đơn thật
.venv\Scripts\python.exe scripts\run_dropo_pancake_bridge.py --profile main --live

# 3. Muốn chạy nền trên máy thay vì GitHub Actions
.venv\Scripts\python.exe scripts\run_dropo_pancake_bridge.py --profile main --live --loop
```

Mã thoát: `0` = ổn · `1` = có đơn lỗi · `2` = thiếu cấu hình.

## Chạy cloud khi tắt máy

Cloudflare Worker là scheduler duy nhất và dispatch workflow
`.github/workflows/dropo-jennie-pancake-bridge.yml` đúng 10 phút/lần (các phút
`00/10/20/30/40/50`, giờ Việt Nam). Worker cũng dispatch
`.github/workflows/pancake-jennie-choo-auto-confirm.yml` trong cùng phiên để đổi
đơn `chờ xác nhận`. GitHub Actions chỉ giữ `workflow_dispatch` cho chạy tay;
không dùng native cron để tránh chạy lệch phiên, chạy trùng và gửi mail lỗi lặp.
Concurrency giữ khóa cứng từng loại worker; nếu phiên trước chưa xong, phiên mới
không chạy song song cùng loại.

**Secrets cần thêm** (Settings → Secrets and variables → Actions):

| Secret | Lấy từ `.env` |
|---|---|
| `PANCAKE_SHOP_ID` | `407330767` |
| `PANCAKE_ACCESS_TOKEN` | Token API shop Jennie |
| `DROPO_PANCAKE_OAUTH_CLIENT_ID` | OAuth Google dùng đọc/ghi Sheet |
| `DROPO_PANCAKE_OAUTH_CLIENT_SECRET` | OAuth Google dùng đọc/ghi Sheet |
| `DROPO_PANCAKE_OAUTH_REFRESH_TOKEN` | OAuth Google dùng đọc/ghi Sheet |

Workflow được Cloudflare dispatch ở chế độ live để lead mới tự lên Pancake. Chạy tay mặc định
dry-run; muốn thử tạo thật thì vào Actions → Jennie Choo Dropo to Pancake → Run workflow
→ tick `live`. Không có đơn thật nào được tạo trong bước kiểm thử local hiện tại.

Mỗi lần chạy có bảng tóm tắt ngay trong tab Actions (dòng nào tạo đơn, dòng nào lỗi).
Lỗi dữ liệu của một dòng vẫn được ghi vào Sheet để quét lại ở phiên sau nhưng không
đánh đỏ toàn bộ workflow; lỗi cấu hình/hạ tầng vẫn bị báo rõ trong log.

**Lưu ý về scheduler cloud:** Cloudflare có thể trễ ngắn khi hệ thống tải cao,
nhưng đơn vẫn nằm trong Sheet và lượt kế tiếp sẽ quét bù; không phụ thuộc máy
của anh bật hay tắt.

## Trạng thái ghi vào cột `Sync status`

| Giá trị | Nghĩa | Lần chạy sau |
|---|---|---|
| `OK` | Đã tạo đơn | Bỏ qua (vì cột order id đã có) |
| `LỖI: ...` | Pancake từ chối | **Thử lại** |
| `BỎ QUA: ...` | Dữ liệu dòng không dựng được đơn (thiếu SĐT, SKU lạ) | Không thử lại |
| trống | Chưa xử lý | Sẽ xử lý |

Muốn ép chạy lại một dòng: xoá cả hai ô `Pancake Order ID` và `Sync status`.

## Đã kiểm thử

`pytest tests/test_dropo_pancake_bridge.py tests/test_pancake_create_order.py` — 45 test, gồm:

- gộp số lượng khi bundle trùng SKU; tách đúng khi bundle trộn màu/size
- cắt đúng hậu tố lô `-B1` (`VXV002-XANH LA-M-B1` → `VXV002-XANH LA-M`)
- SKU lạ không tạo item rác
- chạy lại **không** tạo đơn trùng
- ghi ngược ngay sau từng đơn, đúng thứ tự `create → write → create → write`
- ghi Sheet lỗi sau khi tạo đơn thì **dừng ngay**, không tạo tiếp
- `custom_id` ổn định qua các lần chạy, khác nhau giữa hai lead
- dry-run không gọi Pancake và không ghi gì vào Sheet
- tự thêm cột theo dõi khi Sheet chưa có
- mặc định luôn là dry-run

## Còn hở — phải biết trước khi bật live

1. **Nên kiểm tra một đơn thật đầu tiên.** Cấu trúc payload đã được đối chiếu với
   catalog/warehouse shop Jennie và dry-run thành công, nhưng vẫn nên theo dõi order đầu
   tiên trong Pancake để xác nhận quy trình kho/địa chỉ theo thực tế.
2. **Địa chỉ Thái.** Landing thu địa chỉ tự do + tỉnh + ZIP. Bridge tự tra geo API
   Pancake để bổ sung `province_id`/`district_id`/`commune_id`, tên chuẩn và
   `full_address` trước khi tạo đơn. Nếu địa chỉ không có đủ dấu hiệu để map
   quận/huyện hoặc phường/xã, bridge sẽ không tạo đơn lỗi mà ghi rõ nguyên nhân
   vào cột `Sync status` để bổ sung lại địa chỉ.
3. **`VXV003`** không có trong Pancake nên không có trong SKU map.
4. Khi Pancake đổi ảnh/biến thể, cần dựng lại `config/vayxa_sku_variation_map.json`
   từ các file `VXV*_variations.json`.
