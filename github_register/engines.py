"""Browser engine registry — one engine carries a run; fallback order on failure.

Nine selectable engines (keys match the "Browser Engine" panel):

| key                  | label                      | family    | proxy route                          |
|----------------------|----------------------------|-----------|--------------------------------------|
| clawbrowser          | Clawbrowser (CB)           | managed   | managed proxy, one pool key/launch   |
| camoufox             | Camoufox (CF)              | firefox   | custom proxy, no pool key read       |
| nextbrowser          | Nextbrowser (NB)           | app       | desktop orchestrator (not embeddable)|
| cloakbrowser         | CloakBrowser (CL)          | chromium  | custom proxy, no pool key read       |
| aliasmode_cloak      | AliasMode + CloakBrowser   | chromium  | custom proxy, via AliasMode CDP      |
| patchright           | Patchright (PR)            | chromium  | custom proxy, no pool key read       |
| aliasmode_firefox    | AliasMode Firefox (AF)     | firefox   | custom proxy, via AliasMode CDP      |
| damru                | Damru (DM)                 | android   | custom proxy, OS-level fingerprint   |
| fingerprint_chromium | Fingerprint Chromium (FC)  | chromium  | custom proxy, binary on disk         |

Nothing selected (`engine` blank) keeps the automatic order from
`engine_order`. An engine that is not installed, cannot launch, or fails all
its sessions is skipped and the run continues with the next one — the mailbox
is ordered once and reused, so a fallback never burns a second mailbox.

Installation facts (verified against upstream docs, 2026-11):
* Patchright:   pip install patchright && patchright install chromium
                https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python
* CloakBrowser: pip install cloakbrowser (binary auto-downloads ~200 MB on
                first launch; license key via CLOAKBROWSER_LICENSE_KEY or
                `cloakbrowser login`)  https://github.com/CloakHQ/CloakBrowser
* Clawbrowser:  standalone `clawctl` CLI + API key (managed Chromium runtime
                behind a CDP endpoint)  https://github.com/clawbrowser/clawbrowser
* Nextbrowser:  free desktop AI-orchestrator app (macOS/Windows, AGPL-3.0)
                that drives Clawbrowser + Camoufox underneath — not a Python
                engine, so it cannot be embedded here.
                https://github.com/nextbrowser-oss/nextbrowser-app
* AliasMode:    open-source antidetect browser app (Windows/macOS/Linux) with
                a Local API; profiles are attached over CDP. Its Firefox engine
                (aliasmode-firefox) is built on top of Camoufox.
                https://github.com/aliasmode/aliasmode
* Damru:        Android-native stealth (real Android in Redroid Docker) —
                Linux only, Ubuntu 24.04 recommended.
                pip install git+https://github.com/akwin1234/damru.git
* Fingerprint Chromium: prebuilt Ungoogled-Chromium fork binaries (Windows
                installer/ZIP, Linux AppImage/tar.xz, macOS dmg) from GitHub
                Releases; no pip package. https://github.com/adryfish/fingerprint-chromium
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional


class EngineUnavailable(RuntimeError):
    """Engine not installed / cannot launch; the caller falls back to the next."""


def _clawctl_has_api_key() -> bool:
    """Clawbrowser stores its key in `clawctl config set` (not the env alone)."""
    path = Path.home() / ".config" / "clawbrowser" / "config.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    return bool(data.get("api_key") or data.get("apiKey"))


def _cloakbrowser_has_key() -> bool:
    """Is a CloakBrowser license key configured (env or `cloakbrowser login`)?"""
    try:
        from cloakbrowser.license import resolve_license_key
        return bool(resolve_license_key(None))
    except Exception:
        # Never let a probe failure masquerade as "no license": fall back to the
        # env var, which is what launch will use.
        return bool(os.environ.get("CLOAKBROWSER_LICENSE_KEY", "").strip())


def _cloakbrowser_binary_info() -> Optional[dict]:
    try:
        from cloakbrowser import binary_info
        return binary_info()
    except Exception:
        return None


@dataclass
class EngineSpec:
    key: str
    label: str
    short: str
    family: str
    proxy_mode: str            # "custom" | "managed" | "app"
    description: str
    hint: str                  # install/activate instructions shown in the UI
    _launch: Callable = field(repr=False, default=None)

    def available(self, cfg) -> tuple[bool, str]:
        """Fast, side-effect-free check: package importable / binary present."""
        path = (getattr(cfg, "engine_paths", None) or {}).get(self.key)
        if self.key == "clawbrowser":
            ok = bool(path) or shutil.which("clawctl") is not None
            if not ok:
                return False, "install clawctl + API key (managed runtime)"
            if not (os.environ.get("CLAWBROWSER_API_KEY") or _clawctl_has_api_key()):
                return False, ("clawctl installed but no API key — run "
                               "`clawctl config set --api-key <key>`")
            return True, "CDP endpoint set in engine_paths" if path else "clawctl + API key ready"
        if self.key == "nextbrowser":
            return False, "desktop app — not embeddable as an engine"
        if self.key == "aliasmode_cloak" or self.key == "aliasmode_firefox":
            ok = bool(path) or bool(os.environ.get("ALIASMODE_CDP_ENDPOINT"))
            return ok, ("CDP endpoint set in engine_paths" if ok else
                        "AliasMode desktop app + Local API required")
        if self.key == "fingerprint_chromium":
            ok = bool(path and os.path.exists(path)) or \
                shutil.which("fingerprint-chromium") is not None or \
                shutil.which("ungoogled-chromium") is not None
            return ok, ("binary path set in engine_paths" if ok else
                        "download binary from GitHub Releases, set engine_paths")
        if self.key == "damru":
            ok = importlib.util.find_spec("damru") is not None
            return ok, ("pip package present" if ok else
                        "pip install git+https://github.com/akwin1234/damru.git "
                        "(Linux + Docker/Redroid)")
        if self.key == "cloakbrowser":
            if importlib.util.find_spec("cloakbrowser") is None:
                return False, self.hint
            if not (os.environ.get("CLOAKBROWSER_LICENSE_KEY") or _cloakbrowser_has_key()):
                return False, ("package installed but no license key — set "
                               "CLOAKBROWSER_LICENSE_KEY or run `cloakbrowser login`")
            info = _cloakbrowser_binary_info()
            if info is not None and not info.get("installed"):
                return False, "license key set but binary missing — first launch downloads it"
            return True, "installed"
        spec = importlib.util.find_spec({
            "camoufox": "camoufox",
            "patchright": "patchright",
        }.get(self.key, self.key))
        ok = spec is not None
        return ok, ("installed" if ok else self.hint)


# ---------------------------------------------------------------------------
# Launch adapters
# ---------------------------------------------------------------------------

_xvfb_proc: Optional[subprocess.Popen] = None


def ensure_display() -> bool:
    """Start one Xvfb for headful runs on a display-less host (server, Docker).

    A headful launch with no DISPLAY is a hard crash, and on DataDome headless
    rendering is the more detectable option — so a virtual display is the
    better fallback. Started once per process and reused by every engine.
    Returns True when a display is usable afterwards.
    """
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return True
    if _xvfb_proc is not None and _xvfb_proc.poll() is None:
        return True
    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        return False
    for num in range(99, 130):
        if os.path.exists(f"/tmp/.X11-unix/X{num}"):
            continue
        proc = subprocess.Popen(
            [xvfb, f":{num}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):  # ~5s: Xvfb creates the socket once ready
            if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                os.environ["DISPLAY"] = f":{num}"
                _xvfb_proc = proc
                return True
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        proc.kill()
    return False


def _playwright_cdp_connect(endpoint: str, opts: dict):
    """Attach Playwright to a running CDP endpoint (Clawbrowser / AliasMode)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - fallback for patchright-only hosts
        from patchright.sync_api import sync_playwright

    class _CdpSession:
        def __init__(self):
            self._pw = sync_playwright().start()
            self._browser = None

        def __enter__(self):
            self._browser = self._pw.chromium.connect_over_cdp(endpoint)
            ctx = self._browser.contexts[0] if self._browser.contexts else None
            if ctx is None:
                raise EngineUnavailable(
                    f"CDP endpoint {endpoint!r} returned no browser context")
            return _ContextView(ctx, self._browser)

        def __exit__(self, *exc):
            try:
                if self._browser is not None:
                    self._browser.close()
            except Exception:
                pass
            try:
                self._pw.stop()
            except Exception:
                pass

    return _CdpSession()


