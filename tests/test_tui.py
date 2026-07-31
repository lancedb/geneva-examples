"""Pilot test for the Textual TUI.

Drives the app headless via Textual's ``run_test`` harness (using ``asyncio.run``
so no pytest-asyncio plugin is required): mount, confirm the example tree and the
auto-selected step's form, and that re-selecting a step rebuilds the form.
"""

from __future__ import annotations

import asyncio

from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Select,
    Static,
    Tree,
)

from geneva_examples.core.config import Config
from geneva_examples.tui.app import GenevaTUI


def _stub_config(monkeypatch) -> None:
    """Resolve a Config for any mode without a real config.yaml on disk.

    Enterprise mode requires the file, and ``config.yaml`` is gitignored — so a
    test that switches backends would otherwise pass only on a machine that
    happens to have one and fail in CI, where the nav picks up a
    ``⚠ config file not found`` leaf instead of the empty listing it asserts.
    """
    from geneva_examples.tui import app as tui_app

    monkeypatch.setattr(
        tui_app,
        "load_config",
        lambda path=None, *, mode_override=None, db_uri_override=None: Config(
            mode=mode_override or "local",
            db_uri=db_uri_override or "db://test",
        ),
    )


def _quiet_tables(monkeypatch) -> list:
    """No-op the startup/manual tables refresh; returns the call log."""
    calls: list = []
    monkeypatch.setattr(
        GenevaTUI, "_list_tables", lambda self, cfg=None, epoch=0: calls.append(cfg)
    )
    return calls


def _quiet_jobs(monkeypatch) -> list:
    """No-op the jobs listing (it would open a connection); returns the log."""
    calls: list = []
    monkeypatch.setattr(
        GenevaTUI, "_list_jobs", lambda self, cfg=None, epoch=0: calls.append(cfg)
    )
    return calls


def test_tui_mounts_examples_and_tables_sections(monkeypatch):
    refreshes = _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#nav", Tree)
            sections = [n.label.plain for n in tree.root.children]
            assert sections == ["Tables", "Jobs", "Examples"]  # tables lead the nav
            # the app opens on the Tables view and refreshes the listing
            assert app.query_one("#main", ContentSwitcher).current == "table-pane"
            assert str(app.query_one("#run", Button).label) == "Refresh ⟳"
            assert len(refreshes) == 1
            assert app.query_one("#mode", Select).value == "local"  # local default
            assert not app.query_one("#table-filter", Input).display  # hidden
            # Jobs are listed on demand, so only the refresh leaf is there
            jobs_node = tree.root.children[1]
            assert [n.label.plain for n in jobs_node.children] == ["↻ refresh"]
            examples_node = tree.root.children[2]
            # images, video, pdf, audio, debugging
            assert len(examples_node.children) == 5

            # first step auto-selected, description + form populated
            assert app._current is not None
            example, step = app._current
            assert example.name == "images" and step.key == "ingest-images"
            assert app._fields  # form has fields

            # selecting a model step rebuilds the form (regression: no dup ids)
            await app._select(example, example.step("embed"))
            await pilot.pause()
            assert "search_demo" in app._fields
            assert "query_text" in app._fields

    asyncio.run(scenario())


