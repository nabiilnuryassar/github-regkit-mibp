"""Cloudflare Email Routing mailbox provider + zone manager.

Mailbox model: Email Routing has no inbox API. The zone catch-all accepts
mail for ANY address — including `local@<sub>.<domain>` once the subdomain
has MX records pointing at Cloudflare's routing MX (route1..3) plus an SPF
TXT — and forwards it (in this setup: to the account's email Worker, or a
destination inbox). The provider therefore INVENTs an address (rotating
domain, subdomain and localpart) and reads the forwarded mail back from the
destination inbox over IMAP (e.g. Gmail).

Token scope: the Cloudflare API token is ONLY used by the management helpers
(`list_zone_state`, `ensure_email_routing`) — the mailbox loop itself never
calls Cloudflare. Minimum permissions for management:

    Zone  -> Email Routing Rules    -> Edit
    Zone  -> Email Routing Settings -> Edit   (enable/disable + settings)
    Zone  -> DNS                    -> Edit    (MX/SPF records)
    Zone  -> Zone                   -> Read    (zone lookup)
    Account -> Email Routing Addresses -> Edit (destination management, optional)

CLI:  python -m github_register.cloudflare ensure
      reads CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONES (comma list),
      CLOUDFLARE_SUBDOMAINS (comma list, default a,b,c), CLOUDFLARE_MAIL_WORKER.
"""
from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import random
import re
import string
import subprocess
import time
from typing import Callable, Iterable, Optional

from .mail_errors import MailboxCancelled, MailboxTimeoutError
from .mailcx import MailCxClient  # reuse its battle-tested code extraction

MX_SERVERS = [
    ("route1.mx.cloudflare.net", 10),
    ("route2.mx.cloudflare.net", 20),
    ("route3.mx.cloudflare.net", 30),
]
SPF_TXT = "v=spf1 include:_spf.mx.cloudflare.net ~all"
API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareMailError(RuntimeError):
    """Fatal provider/config error — the caller aborts the whole job."""


