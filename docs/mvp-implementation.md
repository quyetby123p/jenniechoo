# MVP Implementation Notes

## Telegram command

Format:

```text
/ads <facebook_post_url> budget=<so_tien_vnd> len moi
```

Example:

```text
/ads https://www.facebook.com/permalink.php?story_fbid=123&id=456 budget=300000 len moi
```

Mode campaign cu:

```text
<facebook_post_url> [SKU...] len cu
<facebook_post_url> len cu camp video
```

## Approval callbacks

- Duplicate:
  - `Tao vN`: tao version moi.
  - `Huy`: huy request.
- Existing campaign selector:
  - `camp_pick:<request_id>:<index>`: chon campaign cu khi co nhieu ket qua.
  - `camp_cancel:<request_id>`: huy chon campaign cu.
- Draft review:
  - `Duyet`: publish campaign/adset/ad sang `ACTIVE`.
  - `Huy`: rollback toan bo object da tao.

## Job states

- `pending`: da tao nhap, cho duyet.
- `published`: da duoc duyet va publish.
- `cancelled`: nguoi dung huy.
- `failed`: loi khi tao/publish.

## Retry and rollback

- Retry Meta API: 3 lan (2s, 5s, 10s theo mac dinh).
- Neu van fail:
  - rollback toan bo entity da tao trong run.
  - luu job vao `storage/jobs/failed`.

## Payload override

Meta API giua cac account/version co the khac nhau.
Tool cho phep override payload tai:

- `config/objective.json`
- `config/message_templates.json`

De chinh field ma khong can sua code.
