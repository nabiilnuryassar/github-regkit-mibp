"""Qoder PAT injection — pure API, no browser needed.

Takes a Personal Access Token (pt-...) and:
  1. exchanges it for a Job Token (POST /api/v1/jobToken/exchange)
  2. reads the user plan (GET /api/v2/user/plan) → "personal_professional_trial"?
  3. claims all eligible campaigns (POST /sash/api/v1/me/campaigns/{id}/claim)
  4. verifies credit usage (GET /sash/api/v2/me/usage)

Reverse-engineered from qoder-ide_x86_64.rpm v1.39.2 (VS Code fork 1.106.3).
Cosy headers mirror the exact _buildCosyHeaders() from the IDE bundle.

PAT file format (one per line):
  pt-xxxx|email@example.com|machine_id|timestamp
Or just:  pt-xxxx

CLI:
  python -m github_register.qoder_inject --pats accounts/qoder_pats.txt
  python -m github_register.qoder_inject --pat pt-xxxx --machine-id <uuid>
  python -m github_register.qoder_inject --pats file.txt --status-only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent

# ── Endpoint constants (from bundle workbench.desktop.main.js v1.39.2) ──
OPENAPI = "https://openapi.qoder.sh"
WEB = "https://qoder.com"
INFERENCE = "https://api2.qoder.sh"

JOB_TOKEN_EXCHANGE = "/api/v1/jobToken/exchange"
DEVICE_TOKEN_POLL = "/api/v1/deviceToken/poll"
USER_INFO = "/api/v1/userinfo"
USER_STATUS = "/api/v3/user/status"
USER_PLAN = "/api/v2/user/plan"
CREDIT_USAGE = "/sash/api/v2/me/usage"
CAMPAIGNS = "/sash/api/v1/me/campaigns"
CLAIM_ELIGIBILITY = "/api/v2/activity/claim/eligibility"
CLAIM = "/api/v2/activity/claim"

# ── Version constants — keep in sync with latest release ──
IDE_VERSION = "1.39.2"     # Qoder IDE v1.39.2 (Nov 9, 2026)
CLI_VERSION = "1.1.59"     # known working CLI exchange version

TIMEOUT = 15


class QoderInjectError(RuntimeError):
    """Fatal injection error — invalid PAT, network, or server rejection."""


# ════════════════════════════════════════════════════════════════════
# Headers (exact replicas from qoder-ide bundle)
# ════════════════════════════════════════════════════════════════════

def ide_headers(machine_id: Optional[str] = None) -> dict:
    """Cosy headers ala desktop qoder-ide (_buildCosyHeaders: ClientType '0')."""
    h = {
        "Cosy-ClientType": "0",
        "Cosy-Version": IDE_VERSION,
        "Cosy-MachineOS": "x86_64_linux",
        "User-Agent": "Qoder",
        "Accept": "application/json",
    }
    if machine_id:
        h["Cosy-MachineId"] = machine_id
    return h


def cli_headers(machine_id: Optional[str] = None) -> dict:
    """Cosy headers for CLI/token exchange (ClientType '5')."""
    h = {
        "Cosy-ClientType": "5",
        "Cosy-Version": CLI_VERSION,
        "Cosy-MachineOS": "x86_64_linux",
        "User-Agent": f"qoder/{CLI_VERSION}",
        "Accept": "application/json",
    }
    if machine_id:
        h["Cosy-MachineId"] = machine_id
    return h


def _request(method: str, url: str, token: Optional[str] = None,
             headers: Optional[dict] = None, json_body: Optional[dict] = None,
             timeout: int = TIMEOUT, ok404: bool = False) -> Optional[dict]:
    """Thin requests wrapper with Bearer auth and error handling."""
    h = {"Accept": "application/json", "User-Agent": "Qoder"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if headers:
        h.update(headers)
    try:
        r = requests.request(method, url, headers=h, json=json_body, timeout=timeout)
    except requests.ConnectionError as exc:
        raise QoderInjectError(f"network: {exc}") from exc
    if ok404 and r.status_code == 404:
        return None
    if r.status_code == 401:
        raise QoderInjectError(f"401 Unauthorized — PAT expired or invalid ({url})")
    if r.status_code == 429:
        raise QoderInjectError(f"429 Rate limited — wait and retry ({url})")
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"_raw": r.text[:200], "_status": r.status_code}


# ════════════════════════════════════════════════════════════════════
# Core API functions
# ════════════════════════════════════════════════════════════════════

def exchange_pat(personal_token: str, machine_id: Optional[str] = None) -> dict:
    """POST /api/v1/jobToken/exchange {personal_token} → {token, expires_at, ...}."""
    headers = cli_headers(machine_id)
    headers["Content-Type"] = "application/json"
    return _request("POST", f"{OPENAPI}{JOB_TOKEN_EXCHANGE}",
                    headers=headers, json_body={"personal_token": personal_token})


def get_user_plan(token: str, machine_id: Optional[str] = None) -> dict:
    """GET /api/v2/user/plan → user_type (the single source of truth)."""
    try:
        return _request("GET", f"{OPENAPI}{USER_PLAN}", token=token,
                        headers=ide_headers(machine_id))
    except requests.HTTPError:
        return _request("GET", f"{OPENAPI}{USER_PLAN}", token=token,
                        headers=cli_headers(machine_id))


def get_campaigns(token: str, machine_id: Optional[str] = None) -> list:
    """GET /sash/api/v1/me/campaigns → campaigns list."""
    data = _request("GET", f"{WEB}{CAMPAIGNS}", token=token,
                    headers=cli_headers(machine_id))
    if isinstance(data, dict):
        inner = data.get("data") or data
        return inner.get("campaigns") or []
    return []


def claim_campaign(token: str, campaign_id: str,
                   machine_id: Optional[str] = None) -> dict:
    """POST /sash/api/v1/me/campaigns/{id}/claim body {} — claims the benefit."""
    headers = cli_headers(machine_id)
    headers["Content-Type"] = "application/json"
    return _request("POST", f"{WEB}{CAMPAIGNS}/{campaign_id}/claim",
                    token=token, headers=headers, json_body={})


def get_usage(token: str, machine_id: Optional[str] = None) -> dict:
    """GET /sash/api/v2/me/usage → {qoderUsage:{userType, addOnQuota, ...}}."""
    return _request("GET", f"{WEB}{CREDIT_USAGE}", token=token,
                    headers=cli_headers(machine_id))


def get_user_status(token: str, machine_id: Optional[str] = None) -> dict:
    """GET /api/v3/user/status — called by IDE right after login."""
    return _request("GET", f"{OPENAPI}{USER_STATUS}", token=token,
                    headers=ide_headers(machine_id))


# ════════════════════════════════════════════════════════════════════
# Plan verdict
# ════════════════════════════════════════════════════════════════════

def plan_verdict(plan: dict) -> dict:
    """Interpret user_type from /api/v2/user/plan."""
    ut = plan.get("user_type", "?")
    claimed = ut in (
        "personal_professional_trial", "personal_professional",
        "personal_professional_plus", "teams", "enterprise",
        "enterprise_standard", "enterprise_professional",
    )
    label = {
        "personal_professional_trial": "PRO TRIAL (active; IDE badge still 'Free')",
        "personal_professional": "PRO (paid)",
        "personal_professional_plus": "PRO+ (paid)",
        "personal_standard": "FREE — trial NOT claimed yet",
    }.get(ut, ut)
    return {"user_type": ut, "label": label,
            "pro_claimed": claimed and ut != "personal_standard",
            "end_date": plan.get("end_date"),
            "raw": plan}


# ════════════════════════════════════════════════════════════════════
# Orchestration: inject a single PAT
# ════════════════════════════════════════════════════════════════════

def inject_pat(personal_token: str, machine_id: Optional[str] = None,
               claim: bool = True, log=print) -> dict:
    """Full injection: PAT → JT → plan check → claim campaigns → verify usage.

    Returns a result dict suitable for JSONL output.
    """
    mid = machine_id or str(uuid.uuid4())
    result = {"machine_id": mid, "personal_token": personal_token[:12] + "..."}

    # Step 1: Exchange PAT → Job Token
    log(f"[*] exchange PAT → JT ...")
    try:
        exch = exchange_pat(personal_token, mid)
    except QoderInjectError as exc:
        result["error"] = f"exchange: {exc}"
        log(f"    ✗ {exc}")
        return result
    token = exch.get("token")
    if not token:
        result["error"] = f"exchange returned no token: {exch}"
        log(f"    ✗ exchange failed: {json.dumps(exch)[:200]}")
        return result
    result["jt_expires_at"] = exch.get("expires_at")
    log(f"    ✓ JT: {token[:16]}... (expires {exch.get('expires_at')})")

    # Step 2: Read plan (this is where Pro trial status lives)
    log("[*] reading /api/v2/user/plan ...")
    try:
        plan = get_user_plan(token, mid)
        verdict = plan_verdict(plan)
        result["plan"] = {"user_type": verdict["user_type"],
                          "pro_claimed": verdict["pro_claimed"],
                          "end_date": verdict.get("end_date")}
        log(f"    ✓ user_type={verdict['user_type']}")
        log(f"      → {verdict['label']}")
    except (QoderInjectError, requests.HTTPError) as exc:
        result["plan"] = {"error": str(exc)}
        log(f"    ! plan error: {exc}")

    # Step 3: Claim campaigns (the 100 credit/day)
    if claim:
        log("[*] claiming campaigns ...")
        try:
            campaigns = get_campaigns(token, mid)
            eligible = [c for c in campaigns
                        if c.get("actionType") == "CLAIM_BENEFIT"
                        and c.get("claimStatus") != "CLAIMED"]
            log(f"    → {len(eligible)}/{len(campaigns)} eligible campaigns")
            claimed_ids = []
            errors = []
            for c in eligible:
                cid = c.get("id") or c.get("campaignId")
                if not cid:
                    continue
                try:
                    r = claim_campaign(token, cid, mid)
                    ok = (r.get("code") == 0 or
                          (r.get("code") is None and "error" not in json.dumps(r).lower()))
                    if ok:
                        claimed_ids.append(cid)
                        log(f"    ✓ claim {cid}")
                    else:
                        errors.append({"id": cid, "resp": r})
                        log(f"    ✗ claim {cid}: {json.dumps(r, ensure_ascii=False)[:160]}")
                except (QoderInjectError, requests.HTTPError) as exc:
                    errors.append({"id": cid, "error": str(exc)})
                    log(f"    ✗ claim {cid}: {exc}")
            result["claimed"] = claimed_ids
            result["claim_errors"] = errors
        except (QoderInjectError, requests.HTTPError) as exc:
            result["claim_errors"] = [{"error": str(exc)}]
            log(f"    ! campaigns error: {exc}")

        # Step 4: Verify usage (proof that credits landed)
        log("[*] verifying /sash/api/v2/me/usage ...")
        try:
            usage = get_usage(token, mid)
            qu = usage.get("qoderUsage") or {}
            result["usage"] = {"addOnQuota": qu.get("addOnQuota"),
                               "userType": qu.get("userType")}
            log(f"    ✓ addOnQuota={qu.get('addOnQuota')} "
                f"userType={qu.get('userType')}")
        except (QoderInjectError, requests.HTTPError) as exc:
            result["usage"] = {"error": str(exc)}
            log(f"    ! usage error: {exc}")

    return result


# ════════════════════════════════════════════════════════════════════
# PAT file I/O
# ════════════════════════════════════════════════════════════════════

PAT_FILE = ROOT / "accounts" / "qoder_pats.txt"


def load_pats(path: Path) -> list[dict]:
    """Parse PAT file. Each line: pt-xxx|email|machine_id|timestamp or just pt-xxx."""
    if not path.is_file():
        return []
    pats = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        pat = parts[0]
        if not pat.startswith("pt-"):
            continue
        pats.append({
            "pat": pat,
            "email": parts[1] if len(parts) > 1 else "",
            "machine_id": parts[2] if len(parts) > 2 else "",
            "timestamp": parts[3] if len(parts) > 3 else "",
        })
    return pats


def append_pat(pat: str, email: str = "", machine_id: str = "",
               path: Path = None) -> None:
    """Append a PAT entry to the PAT file."""
    path = path or PAT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{pat}|{email}|{machine_id}|{ts}"
    with path.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")


def save_results(results: list[dict], path: Path) -> None:
    """Save injection results as JSONL for the web console."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m github_register.qoder_inject",
        description="Qoder PAT injection — exchange PAT → JT → claim Pro trial "
                    "credits (pure API, no browser).",
    )
    ap.add_argument("--pats", default="", help="PAT file path")
    ap.add_argument("--pat", default="", help="Single PAT (pt-xxx)")
    ap.add_argument("--machine-id", default="", help="Cosy-MachineId (default: new UUID)")
    ap.add_argument("--status-only", action="store_true",
                    help="Only read plan status, skip claiming")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="Delay between PATs in seconds (default: 3)")
    args = ap.parse_args(argv)

    log = lambda msg: print(msg, flush=True)  # noqa: E731

    entries: list[dict] = []
    if args.pat:
        entries = [{"pat": args.pat, "machine_id": args.machine_id}]
    elif args.pats:
        entries = load_pats(Path(args.pats))
    else:
        entries = load_pats(PAT_FILE)

    if not entries:
        log("[!] no PATs found — provide --pat pt-xxx or --pats <file>")
        return 2

    log(f"[*] qoder_inject | {len(entries)} PAT(s) | IDE={IDE_VERSION} "
        f"| CLI={CLI_VERSION} | claim={'no' if args.status_only else 'yes'}")

    results: list[dict] = []
    ok = fail = 0
    for i, entry in enumerate(entries, 1):
        log(f"\n{'═' * 50}")
        log(f"  PAT {i}/{len(entries)}: {entry['pat'][:12]}...")
        log(f"{'═' * 50}")
        try:
            r = inject_pat(
                entry["pat"],
                machine_id=entry.get("machine_id") or args.machine_id or None,
                claim=not args.status_only,
                log=log,
            )
            r["email"] = entry.get("email", "")
            results.append(r)
            if r.get("plan", {}).get("pro_claimed"):
                ok += 1
                log(f"  ✓ PRO TRIAL ACTIVE")
            else:
                fail += 1
                log(f"  ✗ not pro yet: {r.get('plan', {}).get('user_type')}")
        except QoderInjectError as exc:
            results.append({"error": str(exc), "pat": entry["pat"][:12]})
            fail += 1
            log(f"  ✗ {exc}")
        if i < len(entries) and args.delay > 0:
            log(f"  ... waiting {args.delay}s before next PAT")
            time.sleep(args.delay)

    # Save results
    results_file = ROOT / "accounts" / "qoder_inject_results.jsonl"
    save_results(results, results_file)
    log(f"\n[*] done: OK {ok} | FAIL {fail} | results → {results_file.name}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
