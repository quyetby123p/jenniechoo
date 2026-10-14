# ADS2 — Ruza Thailand

ADS2 hiện dùng page/ad account Ruza Thailand. Mỗi campaign Messenger tạo ở trạng thái
`PAUSED` theo layout `1 campaign -> 2 ad set -> 2 ad`, với hai nhóm cố định:

- `Công sở`
- `Thời trang`

## SKU catalog

Nguồn SKU là tab `Sản phẩm và giá`, cột J của [Google Sheet Ruza](https://docs.google.com/spreadsheets/d/12s9p2o11HOPp83AX3mLEXmphHxEMWSxNJGkgkXGbrVQ/edit?gid=0#gid=0).
Bot chạy theo snapshot tại `config/ads2/sku_catalog.json`, không đọc Sheet trong lúc tạo camp.

Đồng bộ snapshot:

```powershell
python -m app.ruza_catalog_sync --profile ads2
```

Caption chấp nhận `ADB485`, `#ADB485`, `ADB485.C2608` hoặc nhiều mã nối bằng `_`,
không phân biệt hoa thường. Mã ngoài snapshot sẽ dừng trước khi gọi Meta; bot không
fallback sang `ALL`.

Nếu Meta không trả được caption hoặc caption chưa có mã, có thể nhập SKU hợp lệ ngay
trong lệnh tạo camp, ví dụ:

```text
<link bài Ruza> KVF273_QLF191 ngân sách 200000 lên mới
```

SKU nhập trong lệnh vẫn phải tồn tại trong snapshot và nếu caption cũng có SKU thì hai
nguồn phải khớp nhau.

## Naming

Ví dụ với `KVF273_QLF191`:

```text
ADS:QUYET|MK:Thailand|KVF273_QLF191
ADS:QUYET|MK:Thailand|KVF273_QLF191 - Công sở
ADS:QUYET|MK:Thailand|SKU:KVF273_QLF191|MED:Video
```

## Cấu hình trước rollout

Hai Saved Audience ID nằm trong `config/ads2/audiences.json`. Template Messenger dùng
tên mẫu đã lưu trong Ads Manager là `Chào Ruza`; bot gửi tên mẫu này để Meta dùng lại
nội dung đã lưu, không cần gọi API quản lý message template hay tạo token mới. Nội dung
đối chiếu tại trường `greeting_text` trong `config/ads2/message_templates.json`.

Page ID `649228828282105` là page VAYXA cũ đã đổi tên thành RUZA Thailand, nên token
page hiện hữu vẫn được giữ lại. Chỉ cần đảm bảo các biến môi trường ADS2 đang trỏ đúng
page/ad account: `ADS2_META_PAGE_ACCESS_TOKEN`, `ADS2_META_AD_ACCOUNT_ID`,
`ADS2_META_PAGE_ID`.

Kiểm tra trước khi chạy bot:

```powershell
python -m app.main --profile ads2 --check-config
```

Bản cấu hình VAYXA trước đây được giữ riêng tại `config/ads2/vayxa/` để rollback.