def test_tui_table_viewer_populates_grid(monkeypatch):
    """The table viewer helpers fill the tree + data grid from fetched rows."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()

            # listing tables adds a leaf per name under the Tables section
            app._set_table_names(["images", "pdfs"], ["geneva_errors"], None)
            await pilot.pause()
            tables_node = app.query_one("#nav", Tree).root.children[0]
            labels = [n.label.plain for n in tables_node.children]
            assert "images" in labels and "pdfs" in labels
            assert "geneva_errors (system)" in labels  # error store is browsable

            # showing rows fills the DataTable and switches to the table pane
            app.query_one("#main").current = "table-pane"
            app._show_table(
                "images",
                ["image_id", "embedding"],
                [{"image_id": "a", "embedding": [0.1] * 512}],
                42,
                None,
            )
            await pilot.pause()
            grid = app.query_one("#table-view", DataTable)
            assert len(grid.columns) == 2
            assert len(grid.rows) == 1

            # highlighting a truncated cell reveals its full value below —
            # the grid shows "[512 floats]" but the pane gets the real list
            grid.focus()
            grid.move_cursor(row=0, column=1)
            await pilot.pause()
            detail = str(app.query_one("#cell-value", Static)._content)
            assert detail.startswith("embedding")
            assert "[512 floats]" not in detail
            assert "0.1, 0.1" in detail

            # "d" toggles the detail pane into expanded (trace-reading) size
            pane = app.query_one("#cell-detail")
            assert not pane.has_class("expanded")
            await pilot.press("d")
            assert pane.has_class("expanded")
            await pilot.press("d")
            assert not pane.has_class("expanded")

    asyncio.run(scenario())


def test_tui_run_builds_argv_and_dispatches(monkeypatch):
    """Pressing Run turns the form + global controls into the step CLI's argv."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        captured: dict = {}
        async with app.run_test() as pilot:
            await pilot.pause()
            example = app._current[0]
            await app._select(example, example.step("lightweight"))
            await pilot.pause()

            # startup lands on the Tables view; running a step needs run-pane
            app.query_one("#main", ContentSwitcher).current = "run-pane"
            app.query_one("#param-table-name", Input).value = "mytable"
            app.query_one("#mode", Select).value = "local"
            # Intercept dispatch so no subprocess/Ray runs.
            app._run_step = lambda step, argv: captured.update(step=step, argv=argv)
            app.action_run()
            await pilot.pause()

        argv = captured["argv"]
        assert captured["step"].key == "lightweight"
        assert argv[argv.index("--mode") + 1] == "local"
        assert argv[argv.index("--table-name") + 1] == "mytable"

    asyncio.run(scenario())


