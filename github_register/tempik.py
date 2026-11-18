"""Tempik mailbox provider — the project's own temp-mail API.

Tempik is a Cloudflare Worker temp-mail service built on top of Cloudflare
Email Routing (catch-all -> Worker). It exposes a mail.cx-style HTTP API:

    GET    /api/session                        -> {"sessionId": ...}
    GET    /api/config                         -> {"mailDomains": [...]}
    POST   /api/inboxes       {"domain": d}    -> {"address": ...}
    GET    /api/inboxes/<addr>/messages        -> [{"subject","body",...}]
    DELETE /api/inboxes/<addr>                 -> cleanup

Session-based, no API key. Mail arrives at `random@[sub.]<domain>` via the
zone catch-all and is exposed through the Worker, so no IMAP is needed.

Reference implementation found on the project VPS:
    /opt/grok-batch/tempik_adapter.py  (TempikMail, curl_cffi based)
This module mirrors that interface (create / peek_code / wait_code / cleanup)
onto the provider protocol used by the rest of the kit.
"""
from __future__ import annotations

import random
import re
import time
from typing import Callable, Iterable, Optional
from urllib.parse import quote

import requests

from .mail_errors import MailboxCancelled, MailboxTimeoutError
from .mailcx import MailCxClient  # github-code extractor reuse

DEFAULT_API = "https://tempik.iotsentinel.web.id/api"

# Default fallback domain pool (apex + rotating a/b/c subdomains)

DEFAULT_DOMAINS = [
    "beelze.biz.id", "a.beelze.biz.id", "b.beelze.biz.id", "c.beelze.biz.id",
    "djaitin.web.id", "a.djaitin.web.id", "b.djaitin.web.id", "c.djaitin.web.id",
    "iotsentinel.web.id", "a.iotsentinel.web.id", "b.iotsentinel.web.id", "c.iotsentinel.web.id",
    "karvy.web.id", "a.karvy.web.id", "b.karvy.web.id", "c.karvy.web.id",
    "urban8.site", "a.urban8.site", "b.urban8.site", "c.urban8.site",
    "vagent.biz.id", "a.vagent.biz.id", "b.vagent.biz.id", "c.vagent.biz.id",
]


class TempikMailError(RuntimeError):
    """Fatal provider error (API down, session rejected) — abort the job."""


class TempikMailClient:
    def __init__(
        self,
        api_url: str = DEFAULT_API,
        domains: Optional[list[str]] = None,
        proxy: Optional[str] = None,
        connect: bool = True,
        timeout: float = 20.0,
    ):
        self.api_url = (api_url or DEFAULT_API).rstrip("/")
        self.timeout = timeout
        self.domains = [d.strip().lower().rstrip(".") for d in (domains or []) if d.strip()]
        self._domain_idx = 0
        self.addr: Optional[str] = None
        self._last_email = ""
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._session_id = ""
        if connect:
            self._session_id = self._create_session()
            if not self.domains:
                self.domains = self._fetch_domains()

    # -- session + domains ---------------------------------------------------

    def _create_session(self) -> str:
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                resp = self.session.get(f"{self.api_url}/session", timeout=self.timeout)
                sid = (resp.json() or {}).get("sessionId")
                if sid:
                    self.session.headers["x-session-id"] = sid
                    return sid
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            time.sleep(2)
        raise TempikMailError(f"tempik: cannot create session ({last_exc})")

    def _fetch_domains(self) -> list[str]:
        for _ in range(3):
            try:
                resp = self.session.get(f"{self.api_url}/config", timeout=self.timeout)
                domains = (resp.json() or {}).get("mailDomains") or []
                if domains:
                    # Fresh client instances start _domain_idx at 0, so without
                    # a shuffle every new process/account lands on the SAME
                    # first domain (mailDomains[0] = djaitin.web.id). One
                    # domain hammered by mass signups gets flagged by GitHub
                    # (accounts suspended instantly) while siblings stay clean.
                    random.shuffle(domains)
                    return domains
            except Exception:
                pass
            time.sleep(2)
        return list(DEFAULT_DOMAINS)

    def _next_domain(self) -> str:
        domain = self.domains[self._domain_idx % len(self.domains)]
        self._domain_idx += 1
        return domain

    # -- mailbox -------------------------------------------------------------

    def create_mailbox(self) -> tuple[str, str]:
        """Create an inbox; returns (address, order_id=address)."""
        if not self.domains:
            raise TempikMailError("tempik: no mail domains available")
        if not self._session_id:  # offline/test mode — compose without the API
            domain = self._next_domain()
            addr = f"inbox{self._domain_idx:x}{int(time.time()) % 10000}@{domain}"
            self.addr = addr
            self._last_email = addr
            return addr, addr
        last_exc: Exception | None = None
        for attempt in range(3):
            domain = self._next_domain()
            try:
                resp = self.session.post(
                    f"{self.api_url}/inboxes",
                    json={"domain": domain},
                    timeout=self.timeout,
                )
                data = resp.json() or {}
                addr = data.get("address")
                if addr:
                    self.addr = addr
                    self._last_email = addr
                    return addr, addr
                last_exc = TempikMailError(
                    f"tempik create failed: {data.get('error', data)}")
            except TempikMailError as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            if attempt < 2:
                time.sleep(3 + attempt * 2)
        raise TempikMailError(f"tempik create error: {last_exc}")

    def get_messages(self, address: str) -> list[dict]:
        encoded = quote(address, safe="")
        resp = self.session.get(
            f"{self.api_url}/inboxes/{encoded}/messages", timeout=self.timeout)
        data = resp.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def extract_code(subject: str, body: str) -> str:
        """GitHub-style codes only — no loose fallbacks.

        GitHub sends `XXXX-XXXX` / `XXXX XXXX` (8 digits) or `code: NNNNNN`.
        Loose 6-char alphanumeric patterns (Grok-style) pick false positives
        from ordinary words, so they are deliberately NOT used here.
        """
        return MailCxClient.extract_github_code(f"{subject or ''}\n{body or ''}")

    def wait_for_code(
        self,
        address: str,
        timeout: int = 240,
        poll_interval: int = 5,
        log: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
        email: str = "",  # ignored — present for API compat with other providers
        exclude_codes: Optional[Iterable[str]] = None,
    ) -> str:
        skip = {str(c).strip() for c in (exclude_codes or ()) if str(c).strip()}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_cb and cancel_cb():
                raise MailboxCancelled("cancelled while waiting for mail")
            try:
                for msg in self.get_messages(address):
                    code = self.extract_code(
                        str(msg.get("subject") or ""),
                        str(msg.get("body") or ""))
                    if code and code not in skip:
                        return code
            except Exception as exc:  # noqa: BLE001
                if log:
                    log(f"[i] tempik poll error (retry): {exc}")
            time.sleep(poll_interval)
        raise MailboxTimeoutError(
            f"no GitHub code for {address} after {timeout}s (tempik {self.api_url})")

    # -- provider protocol ---------------------------------------------------

    def mark_success(self, order_id: str) -> dict:
        if order_id:
            try:
                encoded = quote(order_id, safe="")
                self.session.delete(f"{self.api_url}/inboxes/{encoded}",
                                    timeout=self.timeout)
            except Exception:
                pass
        return {"status": "ok", "note": "tempik inbox cleaned up"}

    def set_status(self, order_id: str, status: str) -> dict:
        return self.mark_success(order_id)

    @property
    def last_order_id(self) -> str:
        return self._last_email
