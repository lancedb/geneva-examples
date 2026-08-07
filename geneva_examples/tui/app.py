"""Textual TUI: browse/run example pipelines, view database tables, inspect jobs.

The left nav has four sections:

* **Tables** — a read-only viewer, and the view the app opens on (with a fresh
  listing). *Refresh* lists the tables in the connected database (using the
  current mode/config controls); selecting one shows a sample of its rows in a
  data grid.
* **Jobs** — the same job records the ``jobs`` CLI reads (geneva has no
  streaming log API, so the record's append-only event list *is* the log).
  *Refresh* lists recent jobs newest-first across every status; selecting one
  shows its full record, and ``f`` follows a running job by re-reading it.
* **Tools** — small apps that *change* the database rather than read it. Today
  that is **Delete Table**, the TUI form of ``uv run delete-table``: pick a
  table (or type its name), then confirm the drop in a modal. Geneva's system
  tables are neither listed there nor droppable through it.
* **Examples** — a tree of examples → steps (from the registry). Selecting a step
  shows its markdown description and a form built from its ``Param`` spec; **Run**
  launches the step's generated CLI in a subprocess and streams its output.

Steps run as a subprocess (not an in-process thread) deliberately: Ray needs a
real stdout file descriptor, which Textual's captured stdout doesn't provide.
Output is streamed via a thread-safe queue drained by a UI timer, so the reader
thread never blocks the event loop. Table and job reads (a plain Lance scan or a
history lookup, no Ray) run in a worker thread and post a single update back.

Table names and job ids are scoped to one database, so the mode/config/db_uri
controls define which database everything on screen came from. Changing any of
them retargets the app: the listings are dropped, in-flight reads from the old
backend are discarded on arrival (see ``_retarget`` and ``_post``), and the
header names the backend now selected.
"""

from __future__ import annotations

import queue
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
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
_TABLE_PLACEHOLDER = "select a table on the left (press [b]t[/b] to list them)"
_DELETE_PLACEHOLDER = "press [b]Refresh ⟳[/b] to list the tables on this backend"


def _target_label(cfg) -> str:
    """Which database ``cfg`` points at — for messages that name the backend.

    Mode-aware on purpose: ``db_uri`` is meaningless on the local backend (it
    connects to ``local_db_path``), so reporting it there would name a database
    the read never touched.
    """
    return str(cfg.local_db_path if cfg.is_local else cfg.db_uri)


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


def _read_error(name: str, exc: Exception) -> str:
    """The message for a failed table read, naming the cause we can recognize.

    Dropping a table and recreating it at the same path leaves this process
    resolving the *previous* manifest: the scan then asks for fragment files
    that were deleted with the old table, and reports a path the reader can do
    nothing with. It is not transient and it is not repairable from here —
    ``checkout_latest()``, a fresh ``lancedb.Session``, ``index_cache_size``
    and ``read_consistency_interval=0`` were all tried, and only a new process
    reads the table again (plain ``lance.dataset()`` is unaffected, so the
    stale state is above Lance, in the geneva/lancedb connection layer).

    ``table_names()`` and ``drop_table()`` stay correct throughout, so the
    Tables *listing* and the Delete Table tool keep working — it is only the
    row scan that breaks. Say so, rather than printing the missing fragment.
    """
    text = str(exc)
    if "Not found:" in text and f"{name}.lance/data/" in text:
        return (
            f"{name} was recreated after this app connected, so the read is "
            "still resolving the old table's files. Restart `uv run tui` to see "
            "it — refreshing here cannot clear that cache. (Listing and delete "
            "are unaffected.)"
        )
    return f"{type(exc).__name__}: {exc}"


def _deletable(names: list[str]) -> list[str]:
    """The table names the delete tool may offer, system tables removed.

    geneva's system tables live in their own namespace, so ``table_names()``
    does not return them today — filtered anyway, so the tool never offers one
    if that changes. ``_request_delete`` refuses them by name as well.
    """
    return [n for n in names if n not in _SYSTEM_TABLES]


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