def test_tui_run_refreshes_table_in_table_view(monkeypatch):
    """In the Tables view the Run action re-loads the table, not a step's UDF."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        loaded: list[str] = []
        ran: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._load_table = lambda cfg, name, system=False, job_id=None, epoch=0: (
                loaded.append(name)
            )
            app._run_step = lambda step, argv: ran.append(step)
            # simulate viewing a table
            app.query_one("#main").current = "table-pane"
            app._current_table = "pdfs"
            app.action_run()
            await pilot.pause()
        assert loaded == ["pdfs"]  # refreshed the shown table
        assert ran == []  # did not run a step UDF

    asyncio.run(scenario())


def test_tui_system_table_filter_pushes_where(monkeypatch):
    """The job_id filter reaches the query as a where() and shows in the info."""
    from _fakes import FakeConn, FakeTable

    from geneva_examples.tui import app as tui_app

    errtable = FakeTable(names=["error_type", "job_id", "timestamp", "error_id"])
    conn = FakeConn(tables={"geneva_errors": errtable}, is_remote=False)
    monkeypatch.setattr(tui_app, "connect", lambda _cfg: conn)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            # quotes are stripped, not escaped — the predicate can't be broken
            app._load_table(app._build_cfg(), "geneva_errors", True, " j-1'23 ")
            for _ in range(50):
                await pilot.pause(0.1)
                if errtable.wheres:
                    break
            assert errtable.wheres == ["job_id LIKE '%j-123%'"]
            await pilot.pause()
            info = str(app.query_one("#table-info").render())
            assert "where job_id LIKE '%j-123%'" in info
            assert "newest first" in info
            # job_id is promoted to the first column on system tables
            grid = app.query_one("#table-view", DataTable)
            labels = [str(c.label) for c in grid.columns.values()]
            assert labels == ["job_id", "error_type", "timestamp", "error_id"]

    asyncio.run(scenario())


def test_tui_db_uri_field_only_shows_in_enterprise_mode(monkeypatch):
    """db_uri is ignored on the local backend, so the field hides there."""
    _stub_config(monkeypatch)
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            field = app.query_one("#db_uri", Input)
            assert not field.display  # local is the default mode

            app.query_one("#mode", Select).value = "enterprise"
            await pilot.pause()
            assert field.display

            # a value typed in enterprise mode reaches argv...
            field.value = "  db://prod  "
            assert app._db_uri_override() == "db://prod"

            # ...but is not read back once the mode no longer honors it
            app.query_one("#mode", Select).value = "local"
            await pilot.pause()
            assert not field.display
            assert app._db_uri_override() is None

    asyncio.run(scenario())


def test_tui_jobs_section_lists_and_shows_a_record(monkeypatch):
    """The Jobs section fills from job records and renders one on selection."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()

            app._set_jobs(
                [("abc123def456", "DONE      abc123de  video_clips")],
                "1 DONE",
                None,
            )
            await pilot.pause()
            jobs_node = app.query_one("#nav", Tree).root.children[1]
            assert jobs_node.label.plain == "Jobs — 1 DONE"  # tally in the header
            labels = [n.label.plain for n in jobs_node.children]
            assert labels == ["↻ refresh", "DONE      abc123de  video_clips"]

            app.query_one("#main").current = "job-pane"
            app._show_job(
                "abc123def456",
                ("DONE", "video_clips.embedding", "0:01:02", "rows 9/10 (90%)"),
                "job_id:     abc123def456\nevents (1 total):\n    [worker] done",
                True,
                None,
            )
            await pilot.pause()
            info = str(app.query_one("#job-info", Static)._content)
            assert "abc123def456" in info and "video_clips.embedding" in info
            assert "0:01:02" in info
            assert "rows 9/10 (90%)" in info  # progress surfaced up top
            # brackets in the event log survive verbatim (not parsed as markup)
            detail = str(app.query_one("#job-detail-value", Static)._content)
            assert "[worker] done" in detail

    asyncio.run(scenario())


def test_tui_job_errors_render_without_markup_crash(monkeypatch):
    """A geneva message containing brackets reaches the info line as text."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#main").current = "job-pane"
            app._show_job("j-1", None, "", True, "RuntimeError: bad [tag] here")
            await pilot.pause()
            info = str(app.query_one("#job-info", Static)._content)
            assert "bad [tag] here" in info

    asyncio.run(scenario())


def test_tui_follow_stops_when_job_reaches_terminal_state(monkeypatch):
    """Following polls a live job and gives up once the record is frozen."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._load_job = lambda cfg, job_id, epoch=0: None  # no connection here
            app.query_one("#main", ContentSwitcher).current = "job-pane"
            app._current_job = "j-1"
            app._current_job_terminal = False

            app.action_toggle_follow()
            assert app._following

            # the next poll comes back DONE -> following switches itself off
            app._show_job(
                "j-1", ("DONE", "images.embedding", "0:00:05", ""), "", True, None
            )
            await pilot.pause()
            assert not app._following
            assert app._follow_timer is None

            # and a finished job can't be followed again
            app.action_toggle_follow()
            assert not app._following

    asyncio.run(scenario())


def test_tui_run_refreshes_job_in_job_view(monkeypatch):
    """In the Jobs view the Run action re-reads the job, not a step's UDF."""
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        loaded: list[str] = []
        ran: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._load_job = lambda cfg, job_id, epoch=0: loaded.append(job_id)
            app._run_step = lambda step, argv: ran.append(step)
            app.query_one("#main").current = "job-pane"
            app._current_job = "j-7"
            app.action_run()
            await pilot.pause()
        assert loaded == ["j-7"]
        assert ran == []

    asyncio.run(scenario())


