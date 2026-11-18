"""GitHub sign-up automation driven by Camoufox (Firefox anti-detect) + Litensi mail."""
from __future__ import annotations

import json
import contextlib
import hashlib
import logging
import os
import random
import socket
import socketserver
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from camoufox.sync_api import Camoufox
import requests

from .config import Config
from .litensi import LitensiClient, LitensiError
from .mail_errors import MailboxCancelled, MailboxTimeoutError
from .mailcx import MailCxClient, MailCxError
from . import engines
from .engines import EngineUnavailable
from .profiles import (
    generate_password,
    generate_username,
    parse_public_profile,
    username_from_email,
)

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
RECOVERY_DIR = ACCOUNTS_DIR / "recovery"


def _save_recovery_per_account(email: str, recovery: str, log) -> None:
    """Store one account's multiline recovery codes in accounts/recovery/."""
    if not recovery:
        return
    try:
        RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
        (RECOVERY_DIR / f"{key}.txt").write_text(recovery.strip() + "\n", encoding="utf-8")
        log(f"[*] recovery codes saved for {email}")
    except Exception as exc:
        log(f"[i] recovery codes write failed: {exc}")

_EMAIL_INPUTS = ["#email", "input[name='email']", "input[type='email']"]
_PASSWORD_INPUTS = ["#password", "input[name='password']"]
_USERNAME_INPUTS = ["#login", "input[name='login']"]
_OTP_INPUTS = [
    "#otp",
    "input[name='otp']",
    "input[autocomplete='one-time-code']",
    "#launch-code-0",  # verify page: 8 single-digit boxes launch-code-0..7
]
# The main signup form (NOT the Google/Apple OAuth forms which live in their own <form> tags)
_SIGNUP_FORM = "form[action*='signup']"
_SUBMIT_SELECTORS = [f"{_SIGNUP_FORM} button[type='submit']", "#submit", "button[type='submit']"]


class SignupError(RuntimeError):
    pass


class SignupBlocked(SignupError):
    pass


class RegistrationCancelled(SignupError):
    pass


class GitHubRateLimited(SignupError):
    pass


class MailboxAddressRejected(SignupError):
    """GitHub refused the mailbox address itself (already registered).

    Permanent per-address, so every retry fails identically: this account is
    dropped instead of burning page reloads and session switches.
    """


_DATADOME_HARD_BLOCK_MARKERS = (
    "access is temporarily restricted",
    "we detected unusual activity",
    "your access is restricted",
    "you have been temporarily blocked",
    # Indonesian localization of the DataDome block page
    "akses dibatasi untuk sementara",
    "kami mendeteksi aktivitas yang tidak biasa",
    "ada robot di jaringan",
)

_RATE_LIMIT_MARKERS = (
    "secondary rate limit",
    "too many requests",
    "you have exceeded a secondary rate limit",
    "please wait a few minutes before you try again",
)

# GitHub serves this tiny no-JS page from behind DataDome when it decides the
# session is not a real browser. It replaces the whole signup page, so the
# post-submit state never resolves — it must be classified, not waited out.
_JS_REJECTION_MARKERS = (
    "please enable js and disable any ad blocker",
    "enable javascript and cookies to continue",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _raise_if_cancelled(stop) -> None:
    if stop and stop():
        raise RegistrationCancelled("stop requested")


def _sleep_with_cancel(seconds: float, stop=None) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        time.sleep(min(0.25, deadline - time.time()))


def silence_playwright_noise() -> None:
    """Suppress the asyncio 'Task exception was never retrieved' spam.

    Playwright leaves in-flight Channel.send tasks behind when the browser is
    closed mid-operation; asyncio then dumps a TargetClosedError traceback for
    each of them. Harmless noise — filter it at the logging level.
    """
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)


def _parse_proxy(url: str) -> Optional[dict]:
    """'http(s)/socks5(h)://user:pass@host:port' -> Camoufox proxy dict, or None.

    Scheme normalization (Playwright accepts only these):
      socks://   -> socks5://   (bare 'socks' is rejected by Firefox)
      socks5h:// -> socks5://   ('h' variant is a curl/requests-only notation;
                                 Firefox resolves DNS remotely by default)
    """
    url = (url or "").strip()
    if not url:
        return None
    p = urlsplit(url)
    if not p.hostname:
        raise SignupError(f"invalid proxy url: {url}")
    scheme = (p.scheme or "http").lower()
    if scheme in ("socks", "socks5h"):
        scheme = "socks5"
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise SignupError(f"unsupported proxy scheme: {p.scheme}:// (use http/socks5)")
    port = p.port or (1080 if scheme.startswith("socks") else (443 if scheme == "https" else 80))
    proxy = {"server": f"{scheme}://{p.hostname}:{port}"}
    if p.username:
        proxy["username"] = p.username
        proxy["password"] = p.password or ""
    return proxy


def load_proxy_pool(name: str) -> list[str]:
    """Valid proxy URLs from a pool file in project root (one per line, # comments ok)."""
    path = ROOT / name.strip()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = urlsplit(line)
        if p.hostname and (p.scheme or "http").lower() in ("http", "https", "socks4", "socks5"):
            out.append(line)
    return out


def _pick_proxy_url(cfg: Config, log=None) -> str:
    """Effective proxy URL, LOCKED for the current account.

    GitHub fingerprints the network path: a signup that arrives from one exit
    IP and then verifies/logs in from another country reads as account takeover
    and gets suspended. So the first pick for an account is remembered and
    reused by every later stage (engine fallbacks, session switches, relogin,
    CodeBuddy OAuth). `account_proxy_begin()` clears it per account, and the
    rate-limit/hard-block rotation clears it deliberately.
    """
    global _account_proxy
    name = (getattr(cfg, "proxy_file", "") or "").strip()
    if name:
        pool = load_proxy_pool(name)
        if pool:
            if _account_proxy and _account_proxy in pool:
                return _account_proxy
            _account_proxy = random.choice(pool)
            if log:
                log(f"[*] proxy locked for this account: {_mask_proxy(_account_proxy)}")
            return _account_proxy
        if log:
            log(f"[!] proxy file {name!r} missing/empty — falling back to single proxy URL")
    if _account_proxy:
        return _account_proxy
    _account_proxy = (cfg.proxy or "").strip()
    return _account_proxy


def _mask_proxy(url: str) -> str:
    """Identify the proxy without leaking its password.

    These gateways give every session the same host:port and encode the sticky
    session in the USERNAME, so host:port alone cannot tell two picks apart.
    Keep the username, drop the secret.
    """
    p = urlsplit(url)
    if not p.hostname:
        return "(set)"
    user = f"{p.username}@" if p.username else ""
    return f"{user}{p.hostname}:{p.port or ''}"


def account_proxy_begin() -> None:
    """Start a new account: its next proxy pick is locked for the whole cycle."""
    global _account_proxy
    _account_proxy = None


def _proxy_is_socks(proxy: Optional[dict]) -> bool:
    return bool(proxy) and str(proxy.get("server", "")).startswith("socks")


def _socks_exit_ip(url: str, timeout: int = 12) -> str:
    """Resolve the proxy exit IP using the 'socks5h://' scheme (remote DNS).

    DataImpulse (and similar gateways) reject IP-based connections under a
    'ruleset' when the client resolves DNS locally (plain socks5://). The
    requests-based geoip probe inside Camoufox uses plain socks5 and dies with
    '0x02: Connection not allowed by ruleset' — so we look the exit IP up
    ourselves over socks5h and hand it to Camoufox via geoip=<ip>.
    """
    import requests as _requests

    p = urlsplit(url.strip())
    scheme = "socks5h" if (p.scheme or "socks").lower().startswith("socks") else (p.scheme or "http")
    auth = f"{p.username}:{p.password}@" if p.username else ""
    port = p.port or 1080
    proxies = {"http": f"{scheme}://{auth}{p.hostname}:{port}",
               "https": f"{scheme}://{auth}{p.hostname}:{port}"}
    last_exc: Exception | None = None
    # sticky ports can take a few seconds to warm up (allocate the IP) — retry
    for attempt in range(2):
        for check_url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.co/ip"):
            try:
                resp = _requests.get(check_url, proxies=proxies, timeout=20)
                ip = (resp.text or "").strip()
                if resp.ok and ip:
                    return ip
            except Exception as exc:
                last_exc = exc
        if attempt == 0:
            time.sleep(3)  # give the sticky session a moment to warm up
    raise SignupError(f"proxy exit-IP lookup failed: {last_exc} "
                        f"(check scheme http/https/socks5, user:pass, and port in proxies.txt — "
                        f"'405/407' = proxy refuses CONNECT/auth)")


# ---------------------------------------------------------------------------
# Sticky proxy session
#
# Residential gateways such as DataImpulse rotate the exit IP on EVERY TCP
# connection by default (rotating ports 823/824). A browser opens dozens of
# parallel connections — mid-session IP changes are an instant DataDome flag
# ("same cookie, different countries within seconds").
#
# Fix: use a STICKY port instead. DataImpulse assigns ports 10000–20000 for
# sticky SOCKS5 — all connections through the same port exit through the SAME
# IP for the session lifetime (~30 min default).
# ---------------------------------------------------------------------------

_sticky_suffix: Optional[str] = None
_last_exit_ip: Optional[str] = None
_account_proxy: Optional[str] = None  # locked for one account's whole cycle
_DISPLAY_WARNING_SHOWN = False


def _ensure_sticky_proxy(url: str, log=None) -> str:
    """Switch a rotating DataImpulse endpoint to a sticky one.

    DataImpulse docs: rotating = port 823 (HTTP) / 824 (SOCKS5); sticky =
    ports 10000-20000. We pick a random sticky port per process so each job
    gets a fresh stable IP. The port is deterministic within the process.
    """
    p = urlsplit(url.strip())
    port = p.port or 0
    # only switch known rotating ports
    if port in (823, 824):
        global _sticky_suffix
        if _sticky_suffix is None:
            import secrets as _secrets

            _sticky_suffix = str(10000 + int(_secrets.token_hex(4), 16) % 10001)
            if log:
                log(f"[*] sticky proxy port: {_sticky_suffix} (IP stabil ~30 menit, DataImpulse)")
        scheme = (p.scheme or "socks5").lower()
        if scheme in ("socks", "socks5h"):
            scheme = "socks5"
        auth = f"{p.username}:{p.password}@" if p.username else ""
        return f"{scheme}://{auth}{p.hostname}:{_sticky_suffix}"
    return url.strip()  # already sticky or non-DataImpulse — untouched


# ---------------------------------------------------------------------------
# Local auth proxy bridge
#
# Firefox does not support authenticated SOCKS5 proxies ("Browser does not
# support socks5 proxy authentication") and many gateways reject locally
# resolved DNS (socks5://). The bridge listens as a plain local HTTP proxy
# (no auth — Firefox loves that) and relays CONNECT/GET traffic to the
# upstream gateway with the credentials injected, resolving DNS remotely.
# Same pattern as LocalAuthProxyBridge in grok-regkit.
# ---------------------------------------------------------------------------

_UPSTREAM: dict = {}


