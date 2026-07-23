"""Textual TUI: browse/run example pipelines and view database tables.

The left nav has two sections:

* **Examples** — a tree of examples → steps (from the registry). Selecting a step
  shows its markdown description and a form built from its ``Param`` spec; **Run**
  launches the step's generated CLI in a subprocess and streams its output.
* **Tables** — a read-only viewer. *Refresh* lists the tables in the connected
  database (using the current mode/config controls); selecting one shows a
  sample of its rows in a data grid.

Steps run as a subprocess (not an in-process thread) deliberately: Ray needs a
real stdout file descriptor, which Textual's captured stdout doesn't provide.
Output is streamed via a thread-safe queue drained by a UI timer, so the reader
thread never blocks the event loop. Table reads (a plain Lance scan, no Ray) run
in a worker thread and post a single update back.
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
from geneva_examples.core.spec import Example, Param, Step
from geneva_examples.examples import all_examples
from geneva_examples.tui.forms import field_id, initial_text

_MODES = [
    ("auto (config / geneva_host)", "auto"),
    ("local", "local"),
    ("enterprise", "enterprise"),
]
_LEVELS = [(lvl, lvl) for lvl in ("INFO", "DEBUG", "WARNING", "ERROR")]
_TABLE_ROW_LIMIT = 100

# Geneva system tables worth browsing after a backfill: the job records and
# the per-row error store. They live in the connection's system namespace.
_SYSTEM_TABLES = ("geneva_jobs", "geneva_errors")


def _open_any_table(conn, name: str, *, system: bool = False):
    """Open a regular table, or a geneva system table via its namespace."""
    if not system:
        return conn.open_table(name)
    namespace = list(getattr(conn, "system_namespace", None) or [])
    return conn.open_table(name, namespace=namespace)


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
    .field-label { color: $text-muted; }
    """

    BINDINGS: ClassVar = [
        ("r", "run", "Run / refresh"),
        ("t", "refresh_tables", "List tables"),
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            yield Tree("nav", id="nav")
            with Vertical(id="right"):
                with Horizontal(id="controls"):
                    yield Select(_MODES, value="local", allow_blank=False, id="mode")
                    yield Input(placeholder="config.yaml (optional)", id="config")
                    yield Input(placeholder="db_uri override (optional)", id="db_uri")
                    yield Select(
                        _LEVELS, value="INFO", allow_blank=False, id="log_level"
                    )
                    yield Button("Run ▶", variant="success", id="run")
                with ContentSwitcher(initial="run-pane", id="main"):
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
                                "filter: job_id = …  (Enter to apply; "
                                "blank shows all rows)"
                            ),
                            id="table-filter",
                        )
                        yield DataTable(id="table-view", zebra_stripes=True)
        yield Footer()

    async def on_mount(self) -> None:
        # Drain queued log lines onto the RichLog on the UI thread (10 Hz).
        self.set_interval(0.1, self._drain_log)
        tree = self.query_one("#nav", Tree)
        tree.show_root = False
        tree.root.expand()

        examples = tree.root.add("Examples", expand=True)
        first: tuple[Example, Step] | None = None
        for ex in self._examples:
            node = examples.add(ex.title, data=("example", ex), expand=True)
            for step in ex.steps:
                node.add_leaf(step.key, data=("step", ex, step))
                if first is None:
                    first = (ex, step)

        self._tables_node = tree.root.add("Tables", expand=True)
        self._tables_node.add_leaf("↻ refresh", data=("tables-refresh",))

        if first is not None:
            await self._select(*first)

    # --- selection --------------------------------------------------------

    @on(Tree.NodeSelected)
    async def _on_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        kind = data[0]
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

    def _build_cfg(self):
        """Build a Config from the current global controls (main thread)."""
        from pathlib import Path

        mode = self.query_one("#mode", Select).value
        config = self.query_one("#config", Input).value.strip()
        db_uri = self.query_one("#db_uri", Input).value.strip()
        cfg = load_config(
            Path(config) if config else None,
            mode_override=None if mode == "auto" else mode,
        )
        if db_uri:
            cfg.db_uri = db_uri
        return cfg

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
        # to escape them so the predicate below can't be broken open.
        job_id = (job_id or "").strip().replace("'", "") or None
        where = f"job_id = '{job_id}'" if job_id else None
        try:
            conn = connect(cfg)
            table = _open_any_table(conn, name, system=system)
            cols = list(table.schema.names)
            if system and "job_id" in cols:
                # job_id leads on geneva_jobs/geneva_errors — it's the key
                # you filter and correlate on.
                cols.insert(0, cols.pop(cols.index("job_id")))
            total = table.count_rows(where) if where else table.count_rows()
            query = table.search()
            if where:
                query = query.where(where)
            rows = query.select(cols).limit(_TABLE_ROW_LIMIT).to_list()
            err = None
        except Exception as exc:  # noqa: BLE001 - surface to the info line
            cols, rows, total, err = [], [], 0, f"{type(exc).__name__}: {exc}"
        self.call_from_thread(self._show_table, name, cols, rows, total, err, where)

    def _show_table(
        self,
        name: str,
        cols: list[str],
        rows: list[dict],
        total: int,
        err: str | None,
        where: str | None = None,
    ) -> None:
        info = self.query_one("#table-info", Static)
        grid = self.query_one("#table-view", DataTable)
        grid.clear(columns=True)
        if err:
            info.update(f"[red]{name}: {err}[/red]")
            return
        filtered = f" where {where}" if where else ""
        info.update(
            f"[b]{name}[/b]{filtered} — {total} rows × {len(cols)} cols "
            f"(showing {len(rows)})"
        )
        if cols:
            grid.add_columns(*cols)
        for row in rows:
            grid.add_row(*[format_cell(row.get(c)) for c in cols])

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
        # In the Tables view the primary action re-queries the shown table rather
        # than running a step's UDF.
        if self.query_one("#main", ContentSwitcher).current == "table-pane":
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
        mode = self.query_one("#mode", Select).value
        if mode and mode != "auto":
            argv += ["--mode", mode]
        config = self.query_one("#config", Input).value.strip()
        if config:
            argv += ["--config", config]
        db_uri = self.query_one("#db_uri", Input).value.strip()
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
