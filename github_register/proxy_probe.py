"""Proxy pool reputation probe.

Answers the question a raw "does it connect" check cannot: would GitHub accept
this exit IP for a fresh signup? Two layers, cheapest first.

1. Reachability — can we reach the echo service through the proxy at all?
2. Reputation — GitHub's own judgement, measured WITHOUT signing anything up:
   - `/signup` still serves the form (not the DataDome "enable JS" stub)
   - `api.github.com/zen` returns normally (rate limit left per IP)

Deliberately no registration traffic: probing must not burn pool reputation or
leave orphaned accounts. Every probe is a plain HTTP GET.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import requests

ECHO_URL = "https://api.ipify.org?format=json"
GITHUB_SIGNUP = "https://github.com/signup"
GITHUB_API = "https://api.github.com/zen"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
# Markers of the DataDome interstitial GitHub serves when it distrusts the IP.
_CHALLENGE_MARKERS = (
    "please enable js and disable any ad blocker",
    "captcha-delivery",
    "access is temporarily restricted",
)
_SIGNUP_OK_MARKERS = ("create your account", "signup", "email")


@dataclass
class ProxyResult:
    proxy: str
    ok: bool = False
    exit_ip: str = ""
    country: str = ""
    latency_ms: int = 0
    signup_ok: bool = False
    signup_challenged: bool = False
    api_ok: bool = False
    api_remaining: Optional[int] = None
    error: str = ""
    verdict: str = "dead"  # dead | blocked | usable
    reasons: list = field(default_factory=list)

    def public(self) -> dict:
        """API shape: no credentials ever leave the server."""
        p = urlsplit(self.proxy)
        return {
            "id": f"{p.username or ''}@{p.hostname}:{p.port or ''}",
            "ok": self.ok,
            "exit_ip": self.exit_ip,
            "country": self.country,
            "latency_ms": self.latency_ms,
            "signup_ok": self.signup_ok,
            "signup_challenged": self.signup_challenged,
            "api_ok": self.api_ok,
            "api_remaining": self.api_remaining,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "error": self.error,
        }


def _session_for(proxy: str) -> requests.Session:
    s = requests.Session()
    s.proxies = {"http": proxy, "https": proxy}
    s.headers["User-Agent"] = UA
    return s


def probe_one(proxy: str, timeout: float = 20.0) -> ProxyResult:
    r = ProxyResult(proxy=proxy)
    started = time.time()
    try:
        s = _session_for(proxy)
        echo = s.get(ECHO_URL, timeout=timeout)
        r.exit_ip = (echo.json() or {}).get("ip", "")
        r.latency_ms = int((time.time() - started) * 1000)
        r.ok = bool(r.exit_ip)
    except Exception as exc:
        r.error = f"{type(exc).__name__}: {str(exc)[:90]}"
        r.reasons.append("unreachable")
        return r

    if r.exit_ip:
        try:
            geo = requests.get(f"https://ipinfo.io/{r.exit_ip}/json", timeout=10)
            r.country = (geo.json() or {}).get("country", "")
        except Exception:
            pass

    # GitHub's own verdict, without creating anything.
    try:
        s = _session_for(proxy)
        res = s.get(GITHUB_SIGNUP, timeout=timeout)
        body = (res.text or "")[:4000].lower()
        challenged = any(m in body for m in _CHALLENGE_MARKERS)
        r.signup_challenged = challenged
        r.signup_ok = res.status_code == 200 and not challenged and \
            any(m in body for m in _SIGNUP_OK_MARKERS)
    except Exception as exc:
        r.reasons.append(f"signup: {type(exc).__name__}")
        r.error = r.error or f"{type(exc).__name__}: {str(exc)[:90]}"

    try:
        s = _session_for(proxy)
        res = s.get(GITHUB_API, timeout=timeout)
        r.api_ok = res.status_code == 200
        remaining = res.headers.get("X-RateLimit-Remaining")
        r.api_remaining = int(remaining) if remaining and remaining.isdigit() else None
    except Exception:
        r.api_ok = False

    # Verdict: a proxy that reaches GitHub but is challenged is worse than dead
    # — it wastes a full signup cycle before failing.
    if r.signup_challenged:
        r.verdict = "blocked"
        r.reasons.append("DataDome challenge on /signup")
    elif not r.signup_ok:
        r.verdict = "blocked"
        r.reasons.append("signup page not served")
    elif not r.api_ok:
        r.verdict = "blocked"
        r.reasons.append("api.github.com refused")
    else:
        r.verdict = "usable"
        if r.api_remaining is not None and r.api_remaining < 10:
            r.reasons.append(f"rate limit nearly exhausted ({r.api_remaining})")
    return r


def probe_pool(proxies: list[str], workers: int = 8, limit: int = 0,
               timeout: float = 20.0) -> list[dict]:
    """Probe every proxy concurrently; results keep the input order."""
    todo = proxies[:limit] if limit > 0 else list(proxies)
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(probe_one, p, timeout): p for p in todo}
        results = {}
        for fut in concurrent.futures.as_completed(futures):
            proxy = futures[fut]
            try:
                results[proxy] = fut.result().public()
            except Exception as exc:  # never lose a row to one bad probe
                results[proxy] = ProxyResult(proxy=proxy, error=str(exc)[:90]).public()
    for p in todo:
        out.append(results.get(p) or ProxyResult(proxy=p, error="not probed").public())
    return out


def summarize(results: list[dict]) -> dict:
    """Pool-level health so the UI can answer 'is this pool worth using?'."""
    total = len(results)
    usable = sum(1 for r in results if r["verdict"] == "usable")
    blocked = sum(1 for r in results if r["verdict"] == "blocked")
    dead = sum(1 for r in results if r["verdict"] == "dead")
    countries: dict[str, int] = {}
    for r in results:
        if r.get("country"):
            countries[r["country"]] = countries.get(r["country"], 0) + 1
    ratio = (usable / total) if total else 0.0
    return {
        "total": total,
        "usable": usable,
        "blocked": blocked,
        "dead": dead,
        "usable_ratio": round(ratio, 3),
        # A pool below 30% usable cannot sustain signups: most attempts burn a
        # full cycle on a challenged IP.
        "healthy": ratio >= 0.3,
        "countries": dict(sorted(countries.items(), key=lambda kv: -kv[1])),
    }
