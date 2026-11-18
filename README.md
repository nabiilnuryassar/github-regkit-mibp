# GitHub Register

A GitHub account registration toolkit driven by **multiple stealth browser
engines** (Camoufox, Patchright, CloakBrowser, Damru, Clawbrowser, AliasMode,
Fingerprint Chromium — with automatic fallback) and **multiple mailbox
providers**: mail.cx (free disposable), Litensi (paid), Cloudflare Email
Routing (your own domains, IMAP read-back), and **Tempik** — the project's
own Cloudflare Worker temp-mail API.

It can be run from the CLI, through a local web console, or as a Docker
service behind an nginx reverse proxy.

> Use this only for accounts and workflows you are authorized to manage.
> Automated account registration may violate GitHub's Terms of Service and can
> result in account or IP restrictions.

## Fork status

This repository is a **fork** — a personal working copy maintained at
[`mhiqrambg/github-regkit-mibp`](https://github.com/mhiqrambg/github-regkit-mibp),
not an upstream distribution. Local modifications on top of the original
toolkit:

- **CodeBuddy module** (`github_register/codebuddy_flow.py`, `CodeBuddyPanel`)
  — token harvest plus credit/quota checks; the vendored Node flow in
  `codebuddy_login/` is itself a fork of `dowerarts/Codebuddy-login-Github`.
- **Qoder module** (`github_register/qoder_flow.py`, `qoder_inject.py`).
- Proxy probing, extra mailbox providers (Tempik, Cloudflare Email Routing),
  and the redesigned console.

Fork-specific rule: **credentials never enter this repository**. `.env`,
`accounts/`, `accounts/codebuddy/*_cli_tokens.json`, `config.json*` backups,
`proxies.txt`, and the CodeBuddy login inputs are gitignored. Copy
`.env.example` to `.env` and fill it in locally.


## Features

- Creates a mailbox, password, and username for GitHub signup.
- Verifies the eight-digit GitHub launch code from the mailbox.
- Logs in again when a newly verified account is redirected to `/login`.
- Optionally creates a first repository, enables TOTP 2FA, and stores recovery
  codes per account.
- Optionally sets a profile status and completes profile fields after 2FA.
- **Browser engine registry** — one engine carries a run; on failure the next
  engine in `engine_order` takes over. A missing engine is skipped
  automatically, and the mailbox is ordered once and reused across fallbacks.
- **Cloudflare Email Routing** — receive GitHub mail on your own domains
  (apex + rotating `a/b/c` subdomains) via catch-all.
- **Tempik provider** — talk directly to the project's Cloudflare Worker
  temp-mail API (`tempik.iotsentinel.web.id`); no IMAP, no API token needed at
  runtime.
- Zone manager: `python -m github_register.cloudflare ensure` enables Email
  Routing, creates MX/SPF records and wires catch-all per domain.
- Provides a web console for configuration, job control, live logs, account
  export, TOTP generation, and recovery-code viewing.
- Works on desktop and mobile: the console reflows to a bottom navigation bar
  and a drawer on phones.
- Organizes accounts into groups, merges account files into one, and exports
  accounts as TXT, CSV, or JSON.
- Reorders a Litensi mailbox from the console to fetch a fresh code, capped at
  two minutes with a manual stop.
- Protects the console with username + password auth (rate-limited,
  server-side sessions) for self-hosting.

<img src="result.webp" alt="GitHub Register web console" width="880">

## Requirements

- Python 3.11 or newer.
- Node.js 18 or newer, only to rebuild the frontend.
- A mailbox provider (see below). `mailcx` works out of the box; `tempik`
  needs the Tempik Worker running.
- One or more browser engines (see [Browser engines](#browser-engines)).
  `camoufox` is the default and included in `requirements.txt`.
- Internet access. A residential proxy may be needed depending on your network.

## Installation

```bash
git clone <repository-url> github-regkit
cd github-regkit

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# Download browser runtimes once.
python -m camoufox fetch               # default engine (Firefox anti-detect)
patchright install chromium            # engine #2 (optional but recommended)
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### Linux (Ubuntu/Debian)

Headful mode (`headless: false`) needs a display plus the Firefox system
libraries. On a desktop these are usually already installed; on a minimal
server install them and run inside a virtual display:

```bash
# Ubuntu 22.04
sudo apt update
sudo apt install -y libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 xvfb

# Ubuntu 24.04 uses libasound2t64 instead of libasound2
sudo apt install -y libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2t64 xvfb

# headless VPS — wrap headful runs with a virtual display
xvfb-run -a python main.py --count 1
```

For unattended servers, `headless: true` (or `python main.py --headless`)
works without a display, though it is slightly more likely to be flagged by
DataDome.

## Configuration

Set your local values in `config.json`. This file must never be committed.

```json
{
  "mail_provider": "tempik",
  "mailcx_domain": "",
  "litensi_api_id": "",
  "litensi_api_key": "",
  "litensi_site": "github.com",
  "litensi_zone": "",
  "cloudflare_domains": ["beelze.biz.id", "djaitin.web.id", "urban8.site"],
  "cloudflare_subdomains": ["a", "b", "c"],
  "tempik_api": "https://tempik.iotsentinel.web.id/api",
  "tempik_domains": [],
  "imap_host": "imap.gmail.com",
  "imap_user": "",
  "imap_pass": "",
  "register_count": 1,
  "proxy": "",
  "proxy_file": "",
  "engine": "",
  "engine_order": ["camoufox", "patchright", "cloakbrowser"],
  "engine_paths": {},
  "headless": false,
  "delay_sec": 5.0,
  "max_username_tries": 6,
  "otp_timeout_sec": 240,
  "browser_profile_dir": ".browser-profile",
  "fresh_profile": true,
  "proxy_hard_block_retries": 2,
  "proxy_rate_limit_retries": 2,
  "create_repo": true,
  "repo_name": "hello",
  "enable_2fa": true,
  "set_profile_status": true,
  "profile_status": "On vacation",
  "complete_profile": true,
  "profile_name": "",
  "profile_bio": "",
  "profile_location": ""
}
```

| Field | Description |
| --- | --- |
| `mail_provider` | Mail backend: `mailcx` (free, default), `litensi` (paid), `cloudflare` (Email Routing + IMAP read-back) or `tempik` (own Worker API — recommended). |
| `mailcx_domain` | Mail.cx domain. Leave blank to auto-pick from available domains. |
| `litensi_api_id` / `litensi_api_key` | Litensi API credentials (used when `mail_provider` is `litensi`). |
| `litensi_site` | Sender domain in Litensi, for example `github.com`. |
| `litensi_zone` | Mailbox zone. Leave blank to choose the cheapest in-stock zone. |
| `cloudflare_domains` | Domains for the `cloudflare` provider; addresses rotate across them. |
| `cloudflare_subdomains` | Rotating subdomains per domain (e.g. `["a","b","c"]`); each gets MX + SPF pointing at Cloudflare's routing MX. |
| `tempik_api` | URL of the project's Tempik temp-mail Worker (`mail_provider: "tempik"`). |
| `tempik_domains` | Optional domain pool for Tempik; blank = auto-fetch from the Worker's `/api/config`. |
| `imap_host` / `imap_user` / `imap_pass` | IMAP destination inbox where Email Routing forwards the mail (Gmail: use an App Password). |
| `register_count` | Accounts to process in one job. |
| `proxy` | Optional single proxy in `http://user:pass@host:port` format. |
| `proxy_file` | Optional proxy pool file in the project root (one `scheme://user:pass@host:port` per line). Each account locks ONE proxy from the pool for its whole cycle (signup → verify → login → CodeBuddy); rotation happens only on a hard block or rate limit. Manage and test the pool from the console's **Proxies** tab. Takes precedence over `proxy`. |
| `engine` | Exclusive browser engine (`""` = automatic fallback order). See [Browser engines](#browser-engines). |
| `engine_order` | Fallback order of engine keys; a run continues with the next engine when one is missing or fails. |
| `engine_paths` | Per-engine extras, e.g. `{"fingerprint_chromium": "/path/to/binary", "clawbrowser": "ws://127.0.0.1:9222"}`. |
| `headless` | Runs without a browser window. `false` is easier to observe and often more stable. |
| `delay_sec` | Delay between accounts. |
| `max_username_tries` | Username conflict retry limit. |
| `otp_timeout_sec` | Maximum wait time for the verification email. |
| `browser_profile_dir` | Persistent browser profile directory (trust cookies are carried separately, so fresh profiles still pass DataDome). |
| `fresh_profile` | Uses a fresh browser profile for each account while carrying trusted cookies separately. |
| `proxy_hard_block_retries` | Rotations after a DataDome hard block. |
| `proxy_rate_limit_retries` | Rotations after a GitHub secondary rate limit. |
| `create_repo` / `repo_name` | Enables and names the first repository. |
| `enable_2fa` | Enables TOTP 2FA and captures recovery codes. |
| `set_profile_status` / `profile_status` | Enables and sets a post-2FA profile status. |
| `complete_profile` | Enables post-2FA profile completion. |
| `profile_name`, `profile_bio`, `profile_location` | Custom profile values. Blank fields use Random User or ZenQuotes data. |

## Mail providers

### Tempik (recommended)

`mail_provider: "tempik"` talks directly to the project's own Cloudflare
Worker temp-mail API (`https://tempik.iotsentinel.web.id/api` by default) —
the Worker sits behind Email Routing catch-all, so **no IMAP and no API
token are needed at runtime**. The kit creates an inbox
(`POST /api/inboxes`), polls messages (`GET /api/inboxes/<addr>/messages`)
for the GitHub code, and cleans the inbox up afterwards. Domains are
auto-fetched from the Worker (`/api/config`), or pinned via `tempik_domains`.

The Worker's allowed domains live in its `MAIL_DOMAIN` plain-text binding
(comma-separated, apex + `a/b/c` subdomains). To add or rotate domains,
either edit the binding in the Workers dashboard or re-upload the script with
full metadata (see *Worker maintenance* below).

### Cloudflare Email Routing provider

`mail_provider: "cloudflare"` uses your own domains through Cloudflare Email
Routing (free) instead of disposable mailboxes. Because Email Routing has no
inbox API, the kit invents an address `random@[sub.]domain` — catch-all accepts
it — and reads the forwarded GitHub code from the destination inbox over IMAP.

One-time setup (also runnable any time, idempotent):

```bash
# .env
CLOUDFLARE_API_TOKEN=...        # Zone: Email Routing Rules/Settings/DNS/Read
CLOUDFLARE_ZONES=a.id,b.id
CLOUDFLARE_SUBDOMAINS=a,b,c
CLOUDFLARE_MAIL_WORKER=tempik   # optional: catch-all -> your email Worker
IMAP_HOST=imap.gmail.com
IMAP_USER=you@gmail.com
IMAP_PASS=your-app-password     # myaccount.google.com/apppasswords

source .venv/bin/activate
python -m github_register.cloudflare ensure
```

`ensure` enables Email Routing per zone, creates apex + subdomain MX/SPF
records, and (with `CLOUDFLARE_MAIL_WORKER`) points catch-all at the Worker.
The web console's `/api/cloudflare/check` (Config → diagnostics) reports each
zone's enabled status, MX records and catch-all action. The mailbox loop never
calls the Cloudflare API — only the IMAP inbox — so the token is only needed
for management.

### mail.cx / Litensi

- `mail_provider: "mailcx"` — free disposable inboxes (uqu.me, ddker.com,
  9k3r.com). Zero setup, but disposable domains are more often challenged by
  GitHub.
- `mail_provider: "litensi"` — paid, more reliable. Fill `litensi_api_id` /
  `litensi_api_key` (from litensi.id), `litensi_site: "github.com"`, and leave
  `litensi_zone` blank for the cheapest in-stock zone.

## Browser engines

One engine carries a run; the choice is exclusive. Nothing selected keeps the
automatic order from `engine_order`. When an engine is not installed, cannot
launch, or exhausts all its page-reload/session retries, the run falls back to
the next engine — the mailbox is ordered once and reused, so a fallback never
burns a second mailbox. A failing engine is logged as
`ENGINE fallback -> <next>` in the Live Log.

| Key | Engine | Family | Proxy | Install |
| --- | --- | --- | --- | --- |
| `camoufox` | Camoufox (CF) | Firefox | custom | `pip install camoufox && python -m camoufox fetch` (default) |
| `patchright` | Patchright (PR) | Chromium | custom | `pip install patchright && patchright install chromium` |
| `cloakbrowser` | CloakBrowser (CL) | Chromium | custom | `pip install cloakbrowser` (binary auto-downloads; set `CLOAKBROWSER_LICENSE_KEY`) |
| `damru` | Damru (DM) | Android (real) | custom | `pip install git+https://github.com/akwin1234/damru.git` — Linux + Docker/Redroid |
| `fingerprint_chromium` | Fingerprint Chromium (FC) | Chromium | custom | download binary from <https://github.com/adryfish/fingerprint-chromium/releases>, set `engine_paths` |
| `clawbrowser` | Clawbrowser (CB) | Managed runtime | managed (one pool key per launch) | `clawctl` CLI + API key; CDP endpoint via `engine_paths` or `clawctl start` |
| `aliasmode_cloak` | AliasMode + CloakBrowser (AM) | Chromium | custom | AliasMode desktop app + Local API; CDP endpoint via `engine_paths` |
| `aliasmode_firefox` | AliasMode Firefox (AF) | Firefox | custom | AliasMode app (its Firefox engine is built on Camoufox); CDP endpoint via `engine_paths` |
| `nextbrowser` | Nextbrowser (NB) | Desktop app | — | Not embeddable: an AGPL-3.0 desktop AI-orchestrator that drives Clawbrowser + Camoufox underneath. |

`camoufox`, `patchright` and `cloakbrowser` install as pip packages and light
up immediately. The others need an external runtime (Docker, CLI key, desktop
app, or a downloaded binary); until then they are listed as *not installed* in
the web console (`/api/engines`) and are skipped automatically by the fallback
order.

## Running

### Web console

Build the UI after frontend changes:

```bash
cd frontend
npm install
npm run build
cd ..
```

Start the local server:

```bash
source .venv/bin/activate
python -m web.server
```

Open <http://127.0.0.1:8093>.

- **Status**: start or stop jobs and inspect progress.
- **Live Log**: review events in real time.
- **Config**: edit settings; **Browser Engine** card (exclusive engine +
  fallback order), **Mail Provider** radio (mail.cx / Litensi / Cloudflare /
  Tempik), Litensi live balance and zone price/stock checks, mail.cx domains,
  and Cloudflare zone diagnostics. Litensi checks use saved credentials and
  run only when requested.
- **Accounts**: copy or export accounts (TXT / CSV / JSON), generate TOTP
  codes, view recovery codes, resend a Litensi mailbox code, merge one account
  file into another, and organize accounts into groups. Each row has an actions
  menu for group, copy, 2FA, recovery, resend, and delete.
- **Proxies**: upload a `.txt` list or paste one, then **Test pool** to measure
  its reputation against GitHub without registering anything. Each proxy is
  probed for reachability and for whether `github.com/signup` serves the form or
  the DataDome stub; a proxy that connects but gets challenged is reported
  *blocked*, because in production it burns a whole signup cycle before failing.
  The panel shows usable/blocked/dead counts, the usable ratio, and the country
  mix of the exit IPs.

The console is responsive: the sidebar collapses to an icon rail on narrow
laptops, and on phones it switches to a top bar, a bottom navigation bar, and a
slide-in drawer, with account rows reflowed into cards.

### Resend mailbox code

**Accounts → row actions → Resend mailbox code** reorders the same Litensi
mailbox and polls its status until a new GitHub code arrives or two minutes
pass. It stops automatically at the cap, and the dialog has a Stop button for
an early abort. Reorder needs only the API credentials from `config.json` and
the email stored in the accounts file, so no extra metadata is kept. The
provider validates ownership of the email, so an address that was not ordered
on this Litensi account fails with `ACTIVATION DOES NOT EXIST`.

Protect the web console with username + password (like n8n/WAHA) for
self-hosting/production. Copy `.env.example` to `.env` and fill it in
(`.env` is git-ignored; server auto-loads it, no extra dependency):

```bash
cp .env.example .env
python -m web.server
```

```dotenv
GITHUB_REGISTER_HOST=127.0.0.1
GITHUB_REGISTER_PORT=8093
GITHUB_REGISTER_USERNAME=admin
GITHUB_REGISTER_PASSWORD=use-a-strong-password
```

Legacy single-password mode still works (`GITHUB_REGISTER_ACCESS_PASSWORD`),
but username+password is preferred. When auth is enabled, the API docs page
is disabled and every API route requires login. Login attempts are rate-limited
per IP (10 failures/60s → HTTP 429), oversized login bodies are rejected
(413), credential comparison is timing-safe, security headers are set
(`nosniff`, `DENY` framing, `no-referrer`, `no-store` on APIs),
and Sign out invalidates the server-side session.
The server binds to `127.0.0.1` by default — only bind `0.0.0.0` behind a trusted reverse proxy with HTTPS.

To open the console from another device on the same network (phone, tablet),
set `GITHUB_REGISTER_HOST=0.0.0.0` in `.env`, restart the server, then browse to
`http://<your-lan-ip>:8093` from the other device. Always set a strong
`GITHUB_REGISTER_USERNAME`/`GITHUB_REGISTER_PASSWORD` first: `config.json` holds
real API keys and proxy credentials, so an unauthenticated `0.0.0.0` bind
exposes them to everyone on that network. Use this only on a trusted private
network, never on public Wi-Fi.

### Docker

Multi-stage image: `node:20-alpine` (frontend build) + `python:3.12-slim`
(runtime, includes Camoufox Firefox, Patchright Chromium + `xvfb` for headful
mode):

```bash
cp .env.example .env          # set a strong GITHUB_REGISTER_PASSWORD
cp config.example.json config.json
touch proxies.txt .datadome-trust.json github_recovery_codes.txt
docker compose up -d --build
```

Open <http://localhost:8093> for a quick local check. (The compose file in
this repo does not publish ports — production traffic goes through nginx,
see below; add a `ports:` entry if you need direct local access.)

Compose overrides `GITHUB_REGISTER_HOST=0.0.0.0` inside the container.
All files below persist on the host via bind-mount
(do not delete): `config.json`, `accounts/` (`github_accounts_*.txt`,
`recovery/`, `groups.json`), `.browser-profile/`, `proxies.txt`,
`.datadome-trust.json` (trust cookie — losing it means DataDome 403s from scratch),
`github_recovery_codes.txt`. Not persistent, which is fine: web login sessions
(in memory — log in again). For display-less VPS, set `"headless": true` in
`config.json` (cheaper, slightly easier for DataDome to flag). A DataDome
challenge cannot be solved without a visible window, so headless runs fail fast
instead of waiting for a manual click; use a residential proxy for headless.

### VPS + nginx reverse proxy (one docker network)

Compose already joins the external `nginx-network` and does **not** publish
ports — public access only via nginx. Make sure the network exists:

```bash
docker network ls | grep nginx-network || docker network create nginx-network
docker compose up -d --build
```

Example server block in nginx (same network, TLS via certbot):

```nginx
server {
    listen 443 ssl;
    server_name regkit.example.com;

    location / {
        proxy_pass http://app:8093;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # SSE live-log: no buffering + no quick timeouts
        proxy_buffering off;
        proxy_read_timeout 86400s;
    }
}
```

`GITHUB_REGISTER_TRUST_PROXY=1` (already in compose) makes rate-limit read the
real IP from `X-Forwarded-For`. Do not enable it when the container is exposed
directly without a proxy.

### CLI

```bash
source .venv/bin/activate
python main.py
python main.py --count 3
python main.py --proxy http://user:pass@host:port
python main.py --headless
python main.py --config config.local.json --count 1
```

Press `Ctrl+C` to stop the CLI or server. A `KeyboardInterrupt` or
`asyncio.CancelledError` during Uvicorn shutdown is expected after interruption.

## Registration Flow

1. Open the signup page and fight through DataDome until the form is ready.
2. Only then order a mailbox with the configured provider (mail.cx and Tempik
   are implicit; Litensi orders a zone, auto-picking the cheapest in-stock zone
   when `litensi_zone` is blank). Ordering late avoids burning
   balance/expiry while DataDome eats time; a fatal provider failure (empty
   balance, bad key, out of stock, IP not allowed) aborts the job immediately.
   A single mailbox that never receives the GitHub code within
   `otp_timeout_sec` is only a per-account failure — that account is counted
   as FAIL and the batch continues with the next one.
3. Open GitHub signup and fill email, password, and a username based on the
   mailbox local part.
4. Submit the form. If an overlay intercepts pointer clicks, the runner falls
   back to a DOM click. A disabled form is refreshed and filled with the same
   data before switching browser sessions.
5. Poll the mailbox and enter the GitHub launch code.
6. Sign in again if GitHub redirects the new account to login.
7. Create the first repository when enabled.
8. Enable TOTP 2FA, capture recovery codes, and persist them per account.
9. Optionally set profile status, then complete profile name, bio, and location.

Retry tiers inside a run: page reloads (same session) → new browser session →
**next engine** (`ENGINE fallback -> <engine>` in the Live Log).
Post-signup stage failures do not discard an account that was already verified.
The reason is written to Live Log.

## Account Output

```text
accounts/
  github_accounts_<timestamp>.txt
  recovery/
    <email-hash>.txt
```

Each account file contains one line per account:

```text
email----password----username----totp_secret----has_recovery
```

Recovery codes are stored separately under `accounts/recovery/`. The Accounts
page can reveal and copy them with the **Recovery** action.

Example account output:

```text
user@example.com----example-password----example-user----EXAMPLETOTPSECRET000
```

Generate a TOTP code manually from the fourth field:

```bash
python -c "import pyotp; print(pyotp.TOTP('EXAMPLETOTPSECRET000').now())"
```

## Recording a Manual Flow

`record_camoufox.py` opens Camoufox and records clicks, inputs, and navigation.

```bash
.venv/bin/python record_camoufox.py
.venv/bin/python record_camoufox.py --url https://github.com/login
```

Its output can contain email addresses, session URLs, and selectors. Treat
`recorded_steps.json` as sensitive local data.

## Worker maintenance (Tempik)

The Tempik Worker exposes its allowed domains via `/api/config`, read from its
`MAIL_DOMAIN` plain-text binding. Two ways to update it:

1. **Dashboard** — Workers & Pages → `tempik` → Settings → Variables →
   `MAIL_DOMAIN` → append `beelze.biz.id,a.beelze.biz.id,...` → Deploy.
2. **Upload API** — download the script
   (`GET /accounts/{id}/workers/scripts/tempik`), then re-upload it with
   `PUT` multipart where the **file part's filename must equal the module
   name** (`index.js`) and the `metadata` part carries the full configuration:

   ```bash
   curl -s -X PUT \
     -H "Authorization: Bearer $CLOUDFLARE_WORKERS_TOKEN" \
     -F 'metadata={"main_module":"index.js","compatibility_date":"2025-06-01","bindings":[{"type":"plain_text","name":"MAIL_DOMAIN","text":"<28 domains>"},{"type":"plain_text","name":"APP_NAME","text":"Tempik"},{"type":"d1","name":"DB","id":"<d1-id>"},{"type":"send_email","name":"EMAIL"},{"type":"plain_text","name":"WEB_HOST","text":"tempik.iotsentinel.web.id"}]};type=application/json' \
     -F 'index.js=@index.js;filename=index.js;type=application/javascript+module' \
     "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/workers/scripts/tempik"
   ```

   Pitfalls learned the hard way: omitting `bindings` from metadata wipes the
   Worker's bindings (the API then returns 500 on every route), and a file
   part whose filename differs from `main_module` fails validation with
   `10021 No such module`. The `bindings`/`settings` sub-endpoints may reject
   API-token writes with `10405` even when `Workers Scripts: Edit` is granted;
   the script-upload endpoint above is the reliable path.

## CodeBuddy login module (separate feature)

`github_register/codebuddy_flow.py` harvests CodeBuddy `accessToken` +
`refreshToken` for GitHub accounts we own. It is deliberately **separate**
from the registration runner: the battle-tested Node flow is vendored
untouched in `codebuddy_login/` (fork of
`dowerarts/Codebuddy-login-Github` with the working fixes), and the Python
bridge only prepares inputs, runs it, and syncs results.

```bash
# akun diambil dari accounts/github_accounts_*.txt terbaru;
# proxy diambil dari proxies.txt (satu per akun, di-pin per email)
.venv/bin/python -m github_register.codebuddy_flow

# kendali penuh
.venv/bin/python -m github_register.codebuddy_flow \
  --accounts accounts/github_accounts_20261112_055059.txt \
  --limit 3 --concurrency 1 --delay-ms 15000

# setelah panen: region + trial, lalu satu turn ACP sebagai bukti
.venv/bin/python -m github_register.codebuddy_flow --activate --prove
```

Input `codebuddy_login/codebuddy.txt` and `configgithublogin.json` are
regenerated from our account/proxy files on every run. Output lands in
`accounts/codebuddy/<email>/` (`github_login_session.json`,
`codebuddy_session.json`, `cli_tokens.json`); failed accounts are listed in
`accounts/codebuddy/bad_accounts.txt`. Real `X-Device-Token` values from a
browser HAR go into `codebuddy_login/device_tokens.txt` (see
`codebuddy_login/UPSTREAM_README.md`). Use only for accounts and services you
are authorized to manage.

## Troubleshooting

| Problem | Action |
| --- | --- |
| `BAD SITE` | Use a complete domain such as `github.com` for `litensi_site`. |
| No zone or stock | Use **Check Zone**, choose an in-stock zone, or leave it blank for automatic selection. |
| No verification email | Check Litensi balance and allow the mailbox reorder retry. For Tempik/Cloudflare, verify catch-all and MX first (`/api/cloudflare/check`). |
| Tempik `400 Invalid domain: ... Allowed: ...` | The domain is not in the Worker's `MAIL_DOMAIN` — update it (see *Worker maintenance*) or pin `tempik_domains` to the allowed set. |
| Tempik API `500 Internal Server Error` | A bad Worker upload wiped the bindings — re-upload with full metadata (see *Worker maintenance*). |
| Engine `not installed` in the console | Install it per the engines table, or rely on `engine_order` — unavailable engines are skipped automatically. |
| `ENGINE fallback -> ...` in the log | The previous engine failed; this is normal. Investigate the engine only when every engine fails. |
| Proxy `405/407` on CONNECT | Scheme/port/auth mismatch in `proxies.txt` (e.g. SOCKS endpoint declared as `http`). Verify with `curl -x <proxy> https://api.ipify.org`. |
| VPS runs old code | The image is stale — `git pull` then `docker compose up -d --build` on the VPS. |
| `config.json`/`proxies.txt` became directories | They did not exist before `up`, so docker created folders. `rm -rf` them, create real files (`cp`/`touch`), then `up` again. |
| VPS IP port 80/443 unreachable | Nothing listening or firewall closed: check `docker ps`, `curl http://127.0.0.1:80` on the host, `ufw status`, and the cloud security group. The app itself exposes no ports — traffic must flow through nginx. |
| Console returns 403 errors after redeploy | Stale token: sessions live in server memory and die on restart, while the browser keeps the old token. Reload the page — the UI now detects this and returns to the login screen automatically. Just log in again. |
| DataDome hard block or signup 403 | Change IP/proxy, disable VPN/WARP, then retry after a delay. |
| Create account or repository will not click | Review Live Log. Native clicks fall back to DOM clicks when an overlay intercepts them. |
| Web UI does not reflect frontend changes | Run `npm run build`, then restart `python -m web.server`. |

## Security

- Never commit `.env`, `config.json`, `proxies.txt`, `accounts/`,
  `.browser-profile/`, `.datadome-trust.json`, `github_recovery_codes.txt`,
  recovery codes, or browser recordings.
- Account files contain full credentials, including password and TOTP secret.
- Recovery codes grant account recovery and should be stored securely.
- Use scoped Cloudflare API tokens (never the Global API Key), with short TTLs.
  A token that has ever been pasted into a chat must be revoked and replaced.
- Web console auth: username + password from `.env`, 10 failed logins/60s
  per IP → HTTP 429, sessions are server-side and die on Sign out.
  Only expose via HTTPS reverse proxy; never enable `TRUST_PROXY` without one.
- Before pushing, inspect `git status --short` and `git diff --cached`.

## License

Released under the [MIT License](LICENSE).
