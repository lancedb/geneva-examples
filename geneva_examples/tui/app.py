"""Textual TUI: browse/run example pipelines, view database tables, inspect jobs.

The left nav has three sections:

* **Tables** — a read-only viewer, and the view the app opens on (with a fresh
  listing). *Refresh* lists the tables in the connected database (using the
  current mode/config controls); selecting one shows a sample of its rows in a
  data grid.
* **Jobs** — the same job records the ``jobs`` CLI reads (geneva has no
  streaming log API, so the record's append-only event list *is* the log).
  *Refresh* lists recent jobs newest-first across every status; selecting one
  shows its full record, and ``f`` follows a running job by re-reading it.
* **Examples** — a tree of examples → steps (from the registry). Selecting a step
  shows its markdown description and a form built from its ``Param`` spec; **Run**
  launches the step's generated CLI in a subprocess and streams its output.

Steps run as a subprocess (not an in-process thread) deliberately: Ray needs a
real stdout file descriptor, which Textual's captured stdout doesn't provide.
Output is streamed via a thread-safe queue drained by a UI timer, so the reader
thread never blocks the event loop. Table and job reads (a plain Lance scan or a
history lookup, no Ray) run in a worker thread and post a single update back.
"""

from __future__ import annotations

import queue
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    Switch,
    Tree,
)

from geneva_examples.core.common import connect, format_cell
from geneva_examples.core.config import load_config
from geneva_examples.core.jobs import (
    ALL_STATUSES,
    TERMINAL_STATUSES,
    elapsed,
    format_detail,
    job_status,
    job_target,
    progress_summary,
    sort_newest_first,
    status_counts,
)
from geneva_examples.core.jobs import list_jobs as query_jobs
from geneva_examples.core.spec import Example, Param, Step
from geneva_examples.examples import all_examples
from geneva_examples.tui.forms import field_id, initial_text

# Always an explicit mode — no "auto" that defers to config.yaml. In an
# interactive app there is no command line recording what you picked, so the
# control should read as exactly the backend you are connected to.
_MODES = [("local", "local"), ("enterprise", "enterprise")]
_LEVELS = [(lvl, lvl) for lvl in ("INFO", "DEBUG", "WARNING", "ERROR")]
_TABLE_ROW_LIMIT = 100
_JOB_LIST_LIMIT = 50
# Follow interval for a non-terminal job. Geneva has no streaming log API, so
# "following" is a re-read of the job record — the same poll `jobs tail` does.
_JOB_POLL_SECONDS = 3.0

# Geneva system tables worth browsing after a backfill: the job records and
# the per-row error store. They live in the connection's system namespace.
# Each maps to (timestamp column, unique key column): the viewer scans just
# that narrow pair, sorts newest-first, then fetches the top rows by key —
# geneva 0.14 accepts but ignores order_by on these scans, so sorting
# server-side isn't an option and a bare limit() would keep the oldest rows.
_SYSTEM_TABLES = {
    "geneva_jobs": ("launched_at", "job_id"),
    "geneva_errors": ("timestamp", "error_id"),
}


_DETAIL_PLACEHOLDER = "select a cell to see its full value"
_JOB_PLACEHOLDER = "select a job on the left (press [b]j[/b] to list them)"


def _detail_text(value) -> str:
    """The complete, untruncated rendering of one cell for the detail pane."""
    if value is None:
        return "(null)"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)


# Job status is the first thing you look for in a list of jobs, so it carries
# the only colour in the pane. Values are rich styles, applied via Text.assemble
# rather than markup — see _show_job.
_STATUS_STYLES = {
    "RUNNING": "cyan",
    "PENDING": "yellow",
    "DONE": "green",
    "FAILED": "bold red",
    "CANCELLED": "yellow",
}


def _open_any_table(conn, name: str, *, system: bool = False):
    """Open a regular table, or a geneva system table via its namespace."""
    if not system:
        return conn.open_table(name)
    namespace = list(getattr(conn, "system_namespace", None) or [])
    return conn.open_table(name, namespace=namespace)