class _AuthBridgeHandler(socketserver.BaseRequestHandler):
    def _relay(self, src: socket.socket, dst: socket.socket, timeout: float = 180.0) -> None:
        """Bidirectional relay using two pump threads (blocking one-way relay
        deadlocks TLS: the handshake needs simultaneous both-direction I/O)."""
        src.settimeout(timeout)
        dst.settimeout(timeout)

        def pump(a: socket.socket, b: socket.socket) -> None:
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for sock in (a, b):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t1 = threading.Thread(target=pump, args=(src, dst), daemon=True)
        t2 = threading.Thread(target=pump, args=(dst, src), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def _connect_upstream(self) -> socket.socket:
        s = socket.create_connection((_UPSTREAM["host"], _UPSTREAM["port"]), timeout=20)
        if _UPSTREAM["socks"]:
            # minimal SOCKS5 handshake with remote DNS (ATYP=0x03 hostname)
            s.sendall(b"\x05\x01\x02")  # greet: support user/pass auth
            resp = s.recv(2)
            if len(resp) < 2 or resp[0] != 5:
                raise OSError("socks5: bad greeting")
            if resp[1] == 0x02:
                user = _UPSTREAM["user"].encode()
                pwd = _UPSTREAM["pass"].encode()
                s.sendall(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
                resp = s.recv(2)
                if len(resp) < 2 or resp[1] != 0:
                    raise OSError("socks5: auth rejected")
            elif resp[1] != 0x00:
                raise OSError("socks5: no acceptable auth method")
        return s

    def _socks5_connect_remote(self, s: socket.socket, host: str, port: int) -> None:
        """SOCKS5 CONNECT with hostname (ATYP=0x03) so DNS resolves at the gateway."""
        h = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + port.to_bytes(2, "big"))
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            raise OSError(f"socks5: connect failed code={resp[1] if len(resp) > 1 else '?'}")

    def _inject_auth_header(self, data: bytes) -> bytes:
        """Rewrite/add 'Proxy-Authorization: Basic ...' on the first request."""
        if not _UPSTREAM.get("user"):
            return data
        import base64

        token = base64.b64encode(f"{_UPSTREAM['user']}:{_UPSTREAM['pass']}".encode()).decode()
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep:
            return data
        lines = head.split(b"\r\n")
        out = [lines[0]]
        for ln in lines[1:]:
            if ln.lower().startswith(b"proxy-authorization:"):
                continue  # drop existing
            out.append(ln)
        out.append(f"Proxy-Authorization: Basic {token}".encode())
        return b"\r\n".join(out) + b"\r\n\r\n" + rest

    def handle(self) -> None:
        try:
            self.request.settimeout(20)
            first = self.request.recv(65536)
            if not first:
                return
            if first[:7] == b"CONNECT":
                # --- HTTPS tunnel ---
                line = first.split(b"\r\n", 1)[0]
                hostport = line.split()[1].decode()
                host, _, port_s = hostport.rpartition(":")
                port = int(port_s or "443")
                upstream = self._connect_upstream()
                if _UPSTREAM["socks"]:
                    # SOCKS5 CONNECT with remote DNS, then tell the browser the
                    # tunnel is up — do NOT wait for upstream data (deadlock:
                    # upstream waits for the browser's TLS ClientHello).
                    self._socks5_connect_remote(upstream, host, port)
                    self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:
                    # HTTP upstream: forward CONNECT with auth injected, relay
                    # the gateway's own 2xx reply to the browser
                    authed = self._inject_auth_header(first)
                    upstream.sendall(authed)
                    reply = self._wait_http_connect_reply(upstream)
                    self.request.sendall(reply)
                self._relay(self.request, upstream)
            else:
                # --- plain HTTP: forward the full request with auth injected ---
                upstream = self._connect_upstream()
                upstream.sendall(self._inject_auth_header(first))
                self._relay(self.request, upstream)
        except OSError:
            pass
        finally:
            try:
                self.request.close()
            except OSError:
                pass

    @staticmethod
    def _wait_http_connect_reply(upstream: socket.socket, timeout: float = 20.0) -> bytes:
        """Read the upstream HTTP proxy's CONNECT reply (up to the blank line)."""
        upstream.settimeout(timeout)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = upstream.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf or b"HTTP/1.1 502 Bad Gateway\r\n\r\n"


class LocalAuthProxyBridge:
    """Run a local no-auth HTTP proxy that forwards to an authed upstream.

    Use for SOCKS5-with-auth upstreams (Firefox can't authenticate to SOCKS5)
    or HTTP upstreams behind DataDome-style rulesets. DNS for CONNECT is
    resolved at the gateway (hostname-based SOCKS5 ATYP=0x03).
    """

    def __init__(self, proxy_url: str):
        p = urlsplit(proxy_url.strip())
        scheme = (p.scheme or "http").lower()
        if scheme in ("socks", "socks5", "socks5h"):
            scheme = "socks5"
        if not p.hostname:
            raise SignupError(f"invalid proxy url for bridge: {proxy_url}")
        self._upstream = {
            "host": p.hostname,
            "port": p.port or (1080 if scheme == "socks5" else 8080),
            "user": p.username or "",
            "pass": p.password or "",
            "socks": scheme == "socks5",
        }
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self.port: Optional[int] = None

    def start(self) -> int:
        global _UPSTREAM
        _UPSTREAM = self._upstream
        for attempt in range(20):
            candidate = 20000 + (os.getpid() % 10000) + attempt * 7
            try:
                self._server = socketserver.ThreadingTCPServer(
                    ("127.0.0.1", candidate), _AuthBridgeHandler
                )
                self._server.daemon_threads = True
                self.port = candidate
                threading.Thread(target=self._server.serve_forever, daemon=True).start()
                return candidate
            except OSError:
                continue
        raise SignupError("local auth proxy bridge: no free port found")

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def browser_proxy(self) -> dict:
        """Playwright proxy dict pointing at the local bridge (no auth)."""
        return {"server": f"http://127.0.0.1:{self.port}"}


_bridge: Optional[LocalAuthProxyBridge] = None


def _stop_proxy_bridge() -> None:
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None


def _stop_proxy_bridge_if_elsewhere(proxy_url: str) -> None:
    """Drop the bridge when it relays to a different upstream than `proxy_url`.

    The bridge is reused across launches of one account, but the account lock
    can move (rotation, new account): a bridge still pointing at the previous
    exit IP would silently undo the new proxy choice.
    """
    if _bridge is None:
        return
    wanted = _parse_proxy(proxy_url) or {}
    up = _bridge._upstream
    current = f"{'socks5' if up['socks'] else 'http'}://{up['host']}:{up['port']}"
    if wanted.get("server") != current:
        _stop_proxy_bridge()


def _rotate_sticky_proxy() -> None:
    """Discard the current proxy for this account (blocked/rate-limited).

    Clears the account lock too, so the next `_pick_proxy_url` really moves to a
    different exit IP instead of handing back the proxy that just got blocked.
    """
    global _sticky_suffix, _last_exit_ip, _account_proxy
    _stop_proxy_bridge()
    _sticky_suffix = None
    _last_exit_ip = None
    _account_proxy = None


def _disable_blocked_proxy(log) -> None:
    """Tell the proxy rotator to permanently disable the current upstream proxy.

    POST to http://127.0.0.1:8100/disable — the rotator comments out the proxy
    in proxies.txt so it's never used again.
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8100/disable",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if data.get("ok"):
            log(f"[!] permanently disabled proxy: {data.get('disabled')} ({data.get('remaining')} remaining)")
        else:
            log(f"[i] proxy disable: {data}")
    except Exception as exc:
        log(f"[i] could not disable proxy via rotator: {exc}")


def _proxy_needs_bridge(proxy: Optional[dict]) -> bool:
    """Firefox rejects authed SOCKS5; bridge it locally."""
    return bool(proxy) and str(proxy.get("server", "")).startswith("socks") and proxy.get("username")


def _page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _first(page, selectors: list[str], visible: bool = False):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            if not visible or loc.is_visible():
                return loc
        except Exception:
            continue
    raise SignupError(f"no visible element matching {selectors}")


def _wait_step(page, selectors: list[str], label: str, timeout: int = 30) -> None:
    try:
        page.wait_for_selector(", ".join(selectors), state="visible", timeout=timeout * 1000)
    except Exception:
        raise SignupError(f"{label} did not appear; body={_page_text(page)[:300]!r}")



def _advance_form_step(page, selectors: list[str], label: str, log, stop, timeout: int = 30) -> None:
    """GitHub's signup may hide later fields until earlier ones validate.

    Wait for `selectors` to become visible; while hidden, click the signup
    form's Continue/submit button (the step advancer) every ~2s. Raises
    SignupError when the step never appears (reloadable by the Tier-1 loop).
    """
    deadline = time.time() + timeout
    clicked = False
    while True:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        try:
            if page.locator(", ".join(selectors)).first.is_visible():
                if clicked and log:
                    log(f"[*] {label} step appeared")
                return
        except Exception:
            pass
        if time.time() >= deadline:
            break
        try:
            page.locator(", ".join(_SUBMIT_SELECTORS)).first.click(timeout=3000)
            if log and not clicked:
                log(f"[*] clicked Continue — waiting for {label} step")
            clicked = True
        except Exception:
            pass
        _sleep_with_cancel(2, stop)
    raise SignupError(f"signup form did not advance to {label} step within {timeout}s")

def _fill(page, selectors: list[str], value: str) -> None:
    _first(page, selectors, visible=True).fill(value)


def _human_fill(page, selectors: list[str], value: str, stop=None) -> None:
    """Type a signup value progressively so GitHub's async validators run.

    `locator.fill()` injects a whole value in one DOM task. GitHub's signup
    form validates email/password/username and obtains an Octocaptcha token
    asynchronously; instant fills often leave Create account disabled. Typing
    at a modest, consistent pace plus blur matches the normal UI path.
    """
    field = _first(page, selectors, visible=True)
    _raise_if_cancelled(stop)
    # Do not pointer-click inputs here. GitHub's Octocaptcha can briefly place
    # an invisible overlay above a perfectly valid field, causing click() to
    # time out after "performing click action". DOM focus has the same input
    # semantics without needing a pointer target.
    try:
        field.focus(timeout=5_000)
    except Exception:
        field.evaluate("el => el.focus()")
    field.fill("")
    # Fixed cadence is deliberate: rapid random typing is less human than a
    # coherent typing speed. Passwords use the same path but are never logged.
    field.press_sequentially(value, delay=55)
    _raise_if_cancelled(stop)
    try:
        field.evaluate("el => el.blur()")
    except Exception:
        pass


def _is_blank_page(page) -> bool:
    """Has the signup page lost its content entirely?

    GitHub posts the form through an async GraphQL mutation; when that handler
    fails the SPA leaves a blank shell on the /signup URL. No amount of waiting
    renders the form — only a reload does.
    """
    try:
        if (page.locator(_SIGNUP_FORM).count() or
                page.locator("button[type='submit']").count()):
            return False
        return len(_page_text(page).strip()) < 40
    except Exception:
        return False


def _email_taken(page) -> bool:
    """Is the signup email already registered? (permanent, never retryable)"""
    hint = _form_validation_hint(page).lower()
    return "already associated with an account" in hint or \
        "email has already been taken" in hint


def _form_validation_hint(page) -> str:
    """Return a concise visible validation error when Create account is disabled."""
    try:
        alerts = page.locator("[role='alert'], .is-error, .error, .flash-error").all()
        messages = []
        for alert in alerts:
            try:
                text = (alert.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            if text and "may only contain alphanumeric" not in text.lower():
                messages.append(text)
        if messages:
            return " | ".join(messages[:3])[:300]
    except Exception:
        pass
    return ""


def _click_submit(page) -> None:
    """Click the real signup Continue button, never the Google/Apple OAuth buttons.

    The signup page has 3 forms: 2 OAuth (/sessions/social/*) and 1 main
    (action contains 'signup'). Scope the submit click to the main form;
    fall back to legacy selectors for later steps (OTP/preferences pages).
    """
    scoped = page.locator("form[action*='signup'] button[type='submit']").first
    try:
        if scoped.count() and scoped.is_visible() and scoped.is_enabled():
            scoped.click()
            return
    except Exception:
        pass
    _first(page, _SUBMIT_SELECTORS, visible=True).click()


def _reject_blocked(page) -> None:
    """GitHub risk engine may force a 'Login to continue' device interstitial."""
    text = _page_text(page).lower()
    for marker in ("login to continue", "log in with a different device"):
        if marker in text:
            raise SignupBlocked(f"github risk check: {marker}")


def _cancel_order(mail, order_id: str, log) -> None:
    """Cancel the Litensi order after a failed registration.

    For Mail.cx this is a no-op (no order system). Litensi returns HTTP 404
    with ``CANCEL AFTER 4 MINUTES`` when the order is already past the cancel
    window — that is expected on longer flows and is logged at info level.
    """
    if isinstance(mail, LitensiClient) and order_id:
        try:
            mail.set_status(order_id, "CANCELED")
            log(f"[*] litensi order {order_id} canceled")
        except Exception as exc:
            msg = str(exc)
            if "CANCEL AFTER" in msg or "HTTP 404" in msg:
                log(f"[i] litensi order {order_id} already past cancel window "
                    f"(no action needed)")
            else:
                log(f"[i] litensi cancel failed (non-fatal): {exc}")


def _confirm_order(mail, order_id: str, log) -> None:
    """Mark the Litensi order as SUCCESS after the code has been consumed.

    Documented in the Litensi API as ``setstatus SUCCESS``; called only when
    the account was successfully registered end-to-end (per litensi docs the
    order must be confirmed by the caller). Mail.cx has no order concept so
    this is a no-op there.
    """
    if isinstance(mail, LitensiClient) and order_id:
        # Prefer the last order id that actually delivered the code — after a
        # ``reorder`` the original id is no longer the right one to confirm.
        confirm_id = mail.last_order_id or order_id
        try:
            mail.mark_success(confirm_id)
            log(f"[*] litensi order {confirm_id} confirmed (SUCCESS)")
        except Exception as exc:
            msg = str(exc)
            if "HTTP 404" in msg:
                # order already expired / auto-confirmed on the provider side
                log(f"[i] litensi order {confirm_id} setstatus skipped "
                    f"(already past confirm window)")
            elif "SUCCESS FAILED" in msg:
                # the transition was refused — usually because the provider
                # already marked the order SUCCESS (duplicate confirm). Verify
                # with a read-only getstatus instead of guessing.
                try:
                    state = mail.get_status(confirm_id).get("status")
                except Exception:
                    state = ""
                if state == "SUCCESS":
                    log(f"[i] litensi order {confirm_id} already SUCCESS "
                        f"(provider-side confirm; ours was a no-op)")
                else:
                    log(f"[i] litensi confirm failed (non-fatal): {exc} "
                        f"(order status={state or 'unknown'})")
            else:
                log(f"[i] litensi confirm failed (non-fatal): {exc}")


def _is_js_rejection(page) -> bool:
    """DataDome replaced the page with the 'enable JS' stub: the session is
    rejected, not slow. Waiting longer cannot help — retry or rotate."""
    try:
        text = _page_text(page).lower()[:600]
    except Exception:
        return False
    return any(marker in text for marker in _JS_REJECTION_MARKERS)


def _is_hard_block(page) -> bool:
    """DataDome hard block: 'Access is temporarily restricted' — no checkbox to solve."""
    text = ""
    try:
        text = _page_text(page).lower()
    except Exception:
        pass
    return any(marker in text for marker in _DATADOME_HARD_BLOCK_MARKERS)


def _raise_if_rate_limited(page) -> None:
    text = _page_text(page).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        raise GitHubRateLimited(
            "GitHub secondary rate limit reached. Stop the job and wait before trying again; "
            "do not rotate/retry this limit."
        )


def _challenge_hint(page) -> str:
    """Return a short description of the anti-bot page GitHub served, or ''."""
    if "captcha-delivery" in page.url:
        return "DataDome challenge (geo.captcha-delivery.com)"
    try:
        html = page.content()[:2000]
    except Exception:
        html = ""
    if "captcha-delivery" in html or "id=\"cmsg\"" in html:
        return "DataDome challenge page"
    if "cf-chl" in html:
        return "Cloudflare challenge"
    return ""


def _try_click_datadome(page, log) -> None:
    """Click the DataDome checkbox once — never repeatedly.

    DataDome solves itself after ~2-5s when the fingerprint passes. Repeatedly
    clicking the challenge frame resets that solve (every click restarts the
    interstitial), which is exactly how a solvable challenge turns into a
    timeout. So: click only when the interactive checkbox is actually visible,
    and let the frame finish on its own otherwise.
    """
    try:
        for frame in page.frames:
            if "captcha-delivery" not in (frame.url or ""):
                continue
            for sel in ("#ddv1-test-tracking", "input[type='checkbox']",
                        "[id*='checkbox']"):
                loc = frame.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000)
                        log("[*] clicked DataDome checkbox")
                        return
                except Exception:
                    continue
            # No checkbox yet: the interstitial is still deciding. Touching the
            # body here only restarts it, so wait instead of poking.
            return
    except Exception:
        pass


def _form_ready(page) -> bool:
    sel = ", ".join(_EMAIL_INPUTS)
    try:
        return page.locator(sel).first.is_visible()
    except Exception:
        return False


SIGNUP_HOME_URL = "https://github.com/?utm_source=google"
SIGNUP_URL = "https://github.com/signup"
_HARD_BLOCK_MSG = (
    "DataDome HARD BLOCK: 'Access is temporarily restricted' — this IP is "
    "temporarily blocked by GitHub. Change IP, disable VPN/WARP, change network, "
    "or configure a residential proxy and retry."
)


def _wait_for_signup(page, log, stop, seconds: int) -> bool:
    """Wait `seconds` for the email form, poking a DataDome challenge if shown.

    Raises SignupBlocked on a hard block and GitHubRateLimited on a rate limit
    so the caller's retry policy still applies. Returns False on timeout.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        if _is_hard_block(page):
            raise SignupBlocked(_HARD_BLOCK_MSG)
        if _form_ready(page):
            return True
        _try_click_datadome(page, log)
        _sleep_with_cancel(2, stop)
    return False


def _open_signup(page, log, attempts: int = 3, stop=None, headless: bool = False) -> None:
    """Open github.com/signup the way a visitor arrives from a search result.

    Entry is the GitHub homepage with a Google referral query, then the header
    "Sign up" link, so the session carries a referral instead of a cold direct
    hit. A direct /signup load is the fallback when the link is missing or the
    form never renders. A manual solve window is given at the end in headed mode.
    """
    last_hint = ""
    for attempt in range(1, attempts + 1):
        _raise_if_cancelled(stop)
        try:
            log(f"[*] opening {SIGNUP_HOME_URL}")
            page.goto(SIGNUP_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log(f"[!] goto homepage failed ({exc}); retry {attempt}/{attempts}")
        # homepage -> "Sign up" link: the navigation a real visitor follows
        try:
            link = page.get_by_role("link", name="Sign up").first
            if link.count():
                link.click(timeout=10_000)
        except Exception as exc:
            log(f"[i] 'Sign up' link skipped: {exc}")
        if _wait_for_signup(page, log, stop, 20):
            log("[*] github.com/signup email form is ready")
            return
        last_hint = _challenge_hint(page) or last_hint
        if attempt < attempts:
            # fallback: cold direct load, still inside the retry budget
            try:
                _raise_if_cancelled(stop)
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                log(f"[!] direct /signup goto failed: {exc}")
            if _wait_for_signup(page, log, stop, 15):
                log("[*] email form ready on direct /signup load")
                return
            last_hint = _challenge_hint(page) or last_hint
            log(f"[!] {last_hint or 'form not ready'} — reload attempt {attempt + 1}/{attempts}")
    if last_hint:
        if headless:
            # no visible window to solve the challenge in — fail fast instead of
            # burning a manual-solve window that cannot be seen
            raise SignupError(
                f"{last_hint} in headless mode — solve requires a visible browser "
                f"window; use a residential proxy or set headless=false"
            )
        # final wait: an interactive challenge needs a human click in the window
        log(f"[!] {last_hint} — waiting up to 60s; solve the check in the browser window "
            f"if visible, or configure a residential proxy")
        _try_click_datadome(page, log)
        if _wait_for_signup(page, log, stop, 60):
            log("[*] challenge passed, email form is ready")
            return
    raise SignupError(f"email form did not appear ({last_hint or 'no challenge marker'}); "
                      f"IP is blocked by DataDome — use a residential proxy in config")


def _username_error(page) -> str:
    """Return the username validation error shown under the field, or ''.

    IMPORTANT: 'Username may only contain alphanumeric...' is a PERMANENT helper
    (id=username-helper), not an error. Real errors render inside the auto-check
    element above it (role=alert / .is-error text), e.g. 'Username is not
    available' or 'Username xyz is not available'.
    """
    try:
        # error text lives in <auto-check> successors with role=alert
        alerts = page.locator("auto-check [role='alert'], .is-error, [role='alert']").all()
        for a in alerts:
            try:
                txt = (a.inner_text(timeout=1000) or "").strip().lower()
            except Exception:
                continue
            if "username" in txt and "may only contain" not in txt:
                if "not available" in txt or "already taken" in txt:
                    return "taken"
                if txt:
                    return "invalid"
        # fallback: visible error paragraphs mentioning the typed name
        text = _page_text(page)[:1200].lower()
        if "username is not available" in text or "username is already taken" in text:
            return "taken"
        # GitHub also renders the typed name in the message:
        # 'Username wiramerona55 is not available.'
        import re as _re
        if _re.search(r"username\s+\S+\s+is\s+not\s+available", text) or \
                _re.search(r"username\s+\S+\s+is\s+already\s+taken", text):
            return "taken"
    except Exception:
        pass
    return ""


def _dom_click_create_account(page) -> bool:
    """JS .click() on the ENABLED submit button — bypasses pointer hit-testing.

    When the button is enabled, JS click() runs the page's real handler (this
    is how a keyboard Enter on a focused form submits). Unlike force=True it
    does NOT fire a pointer event into whatever overlay covers the button, so
    it cannot trigger the 'Sorry, something went wrong' flash error.
    Returns True when the click landed on an enabled button.
    """
    return bool(
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='signup']");
                const b = form && form.querySelector("button[type='submit']");
                if (!b || b.disabled) return false;
                b.click();
                return true;
            }"""
        )
    )


def _click_create_account(page, log, wait_enabled: int = 30, stop=None) -> None:
    """Click 'Create account' once it is ENABLED.

    GitHub gates the button on the octocaptcha token, so first wait for
    `disabled` to clear. Then submit in the safest order:
      1. native pointer click (most human-like)
      2. JS DOM click on the enabled button — pointer events can be eaten by
         an invisible Octocaptcha/DataDome overlay; DOM click cannot
    A force=True pointer click is deliberately NOT used: it fires a real
    pointer event at the overlay's coordinates and has produced GitHub's
    'Sorry, something went wrong' flash error.
    """
    btn = page.locator("form[action*='signup'] button[type='submit']").first
    deadline = time.time() + wait_enabled
    enabled = False
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        _sleep_with_cancel(0.8, stop)
    if enabled:
        # an invisible/visible Octocaptcha overlay is often what eats the
        # pointer click — poke the captcha frame first so it can finish
        _try_click_datadome(page, log)
        try:
            btn.click(timeout=10_000)
            log("[*] 'Create account' clicked (button enabled)")
            return
        except Exception as exc:
            log(f"[i] native click intercepted ({exc}); trying DOM click on enabled button")
            if _dom_click_create_account(page):
                log("[*] 'Create account' clicked via DOM (overlay bypassed)")
                return
            log("[!] DOM click found the button disabled again — validation regressed")
    # Do not force-submit a disabled form. Its disabled state means GitHub has
    # not completed its email/password/username/Octocaptcha checks yet; forcing
    # it creates false submits, secondary rate-limit pressure, and stuck flows.
    hint = _form_validation_hint(page)
    if _email_taken(page):
        # Reloading cannot fix a registered address: every retry burns a full
        # session and always fails the same way.
        raise MailboxAddressRejected(
            f"signup email is already registered ({hint}); order a new mailbox")
    raise SignupError(
        "Create account stayed disabled after validation wait"
        + (f": {hint}" if hint else " (Octocaptcha or async validation still pending)")
    )


def _fill_and_create_account(page, base_username: str, tries: int, log, stop=None) -> str:
    """Fill username, wait 3s, CLICK 'Create account', verify the page reacts.

    If GitHub answers with a username error, append one digit and retry
    (name -> name2 -> name3 ...). Returns the accepted username once the
    page actually moves past the signup form.
    """
    name = base_username
    for attempt in range(1, tries + 1):
        _raise_if_cancelled(stop)
        _human_fill(page, _USERNAME_INPUTS, name, stop=stop)
        # GitHub debounces username availability; wait for the server result.
        _sleep_with_cancel(3.5, stop)
        _click_create_account(page, log, stop=stop)

        # wait for reaction: error under username field OR page moving forward
        deadline = time.time() + 15
        reacted = False
        while time.time() < deadline:
            _raise_if_cancelled(stop)
            _raise_if_rate_limited(page)
            _sleep_with_cancel(1, stop)
            err = _username_error(page)
            if err == "taken":
                log(f"[*] username {name} taken, retry with +1 digit ({attempt}/{tries})")
                name = f"{base_username}{attempt + 1}"  # name2, name3, ...
                reacted = True
                break
            if err == "invalid":
                raise SignupError(f"username {name} rejected as invalid")
            # page moved on from the signup form -> submit accepted
            if not _form_ready(page):
                return name
            if "signup" not in page.url:
                return name
        if reacted:
            continue  # username was taken — loop with the next suffix
        # no error and no movement: the submit never registered (button still
        # disabled by octocaptcha?) — one JS-click retry, then fail loudly
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='signup']");
                const b = form && form.querySelector("button[type='submit']");
                if (b) b.click();
            }"""
        )
        _sleep_with_cancel(5, stop)
        if not _form_ready(page) or "signup" not in page.url:
            return name
        raise SignupError(
            f"'Create account' did nothing after two clicks (username={name}); "
            f"octocaptcha/DataDome gate never lifted — retry the run or change IP"
        )
    raise SignupError(f"username still taken after {tries} tries (base={base_username})")


def _verify_input_visible(page) -> bool:
    """Is any e-mail verification code input visible? (launch-code page)"""
    for sel in _OTP_INPUTS:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def _verify_page_markers(page) -> bool:
    """Text markers of the email-verification ('launch code') page."""
    try:
        text = _page_text(page)[:2000].lower()
    except Exception:
        return False
    return any(
        m in text
        for m in ("launch code", "verify your email", "check your email",
                  "enter the code", "we sent a code", "verification code")
    )


def _logged_in(context) -> bool:
    """Reliable success signal: GitHub sets cookie logged_in=yes on a real session."""
    try:
        for c in context.cookies():
            if c.get("name") == "logged_in" and str(c.get("value", "")).lower() == "yes":
                return True
    except Exception:
        pass
    return False


def _post_submit_state(page, context) -> str:
    """Classify what GitHub shows after 'Create account'.

    Returns one of:
      'verify' — email verification (launch code) page: code input visible or markers
      'done'   — logged in (cookie logged_in=yes) or a welcome/onboarding page
      'pending'— still transitioning
    """
    if _verify_input_visible(page):
        return "verify"
    if _logged_in(context):
        return "done"
    url = page.url or ""
    text = ""
    try:
        text = _page_text(page)[:2000].lower()
    except Exception:
        pass
    if _verify_page_markers(page):
        return "verify"
    if "signup" in url:
        return "pending"
    # off /signup without verify markers and without login cookie — ambiguous,
    # treat onboarding/welcome/created-successfully text as done, else pending
    if any(m in text for m in ("welcome to github", "let's get started", "get started",
                               "what do you want to do", "your github journey",
                               "your account was created successfully")):
        return "done"
    return "pending"


def _wait_post_submit(page, context, timeout: int = 120, log=None, stop=None) -> str:
    """Wait after submit until the state is stable (not 'pending').

    Anti-race: require the state to hold for 2 consecutive checks (≥4s) before
    deciding, so a mid-transition page can't be misread as 'done'.
    """
    stable_state = ""
    stable_hits = 0
    deadline = time.time() + timeout
    last_log = 0.0
    js_rejections = 0
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        if _is_js_rejection(page):
            # One reload is worth a try (the stub is served before DataDome's
            # cookie lands); a second one means the session is refused outright.
            js_rejections += 1
            if js_rejections > 1:
                raise SignupBlocked(
                    "DataDome refused the session ('Please enable JS and disable any "
                    "ad blocker') — the browser fingerprint or the exit IP is rejected; "
                    "use a residential proxy or run headful so the check can be solved")
            if log:
                log("[!] DataDome JS-rejection stub — reloading once")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            _sleep_with_cancel(3, stop)
            continue
        if _is_blank_page(page):
            raise SignupError(
                "signup page went blank after submit — GitHub's async signup "
                "mutation failed for this session; a page reload is needed")
        state = _post_submit_state(page, context)
        if state != "pending":
            if state == stable_state:
                stable_hits += 1
            else:
                stable_state = state
                stable_hits = 1
            if stable_hits >= 2:
                return state
        else:
            stable_state = ""
            stable_hits = 0
        if log and time.time() - last_log >= 3:
            log(f"[*] post-submit state={state or 'pending'} url={page.url}")
            last_log = time.time()
        _sleep_with_cancel(2, stop)
    raise SignupError(
        f"post-submit state never stabilized; url={page.url} "
        f"body={_page_text(page)[:200]!r}"
    )


def _resolve_headless(cfg: Config, log=None) -> bool:
    """Headful needs a display; when the host has none, start Xvfb before
    giving up and going headless.

    Headless rendering is a Picasso tell, so a virtual display is the better
    fallback: the browser still paints real pixels into an X server, which is
    what DataDome scores. Only when Xvfb is unavailable does the run truly go
    headless — and that is logged, because a DataDome challenge can no longer
    be solved manually from there.
    """
    if cfg.headless:
        return True
    if engines.ensure_display():
        return False
    global _DISPLAY_WARNING_SHOWN
    if log and not _DISPLAY_WARNING_SHOWN:
        _DISPLAY_WARNING_SHOWN = True
        log("[!] no DISPLAY and Xvfb unavailable — falling back to headless; a DataDome "
            "challenge cannot be solved without a visible window (use a residential proxy)")
    return True


def _browser_ctx_options(cfg: Config, log=None) -> dict:
    """Launch options tuned for DataDome (see 2026 field guides):

    - fresh_profile=True: a NEW browser per account (incognito-like, zero
      cached state — no stacked GitHub logins). The DataDome trust cookie is
      carried over separately via .datadome-trust.json (see _save_trust_cookie
      / _restore_trust_cookie) so the signup page keeps loading.
    - persistent profile (fresh_profile=False): keeps the whole profile incl.
      the `datadome` cookie (accumulated trust) — but GitHub sessions stack.
    - geoip=True: timezone/locale aligned with the (proxy) exit IP
    - os=host OS: Picasso canvas hash matches the REAL device class we run on
    - headful by default: headless rendering is a Picasso tell

    SOCKS proxies: Camoufox's own geoip probe uses plain 'socks5://' which many
    gateways (DataImpulse: '0x02 connection not allowed by ruleset') reject
    because DNS is resolved locally. For SOCKS we resolve the exit IP ourselves
    via 'socks5h://' and pass geoip=<ip> so Camoufox skips its probe.
    """
    import platform

    headless = _resolve_headless(cfg, log)
    opts = {"headless": headless, "humanize": True, "geoip": True}
    host_os = platform.system()
    if host_os == "Darwin":
        opts["os"] = "macos"  # canvas/GPU class must match the real machine
    elif host_os == "Linux":
        opts["os"] = "linux"
    elif host_os == "Windows":
        opts["os"] = "windows"
    # sticky session FIRST: one stable exit IP for the whole job — rotating
    # IPs mid-session (DataImpulse default) are an instant DataDome flag
    raw_proxy = _pick_proxy_url(cfg, log=log)
    proxy_url = _ensure_sticky_proxy(raw_proxy, log=log) if raw_proxy else ""
    opts["proxy_url"] = proxy_url  # engines that take a URL string (cloak/damru/...)
    proxy = _parse_proxy(proxy_url) if proxy_url else None
    if proxy:
        if _proxy_needs_bridge(proxy):
            # Firefox cannot authenticate to SOCKS5 — run a local no-auth HTTP
            # bridge that relays to the authed upstream with remote DNS.
            # Reuse an already-running bridge so a NEW bridge is NOT started
            # for every fresh-profile launch (bridge is sticky-session-bound),
            # but rebuild it when the account's locked proxy changed — a bridge
            # to the previous account's exit IP would silently undo the lock.
            global _bridge
            _stop_proxy_bridge_if_elsewhere(proxy_url)
            if _bridge is None:
                _bridge = LocalAuthProxyBridge(proxy_url)
                _bridge.start()
                if log:
                    log(f"[*] local auth bridge 127.0.0.1:{_bridge.port} -> "
                        f"{proxy['server']} (socks5 auth handled locally)")
            opts["proxy"] = _bridge.browser_proxy()
        else:
            opts["proxy"] = proxy
        if _proxy_is_socks(proxy):
            try:
                exit_ip = _socks_exit_ip(proxy_url)
                opts["geoip"] = exit_ip
                global _last_exit_ip
                _last_exit_ip = exit_ip  # consumed by trust-cookie IP binding
                if log:
                    log(f"[*] socks proxy exit IP: {exit_ip} (geoip pinned, sticky)")
            except Exception as exc:
                opts["geoip"] = False
                _last_exit_ip = None  # no IP to bind — do NOT restore stale cookies
                if log:
                    log(f"[!] socks exit-IP lookup failed ({exc}); geoip disabled — "
                        f"timezone/locale may mismatch the proxy country. "
                        f"Trust cookie will NOT be restored (IP unknown).")
    if getattr(cfg, "fresh_profile", False):
        # fresh browser per account — no user_data_dir at all
        if log:
            log("[*] fresh profile mode: new browser without cache (DataDome trust cloned)")
    elif cfg.browser_profile_dir:
        opts["persistent_context"] = True
        opts["user_data_dir"] = str((ROOT / cfg.browser_profile_dir).resolve())
    return opts


# ---------------------------------------------------------------------------
# DataDome trust-cookie carry-over for fresh-profile mode
#
# A brand-new browser has zero cookies — DataDome will challenge it. We persist
# ONLY the `datadome` cookie (+device id) to .datadome-trust.json after each
# successful run and inject it into every fresh context. No GitHub session
# state is ever carried over, so accounts never stack.
# ---------------------------------------------------------------------------

_TRUST_FILE = ROOT / ".datadome-trust.json"
_TRUST_COOKIE_NAMES = {"datadome", "datadome_proxied", "device_id", "_device_id"}

# GitHub's authenticated session. `user_session` is the real credential;
# `logged_in=yes` alone is a UI hint. Captured right after email verification
# so the CodeBuddy OAuth hop reuses it instead of signing in a second time —
# a fresh signup that logs in twice is exactly what GitHub flags.
_SESSION_COOKIE_NAMES = {"user_session", "logged_in", "_gh_sess", "dotcom_user",
                         "__Host-user_session_same_site", "preferred_color_mode"}
SESSION_DIR = ROOT / "accounts" / "sessions"


def _session_file(email: str) -> Path:
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    return SESSION_DIR / f"{digest}.json"


def _capture_github_session(context, email: str, log=None) -> bool:
    """Persist this browser's authenticated GitHub cookies for `email`.

    Returns False when the browser never authenticated (no user_session), which
    is the honest signal — replaying a cookie jar without it does nothing.
    """
    try:
        cookies = [c for c in context.cookies()
                   if c.get("domain", "").endswith("github.com")]
    except Exception as exc:
        if log:
            log(f"[i] session capture skipped: {exc}")
        return False
    if not any(c.get("name") in ("user_session", "__Host-user_session_same_site")
               for c in cookies):
        if log:
            log("[i] no user_session yet — session not captured")
        return False
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _session_file(email).write_text(json.dumps({
            "email": email,
            "cookies": cookies,
            "exit_ip": _last_exit_ip or "",
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }, indent=1), encoding="utf-8")
        try:
            os.chmod(_session_file(email), 0o600)  # a session is a live credential
        except OSError:
            pass
        if log:
            log(f"[*] github session captured ({len(cookies)} cookies)")
        return True
    except Exception as exc:
        if log:
            log(f"[!] session capture failed: {exc}")
        return False


def _load_github_session(email: str) -> list[dict]:
    path = _session_file(email)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("cookies") or []
    except Exception:
        return []


def _inject_github_session(context, email: str, log=None) -> bool:
    """Put the captured GitHub session into a NEW browser context."""
    cookies = _load_github_session(email)
    if not cookies:
        if log:
            log("[i] no stored github session for this account")
        return False
    try:
        context.add_cookies(cookies)
        if log:
            log(f"[*] github session injected ({len(cookies)} cookies)")
        return True
    except Exception as exc:
        if log:
            log(f"[!] session injection failed: {exc}")
        return False



def _save_trust_cookie(context, log=None) -> None:
    """Persist only the DataDome trust cookies, bound to the current exit IP.

    A datadome cookie issued for IP A looks forged when replayed from IP B —
    worse than no cookie at all. We therefore store the exit IP alongside and
    only restore when the IP matches (sticky session keeps it stable in-job).
    """
    try:
        cookies = context.cookies()
        keep = [
            c for c in cookies
            if c.get("name") in _TRUST_COOKIE_NAMES and c.get("domain", "").endswith("github.com")
        ]
        if not keep:
            return
        _TRUST_FILE.write_text(
            json.dumps({
                "cookies": keep,
                "exit_ip": _last_exit_ip or "",
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }),
            encoding="utf-8",
        )
        if log:
            log(f"[*] datadome trust cookie saved ({len(keep)} cookies, ip={_last_exit_ip or 'n/a'})")
    except Exception as exc:
        if log:
            log(f"[i] trust cookie save failed: {exc}")


def _restore_trust_cookie(context, log=None) -> None:
    """Inject persisted DataDome trust cookies — ONLY if the exit IP matches.

    Mismatched IP -> skip silently (a fresh challenge is less suspicious than
    a cookie replayed from the wrong IP). When exit IP is unknown (lookup
    failed), also skip — restoring a stale IP-bound cookie is worse than none.
    """
    try:
        if not _TRUST_FILE.is_file():
            return
        data = json.loads(_TRUST_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not cookies:
            return
        bound_ip = data.get("exit_ip") or ""
        # No current exit IP? Don't guess — skip restore entirely
        if not _last_exit_ip:
            if log:
                log("[i] trust cookie skipped (current exit IP is unknown; lookup failed)")
            return
        if bound_ip and _last_exit_ip and bound_ip != _last_exit_ip:
            if log:
                log(f"[i] trust cookie skipped (bound to IP {bound_ip}, current IP {_last_exit_ip})")
            return
        # context.add_cookies requires url OR domain+path
        clean = []
        for c in cookies:
            cc = {k: c.get(k) for k in ("name", "value", "domain", "path",
                                        "expires", "httpOnly", "secure", "sameSite") if c.get(k) is not None}
            if "domain" not in cc or "path" not in cc:
                cc["domain"] = ".github.com"
                cc["path"] = "/"
            clean.append(cc)
        context.add_cookies(clean)
        if log:
            log(f"[*] datadome trust cookie restored ({len(clean)} cookies, ip={bound_ip or 'unbound'})")
    except Exception as exc:
        if log:
            log(f"[i] trust cookie restore failed: {exc}")


def _headless_chromium_context_kwargs(page_factory, opts: dict) -> dict:
    """Context kwargs that remove the remaining headless tell on Chromium.

    `--disable-blink-features=AutomationControlled` (see engines) already drops
    `navigator.webdriver`; the UA string still carries the `HeadlessChrome`
    token. Patchright deliberately no-ops `add_init_script` (an injected script
    is itself a tell), so the UA override is the whole available surface here.
    """
    if not opts.get("headless"):
        return {}
    probe = page_factory()
    try:
        ua = probe.evaluate("navigator.userAgent")
    finally:
        probe.close()
    if "HeadlessChrome" not in ua:
        return {}
    return {"user_agent": ua.replace("HeadlessChrome", "Chrome")}


def _context_and_page(browser, opts: Optional[dict] = None):
    """Return (context, page) for BOTH launch modes.

    persistent_context=True -> Camoufox returns a BrowserContext with one page
    fresh launch             -> Camoufox returns a Browser; create a context
                                + page ourselves.

    `opts` enables the headless Chromium UA patch; a headful or Camoufox
    context is left exactly as the engine built it.
    """
    opts = opts or {}
    if hasattr(browser, "cookies"):  # BrowserContext (persistent mode)
        context = browser
        pages = getattr(context, "pages", None)
        page = pages[0] if pages else context.new_page()
    else:  # Browser (fresh mode)
        kw = {"locale": "en-US"}
        if getattr(browser, "_headless_chromium", False):
            kw.update(_headless_chromium_context_kwargs(browser.new_page, opts))
        context = browser.new_context(**kw)
        page = context.new_page()
    return context, page


def _clean_github_session_cookies(context, log) -> None:
    """Between accounts: drop GitHub login cookies, keep DataDome/trust cookies.

    A persistent profile survives across accounts, so 'logged_in'/'user_session'
    cookies must be cleared to avoid signing INTO the previous account instead
    of signing UP a new one. DataDome (datadome) cookies are kept — they carry
    the anti-bot trust that lets /signup load at all.
    """
    drop = {"logged_in", "user_session", "__Host-user_session_same_site", "_gh_sess", "dotcom_user"}
    try:
        cookies = context.cookies()
        keep = [c for c in cookies if c.get("name") not in drop]
        if len(keep) == len(cookies):
            return  # nothing to clean
        context.clear_cookies()
        for c in keep:
            try:
                context.add_cookies([c])
            except Exception:
                pass
        log("[*] session cookies cleared (DataDome trust kept)")
    except Exception as exc:
        log(f"[i] cookie cleanup skipped: {exc}")


def _fill_launch_code(page, code: str, log) -> None:
    """Fill the 8-box launch-code page (one digit per #launch-code-N input).

    Falls back to a single OTP input when the boxes are not present.
    """
    boxes = page.locator("input[id^='launch-code-']")
    count = boxes.count()
    if count >= len(code):  # 8 boxes for an 8-digit code
        for i, digit in enumerate(code):
            try:
                box = boxes.nth(i)
                box.click(timeout=3000)
                # explicit short timeouts: a stalled fill() used the page's
                # 20s default and let later boxes miss digits unlogged
                box.fill("", timeout=3000)
                box.press_sequentially(digit, delay=60, timeout=5000)
            except Exception:
                # GitHub re-renders the boxes after the last digit; a stale
                # handle here means the code was already accepted.
                pass
            time.sleep(0.12)  # small human cadence between boxes
        # READBACK: a box that silently missed its digit submits a wrong code
        # and GitHub just re-shows the verify page — indistinguishable from a
        # flagged account. Repair mismatches via the native value setter so
        # React state sees every digit before submit.
        mismatch = []
        for i, digit in enumerate(code):
            try:
                if boxes.nth(i).input_value(timeout=2000) != digit:
                    mismatch.append(i)
            except Exception:
                mismatch.append(i)
        if mismatch:
            log(f"[!] launch code readback mismatch in boxes {mismatch} — repairing")
            try:
                page.evaluate(
                    """([pairs]) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, "value").set;
                        const els = document.querySelectorAll("input[id^='launch-code-']");
                        for (const [i, d] of pairs) {
                            const el = els[i];
                            if (!el) continue;
                            setter.call(el, d);
                            el.dispatchEvent(new Event("input", {bubbles: true}));
                        }
                    }""",
                    [[[i, code[i]] for i in mismatch]],
                )
            except Exception as exc:
                log(f"[!] launch code repair failed: {exc}")
        log(f"[*] launch code typed into {len(code)} boxes")
        time.sleep(1.0)
        try:
            still_here = _verify_input_visible(page) and "account_verifications" in (page.url or "")
        except Exception:
            still_here = False  # DOM navigated away mid-check
        if not still_here:
            log("[*] launch code auto-submitted")
            return
        submit_sel = (
            "button[class*='Button--primary'], "
            "button[class*='Button-module__Button--primary'], "
            "form button[type='submit'], "
            "form input[type='submit']"
        )
        btn = page.locator(submit_sel).first
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                btn.click(timeout=4000)
                log("[*] launch code submitted")
                return
        except Exception as exc:
            log(f"[i] launch code button click failed ({exc}); trying DOM/form submit")
        submitted = page.evaluate(
            """(sel) => {
                const b = document.querySelector(sel);
                if (b && !b.disabled) { b.click(); return 'dom-button'; }
                const box = document.querySelector("input[id^='launch-code-']");
                const form = box && box.form;
                if (form) {
                    if (typeof form.requestSubmit === 'function') form.requestSubmit();
                    else form.submit();
                    return 'form-submit';
                }
                return '';
            }""",
            submit_sel,
        )
        if submitted:
            log(f"[*] launch code submitted ({submitted})")
        else:
            try:
                boxes.nth(len(code) - 1).press("Enter")
                log("[*] launch code submitted (Enter)")
            except Exception:
                log("[*] no submit button or form found — waiting for auto-submit")
        return
    # single input fallback
    otp = _first(page, _OTP_INPUTS, visible=True)
    if not otp.input_value():
        otp.fill(code)
    _click_submit(page)
    log("[*] OTP submitted")


_LOGIN_INPUTS = ["#login_field", "input[name='login']", "input#login"]
_LOGIN_PASS_INPUTS = ["#password", "input[name='password']", "input[type='password']"]


def _try_login(
    page,
    username: str,
    password: str,
    context,
    log,
    mail=None,
    order_id: str = "",
    email: str = "",
    otp_timeout: int = 240,
    used_codes: Optional[set] = None,
    stop=None,
) -> bool:
    """GitHub sends fresh signups to /login: sign in to obtain logged_in=yes.

    Because ``fresh_profile`` mode gives every account a brand-new browser
    profile, GitHub often treats the resulting login as coming from an
    unrecognized device and challenges it with ANOTHER launch-code email
    (device verification). When that page shows up after the credentials are
    submitted, poll the same mailbox for a NEW code — codes already used
    (e.g. the one from signup) are excluded — and fill it in before waiting
    for the ``logged_in`` session cookie.

    Returns True when the login cookie is present afterwards.
    """
    used = set(used_codes or ())
    try:
        # The caller may be on the landing page (GitHub drops verified new
        # accounts at '/'), so open /login first — otherwise there is no form
        # to fill and the login silently "succeeds" with no session.
        user = page.locator(", ".join(_LOGIN_INPUTS)).first
        if not (user.count() and user.is_visible()):
            log("[*] opening /login for the post-signup session")
            page.goto("https://github.com/login", wait_until="domcontentloaded",
                      timeout=60_000)
            user = page.locator(", ".join(_LOGIN_INPUTS)).first
        if not user.is_visible():
            return _logged_in(context)
        user.fill(username, timeout=5000)
        page.locator(", ".join(_LOGIN_PASS_INPUTS)).first.fill(password, timeout=5000)
        time.sleep(0.5)
        # the sign-in button lives in form[action='/session'] but is NOT
        # type=submit (only Google/Apple are). Click the form's own button.
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='session']");
                if (!form) return;
                // prefer a real submit element, else the last button in the form
                let btn = form.querySelector("input[type='submit'], button:not([type='button'])");
                if (!btn) {
                    const btns = form.querySelectorAll("button");
                    btn = btns[btns.length - 1];
                }
                if (btn) btn.click();
            }"""
        )
        log("[*] login form submitted after signup")
    except Exception as exc:
        log(f"[i] auto-login skipped: {exc}")

    verifications = 0
    while True:
        _raise_if_cancelled(stop)
        try:
            _raise_if_rate_limited(page)
        except Exception:
            raise  # GitHubRateLimited propagates for proxy rotation
        if _logged_in(context):
            return True
        on_verify = False
        try:
            on_verify = _verify_input_visible(page) or _verify_page_markers(page)
        except Exception:
            on_verify = False
        if on_verify:
            if mail is None:
                log("[!] device verification page shown but no mail client available")
                return False
            if verifications >= 2:
                log("[!] device verification requested more than twice — giving up")
                return False
            verifications += 1
            log(f"[*] device verification after login (url={page.url}) — waiting for a new code")
            try:
                code = mail.wait_for_code(
                    order_id,
                    timeout=otp_timeout,
                    log=log,
                    cancel_cb=stop,
                    email=email,
                    exclude_codes=used,
                )
            except RegistrationCancelled:
                raise
            except Exception as exc:
                if stop and stop():
                    raise RegistrationCancelled("cancelled during device verification wait")
                log(f"[!] second launch code never arrived: {exc}")
                return False
            log(f"[*] login verification code: {code}")
            used.add(code)
            _fill_launch_code(page, code, log)
            try:
                state = _wait_post_submit(page, context, timeout=90, log=log, stop=stop)
            except (RegistrationCancelled, GitHubRateLimited):
                raise
            except Exception as exc:
                log(f"[!] post-verification wait failed: {exc}")
                return False
            if state == "verify":
                log("[!] login verification code rejected (still on verify page)")
                return False
            continue
        # no verify page — wait up to 30s for the cookie, breaking early if
        # a verification page appears mid-wait
        deadline = time.time() + 30
        appeared = False
        while time.time() < deadline:
            _raise_if_cancelled(stop)
            if _logged_in(context):
                return True
            try:
                if _verify_input_visible(page) or _verify_page_markers(page):
                    appeared = True
                    break
            except Exception:
                pass
            time.sleep(1.5)
        if not appeared:
            log(f"[!] auto-login not confirmed within 30s — url={page.url}")
            return False


def _create_repository(page, username: str, base_name: str, log) -> str:
    """Stage 4 (user recording): create the first repository on /new.

    The name field auto-generates a suggestion; we type our own name and submit.
    Returns the repository name created.
    """
    def _submit() -> None:
        """Submit the visible enabled repo form without clicking an overlay."""
        btn = page.get_by_role("button", name="Create repository").first
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if btn.count() and btn.is_visible() and btn.is_enabled():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SignupError("Create repository stayed disabled after validation wait")

        try:
            btn.click(timeout=10_000)
            log("[*] 'Create repository' clicked")
            return
        except Exception as exc:
            log(f"[i] repository native click intercepted ({exc}); trying DOM click")

        clicked = bool(page.evaluate(
            """() => {
                const buttons = [...document.querySelectorAll('button')];
                const button = buttons.find((b) =>
                    b.offsetParent !== null && !b.disabled &&
                    (b.textContent || '').trim() === 'Create repository'
                );
                if (!button) return false;
                button.click();
                return true;
            }"""
        ))
        if not clicked:
            raise SignupError("Create repository button was not visible/enabled for DOM click")
        log("[*] 'Create repository' clicked via DOM (overlay bypassed)")

    name = base_name or "hello"
    page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("#repository-name-input", state="visible", timeout=30_000)
    except Exception:
        raise SignupError(f"repo form not found; url={page.url} body={_page_text(page)[:200]!r}")
    inp = page.locator("#repository-name-input").first
    inp.fill(name)
    time.sleep(1.5)  # let GitHub validate + enable the submit button
    try:
        _submit()
    except Exception as exc:
        raise SignupError(f"cannot click 'Create repository': {exc}")
    # success = redirected to /<username>/<repo>
    deadline = time.time() + 30
    while time.time() < deadline:
        url = page.url or ""
        if "/new" not in url and f"/{username}/" in url:
            log(f"[*] repository created: {url}")
            return name
        # name conflict? GitHub shows an error — retry with a numeric suffix
        err = ""
        try:
            err = _page_text(page)[:600].lower()
        except Exception:
            pass
        if "already exists" in err and "/new" in url:
            log(f"[*] repo {name} exists, retry with suffix")
            name = f"{base_name}{int(time.time()) % 10000}"
            page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#repository-name-input", state="visible", timeout=20_000)
            page.locator("#repository-name-input").first.fill(name)
            time.sleep(1.5)
            _submit()
        time.sleep(1)
    raise SignupError(f"repository creation not confirmed; url={page.url}")


def _fetch_public_profile() -> dict[str, str]:
    """Fetch one display identity and one quote without using their credentials."""
    random_user = requests.get("https://randomuser.me/api/", timeout=15).json()
    quote = requests.get("https://zenquotes.io/api/random", timeout=15).json()
    return parse_public_profile(random_user, quote)


def _codebuddy_with_stored_session(cfg: Config, email: str, log) -> bool:
    """Replay the captured GitHub session into a fresh browser, then OAuth.

    Used when the signup browser never established a session (GitHub parks new
    accounts on the marketing page or on /suspended). No credential is replayed
    and no second login happens: only the cookie jar captured at verification.
    """
    cookies = _load_github_session(email)
    if not cookies:
        log("[!] codebuddy: no stored github session to reuse")
        return False
    opts = _browser_ctx_options(cfg, log=log)
    with engines.launch_engine(cfg.engine or "cloakbrowser", cfg, opts, log) as browser:
        context, page = _context_and_page(browser, opts)
        page.set_default_timeout(30_000)
        if getattr(cfg, "fresh_profile", False):
            _restore_trust_cookie(context, log)
        if not _inject_github_session(context, email, log):
            return False
        page.goto("https://github.com/", wait_until="domcontentloaded", timeout=60_000)
        if not _logged_in(context):
            log("[!] codebuddy: injected session is not authenticated "
                "(GitHub did not honour the cookies) — account likely restricted")
            return False
        log("[*] codebuddy: github session live from stored cookies")
        return _login_codebuddy_with_session(page, cfg, log, email)


_CB = "https://www.codebuddy.ai"
_CB_AUTH = (_CB + "/auth/realms/copilot/protocol/openid-connect/auth?client_id=console"
            "&response_type=code&redirect_uri=https%3A%2F%2Fwww.codebuddy.ai%2Flogin%2Fselect"
            "%3Fredirect_uri%3Dhttps%253A%252F%252Fwww.codebuddy.ai%252Fhome"
            "&v=2210&product=codebuddy")


CODEBUDDY_TOKENS_DIR = ROOT / "accounts" / "codebuddy"

_CB_CLI_HEADERS = {
    "X-Domain": "www.codebuddy.ai",
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
    "Accept": "application/json",
}


def _cb_mint_cli_state(page, log) -> str:
    """Create a `platform=CLI` auth state BEFORE the OAuth login.

    Crawled from the harvester: the CLI state must exist before the website
    session authorises it; minting it afterwards gets refused at the bind.
    """
    try:
        r = page.request.post(
            f"{_CB}/v2/plugin/auth/state?platform=CLI",
            data="{}",
            headers={**_CB_CLI_HEADERS, "Content-Type": "application/json",
                     "Host": "www.codebuddy.ai"},
            timeout=30_000,
        )
        body = r.json() if r.ok else {}
        state = ((body.get("data") or {}).get("state") or "")
        if state:
            log(f"[*] codebuddy: CLI state minted ({state[:10]}…)")
        else:
            log(f"[!] codebuddy: CLI state missing (HTTP {r.status})")
        return state
    except Exception as exc:
        log(f"[!] codebuddy: CLI state request failed ({str(exc)[:70]})")
        return ""


def _cb_fetch_cli_token(page, state: str, email: str, log) -> bool:
    """Bind the state to the logged-in session, then poll for the bearer.

    The bind answers 11217 ('RetryFetchToken') while it settles, so the poll is
    bounded and every code is logged — a refused bind shows up as 401 on the
    bind itself rather than as a silent wait.
    """
    if not state:
        return False
    try:
        bind = page.request.get(
            f"{_CB}/console/auth/login?platform=CLI&state={state}&domain=www.codebuddy.ai",
            headers=_CB_CLI_HEADERS, timeout=30_000,
        )
        log(f"[*] codebuddy: CLI bind HTTP {bind.status}")
    except Exception as exc:
        log(f"[!] codebuddy: CLI bind failed ({str(exc)[:70]})")
        return False

    for attempt in range(20):
        try:
            r = page.request.get(
                f"{_CB}/v2/plugin/auth/token?state={state}",
                headers=_CB_CLI_HEADERS, timeout=30_000,
            )
            data = (r.json() if r.ok else {})
            payload = data.get("data") or {}
            token = payload.get("accessToken") or ""
            if token:
                _save_codebuddy_tokens(email, payload, log)
                log(f"[*] codebuddy: TOKEN OK — access {len(token)}c, "
                    f"refresh {len(payload.get('refreshToken') or '')}c")
                return True
            log(f"[i] codebuddy: token poll {attempt + 1}/20 — "
                f"code={data.get('code')} {str(data.get('msg') or '')[:40]}")
        except Exception as exc:
            log(f"[i] codebuddy: token poll {attempt + 1} error ({str(exc)[:50]})")
        _sleep_with_cancel(3, None)
    log("[!] codebuddy: token never arrived after 20 polls")
    return False


def _save_codebuddy_tokens(email: str, payload: dict, log) -> None:
    """Write the CLI bearer next to the accounts file (mode 600)."""
    try:
        CODEBUDDY_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        slug = "".join(c if c.isalnum() else "_" for c in email.lower())
        path = CODEBUDDY_TOKENS_DIR / f"{slug}_cli_tokens.json"
        path.write_text(json.dumps({
            "email": email,
            "accessToken": payload.get("accessToken", ""),
            "refreshToken": payload.get("refreshToken", ""),
            "expiresIn": payload.get("expiresIn", 0),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }, indent=1), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # a bearer token is a live credential
        log(f"[*] codebuddy: tokens saved to accounts/codebuddy/{path.name}")
    except Exception as exc:
        log(f"[!] codebuddy: saving tokens failed ({str(exc)[:60]})")


def _login_codebuddy_with_session(page, cfg: Config, log, email: str = "") -> bool:
    """Sign in to CodeBuddy with the GitHub session that is already live here.

    Crawled flow (the SPA at /login/select never finishes rendering without a
    bootstrap cookie, so it must not be opened directly):

      1. GET  /console/accounts        -> 302 + `session` cookie   (bootstrap)
      2. GET  Keycloak realm auth      -> page with the broker href
      3. GET  /auth/realms/copilot/broker/github/login?...
                                       -> 302 to github.com/login/oauth/authorize
      4. POST the GitHub consent form  (the only step needing a human)

    Steps 1-3 are plain navigations; step 4 is a real click so GitHub sees the
    consent in the authenticated session and nothing is replayed.

    Returns True when CodeBuddy lands back on its own origin, authenticated.
    """
    if not getattr(cfg, "codebuddy_login", False):
        return False
    log("[*] codebuddy: bootstrapping a session cookie")
    try:
        # Bootstrap: this 302 hands out the `session` cookie the SPA waits for.
        page.goto(f"{_CB}/console/accounts", wait_until="commit", timeout=45_000)
        _sleep_with_cancel(2, None)
    except Exception as exc:
        log(f"[!] codebuddy: bootstrap failed ({str(exc)[:80]})")
        return False

    log("[*] codebuddy: opening the CodeBuddy sign-in page")
    try:
        # The bootstrap redirect already lands on this page; re-open it only
        # when it did not, since the page carries the one-shot session_code.
        if "openid-connect/auth" not in (page.url or ""):
            page.goto(_CB_AUTH, wait_until="commit", timeout=45_000)
        _sleep_with_cancel(3, None)
    except Exception as exc:
        log(f"[!] codebuddy: sign-in page failed ({str(exc)[:80]})")

    # The CLI state must exist BEFORE the OAuth authorises the website session;
    # minting it afterwards is refused at the bind (crawled behaviour).
    cli_state = _cb_mint_cli_state(page, log)

    broker = _codebuddy_broker_href(page)
    if broker:
        log("[*] codebuddy: following the GitHub broker hop")
        try:
            page.goto(broker, wait_until="commit", timeout=45_000)
            _sleep_with_cancel(2, None)
        except Exception as exc:
            log(f"[i] codebuddy: broker navigation ({str(exc)[:70]})")

    for step in range(10):
        url = page.url or ""
        # GitHub bounces the consent hop through /login when the browser's
        # session is NOT live. That is the one case this path cannot finish: the
        # session must have been established during signup.
        if "github.com/login" in url and "return_to" in url:
            log("[!] codebuddy: github session is not live here — the OAuth hop "
                "landed on the sign-in form; capture must happen during signup")
            return False
        if "/login/oauth/authorize" in url:
            try:
                page.wait_for_selector(
                    "button[type='submit'], input[type='submit'], "
                    "button:has-text('Authorize')", timeout=15_000)
            except Exception:
                pass
            body = _safe_page_html(page)
            if "This account is flagged" in body:
                log("[!] codebuddy: GitHub refuses OAuth ('account is flagged')")
                return False
            if _approve_github_oauth(page, log):
                _sleep_with_cancel(3, None)
                continue
        if _codebuddy_authenticated(url):
            log(f"[*] codebuddy: signed in ({url.split('?')[0]})")
            if cli_state:
                _cb_fetch_cli_token(page, cli_state, email, log)
            return True
        # Detour pages: broker landing, Keycloak "continue", provider buttons.
        href = _codebuddy_broker_href(page)
        if href:
            try:
                page.goto(href, wait_until="commit", timeout=45_000)
                _sleep_with_cancel(2, None)
                continue
            except Exception:
                pass
        _sleep_with_cancel(2, None)
    log(f"[!] codebuddy: handoff stalled (url={(page.url or '')[:90]})")
    return False


def _safe_page_html(page, limit: int = 20000) -> str:
    try:
        return (page.content() or "")[:limit]
    except Exception:
        return ""


def _codebuddy_broker_href(page, wait_ms: int = 15_000) -> str:
    """The GitHub broker URL offered by the CodeBuddy login page, or ''.

    Crawled: the option is `<a id="social-github">` and its href is ABSOLUTE and
    fully parameterised (tab_id + session_code + client_data). Clicking it does
    nothing under Playwright (the SPA swallows the event), so the href itself is
    what must be followed. The link renders async, so wait for it instead of
    scraping the HTML once.
    """
    try:
        page.wait_for_selector("#social-github", timeout=wait_ms)
        href = page.get_attribute("#social-github", "href") or ""
    except Exception:
        return ""
    href = href.replace("&amp;", "&").strip()
    if not href:
        return ""
    return href if href.startswith("http") else _CB + href


def _codebuddy_authenticated(url: str) -> bool:
    """CodeBuddy origin reached with no auth hop in the URL."""
    u = url or ""
    if "codebuddy.ai" not in u:
        return False
    if any(k in u for k in ("/auth/realms", "/login/select", "/login/oauth", "broker/")):
        return False
    return "/home" in u or "/console" in u


def _approve_github_oauth(page, log) -> bool:
    """Submit the GitHub OAuth consent form for a third-party app."""
    try:
        btn = page.locator(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Authorize'), button:has-text('Allow')").first
        if btn.count() and btn.is_visible():
            btn.click(timeout=8_000)
            log("[*] codebuddy: GitHub OAuth consent approved")
            return True
    except Exception as exc:
        log(f"[i] codebuddy: consent click failed ({str(exc)[:70]})")
    return False


def _visible_dom_click(page, matcher_js: str) -> bool:
    """Click a visible enabled button through DOM when overlays eat pointer input."""
    return bool(page.evaluate(
        f"""() => {{
            const button = [...document.querySelectorAll('button')].find({matcher_js});
            if (!button || button.disabled || button.offsetParent === null) return false;
            button.click();
            return true;
        }}"""
    ))


def _complete_profile(page, username: str, cfg: Config, log) -> None:
    """Set recorded status and public profile fields after 2FA is secured."""
    if not (cfg.set_profile_status or cfg.complete_profile):
        return
    profile = None
    if cfg.complete_profile:
        custom = {
            "name": cfg.profile_name.strip(),
            "bio": cfg.profile_bio.strip(),
            "location": cfg.profile_location.strip(),
        }
        # Avoid external APIs entirely when every profile field is configured.
        profile = _fetch_public_profile() if not all(custom.values()) else {}
        profile = {key: custom[key] or profile[key] for key in custom}
    page.goto(f"https://github.com/{username}", wait_until="domcontentloaded", timeout=60_000)

    if cfg.set_profile_status:
        status = cfg.profile_status.strip() or "On vacation"
        # Recording: profile -> react-partial-anchor button "Set status" ->
        # #user-status-status-input -> portal "Set status" submit button.
        # Do not use the preset chip: it is not present on a fresh profile.
        launcher = page.locator("react-partial-anchor button, button").filter(
            has_text="Set status"
        ).first
        launcher_opened = False
        try:
            launcher.click(timeout=8_000)
            launcher_opened = True
        except Exception:
            launcher_opened = _visible_dom_click(
                page,
                "b => /status/i.test(b.getAttribute('aria-label') || '') || "
                "(b.textContent || '').trim() === 'Set status'",
            )
            if not launcher_opened:
                log("[i] profile status launcher not found; status skipped")
            else:
                log("[*] profile status launcher clicked via DOM")
        if launcher_opened:
            status_input = page.locator("#user-status-status-input").first
            try:
                status_input.wait_for(state="visible", timeout=8_000)
            except Exception:
                raise SignupError("profile status popup did not open")
            status_input.fill(status, timeout=8_000)
            if status_input.input_value(timeout=3_000) != status:
                raise SignupError("profile status input did not retain the configured value")

            submit = page.locator("#__primerPortalRoot__ button").filter(
                has_text="Set status"
            ).last
            try:
                submit.click(timeout=8_000)
            except Exception:
                if not _visible_dom_click(
                    page,
                    "b => b.closest('#__primerPortalRoot') && "
                    "(b.textContent || '').trim() === 'Set status'",
                ):
                    raise SignupError("cannot submit profile status")
                log(f"[*] profile status submitted via DOM: {status}")

            # A successful submit closes the status popup. It is the reliable
            # confirmation independent of profile-page text rendering timing.
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if not status_input.is_visible():
                        log(f"[*] profile status saved: {status}")
                        break
                except Exception:
                    log(f"[*] profile status saved: {status}")
                    break
                time.sleep(0.4)
            else:
                raise SignupError(f"profile status did not save: {status}")

    if not profile:
        return
    edit_button = page.locator("button[name='button']").filter(has_text="Edit profile").first
    try:
        edit_button.click(timeout=10_000)
    except Exception as exc:
        log(f"[i] Edit profile native click intercepted ({exc}); trying DOM click")
        if not _visible_dom_click(
            page,
            "b => (b.textContent || '').trim() === 'Edit profile' || "
            "b.classList.contains('js-profile-editable-edit-button')",
        ):
            raise SignupError("cannot open Edit profile (button not found for DOM click)")
        log("[*] Edit profile clicked via DOM (overlay bypassed)")

    name_input = page.locator("#user_profile_name").first
    bio_input = page.locator("#user_profile_bio").first
    location_input = page.locator("input[name='user[profile_location]']").first
    for field in (name_input, bio_input, location_input):
        field.wait_for(state="visible", timeout=15_000)
    name_input.fill(profile["name"])
    bio_input.fill(profile["bio"])
    location_input.fill(profile["location"])

    try:
        page.locator(f"form[action='/users/{username}'] button").filter(
            has_text="Save"
        ).first.click(timeout=10_000)
    except Exception:
        if not _visible_dom_click(page, "b => (b.textContent || '').trim() === 'Save'"):
            raise SignupError("cannot submit Edit profile")
    try:
        page.wait_for_timeout(1_500)
        # After a successful save, either profile text is rendered or the form
        # retains the saved input value during its partial refresh.
        if profile["name"] not in _page_text(page) and name_input.input_value() != profile["name"]:
            raise SignupError("profile save was not confirmed")
    except SignupError:
        raise
    except Exception:
        pass
    log(f"[*] profile completed: {profile['name']} | {profile['location']}")


def _enable_2fa(page, log) -> tuple[str, str]:
    """Stage 5 (user recording): enable TOTP 2FA and return the secret.

    Flow (from the recording):
      Settings → Password and authentication → 'Enable two-factor authentication'
      → 'Authenticator apps and browser extension' → click 'setup key' to reveal
      the secret in a textfield → READ the secret → compute TOTP via pyotp →
      fill input[name='otp'] → Continue → save recovery codes → 'I have saved my
      recovery codes' → Done.
    """
    import pyotp

    page.goto("https://github.com/settings/security", wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("#settings-frame", state="visible", timeout=30_000)
    except Exception:
        raise SignupError(f"security settings page failed; url={page.url}")

    # 'Enable two-factor authentication' is an <a href> link (NOT a button):
    # /settings/two_factor_authentication/setup/intro — navigate straight to it.
    # NOTE: GitHub REGENERATES the TOTP secret on every load of this page, so
    # read the secret only from the page we actually fill the code into.
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            page.goto(
                "https://github.com/settings/two_factor_authentication/setup/intro",
                wait_until="domcontentloaded", timeout=60_000,
            )
    except Exception:
        pass  # already on the page; proceed

    # wait for the setup wizard (QR code page shows the secret in a hidden dialog)
    try:
        page.wait_for_selector(
            "div[data-target='two-factor-setup-verification.mashedSecret']",
            state="attached",  # present in DOM even while the dialog is closed
            timeout=45_000,
        )
    except Exception:
        raise SignupError(f"2FA setup wizard did not load; url={page.url}")

    # reveal the setup key via the 'setup key' button (mirrors the recording),
    # then read the secret from the dialog's data-target div.
    try:
        page.locator("#dialog-show-two-factor-setup-verification-mashed-secret").first.click(
            timeout=10_000
        )
        time.sleep(0.8)
    except Exception:
        pass  # dialog content is in the DOM even when closed — read anyway

    secret = ""
    try:
        secret = (
            page.locator(
                "div[data-target='two-factor-setup-verification.mashedSecret']"
            ).first.inner_text(timeout=5000)
            or ""
        ).strip()
    except Exception:
        pass
    if not secret:
        # fallback: scan page HTML for a base32-looking secret (16-32 chars)
        import re

        body = ""
        try:
            body = page.content()
        except Exception:
            body = ""
        m = re.search(r"\b([A-Z2-7]{16,32})\b", body or "")
        if m:
            secret = m.group(1)
    if not secret or len(secret) < 16:
        raise SignupError(f"TOTP secret not found (got {secret!r})")
    log(f"[*] TOTP secret captured: {secret}")

    # close the setup-key dialog if it opened
    try:
        page.locator("[aria-label='Close']").first.click(timeout=3000)
    except Exception:
        pass

    # compute the current TOTP code and submit it
    totp = pyotp.TOTP(secret)
    code = totp.now()
    log(f"[*] TOTP code generated: {code}")

    # --- helper: click the VISIBLE enabled wizard button by its label ---
    # The wizard keeps all steps' buttons in the DOM; Playwright's is_visible()
    # is unreliable there, so use the browser's own visibility semantics
    # (offsetParent !== null) to find the ACTIVE step's button.
    def _click_active_wizard_button(label: str) -> bool:
        try:
            clicked = page.evaluate(
                """(label) => {
                    const btns = [...document.querySelectorAll(
                        "button[data-target='single-page-wizard-step.nextButton'], " +
                        "button[data-action='click:two-factor-setup-recovery-codes#onDownloadClick'], " +
                        "button[data-action='click:single-page-wizard-step#onNext']"
                    )];
                    for (const b of btns) {
                        if (b.offsetParent !== null && !b.disabled &&
                            (b.textContent || '').trim().toLowerCase() === label.toLowerCase()) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                label,
            )
            return bool(clicked)
        except Exception:
            return False

    def _click_any_active_next() -> bool:
        try:
            return bool(page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll(
                        "button[data-target='single-page-wizard-step.nextButton']"
                    )];
                    for (const b of btns) {
                        if (b.offsetParent !== null && !b.disabled) { b.click(); return true; }
                    }
                    return false;
                }"""
            ))
        except Exception:
            return False

    def _find_verify_input():
        # The enabled otp input is the one with aria-label (from the old
        # recording); input[name='otp'] is a hidden/disabled twin. Fall back
        # to ANY visible non-hidden input inside the wizard when the recording
        # selectors are stale.
        for sel in (
            "input[aria-label='Verify the code from the app']:not([disabled])",
            "input[name='otp']:not([disabled])",
            "input:not([type='hidden']):not([disabled]):visible",
        ):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    return el
            except Exception:
                pass
        return None

    otp_input = _find_verify_input()
    if otp_input is None:
        # The wizard now starts on an intro step; the verify input renders on
        # the NEXT step (hidden until advanced). Advance once, then retry.
        _click_active_wizard_button("Continue") or _click_any_active_next()
        for _ in range(5):
            time.sleep(1)
            otp_input = _find_verify_input()
            if otp_input:
                break
    if otp_input is None:
        try:
            inputs = page.evaluate(
                """() => [...document.querySelectorAll('input')].map(i => ({
                    name: i.name, id: i.id, type: i.type,
                    aria: i.getAttribute('aria-label'),
                    vis: i.offsetParent !== null
                }))"""
            )
        except Exception:
            inputs = []
        raise SignupError(f"2FA verify input never became visible; inputs={inputs!r}")
    otp_input.fill(code, timeout=10_000)

    if not _click_active_wizard_button("Continue"):
        # fallback: any visible enabled next button (its label may be icon-only)
        _click_any_active_next()
    log("[*] TOTP code submitted → Continue")
    time.sleep(3)

    # ---- recovery codes step ----
    recovery = ""
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            # prefer the dedicated element, else scan the page text
            codes: list[str] = []
            try:
                rc_el = page.locator("two-factor-setup-recovery-codes, [data-target='two-factor-setup-recovery-codes']")
                if rc_el.count():
                    txt = rc_el.first.inner_text(timeout=3000) or ""
                else:
                    txt = _page_text(page)
            except Exception:
                txt = _page_text(page)
            import re as _re

            codes = list(dict.fromkeys(_re.findall(r"\b[a-z0-9]{5,6}-[a-z0-9]{5,6}\b", txt, _re.I)))
            if codes:
                recovery = "\n".join(codes[:16])
                break
            time.sleep(1)
        if recovery:
            log(f"[*] recovery codes captured ({len(recovery.splitlines())} codes)")
    except Exception:
        pass

    # download recovery codes (as recorded), then confirm & finish
    try:
        with page.expect_download(timeout=10_000) as dl_info:
            page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('button')].find(
                        b => b.offsetParent !== null && !b.disabled &&
                             /download/i.test((b.textContent || '').trim())
                    );
                    if (b) b.click();
                }"""
            )
        download = dl_info.value
        log(f"[*] recovery codes downloaded: {download.suggested_filename}")
        try:
            path = str(download.path())
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    dl_text = f.read()
                if dl_text and not recovery:
                    import re as _re

                    codes = list(dict.fromkeys(_re.findall(r"\b[a-z0-9]{5,6}-[a-z0-9]{5,6}\b", dl_text, _re.I)))
                    if codes:
                        recovery = "\n".join(codes[:16])
                        log(f"[*] recovery codes from download ({len(codes)} codes)")
        except Exception:
            pass
    except Exception as exc:
        log(f"[i] recovery codes download skipped: {exc}")

    if not _click_active_wizard_button("I have saved my recovery codes"):
        _click_any_active_next()
    log("[*] recovery codes confirmed")
    time.sleep(2)
    if not _click_active_wizard_button("Done"):
        _click_any_active_next()
    log("[*] 2FA wizard finished")
    time.sleep(2)

    # persist recovery codes next to the accounts file for account recovery
    if recovery:
        try:
            rc_path = ROOT / "github_recovery_codes.txt"
            with rc_path.open("a", encoding="utf-8") as f:
                f.write(f"=== {page.url} @ {datetime.now().isoformat(timespec='seconds')} ===\n")
                f.write(recovery + "\n\n")
            log(f"[*] recovery codes saved to {rc_path.name}")
        except Exception as exc:
            log(f"[i] recovery codes write failed: {exc}")
    return secret, recovery


def _fill_signup_form(page, cfg, email, password, log, stop) -> str:
    """Fill the single-page signup form (email -> password -> username).

    Returns the accepted username. Raises SignupError with a clear reason when
    the form cannot be completed (validation error, overlay, rate limit).
    """
    # Fill in the same order as a person: email -> wait -> password ->
    # wait -> username. Each blur gives GitHub's async form validators and
    # Octocaptcha time to settle before Create account is considered.
    _human_fill(page, _EMAIL_INPUTS, email, stop=stop)
    _sleep_with_cancel(1.5, stop)
    _raise_if_rate_limited(page)
    _advance_form_step(page, _PASSWORD_INPUTS, "password", log, stop)
    _human_fill(page, _PASSWORD_INPUTS, password, stop=stop)
    _sleep_with_cancel(1.5, stop)
    _raise_if_rate_limited(page)
    _advance_form_step(page, _USERNAME_INPUTS, "username", log, stop)
    # 3s pause after username -> CLICK Create account -> on username error
    # append one digit and retry (name -> name2 -> name3 ...)
    return _fill_and_create_account(
        page, username_from_email(email), cfg.max_username_tries, log, stop=stop
    )


def _post_form_flow(
    page, context, cfg: Config, email: str, password: str, username: str,
    mail, order_id: str, log, stop,
) -> tuple[str, str, str]:
    """Everything AFTER the signup form was accepted: email verification
    (launch code), auto-login, first repository (stage 4), TOTP 2FA (stage 5).
    Returns (username, totp_secret, recovery_codes)."""
    # after submit GitHub either shows the email verification (launch code)
    # page, or (high-trust sessions) logs straight in.
    state = _wait_post_submit(page, context, timeout=120, log=log, stop=stop)
    used_codes: set[str] = set()
    if state == "verify":
        log(f"[*] verification page: {page.url}")
        code = mail.wait_for_code(
            order_id,
            timeout=cfg.otp_timeout_sec,
            log=log,
            cancel_cb=stop,
            email=email,
            exclude_codes=used_codes,
        )
        log(f"[*] verification code: {code}")
        used_codes.add(code)
        _fill_launch_code(page, code, log)
        # mail.cx has no order confirmation — code already extracted
        log(f"[*] verification code extracted and submitted")
        # after OTP: must reach a logged-in state
        state2 = _wait_post_submit(page, context, timeout=90, log=log, stop=stop)
        if state2 == "verify":
            # Judge by the SESSION, not by a transient DOM/URL snapshot: the
            # verification page swaps to the marketing shell the moment the
            # code is accepted, and a leftover code input from that swap is not
            # a rejection while logged_in=yes already exists.
            if not _logged_in(context) and _verify_input_visible(page):
                _sleep_with_cancel(3, stop)  # let the post-submit redirect land
                if not _logged_in(context) and _verify_input_visible(page):
                    body_snippet = _page_text(page)[:200]
                    # GitHub's INLINE reason (wrong code vs flagged account)
                    # lives in the flash/alert region — the marketing header
                    # alone cannot tell the two apart.
                    inline = ""
                    try:
                        inline = " | ".join(
                            t.strip()
                            for t in (
                                page.locator("#js-flash-container").inner_text(timeout=3000),
                                page.locator("[role='alert']").first.inner_text(timeout=3000),
                            )
                            if t and t.strip()
                        )
                    except Exception:
                        pass
                    shot = ""
                    try:
                        shot_path = ROOT / "debug" / (
                            "verify_rejected_"
                            + datetime.now().strftime("%Y%m%d_%H%M%S")
                            + ".png"
                        )
                        shot_path.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shot_path))
                        shot = f"; screenshot={shot_path}"
                    except Exception:
                        pass
                    log(
                        f"[!] verify input still visible; body={body_snippet!r}"
                        f"; inline_error={inline!r}{shot}"
                    )
                    raise SignupError(
                        f"verification code rejected (still on verify page): "
                        f"body={body_snippet!r} inline_error={inline!r}{shot}"
                    )
            # Code accepted (session cookie present, or the verify form cleared):
            # never throw away an account that was just created.
            log(f"[*] verification accepted (logged_in={_logged_in(context)}, "
                f"url={page.url}) — continuing to the logged-in check")
        # First chance to keep the session: GitHub is authenticated here even
        # when it lands on the marketing page afterwards.
        _capture_github_session(context, email, log)

    def _finalize(post_login: bool) -> tuple[str, str, str]:
        """Run the optional post-login stages (repo, 2FA, profile).

        ``post_login`` is False when the browser never confirmed the
        logged_in cookie — in that case we still keep the (already verified)
        account credentials but skip stages that require an active session.
        """
        totp_secret = ""
        recovery = ""
        if post_login:
            _capture_github_session(context, email, log)  # refresh after 2FA/login
            # ---- stage 4: create first repository ----
            if cfg.create_repo:
                try:
                    _create_repository(page, username, cfg.repo_name, log)
                except Exception as exc:
                    log(f"[i] create repo stage skipped: {exc}")
            # ---- stage 5: enable TOTP 2FA ----
            if cfg.enable_2fa:
                try:
                    totp_secret, recovery = _enable_2fa(page, log)
                except Exception as exc:
                    log(f"[i] 2FA stage failed (account still saved): {exc}")
            _save_recovery_per_account(email, recovery, log)
            try:
                _complete_profile(page, username, cfg, log)
            except Exception as exc:
                log(f"[i] profile stage skipped (account still saved): {exc}")
        # ---- stage 6: CodeBuddy sign-in ----
        # Runs even without a live session: the captured cookies are replayed
        # into a fresh context, so the OAuth hop never signs in a second time.
        if getattr(cfg, "codebuddy_login", False):
            try:
                if post_login:
                    ok = _login_codebuddy_with_session(page, cfg, log, email)
                else:
                    ok = _codebuddy_with_stored_session(cfg, email, log)
                if ok:
                    log("[+] codebuddy: authenticated for this account")
                else:
                    log("[!] codebuddy: not authenticated (account still saved)")
            except Exception as exc:
                log(f"[i] codebuddy stage skipped (account still saved): {exc}")
        try:
            _save_trust_cookie(context, log)  # persist DataDome trust for the next fresh run
        except Exception as exc:
            log(f"[i] trust-cookie save skipped: {exc}")
        return username, totp_secret, recovery

    # state 'done' required — no more accepting bare redirects
    deadline = time.time() + 60
    login_tried = False
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        if _logged_in(context):
            log("[*] logged_in cookie confirmed — account is active")
            return _finalize(post_login=True)
        # GitHub sends fresh signups to /login: sign in with the new creds
        if "/login" in (page.url or "") or (not login_tried and time.time() > deadline - 25):
            # Second trigger: verification finished without establishing a
            # session (GitHub drops new accounts on the marketing page). The
            # credentials are valid, so sign in explicitly instead of saving an
            # account nobody is logged into — stages 4/5 need that session.
            login_tried = True
            if _try_login(
                page, email, password, context, log,
                mail=mail, order_id=order_id, email=email,
                otp_timeout=cfg.otp_timeout_sec,
                used_codes=used_codes, stop=stop,
            ):
                log("[*] logged_in cookie confirmed after auto-login")
                return _finalize(post_login=True)
            # The account exists on GitHub and its email is verified —
            # matching the README's post-signup-failure policy, keep the
            # credentials instead of discarding them. Stages 4/5 need an
            # active session so they are skipped.
            log("[!] auto-login not completed — saving the verified account "
                "without repo/2FA/profile stages")
            return _finalize(post_login=False)
        if _post_submit_state(page, context) == "pending":
            _sleep_with_cancel(2, stop)
            continue
        if _wait_post_submit(page, context, timeout=20, log=log, stop=stop) == "done":
            continue  # loop will hit the _logged_in check above
        _sleep_with_cancel(2, stop)
    # deadline exhausted: the account is created and email-verified, but the
    # session cookie never appeared and no /login redirect brought us there.
    # Preserve the account rather than discarding it.
    log(f"[!] session cookie not confirmed within 60s — saving the verified "
        f"account (url={page.url})")
    return _finalize(post_login=False)


def _run_signup(
    cfg: Config,
    password: str,
    mail: MailCxClient,
    provider: str,
    pending: dict,
    log,
    stop,
) -> tuple[str, str]:
    """Run the whole sign-up; returns (username, totp).

    GitHub's signup is now a SINGLE page: Email* / Password* / Username* in one
    form (action=/signup?social=false), submit = "Create account" button.
    OAuth (Google/Apple) buttons live in separate <form> tags — never click them.

    The Octocaptcha token sometimes never settles on a given page load — the
    Create account button stays disabled forever. Two-tier retry strategy:

    Tier 1 (fast, cheap): within the SAME browser session, do `page.reload()`
    (Cmd+R equivalent) and re-fill the form with the SAME data (email +
    password + username). Up to `page_reloads` in-session retries.

    Tier 2 (slow, expensive): if Tier 1 exhausts, close the browser and open
    a completely fresh session (new fingerprint / cookies) and try again. Up
    to `session_reloads` full-session restarts.

    Tier 3 (engine fallback): if an engine is not installed (EngineUnavailable)
    or all of its sessions fail (SignupError), the run continues with the NEXT
    engine from `engine_order` (exclusive when `engine` is set). The mailbox is
    ordered once and reused across engines, so a fallback never burns a second
    mailbox.
    """
    page_reloads = 3      # in-session refresh (Cmd+R) attempts before switching session
    session_reloads = 2   # full browser restarts (new fingerprint) after page reloads fail
    engine_keys = engines.resolve_engine_order(cfg)
    log(f"[*] engines: {', '.join(engine_keys)}")
    last_exc: Exception | None = None
    for engine_key in engine_keys:
        _raise_if_cancelled(stop)
        if last_exc is not None:
            log(f"[*] ENGINE fallback -> {engine_key} ({str(last_exc)[:140]})")
        try:
            return _run_signup_with_engine(
                cfg, engine_key, password, mail, provider, pending, log, stop,
                page_reloads=page_reloads, session_reloads=session_reloads,
            )
        except EngineUnavailable as exc:
            last_exc = exc
            log(f"[!] engine {engine_key} unavailable: {exc}")
        except SignupError as exc:
            last_exc = exc
            log(f"[!] engine {engine_key} failed: {exc}")
    msg = str(last_exc)
    if any(k in msg.lower() for k in ("datadome", "challenge", "connection", "timeout")):
        # Every engine died on the NETWORK, not on the form: the locked exit IP
        # is the common factor. Signal the caller to retry with another proxy.
        raise SignupError(f"all engines failed on network/anti-bot: {last_exc}") from last_exc
    raise SignupError(
        f"all engines failed for this account: {last_exc} "
        f"(engines tried: {', '.join(engine_keys)}; a package missing from this "
        f"interpreter says nothing about the other venvs on the host — run the "
        f"server with the venv that has them installed)")


def _run_signup_with_engine(
    cfg: Config,
    engine_key: str,
    password: str,
    mail: MailCxClient,
    provider: str,
    pending: dict,
    log,
    stop,
    page_reloads: int = 3,
    session_reloads: int = 2,
) -> tuple[str, str]:
    """Tier-1 (page reload) + Tier-2 (session switch) loop for ONE engine.

    Raises EngineUnavailable when the engine cannot start at all and
    SignupError when its sessions are exhausted — both make `_run_signup`
    fall back to the next engine in the order.
    """
    last_exc: Exception | None = None
    for session_attempt in range(1, session_reloads + 2):
        _raise_if_cancelled(stop)
        if session_attempt > 1:
            log(f"[*] SESSION switch {session_attempt - 1}/{session_reloads} "
                f"(fresh browser + new fingerprint)")
        opts = _browser_ctx_options(cfg, log=log if session_attempt == 1 else None)
        with engines.launch_engine(engine_key, cfg, opts, log) as browser:
            # works for BOTH modes: persistent context (BrowserContext) and fresh
            # launch (Browser -> new context/page per account)
            context, page = _context_and_page(browser, opts)
            if getattr(cfg, "fresh_profile", False):
                # fresh mode: inject ONLY the DataDome trust cookie (no GitHub state)
                _restore_trust_cookie(context, log)
            else:
                # persistent mode: wipe login state, keep DataDome trust cookies
                _clean_github_session_cookies(context, log)
            page.set_default_timeout(20_000)
            _open_signup(page, log, stop=stop, attempts=2 if session_attempt > 1 else 3, headless=cfg.headless)
            _reject_blocked(page)

            if "email" not in pending:
                # Order the mailbox ONLY now that the form is ready — a Litensi
                # order costs balance and expires in minutes, so never open it
                # while DataDome may still burn time. Created once, reused
                # across Tier-1 reloads, Tier-2 session switches AND engine
                # fallbacks.
                pending["email"], pending["order_id"] = mail.create_mailbox()
                log(f"[*] mailbox: {pending['email']} ({provider})")
            email = pending["email"]

            # --- Tier 1: in-session page reloads with same data ---
            page_last_exc: Exception | None = None
            username: str | None = None
            for page_attempt in range(1, page_reloads + 1):
                _raise_if_cancelled(stop)
                if page_attempt > 1:
                    log(f"[*] PAGE reload {page_attempt - 1}/{page_reloads - 1} "
                        f"(Reload with same data)")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60_000)
                    except Exception as exc:
                        log(f"[!] page.reload() failed ({exc}); falling back to goto()")
                        try:
                            page.goto(
                                "https://github.com/signup",
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                        except Exception as exc2:
                            page_last_exc = SignupError(f"page reload/goto failed: {exc2}")
                            break
                    # wait for the form to be ready again on the reloaded page
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        _raise_if_cancelled(stop)
                        _raise_if_rate_limited(page)
                        if _form_ready(page):
                            break
                        _sleep_with_cancel(1, stop)
                    else:
                        page_last_exc = SignupError("form not ready after page reload")
                        continue
                    _reject_blocked(page)

                try:
                    username = _fill_signup_form(page, cfg, email, password, log, stop)
                    log(f"[*] form submitted: email + password + username={username}")
                    break  # success — leave Tier 1 loop
                except SignupError as exc:
                    msg = str(exc)
                    reloadable = (
                        "stayed disabled" in msg
                        or "went blank" in msg.lower()
                        or "click" in msg.lower()
                        or "overlay" in msg.lower()
                        or "form" in msg.lower()
                    )
                    if reloadable and page_attempt < page_reloads:
                        page_last_exc = exc
                        log(f"[!] page attempt {page_attempt}/{page_reloads} failed "
                            f"({msg[:120]}); will refresh page and retry with same data")
                        continue
                    # either not-reloadable, or Tier 1 exhausted -> propagate to Tier 2 handler
                    page_last_exc = exc
                    break

            if username is None:
                # Tier 1 failed — decide whether to switch session (Tier 2)
                exc = page_last_exc or SignupError("form submit failed with unknown reason")
                msg = str(exc)
                reloadable = (
                    "stayed disabled" in msg
                    or "went blank" in msg.lower()
                    or "click" in msg.lower()
                    or "overlay" in msg.lower()
                    or "form" in msg.lower()
                )
                if reloadable and session_attempt <= session_reloads:
                    last_exc = exc
                    log(f"[!] {page_reloads} page-reloads exhausted; switching SESSION "
                        f"({msg[:120]})")
                    continue  # browser closes here; outer loop starts a fresh one
                raise exc

            # form accepted — continue with the rest of the flow in this same session
            return _post_form_flow(
                page, context, cfg, email, password, username,
                mail, pending["order_id"], log, stop,
            )
            # non-SignupError exceptions propagate immediately (with-block closes browser)
    raise SignupError(
        f"signup form never completed on engine {engine_key} after "
        f"{page_reloads} page-reloads x {session_reloads + 1} sessions: {last_exc}"
    )


def register_one(
    cfg: Config, log: Callable[[str], None], cancel_cb: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """Register one account; returns its one-line account record or None."""
    stop = cancel_cb or (lambda: False)

    # --- create mail client based on provider (order itself is deferred
    # until the signup form is ready — see _run_signup) ---
    provider = getattr(cfg, "mail_provider", "mailcx") or "mailcx"
    if provider == "litensi":
        mail = LitensiClient(
            api_id=cfg.litensi_api_id,
            api_key=cfg.litensi_api_key,
            site=cfg.litensi_site,
            zone=cfg.litensi_zone,
        )
    elif provider == "cloudflare":
        from .cloudflare import CloudflareMailClient
        mail = CloudflareMailClient(
            domains=cfg.cloudflare_domains or [],
            subdomains=cfg.cloudflare_subdomains or [""],
            imap_host=cfg.imap_host,
            imap_user=cfg.imap_user,
            imap_pass=cfg.imap_pass,
        )
    elif provider == "tempik":
        from .tempik import TempikMailClient
        mail = TempikMailClient(
            api_url=cfg.tempik_api,
            domains=cfg.tempik_domains or [],
        )
    else:
        mail = MailCxClient(domain=cfg.mailcx_domain)
    pending: dict = {}

    succeeded = False
    try:
        # One exit IP for this account's whole life (signup -> verify -> login
        # -> CodeBuddy). Rotation mid-cycle is what got fresh accounts suspended.
        account_proxy_begin()
        password = generate_password()
        has_proxy = bool((cfg.proxy or "").strip() or (getattr(cfg, "proxy_file", "") or "").strip())
        hard_left = int(getattr(cfg, "proxy_hard_block_retries", 0) or 0) if has_proxy else 0
        rate_left = int(getattr(cfg, "proxy_rate_limit_retries", 0) or 0) if has_proxy else 0
        while True:
            _raise_if_cancelled(stop)
            try:
                username, totp_secret, recovery = _run_signup(
                    cfg, password, mail, provider, pending, log, stop
                )
                break
            except SignupBlocked as exc:
                if hard_left <= 0:
                    raise
                hard_left -= 1
                log(f"[!] DataDome hard block ({exc}); disabling proxy + rotating, {hard_left} retries left")
                _disable_blocked_proxy(log)
                _rotate_sticky_proxy()
                _sleep_with_cancel(5, stop)
            except GitHubRateLimited as exc:
                if rate_left <= 0:
                    raise
                rate_left -= 1
                log(f"[!] GitHub secondary rate limit ({exc}); rotating sticky proxy/IP, "
                    f"{rate_left} retries left")
                _rotate_sticky_proxy()
                _sleep_with_cancel(8, stop)
            except SignupError as exc:
                # The locked exit IP was the common factor across every engine
                # (DataDome/network). Same account, DIFFERENT proxy — the lock
                # exists to stop mid-cycle rotation, not to doom a bad pick.
                if "all engines failed on network" not in str(exc) or hard_left <= 0:
                    raise
                hard_left -= 1
                log(f"[!] every engine hit the network wall ({str(exc)[:120]}); "
                    f"retrying with another proxy, {hard_left} left")
                _disable_blocked_proxy(log)
                _rotate_sticky_proxy()
                _sleep_with_cancel(5, stop)
        # Recovery codes are stored in accounts/recovery/<email-hash>.txt.
        # This fifth marker lets the account UI show the recovery-code action
        # without exposing the codes in the main account list.
        succeeded = True
        email = pending["email"]
        return f"{email}----{password}----{username}----{totp_secret}----{int(bool(recovery))}"
    except KeyboardInterrupt:
        raise
    except RegistrationCancelled:
        raise
    except GitHubRateLimited:
        raise
    except MailboxCancelled:
        # Stop was pressed while waiting for the verification email — this is
        # a clean cancellation, not a provider failure.
        raise RegistrationCancelled("cancelled while waiting for mail")
    except MailboxAddressRejected as exc:
        # GitHub refused the address itself: retrying cannot help, and it must
        # not abort the batch — fail this account and move on.
        log(f"[-] account failed: {exc}; continuing with the next account")
        return None
    except MailboxTimeoutError as exc:
        # ONE mailbox never received the GitHub code in time. This is a
        # per-account transient failure — fail only this account and let the
        # batch continue. (Fatal provider errors such as a bad key or an empty
        # balance are LitensiError/MailCxError and still abort below.)
        log(f"[-] account failed: mailbox timeout ({exc}); continuing with the next account")
        return None
    except (MailCxError, LitensiError) as exc:
        # Provider failure (empty balance, bad key, no stock): surface the
        # error and stop — retrying the next account would fail identically.
        log(f"[!] mail provider error, aborting: {exc}")
        raise
    except Exception as exc:
        log(f"[-] account failed: {exc}")
        return None
    finally:
        order_id = pending.get("order_id")
        if provider == "litensi" and order_id is not None:
            # Confirm or cancel the Litensi order based on the actual outcome.
            # Before: this branch always canceled — that discarded successful
            # orders (docs say the caller MUST setstatus SUCCESS once the
            # code has been consumed) and produced misleading logs on both
            # sides.
            if succeeded:
                _confirm_order(mail, order_id, log)
            else:
                _cancel_order(mail, order_id, log)
        elif provider != "litensi":
            log(f"[*] mailbox cleanup: no action needed ({provider})")
        # else: form never became ready — no order was ever placed, nothing to settle


def run_job(
    cfg: Config,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    overrides: Optional[dict] = None,
) -> tuple[int, int, Path]:
    """Register `register_count` accounts; returns (ok, fail, output_file).

    `progress_cb(ok, fail)` (optional) is invoked after each account attempt so
    external observers (e.g. the web UI) can render live stats instead of only
    seeing the final totals when the job returns.

    `overrides` applies per-run fields (currently `headless`) on top of the
    saved config, so a console toggle takes effect without rewriting config.json.
    """
    if overrides:
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
    if log is None:
        log = lambda msg: print(f"[{_now()}] {msg}")  # noqa: E731
    stop = cancel_cb or (lambda: False)

    def _emit_progress(ok_count: int, fail_count: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(ok_count, fail_count)
        except Exception as exc:
            # progress reporting must never break the job
            log(f"[i] progress_cb error ignored: {exc}")

    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    out = ACCOUNTS_DIR / f"github_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    ok = fail = 0
    provider = (getattr(cfg, "mail_provider", "mailcx") or "mailcx").strip().lower()
    engine_note = getattr(cfg, "engine", "") or (
        "auto:" + ",".join((getattr(cfg, "engine_order", None) or ["camoufox"])[:3]))
    log(f"[*] github-regkit | engine={engine_note} | mail_provider={provider} "
        f"| headless={cfg.headless} | target={cfg.register_count} | output={out.name}")
    _emit_progress(ok, fail)  # initial snapshot: 0/0
    try:
        for i in range(1, cfg.register_count + 1):
            if stop():
                break
            log(f"--- account {i}/{cfg.register_count} ---")
            line = None
            try:
                line = register_one(cfg, log, stop)
            except KeyboardInterrupt:
                raise
            except RegistrationCancelled:
                log("[!] stop requested — browser flow cancelled")
                break
            except GitHubRateLimited as exc:
                log(f"[!] rate-limit retries exhausted — stopping job: {exc}")
                break
            except (MailCxError, LitensiError) as exc:  # provider-level error: abort job
                log(f"[!] mail provider error, aborting: {exc}")
                break
            if line:
                with out.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                ok += 1
                log(f"[+] {line.split('----')[0]} saved to {out.name}")
            else:
                fail += 1
            log(f"[*] stats: OK {ok} | FAIL {fail}")
            _emit_progress(ok, fail)  # live update after each account
            if i < cfg.register_count and not stop():
                _sleep_with_cancel(cfg.delay_sec, stop)
    except RegistrationCancelled:
        # A web Stop click may arrive during inter-account delay, not only
        # inside register_one. This is expected control flow, not a job error.
        log("[!] stop requested — job ended cleanly")
    finally:
        _stop_proxy_bridge()  # stop the local auth bridge if it was started
        log(f"[*] done: OK {ok} | FAIL {fail}")
        _emit_progress(ok, fail)  # final snapshot
    return ok, fail, out
