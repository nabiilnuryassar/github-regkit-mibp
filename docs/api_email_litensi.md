# Litensi Email Activation API

Sumber: enam screenshot dokumentasi dashboard Litensi yang diberikan pengguna.
Terpisah dari SMS Activation API. Kredensial asli tidak disimpan di dokumen ini.

## Autentikasi dan format

Semua endpoint membutuhkan form field `api_id` (integer) dan `api_key` (string).
Endpoint mail memakai POST. Screenshot Profile tidak menampilkan metode;
klien proyek memakai POST untuk Profile.

Respons sukses: `{"success":true,"data":...}`.
Respons gagal: `{"success":false,"data":"PESAN ERROR"}`; screenshot endpoint mail menyebut HTTP 400 untuk kegagalan dan 200 untuk sukses.

## Profile

Endpoint: `https://litensi.id/api/profile`

Parameter: `api_id`, `api_key` (keduanya wajib).

```json
{"success":true,"data":{"username":"litensi","full_name":"Litensi","balance":"99593.04"}}
```

Gagal: `{"success":false,"data":"API salah atau tidak aktif"}`.
Nilai saldo di atas adalah contoh dokumentasi, bukan saldo akun pengguna.

## Prices

POST `https://litensi.id/api/mail/prices`

Parameter wajib: `api_id`, `api_key`, `site` (string, domain pengirim).

```json
{"success":true,"data":[{"zone":"hotmail.com","price":180,"stock":1382237},{"zone":"outlook.com","price":180,"stock":310105}]}
```

Gagal: `{"success":false,"data":"XXXXXX"}`.

## Order

POST `https://litensi.id/api/mail/order`

Parameter wajib: `api_id`, `api_key`, `zone` (string dari Prices, misalnya `gmail.com`), `site` (string domain pengirim).
Untuk konfigurasi proyek: `site=github.com`, `zone=gmail.com`.
Order adalah pembelian mailbox; bukan sekadar pengecekan.

```json
{"success":true,"data":{"order_id":5,"zone":"gmail.com","site":"tiktok.com","price":480,"email":"befasyza2017@gmail.com","expired_at":"2024-02-24 01:05:49","status":"WAITING"}}
```

Error yang ditampilkan:

| Pesan | Keterangan |
| --- | --- |
| UNAUTHORIZED API ACCESS | API ID/key salah atau tidak aktif |
| BAD SITE | Situs salah |
| BAD ZONE | Zona salah atau tidak tersedia |
| OUT OF STOCK | Stok tidak tersedia |
| QUEUE LIMIT | Maksimum 100 pesanan aktif |
| NO BALANCE | Saldo tidak mencukupi |
| SERVER MAINTENANCE | Pemeliharaan server |
| ERORR OCCURS | Terjadi kesalahan; ejaan mengikuti dokumentasi |
| SYSTEM ERROR | Hubungi admin |
| TIME LIMIT EXCEED | Batasan pesanan |
| XXXXX | Error lainnya |

## Check Status

POST `https://litensi.id/api/mail/getstatus`

Parameter wajib: `api_id`, `api_key`, `order_id` (integer dari Order).

```json
{"success":true,"data":{"order_id":5,"email":"befasyza2017@gmail.com","message":"","full_message":"","status":"WAITING"}}
```

| Status | Arti |
| --- | --- |
| WAITING | Menunggu pesan email |
| RECEIVED | Pesan diterima |
| CANCELED | Pesanan dibatalkan |
| SUCCESS | Pesanan selesai |

Error: `UNAUTHORIZED API ACCESS`, `ACTIVATION DOES NOT EXIST`, atau pesan lainnya.

## Set Status

POST `https://litensi.id/api/mail/setstatus`

Parameter wajib: `api_id`, `api_key`, `order_id` (integer), `status` (string).
Screenshot ini hanya mencantumkan `SUCCESS` dan `CANCELED`, bukan `RESEND`.

- `CANCELED` valid hanya sebelum pesan diterima.
- `SUCCESS` valid hanya setelah pesan diterima; tidak otomatis.

```json
{"success":true,"data":{"order_id":5,"email":"befasyza2017@gmail.com","status":"CANCELED"}}
```

Error: `UNAUTHORIZED API ACCESS`, `REQUEST INVALID`, `CANCEL FAILED`, `SUCCESS FAILED`, `EMAIL ACTIVATION EXPIRED`, `ACTIVATION DOES NOT EXIST`.

## Reorder

POST `https://litensi.id/api/mail/reorder`

Parameter wajib: `api_id`, `api_key`, `site` (string domain pengirim yang pernah dipesan), `email` (string alamat yang pernah dipesan).

```json
{"success":true,"data":{"order_id":5,"email":"befasyza2017@gmail.com","expired_at":"2024-02-24 01:54:30"}}
```

Error: `UNAUTHORIZED API ACCESS`, `BAD EMAIL` (mail diblokir), `FAILED`, `ERROR`, `ACTIVATION IS ACTIVE`, `ACTIVATION DOES NOT EXIST` (aktivasi tidak ditemukan atau bukan milik pengguna), atau pesan lainnya.
