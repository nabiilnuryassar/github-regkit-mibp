# CodeBuddy login via GitHub

Harvests a CodeBuddy `accessToken` + `refreshToken` from a GitHub account, without a
browser, and proves the account can actually answer a model turn.

Fork of [`dowerarts/Codebuddy-login-Github`](https://github.com/dowerarts/Codebuddy-login-Github)
with the fixes that made it work end to end.

## What changed

The upstream script minted tokens but hit `403 Account Access Restricted` on
`login-actions/first-broker-login`. Four measured changes fixed it:

| change | why |
|---|---|
| `GET /console/logout` before login | a still-attached Keycloak session puts the account on `CB-4 302` (already authorized) and the broker answers 403. Releasing the session restores the consent path (`CB-4 200`) |
| `device_tokens.txt` from a real HAR | upstream invents `v3:<b64>#0#<b64>`; a real token is ~970 chars with binary payload and is what the edge accepts |
| `X-Product-Code: workbuddy` on `/console/auth/risk-context` | the real client sends it |
| `retryTransient` + 180 s timeout | sticky proxies drop sockets; one drop used to kill the whole run |

Plus a `CB-CLI` block that mints the plugin token, and `tools/` for provisioning and proof.

## Flow

```
1. GET  /console/logout                     release the stale Keycloak session
2. node github-login.js                     GitHub -> 2FA -> CodeBuddy OAuth -> CLI token
3. python3 tools/activate.py <account>      region + registerCloud + trial, when areaInfoComplete is false
4. python3 tools/prove.py <account>         one real ACP turn; stopReason=end_turn = working
```

`stopReason=refusal` with `429 The trial version is not yet activated` means step 3 was
skipped. `429 Credits exhausted` means the trial quota is spent — the account is fine.

## Setup

```bash
npm install

cp configgithublogin.example.json configgithublogin.json   # proxy list
# accounts: email|password|username|totp_secret, one per line
# device tokens: one real X-Device-Token per line, grabbed from a browser HAR
```

Then:

```bash
CONCURRENCY=1 node github-login.js
```

Outputs land in `results/<email>/`: `github_login_session.json`, `codebuddy_session.json`,
`cli_tokens.json`.

## Getting real device tokens

Open a HAR capture of a working `codebuddy.ai` login and pull the header:

```python
import json
d = json.load(open('capture.har'))
toks = {h['value'] for e in d['log']['entries'] for h in e['request']['headers']
        if h['name'].lower() == 'x-device-token'}
open('device_tokens.txt', 'w').write("\n".join(sorted(toks)) + "\n")
```

## Token endpoints

```
POST /v2/plugin/auth/state?platform=CLI                                      -> state
GET  /console/auth/login?platform=CLI&state=<state>&domain=www.codebuddy.ai  -> bind (302 ok)
GET  /v2/plugin/auth/token?state=<state>                                     -> accessToken, refreshToken
POST /v2/plugin/auth/token/refresh
     X-Refresh-Token: <refresh>   X-Auth-Refresh-Source: plugin   X-Product: SaaS
```

`code 11217` from the poll means still pending. `X-Domain` is load-bearing.

## Notes

- Nothing in `codebuddy.txt`, `device_tokens.txt`, `proxy.txt`, `configgithublogin.json`
  or `results/` is tracked — they hold credentials. `.gitignore` covers all of it.
- `tools/tdid_spoof.js` is kept as a **failed** experiment: the TDID SDK is a 725 KB
  obfuscated VM that needs real canvas/WebRTC/audio, so it cannot be spoofed from Node.
  Recorded so nobody repeats it.
- Upstream README follows below.

---

# GitHub OAuth Login Automation

Node.js automation script untuk melakukan login GitHub dan otorisasi OAuth ke CodeBuddy secara otomatis. Script ini dirancang untuk berjalan dengan proxy rotasi, User-Agent acak, dan delay antar akun guna mengurangi deteksi mekanisme keamanan.

![Demo](gambar.jpg)

## Fitur Utama

- **Login GitHub otomatis** dengan email, password, dan 2FA (TOTP/6 digit).
- **OAuth CodeBuddy** melalui broker Keycloak (`Login with GitHub`).
- **Rotasi proxy** dengan session ID acak per akun (mendukung DataImpulse).
- **User-Agent & device token** acak untuk mengurangi fingerprint statis.
- **Delay antar akun** dan kontrol concurrency lewat environment variable.
- **Penanganan akun bermasalah**: akun yang `flagged`, `suspended`, atau `access-restricted` dicatat di `bad_accounts.txt` agar tidak dicoba ulang.
- **Session persistence**: menyimpan cookie hasil login ke file JSON per akun.

## Struktur File

```
.
├── github-login.js                # Script utama
├── configgithublogin.json         # Konfigurasi bot (dari example)
├── configgithublogin.example.json # Contoh konfigurasi
├── codebuddy.txt                  # Daftar akun (format: email|password|username|2fa)
├── bad_accounts.txt               # (auto-generated) Akun bermasalah
└── sessions/                      # (auto-generated) Cookie hasil login
```

## Persyaratan

- Node.js >= 18
- Paket npm: `axios`, `https-proxy-agent`, `totp-generator`

Install dependency:

```bash
npm install axios https-proxy-agent totp-generator
```

## Format Input

### `codebuddy.txt`

```
email1@example.com|password1|username1|2FA_SECRET_OR_CODE
email2@example.com|password2|username2
email3@example.com|password3|username3|2FA_SECRET_OR_CODE
```

Kolom:
1. Email akun GitHub
2. Password
3. Username GitHub
4. 2FA (opsional). Bisa berupa secret TOTP base32 atau kode 6 digit langsung.

### `device_tokens.txt` (opsional)

Satu `X-Device-Token` CodeBuddy per baris. Jika tidak ada, script akan generate token fallback acak per akun. Token terbaik adalah yang diambil dari browser DevTools saat login manual.

## Konfigurasi via `configgithublogin.json`

Salin example terlebih dahulu:

```bash
cp configgithublogin.example.json configgithublogin.json
```

Lalu edit `configgithublogin.json` sesuai kebutuhan:

```json
{
    "concurrency": 1,
    "delayMs": 15000,
    "proxy": [
        "http://user:pass@host:port",
        "http://user:pass@gw.dataimpulse.com:823"
    ]
}
```

| Key | Default | Keterangan |
|---|---|---|
| `concurrency` | `1` | Jumlah akun yang diproses bersamaan (maks 10). |
| `delayMs` | `10000` | Jeda minimum antar akun (dalam ms). |
| `proxy` | - (wajib diisi) | Daftar proxy HTTP(S). Bisa string atau array. |

Untuk proxy DataImpulse, script akan otomatis menambahkan `;sessid.<random>` pada username supaya setiap akun keluar dari IP/session berbeda.

`userAgent`, `octo`, dan `cbDeviceToken` di-hardcode/randomize di dalam script. Environment variable `CONCURRENCY`, `DELAY_MS`, `PROXY`, `USER_AGENT`, `OCTO`, `CB_DEVICE_TOKEN` tetap dapat menimpa nilai di JSON.

## Cara Penggunaan

```bash
node github-login.js
```

Atau dengan pengaturan sementara lewat environment variable:

```bash
CONCURRENCY=1 DELAY_MS=15000 node github-login.js
```

## Alur Kerja

1. **Memuat daftar akun** dari `codebuddy.txt`.
2. **Memilih proxy & UA** secara acak untuk setiap akun.
3. **Login GitHub**:
   - GET halaman home & login.
   - POST kredensial ke `/session`.
   - Jika muncul 2FA, generate TOTP dan POST ke `/sessions/two-factor`.
4. **OAuth CodeBuddy**:
   - Inisialisasi session CodeBuddy (`/console/accounts`, `/console/auth/risk-context`).
   - GET halaman authorize Keycloak.
   - Klik tombol `social-github` (broker login).
   - Redirect ke GitHub OAuth authorize dengan cookie login.
   - Kembali ke CodeBuddy dengan `code` untuk menukar session authenticated.
5. **Menyimpan hasil**:
   - Jika berhasil, cookie GitHub/CodeBuddy disimpan di folder `sessions/<email>/`.
   - Jika gagal, alasan kegagalan dicatat di ringkasan akhir.

## Output

Log di terminal menampilkan status tiap step:

```
[14.10.00] [>] [Main] ========== AKUN 1/5: user@example.com ==========
[14.10.00] [i] [Config] Using proxy: http://...__cr.id;sessid.xxx@gw.dataimpulse.com:823
[14.10.01] [+] [Step 1] Status: 200
...
[14.10.15] [i] [Main] ========== RINGKASAN ==========
[14.10.15] [+] [Main] Sukses: 1/5
[14.10.15] [!] [Main] Gagal: 4/5
```

## Troubleshooting

| Masalah | Penyebab umum | Solusi |
|---|---|---|
| `407 Proxy Authentication Required` | Kredensial proxy salah / session tidak valid. | Cek `configgithublogin.json`, pastikan username & password proxy benar. |
| `account-flagged` / `suspended` | Akun GitHub sudah di-flag atau di-suspend. | Ganti akun; akun tersebut dicatat di `bad_accounts.txt`. |
| `access-restricted` / `security policy` | CodeBuddy/Tencent WAF mendeteksi pola otomatis. | Turunkan `CONCURRENCY`, naikkan `DELAY_MS`, ganti provider proxy, atau cooldown beberapa jam. |
| `authenticity_token tidak ditemukan` | GitHub mengembalikan halaman challenge/error. | Cek proxy, UA, dan pastikan IP tidak di-ban. |
| `no-first-session` | CodeBuddy tidak memberikan cookie session. | Cek koneksi/proxy atau device token. |

## Catatan Keamanan

- Script ini menyimpan password di plaintext (`codebuddy.txt`). Pastikan file tersebut tidak di-push ke repositori publik.
- Gunakan `.gitignore` untuk mengabaikan file sensitif:

```gitignore
codebuddy.txt
bad_accounts.txt
sessions/
configgithublogin.json
```

- Gunakan proxy yang valid dan jangan jalankan terlalu agresif supaya tidak melanggar kebijakan platform.
- `configgithublogin.json` bersifat pribadi dan tidak perlu di-push ke GitHub.

## Disclaimer

Script ini dibuat untuk tujuan edukasi dan otomatisasi akun yang Anda miliki secara sah. Penggunaan untuk aktivitas yang melanggar Terms of Service GitHub, CodeBuddy, atau hukum yang berlaku menjadi tanggung jawab pengguna sepenuhnya.