class _ContextView:
    """Expose a BrowserContext as the 'browser' object the runner expects."""

    def __init__(self, ctx, owner):
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._ctx, name)


# Headless Chromium hides its window, not its automation surface: without these
# the classic tells (webdriver flag, missing plugins) survive the launch and
# DataDome scores them. Camoufox needs none of this — it patches the Firefox
# build itself, so only the Chromium adapters call this.
_HEADLESS_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",      # small /dev/shm in containers = renderer crash
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
]


def _apply_headless_stealth(kw: dict, opts: dict) -> None:
    """Strip headless tells from a Chromium launch (mutates `kw` in place)."""
    if not opts.get("headless"):
        return
    kw["args"] = list(kw.get("args") or []) + _HEADLESS_CHROMIUM_ARGS
    kw["ignore_default_args"] = ["--enable-automation"]


def _launch_camoufox(cfg, opts: dict, log):
    from camoufox.sync_api import Camoufox

    # Camoufox rejects unknown kwargs (e.g. proxy_url / fresh_profile), so
    # pass only the keys its launch API understands.
    known = {"headless", "humanize", "geoip", "os", "proxy",
             "persistent_context", "user_data_dir"}
    kw = {k: v for k, v in opts.items() if k in known}

    @contextlib.contextmanager
    def _cm():
        with Camoufox(**kw) as browser:
            yield browser

    return _cm()