def _fetch_newest_first(
    table, cols: list[str], where: str | None, ts_col: str, key_col: str, limit: int
) -> tuple[int, list[dict]]:
    """The newest ``limit`` rows of a system table, newest first.

    Two narrow passes through public query APIs: scan ``(ts, key)`` for every
    matching row, pick the newest keys client-side, then fetch only those rows
    in full. The key scan stays small even when the full rows carry fat
    payloads like ``error_trace``.
    """
    narrow = table.search()
    if where:
        narrow = narrow.where(where)
    index = narrow.select([ts_col, key_col]).to_list()
    index.sort(key=lambda r: (r.get(ts_col) is not None, r.get(ts_col) or 0))
    index.reverse()
    newest = index[:limit]
    if not newest:
        return len(index), []

    keys = ",".join("'{}'".format(str(r[key_col]).replace("'", "")) for r in newest)
    rows = (
        table.search()
        .where(f"{key_col} IN ({keys})")
        .select(cols)
        .limit(limit)
        .to_list()
    )
    order = {str(r[key_col]): i for i, r in enumerate(newest)}
    rows.sort(key=lambda r: order.get(str(r.get(key_col)), len(order)))
    return len(index), rows


class GenevaTUI(App):
    """Interactive runner + table viewer for the geneva-examples pipelines."""

    CSS = """
    #body { height: 1fr; }
    #nav { width: 38; border-right: solid $panel; }
    #right { padding: 0 1; }
    #controls { height: auto; padding: 1 0; }
    #controls Select { width: 26; }
    #controls Input { width: 24; }
    #main { height: 1fr; }
    #desc { height: auto; max-height: 45%; border-bottom: solid $panel; }
    #form { height: 1fr; padding: 0 1; }
    #log { height: 40%; border-top: solid $panel; }
    #table-info { height: auto; padding: 0 0 1 0; color: $text-muted; }
    #table-filter { display: none; }
    #table-view { height: 1fr; }
    #cell-detail { height: auto; min-height: 8; max-height: 60%;
                   border-top: solid $panel; padding: 0 1; }
    #cell-detail.expanded { min-height: 60%; max-height: 85%; }
    #job-info { height: auto; padding: 0 0 1 0; }
    #job-detail { height: 1fr; border-top: solid $panel; padding: 0 1; }
    .field-label { color: $text-muted; }
    """

    BINDINGS: ClassVar = [
        ("r", "run", "Run / refresh"),
        ("t", "refresh_tables", "List tables"),
        ("j", "refresh_jobs", "List jobs"),
        ("f", "toggle_follow", "Follow job"),
        ("d", "toggle_detail", "Detail size"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._examples = all_examples()
        self._current: tuple[Example, Step] | None = None
        self._fields: dict[str, tuple[Param, object]] = {}
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._tables_node = None
        self._current_table: str | None = None
        self._current_table_system = False
        # Raw rows behind the grid: cells render truncated via format_cell,
        # so the detail pane resolves full values from these by coordinate.
        self._table_cols: list[str] = []
        self._table_rows: list[dict] = []
        self._jobs_node = None
        self._current_job: str | None = None
        self._current_job_terminal = True
        self._following = False
        self._follow_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            yield Tree("nav", id="nav")
            with Vertical(id="right"):
                with Horizontal(id="controls"):
                    yield Select(_MODES, value="local", allow_blank=False, id="mode")
                    yield Input(placeholder="config.yaml (optional)", id="config")
                    # Enterprise-only: geneva ignores db_uri on the local backend
                    # (it connects to local_db_path). Shown/hidden by mode below.
                    yield Input(placeholder="db_uri override (optional)", id="db_uri")
                    yield Select(
                        _LEVELS, value="INFO", allow_blank=False, id="log_level"
                    )
                    yield Button("Refresh ⟳", variant="success", id="run")
                with ContentSwitcher(initial="table-pane", id="main"):
                    with Vertical(id="run-pane"):
                        yield Markdown(
                            "# geneva-examples\n\nSelect a step on the left.", id="desc"
                        )
                        yield VerticalScroll(id="form")
                        yield RichLog(id="log", highlight=True, markup=True, wrap=True)
                    with Vertical(id="table-pane"):
                        yield Static("Select a table on the left.", id="table-info")
                        yield Input(
                            placeholder=(
                                "filter: job_id contains …  (Enter to apply; "
                                "a prefix works; blank shows all rows)"
                            ),
                            id="table-filter",
                        )
                        yield DataTable(id="table-view", zebra_stripes=True)
                        with VerticalScroll(id="cell-detail"):
                            yield Static(_DETAIL_PLACEHOLDER, id="cell-value")
                    with Vertical(id="job-pane"):
                        yield Static(_JOB_PLACEHOLDER, id="job-info")
                        with VerticalScroll(id="job-detail"):
                            yield Static("", id="job-detail-value")
        yield Footer()

    async def on_mount(self) -> None:
        # Drain queued log lines onto the RichLog on the UI thread (10 Hz).
        self.set_interval(0.1, self._drain_log)
        self._sync_db_uri_visibility()
        tree = self.query_one("#nav", Tree)
        tree.show_root = False
        tree.root.expand()

        # Tables lead the nav: after a run, inspecting data (and the geneva
        # system tables) is the more frequent destination than re-running.
        self._tables_node = tree.root.add("Tables", expand=True)
        self._tables_node.add_leaf("↻ refresh", data=("tables-refresh",))

        # Jobs sit between the data and the pipelines that produced it: after a
        # run you either look at the rows or at why there aren't any. Listed on
        # demand (unlike Tables) so startup opens exactly one connection.
        self._jobs_node = tree.root.add("Jobs", expand=True)
        self._jobs_node.add_leaf("↻ refresh", data=("jobs-refresh",))

        examples = tree.root.add("Examples", expand=True)
        first: tuple[Example, Step] | None = None
        for ex in self._examples:
            node = examples.add(ex.title, data=("example", ex), expand=True)
            for step in ex.steps:
                node.add_leaf(step.key, data=("step", ex, step))
                if first is None:
                    first = (ex, step)

        if first is not None:
            # Pre-populate the run pane so the first step-click is instant…
            await self._select(*first)
        # …but land on the Tables view with a fresh listing: inspecting data
        # is the primary entry point; the run pane is one step-click away.
        self._list_tables(self._build_cfg())

    # --- selection --------------------------------------------------------

    @on(Tree.NodeSelected)
    async def _on_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        kind = data[0]
        # Navigating away from the job view stops the poll: no background
        # re-connecting for a pane nobody is looking at.
        if kind not in ("job", "jobs-refresh"):
            self._stop_follow()
        switcher = self.query_one("#main", ContentSwitcher)
        run_button = self.query_one("#run", Button)
        if kind == "step":
            switcher.current = "run-pane"
            run_button.label = "Run ▶"
            await self._select(data[1], data[2])
        elif kind == "example":
            switcher.current = "run-pane"
            run_button.label = "Run ▶"
            ex = data[1]
            self.query_one("#desc", Markdown).update(
                f"# {ex.title}\n\n{ex.description}"
            )
        elif kind == "jobs-refresh":
            self._list_jobs(self._build_cfg())
        elif kind == "job":
            switcher.current = "job-pane"
            run_button.label = "Refresh ⟳"
            self._select_job(data[1])
        elif kind == "tables-refresh":
            self._list_tables(self._build_cfg())
        elif kind == "table":
            switcher.current = "table-pane"
            run_button.label = "Refresh ⟳"
            self._current_table = data[1]
            self._current_table_system = len(data) > 2 and bool(data[2])
            # System tables (geneva_jobs / geneva_errors) carry a job_id
            # column, so they get the job_id filter box; plain tables don't.
            self.query_one("#table-filter", Input).display = self._current_table_system
            self.query_one("#table-info", Static).update(f"loading {data[1]}…")
            self._load_table(
                self._build_cfg(),
                data[1],
                self._current_table_system,
                self._job_id_filter(),
            )

    async def _select(self, example: Example, step: Step) -> None:
        self._current = (example, step)
        hints = []
        if step.gpu:
            hints.append("_GPU model — runs on CPU in local mode._")
        if step.requires:
            hints.append(f"_Requires: {step.requires}._")
        hint_text = ("\n\n" + "  \n".join(hints)) if hints else ""
        self.query_one("#desc", Markdown).update(
            f"# {example.title} → {step.title}\n\n{step.description}{hint_text}"
        )
        await self._build_form(step)

    async def _build_form(self, step: Step) -> None:
        form = self.query_one("#form", VerticalScroll)
        # Await removal so the old field ids are gone before we mount new ones.
        await form.remove_children()
        self._fields = {}
        widgets: list[object] = []
        for param in step.params:
            wid = field_id(param)
            widgets.append(Label(f"{param.name} — {param.help}", classes="field-label"))
            if param.type is bool:
                widget: object = Switch(value=bool(param.default), id=wid)
            elif param.choices is not None:
                widget = Select(
                    [(c, c) for c in param.choices],
                    value=param.default,
                    allow_blank=False,
                    id=wid,
                )
            else:
                widget = Input(value=initial_text(param), id=wid)
            widgets.append(widget)
            self._fields[param.name] = (param, widget)
        if widgets:
            await form.mount(*widgets)  # type: ignore[arg-type]

    @on(Select.Changed, "#mode")
    def _on_mode_changed(self, _event: Select.Changed) -> None:
        self._sync_db_uri_visibility()

    def _sync_db_uri_visibility(self) -> None:
        """Show the db_uri field only on the backend that reads it.

        Local mode connects to ``local_db_path`` and ignores ``db_uri`` entirely,
        so leaving the box visible there invites edits that do nothing.
        """
        enterprise = self.query_one("#mode", Select).value == "enterprise"
        self.query_one("#db_uri", Input).display = enterprise

    def _db_uri_override(self) -> str | None:
        """The db_uri override — only read on the backend that honors it."""
        if self.query_one("#mode", Select).value != "enterprise":
            return None
        return self.query_one("#db_uri", Input).value.strip() or None

    def _build_cfg(self):
        """Build a Config from the current global controls (main thread)."""
        from pathlib import Path

        mode = self.query_one("#mode", Select).value
        config = self.query_one("#config", Input).value.strip()
        return load_config(
            Path(config) if config else None,
            mode_override=mode,
            # Only meaningful on the enterprise backend, where the field shows.
            db_uri_override=self._db_uri_override(),
        )

    # --- table viewer -----------------------------------------------------

    def action_refresh_tables(self) -> None:
        self._list_tables(self._build_cfg())

    def _job_id_filter(self) -> str | None:
        """The job_id filter value — only meaningful on system tables."""
        if not self._current_table_system:
            return None
        return self.query_one("#table-filter", Input).value.strip() or None

    @on(Input.Submitted, "#table-filter")
    def _on_filter_submitted(self, _event: Input.Submitted) -> None:
        if self._current_table and self._current_table_system:
            self.query_one("#table-info", Static).update(
                f"filtering {self._current_table}…"
            )
            self._load_table(
                self._build_cfg(),
                self._current_table,
                True,
                self._job_id_filter(),
            )

    @work(thread=True, group="viewer", exclusive=True)
    def _list_tables(self, cfg) -> None:
        try:
            conn = connect(cfg)
            names = sorted(conn.table_names())
            # Geneva's system tables (job records, per-row error store) live in
            # a separate namespace, so table_names() never lists them — probe
            # each so failed backfills can be analyzed right here.
            system = []
            for name in _SYSTEM_TABLES:
                try:
                    _open_any_table(conn, name, system=True)
                    system.append(name)
                except Exception:  # noqa: BLE001 - absent until first job
                    pass
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the tree
            names, system, err = [], [], f"{type(exc).__name__}: {exc}"
        self.call_from_thread(self._set_table_names, names, system, err)

    def _set_table_names(
        self, names: list[str], system: list[str], err: str | None
    ) -> None:
        node = self._tables_node
        if node is None:
            return
        node.remove_children()
        node.add_leaf("↻ refresh", data=("tables-refresh",))
        if err:
            node.add_leaf(f"⚠ {err[:48]}", data=None)
        elif not names and not system:
            node.add_leaf("(no tables)", data=None)
        else:
            for name in names:
                node.add_leaf(name, data=("table", name, False))
            for name in system:
                node.add_leaf(f"{name} (system)", data=("table", name, True))
        node.expand()

    @work(thread=True, group="viewer", exclusive=True)
    def _load_table(
        self,
        cfg,
        name: str,
        system: bool = False,
        job_id: str | None = None,
    ) -> None:
        # job_id values are hex/uuid strings; drop quotes rather than trying
        # to escape them so the predicate below can't be broken open. Partial
        # ids match too — pasting the 8-char prefix from a log line is enough.
        job_id = (job_id or "").strip().replace("'", "") or None
        where = f"job_id LIKE '%{job_id}%'" if job_id else None
        try:
            conn = connect(cfg)
            table = _open_any_table(conn, name, system=system)
            cols = list(table.schema.names)
            if system and "job_id" in cols:
                # job_id leads on geneva_jobs/geneva_errors — it's the key
                # you filter and correlate on.
                cols.insert(0, cols.pop(cols.index("job_id")))
            ts_col, key_col = (
                _SYSTEM_TABLES.get(name, (None, None)) if system else (None, None)
            )
            if ts_col and ts_col in cols and key_col in cols:
                total, rows = _fetch_newest_first(
                    table, cols, where, ts_col, key_col, _TABLE_ROW_LIMIT
                )
            else:
                ts_col = None
                total = table.count_rows(where) if where else table.count_rows()
                query = table.search()
                if where:
                    query = query.where(where)
                rows = query.select(cols).limit(_TABLE_ROW_LIMIT).to_list()
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the info line
            cols, rows, total, err = [], [], 0, f"{type(exc).__name__}: {exc}"
            ts_col = None
        self.call_from_thread(
            self._show_table, name, cols, rows, total, err, where, bool(ts_col)
        )

    def _show_table(
        self,
        name: str,
        cols: list[str],
        rows: list[dict],
        total: int,
        err: str | None,
        where: str | None = None,
        newest_first: bool = False,
    ) -> None:
        info = self.query_one("#table-info", Static)
        grid = self.query_one("#table-view", DataTable)
        grid.clear(columns=True)
        self._table_cols, self._table_rows = list(cols), list(rows)
        self.query_one("#cell-value", Static).update(_DETAIL_PLACEHOLDER)
        if err:
            info.update(f"[red]{name}: {err}[/red]")
            return
        filtered = f" where {where}" if where else ""
        order = " · newest first" if newest_first else ""
        info.update(
            f"[b]{name}[/b]{filtered} — {total} rows × {len(cols)} cols "
            f"(showing {len(rows)}{order})"
        )
        if cols:
            grid.add_columns(*cols)
        for row in rows:
            grid.add_row(*[format_cell(row.get(c)) for c in cols])

    def action_toggle_detail(self) -> None:
        """Grow the cell-detail pane for reading long values (tracebacks)."""
        self.query_one("#cell-detail", VerticalScroll).toggle_class("expanded")

    @on(DataTable.CellHighlighted, "#table-view")
    def _on_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Show the highlighted cell's full value under the grid.

        The grid renders truncated cells (tracebacks are bounded to one
        line), so the pane resolves the raw value by cursor coordinate.
        """
        from rich.text import Text

        row, col = event.coordinate
        if not (0 <= row < len(self._table_rows) and 0 <= col < len(self._table_cols)):
            return
        column = self._table_cols[col]
        value = self._table_rows[row].get(column)
        # Text (not markup) so brackets in tracebacks render literally.
        self.query_one("#cell-value", Static).update(
            Text.assemble((column, "bold"), "\n", _detail_text(value))
        )

    # --- jobs viewer ------------------------------------------------------

    def action_refresh_jobs(self) -> None:
        self._list_jobs(self._build_cfg())

    @work(thread=True, group="viewer", exclusive=True)
    def _list_jobs(self, cfg) -> None:
        try:
            conn = connect(cfg)
            # Every status, not just the active ones the CLI defaults to: in a
            # browser the interesting job is usually the one that already
            # finished (or failed), not the one still running.
            jobs = sort_newest_first(query_jobs(conn, None, ALL_STATUSES))
            tally = status_counts(jobs)
            rows = [
                (
                    str(getattr(jr, "job_id", "") or ""),
                    "{:<9} {:<9} {}".format(
                        job_status(jr),
                        str(getattr(jr, "job_id", "-"))[:8],
                        getattr(jr, "table_name", "-"),
                    ),
                )
                for jr in jobs[:_JOB_LIST_LIMIT]
            ]
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the tree
            rows, tally, err = [], "", f"{type(exc).__name__}: {exc}"
        self.call_from_thread(self._set_jobs, rows, tally, err)

    def _set_jobs(
        self, rows: list[tuple[str, str]], tally: str, err: str | None
    ) -> None:
        node = self._jobs_node
        if node is None:
            return
        node.remove_children()
        node.set_label(f"Jobs — {tally}" if tally else "Jobs")
        node.add_leaf("↻ refresh", data=("jobs-refresh",))
        if err:
            node.add_leaf(f"⚠ {err[:48]}", data=None)
        elif not rows:
            node.add_leaf("(no jobs)", data=None)
        else:
            for job_id, label in rows:
                node.add_leaf(label, data=("job", job_id))
        node.expand()

    def _select_job(self, job_id: str) -> None:
        """Show one job's record, loading it in the background."""
        self._current_job = job_id
        self.query_one("#job-info", Static).update(f"loading job {job_id}…")
        self._load_job(self._build_cfg(), job_id)

    @work(thread=True, group="viewer", exclusive=True)
    def _load_job(self, cfg, job_id: str) -> None:
        try:
            conn = connect(cfg)
            jr = conn.get_job(job_id)
            status = job_status(jr)
            summary = (status, job_target(jr), elapsed(jr), progress_summary(jr))
            # No events_limit: the CLI truncates because a terminal dump is
            # expensive, but this pane scrolls — show the whole log.
            detail = format_detail(jr, events_limit=None)
            terminal = status in TERMINAL_STATUSES
            err = None
        except ValueError:  # geneva raises this for an unknown id
            summary, detail, terminal, err = None, "", True, "not found"
        except Exception as exc:  # noqa: BLE001 - surface to the info line
            summary, detail, terminal = None, "", True
            err = f"{type(exc).__name__}: {exc}"
        self.call_from_thread(self._show_job, job_id, summary, detail, terminal, err)

    def _show_job(
        self,
        job_id: str,
        summary: tuple[str, str, str, str] | None,
        detail: str,
        terminal: bool,
        err: str | None,
    ) -> None:
        """Render one job record.

        Assembled as ``Text``, never markup: job ids, table/column names and
        exception messages are geneva-supplied strings, and a stray bracket in
        one of them would otherwise blow up the markup parser mid-render.
        """
        from rich.text import Text

        info = self.query_one("#job-info", Static)
        value = self.query_one("#job-detail-value", Static)
        self._current_job_terminal = terminal
        if err or summary is None:
            info.update(Text(f"{job_id}: {err}", style="red"))
            value.update("")
            self._stop_follow()
            return
        status, target, elapsed_for, progress = summary
        line = Text.assemble(
            (job_id, "bold"),
            "  ",
            (status, _STATUS_STYLES.get(status, "")),
            f" · {target} · elapsed {elapsed_for}",
        )
        if progress:
            line.append(f"\nprogress: {progress}", style="dim")
        if self._following:
            line.append(
                f"  · following every {_JOB_POLL_SECONDS:g}s", style="dim italic"
            )
        info.update(line)
        value.update(Text(detail))
        # A job that reached a terminal state won't change again — stop polling
        # rather than hammer the connection for a record that is now frozen.
        if terminal:
            self._stop_follow()

    def action_toggle_follow(self) -> None:
        """Re-read the selected job every few seconds while it is still running.

        The TUI equivalent of ``jobs tail``: geneva exposes no streaming log, so
        following means polling the record. Only offered on a live job.
        """
        if self.query_one("#main", ContentSwitcher).current != "job-pane":
            return
        if self._following:
            self._stop_follow()
            self.notify("stopped following")
            return
        if not self._current_job:
            self.notify("select a job first")
            return
        if self._current_job_terminal:
            self.notify("job has already finished — nothing to follow")
            return
        self._following = True
        self._follow_timer = self.set_interval(_JOB_POLL_SECONDS, self._poll_job)
        self._select_job(self._current_job)

    def _stop_follow(self) -> None:
        self._following = False
        if self._follow_timer is not None:
            self._follow_timer.stop()
            self._follow_timer = None

    def _poll_job(self) -> None:
        if self._following and self._current_job:
            self._load_job(self._build_cfg(), self._current_job)

    # --- running ----------------------------------------------------------

    def write_log(self, message: str) -> None:
        """Queue a message for the log pane (safe from any thread)."""
        self._log_queue.put(message)

    def _drain_log(self) -> None:
        """Flush queued log lines to the RichLog (runs on the UI thread)."""
        try:
            log = self.query_one("#log", RichLog)
        except Exception:  # noqa: BLE001 - not mounted yet / tearing down
            return
        while True:
            try:
                log.write(self._log_queue.get_nowait())
            except queue.Empty:
                break

    def action_run(self) -> None:
        self._start_run()

    @on(Button.Pressed, "#run")
    def _on_run(self, _event: Button.Pressed) -> None:
        self._start_run()

    def _start_run(self) -> None:
        # In the Tables/Jobs views the primary action re-queries what is on
        # screen rather than running a step's UDF.
        pane = self.query_one("#main", ContentSwitcher).current
        if pane == "job-pane":
            if self._current_job:
                self._select_job(self._current_job)
            else:
                self._list_jobs(self._build_cfg())
            return
        if pane == "table-pane":
            if self._current_table:
                self.query_one("#table-info", Static).update(
                    f"refreshing {self._current_table}…"
                )
                self._load_table(
                    self._build_cfg(),
                    self._current_table,
                    self._current_table_system,
                    self._job_id_filter(),
                )
            return
        if self._current is None:
            return
        example, step = self._current
        argv = self._build_argv(step)
        self.write_log(f"[b]▶ {example.name}:{step.key}[/b]")
        self._run_step(step, argv)

    def _build_argv(self, step: Step) -> list[str]:
        """Translate the form + global controls into the step CLI's arguments."""
        argv: list[str] = []
        argv += ["--mode", self.query_one("#mode", Select).value]
        config = self.query_one("#config", Input).value.strip()
        if config:
            argv += ["--config", config]
        db_uri = self._db_uri_override()
        if db_uri:
            argv += ["--db-uri", db_uri]
        argv += ["--log-level", self.query_one("#log_level", Select).value]
        for name, (param, widget) in self._fields.items():
            flag = "--" + name.replace("_", "-")
            if param.type is bool:
                argv.append(flag if widget.value else f"--no-{name.replace('_', '-')}")
            else:
                value = str(widget.value).strip()
                if value:  # blank means "use the step default"
                    argv += [flag, value]
        return argv

    @work(thread=True, group="runner", exclusive=True)
    def _run_step(self, step: Step, argv: list[str]) -> None:
        """Run the step's generated CLI in a subprocess, streaming its output."""
        import os
        import subprocess
        import sys
        from pathlib import Path

        exe = Path(sys.executable).with_name(step.key)
        if exe.exists():
            cmd = [str(exe), *argv]
        else:  # fallback: invoke the generated click command directly
            attr = step.key.replace("-", "_")
            cmd = [
                sys.executable,
                "-c",
                f"from geneva_examples.examples.cli import {attr} as c; c()",
                *argv,
            ]
        self.write_log(f"[dim]$ {step.key} {' '.join(argv)}[/dim]")
        try:
            proc = subprocess.Popen(  # noqa: S603 - cmd is our own console script
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except Exception as exc:  # noqa: BLE001
            self.write_log(f"[red]✗ failed to launch {step.key}: {exc}[/red]")
            return
        for line in proc.stdout or []:
            self.write_log(line.rstrip("\n"))
        code = proc.wait()
        if code == 0:
            self.write_log(f"[green]✔ {step.key} finished[/green]")
        else:
            self.write_log(f"[red]✗ {step.key} exited with code {code}[/red]")


def main() -> None:
    """Console-script entry point for ``uv run tui``."""
    GenevaTUI().run()


if __name__ == "__main__":
    main()
