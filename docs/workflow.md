# Workflow de xuat

## Luong van hanh bot

1. Anh gui lenh Telegram: `/ads <link> budget=<vnd> len moi` hoac `<link> ... len cu`.
2. Bot validate user + link + budget.
3. Neu cuoi cau lenh co `len cu`:
   - Bot khong tao campaign moi.
   - Neu co `camp <hint>` (vi du `camp video`): tim campaign `ACTIVE` theo keyword tu hint.
   - Neu len theo `camp <hint>`: ten ad dung `SKU:ALL`.
   - Neu khong co hint: tim campaign `ACTIVE` theo SKU (uu tien SKU nhap tay, fallback hashtag `#JC...` tu post).
   - Neu khop nhieu campaign: gui danh sach de bam chon.
   - Tao ads moi vao toan bo adset `ACTIVE/PAUSED` san co (toi da 20 adset).
   - Khi `Duyet`: chi publish ads moi tao, khong doi status campaign/adset cu.
4. Bot kiem tra trung link:
   - Neu trung: canh bao, cho bam `Tao vN`.
   - Neu khong trung: tao nhap ngay.
5. Neu cuoi cau lenh la `len moi`: bot tao bo cuc `1 campaign - 3 ad set - 3 ad` o trang thai `PAUSED`.
6. Bot gui ket qua + nut `Duyet/Huy`.
7. Neu `Duyet`: publish sang `ACTIVE`.
8. Neu `Huy` hoac loi:
   - Luong campaign moi: rollback campaign/adset/ad/creative vua tao.
   - Luong campaign cu: rollback ads/creative vua tao, khong dung vao campaign/adset cu.

## Luong quan tri du lieu

1. Tao run id bang `scripts/new-run.ps1` khi can track mot batch lon.
2. Tao artifact bang `scripts/new-artifact.ps1` de tranh ghi de.
3. Backup storage bang `scripts/backup-storage.ps1` truoc thay doi lon.

## Quy tac an toan

- Khong luu secret trong code.
- Khong cho user Telegram la ngoai whitelist thao tac.
- Luon rollback neu create/publish loi giua chung.
- Luon luu job state vao `storage/jobs/*` de audit.
