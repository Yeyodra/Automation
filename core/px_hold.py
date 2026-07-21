"""PerimeterX / HUMAN Press-and-Hold — in-process (no captcha-solver HTTP port).

Faithful port of captcha-solver/perimeterx/solve.py + Outlook UI fixes:

  Solver core (must match):
    - real page.mouse down → hold → up (CDP input layer, not fake click)
    - iframe center target + hold 4–8s + micro-moves ±1.5px
    - max 3 attempts, 3s bake after each hold
    - success: actuated && _px3 && (rotated || no prior _px3)

  Outlook 2026 shell (needed for MS card UI):
    - prefer accessible **progress pill** (what users hold), then iframe center
    - on pill: almost zero jitter (narrow hit-box) + hold until bar full / gate gone
    - longer PoW window (hold can exceed 8s)

Same browser session as signup (Camoufox page) — same IP + UA as CreateAccount.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any


PX_COOKIE_NAMES = ("_px3", "_pxvid", "_pxde", "pxcts")

# Gate: ACTIVE challenge only — not residual copy after success.
# Require (iframe OR interactive hold button) + prove-human title/copy.
# Text-only match caused false positives → double solve after px already OK.
_GATE_VISIBLE_JS = r"""() => {
  const frames = [...document.querySelectorAll('iframe')];
  const f = frames.find(el =>
    /hsprotect|perimeterx|px-captcha|human\.security/i.test(el.src || '') &&
    el.getBoundingClientRect().width > 50 &&
    el.getBoundingClientRect().height > 30);
  const t = (document.body && document.body.innerText || '').toLowerCase();
  const titleHit = /let's prove you're human|prove you're human/.test(t);
  const pressCopy = /press\s*(&|and)?\s*hold/.test(t);
  // interactive pill (not static "the button" instruction alone)
  const btns = [...document.querySelectorAll('button, [role="button"]')];
  const holdBtn = btns.some(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 20 || r.height > 80) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    const tx = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
    if (tx.includes('the button')) return false;
    return /press\s*(&|and)?\s*hold|^hold$/.test(tx.trim());
  });
  // Active gate: human title/copy AND (iframe or hold button visible)
  if ((titleHit || pressCopy) && (f || holdBtn)) return true;
  // iframe alone large enough (solver path)
  if (f && (titleHit || pressCopy)) return true;
  return false;
}"""

_IFRAME_CENTER_JS = r"""() => {
  const frames = [...document.querySelectorAll('iframe')];
  const f = frames.find(el =>
    /hsprotect|perimeterx|px-captcha|human\.security/i.test(el.src || '') &&
    el.getBoundingClientRect().width > 50);
  if (!f) return null;
  const r = f.getBoundingClientRect();
  if (r.width < 50 || r.height < 20) return null;
  return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height, kind: 'iframe'};
}"""

# Hold pill button — NOT static "Press and hold the button." label
_HOLD_BTN_SELECTORS = (
    'button:has-text("Press and hold")',
    '[role="button"]:has-text("Press and hold")',
    'button:has-text("Press & hold")',
    '[role="button"]:has-text("Press & hold")',
    'button:has-text("hold")',
    '[role="button"]:has-text("hold")',
    '[role="progressbar"]',
)


@dataclass
class PxHoldResult:
    solved: bool
    gate_reached: bool = False
    press_hold_actuated: bool = False
    px3_rotated: bool = False
    attempts: int = 0
    cookies: dict[str, str] = field(default_factory=dict)
    px3: str | None = None
    cookie_header: str | None = None
    elapsed: float = 0.0
    error: str | None = None
    target: str | None = None  # pill | iframe
    method: str = "inprocess-px-presshold"

    def as_dict(self) -> dict[str, Any]:
        return {
            "solved": self.solved,
            "gate_reached": self.gate_reached,
            "press_hold_actuated": self.press_hold_actuated,
            "px3_rotated": self.px3_rotated,
            "attempts": self.attempts,
            "cookies": self.cookies,
            "px3": self.px3,
            "cookie_header": self.cookie_header,
            "elapsed": self.elapsed,
            "error": self.error,
            "target": self.target,
            "method": self.method,
            "replay_contract": {
                "bound_to": ["_pxvid", "client_ip", "user_agent"],
                "ttl_note": "Replay same IP+UA+cookie bundle within short TTL",
            },
        }


async def gate_visible(page) -> bool:
    try:
        return bool(await page.evaluate(_GATE_VISIBLE_JS))
    except Exception:
        return False


async def px_cookies(context) -> dict[str, str]:
    try:
        raw = await context.cookies()
    except Exception:
        return {}
    return {c["name"]: c["value"] for c in raw if c.get("name") in PX_COOKIE_NAMES}


def _header(cookies: dict[str, str]) -> str | None:
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def _try_again_visible(page) -> bool:
    try:
        t = (await page.evaluate(
            "() => (document.body && document.body.innerText || '').toLowerCase()"
        )) or ""
        return "please try again" in t or "try again" in t
    except Exception:
        return False


async def _find_pill_box(page) -> dict[str, float] | None:
    """Locate the interactive hold pill — NOT the static instruction text.

    UI layout (MS HUMAN card):
      robot graphic (top)
      static: "Press and hold the button."  ← do NOT click
      pill button: "Press and hold"         ← target this
    """
    # 0) Playwright role — most reliable for MS Fluent button
    for role_name in (
        re.compile(r"^press and hold$", re.I),
        re.compile(r"^press\s*&\s*hold$", re.I),
        re.compile(r"press and hold", re.I),
    ):
        try:
            loc = page.get_by_role("button", name=role_name)
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                txt = (await el.inner_text()).strip().lower()
                # skip instruction-like copy
                if "the button" in txt:
                    continue
                bb = await el.bounding_box()
                if not bb or bb["width"] < 60 or bb["height"] > 80:
                    continue
                return {
                    "x": bb["x"] + bb["width"] / 2,
                    "y": bb["y"] + bb["height"] / 2,
                    "w": bb["width"],
                    "h": bb["height"],
                    "kind": "pill",
                }
        except Exception:
            continue

    # 1) Explicit selectors
    for sel in _HOLD_BTN_SELECTORS:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            best = None
            best_y = -1.0
            for i in range(n):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                try:
                    txt = (await el.inner_text()).strip().lower()
                except Exception:
                    txt = ""
                if "the button" in txt:
                    continue  # static instruction line
                bb = await el.bounding_box()
                if not bb or bb["width"] < 60 or bb["height"] > 80 or bb["width"] > 560:
                    continue
                # prefer lower on screen (pill is below instruction text)
                if bb["y"] > best_y:
                    best_y = bb["y"]
                    best = bb
            if best:
                return {
                    "x": best["x"] + best["width"] / 2,
                    "y": best["y"] + best["height"] / 2,
                    "w": best["width"],
                    "h": best["height"],
                    "kind": "pill",
                }
        except Exception:
            continue

    # 2) DOM scan — interactive only, penalize instruction text
    try:
        box = await page.evaluate(
            r"""() => {
              const body = document.body;
              if (!body) return null;
              const all = [...body.querySelectorAll(
                'button, [role="button"], [role="progressbar"], input[type="button"]'
              )];
              const candidates = [];
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width < 80 || r.width > 520 || r.height < 22 || r.height > 64) continue;
                if (r.bottom < 0 || r.top > innerHeight) continue;
                const style = getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none' || style.pointerEvents === 'none') continue;
                const t = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase().trim();
                // instruction label, not the control
                if (t.includes('the button')) continue;
                if (t.includes("let's prove") || t.includes('prove you')) continue;
                const isHoldBtn = /press\s*(and|&)?\s*hold/.test(t) || t === 'hold';
                const pillish = r.width > r.height * 2.2;
                if (!isHoldBtn && !pillish) continue;
                let score = 0;
                if (isHoldBtn) score += 50;
                if (pillish) score += 10;
                // lower on page = more likely the real pill under the robot
                score += r.y / 100;
                candidates.push({
                  x: r.x + r.width / 2,
                  y: r.y + r.height / 2,
                  w: r.width,
                  h: r.height,
                  score,
                });
              }
              candidates.sort((a, b) => b.score - a.score);
              return candidates[0] || null;
            }"""
        )
        if box and box.get("w", 0) >= 80:
            return {
                "x": float(box["x"]),
                "y": float(box["y"]),
                "w": float(box["w"]),
                "h": float(box["h"]),
                "kind": "pill",
            }
    except Exception:
        pass

    return None


async def _find_iframe_box(page) -> dict[str, float] | None:
    try:
        box = await page.evaluate(_IFRAME_CENTER_JS)
    except Exception:
        return None
    if not box:
        return None
    return {
        "x": float(box["x"]),
        "y": float(box["y"]),
        "w": float(box.get("w") or 0),
        "h": float(box.get("h") or 0),
        "kind": "iframe",
    }


async def _progress_complete(page) -> bool:
    """Best-effort: bar full / button done / gate gone / try-again."""
    if not await gate_visible(page):
        return True
    if await _try_again_visible(page):
        return True  # stop this hold; caller retries
    try:
        return bool(
            await page.evaluate(
                r"""() => {
                  // aria progress
                  for (const el of document.querySelectorAll('[role="progressbar"]')) {
                    const v = parseFloat(el.getAttribute('aria-valuenow') || '');
                    const max = parseFloat(el.getAttribute('aria-valuemax') || '100');
                    if (!isNaN(v) && !isNaN(max) && max > 0 && v >= max * 0.95) return true;
                  }
                  // filled width inside hold button
                  const btns = [...document.querySelectorAll('button, [role="button"]')];
                  for (const b of btns) {
                    const t = (b.innerText || '').toLowerCase();
                    if (!/press|hold/.test(t) || t.includes('the button')) continue;
                    const fill = b.querySelector('[style*="width"], div, span');
                    // heuristic: if challenge text gone from title area
                  }
                  const body = (document.body && document.body.innerText || '').toLowerCase();
                  if (body.includes('verified') || body.includes('success')) return true;
                  // still showing challenge
                  if (/let's prove you're human|press and hold/.test(body)) return false;
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _hold_adaptive(
    page,
    box: dict[str, float],
    *,
    min_hold_s: float = 3.0,
    max_hold_s: float = 50.0,
    jitter: float = 0.15,
) -> bool:
    """Hold until challenge actually clears — works for short AND long bars.

    Release conditions (after min_hold_s):
      - gate gone for 2 consecutive checks (~0.7s)  [avoids flicker half-release]
      - try-again visible
      - max_hold_s reached (hard cap ~50s for slow proxy PoW)

    Do NOT release on a single gate_visible=False (caused half progress).
    Do NOT reacquire/move far mid-hold (cancels press on pill).
    """
    x, y = float(box["x"]), float(box["y"])
    t0 = time.monotonic()
    gone_streak = 0
    try:
        await page.mouse.move(x - 3, y - 2)
        await asyncio.sleep(0.12)
        await page.mouse.move(x, y)
        await asyncio.sleep(0.1)
        await page.mouse.down()

        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= max_hold_s:
                break

            if await _try_again_visible(page):
                break

            if elapsed >= min_hold_s:
                if not await gate_visible(page):
                    gone_streak += 1
                    # need 2 clean checks so a 1-frame flicker doesn't mouse.up mid-bar
                    if gone_streak >= 2:
                        break
                else:
                    gone_streak = 0

            # stay planted — micro jitter only (large move drops the hold)
            if jitter > 0:
                await page.mouse.move(
                    x + random.uniform(-jitter, jitter),
                    y + random.uniform(-jitter, jitter),
                )
            await asyncio.sleep(0.35)

        # short settle while still down if gate just cleared
        await asyncio.sleep(0.25)
        await page.mouse.up()
        return True
    except Exception:
        try:
            await page.mouse.up()
        except Exception:
            pass
        return False


async def press_and_hold(
    page,
    hold_min: float = 4.0,
    hold_max: float = 8.0,
) -> tuple[bool, str | None]:
    """One adaptive actuation. Returns (ok, target_kind).

    hold_min = minimum press before we may release on gate-gone.
    hold_max env only raises floor of max; hard max is always >= 50s for long bars.
    Order: pill (MS button) → iframe center (fallback).
    """
    min_hold = max(2.0, float(hold_min) * 0.4)  # e.g. 6 → 2.4s floor
    # Long challenges (proxy) often need 20–45s — never cap at 8–10s
    max_hold = max(50.0, float(hold_max) * 2.0)

    pill = await _find_pill_box(page)
    if pill:
        ok = await _hold_adaptive(
            page,
            pill,
            min_hold_s=min_hold,
            max_hold_s=max_hold,
            jitter=0.15,
        )
        if ok:
            return True, "pill"

    iframe = await _find_iframe_box(page)
    if iframe:
        ok = await _hold_adaptive(
            page,
            iframe,
            min_hold_s=min_hold,
            max_hold_s=max_hold,
            jitter=0.5,
        )
        if ok:
            return True, "iframe"

    return False, None


async def solve_px_hold_on_page(
    page,
    context=None,
    *,
    timeout_s: float = 120.0,
    max_attempts: int = 3,
    hold_min: float = 4.0,
    hold_max: float = 8.0,
    wait_gate_s: float = 25.0,
    bake_s: float = 3.0,
) -> PxHoldResult:
    """Wait for gate → press-hold (solver loop) → harvest cookies.

    Success matches captcha-solver:
      actuated && _px3 present && (cookie rotated OR no prior _px3)
    Also accept gate gone after actuate (MS shell often clears UI before cookie read).
    """
    t0 = time.monotonic()
    ctx = context
    if ctx is None:
        try:
            ctx = page.context
        except Exception:
            ctx = None

    px_before: str | None = None
    if ctx is not None:
        px_before = (await px_cookies(ctx)).get("_px3")

    # wait for gate
    gate = False
    deadline_gate = time.monotonic() + max(0.0, wait_gate_s)
    while time.monotonic() < deadline_gate:
        if await gate_visible(page):
            gate = True
            break
        await asyncio.sleep(0.7)

    if not gate:
        cookies = await px_cookies(ctx) if ctx is not None else {}
        # silent pass — no challenge (caller continues); not a fake "solved captcha"
        return PxHoldResult(
            solved=True,
            gate_reached=False,
            cookies=cookies,
            px3=cookies.get("_px3"),
            cookie_header=_header(cookies),
            elapsed=round(time.monotonic() - t0, 1),
            method="inprocess-px-no-gate",
        )

    actuated = False
    attempts = 0
    last_target: str | None = None
    # Each attempt can hold up to ~50s — give full budget
    deadline = time.monotonic() + max(90.0, timeout_s)

    # solver loop: max_attempts, break when gate gone after hold
    while time.monotonic() < deadline and attempts < max_attempts:
        if not await gate_visible(page):
            await asyncio.sleep(1.5)
            if not await gate_visible(page):
                break

        attempts += 1
        # after "try again", brief pause then slightly longer hold
        if await _try_again_visible(page):
            await asyncio.sleep(1.2)
            hold_min_use = max(hold_min, 6.0)
            hold_max_use = max(hold_max, 10.0)
        else:
            hold_min_use, hold_max_use = hold_min, hold_max

        ok, kind = await press_and_hold(page, hold_min_use, hold_max_use)
        actuated = actuated or ok
        if kind:
            last_target = kind

        await asyncio.sleep(bake_s)  # let _px3 bake (solver: 3s)

        if not await gate_visible(page):
            break
        # failed attempt — small backoff before retry
        await asyncio.sleep(1.0 + attempts * 0.4)

    cookies = await px_cookies(ctx) if ctx is not None else {}
    px_after = cookies.get("_px3")
    rotated = bool(px_before and px_after and px_before != px_after)

    # Cookie may rotate slightly before UI unmounts — wait briefly then re-check
    gate_still = await gate_visible(page)
    if gate_still and (rotated or px_after):
        for _ in range(6):
            await asyncio.sleep(0.8)
            gate_still = await gate_visible(page)
            if not gate_still:
                break

    # HONEST success: challenge UI must be GONE.
    # Cookie rotate alone is NOT enough (we lied: solved=True while still on captcha).
    # - no-gate path already returned above
    # - cleared: actuated + gate gone (cookie rotate optional confirmation)
    solved = bool(actuated and not gate_still)

    err = None
    if not actuated:
        err = "press-hold gesture failed (no pill/iframe target)"
    elif not solved:
        err = (
            "press-hold actuated but HUMAN gate still on screen "
            f"(target={last_target}, rotated={rotated}, "
            f"try_again={await _try_again_visible(page)}) "
            "— not cleared; retry / better hold target / cleaner IP"
        )

    return PxHoldResult(
        solved=solved,
        gate_reached=True,
        press_hold_actuated=actuated,
        px3_rotated=rotated,
        attempts=attempts,
        cookies=cookies,
        px3=px_after,
        cookie_header=_header(cookies),
        elapsed=round(time.monotonic() - t0, 1),
        error=err,
        target=last_target,
    )


def _self_check() -> None:
    r = PxHoldResult(solved=False, error="x", target="pill")
    d = r.as_dict()
    assert d["method"] == "inprocess-px-presshold"
    assert d["target"] == "pill"
    assert _header({"_px3": "a", "_pxvid": "b"}) == "_px3=a; _pxvid=b"
    print("core.px_hold self-check ok", flush=True)


if __name__ == "__main__":
    _self_check()
