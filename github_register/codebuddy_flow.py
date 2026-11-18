"""CodeBuddy login flow — a SEPARATE feature module for the regkit.

Harvests CodeBuddy `accessToken` / `refreshToken` for GitHub accounts we own,
through the same account lists the registration runner produces.

Design: the battle-tested Node flow lives untouched in `codebuddy_login/`
(vendored from Codebuddy-login-Github). This module is the Python bridge:

  1. parses our accounts files (`email----password----username----totp`)
     or the pipe format (`email|password|username|2fa`),
  2. writes `codebuddy_login/codebuddy.txt` + `configgithublogin.json`
     (proxies from our `proxies.txt`, per-account pinning),
  3. runs `node github-login.js` (concurrency/delay via env),
  4. syncs `results/<email>/` into `accounts/codebuddy/<email>/`,
  5. optionally runs `tools/activate.py` / `tools/prove.py` (region, trial,
     one real ACP turn) for each account.

Nothing here touches the registration runner — that is the separation.
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CB_DIR = ROOT / "codebuddy_login"
OUT_DIR = ROOT / "accounts" / "codebuddy"
PROXY_FILE_DEFAULT = ROOT / "proxies.txt"

# Our accounts format: email----password----username----totp_secret----has_recovery
KIT_SEP = "----"


def slug(email: str) -> str:
    return str(email).strip().replace("@", "_")


# ---------------------------------------------------------------------------
# Account parsing / conversion
# ---------------------------------------------------------------------------

def load_accounts(path: str | Path) -> list[dict]:
    """Parse one accounts file; accepts BOTH formats line by line."""
    out: list[dict] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if KIT_SEP in line:
            parts = [p.strip() for p in line.split(KIT_SEP)]
            email, password = parts[0], parts[1]
            username = parts[2] if len(parts) > 2 else ""
            totp = parts[3] if len(parts) > 3 else ""
        else:  # pipe format from the upstream project
            parts = [p.strip() for p in line.split("|")]
            email, password = parts[0], parts[1]
            username = parts[2] if len(parts) > 2 else ""
            totp = parts[3] if len(parts) > 3 else ""
        if email and password:
            out.append({"email": email, "password": password,
                        "username": username, "totp": totp})
    return out


def find_latest_accounts() -> Path | None:
    files = sorted(ROOT.glob("accounts/github_accounts_*.txt"))
    return files[-1] if files else None


def write_codebuddy_input(accounts: list[dict], dest: Path) -> None:
    """Write codebuddy.txt (email|password|username|2fa)."""
    lines = [
        f"{a['email']}|{a['password']}|{a['username']}|{a['totp']}"
        for a in accounts
    ]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_proxy_lines(proxy_file: str | Path | None) -> list[str]:
    path = Path(proxy_file) if proxy_file else PROXY_FILE_DEFAULT
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def write_codebuddy_config(
    proxies: list[str],
    accounts: list[dict],
    concurrency: int = 1,
    delay_ms: int = 15000,
    dest: Path | None = None,
) -> None:
    """Write configgithublogin.json; pin each account to its own proxy."""
    dest = dest or (CB_DIR / "configgithublogin.json")
    pin: dict[str, str] = {}
    for idx, acc in enumerate(accounts):
        if proxies:
            pin[acc["email"]] = proxies[idx % len(proxies)]
    payload = {
        "concurrency": max(1, min(int(concurrency), 10)),
        "delayMs": max(0, int(delay_ms)),
        "proxy": proxies or [],
        "accounts": pin,
    }
    dest.write_text(json.dumps(payload, indent=4), encoding="utf-8")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def ensure_node_deps(log=print) -> None:
    if (CB_DIR / "node_modules").is_dir():
        return
    log("[*] codebuddy_login: npm install (sekali saja) ...")
    proc = subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=CB_DIR, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"npm install gagal: {proc.stderr[-400:]}")


def run_harvest(concurrency: int, delay_ms: int, log=print, stop=None) -> int:
    """Run the vendored Node flow; returns its exit code. `stop` -> terminate."""
    env = dict(__import__("os").environ)
    env["CONCURRENCY"] = str(concurrency)
    env["DELAY_MS"] = str(delay_ms)
    # ROUTER9 passthrough when configured in our .env
    for key in ("ROUTER9_URL", "ROUTER9_PASS"):
        if __import__("os").environ.get(key):
            env[key] = __import__("os").environ[key]
    proc = subprocess.Popen(
        ["node", "github-login.js"], cwd=CB_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    terminated = False
    for line in proc.stdout:
        log(line.rstrip())
        if stop and stop():
            terminated = True
            proc.terminate()
            break
    proc.wait()
    if terminated:
        log("[!] codebuddy harvest dihentikan oleh Stop")
    return proc.returncode


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:  # test runs with a temp OUT_DIR
        return str(p)


def sync_results(accounts: list[dict], log=print) -> int:
    """Copy results/<slug>/ into accounts/codebuddy/<slug>/; returns synced count."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    synced = 0
    for acc in accounts:
        src = CB_DIR / "results" / slug(acc["email"])
        if not src.is_dir():
            continue
        dst = OUT_DIR / slug(acc["email"])
        if dst.is_dir():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        tokens = dst / "cli_tokens.json"
        if tokens.is_file():
            log(f"[+] {acc['email']}: tokens -> {_rel(tokens)}")
            synced += 1
        else:
            log(f"[i] {acc['email']}: sessions tersimpan, token belum jadi")
    bad = CB_DIR / "bad_accounts.txt"
    if bad.is_file():
        shutil.copy2(bad, OUT_DIR / "bad_accounts.txt")
        log(f"[!] bad_accounts.txt disalin ke {_rel(OUT_DIR)}")
    return synced