def _launch_patchright(cfg, opts: dict, log):
    from patchright.sync_api import sync_playwright

    kw = {"headless": bool(opts.get("headless"))}
    if opts.get("proxy"):
        kw["proxy"] = opts["proxy"]
    _apply_headless_stealth(kw, opts)
    if opts.get("headless"):
        # Playwright defaults headless Chromium to chrome-headless-shell, a
        # SEPARATE build whose fingerprint differs from the full browser (no
        # GPU, different UA/plugins/WebGL). This is the classic headless tell,
        # so force the real browser binary and hide the window via --headless=new.
        kw["headless"] = False
        kw["args"] = list(kw.get("args") or []) + ["--headless=new"]
    exec_path = (getattr(cfg, "engine_paths", None) or {}).get("patchright")
    if exec_path:
        kw["executable_path"] = exec_path

    @contextlib.contextmanager
    def _cm():
        pw = sync_playwright().start()
        browser = None
        try:
            if not opts.get("fresh_profile", True) and opts.get("user_data_dir"):
                browser = pw.chromium.launch_persistent_context(
                    opts["user_data_dir"], **kw)
            else:
                browser = pw.chromium.launch(**kw)
            # runner reads this to patch the remaining headless tells (UA/plugins)
            with contextlib.suppress(Exception):
                browser._headless_chromium = bool(opts.get("headless"))
            yield browser
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            pw.stop()

    return _cm()


def _launch_cloakbrowser(cfg, opts: dict, log):
    from cloakbrowser import launch

    kw = {
        "headless": bool(opts.get("headless")),
        "humanize": True,
        "geoip": bool(opts.get("geoip")),
    }
    if opts.get("proxy_url"):
        kw["proxy"] = opts["proxy_url"]

    @contextlib.contextmanager
    def _cm():
        browser = launch(**kw)
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return _cm()


def _launch_clawbrowser(cfg, opts: dict, log):
    endpoint = (getattr(cfg, "engine_paths", None) or {}).get("clawbrowser") \
        or os.environ.get("CLAWBROWSER_CDP_ENDPOINT")
    if not endpoint:
        endpoint = _clawctl_endpoint(log)
    if not endpoint:
        raise EngineUnavailable(
            "clawbrowser needs a CDP endpoint: set engine_paths.clawbrowser or "
            "run `clawctl start --profile regkit --url about:blank --json` "
            "(managed runtime + API key)")
    if log:
        log(f"[*] clawbrowser CDP endpoint: {endpoint}")
    return _playwright_cdp_connect(endpoint, opts)


def _clawctl_endpoint(log) -> str:
    clawctl = shutil.which("clawctl")
    if not clawctl:
        return ""
    try:
        proc = subprocess.run(
            [clawctl, "start", "--profile", "regkit", "--url",
             "about:blank", "--json"],
            capture_output=True, text=True, timeout=120,
        )
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {}
        for key in ("endpoint", "ws_url", "cdp", "wsEndpoint"):
            if isinstance(data, dict) and data.get(key):
                return str(data[key])
        for line in out.splitlines():
            if line.startswith(("ws://", "wss://", "http://")):
                return line.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        if log:
            log(f"[!] clawctl start failed: {exc}")
    return ""


