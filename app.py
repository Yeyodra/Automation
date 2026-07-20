"""Automation hub — Textual HUD (default) + thin CLI.

  python app.py                 # TUI
  python app.py --no-tui list
  python app.py run grok -n 1

Progress tracking is GLOBAL (core.progress.BatchProgress).
Any farm that prints the hub log contract is auto-tracked.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import typer
from rich.console import Console
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Log, Static

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
        max-height: 8;
        border: solid $accent;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    #form .row { height: 3; margin-top: 0; }
    #form Label { width: auto; color: $text-muted; padding: 0 1 0 0; }
    #form Input { width: 1fr; }
    #form Checkbox { width: auto; margin-right: 2; }
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
        Binding("f", "focus_log", "Log", show=True),
        Binding("end", "log_end", "End", show=False),
        Binding("home", "log_home", "Home", show=False),
        Binding("pageup", "log_page_up", "PgUp", show=False),
        Binding("pagedown", "log_page_down", "PgDn", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._progress = BatchProgress()
        self._syncing_form = False  # prevent Input.Changed feedback loop

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            with Vertical(id="form"):
                with Horizontal(classes="row"):
                    yield Label("Job")
                    yield Input(value="grok", id="job", placeholder="grok")
                    yield Label("  -n")
                    yield Input(value="20", id="count", placeholder="20")
                    yield Label("  -c")
                    yield Input(value="3", id="concurrent", placeholder="3")
                with Horizontal(classes="row"):
                    yield Label("Args")
                    yield Input(value="-y", id="extra", placeholder="-y --headless")
                    yield Label(" everyN")
                    yield Input(
                        value="3",
                        id="every-n",
                        placeholder="0=off else =c",
                    )
                    yield Checkbox("Dry-run", id="chk-dry", value=False)
            yield ProgressPanel("Progress: idle — set -n then Run", id="progress")
            with Horizontal(id="actions"):
                yield Button("Run [R]", id="btn-run", variant="success")
                yield Button("Stop [S]", id="btn-stop", variant="error")
                yield Button("Status [W]", id="btn-warp-st", variant="primary")
                yield Button("Rotate", id="btn-warp-rot", variant="warning")
                yield Button("Jobs [L]", id="btn-list")
                yield Button("Clear [C]", id="btn-clear")
                yield Button("Quit [Q]", id="btn-quit")
            log = Log(id="log", highlight=True, max_lines=5000, auto_scroll=True)
            log.can_focus = True
            yield log
        yield StatusBar(
            "everyN=c (live) · 0=off · S=Stop · R=Run · Q=Quit"
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#job", Input).focus()
        jobs = ", ".join(j.id for j in list_jobs()) or "(none)"
        self._log(f"Hub ready. Jobs: {jobs}")
        self._log("everyN LIVE sync: ubah -c → everyN ikut (kalau everyN≠0). 0=off.")
        self._log("Stop [S]=kill farm · Status/Rotate=manual WARP · Quit=close HUD")
        self._sync_every_n_ui(silent=True)
        self._paint_progress()
        self._warp_worker("status", quiet=True)

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

    def _log(self, msg: str) -> None:
        w = self._log_widget()
        w.write_line(msg)
        if w.auto_scroll:
            w.scroll_end(animate=False)
        if self._progress.ingest(msg):
            self._paint_progress()

    def _status(self, msg: str) -> None:
        self.query_one(StatusBar).set_status(msg)

    def _thread_log(self, msg: str) -> None:
        self.call_from_thread(self._log, msg.rstrip("\n"))

    def _thread_status(self, msg: str) -> None:
        self.call_from_thread(self._status, msg)

    def action_focus_log(self) -> None:
        self._log_widget().focus()
        self._status("Log focused — scroll wheel / PgUp / PgDn / End")

    def action_log_end(self) -> None:
        self._log_widget().scroll_end(animate=False)

    def action_log_home(self) -> None:
        self._log_widget().scroll_home(animate=False)

    def action_log_page_up(self) -> None:
        self._log_widget().scroll_page_up(animate=False)

    def action_log_page_down(self) -> None:
        self._log_widget().scroll_page_down(animate=False)

    def _read_form(self) -> tuple[str, list[str], bool, int, int, int, str | None]:
        """Returns job, args, dry, n, c, every_n, policy_note."""
        from core.warp_policy import normalize_every_n

        job = self.query_one("#job", Input).value.strip() or "grok"
        try:
            n = max(1, int(self.query_one("#count", Input).value.strip() or "1"))
        except ValueError:
            n = 1
        try:
            c = max(1, int(self.query_one("#concurrent", Input).value.strip() or "1"))
        except ValueError:
            c = 1
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
        self._status("Log cleared")

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
            get_job(job)
        except KeyError as e:
            self._log(f"[error] {e}")
            self._log("  Tip: job yang ada cuma 'grok' (enter belum).")
            self._status("Unknown job")
            return
        self._busy = True
        self._reset_progress(0 if dry else n)
        if policy_note:
            self._log(f"[hub] WARP policy: {policy_note}")
        self._status(
            f"Running {job} n={n} c={c}"
            + (f" everyN={every_n}" if every_n else " everyN=off")
            + (" dry" if dry else "")
        )
        self._log(
            f"-- run {job}  {' '.join(args)}  c={c} everyN={every_n or 'off'} dry={dry}"
        )
        self._log_widget().focus()
        self._log_widget().auto_scroll = True
        self._run_job_worker(job, args, dry, every_n)

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
    ) -> None:
        try:
            result = run_job(
                job,
                args,
                warp_connect=every_n > 0,
                warp_rotate=False,  # pre-rotate: CLI --warp-rotate only
                warp_every_n=every_n,
                dry_run=dry,
                log=self._thread_log,
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