class CloudflareMailClient:
    """Mailbox provider: rotate `<random>@[sub.]<domain>` + read code via IMAP."""

    def __init__(
        self,
        domains: list[str],
        subdomains: Optional[list[str]] = None,
        imap_host: str = "imap.gmail.com",
        imap_user: str = "",
        imap_pass: str = "",
    ):
        domains = [str(d).strip().lower().rstrip(".") for d in domains if str(d).strip()]
        if not domains:
            raise CloudflareMailError(
                "cloudflare_domains kosong — isi daftar domain Email Routing di config.json")
        self.domains = domains
        # "" = apex; "a" = a.<domain> — rotation across subdomains per domain
        self.subdomains = [str(s).strip().lower().strip(".") for s in (subdomains or [""])]
        self.imap_host = imap_host or "imap.gmail.com"
        self.imap_user = imap_user or os.environ.get("IMAP_USER", "")
        # Gmail shows App Passwords as "xxxx xxxx xxxx xxxx"; the spaces are
        # display-only and IMAP rejects them, so normalise before login.
        self.imap_pass = (imap_pass or os.environ.get("IMAP_PASS", "")).replace(" ", "")
        self._last_email = ""

    # -- mailbox ------------------------------------------------------------

    def create_mailbox(self) -> tuple[str, str]:
        """Invent a fresh address; Email Routing catch-all accepts it as-is."""
        local = "".join(
            random.choices(string.ascii_lowercase + string.digits,
                           k=random.randint(8, 12)))
        domain = random.choice(self.domains)
        sub = random.choice(self.subdomains)
        host = f"{sub}.{domain}" if sub else domain
        addr = f"{local}@{host}"
        self._last_email = addr
        return addr, addr  # order_id = the address itself

    @staticmethod
    def extract_github_code(body: str) -> str:
        """GitHub's XXXX-XXXX (8-digit) code, reusing the mail.cx extractor."""
        return MailCxClient.extract_github_code(body or "")

    # -- reading mail -------------------------------------------------------

    def _open_imap(self) -> imaplib.IMAP4_SSL:
        if not self.imap_user or not self.imap_pass:
            raise CloudflareMailError(
                "IMAP credentials missing — set imap_host/imap_user/imap_pass in "
                "config.json or IMAP_HOST/IMAP_USER/IMAP_PASS in .env "
                "(Gmail: buat App Password di myaccount.google.com/apppasswords)")
        imap = imaplib.IMAP4_SSL(self.imap_host, timeout=30)
        try:
            imap.login(self.imap_user, self.imap_pass)
            imap.select("INBOX")
        except imaplib.IMAP4.error as exc:
            try:
                imap.logout()
            except Exception:
                pass
            raise CloudflareMailError(f"IMAP login failed ({exc}) — check "
                                      f"host/user/App Password") from exc
        return imap

    @staticmethod
    def _message_text(raw: bytes) -> tuple[str, str]:
        """Return (to_header, body_text) from a raw RFC822 message."""
        msg = email.message_from_bytes(raw)
        to_parts = email.header.decode_header(msg.get("To") or "")
        to = "".join(
            part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part
            for part, enc in to_parts)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode("utf-8", "ignore") + "\n"
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", "ignore")
        if not body:  # HTML-only mail — strip tags so the code pattern matches
            for part in msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode("utf-8", "ignore") + "\n"
        return to, body

    def wait_for_code(
        self,
        address: str,
        timeout: int = 240,
        poll_interval: int = 15,
        log: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
        email: str = "",  # ignored — present for API compat with other providers
        exclude_codes: Optional[Iterable[str]] = None,
    ) -> str:
        """Poll IMAP for the GitHub launch code sent to `address`."""
        skip = {str(c).strip() for c in (exclude_codes or ()) if str(c).strip()}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_cb and cancel_cb():
                raise MailboxCancelled("cancelled while waiting for mail")
            imap = self._open_imap()
            try:
                ok, data = imap.search(None, "UNSEEN")
                ids = data[0].split() if ok and data and data[0] else []
                for num in reversed(ids[-12:]):  # newest 12 first
                    _, msg_data = imap.fetch(num, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    to, body = self._message_text(raw)
                    if address.lower() not in to.lower() and \
                            address.lower() not in body.lower():
                        continue
                    code = self.extract_github_code(body)
                    if code and code not in skip:
                        try:
                            imap.store(num, "+FLAGS", "\\Seen")
                        except Exception:
                            pass
                        return code
            except CloudflareMailError:
                raise
            except (imaplib.IMAP4.error, OSError, ConnectionError) as exc:
                if log:
                    log(f"[i] imap poll error (retry): {exc}")
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
            time.sleep(poll_interval)
        raise MailboxTimeoutError(
            f"no GitHub code for {address} after {timeout}s (IMAP {self.imap_user})")

    # -- provider protocol (Litensi/mail.cx parity) -------------------------

    def mark_success(self, order_id: str) -> dict:
        return {"status": "ok", "note": "cloudflare email routing has no order system"}

    def set_status(self, order_id: str, status: str) -> dict:
        return self.mark_success(order_id)

    @property
    def last_order_id(self) -> str:
        return self._last_email


# ---------------------------------------------------------------------------
# Zone management (needs the API token; read-only helpers also for diagnostics)
# ---------------------------------------------------------------------------

def cf_request(token: str, method: str, path: str, data: Optional[dict] = None) -> dict:
    args = ["curl", "-s", "-X", method, "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json"]
    if data:
        args += ["-d", json.dumps(data)]
    args.append(f"{API_BASE}{path}")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"success": False, "errors": [{"message": (proc.stdout or "")[:200]}]}


def _zone_id(token: str, domain: str) -> str:
    res = cf_request(token, "GET", f"/zones?name={domain}")
    zones = res.get("result") or []
    return zones[0]["id"] if zones else ""


def list_zone_state(token: str, domains: list[str]) -> list[dict]:
    """Diagnostics: enabled? apex MX? subdomain MX? catch-all action?"""
    out = []
    for domain in domains:
        zid = _zone_id(token, domain)
        if not zid:
            out.append({"domain": domain, "error": "zone not found for token"})
            continue
        routing = cf_request(token, "GET", f"/zones/{zid}/email/routing")
        rules = cf_request(token, "GET", f"/zones/{zid}/email/routing/rules")
        mx = cf_request(token, "GET", f"/zones/{zid}/dns_records?type=MX")
        names = {r.get("name") for r in (mx.get("result") or [])}
        catch = [(r.get("actions"), r.get("enabled")) for r in (rules.get("result") or [])
                 if (r.get("matchers") or [{}])[0].get("type") == "all"]
        out.append({
            "domain": domain,
            "zone_id": zid,
            "enabled": (routing.get("result") or {}).get("enabled"),
            "status": (routing.get("result") or {}).get("status"),
            "apex_mx": domain in names,
            "sub_mx": sorted(n for n in names if n.endswith("." + domain)),
            "catch_all": catch,
        })
    return out


