"""Finish a CodeBuddy account: write the region and its billing tail, then re-probe.

The token was only half the account. A fresh account answers the agent with

    429 The trial version is not yet activated.

and ConTools (`workbuddy.set_region`, the page's RegisterRegion submit call for call)
is what provisions it — over HTTPS, on the account's own bearer, no browser:

    GET  /console/accounts                                  -> areaInfoComplete? uid?
    POST /console/login/account   {attributes:{countryCode,countryFullName,countryName}}
    GET  /auth/realms/copilot/overseas/user/register?userId=<uid>
    POST /billing/ide/trial                                 -> 0, or 14051 (already done)

`areaInfoComplete` is the client's own "region is set" flag, so an account that already
has one is read-only here — re-writing a billing region is a chance to change it, not a
harmless repeat.

Usage:
    python3 /tmp/cb_activate.py            # then it probes one real turn
    python3 /tmp/cb_activate.py --no-probe
"""
from __future__ import annotations

import argparse
import json
import sys
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
_slug = sys.argv[1] if len(sys.argv) > 1 else "gisbert.pemberton_hushmail.com"
OUT = ROOT / "results" / _slug.replace("@", "_")
TOKENS = OUT / "cli_tokens.json"
ORIGIN = "https://www.codebuddy.ai"
TRIAL_ALREADY_APPLIED = 14051

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Product": "SaaS",
    "X-IDE-Type": "IDE",
    "X-IDE-Name": "IDE",
    "X-CodeBuddy-Request": "1",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Referer": f"{ORIGIN}/app",
}

#: The country the run signed up from, in the three shapes the endpoint wants.
REGION = {"countryCode": ["65"], "countryFullName": ["Singapore"], "countryName": ["sg"]}


def _call(path: str, bearer: str, *, method: str = "POST", body: dict | None = None,
          timeout: int = 30) -> tuple[int, dict]:
    url = path if path.startswith("http") else ORIGIN + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**HEADERS, "Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001 — a network answer is an answer
        return 0, {"error": str(exc)[:140]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true")
    args = ap.parse_args(argv)

    bearer = json.loads(TOKENS.read_text(encoding="utf-8"))["accessToken"]
    print(f"bearer {len(bearer)} chars", flush=True)

    # ── 1. read the account: is the region already set, and what is the uid ──
    status, acc = _call("/console/accounts", bearer, method="GET")
    data = acc.get("data") or {}
    accounts = data.get("accounts") or []
    me = next((a for a in accounts if a.get("type") == "personal"),
              accounts[0] if accounts else {})
    uid = str(me.get("uid") or "")
    complete = bool(me.get("areaInfoComplete") or data.get("areaInfoComplete"))
    print(f"1) /console/accounts -> {status} uid={uid} areaInfoComplete={complete}",
          flush=True)
    if status != 200:
        print(f"   {str(acc)[:200]}", flush=True)
        return 1

    if complete:
        print("   region already complete — nothing to write", flush=True)
    else:
        # ── 2. the region write (the page's RegisterRegion submit) ──────────
        status, resp = _call("/console/login/account", bearer,
                            body={"attributes": REGION})
        print(f"2) POST /console/login/account -> {status} code={resp.get('code')} "
              f"msg={str(resp.get('msg') or '')[:80]}", flush=True)
        if status != 200 or resp.get("code") != 0:
            print(f"   refused: {str(resp)[:250]}", flush=True)
            return 1

        # ── 3. the cloud registration, then the trial ───────────────────────
        st, cl = _call(f"/auth/realms/copilot/overseas/user/register?userId={uid}",
                       bearer, method="GET")
        print(f"3) registerCloud -> {st} code={cl.get('code')}", flush=True)

        st, tr = _call("/billing/ide/trial", bearer, body={})
        code = tr.get("code") if st == 200 else st
        print(f"4) POST /billing/ide/trial -> {st} code={code} "
              f"msg={str(tr.get('msg') or '')[:80]}", flush=True)
        if code not in (0, TRIAL_ALREADY_APPLIED):
            print("   the trial was not granted — see the code above", flush=True)

    if args.no_probe:
        return 0

    # ── 5. the only verdict that counts: one real turn ──────────────────────
    print("\n5) probing one real turn...", flush=True)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "prove.py")],
                          capture_output=True, text=True, timeout=300)
    for line in (proc.stdout or "").splitlines():
        print("   " + line, flush=True)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
