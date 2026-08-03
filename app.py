"""Automation hub — Textual HUD (default) + thin CLI.

  python app.py                 # TUI
  python app.py --no-tui list
  python app.py run grok -n 1

Progress tracking is GLOBAL (core.progress.BatchProgress).
Any farm that prints the hub log contract is auto-tracked.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import typer
from rich.console import Console
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Log,
    Static,
    TextArea,
)

# Farm log contract: [HH:MM:SS] [id] step  …
_RE_HUD_TS_STEP = re.compile(
    r"^\[(?P<ts>\d{1,2}:\d{2}:\d{2})\]\s+\[(?P<id>\d+)\]\s+(?P<step>\S+)"
)
_RE_HUD_BARE_OK_FAIL = re.compile(r"^\[(?P<id>\d+)\]\s+(?P<step>OK|FAIL)\b", re.I)
# HUD steps: outcomes + key farm milestones (not every google fill retry)
_HUD_SHOW_STEPS = frozenset(
    {
        "start",
        "ok",
        "success",
        "done",
        "saved",
        "fail",
        "failed",
        "error",
        "err",
        "timeout",
        # getunikey / shared milestones
        "nav",
        "oauth",
        "google",
        "user",
        "token",
        "smoke",
        "usage",
        "shot",
    }
)


def hud_show_line(msg: str) -> bool:
    """HUD: hub + milestones + OK/FAIL + WARP. Skip noisy retries."""
    raw = (msg or "").rstrip("\n")
    if not raw.strip():
        return False
    low = raw.lower()
    if low.startswith(("[hub]", "[jobctl]", "[error]", "[getunikey]", "[vps-push]", "[warp]")):
        return True
    if "warp every_n" in low or "warp wave" in low:
        return True
    if "warp: rotate" in low or "warp every_n rotate" in low:
        return True
    if "[warp-policy]" in low:
        return True
    if low.startswith("[done]") or "exit=" in low and low.startswith("[hub]"):
        return True
    # Skip noisy mid-step spam
    if "fill retry" in low or "still on google interstitial" in low:
        return False
    m = _RE_HUD_TS_STEP.match(raw.strip())
    if m:
        step = m.group("step").lower()
        if step not in _HUD_SHOW_STEPS:
            return False
        # google: only milestones, not every "waiting …"
        if step == "google":
            rest = raw.lower()
            return any(
                x in rest
                for x in (
                    "email filled",
                    "password filled",
                    "consent",
                    "post-password",
                    "page https",
                    "2fa",
                    "switched",
                )
            )
        return True
    if _RE_HUD_BARE_OK_FAIL.match(raw.strip()):
        return True
    # Keep short hub tips / list_jobs lines (indented ok/MISSING)
    if raw.startswith("  [") and ("ok]" in low or "missing]" in low):
        return True
    return False

_HUB = Path(__file__).resolve().parent
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from core.progress import BatchProgress  # noqa: E402
from jobs.registry import get_job, list_jobs  # noqa: E402
from jobs.runner import run_job  # noqa: E402

cli = typer.Typer(add_completion=False, no_args_is_help=False)
console = Console()


class StatusBar(Static):
    def set_status(self, text: str) -> None:
        self.update(f"  {text}")


class ProgressPanel(Static):
    """Renders BatchProgress.render() — logic lives in core.progress."""

    def set_text(self, text: str) -> None:
        self.update(text)


class HubApp(App[None]):
    """Compact HUD: form → progress → actions → log."""

    TITLE = "Automation Hub"
    # Do NOT name attrs workers / _workers — Textual owns App.workers
    _busy: bool = False
    _progress: BatchProgress

    CSS = """
    Screen { layout: vertical; }
    #body {
        height: 1fr;
        padding: 0 1;
        layout: vertical;
    }
    #form {
        height: auto;
        max-height: 22;
        border: solid $accent;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #form .row { height: 3; margin-top: 0; }
    #form Label { width: auto; color: $text-muted; padding: 0 1 0 0; }
    #form Input { width: 1fr; }
    #form TextArea { width: 1fr; height: 6; }
    #form Checkbox { width: auto; margin-right: 2; }
    #email-row { height: 3; }
    #email-row.hidden { display: none; }
    #email-row Button { min-width: 12; margin-right: 1; }
    #gift-row { height: 3; }
    #gift-row.hidden { display: none; }
    #getunikey-row { height: auto; min-height: 3; }
    #getunikey-row.hidden { display: none; }
    #getunikey-gmail-row { height: 7; }
    #getunikey-gmail-row.hidden { display: none; }
    #count-label.dim { color: $text-muted; }
    #btn-job-cycle { min-width: 5; margin: 0 1 0 0; }
    #progress {
        height: auto;
        min-height: 3;
        max-height: 6;
        border: solid $warning;
        background: $surface;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $text;
    }
    #actions { height: 3; margin-bottom: 1; }
    #actions Button { margin-right: 1; min-width: 12; }
    #log {
        height: 1fr;
        min-height: 6;
        border: solid $primary;
        background: $surface;
        scrollbar-size-vertical: 2;
        scrollbar-gutter: stable;
    }
    #log:focus { border: solid $success; }
    StatusBar {
        dock: bottom;
        height: 1;
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "run", "Run", show=True),
        Binding("s", "stop_job", "Stop", show=True),
        Binding("w", "warp_status", "Status", show=True),
        Binding("W", "warp_rotate", "Rotate", show=True),
        Binding("c", "clear_log", "Clear", show=True),
        Binding("l", "list_jobs", "Jobs", show=True),
        Binding("j", "cycle_job", "Job↻", show=True),
        Binding("f", "focus_log", "Log", show=True),
        Binding("end", "log_end", "End", show=False),
        Binding("home", "log_home", "Home", show=False),
        Binding("pageup", "log_page_up", "PgUp", show=False),
        Binding("pagedown", "log_page_down", "PgDn", show=False),
    ]

    # Jobs that support IMAP domain vs GPTMail toggle in HUD
    _EMAIL_TOGGLE_JOBS = frozenset({"grok", "enter"})
    # Jobs that support inject target Local vs VPS
    _INJECT_TOGGLE_JOBS = frozenset({"grok", "grok-reauth"})

    def __init__(self) -> None:
        super().__init__()
        self._progress = BatchProgress()
        self._syncing_form = False  # prevent Input.Changed feedback loop
        # per-job email mode: domain (IMAP) | gptmail — seeded from hub .env on mount
        self._email_mode: dict[str, str] = {"grok": "domain", "enter": "gptmail"}
        # Inject target for grok / grok-reauth: local | vps (default vps if env push on)
        self._inject_target: str = "vps"
        # Log follow: pause when user scrolls up; End / Run resumes
        self._log_follow: bool = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            with Vertical(id="form"):
                with Horizontal(classes="row"):
                    yield Label("Job")
                    yield Input(
                        value="grok",
                        id="job",
                        placeholder="grok|grok-reauth|enter|outlook|getunikey",
                    )
                    yield Button("↻", id="btn-job-cycle", variant="primary")
                    yield Label("  -n", id="count-label")
                    yield Input(value="20", id="count", placeholder="20")
                    yield Label("  -c")
                    yield Input(value="3", id="concurrent", placeholder="3")
                with Horizontal(classes="row"):
                    yield Label("Args")
                    yield Input(value="-y", id="extra", placeholder="-y --headed")
                    yield Label(" everyN")
                    yield Input(
                        value="3",
                        id="every-n",
                        placeholder="0=off else =c",
                    )
                    yield Checkbox("Dry-run", id="chk-dry", value=False)
                # Email provider (shown for grok + enter)
                with Horizontal(id="email-row", classes="row"):
                    yield Label("Email")
                    yield Button("IMAP", id="btn-email-imap", variant="success")
                    yield Button("GPTMail", id="btn-email-gptmail", variant="default")
                    yield Button("Exzork", id="btn-email-exzork", variant="default")
                    yield Label("  (grok/enter)", id="email-hint")
                # Inject target: local 9router DB vs VPS merge push (grok + grok-reauth)
                with Horizontal(id="inject-row", classes="row"):
                    yield Label("Inject")
                    yield Button("Local", id="btn-inject-local", variant="success")
                    yield Button("VPS", id="btn-inject-vps", variant="default")
                    yield Label("  9router DB", id="inject-hint")
                # Enter gift code (shown when Job=enter)
                with Horizontal(id="gift-row", classes="row"):
                    yield Label("Gift")
                    yield Input(
                        value="",
                        id="gift-code",
                        placeholder="ENTER_GIFT_CODE (referral)",
                    )
                # GetUniKey: referral start URL + Google account list
                with Horizontal(id="getunikey-row", classes="row"):
                    yield Label("RefURL")
                    yield Input(
                        value="",
                        id="getunikey-referral",
                        placeholder="https://www.getunikey.ai/sign-up?aff=bTOY (empty=default)",
                    )
                with Horizontal(id="getunikey-gmail-row", classes="row"):
                    yield Label("Gmail")
                    yield TextArea(
                        id="getunikey-gmails",
                        placeholder=(
                            "email|password  (one per line)\n"
                            "lanjar4@mangtoha.com|bintang088"
                        ),
                    )
            yield ProgressPanel("Progress: idle — set -n then Run", id="progress")
            with Horizontal(id="actions"):
                yield Button("Run [R]", id="btn-run", variant="success")
                yield Button("Stop [S]", id="btn-stop", variant="error")
                yield Button("Status [W]", id="btn-warp-st", variant="primary")
                yield Button("Rotate", id="btn-warp-rot", variant="warning")
                yield Button("Jobs [L]", id="btn-list")
                yield Button("Clear [C]", id="btn-clear")
                yield Button("Quit [Q]", id="btn-quit")
            # Quiet HUD: short buffer, no rich highlight (n=1k/2k stays scrollable)
            log = Log(id="log", highlight=False, max_lines=500, auto_scroll=False)
            log.can_focus = True
            yield log
        yield StatusBar(
            "J=cycle · quiet log · End=follow · S=Stop · R=Run · Q=Quit"
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#job", Input).focus()
        jobs = ", ".join(j.id for j in list_jobs()) or "(none)"
        self._log(f"Hub ready. Jobs: {jobs}")
        self._log("everyN LIVE sync: ubah -c → everyN ikut (kalau everyN≠0). 0=off.")
        self._log("Run = auto WARP connect · everyN>0 = mid-batch rotate · Rotate = manual IP")
        self._log(
            "Job ↻ / [J] = cycle · Email IMAP|GPTMail|Exzork (grok) · Gift (enter) · "
            "Gmail list + RefURL (getunikey)"
        )
        self._log("Log quiet: START/OK/FAIL + hub/WARP only · full detail → farms/*/results/*/farm.log")
        self._log("Scroll up = pause follow · End / Run = resume tail")
        self._seed_modes_from_env()
        self._seed_gift_from_env()
        self._seed_getunikey_from_env()
        self._seed_inject_from_env()

        self._log("Stop [S]=kill farm · Status/Rotate=manual WARP · Quit=close HUD")
        self._log("Grok Email: IMAP | GPTMail | Exzork (mailer.exzork.me wildcard).")
        self._log("Inject Local|VPS (grok / grok-reauth) → 9router DB target")
        self._log("getunikey: paste Gmail list email|pass · n=pool size · RefURL optional")
        self._sync_every_n_ui(silent=True)
        self._sync_email_row()
        self._sync_gift_row()
        self._sync_getunikey_rows()
        self._sync_inject_row()
        self._paint_progress()
        self._warp_worker("status", quiet=True)

    def _job_id(self) -> str:
        try:
            return (self.query_one("#job", Input).value.strip() or "grok").lower()
        except Exception:
            return "grok"

    def _job_cycle_ids(self) -> list[str]:
        ids = [j.id for j in list_jobs()]
        return ids or ["grok"]

    def _seed_modes_from_env(self) -> None:
        """Seed email toggles from hub .env (GROK_*/ENTER_* or shared EMAIL_MODE)."""
        try:
            from core.env import parse_env_file, HUB_ENV

            hub = parse_env_file(HUB_ENV)
        except Exception:
            hub = {}
        shared = (hub.get("EMAIL_MODE") or "domain").strip().lower()
        _ok = ("domain", "plus_trick", "gptmail", "exzork")
        for jid, key, default in (
            (
                "grok",
                "GROK_EMAIL_MODE",
                shared if shared in ("domain", "gptmail", "exzork") else "domain",
            ),
            ("enter", "ENTER_EMAIL_MODE", "gptmail"),
        ):
            raw = (hub.get(key) or default or "domain").strip().lower()
            if raw not in _ok:
                raw = default if default in ("domain", "gptmail", "exzork") else "domain"
            # HUD: domain | gptmail | exzork (plus_trick → IMAP/domain button)
            if raw == "gptmail":
                self._email_mode[jid] = "gptmail"
            elif raw == "exzork":
                self._email_mode[jid] = "exzork"
            else:
                self._email_mode[jid] = "domain"

    def _seed_gift_from_env(self) -> None:
        try:
            from core.env import parse_env_file, HUB_ENV

            hub = parse_env_file(HUB_ENV)
            gift = (hub.get("ENTER_GIFT_CODE") or "").strip()
        except Exception:
            gift = ""
        try:
            inp = self.query_one("#gift-code", Input)
            if gift and not inp.value.strip():
                inp.value = gift
        except Exception:
            pass

    def _email_mode_for(self, job: str | None = None) -> str:
        jid = (job or self._job_id()).lower()
        mode = self._email_mode.get(jid, "domain")
        return mode if mode in ("domain", "gptmail", "exzork") else "domain"

    def _set_email_mode(self, mode: str) -> None:
        jid = self._job_id()
        if jid not in self._EMAIL_TOGGLE_JOBS:
            return
        if mode not in ("domain", "gptmail", "exzork"):
            return
        # Exzork is grok-only for now (enter stays gptmail/domain)
        if mode == "exzork" and jid != "grok":
            self._status("Exzork: grok only")
            return
        self._email_mode[jid] = mode
        self._paint_email_buttons()
        label = {
            "gptmail": "GPTMail (API)",
            "exzork": "Exzork (API)",
            "domain": "IMAP (domain)",
        }.get(mode, mode)
        self._status(f"{jid} email: {label}")
        self._log(f"[hub] {jid} EMAIL_MODE → {mode}")

    def _sync_email_row(self) -> None:
        """Show IMAP/GPTMail/Exzork toggles for grok + enter."""
        try:
            row = self.query_one("#email-row", Horizontal)
            hint = self.query_one("#email-hint", Label)
        except Exception:
            return
        jid = self._job_id()
        show = jid in self._EMAIL_TOGGLE_JOBS
        row.set_class(not show, "hidden")
        try:
            self.query_one("#btn-email-exzork", Button).display = jid == "grok"
        except Exception:
            pass
        if show:
            try:
                hint.update(f"  ({jid})")
            except Exception:
                pass
            self._paint_email_buttons()

    def _sync_gift_row(self) -> None:
        """Show gift code field only for enter."""
        try:
            row = self.query_one("#gift-row", Horizontal)
        except Exception:
            return
        row.set_class(self._job_id() != "enter", "hidden")

    def _seed_getunikey_from_env(self) -> None:
        """Seed referral URL from hub .env if present."""
        try:
            from core.env import load_hub_env

            hub = load_hub_env()
            ref = (hub.get("GETUNIKEY_REFERRAL_URL") or "").strip()
        except Exception:
            ref = ""
        try:
            inp = self.query_one("#getunikey-referral", Input)
            if ref and not inp.value.strip():
                inp.value = ref
        except Exception:
            pass

    _GMAIL_LIST_JOBS = frozenset({"getunikey", "getunikey-farm", "firecrawl", "firecrawl-farm"})

    def _sync_getunikey_rows(self) -> None:
        """Show Gmail list + referral URL for getunikey/firecrawl/tasklet; hide -n (pool-sized)."""
        jid = self._job_id()
        is_guk = jid in ("getunikey", "getunikey-farm")
        is_fc = jid in ("firecrawl", "firecrawl-farm")
        is_reauth = jid in ("grok-reauth", "grok_reauth")
        show_gmail = is_guk or is_fc
        for rid in ("#getunikey-gmail-row",):
            try:
                self.query_one(rid, Horizontal).set_class(not show_gmail, "hidden")
            except Exception:
                pass
        # Referral URL only for getunikey
        try:
            self.query_one("#getunikey-row", Horizontal).set_class(not is_guk, "hidden")
        except Exception:
            pass
        try:
            count_inp = self.query_one("#count", Input)
            count_lbl = self.query_one("#count-label", Label)
            pool_sized = is_guk or is_fc
            count_inp.disabled = pool_sized
            count_lbl.set_class(pool_sized, "dim")
            if pool_sized:
                count_inp.placeholder = "pool"
                count_inp.value = "0"
            elif is_reauth:
                count_inp.placeholder = "0=all"
                # keep user value; default 0 = full expired pool
                if (count_inp.value or "").strip() == "":
                    count_inp.value = "0"
            else:
                count_inp.placeholder = "20"
                if (count_inp.value or "").strip() in ("0", ""):
                    count_inp.value = "20"
        except Exception:
            pass

    def _parse_getunikey_gmail_list(self) -> list[str]:
        """Normalize HUD Gmail lines to email|password (accept : too)."""
        try:
            raw = self.query_one("#getunikey-gmails", TextArea).text
        except Exception:
            return []
        lines: list[str] = []
        for ln in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if "|" in s:
                email, _, password = s.partition("|")
            elif ":" in s:
                email, _, password = s.partition(":")
            else:
                continue
            email, password = email.strip(), password.strip()
            if email and password:
                lines.append(f"{email}|{password}")
        return lines

    def _paint_email_buttons(self) -> None:
        mode = self._email_mode_for()
        try:
            btn_imap = self.query_one("#btn-email-imap", Button)
            btn_gpt = self.query_one("#btn-email-gptmail", Button)
            btn_exz = self.query_one("#btn-email-exzork", Button)
            btn_imap.variant = "success" if mode == "domain" else "default"
            btn_gpt.variant = "success" if mode == "gptmail" else "default"
            btn_exz.variant = "success" if mode == "exzork" else "default"
        except Exception:
            pass

    def _seed_inject_from_env(self) -> None:
        """Default inject target: VPS if GROK_VPS_PUSH on, else Local."""
        try:
            from core.env import load_hub_env

            hub = load_hub_env()
            raw = (hub.get("GROK_VPS_PUSH") or "").strip().lower()
            self._inject_target = (
                "vps" if raw in ("1", "true", "yes", "on") else "local"
            )
        except Exception:
            self._inject_target = "local"

    def _sync_inject_row(self) -> None:
        """Show Inject Local|VPS only for grok / grok-reauth."""
        try:
            row = self.query_one("#inject-row", Horizontal)
        except Exception:
            return
        show = self._job_id() in self._INJECT_TOGGLE_JOBS
        row.set_class(not show, "hidden")
        if show:
            self._paint_inject_buttons()

    def _paint_inject_buttons(self) -> None:
        t = (self._inject_target or "local").lower()
        try:
            btn_l = self.query_one("#btn-inject-local", Button)
            btn_v = self.query_one("#btn-inject-vps", Button)
            btn_l.variant = "success" if t == "local" else "default"
            btn_v.variant = "success" if t == "vps" else "default"
        except Exception:
            pass

    def _set_inject_target(self, target: str) -> None:
        t = (target or "local").strip().lower()
        if t not in ("local", "vps"):
            t = "local"
        self._inject_target = t
        self._paint_inject_buttons()
        self._log(f"[hub] Inject → {t.upper()} 9router DB")
        self._status(f"Inject: {t}")

    @on(Button.Pressed, "#btn-inject-local")
    def on_inject_local(self) -> None:
        self._set_inject_target("local")

    @on(Button.Pressed, "#btn-inject-vps")
    def on_inject_vps(self) -> None:
        self._set_inject_target("vps")

    @on(Input.Changed, "#job")
    def on_job_changed(self, _event: Input.Changed) -> None:
        self._sync_email_row()
        self._sync_gift_row()
        self._sync_getunikey_rows()
        self._sync_inject_row()

    @on(Button.Pressed, "#btn-job-cycle")
    def on_job_cycle_btn(self) -> None:
        self.action_cycle_job()

    def action_cycle_job(self) -> None:
        """Cycle Job field: grok → enter → outlook → …"""
        ids = self._job_cycle_ids()
        if not ids:
            return
        cur = self._job_id()
        try:
            idx = ids.index(cur)
            nxt = ids[(idx + 1) % len(ids)]
        except ValueError:
            nxt = ids[0]
        try:
            self.query_one("#job", Input).value = nxt
        except Exception:
            return
        self._sync_email_row()
        self._sync_gift_row()
        self._sync_getunikey_rows()
        self._sync_inject_row()
        self._status(f"Job → {nxt}")
        self._log(f"[hub] job cycle → {nxt}")

    @on(Button.Pressed, "#btn-email-imap")
    def on_email_imap(self) -> None:
        if self._job_id() not in self._EMAIL_TOGGLE_JOBS:
            return
        self._set_email_mode("domain")

    @on(Button.Pressed, "#btn-email-gptmail")
    def on_email_gptmail(self) -> None:
        if self._job_id() not in self._EMAIL_TOGGLE_JOBS:
            return
        self._set_email_mode("gptmail")

    @on(Button.Pressed, "#btn-email-exzork")
    def on_email_exzork(self) -> None:
        if self._job_id() not in self._EMAIL_TOGGLE_JOBS:
            return
        self._set_email_mode("exzork")

    def _parse_int_field(self, field_id: str, default: int = 1) -> int:
        try:
            raw = self.query_one(f"#{field_id}", Input).value.strip()
            if raw == "" or raw == "-":
                return default
            return int(raw)
        except Exception:
            return default

    def _sync_every_n_ui(self, *, silent: bool = False) -> None:
        """Live 1:1: if everyN>0 then everyN := max(1, c). everyN=0 stays off."""
        if self._syncing_form:
            return
        from core.warp_policy import normalize_every_n

        c = max(1, self._parse_int_field("concurrent", 1))
        every_raw = self._parse_int_field("every-n", 0)
        if every_raw < 0:
            every_raw = 0
        every, note = normalize_every_n(c, every_raw)
        every_inp = self.query_one("#every-n", Input)
        cur = every_inp.value.strip()
        want = str(every)
        if cur != want:
            self._syncing_form = True
            try:
                every_inp.value = want
            finally:
                self._syncing_form = False
            if note and not silent:
                self._status(f"everyN auto → {every} (= -c)")
                # one-line log only when value actually changed due to policy
                if every_raw != every:
                    self._log(f"[hub] everyN {every_raw} → {every} (1:1 with -c {c})")

    @on(Input.Changed, "#concurrent")
    def on_concurrent_changed(self, _event: Input.Changed) -> None:
        # typing "8" → everyN becomes 8 if everyN was on
        self._sync_every_n_ui(silent=False)

    @on(Input.Changed, "#every-n")
    def on_every_n_changed(self, _event: Input.Changed) -> None:
        # user typed 3 while c=8 → snap to 8 (unless 0)
        self._sync_every_n_ui(silent=False)


    # ── progress (delegates to core.progress) ────────────────────────────────

    def _paint_progress(self) -> None:
        p = self._progress
        try:
            self.query_one("#progress", ProgressPanel).set_text(p.render())
        except Exception:
            pass
        try:
            line = p.status_line()
            if self._busy:
                line += "  BUSY"
            self.query_one(StatusBar).set_status(line)
        except Exception:
            pass

    def _reset_progress(self, target: int) -> None:
        self._progress.reset(target)
        self._paint_progress()

    # ── log ──────────────────────────────────────────────────────────────────

    def _log_widget(self) -> Log:
        return self.query_one("#log", Log)

    def _log_near_bottom(self) -> bool:
        """True if viewport is at (or near) the tail."""
        try:
            w = self._log_widget()
            max_y = float(getattr(w, "max_scroll_y", 0) or 0)
            y = float(getattr(w, "scroll_y", 0) or 0)
            if max_y <= 0:
                return True
            return y >= max_y - 2.0
        except Exception:
            return True

    def _log(self, msg: str) -> None:
        """Ingest all lines for progress; display only quiet subset."""
        raw = (msg or "").rstrip("\n")
        if self._progress.ingest(raw):
            self._paint_progress()
        if not hud_show_line(raw):
            return
        w = self._log_widget()
        w.write_line(raw)
        if self._log_follow:
            w.scroll_end(animate=False)

    def _status(self, msg: str) -> None:
        self.query_one(StatusBar).set_status(msg)

    def _thread_log(self, msg: str) -> None:
        self.call_from_thread(self._log, msg.rstrip("\n"))

    def _thread_status(self, msg: str) -> None:
        self.call_from_thread(self._status, msg)

    def action_focus_log(self) -> None:
        self._log_widget().focus()
        self._status("Log focused — PgUp pauses follow · End resumes tail")

    def action_log_end(self) -> None:
        self._log_follow = True
        self._log_widget().scroll_end(animate=False)
        self._status("Log follow ON (tail)")

    def action_log_home(self) -> None:
        self._log_follow = False
        self._log_widget().scroll_home(animate=False)
        self._status("Log follow OFF (reading history)")

    def action_log_page_up(self) -> None:
        self._log_follow = False
        self._log_widget().scroll_page_up(animate=False)

    def action_log_page_down(self) -> None:
        w = self._log_widget()
        w.scroll_page_down(animate=False)
        if self._log_near_bottom():
            self._log_follow = True

    def _read_form(self) -> tuple[str, list[str], bool, int, int, int, str | None]:
        """Returns job, args, dry, n, c, every_n, policy_note.

        getunikey: n = Gmail list size (pool); -n 0 means entire pool in farm.
        grok-reauth: -n 0 → --all (full expired/revoked pool); no -y.
        """
        from core.warp_policy import normalize_every_n

        job = self.query_one("#job", Input).value.strip() or "grok"
        jlow = job.lower()
        is_guk = jlow in ("getunikey", "getunikey-farm")
        is_fc = jlow in ("firecrawl", "firecrawl-farm")
        is_reauth = jlow in ("grok-reauth", "grok_reauth")
        try:
            c = max(1, int(self.query_one("#concurrent", Input).value.strip() or "1"))
        except ValueError:
            c = 1
        if is_guk or is_fc:
            gmails = self._parse_getunikey_gmail_list()
            n = max(1, len(gmails)) if gmails else 0
        elif is_reauth:
            # 0 = full pool (--all); >0 = limit
            try:
                raw_n = int(self.query_one("#count", Input).value.strip() or "0")
            except ValueError:
                raw_n = 0
            n = max(0, raw_n)
        else:
            try:
                n = max(1, int(self.query_one("#count", Input).value.strip() or "1"))
            except ValueError:
                n = 1
        try:
            every_raw = max(0, int(self.query_one("#every-n", Input).value.strip() or "0"))
        except ValueError:
            every_raw = 0
        # Best practice: everyN is 0 (off) or == concurrent
        every_n, note = normalize_every_n(c, every_raw)
        if note:
            # Reflect forced value in the form so user sees truth
            try:
                self.query_one("#every-n", Input).value = str(every_n)
            except Exception:
                pass
        extra_raw = self.query_one("#extra", Input).value.strip()
        try:
            extra = shlex.split(extra_raw, posix=False) if extra_raw else []
        except ValueError:
            extra = extra_raw.split()
        if is_reauth:
            # HUD Args often still has farm default -y — strip farm-only flags
            extra = [
                x
                for x in extra
                if x not in ("-y", "--yes", "--non-interactive")
            ]
            # reauth_device_oauth.py: --all when n=0
            args = ["-c", str(c)]
            if n <= 0:
                args = ["--all", *args]
            else:
                args = ["-n", str(n), *args]
            # Pre-rotate once for long pool runs
            if "--warp-rotate" not in extra and every_n > 0:
                args.append("--warp-rotate")
            # Pass through extra flags (e.g. --no-delete-on-access-denied)
            args = [*args, *extra]
            # everyN also via hub inject; CLI flag for standalone clarity
            if every_n > 0 and "--warp-every-n" not in " ".join(extra):
                args.extend(["--warp-every-n", str(every_n)])
        elif is_guk:
            if "-y" not in extra and "--yes" not in extra:
                extra = ["-y", *extra]
            args = ["-n", "0", "-c", str(c), *extra]
        elif is_fc:
            if "-y" not in extra and "--yes" not in extra:
                extra = ["-y", *extra]
            args = ["-n", str(n), "-c", str(c), *extra]
        else:
            if "-y" not in extra and "--yes" not in extra:
                extra = ["-y", *extra]
            args = ["-n", str(n), "-c", str(c), *extra]
        dry = bool(self.query_one("#chk-dry", Checkbox).value)
        return job, args, dry, n, c, every_n, note

    # ── buttons ──────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-run")
    def on_run_pressed(self) -> None:
        self.action_run()

    @on(Button.Pressed, "#btn-stop")
    def on_stop_pressed(self) -> None:
        self.action_stop_job()

    @on(Button.Pressed, "#btn-warp-st")
    def on_warp_st(self) -> None:
        self.action_warp_status()

    @on(Button.Pressed, "#btn-warp-rot")
    def on_warp_rot(self) -> None:
        self.action_warp_rotate()

    @on(Button.Pressed, "#btn-list")
    def on_list(self) -> None:
        self.action_list_jobs()

    @on(Button.Pressed, "#btn-clear")
    def on_clear(self) -> None:
        self.action_clear_log()

    @on(Button.Pressed, "#btn-quit")
    def on_quit(self) -> None:
        # stop farm first if still running, then close HUD
        try:
            from core.jobctl import is_running, stop_all

            if is_running():
                self._log("[hub] Quit: stopping active farm first…")
                stop_all(log=self._log, grace_s=2.0)
        except Exception as e:
            self._log(f"[hub] stop on quit: {e}")
        self.exit()


    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_run()

    def action_clear_log(self) -> None:
        self.query_one("#log", Log).clear()
        self._log_follow = True
        self._status("Log cleared · follow ON")

    def action_list_jobs(self) -> None:
        for j in list_jobs():
            ok = j.cwd.is_dir() and (j.cwd / j.entry).is_file()
            self._log(f"  [{('ok' if ok else 'MISSING')}] {j.id}  ->  {j.cwd}")
        self._status("Jobs listed")

    def action_run(self) -> None:
        if self._busy:
            self._log("[hub] masih jalan — Stop [S] dulu, atau tunggu selesai")
            self._status("Busy — Stop or wait")
            return
        job, args, dry, n, c, every_n, policy_note = self._read_form()
        try:
            jdef = get_job(job)
        except KeyError as e:
            self._log(f"[error] {e}")
            known = ", ".join(j.id for j in list_jobs()) or "(none)"
            self._log(f"  Tip: known jobs: {known}")
            self._status("Unknown job")
            return
        jlow = job.lower()
        env_overrides: dict[str, str] = {}
        if jlow in ("getunikey", "getunikey-farm"):
            gmails = self._parse_getunikey_gmail_list()
            if not gmails:
                self._log("[hub] ERROR: Gmail list empty — paste email|password lines")
                self._status("getunikey: Gmail list required")
                return
            n = len(gmails)
            env_overrides["GETUNIKEY_ACCOUNTS_LIST"] = "\n".join(gmails)
            self._log(f"[hub] getunikey Gmail list: {n} account(s) → pool")
            try:
                ref = self.query_one("#getunikey-referral", Input).value.strip()
            except Exception:
                ref = ""
            if ref:
                env_overrides["GETUNIKEY_REFERRAL_URL"] = ref
                self._log(f"[hub] GETUNIKEY_REFERRAL_URL={ref[:72]}")
            else:
                self._log(
                    "[hub] referral empty — farm default "
                    "https://www.getunikey.ai/sign-up?aff=bTOY"
                )
        elif jlow in ("firecrawl", "firecrawl-farm"):
            gmails = self._parse_getunikey_gmail_list()
            if not gmails:
                self._log("[hub] ERROR: Gmail list empty — paste email|password lines")
                self._status("firecrawl: Gmail list required")
                return
            n = len(gmails)
            accounts_file = Path(jdef.cwd) / "google_accounts.txt"
            accounts_file.write_text(
                "\n".join(gmails) + "\n", encoding="utf-8"
            )
            self._log(f"[hub] firecrawl Gmail list: {n} account(s) → {accounts_file.name}")
        # Jobs with warp_enabled=False (outlook): ignore form everyN / no auto-connect
        warp_ok = bool(getattr(jdef, "warp_enabled", True))
        if not warp_ok:
            if every_n > 0:
                self._log(
                    f"[hub] WARP disabled for job={jdef.id} "
                    f"— everyN {every_n} ignored (form c={c} still used for farm only)"
                )
            every_n = 0
            policy_note = ""
        self._busy = True
        # reauth -n 0 = full pool; target fills from "Targets: N" log line
        if jlow in ("grok-reauth", "grok_reauth"):
            prog_target = 0 if dry else (n if n > 0 else 0)
        else:
            prog_target = 0 if dry else max(n, 1)
        self._reset_progress(prog_target)
        if policy_note:
            self._log(f"[hub] WARP policy: {policy_note}")
        n_label = "all" if (jlow in ("grok-reauth", "grok_reauth") and n <= 0) else str(n)
        self._status(
            f"Running {job} n={n_label} c={c}"
            + (f" everyN={every_n}" if every_n else " everyN=off")
            + ("" if warp_ok else " WARP=off")
            + (" dry" if dry else "")
        )
        if jlow in self._EMAIL_TOGGLE_JOBS:
            mode = self._email_mode_for(jlow)
            if jlow == "grok":
                env_overrides["GROK_EMAIL_MODE"] = mode
                env_overrides["EMAIL_MODE"] = mode
            elif jlow == "enter":
                env_overrides["ENTER_EMAIL_MODE"] = mode
                # do NOT override shared EMAIL_MODE (keep grok/outlook domain)
            self._log(f"[hub] {jlow} EMAIL_MODE={mode} (HUD)")
        if jlow == "enter":
            try:
                gift = self.query_one("#gift-code", Input).value.strip()
            except Exception:
                gift = ""
            if gift:
                env_overrides["ENTER_GIFT_CODE"] = gift
                self._log(f"[hub] ENTER_GIFT_CODE set ({gift[:4]}…{gift[-2:] if len(gift) > 6 else ''})")
            else:
                self._log("[hub] WARN: Gift empty — farm uses ENTER_GIFT_CODE from .env / default")
        # Grok inject target: Local 9router DB only vs VPS merge push
        if jlow in self._INJECT_TOGGLE_JOBS:
            inj = (self._inject_target or "local").lower()
            if inj == "vps":
                env_overrides["GROK_VPS_PUSH"] = "1"
                env_overrides["GROK_INJECT_TARGET"] = "vps"
            else:
                env_overrides["GROK_VPS_PUSH"] = "0"
                env_overrides["GROK_INJECT_TARGET"] = "local"
            self._log(f"[hub] Inject target={inj} (GROK_VPS_PUSH={env_overrides['GROK_VPS_PUSH']})")
        env_ov: dict[str, str] | None = env_overrides or None
        if not dry:
            if warp_ok:
                self._log("[hub] WARP: auto-connect on job start")
            else:
                self._log(f"[hub] WARP: skipped for job={jdef.id} (warp_enabled=false)")
        self._log(
            f"-- run {job}  {' '.join(args)}  c={c} everyN={every_n or 'off'} "
            f"warp={'on' if warp_ok else 'off'} dry={dry}"
        )
        self._log_follow = True
        self._log_widget().focus()
        self._log_widget().scroll_end(animate=False)
        self._run_job_worker(job, args, dry, every_n, env_ov, warp_ok)

    def action_stop_job(self) -> None:
        """Global stop: kill farm process tree (Camoufox children)."""
        self._log("[hub] STOP requested…")
        self._status("Stopping…")
        self._stop_worker()

    def action_warp_status(self) -> None:
        self._status("WARP status...")
        self._warp_worker("status")

    def action_warp_rotate(self) -> None:
        if self._busy:
            self._log("[hub] farm busy — use Stop first, or wait for everyN rotate")
            return
        self._status("WARP rotate...")
        self._warp_worker("rotate")


    @work(exclusive=True, thread=True)
    def _run_job_worker(
        self,
        job: str,
        args: list[str],
        dry: bool,
        every_n: int = 0,
        env_overrides: dict[str, str] | None = None,
        warp_ok: bool = True,
    ) -> None:
        try:
            result = run_job(
                job,
                args,
                # Default jobs: connect WARP on start (even if everyN=0).
                # Jobs with warp_enabled=False (outlook): never connect / inject / rotate.
                warp_connect=bool(warp_ok),
                warp_rotate=False,
                warp_every_n=(every_n if warp_ok else 0),
                dry_run=dry,
                log=self._thread_log,
                env_overrides=env_overrides,
            )
            if result.stopped:
                self._thread_status(f"STOPPED ({result.duration_s:.0f}s)")
            else:
                self._thread_status(
                    f"Done exit={result.exit_code} ({result.duration_s:.0f}s)"
                    + (" OK" if result.ok else " FAIL")
                )
            if result.ok and not dry and not result.stopped:
                self._thread_log(
                    f"[hub] cek results: farms/{job}/results/ (batch terbaru)"
                )
            self.call_from_thread(self._paint_progress)
        except Exception as e:
            self._thread_log(f"[error] {type(e).__name__}: {e}")
            self._thread_status(f"Error: {e}")
        finally:
            self.call_from_thread(self._clear_busy)

    def _clear_busy(self) -> None:
        self._busy = False
        self._paint_progress()

    @work(exclusive=True, thread=True)
    def _stop_worker(self) -> None:
        try:
            from core.jobctl import stop_all, active_summary

            self._thread_log(f"[jobctl] active: {active_summary()}")
            rows = stop_all(log=self._thread_log, grace_s=2.0)
            self._thread_log(f"[jobctl] stop done ({len(rows)} handle(s))")
            self._thread_status("Stopped" if rows else "Nothing running")
        except Exception as e:
            self._thread_log(f"[jobctl] error: {e}")
            self._thread_status(f"Stop error: {e}")
        finally:
            self.call_from_thread(self._clear_busy)

    @work(exclusive=True, thread=True)
    def _warp_worker(self, mode: str, quiet: bool = False) -> None:
        try:
            from core.warp import WarpClient, public_ip

            w = WarpClient(log=(lambda _m: None) if quiet else self._thread_log)
            if mode == "status":
                st = w.status_text().replace("\n", " | ")
                ip = public_ip()
                conn = w.is_connected()
                self._thread_log(f"WARP connected={conn} ip={ip} | {st[:120]}")
                self._thread_status(f"WARP {'ON' if conn else 'OFF'} {ip}")
            else:
                r = w.rotate_ip(force=True)
                self._thread_log(str(r))
                self._thread_status(f"WARP {r.before}->{r.after} changed={r.changed}")
        except Exception as e:
            self._thread_log(f"[warp error] {e}")
            self._thread_status(f"WARP error: {e}")


@cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    tui: bool = typer.Option(True, "--tui/--no-tui", help="Open HUD (default)"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if tui:
        HubApp().run()
        return
    console.print("Use: python -m jobs ...  or  python app.py (HUD)")
    raise typer.Exit(0)


@cli.command("list")
def cmd_list() -> None:
    for j in list_jobs():
        ok = j.cwd.is_dir() and (j.cwd / j.entry).is_file()
        flag = "ok" if ok else "MISS"
        console.print(f"{j.id:8} [{flag}] {j.cwd}", markup=False)


@cli.command("run")
def cmd_run(
    job: str = typer.Argument("grok"),
    n: int = typer.Option(1, "-n", "--count"),
    c: int = typer.Option(1, "--concurrent"),
    yes: bool = typer.Option(True, "--yes/--ask", "-y"),
    warp_rotate: bool = typer.Option(False, "--warp-rotate"),
    warp_every_n: int = typer.Option(
        0, "--warp-every-n", help="0=off; if >0 forced to equal -c (1:1 wave)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    from core.warp_policy import normalize_every_n

    args = ["-n", str(n), "-c", str(c)]
    if yes:
        args.append("-y")
    every, note = normalize_every_n(c, max(0, warp_every_n))
    if note:
        console.print(f"[hub] WARP policy: {note}")
    result = run_job(
        job,
        args,
        warp_connect=warp_rotate or every > 0,
        warp_rotate=warp_rotate,
        warp_every_n=every,
        dry_run=dry_run,
    )
    raise typer.Exit(0 if result.ok else (result.exit_code or 1))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    cli()
