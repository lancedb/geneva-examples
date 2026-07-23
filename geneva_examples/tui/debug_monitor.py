"""Live job-monitor TUI behind ``uv run debug watch``.

A compact "mission control" for one backfill/refresh job: status + phase,
progress bars for the record's metrics, a rolling throughput estimate, the
trailing event log, and the diagnosis findings from
:mod:`geneva_examples.core.diagnose` — refreshed on a poll interval.

The polling source is injected (``LiveSource`` for a cluster job,
``ReplaySource`` for recorded snapshots), so the same screen powers real
debugging and offline demos.
"""

from __future__ import annotations

from collections import deque
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, RichLog, Static

from geneva_examples.core import diagnose as dx

_STATUS_MARKUP = {
    "RUNNING": "[bold green]RUNNING[/]",
    "DONE": "[bold green]DONE[/]",
    "FAILED": "[bold red]FAILED[/]",
    "CANCELLED": "[bold yellow]CANCELLED[/]",
    "PENDING": "[bold yellow]PENDING[/]",
}

_SEV_MARKUP = {
    dx.SEV_CRIT: "[bold red]",
    dx.SEV_WARN: "[yellow]",
    dx.SEV_INFO: "[cyan]",
}


def _bar(n: int, total: int, width: int = 28) -> str:
    """A plain-text progress bar: ``###.......  n/total``."""
    if total <= 0:
        return f"{n}"
    filled = min(width, round(width * min(n, total) / total))
    return f"{'#' * filled}{'.' * (width - filled)}  {n}/{total}"


class JobMonitorApp(App):
    """Poll one job's record and render it as a live dashboard."""

    TITLE = "geneva debug watch"

    CSS = """
    #status   { height: auto; padding: 0 1; border: round $accent; }
    #metrics  { height: auto; min-height: 3; padding: 0 1;
                border: round $primary; }
    #findings { height: auto; min-height: 3; padding: 0 1;
                border: round $warning; }
    #events   { border: round $panel; }
    """

    BINDINGS: ClassVar = [("q", "quit", "Quit")]

    def __init__(
        self,
        source: Any,
        refresh_secs: float = 3.0,
        errors_every: int = 5,
    ) -> None:
        super().__init__()
        self._source = source
        self._refresh_secs = max(0.2, refresh_secs)
        self._errors_every = max(1, errors_every)
        self._samples: deque[dx.Sample] = deque(maxlen=20)
        self._errors: list = []
        self._tick_no = 0
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        yield Static(id="metrics")
        yield Static(id="findings")
        yield RichLog(id="events", markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._seen_events = 0
        self.sub_title = f"job {getattr(self._source, 'job_id', '?')}"
        self.set_interval(self._refresh_secs, self._schedule_poll)
        self._schedule_poll()

    # -- polling ------------------------------------------------------------

    def _schedule_poll(self) -> None:
        if not self._finished:
            self.run_worker(self._poll, thread=True, exclusive=True)

    def _poll(self) -> None:
        """Worker thread: fetch the record (and occasionally errors)."""
        try:
            record = self._source.fetch()
        except Exception as exc:  # noqa: BLE001 — keep the screen alive
            self.call_from_thread(self._render_error, exc)
            return
        self._tick_no += 1
        if self._tick_no % self._errors_every == 1:
            self._errors = self._source.errors()
        self.call_from_thread(self._render, record)

    # -- rendering ----------------------------------------------------------

    def _render_error(self, exc: Exception) -> None:
        self.query_one("#status", Static).update(
            f"[bold red]poll failed:[/] {exc} — retrying"
        )

    def _render(self, record: object) -> None:
        status = dx.status_of(record)
        phase = dx.phase_of(getattr(record, "events", None))

        self._samples.append(dx.sample(record))
        rate = None
        if len(self._samples) > 1:
            rate = dx.rate_per_second(self._samples[0], self._samples[-1])

        target = (
            f"{getattr(record, 'table_name', '-')}."
            f"{getattr(record, 'column_name', '-')}"
        )
        rate_txt = f"   ~{rate:.1f} rows/s" if rate is not None else ""
        self.query_one("#status", Static).update(
            f"{_STATUS_MARKUP.get(status, status)}  {target}"
            f"   phase: {phase or '-'}{rate_txt}"
            f"   errors: {len(self._errors)}"
        )

        lines = []
        for m in getattr(record, "metrics", None) or []:
            name = str(getattr(m, "name", "?"))
            n = int(getattr(m, "n", 0) or 0)
            total = int(getattr(m, "total", 0) or 0)
            lines.append(f"{name:>22}  {_bar(n, total)}")
        metrics_widget = self.query_one("#metrics", Static)
        metrics_widget.update("\n".join(lines) or "[dim]no metrics yet[/]")

        findings = dx.diagnose(
            record,
            rate=rate,
            error_count=len(self._errors) or None,
            now=self._source.record_now(record),
        )
        rendered = [
            f"{_SEV_MARKUP.get(f.severity, '')}[{f.severity.upper()}][/] "
            f"{f.signal}\n    next: {f.action}"
            for f in findings[:4]
        ]
        self.query_one("#findings", Static).update(
            "\n".join(rendered) or "[dim]no findings[/]"
        )

        events = getattr(record, "events", None) or []
        log = self.query_one("#events", RichLog)
        for event in events[self._seen_events :]:
            log.write(str(event))
        self._seen_events = len(events)

        if status in dx.TERMINAL_STATUSES and not self._finished:
            self._finished = True
            log.write(f"--- job reached terminal state {status}; press q ---")