def test_tui_switching_mode_drops_the_other_backends_listings(monkeypatch):
    """A job id from local mode must not be re-read against enterprise."""
    _stub_config(monkeypatch)
    table_refreshes = _quiet_tables(monkeypatch)
    job_refreshes = _quiet_jobs(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            # browsing local: a job listed, selected, and being followed
            app._load_job = lambda cfg, job_id, epoch=0: None
            app._set_jobs(
                [("j-local", "RUNNING   j-local   images")], "1 RUNNING", None
            )
            app._set_table_names(["images"], ["geneva_jobs"], None)
            app.query_one("#main", ContentSwitcher).current = "job-pane"
            app._current_job = "j-local"
            app._current_job_terminal = False
            app._current_table = "images"
            app.action_toggle_follow()
            await pilot.pause()
            assert app._following

            app.query_one("#mode", Select).value = "enterprise"
            await pilot.pause()

            # nothing from the local database survives the switch
            assert app._current_job is None
            assert app._current_table is None
            assert not app._following  # and no poll against the new backend
            tree = app.query_one("#nav", Tree)
            tables_node, jobs_node = tree.root.children[0], tree.root.children[1]
            assert [n.label.plain for n in tables_node.children] == ["↻ refresh"]
            assert [n.label.plain for n in jobs_node.children] == ["↻ refresh"]
            assert jobs_node.label.plain == "Jobs"  # stale tally dropped too
            assert "enterprise" in app.sub_title  # header names the new backend
            # switching while reading jobs re-lists jobs for the new backend,
            # so the tables listing is still just the one from startup
            assert len(job_refreshes) == 1
            assert len(table_refreshes) == 1

    asyncio.run(scenario())


def test_tui_late_read_from_the_previous_backend_is_dropped(monkeypatch):
    """A result in flight when the target changes never reaches the pane."""
    _stub_config(monkeypatch)
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            epoch = app._epoch
            app.query_one("#mode", Select).value = "enterprise"
            await pilot.pause()

            # the local read finishes now, stamped with the epoch it started in
            # (posted from a thread, the way a worker delivers its result)
            rows = [("j-1", "DONE      j-1       images")]
            await asyncio.to_thread(
                app._post, epoch, app._set_jobs, rows, "1 DONE", None
            )
            await pilot.pause()
            jobs_node = app.query_one("#nav", Tree).root.children[1]
            assert [n.label.plain for n in jobs_node.children] == ["↻ refresh"]

            # a read started after the switch still lands
            rows = [("j-2", "DONE      j-2       pdfs")]
            await asyncio.to_thread(
                app._post, app._epoch, app._set_jobs, rows, "", None
            )
            await pilot.pause()
            assert len(jobs_node.children) == 2

    asyncio.run(scenario())


def test_tui_unusable_target_reports_instead_of_crashing(monkeypatch):
    """Enterprise mode without a config is a message, not a crash dialog."""
    _quiet_tables(monkeypatch)

    from geneva_examples.tui import app as tui_app

    def boom(*_args, **_kwargs):
        raise RuntimeError("config file not found: config.yaml")

    monkeypatch.setattr(tui_app, "load_config", boom)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh_jobs()
            await pilot.pause()
            jobs_node = app.query_one("#nav", Tree).root.children[1]
            labels = [n.label.plain for n in jobs_node.children]
            assert any("config file not found" in label for label in labels)

            # and the same error reaches the pane a job would have loaded into
            app.query_one("#main", ContentSwitcher).current = "job-pane"
            app._select_job("j-1")
            await pilot.pause()
            info = str(app.query_one("#job-info", Static)._content)
            assert "config file not found" in info

    asyncio.run(scenario())


def test_tui_job_id_filter_only_reads_on_system_tables(monkeypatch):
    _quiet_tables(monkeypatch)

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#table-filter", Input).value = "  j-9  "
            app._current_table_system = False
            assert app._job_id_filter() is None  # plain tables: no filter
            app._current_table_system = True
            assert app._job_id_filter() == "j-9"

    asyncio.run(scenario())