def _launch_aliasmode(cfg, opts: dict, log):
    endpoint = (getattr(cfg, "engine_paths", None) or {}).get("aliasmode") \
        or os.environ.get("ALIASMODE_CDP_ENDPOINT")
    if not endpoint:
        raise EngineUnavailable(
            "AliasMode engines attach over CDP: start a profile via the "
            "AliasMode Local API and set engine_paths.aliasmode to its CDP "
            "endpoint (https://aliasmode.com/docs/local-api/)")
    return _playwright_cdp_connect(endpoint, opts)


def _launch_damru(cfg, opts: dict, log):
    from damru import Damru

    kw: dict = {}
    if opts.get("proxy_url"):
        kw["proxy"] = opts["proxy_url"]
    device = (getattr(cfg, "engine_paths", None) or {}).get("damru_device")
    if device:
        kw["device"] = device

    @contextlib.contextmanager
    def _cm():
        with Damru(**kw) as browser:
            yield browser

    return _cm()


def _launch_fingerprint_chromium(cfg, opts: dict, log):
    path = (getattr(cfg, "engine_paths", None) or {}).get("fingerprint_chromium") \
        or shutil.which("fingerprint-chromium") or shutil.which("ungoogled-chromium")
    if not path:
        raise EngineUnavailable(
            "fingerprint_chromium binary not found — download the build for "
            "your OS from https://github.com/adryfish/fingerprint-chromium/"
            "releases and set engine_paths.fingerprint_chromium")
    from playwright.sync_api import sync_playwright

    kw = {
        "executable_path": path,
        "headless": bool(opts.get("headless")),
    }
    if opts.get("proxy"):
        kw["proxy"] = opts["proxy"]
    _apply_headless_stealth(kw, opts)
    if opts.get("headless"):
        # Same reason as patchright: keep the REAL fingerprint build, hide the
        # window with the new headless mode instead of the headless shell.
        kw["headless"] = False
        kw["args"] = list(kw.get("args") or []) + ["--headless=new"]

    @contextlib.contextmanager
    def _cm():
        pw = sync_playwright().start()
        browser = None
        try:
            browser = pw.chromium.launch(**kw)
            with contextlib.suppress(Exception):
                browser._headless_chromium = bool(opts.get("headless"))
            yield browser
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            pw.stop()

    return _cm()


def _launch_nextbrowser(cfg, opts: dict, log):
    raise EngineUnavailable(
        "Nextbrowser is a desktop AI-orchestrator app (macOS/Windows) that "
        "drives Clawbrowser + Camoufox underneath — it cannot be embedded as "
        "a Python engine. Pick Clawbrowser or Camoufox directly, or run "
        "Nextbrowser as the outer orchestrator instead of this toolkit.")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ENGINES: dict[str, EngineSpec] = {
    "clawbrowser": EngineSpec(
        "clawbrowser", "Clawbrowser", "CB", "managed", "managed",
        "Managed proxy — one pool key per launch (CDP endpoint via clawctl).",
        "clawctl CLI + API key: https://github.com/clawbrowser/clawbrowser",
        _launch_clawbrowser,
    ),
    "camoufox": EngineSpec(
        "camoufox", "Camoufox", "CF", "firefox", "custom",
        "Your custom proxy — no pool key read (default engine).",
        "pip install camoufox && python -m camoufox fetch",
        _launch_camoufox,
    ),
    "nextbrowser": EngineSpec(
        "nextbrowser", "Nextbrowser", "NB", "app", "app",
        "Desktop AI orchestrator (macOS/Windows) — not embeddable.",
        "https://github.com/nextbrowser-oss/nextbrowser-app",
        _launch_nextbrowser,
    ),
    "cloakbrowser": EngineSpec(
        "cloakbrowser", "CloakBrowser", "CL", "chromium", "custom",
        "Your custom proxy — no pool key read (source-patched Chromium).",
        "pip install cloakbrowser (binary auto-downloads; CLOAKBROWSER_LICENSE_KEY)",
        _launch_cloakbrowser,
    ),
    "aliasmode_cloak": EngineSpec(
        "aliasmode_cloak", "AliasMode + CloakBrowser", "AM", "chromium", "custom",
        "Your custom proxy — AliasMode profile attached over CDP.",
        "AliasMode desktop app + Local API: https://aliasmode.com/docs/local-api/",
        _launch_aliasmode,
    ),
    "patchright": EngineSpec(
        "patchright", "Patchright", "PR", "chromium", "custom",
        "Your custom proxy — no pool key read (patched Playwright).",
        "pip install patchright && patchright install chromium",
        _launch_patchright,
    ),
    "aliasmode_firefox": EngineSpec(
        "aliasmode_firefox", "AliasMode Firefox", "AF", "firefox", "custom",
        "Your custom proxy — AliasMode's Camoufox-based Firefox over CDP.",
        "AliasMode desktop app; see https://github.com/aliasmode/aliasmode-firefox",
        _launch_aliasmode,
    ),
    "damru": EngineSpec(
        "damru", "Damru", "DM", "android", "custom",
        "Your custom proxy — real Android Chrome (Redroid in Docker).",
        "pip install git+https://github.com/akwin1234/damru.git (Linux + Docker)",
        _launch_damru,
    ),
    "fingerprint_chromium": EngineSpec(
        "fingerprint_chromium", "Fingerprint Chromium", "FC", "chromium", "custom",
        "Your custom proxy — Ungoogled-Chromium fingerprint fork binary.",
        "Download from https://github.com/adryfish/fingerprint-chromium/releases "
        "and set engine_paths.fingerprint_chromium",
        _launch_fingerprint_chromium,
    ),
}