# ---------------------------------------------------------------------------
# Harvested tokens: list, export, live quota
# ---------------------------------------------------------------------------

CB_API = "https://www.codebuddy.ai"
_CB_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Product": "SaaS",
    "X-IDE-Type": "IDE",
    "X-IDE-Name": "IDE",
    "X-CodeBuddy-Request": "1",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:151.0) "
                   "Gecko/20100101 Firefox/151.0"),
    "Referer": f"{CB_API}/app",
}
TOKEN_KINDS = {"access": "accessToken", "refresh": "refreshToken"}

def token_files() -> list[Path]:
    return sorted(OUT_DIR.glob("*_cli_tokens.json"))

def read_token_file(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def jwt_expiry(token: str) -> int | None:
    """`exp` claim of a JWT, or None when the token is not a decodable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp else None
    except (IndexError, ValueError, TypeError):
        return None

def list_tokens() -> list[dict]:
    """Token metadata only — never the token values themselves."""
    rows: list[dict] = []
    for f in token_files():
        d = read_token_file(f)
        email = d.get("email") or f.name.removesuffix("_cli_tokens.json").replace("_", "@")
        access = d.get("accessToken") or ""
        refresh = d.get("refreshToken") or ""
        rows.append({
            "email": email,
            "file": f.name,
            "has_access": bool(access),
            "has_refresh": bool(refresh),
            "saved_at": d.get("saved_at") or "",
            "access_exp": jwt_expiry(access),
            "refresh_exp": jwt_expiry(refresh),
        })
    return rows

def export_tokens(kind: str, emails: list[str] | None = None) -> tuple[str, str]:
    """Build `(filename, content)` for one token kind: `email----token` lines."""
    field = TOKEN_KINDS.get(kind)
    if not field:
        raise ValueError(f"kind harus salah satu dari {sorted(TOKEN_KINDS)}")
    wanted = {e.strip().lower() for e in emails or [] if e.strip()}
    lines: list[str] = []
    for f in token_files():
        d = read_token_file(f)
        email = d.get("email") or f.name.removesuffix("_cli_tokens.json").replace("_", "@")
        token = d.get(field) or ""
        if not token or (wanted and email.lower() not in wanted):
            continue
        lines.append(f"{email}----{token}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"codebuddy_{kind}_tokens_{stamp}.txt", "\n".join(lines) + ("\n" if lines else "")

def _cb_call(path: str, token: str, body: dict | None = None,
             method: str = "POST", headers: dict | None = None,
             timeout: int = 25) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        CB_API + path, data=data, method=method,
        headers={**_CB_HEADERS, "Authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

def fetch_quota(access_token: str, timeout: int = 25) -> dict:
    """Live credits for one account, read with its CLI access token.

    Two console reads (uid + region flag) then the billing meter. The meter
    answers 500 while the account has no region/trial, so that case is named
    instead of surfacing a bare upstream error.
    """
    st, acc = _cb_call("/console/accounts", access_token, method="GET", timeout=timeout)
    if st != 200:
        return {"ok": False, "detail": f"/console/accounts HTTP {st}",
                "upstream": acc.get("msg") or acc.get("error") or ""}
    accounts = (acc.get("data") or {}).get("accounts") or []
    me = next((a for a in accounts if a.get("type") == "personal"),
              accounts[0] if accounts else {})
    uid = str(me.get("uid") or "")
    region_complete = bool(me.get("areaInfoComplete"))

    st, pay = _cb_call("/v2/billing/meter/get-payment-type", access_token,
                       body={}, headers={"X-User-Id": uid} if uid else None,
                       timeout=timeout)
    payment_type = ((pay.get("data") or {}).get("paymentType") or "") if st == 200 else ""

    now = time.localtime()
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", now),
        "PackageEndTimeRangeEnd": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 100 * 365 * 86400)),
    }
    st, res = _cb_call("/v2/billing/meter/get-user-resource", access_token, body=body,
                       headers={"X-User-Id": uid} if uid else None, timeout=timeout)
    if st != 200 or res.get("code") not in (0, None):
        detail = f"billing meter HTTP {st}"
        if not region_complete:
            detail += " — akun belum diaktifkan (region/trial); jalankan --activate"
        return {"ok": False, "detail": detail, "uid": uid,
                "region_complete": region_complete, "payment_type": payment_type,
                "upstream": str(res.get("msg") or res.get("error") or "")[:160]}

    data = (((res.get("data") or {}).get("Response") or {}).get("Data") or {})
    packages: list[dict] = []
    remain = used = size = 0
    for pkg in data.get("Accounts") or []:
        p_remain = int(pkg.get("CapacityRemain") or 0)
        p_size = int(pkg.get("CapacitySize") or 0)
        p_used = int(pkg.get("CapacityUsed") or 0)
        if p_size <= 0:
            p_size = p_remain + p_used
        if p_used == 0 and p_size > p_remain:
            p_used = p_size - p_remain
        remain += p_remain
        used += p_used
        size += p_size
        packages.append({
            "name": pkg.get("PackageName") or "",
            "remain": p_remain,
            "used": p_used,
            "size": p_size,
            "cycle_start": pkg.get("CycleStartTime") or "",
            "cycle_end": pkg.get("CycleEndTime") or "",
        })
    if int(data.get("TotalDosage") or 0) > size:
        size = int(data.get("TotalDosage") or 0)
    if size > remain and size - remain > used:
        used = size - remain
    return {
        "ok": True,
        "uid": uid,
        "region_complete": region_complete,
        "payment_type": payment_type,
        "total_remain": remain,
        "total_used": used,
        "total_size": size,
        "packages": packages,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

def run_stage(tool: str, accounts: list[dict], log=print, stop=None) -> None:
    """Run tools/activate.py or tools/prove.py per harvested account."""
    for acc in accounts:
        if stop and stop():
            return
        if not (OUT_DIR / slug(acc["email"]) / "cli_tokens.json").is_file():
            continue
        log(f"[*] {tool} untuk {acc['email']} ...")
        proc = subprocess.run(
            [sys.executable, f"tools/{tool}.py", slug(acc["email"])],
            cwd=CB_DIR, capture_output=True, text=True, timeout=300,
        )
        for line in (proc.stdout or "").splitlines():
            log(line.rstrip())
        if proc.returncode != 0:
            log(f"[!] {tool} exit={proc.returncode}: {(proc.stderr or '')[-200:]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m github_register.codebuddy_flow",
        description="CodeBuddy token harvest untuk akun GitHub hasil regkit "
                    "(modul terpisah; mesin Node di codebuddy_login/).",
    )
    ap.add_argument("--accounts", default="", help="accounts file "
                    "(default: accounts/github_accounts_*.txt terbaru)")
    ap.add_argument("--limit", type=int, default=0, help="maks akun diproses")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--delay-ms", type=int, default=15000)
    ap.add_argument("--proxy-file", default="",
                    help="proxy pool (default: proxies.txt)")
    ap.add_argument("--activate", action="store_true",
                    help="jalankan tools/activate.py (region + trial)")
    ap.add_argument("--prove", action="store_true",
                    help="jalankan tools/prove.py (satu turn ACP)")
    args = ap.parse_args(argv)

    log = lambda msg: print(msg, flush=True)  # noqa: E731
    acc_path = Path(args.accounts) if args.accounts else find_latest_accounts()
    if acc_path is None or not acc_path.is_file():
        print(f"[!] accounts file tidak ditemukan: {args.accounts or '(default)'}")
        return 2
    accounts = load_accounts(acc_path)
    if args.limit > 0:
        accounts = accounts[: args.limit]
    if not accounts:
        print("[!] tidak ada akun valid di file itu")
        return 2
    log(f"[*] codebuddy_flow | akun: {len(accounts)} | concurrency={args.concurrency} "
        f"| delayMs={args.delay_ms} | sumber={acc_path}")

    proxies = load_proxy_lines(args.proxy_file or None)
    log(f"[*] proxy pool: {len(proxies)}")
    write_codebuddy_input(accounts, CB_DIR / "codebuddy.txt")
    write_codebuddy_config(proxies, accounts, args.concurrency, args.delay_ms)
    ensure_node_deps(log)

    code = run_harvest(args.concurrency, args.delay_ms, log)
    synced = sync_results(accounts, log)
    log(f"[*] harvest exit={code} | hasil tersinkron: {synced}/{len(accounts)}")
    if code == 0 and args.activate:
        run_stage("activate", accounts, log)
    if code == 0 and args.prove:
        run_stage("prove", accounts, log)
    return code


if __name__ == "__main__":
    sys.exit(main())