class ConfirmDelete(ModalScreen[bool]):
    """The "are you sure?" gate in front of a table drop.

    A modal rather than an inline prompt: dropping a table is the one action in
    this app that destroys data and cannot be undone, so it should interrupt
    rather than sit as another control the eye can skip past. Dismisses ``True``
    only if the Delete button is pressed; Cancel and Escape dismiss ``False``.
    """

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        from rich.text import Text

        with Vertical(id="confirm-box"):
            # Text, not markup: a table name is a geneva-supplied string, and a
            # stray bracket in one would otherwise be parsed as a markup tag.
            yield Static(
                Text(f"Are you sure you want to delete {self._name}?"),
                id="confirm-question",
            )
            yield Static(
                "This permanently drops the table and every row in it.",
                classes="field-label",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="confirm-no")
                yield Button("Delete", variant="error", id="confirm-yes")

    @on(Button.Pressed, "#confirm-yes")
    def _on_yes(self, _event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _on_no(self, _event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


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
    #delete-info { height: auto; padding: 0 0 1 0; color: $text-muted; }
    #delete-list { height: 1fr; border: solid $panel; }
    #delete-actions { height: auto; padding: 1 0; }
    #delete-actions Input { width: 1fr; margin-right: 1; }
    #delete-status { height: auto; min-height: 1; }
    ConfirmDelete { align: center middle; }
    #confirm-box { width: 64; height: auto; padding: 1 2;
                   border: thick $error; background: $surface; }
    #confirm-question { text-style: bold; }
    #confirm-buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #confirm-buttons Button { margin-left: 1; }
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
        self._tools_node = None
        # The delete tool's own listing — what it will accept as a target, so a
        # typed name is checked against the same set the list offers.
        self._delete_names: list[str] = []
        self._jobs_node = None
        self._current_job: str | None = None
        self._current_job_terminal = True
        self._following = False
        self._follow_timer = None
        # Which database the panes were read from, and a counter bumped every
        # time that changes. Matches the control defaults in compose() so the
        # initial Select.Changed doesn't read as a retarget.
        self._target: tuple[str, str, str] = ("local", "", "")
        self._epoch = 0

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
                        yield Static(_TABLE_PLACEHOLDER, id="table-info")
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
                    with Vertical(id="tool-pane"):
                        yield Static(_DELETE_PLACEHOLDER, id="delete-info")
                        yield OptionList(id="delete-list")
                        with Horizontal(id="delete-actions"):
                            yield Input(
                                placeholder=(
                                    "table to delete — pick one above, "
                                    "or type its name and press Enter"
                                ),
                                id="delete-name",
                            )
                            yield Button("Delete", variant="error", id="delete-go")
                        yield Static("", id="delete-status")
        yield Footer()

    async def on_mount(self) -> None:
        # Drain queued log lines onto the RichLog on the UI thread (10 Hz).
        self.set_interval(0.1, self._drain_log)
        self._sync_db_uri_visibility()
        self._target = self._target_key()
        self._show_target()
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

        # Tools sits above Examples, not below: the examples tree is long
        # enough to push anything after it off a normal terminal. Its leaves
        # are fixed apps, not a listing, so there is no refresh leaf here —
        # each tool lists whatever it needs when you open it.
        self._tools_node = tree.root.add("Tools", expand=True)
        self._tools_node.add_leaf("Delete Table", data=("tool", "delete-table"))

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
        self._refresh_tables()

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
            self._refresh_jobs()
        elif kind == "job":
            switcher.current = "job-pane"
            run_button.label = "Refresh ⟳"
            self._select_job(data[1])
        elif kind == "tool":
            switcher.current = "tool-pane"
            run_button.label = "Refresh ⟳"
            self._refresh_delete_tables()
        elif kind == "tables-refresh":
            self._refresh_tables()
        elif kind == "table":
            switcher.current = "table-pane"
            run_button.label = "Refresh ⟳"
            self._open_table(data[1], len(data) > 2 and bool(data[2]), "loading")

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

    # --- connection target ------------------------------------------------

    @on(Select.Changed, "#mode")
    def _on_mode_changed(self, _event: Select.Changed) -> None:
        self._sync_db_uri_visibility()
        if self._retarget():
            # Picking a mode is a finished choice, so the section being browsed
            # is re-listed against the new backend straight away.
            self._refresh_browsed_section()

    @on(Input.Changed, "#config")
    @on(Input.Changed, "#db_uri")
    def _on_target_input_changed(self, _event: Input.Changed) -> None:
        # Half-typed text is not a finished choice: forget the old backend's
        # listings, but don't open a connection on every keystroke. Enter does
        # that, below.
        self._retarget()

    @on(Input.Submitted, "#config")
    @on(Input.Submitted, "#db_uri")
    def _on_target_input_submitted(self, _event: Input.Submitted) -> None:
        self._retarget()
        self._refresh_browsed_section()

    def _refresh_browsed_section(self) -> None:
        """List whichever section is on screen for the newly selected backend.

        Switching backends while reading job records means you want *that*
        backend's jobs; anywhere else, tables — the view the app opens on.
        """
        pane = self.query_one("#main", ContentSwitcher).current
        if pane == "job-pane":
            self._refresh_jobs()
        elif pane == "tool-pane":
            self._refresh_delete_tables()
        else:
            self._refresh_tables()

    def _target_key(self) -> tuple[str, str, str]:
        """Identity of the database the controls currently point at."""
        return (
            str(self.query_one("#mode", Select).value),
            self.query_one("#config", Input).value.strip(),
            self._db_uri_override() or "",
        )

    def _retarget(self) -> bool:
        """Notice a change of backend; return whether one happened.

        Table names and job ids belong to a single database, so once the target
        moves everything on screen describes somewhere else — re-reading a job
        id listed under local mode against the enterprise cluster just fails
        with "not found". Rather than let that stale state be acted on, the
        listings are dropped and the reads still in flight are marked stale.
        """
        key = self._target_key()
        if key == self._target:
            return False
        self._target = key
        self._epoch += 1
        self._forget_browsed()
        self._show_target()
        return True

    def _show_target(self) -> None:
        """Name the selected backend in the header, so it is never a guess."""
        mode, config, db_uri = self._target
        self.sub_title = " · ".join(p for p in (mode, db_uri, config) if p)

    def _forget_browsed(self) -> None:
        """Reset the nav sections and viewer panes to "nothing read yet"."""
        self._stop_follow()
        self._current_job = None
        self._current_job_terminal = True
        self._current_table = None
        self._current_table_system = False
        self._table_cols, self._table_rows = [], []
        self._reset_section(self._tables_node, "tables-refresh")
        self._reset_section(self._jobs_node, "jobs-refresh", label="Jobs")
        self.query_one("#table-filter", Input).display = False
        self.query_one("#table-info", Static).update(_TABLE_PLACEHOLDER)
        self.query_one("#table-view", DataTable).clear(columns=True)
        self.query_one("#cell-value", Static).update(_DETAIL_PLACEHOLDER)
        self.query_one("#job-info", Static).update(_JOB_PLACEHOLDER)
        self.query_one("#job-detail-value", Static).update("")
        self._clear_delete_tool()

    def _clear_delete_tool(self) -> None:
        """Empty the delete tool — its listing named the previous database."""
        self._delete_names = []
        self._pane("#delete-list", OptionList).clear_options()
        self._pane("#delete-name", Input).value = ""
        self._pane("#delete-info", Static).update(_DELETE_PLACEHOLDER)
        self._pane("#delete-status", Static).update("")

    def _post(self, epoch: int, callback, *args) -> None:
        """Hand a worker's result to the UI unless the target moved under it.

        A read holds the config it was started with, so a result arriving after
        a retarget describes the previous database. Dropping it here keeps a
        slow local scan from repainting the pane after a switch to enterprise.
        """
        if epoch == self._epoch:
            self.call_from_thread(callback, *args)

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

    def _cfg(self):
        """``(config, None)`` from the controls, or ``(None, message)``.

        ``load_config`` rejects a target it can't use — enterprise mode with no
        config.yaml, or one missing credentials — and selecting a mode this
        machine isn't set up for is an ordinary thing to do in a mode switcher.
        The complaint belongs on the pane the user is looking at, not in a
        crash dialog, so callers render it where the result would have gone.
        """
        try:
            return self._build_cfg(), None
        except Exception as exc:  # noqa: BLE001 - surface to the caller's pane
            return None, f"{type(exc).__name__}: {exc}"

    # --- table viewer -----------------------------------------------------

    def action_refresh_tables(self) -> None:
        self._refresh_tables()

    def _refresh_tables(self) -> None:
        """List the current backend's tables into the nav."""
        cfg, err = self._cfg()
        if err:
            self._set_table_names([], [], err)
        else:
            self._list_tables(cfg, self._epoch)

    def _open_table(self, name: str, system: bool, note: str) -> None:
        """Show one table, loading its rows in the background."""
        self._current_table = name
        self._current_table_system = system
        # System tables (geneva_jobs / geneva_errors) carry a job_id column, so
        # they get the job_id filter box; plain tables don't.
        self.query_one("#table-filter", Input).display = system
        cfg, err = self._cfg()
        if err:
            self._show_table(name, [], [], 0, err)
            return
        self.query_one("#table-info", Static).update(f"{note} {name}…")
        self._load_table(cfg, name, system, self._job_id_filter(), self._epoch)

    def _job_id_filter(self) -> str | None:
        """The job_id filter value — only meaningful on system tables."""
        if not self._current_table_system:
            return None
        return self.query_one("#table-filter", Input).value.strip() or None

    @on(Input.Submitted, "#table-filter")
    def _on_filter_submitted(self, _event: Input.Submitted) -> None:
        if self._current_table and self._current_table_system:
            self._open_table(self._current_table, True, "filtering")

    @work(thread=True, group="viewer", exclusive=True)
    def _list_tables(self, cfg, epoch: int = 0) -> None:
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
        self._post(epoch, self._set_table_names, names, system, err)

    def _reset_section(self, node, kind: str, label: str | None = None) -> None:
        """Empty a nav section back to just its refresh leaf."""
        if node is None:
            return
        node.remove_children()
        if label is not None:
            node.set_label(label)
        node.add_leaf("↻ refresh", data=(kind,))
        node.expand()

    def _set_table_names(
        self, names: list[str], system: list[str], err: str | None
    ) -> None:
        node = self._tables_node
        if node is None:
            return
        self._reset_section(node, "tables-refresh")
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
        epoch: int = 0,
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
            cols, rows, total, err = [], [], 0, _read_error(name, exc)
            ts_col = None
        self._post(
            epoch, self._show_table, name, cols, rows, total, err, where, bool(ts_col)
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
        self._refresh_jobs()

    def _refresh_jobs(self) -> None:
        """List the current backend's jobs into the nav."""
        cfg, err = self._cfg()
        if err:
            self._set_jobs([], "", err)
        else:
            self._list_jobs(cfg, self._epoch)

    @work(thread=True, group="viewer", exclusive=True)
    def _list_jobs(self, cfg, epoch: int = 0) -> None:
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
        self._post(epoch, self._set_jobs, rows, tally, err)

    def _set_jobs(
        self, rows: list[tuple[str, str]], tally: str, err: str | None
    ) -> None:
        node = self._jobs_node
        if node is None:
            return
        self._reset_section(
            node, "jobs-refresh", f"Jobs — {tally}" if tally else "Jobs"
        )
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
        cfg, err = self._cfg()
        if err:
            self._show_job(job_id, None, "", True, err)
            return
        self.query_one("#job-info", Static).update(f"loading job {job_id}…")
        self._load_job(cfg, job_id, self._epoch)

    @work(thread=True, group="viewer", exclusive=True)
    def _load_job(self, cfg, job_id: str, epoch: int = 0) -> None:
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
            # Name the database: the usual way to get here is an id belonging
            # to the other mode's database, not a job that never existed.
            summary, detail, terminal = None, "", True
            err = f"no such job in the {cfg.mode} database {_target_label(cfg)}"
        except Exception as exc:  # noqa: BLE001 - surface to the info line
            summary, detail, terminal = None, "", True
            err = f"{type(exc).__name__}: {exc}"
        self._post(epoch, self._show_job, job_id, summary, detail, terminal, err)

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
        if not (self._following and self._current_job):
            return
        cfg, err = self._cfg()
        if err:
            # _show_job stops the follow, so a broken target can't keep polling.
            self._show_job(self._current_job, None, "", True, err)
            return
        self._load_job(cfg, self._current_job, self._epoch)

    # --- tools: delete table ----------------------------------------------

    def _pane(self, selector: str, kind):
        """Query the app's own screen, not whatever is on top of it.

        ``App.query_one`` resolves against the topmost screen, so every lookup
        into the tool pane would fail while ``ConfirmDelete`` is up — and a
        listing that lands from a worker mid-confirmation does exactly that.
        """
        return self.screen_stack[0].query_one(selector, kind)

    def _refresh_delete_tables(self) -> None:
        """List what the delete tool is allowed to drop on this backend."""
        cfg, err = self._cfg()
        if err:
            self._set_delete_tables([], err)
        else:
            self._list_delete_tables(cfg, self._epoch)

    @work(thread=True, group="tools")
    def _list_delete_tables(self, cfg, epoch: int = 0) -> None:
        try:
            names = _deletable(sorted(connect(cfg).table_names()))
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the pane
            names, err = [], f"{type(exc).__name__}: {exc}"
        self._post(epoch, self._set_delete_tables, names, err)

    def _set_delete_tables(self, names: list[str], err: str | None) -> None:
        from rich.text import Text

        self._delete_names = list(names)
        info = self._pane("#delete-info", Static)
        options = self._pane("#delete-list", OptionList)
        options.clear_options()
        if err:
            info.update(Text(err, style="red"))
        elif not names:
            info.update("no deletable tables on this backend")
        else:
            info.update(
                f"{len(names)} table(s) on {self._target[0]} — "
                "pick one, or type its name below"
            )
            # Text prompts, so a bracket in a table name is not read as markup.
            options.add_options([Text(n) for n in names])

    @on(OptionList.OptionSelected, "#delete-list")
    def _on_delete_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a listed table fills the name box rather than deleting it.

        Selection and deletion stay separate actions: a stray Enter in a list
        should never be one keypress away from dropping a table.
        """
        if 0 <= event.option_index < len(self._delete_names):
            self._pane("#delete-name", Input).value = self._delete_names[
                event.option_index
            ]
            self._pane("#delete-status", Static).update("")

    @on(Input.Submitted, "#delete-name")
    def _on_delete_name_submitted(self, _event: Input.Submitted) -> None:
        self._request_delete()

    @on(Button.Pressed, "#delete-go")
    def _on_delete_pressed(self, _event: Button.Pressed) -> None:
        self._request_delete()

    def _request_delete(self) -> None:
        """Check the named table against the backend, then raise the modal."""
        from rich.text import Text

        name = self._pane("#delete-name", Input).value.strip()
        status = self._pane("#delete-status", Static)
        if not name:
            status.update(Text("pick a table above, or type its name", "yellow"))
            return
        if name in _SYSTEM_TABLES:
            # The job records and the per-row error store: geneva's own
            # bookkeeping, browsable from the Tables section but not ours to
            # drop. Dropping one loses the history every job view reads. A
            # name-based rule, so it needs no read to enforce.
            status.update(
                Text(f"{name} is a geneva system table — it can't be deleted", "red")
            )
            return
        cfg, err = self._cfg()
        if err:
            status.update(Text(err, "red"))
            return
        status.update(f"checking {name}…")
        self._verify_target(cfg, name, self._epoch)

    @work(thread=True, group="tools")
    def _verify_target(self, cfg, name: str, epoch: int = 0) -> None:
        """Re-read the table list so the verdict describes the database *now*.

        The pane's listing is a snapshot from the last refresh. Deciding
        against it means a table recreated since — by ``ingest-images`` in a
        terminal, or by this app's own Examples runner, which shells out to the
        step CLI — gets refused as "no such table" while the Tables section
        sitting right next to it plainly lists the thing. So the check that
        gates a drop reads the backend rather than trusting the snapshot, and
        the pane is repainted from the same read.
        """
        try:
            names = _deletable(sorted(connect(cfg).table_names()))
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the pane
            names, err = [], f"{type(exc).__name__}: {exc}"
        self._post(epoch, self._confirm_target, name, names, err)

    def _confirm_target(self, name: str, names: list[str], err: str | None) -> None:
        from rich.text import Text

        status = self._pane("#delete-status", Static)
        if err:
            status.update(Text(err, "red"))
            return
        # Repaint the list from the read that just decided this, so what the
        # pane offers and what it will accept can never disagree.
        self._set_delete_tables(names, None)
        self._pane("#delete-name", Input).value = name
        if name not in names:
            status.update(Text(f"no table named {name!r} on this backend", "red"))
            return
        status.update("")
        self.push_screen(ConfirmDelete(name), lambda ok: self._on_confirmed(name, ok))

    def _on_confirmed(self, name: str, confirmed: bool | None) -> None:
        """Run the drop, once the modal comes back with a yes."""
        if not confirmed:
            return
        cfg, err = self._cfg()
        if err:
            self._table_deleted(name, err)
            return
        self._pane("#delete-status", Static).update(f"deleting {name}…")
        self._delete_table(cfg, name, self._epoch)

    @work(thread=True, group="tools")
    def _delete_table(self, cfg, name: str, epoch: int = 0) -> None:
        try:
            connect(cfg).drop_table(name)
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the pane
            err = f"{type(exc).__name__}: {exc}"
        self._post(epoch, self._table_deleted, name, err)

    def _table_deleted(self, name: str, err: str | None) -> None:
        from rich.text import Text

        status = self._pane("#delete-status", Static)
        if err:
            status.update(Text(f"{name}: {err}", "red"))
            return
        status.update(Text(f"dropped {name}", "green"))
        self._pane("#delete-name", Input).value = ""
        self.notify(f"dropped {name}")
        # The table is gone from every listing that named it — including the
        # viewer, if that is what it happened to be showing.
        if self._current_table == name:
            self._current_table = None
            self._current_table_system = False
            self._table_cols, self._table_rows = [], []
            self.query_one("#table-info", Static).update(_TABLE_PLACEHOLDER)
            self.query_one("#table-view", DataTable).clear(columns=True)
            self.query_one("#cell-value", Static).update(_DETAIL_PLACEHOLDER)
        self._refresh_delete_tables()
        self._refresh_tables()

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
        # With nothing selected — the state a retarget leaves behind — it
        # re-lists the section instead, so one press always does something.
        if pane == "job-pane":
            if self._current_job:
                self._select_job(self._current_job)
            else:
                self._refresh_jobs()
            return
        if pane == "table-pane":
            if self._current_table:
                self._open_table(
                    self._current_table, self._current_table_system, "refreshing"
                )
            else:
                self._refresh_tables()
            return
        if pane == "tool-pane":
            self._refresh_delete_tables()
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
