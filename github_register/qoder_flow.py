"""Qoder account registration + PAT capture — browser-based, separate module.

Flow (in one browser session, one proxy):
  1. Create temp email via Tempik (domain rotation across our 28 domains)
  2. Navigate to qoder.com/users/sign-up → fill form → solve Aliyun CAPTCHA
  3. Poll OTP from Tempik → verify → login
  4. Navigate to /account/integrations → capture PAT (pt-...)
  5. Save PAT to accounts/qoder_pats.txt

The Aliyun CAPTCHA solver uses the proven approach from
sanhaji182/captcha-solvers-toolkit (NCC template matching + human-like drag),
ported to Python + Playwright.

CLI:
  python -m github_register.qoder_flow --count 1
  python -m github_register.qoder_flow --count 3 --engine camoufox
  python -m github_register.qoder_flow --headless --skip-captcha-wait
"""
from __future__ import annotations

import argparse
import json
import random
import re
import string
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pyotp

from . import engines
from .config import Config
from .runner import _browser_ctx_options
from .tempik import TempikMailClient, TempikMailError

ROOT = Path(__file__).resolve().parent.parent
QODER_PAT_FILE = ROOT / "accounts" / "qoder_pats.txt"
QODER_CREDS_FILE = ROOT / "accounts" / "qoder_creds.jsonl"

QODER_SIGNUP = "https://qoder.com/users/sign-up"
QODER_INTEGRATIONS = "https://qoder.com/account/integrations"

PAT_RE = re.compile(r'pt-[A-Za-z0-9_-]{8,}')


class QoderFlowError(RuntimeError):
    pass


# ════════════════════════════════════════════════════════════════════
# Random identity
# ════════════════════════════════════════════════════════════════════

def random_name(min_len: int = 4, max_len: int = 8) -> tuple[str, str]:
    length = random.randint(min_len, max_len)
    first = "".join(random.choices(string.ascii_lowercase, k=length)).capitalize()
    last = "".join(random.choices(string.ascii_lowercase, k=length)).capitalize()
    return first, last


def random_password(length: int = 14) -> str:
    # Qoder rejects passwords without a special character ("must contain
    # numbers, uppercase, lowercase, AND special characters"), so all four
    # classes are seeded explicitly. Specials are keyboard-safe — no quotes
    # or backslash that could break selectors or form serialization.
    specials = "!@#$%^&*_-+="
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    special = random.choice(specials)
    rest = "".join(random.choices(
        string.ascii_letters + string.digits + specials, k=length - 4))
    pwd = upper + lower + digit + special + rest
    return "".join(random.sample(pwd, len(pwd)))


def _human_drag(page, target_x: float, target_y: float, dx: float, log=print):
    """Biomechanical slider drag.

    The old drag failed 40/40 server verdicts while its gap positions were
    provably correct, so the tell was the MOTION itself. Rebuilt from cursor
    dynamics of real pointer input:
      - approach the handle in 2-3 visible steps instead of teleporting
      - grip: slow start (finger pressure), then a velocity burst, then a
        long deceleration tail (two-phase minimum-jerk profile)
      - bursty timing: variable dt 4-45ms with occasional 30-140ms
        hesitations — never a uniform sampling interval
      - y-wobble is a smooth clamped random walk correlated with the x
        velocity, not independent per-event noise
      - overshoot with 1-2 decaying oscillations before release
    """
    import math

    dirx = 1 if dx >= 0 else -1
    dist = abs(dx)

    # ── approach: humans acquire the target, they don't spawn on it ──
    ax, ay = target_x - dirx * random.uniform(30, 70), target_y + random.uniform(-24, 24)
    page.mouse.move(ax, ay)
    page.wait_for_timeout(random.randint(90, 220))
    page.mouse.move((ax + target_x) / 2 + random.uniform(-3, 3),
                    (ay + target_y) / 2 + random.uniform(-3, 3))
    page.wait_for_timeout(random.randint(40, 110))
    page.mouse.move(target_x, target_y)
    page.wait_for_timeout(random.randint(180, 420))  # grip pause

    page.mouse.down()
    page.wait_for_timeout(random.randint(60, 140))

    # ── two-phase x-profile: min-jerk blend, sampled with noise ──
    # phase A (0-35%): accelerating grip-to-burst; phase B: long decel
    steps = random.randint(34, 52)
    y_off = 0.0
    last_prog = 0.0
    for step in range(steps):
        t = (step + 1) / steps
        # min-jerk: 10t³-15t⁴+5t⁵ — zero velocity/accel at both ends
        prog = 10 * t**3 - 15 * t**4 + 5 * t**5
        # velocity noise: humans don't sample uniformly
        prog += random.uniform(-0.015, 0.015) * math.sin(step * 1.7)
        prog = min(max(prog, last_prog), 1.0)
        last_prog = prog

        x = target_x + dirx * dist * prog

        # y: correlated random walk — thumb pivots, never wanders far
        y_off += random.uniform(-0.8, 0.8)
        y_off = min(max(y_off, -4.5), 4.5)  # clamp: thumb pivots, never wanders
        page.mouse.move(x, target_y + y_off)

        # bursty dt with hesitation bursts
        if random.random() < 0.12:
            page.wait_for_timeout(random.randint(30, 140))  # micro-hesitation
        else:
            page.wait_for_timeout(random.randint(4, 45))

    # ── release: overshoot, decaying oscillation, settle ──
    over = dirx * random.uniform(3, 9)
    page.mouse.move(target_x + dirx * dist + over, target_y + random.uniform(-1.5, 1.5))
    page.wait_for_timeout(random.randint(60, 160))
    back = over * random.uniform(0.4, 0.7)
    page.mouse.move(target_x + dirx * dist + over - back, target_y + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(40, 120))
    page.mouse.move(target_x + dirx * dist, target_y)
    page.wait_for_timeout(random.randint(120, 280))  # settle before release
    page.mouse.up()
    page.wait_for_timeout(random.randint(150, 350))