DEFAULT_ENGINE_ORDER = [
    "camoufox",
    "patchright",
    "cloakbrowser",
    "damru",
    "fingerprint_chromium",
    "clawbrowser",
    "aliasmode_cloak",
    "aliasmode_firefox",
]


def resolve_engine_order(cfg) -> list[str]:
    """Effective engine order: exclusive `engine` if set, else `engine_order`.

    Unknown keys are dropped; an empty/invalid list falls back to
    DEFAULT_ENGINE_ORDER. `engine` set to an unknown key raises so a typo
    never silently runs the wrong browser.
    """
    explicit = (getattr(cfg, "engine", "") or "").strip().lower()
    if explicit:
        if explicit not in ENGINES:
            raise EngineUnavailable(
                f"unknown engine {explicit!r} (known: {', '.join(ENGINES)})")
        return [explicit]
    order = [str(s).strip().lower()
             for s in (getattr(cfg, "engine_order", None) or [])
             if str(s).strip()]
    order = [k for k in order if k in ENGINES]
    return order or list(DEFAULT_ENGINE_ORDER)


def list_engines(cfg=None) -> list[dict]:
    """UI metadata for every engine (availability is cheap, not a live probe)."""
    out = []
    for key in DEFAULT_ENGINE_ORDER:
        spec = ENGINES[key]
        ok, note = spec.available(cfg) if cfg is not None else (True, "")
        out.append({
            "key": spec.key,
            "label": spec.label,
            "short": spec.short,
            "family": spec.family,
            "proxy_mode": spec.proxy_mode,
            "description": spec.description,
            "hint": spec.hint,
            "available": ok,
            "status": note,
        })
    return out


def launch_engine(key: str, cfg, opts: dict, log=None):
    """Return a context manager yielding the engine's browser object.

    `opts` is the shared launch options dict produced by the runner
    (headless, proxy dict, proxy_url, fresh_profile, user_data_dir, ...);
    each adapter picks only the keys its engine understands. Raises
    EngineUnavailable when the engine cannot start — the caller then falls
    back to the next engine in the order.
    """
    spec = ENGINES.get(str(key).strip().lower())
    if spec is None:
        raise EngineUnavailable(f"unknown engine {key!r}")
    if key in ("nextbrowser",):
        return spec._launch(cfg, opts, log)
    ok, note = spec.available(cfg)
    if not ok:
        raise EngineUnavailable(f"{spec.label}: {note}")
    try:
        return spec._launch(cfg, opts, log)
    except EngineUnavailable:
        raise
    except ImportError as exc:  # pragma: no cover - lazy import safety net
        raise EngineUnavailable(f"{spec.label} import failed: {exc}") from exc