def ensure_email_routing(
    token: str,
    domains: list[str],
    subdomains: Optional[list[str]] = None,
    worker: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Make one domain ready for Email Routing (idempotent).

    * enables Email Routing,
    * creates the apex MX+TXT via the official endpoint,
    * creates MX + SPF TXT for every rotating subdomain,
    * optionally points catch-all at an email Worker (`worker`).
    """
    subs = [str(s).strip().lower().strip(".") for s in (subdomains or ["a", "b", "c"]) if str(s).strip()]
    log = log or (lambda m: None)
    summary = {}
    for domain in domains:
        log(f"== {domain}")
        zid = _zone_id(token, domain)
        if not zid:
            summary[domain] = {"ok": False, "error": "zone not found for token"}
            continue
        res = {"ok": True, "errors": []}

        r = cf_request(token, "POST", f"/zones/{zid}/email/routing/enable")
        res["enable"] = r.get("success")
        res["errors"] += r.get("errors") or []

        r = cf_request(token, "POST", f"/zones/{zid}/email/routing/dns")
        res["apex_dns"] = r.get("success")
        res["errors"] += r.get("errors") or []

        created = []
        for host, pri in MX_SERVERS:
            have = cf_request(token, "GET", f"/zones/{zid}/dns_records?type=MX&name={domain}")
            if not (have.get("result") or []):
                cf_request(token, "POST", f"/zones/{zid}/dns_records",
                           {"type": "MX", "name": domain, "content": host,
                            "priority": pri, "ttl": 1})
        for sub in subs:
            fqdn = f"{sub}.{domain}"
            for host, pri in MX_SERVERS:
                have = cf_request(token, "GET", f"/zones/{zid}/dns_records?type=MX&name={fqdn}")
                if not (have.get("result") or []):
                    cf_request(token, "POST", f"/zones/{zid}/dns_records",
                               {"type": "MX", "name": fqdn, "content": host,
                                "priority": pri, "ttl": 1})
                    created.append(f"MX {fqdn}")
            have = cf_request(token, "GET", f"/zones/{zid}/dns_records?type=TXT&name={fqdn}")
            if not (have.get("result") or []):
                cf_request(token, "POST", f"/zones/{zid}/dns_records",
                           {"type": "TXT", "name": fqdn, "content": SPF_TXT, "ttl": 1})
                created.append(f"TXT {fqdn}")
        res["created"] = created

        if worker:
            r = cf_request(token, "PUT", f"/zones/{zid}/email/routing/rules/catch_all",
                           {"actions": [{"type": "worker", "value": [worker]}],
                            "enabled": True, "matchers": [{"type": "all"}]})
            res["catch_all"] = r.get("success")
            res["errors"] += r.get("errors") or []
            log(f"  catch-all -> worker {worker}: {r.get('success')} {r.get('errors')}")
        summary[domain] = res
    return summary


def _env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def ensure_from_env() -> None:
    """CLI entry: manage zones from .env (CLOUDFLARE_API_TOKEN etc.)."""
    from .dotenv import load_dotenv

    load_dotenv()  # the CLI runs standalone: .env is not loaded by anything else
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise SystemExit("set CLOUDFLARE_API_TOKEN in .env first")
    zones = _env_list("CLOUDFLARE_ZONES")
    if not zones:
        raise SystemExit("set CLOUDFLARE_ZONES (comma list) in .env")
    subs = _env_list("CLOUDFLARE_SUBDOMAINS", "a,b,c")
    worker = os.environ.get("CLOUDFLARE_MAIL_WORKER", "") or None
    summary = ensure_email_routing(token, zones, subs, worker, log=print)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    ensure_from_env()