def solve_aliyun_captcha(page, log=print, max_attempts: int = 20,
                         manual_timeout: int = 90) -> bool:
    """Solve Aliyun V3 slider puzzle CAPTCHA.

    Strategy: find the gap position by comparing background image brightness
    with the puzzle piece, then drag the slider with human-like motion.

    Returns True if CAPTCHA was solved (or not present).
    """
    # Two presentation modes (verified against the live DOM):
    #   1. Interstitial: "Click to verify" = #aliyunCaptcha-captcha-left
    #      (visible). The slider elements exist in the DOM but stay hidden
    #      until that click. NOTE: #captcha-button is a DIFFERENT div that
    #      never becomes visible — targeting it silently matches nothing.
    #   2. Direct: slider already visible.
    # Also: the SDK lazy-renders, so a single query right after submit sees
    # nothing and the solver used to report "no CAPTCHA" while signup sat
    # blocked on the interstitial. Wait for either landmark to appear.
    def _slider_visible() -> bool:
        s = page.query_selector('#aliyunCaptcha-sliding-slider')
        return bool(s and s.is_visible())

    def _click_verify() -> bool:
        btn = page.query_selector('#aliyunCaptcha-captcha-left')
        if not (btn and btn.is_visible()):
            return False
        log("    [i] interstitial CAPTCHA — clicking 'Click to verify' ...")
        try:
            btn.click()
        except Exception:
            page.evaluate("""() =>
                document.querySelector('#aliyunCaptcha-captcha-left')?.click()""")
        try:
            page.wait_for_selector('#aliyunCaptcha-sliding-slider',
                                   state="visible", timeout=15_000)
            return True
        except Exception:
            log("    [!] slider never spawned after clicking verify")
            return False

    # Wait for the CAPTCHA to render at all (slider or interstitial)
    try:
        page.wait_for_function(
            """() => {
                const s = document.querySelector('#aliyunCaptcha-sliding-slider');
                const b = document.querySelector('#aliyunCaptcha-captcha-left');
                const vis = e => e && e.getBoundingClientRect().width > 0
                                   && e.getBoundingClientRect().height > 0;
                return (s && vis(s)) || (b && vis(b));
            }""",
            timeout=30_000,
        )
    except Exception:
        # Not rendering is only "no CAPTCHA" when the page is NOT showing the
        # human-check copy — otherwise the SDK stalled and signup is still
        # blocked. Claiming success there sends the flow into a doomed 120s
        # email poll (seen live: human-check page, zero captcha elements).
        body = page.inner_text("body")
        if "human" in body.lower():
            log("    [!] human-check page shown but CAPTCHA SDK never rendered")
            return False
        log("    [i] no Aliyun CAPTCHA rendered — skipping")
        return True

    if not _slider_visible() and not _click_verify():
        log("    [i] CAPTCHA present but slider never appeared — skipping")
        return False

    def _refresh_puzzle() -> None:
        """Reset a stuck slider via the SDK's refresh button.

        After a rejected drag the handle can stay parked at the dragged
        position (seen live: every retry then computes delta<2 and does
        nothing forever). Clicking refresh loads a fresh puzzle and re-cents
        the handle.
        """
        btn = page.query_selector('#aliyunCaptcha-btn-refresh')
        if not btn:
            return
        log("    [i] refreshing stuck puzzle ...")
        try:
            btn.click()
        except Exception:
            page.evaluate(
                "() => document.querySelector('#aliyunCaptcha-btn-refresh')?.click()")
        page.wait_for_timeout(2000)

    prev_src = ""
    for attempt in range(1, max_attempts + 1):
        log(f"    ▸ CAPTCHA attempt {attempt}/{max_attempts}")

        # Wedged-SDK detection: after "Verify failed, please try again" the
        # puzzle image stops rotating entirely — the refresh button loads
        # nothing new and every subsequent drag is a no-op (seen live: the
        # exact same NCC probe result for 19 consecutive attempts). Recovery:
        # close the captcha window and re-enter via the interstitial, which
        # re-initialises the SDK with a fresh CertifyId.
        img_src = page.evaluate(
            "() => document.querySelector('#aliyunCaptcha-img')?.src || ''")
        wedged = ("verify failed" in page.inner_text("body").lower()
                  or (img_src and img_src == prev_src))
        if wedged:
            log("    [i] captcha wedged (stale image / verify-failed) — reopening")
            close_btn = page.query_selector('#aliyunCaptcha-btn-close')
            if close_btn:
                try:
                    close_btn.click()
                except Exception:
                    page.evaluate(
                        "() => document.querySelector('#aliyunCaptcha-btn-close')?.click()")
                page.wait_for_timeout(1500)
            if not _click_verify():
                log("    [!] could not reopen the captcha — giving up this run")
                return False
        prev_src = img_src
        # Re-query every attempt: the handle moves during a failed drag, so a
        # stale ElementHandle would report the old (already-dragged) position.
        # A vanished slider means either success or a reset back to the
        # interstitial — distinguish the two before claiming victory.
        slider = page.query_selector('#aliyunCaptcha-sliding-slider')
        if not slider or not slider.is_visible():
            reset = page.query_selector('#aliyunCaptcha-captcha-left')
            if reset and reset.is_visible():
                log("    [i] CAPTCHA reset to interstitial — re-clicking")
                if not _click_verify():
                    continue
            else:
                log("    ✓ CAPTCHA cleared")
                return True
        try:
            # Wait for images to be ready
            page.wait_for_function(
                """() => {
                    const img = document.querySelector('#aliyunCaptcha-img');
                    const sl = document.querySelector('#aliyunCaptcha-sliding-slider');
                    return img && img.complete && img.naturalWidth > 0 &&
                           sl && sl.getBoundingClientRect().width > 0;
                }""",
                timeout=15000,
            )
            page.wait_for_timeout(500)

            # ── Find the gap: NCC template matching + vertical edge pair ──
            #
            # Two independent estimators, then median consensus:
            #   1. NCC — the puzzle piece is a cut-out of the background, so it
            #      correlates best at the gap. Copes with texture but drifts on
            #      flat/sky regions (score < 0.5 is a weak match).
            #   2. Vertical edge pair — the cut-out leaves strong vertical
            #      boundaries exactly `puzzle_w` apart. Robust on flat regions
            #      where NCC is blind, but can latch onto scene edges.
            # Median of the two/three candidates beats either alone.
            probe = page.evaluate("""async () => {
                const bgImg = document.querySelector('#aliyunCaptcha-img');
                const pzImg = document.querySelector('#aliyunCaptcha-puzzle');
                if (!bgImg || !pzImg || !bgImg.complete || bgImg.naturalWidth === 0) {
                    return { error: 'images not ready' };
                }
                async function pixels(img) {
                    const res = await fetch(img.src);
                    const url = URL.createObjectURL(await res.blob());
                    const tmp = new Image();
                    await new Promise((ok, no) => { tmp.onload = ok; tmp.onerror = no; tmp.src = url; });
                    const c = document.createElement('canvas');
                    c.width = tmp.naturalWidth; c.height = tmp.naturalHeight;
                    const ctx = c.getContext('2d');
                    ctx.drawImage(tmp, 0, 0);
                    const data = ctx.getImageData(0, 0, c.width, c.height).data;
                    URL.revokeObjectURL(url);
                    return { w: c.width, h: c.height, data };
                }
                const bg = await pixels(bgImg);
                const pz = await pixels(pzImg);

                // Opaque bounding box of the puzzle piece (alpha > 128)
                let minX = pz.w, maxX = -1, minY = pz.h, maxY = -1;
                for (let y = 0; y < pz.h; y++) {
                    for (let x = 0; x < pz.w; x++) {
                        if (pz.data[(y * pz.w + x) * 4 + 3] > 128) {
                            if (x < minX) minX = x;
                            if (x > maxX) maxX = x;
                            if (y < minY) minY = y;
                            if (y > maxY) maxY = y;
                        }
                    }
                }
                if (maxX < 0) return { error: 'puzzle fully transparent' };
                const cw = maxX - minX + 1, ch = maxY - minY + 1;

                const pg = new Float64Array(cw * ch);
                for (let y = 0; y < ch; y++) {
                    for (let x = 0; x < cw; x++) {
                        const i = ((minY + y) * pz.w + (minX + x)) * 4;
                        pg[y * cw + x] = (pz.data[i] + pz.data[i+1] + pz.data[i+2]) / 3;
                    }
                }
                const bgG = new Float64Array(bg.w * bg.h);
                for (let i = 0; i < bg.w * bg.h; i++) {
                    const j = i * 4;
                    bgG[i] = (bg.data[j] + bg.data[j+1] + bg.data[j+2]) / 3;
                }

                // ── estimator 1: NCC over every horizontal offset ──
                const margin = 20;
                const limit = bg.w - cw - margin;
                let nccX = -1, nccBest = -2;
                const curve = [];
                for (let ox = margin; ox < limit; ox++) {
                    let tm = 0, sm = 0;
                    for (let y = 0; y < ch; y++) {
                        for (let x = 0; x < cw; x++) {
                            tm += pg[y * cw + x];
                            sm += bgG[(minY + y) * bg.w + (ox + x)];
                        }
                    }
                    const n = cw * ch;
                    tm /= n; sm /= n;
                    let tv = 0, sv = 0, cv = 0;
                    for (let y = 0; y < ch; y++) {
                        for (let x = 0; x < cw; x++) {
                            const td = pg[y * cw + x] - tm;
                            const sd = bgG[(minY + y) * bg.w + (ox + x)] - sm;
                            tv += td * td; sv += sd * sd; cv += sd * td;
                        }
                    }
                    const nc = cv / Math.sqrt(tv * sv + 1e-10);
                    curve.push(nc);
                    if (nc > nccBest) { nccBest = nc; nccX = ox; }
                }
                // smoothed peak: a lone spike is usually noise, a real gap is a hill
                let smX = nccX;
                if (curve.length > 4) {
                    let best = -2;
                    for (let i = 2; i < curve.length - 2; i++) {
                        const v = (curve[i-2] + curve[i-1] + curve[i] + curve[i+1] + curve[i+2]) / 5;
                        if (v > best) { best = v; smX = margin + i; }
                    }
                }

                // ── estimator 2: strongest vertical edge pair cw apart ──
                const colEdge = new Float64Array(bg.w);
                for (let x = 1; x < bg.w - 1; x++) {
                    let s = 0;
                    for (let y = Math.floor(bg.h * 0.15); y < Math.floor(bg.h * 0.85); y++) {
                        const li = (y * bg.w + x - 1) * 4;
                        const ri = (y * bg.w + x + 1) * 4;
                        const lv = (bg.data[li] + bg.data[li+1] + bg.data[li+2]) / 3;
                        const rv = (bg.data[ri] + bg.data[ri+1] + bg.data[ri+2]) / 3;
                        s += Math.abs(lv - rv);
                    }
                    colEdge[x] = s;
                }
                let edgeX = -1, edgeBest = -1;
                for (let x = margin; x < limit; x++) {
                    let s = 0;
                    for (let j = 0; j < 5; j++) {
                        s += (colEdge[x + j] || 0) + (colEdge[x + cw - 1 - j] || 0);
                    }
                    if (s > edgeBest) { edgeBest = s; edgeX = x; }
                }

                return {
                    nccX, nccScore: nccBest, smoothX: smX, edgeX,
                    puzzleW: cw, puzzleH: ch, bgW: bg.w, bgH: bg.h,
                    opaqueTop: minY,
                };
            }""")

            if not probe or probe.get("error"):
                log(f"    [!] could not analyze CAPTCHA image: {probe.get('error') if probe else 'no probe'}")
                continue

            candidates = sorted(x for x in (probe["nccX"], probe["smoothX"], probe["edgeX"]) if x > 0)
            gap_x = candidates[len(candidates) // 2]
            log(f"    → ncc={probe['nccX']} (score={probe['nccScore']:.3f}) "
                f"smooth={probe['smoothX']} edge={probe['edgeX']} → gap_x={gap_x}")

            # Get slider element bounds
            slider_box = slider.bounding_box()
            if not slider_box:
                continue

            # Image coords → screen coords: the background is displayed at
            # `img_box.width` for `bgW` natural pixels, so one image pixel is
            # `img_box.width / bgW` screen pixels.
            img_el = page.query_selector('#aliyunCaptcha-img')
            img_box = img_el.bounding_box() if img_el else None
            if not img_box:
                continue
            drag_distance = int(round(gap_x * (img_box["width"] / probe["bgW"])))
            if drag_distance < 10:
                log("    [!] implausible drag distance, skipping attempt")
                continue

            log(f"    → drag={drag_distance}px")

            # ── Human-like drag simulation ──
            # The handle rides the same horizontal track as the background
            # image, so the target handle-left = image left + gap offset.
            # Using the delta from the handle's CURRENT left makes retries
            # correct: a handle left mid-track is pulled to the same target
            # instead of overshooting by its accumulated offset.
            delta = (img_box["x"] + drag_distance) - slider_box["x"]
            if abs(delta) < 2:
                # Handle parked at the (wrong) dragged spot — a re-drag is a
                # no-op. Pull a fresh puzzle instead of spinning.
                _refresh_puzzle()
                continue
            start_x = slider_box["x"] + slider_box["width"] / 2
            start_y = slider_box["y"] + slider_box["height"] / 2

            _human_drag(page, start_x, start_y, delta, log)

            # Wait for a DEFINITIVE server verdict. Right after a rejected
            # drag the slider is briefly gone while the interstitial respawns
            # — claiming "solved" at that instant is a false positive.
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
            if "Unable to verify" in body:
                log(f"    [!] attempt {attempt}: server rejected the drag")
                page.wait_for_timeout(1000)
                continue

            slider_after = page.query_selector('#aliyunCaptcha-sliding-slider')
            if not slider_after or not slider_after.is_visible():
                # Confirm the interstitial didn't just respawn (give the SDK
                # a beat to re-render it) and the page actually moved on.
                page.wait_for_timeout(1500)
                body = page.inner_text("body")
                if "Click to verify" in body or "human" in body:
                    log(f"    [i] attempt {attempt}: CAPTCHA reset — retrying")
                    continue
                log("    ✓ CAPTCHA solved!")
                return True

            # Success token check (Aliyun writes captcha_verify_param on pass)
            success = page.evaluate("""() => {
                const el = document.querySelector('[id*="captcha_verify"]');
                if (el && el.value && el.value.length > 10) return true;
                return false;
            }""")
            if success:
                log("    ✓ CAPTCHA verified (token present)")
                return True

            log(f"    [!] attempt {attempt}: CAPTCHA still present, retrying...")
            page.wait_for_timeout(1000)

        except Exception as exc:
            log(f"    [!] CAPTCHA attempt {attempt} error: {exc}")
            page.wait_for_timeout(1000)

    # All attempts exhausted
    log(f"    [!] CAPTCHA not solved after {max_attempts} attempts")
    # In headful mode, wait for manual solve
    try:
        if page.query_selector('#aliyunCaptcha-sliding-slider'):
            log(f"    [i] waiting up to {manual_timeout}s for manual solve...")
            for _ in range(manual_timeout):
                page.wait_for_timeout(1000)
                if not page.query_selector('#aliyunCaptcha-sliding-slider'):
                    log("    ✓ manual solve detected")
                    return True
    except Exception:
        pass
    return False


# ════════════════════════════════════════════════════════════════════
# PAT capture from browser
# ════════════════════════════════════════════════════════════════════

def capture_pat(page, log=print, timeout: int = 60) -> Optional[str]:
    """Navigate to /account/integrations and capture PAT from network responses."""
    captured: list[str] = []

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = resp.text()
            for m in PAT_RE.finditer(body):
                captured.append(m.group(0))
        except Exception:
            pass

    page.on("response", on_response)
    try:
        log("[*] navigating to /account/integrations ...")
        page.goto(QODER_INTEGRATIONS, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        if "sign-in" in page.url or "login" in page.url:
            log("    [!] /account/integrations redirects to login — session lost")
            return None

        # Click Create/New/Generate token button
        clicked = False
        for sel in ['button:has-text("Create")', 'button:has-text("New")',
                    'button:has-text("Generate")', 'button:has-text("Add")',
                    'text=Create token']:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    clicked = True
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        if clicked:
            # Fill token name
            for sel in ['input[placeholder*="name" i]', '#basic_name']:
                try:
                    inp = page.query_selector(sel)
                    if inp and inp.is_visible():
                        inp.fill(f"auto-{int(time.time())}")
                        break
                except Exception:
                    continue
            # Confirm
            for sel in ['button:has-text("Confirm")', 'button:has-text("OK")',
                        'button:has-text("Create")', 'button[type="submit"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue

        # Wait for network capture
        deadline = time.time() + timeout
        while time.time() < deadline and not captured:
            time.sleep(1)

        # Fallback: scan DOM
        if not captured:
            for m in PAT_RE.finditer(page.content()):
                captured.append(m.group(0))
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if captured:
        log(f"    ✓ PAT captured: {captured[0][:16]}...")
        return captured[0]
    log("    [!] PAT not captured (may need manual creation)")
    return None


# ════════════════════════════════════════════════════════════════════
# Registration flow
# ════════════════════════════════════════════════════════════════════

def _try_fill(page, selector: str, value: str, timeout: int = 3000) -> bool:
    try:
        el = page.wait_for_selector(selector, timeout=timeout)
        if el:
            el.fill(value)
            return True
    except Exception:
        pass
    return False


def _try_click(page, selector: str, timeout: int = 5000) -> bool:
    try:
        el = page.wait_for_selector(selector, timeout=timeout)
        if el:
            el.click()
            return True
    except Exception:
        pass
    return False


def register_qoder(page, cfg: Config, mail: TempikMailClient,
                   log=print) -> Optional[dict]:
    """Register one Qoder account. Returns {email, password, pat, ...} or None."""

    first, last = random_name()
    qoder_pass = random_password()

    # Create temp email
    email, _ = mail.create_mailbox()
    log(f"[*] Qoder email: {email}")

    # Save credentials early
    creds = {"email": email, "qoder_password": qoder_pass,
             "name": f"{first} {last}",
             "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    QODER_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with QODER_CREDS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(creds) + "\n")

    # Navigate to signup
    log("[*] navigating to qoder.com/users/sign-up ...")
    page.goto(QODER_SIGNUP, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # Fill form step 1: name + email
    #
    # The email form is rendered client-side AFTER the Google/GitHub buttons,
    # and only once the SPA bundle has hydrated — a fixed sleep races it. Fill
    # attempts that miss leave the inputs empty, Continue then fails form
    # validation, and step 2 never appears (the "password field not found"
    # symptom). So: block until the field exists, then verify the fills landed.
    log("[*] waiting for the signup form to render ...")
    form_sel = '#basic_firstName, input[placeholder*="first name" i]'
    try:
        page.wait_for_selector(form_sel, state="visible", timeout=30_000)
    except Exception:
        log("    [!] signup form never rendered")
        return None

    log("[*] filling registration form ...")
    filled_ok = (
        _try_fill(page, form_sel, first, timeout=15_000)
        and _try_fill(page, '#basic_lastName, input[placeholder*="Last Name" i]',
                      last, timeout=15_000)
        and _try_fill(page, '#basic_email, input[placeholder*="email" i]',
                      email, timeout=15_000)
    )
    if not filled_ok:
        log("    [!] could not fill name/email inputs")
        return None
    _try_click(page, '.ant-checkbox-input, input[type="checkbox"]', timeout=10_000)

    log("    → clicking Continue (step 1)...")
    if not _try_click(page, 'button:has-text("Continue")'):
        _try_click(page, 'button[type="submit"]')
    page.wait_for_timeout(3000)

    # Step 2: password
    log("[*] setting password ...")
    pwd_filled = False
    for _ in range(10):
        if _try_fill(page, '#basic_password, input[type="password"]', qoder_pass):
            pwd_filled = True
            break
        page.wait_for_timeout(1000)
    if not pwd_filled:
        log("    [!] password field not found")
        return None

    page.wait_for_timeout(500)
    if _try_click(page, 'button:has-text("Continue")'):
        log("    ✓ password submitted, waiting for CAPTCHA...")
    else:
        _try_click(page, 'button[type="submit"]')

    # Step 3: Solve CAPTCHA
    log("[*] handling CAPTCHA ...")
    if not solve_aliyun_captcha(page, log=log):
        log("    [!] CAPTCHA not solved — account may not be registered")
        # Continue anyway — some domains don't require CAPTCHA

    # Step 4: Wait for OTP page
    #
    # Fail fast: if no OTP input ever appears, the signup never completed
    # (CAPTCHA / validation failed upstream) and no verification email is
    # coming — polling Tempik would just burn the full 120s timeout.
    log("[*] waiting for OTP page ...")
    otp_sel = ('input[maxlength="1"], '
               'input[placeholder*="verification" i], '
               'input[placeholder*="code" i]')
    try:
        page.wait_for_selector(otp_sel, state="visible", timeout=15_000)
    except Exception:
        # The verify page can render its code input with neither a telling
        # placeholder nor maxlength (seen live: "Enter the code sent to
        # <email>:" with a bare box). Any visible text/tel input there IS
        # the OTP field — the form has nothing else left at this stage.
        try:
            page.wait_for_selector('input[type="text"], input[type="tel"], '
                                   'input:not([type])', state="visible",
                                   timeout=10_000)
        except Exception:
            body = page.inner_text("body")[:300].replace("\n", " | ")
            log(f"    [!] OTP page never appeared (url={page.url})")
            log(f"    [!] page says: {body}")
            return None
    page.screenshot(path="/tmp/qoder-after-captcha.png")

    # Step 5: Poll OTP from Tempik
    log("[*] polling OTP from Tempik ...")
    try:
        code = mail.wait_for_code(email, timeout=120, log=log,
                                  cancel_cb=lambda: False)
        log(f"    ✓ OTP: {code}")
    except Exception as exc:
        log(f"    [!] OTP timeout: {exc}")
        return None

    # Step 6: Fill OTP
    log("[*] filling OTP ...")
    single_digits = [i for i in page.query_selector_all('input[maxlength="1"]')
                     if i.is_visible()]
    if len(single_digits) >= 4:
        # Segmented boxes: click the first, then type — these are React
        # controlled inputs where fill() often doesn't trigger the
        # onChange/advance logic, but real key events do. Refuse a length
        # mismatch: typing a 6-char code into 8 boxes leaves it truncated.
        if len(single_digits) != len(code):
            log(f"    [!] {len(single_digits)} OTP boxes but code has "
                f"{len(code)} digits — refusing to half-fill")
            return None
        single_digits[0].click()
        for digit in code:
            page.keyboard.type(digit, delay=random.randint(60, 140))
    else:
        if not (_try_fill(page, 'input[placeholder*="verification" i]', code)
                or _try_fill(page, 'input[placeholder*="code" i]', code)
                or _try_fill(page, 'input[placeholder*="OTP" i]', code)):
            # Bare code box (no placeholder) — the only visible text input on
            # the verify page. Keyboard-type it so React onChange fires.
            box = page.query_selector('input[type="text"], input[type="tel"], '
                                      'input:not([type])')
            if box and box.is_visible():
                box.click()
                page.keyboard.type(code, delay=random.randint(60, 140))
            else:
                log("    [!] no usable OTP input found")
                return None

    page.wait_for_timeout(500)
    _try_click(page, 'button:has-text("Verify")')
    _try_click(page, 'button:has-text("Continue")')
    _try_click(page, 'button[type="submit"]')
    page.wait_for_timeout(5000)
    page.screenshot(path="/tmp/qoder-after-verify.png")

    # Check login success
    logged_in = "sign-up" not in page.url and "sign-in" not in page.url
    if not logged_in:
        # Try pressing Enter as fallback
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        logged_in = "sign-up" not in page.url and "sign-in" not in page.url

    if not logged_in:
        log(f"    [!] OTP verification may have failed (url={page.url})")
        # Continue to PAT attempt anyway

    # Step 7: Capture PAT
    log("[*] capturing PAT ...")
    pat = capture_pat(page, log=log)

    if pat:
        from .qoder_inject import append_pat
        append_pat(pat, email=email)
        creds["pat"] = pat
        log(f"    ✓ PAT saved: {pat[:16]}...")
    else:
        log("    [!] PAT not captured")

    return creds


# ════════════════════════════════════════════════════════════════════
# GitHub OAuth signup path
#
# Sign in to GitHub with an account from accounts/github_accounts_*.txt,
# then register Qoder via "Sign up with GitHub". This bypasses the Aliyun
# CAPTCHA and the email-verification stage entirely — GitHub's own session
# proves the identity.
# ════════════════════════════════════════════════════════════════════

GITHUB_USED_FILE = ROOT / "accounts" / "qoder_github_used.txt"


def _load_github_pool(accounts_path: str, log=print) -> list[dict]:
    from .codebuddy_flow import load_accounts, find_latest_accounts
    path = Path(accounts_path) if accounts_path else find_latest_accounts()
    if not path or not Path(path).is_file():
        raise FileNotFoundError(
            "no github accounts file (accounts/github_accounts_*.txt)")
    pool = load_accounts(path)
    used = set()
    if GITHUB_USED_FILE.is_file():
        used = {l.strip() for l in GITHUB_USED_FILE.read_text().splitlines() if l.strip()}
    fresh = [a for a in pool if a["email"] not in used]
    log(f"[*] github pool: {len(pool)} accounts, {len(fresh)} not yet used for qoder")
    return fresh


def _mark_github_used(email: str) -> None:
    GITHUB_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with GITHUB_USED_FILE.open("a", encoding="utf-8") as f:
        f.write(email + "\n")


def github_login(page, email: str, password: str, totp: str, mail, log=print) -> bool:
    """Sign in to github.com. Handles TOTP 2FA and device-verification email.

    Returns True once signed in (avatar visible or url left /login).
    """
    log("[*] signing in to GitHub ...")
    page.goto("https://github.com/login", wait_until="domcontentloaded")
    page.wait_for_selector("#login_field", state="visible", timeout=30_000)
    page.fill("#login_field", email)
    page.fill("#password", password)
    page.wait_for_timeout(random.randint(300, 700))
    page.click('input[name="commit"], button[type="submit"]')
    page.wait_for_timeout(4000)

    # 2FA (TOTP) if the account has a secret
    if page.query_selector("#app_totp, input[name='otp']") and "two-factor" in page.url:
        if not totp:
            log("    [!] GitHub demands 2FA but the account has no TOTP secret")
            return False
        code = pyotp.TOTP(totp).now()
        log(f"    [*] TOTP code: {code}")
        page.fill("#app_totp, input[name='otp']", code)
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)

    # Device verification: GitHub emails a code to the account address
    # (a Tempik catch-all domain — the code lands in the same inbox API).
    body = page.inner_text("body").lower()
    if "verify your device" in body or "device verification" in body:
        log("[*] GitHub device verification — polling Tempik for the code ...")
        try:
            code = mail.wait_for_code(email, timeout=120, log=log,
                                      cancel_cb=lambda: False)
        except Exception as exc:
            log(f"    [!] device-verification email never arrived: {exc}")
            return False
        page.fill("input[name='otp'], #otp, input[inputmode='numeric']", code)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

    url = page.url
    ok = "/login" not in url and "/session" not in url
    log(f"    {'✓ GitHub signed in' if ok else '[!] GitHub login failed'} (url={url})")
    return ok


def register_qoder_via_github(page, cfg: Config, account: dict, mail, log=print) -> Optional[dict]:
    """OAuth route: GitHub session → 'Sign up with GitHub' → PAT."""
    email = account["email"]
    creds = {"email": email, "github_username": account.get("username", ""),
             "mode": "github-oauth", "proxy": "",
             "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}

    if not github_login(page, email, account["password"], account.get("totp", ""), mail, log):
        return None

    log("[*] opening qoder signup → GitHub OAuth ...")
    page.goto(QODER_SIGNUP, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    gh_btn = page.query_selector('button:has-text("GitHub"), a:has-text("Sign up with GitHub")')
    if not gh_btn:
        body = page.inner_text("body")[:200].replace("\n", " | ")
        log(f"    [!] 'Sign up with GitHub' button not found. Page: {body}")
        return None
    gh_btn.click()

    # GitHub authorize prompt (first time only; later runs redirect straight)
    try:
        page.wait_for_selector(
            'button:has-text("Authorize"), input[value="Authorize"]',
            timeout=15_000)
        page.wait_for_timeout(random.randint(400, 900))
        page.click('button:has-text("Authorize"), input[value="Authorize"]')
        log("    ✓ OAuth authorized")
    except Exception:
        log("    [i] no authorize prompt — likely already authorized")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(6000)
    url = page.url
    log(f"[*] after OAuth: {url}")
    if "qoder.com" not in url:
        log("    [!] OAuth did not return to qoder.com")
        return None
    if "/users/sign-up" in url:
        log("    [!] still on signup — OAuth did not complete")
        return None

    creds["qoder_url"] = url
    QODER_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with QODER_CREDS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(creds) + "\n")

    log("[*] capturing PAT ...")
    pat = capture_pat(page, log=log)
    if pat:
        from .qoder_inject import append_pat
        append_pat(pat, email=email)
        creds["pat"] = pat
        log(f"    ✓ PAT saved: {pat[:16]}...")
    _mark_github_used(email)
    return creds


# ════════════════════════════════════════════════════════════════════
# Main runner
# ════════════════════════════════════════════════════════════════════

def run(cfg: Config, count: int = 1, engine: str = "",
        headless: bool = False, log=print, via_github: bool = False,
        accounts_path: str = "") -> list[dict]:
    """Run Qoder registration for `count` accounts.

    via_github: sign in to GitHub with accounts from accounts/github_accounts_*.txt
    and register through "Sign up with GitHub" (no CAPTCHA, no email verify).
    """
    results = []
    engine_keys = [engine] if engine else ["camoufox", "cloakbrowser", "patchright"]

    gh_pool: list[dict] = []
    if via_github:
        gh_pool = _load_github_pool(accounts_path, log)
        if not gh_pool:
            log("[!] no unused GitHub accounts — nothing to do")
            return results
        count = min(count, len(gh_pool))

    for i in range(1, count + 1):
        log(f"\n{'═' * 50}")
        log(f"  QODER ACCOUNT {i}/{count}")
        log(f"{'═' * 50}")

        mail = TempikMailClient(api_url=cfg.tempik_api,
                                domains=cfg.tempik_domains or [])

        for engine_key in engine_keys:
            try:
                opts = _browser_ctx_options(cfg, log)
                if headless:
                    opts["headless"] = True
                with engines.launch_engine(engine_key, cfg, opts, log) as browser:
                    ctx = browser.new_context(locale="en-US")
                    page = ctx.new_page()
                    page.set_default_timeout(20_000)
                    if via_github:
                        result = register_qoder_via_github(
                            page, cfg, gh_pool[i - 1], mail, log)
                    else:
                        result = register_qoder(page, cfg, mail, log)
                    if result:
                        results.append(result)
                    ctx.close()
                    break  # engine worked
            except engines.EngineUnavailable as exc:
                log(f"[!] engine {engine_key} unavailable: {exc}")
                continue
            except Exception as exc:
                log(f"[!] engine {engine_key} error: {exc}")
                continue

        if i < count:
            log(f"[*] waiting 10s before next account...")
            time.sleep(10)

    log(f"\n[*] done: {len(results)}/{count} Qoder accounts registered")
    return results


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m github_register.qoder_flow",
        description="Qoder account registration + PAT capture.",
    )
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--engine", default="", help="Force specific engine")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--via-github", action="store_true",
                    help="register via GitHub OAuth (skips CAPTCHA + email verify)")
    ap.add_argument("--accounts", default="",
                    help="github accounts file (default: latest accounts/github_accounts_*.txt)")
    args = ap.parse_args(argv)

    log = lambda msg: print(msg, flush=True)  # noqa: E731

    from .config import load_config
    cfg = load_config(args.config)
    results = run(cfg, count=args.count, engine=args.engine,
                  headless=args.headless, log=log,
                  via_github=args.via_github, accounts_path=args.accounts)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
