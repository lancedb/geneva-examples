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
    OptionList,
    Select,
    Static,
    Tree,
)

from geneva_examples.core.config import Config
from geneva_examples.tui.app import ConfirmDelete, GenevaTUI


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
            # tables lead the nav; Tools sits above the long Examples tree
            assert sections == ["Tables", "Jobs", "Tools", "Examples"]
            # the app opens on the Tables view and refreshes the listing
            assert app.query_one("#main", ContentSwitcher).current == "table-pane"
            assert str(app.query_one("#run", Button).label) == "Refresh ⟳"
            assert len(refreshes) == 1
            assert app.query_one("#mode", Select).value == "local"  # local default
            assert not app.query_one("#table-filter", Input).display  # hidden
            # Jobs are listed on demand, so only the refresh leaf is there
            jobs_node = tree.root.children[1]
            assert [n.label.plain for n in jobs_node.children] == ["↻ refresh"]
            # Tools are fixed apps, not a listing — no refresh leaf
            tools_node = tree.root.children[2]
            assert [n.label.plain for n in tools_node.children] == ["Delete Table"]
            examples_node = tree.root.children[3]
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


def test_tui_recreated_table_read_explains_itself():
    """A drop+recreate poisons this process's reads; say that, not the path.

    ``table_names()`` and ``drop_table()`` keep working across a recreate —
    only the row scan resolves the old table's fragments — so the message has
    to point at the one thing that helps (a restart) and not imply the
    listing or the delete tool are broken too.
    """
    from geneva_examples.tui.app import _read_error

    stale = RuntimeError(
        "External error: Not found: /db/images.lance/data/0111d7a7af45.lance, "
        "/rust/lance-io/src/local.rs:133:40"
    )
    msg = _read_error("images", stale)
    assert "recreated after this app connected" in msg
    assert "Restart `uv run tui`" in msg
    assert ".lance" not in msg  # the unactionable path is not what we show

    # anything else keeps its own message, typed
    other = ValueError("something else entirely")
    assert _read_error("images", other) == "ValueError: something else entirely"
    # and a missing file that isn't this table's own fragment is left alone
    elsewhere = RuntimeError("Not found: /db/other.lance/data/abc.lance")
    assert _read_error("images", elsewhere).startswith("RuntimeError:")


def _delete_tool(monkeypatch, tables: list[str]):
    """A fake backend for the Tools → Delete Table app; returns the connection."""
    from _fakes import FakeConn, FakeTable

    from geneva_examples.tui import app as tui_app

    _stub_config(monkeypatch)
    _quiet_tables(monkeypatch)
    conn = FakeConn(tables={name: FakeTable() for name in tables}, is_remote=False)
    monkeypatch.setattr(tui_app, "connect", lambda _cfg: conn)
    return conn


async def _open_delete_tool(app, pilot):
    """Select Tools → Delete Table and wait for its listing to land."""
    tools_node = app.query_one("#nav", Tree).root.children[2]
    app.query_one("#nav", Tree).select_node(tools_node.children[0])
    for _ in range(50):
        await pilot.pause(0.1)
        if app._delete_names:
            break


def _status(app) -> str:
    """The tool's status line, read off the app's own screen.

    ``app.query_one`` resolves against the topmost screen, so this has to go
    through ``screen_stack[0]`` to stay readable while the modal is up.
    """
    return str(app.screen_stack[0].query_one("#delete-status", Static)._content)


async def _press_delete(app, pilot):
    """Press Delete and wait out the live re-check that gates the modal."""
    app.screen_stack[0].query_one("#delete-go", Button).press()
    for _ in range(50):
        await pilot.pause(0.1)
        if isinstance(app.screen, ConfirmDelete) or "checking" not in _status(app):
            return


def test_tui_delete_tool_lists_tables_and_hides_system_tables(monkeypatch):
    """The tool offers real tables only — geneva's own bookkeeping is not listed."""
    _delete_tool(monkeypatch, ["videos", "geneva_jobs", "images"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            assert app.query_one("#main", ContentSwitcher).current == "tool-pane"
            assert app._delete_names == ["images", "videos"]  # geneva_jobs filtered
            options = app.query_one("#delete-list", OptionList)
            assert options.option_count == 2

            # clicking a listed table fills the name box, it does not delete
            options.focus()
            app.post_message(OptionList.OptionSelected(options, 1))
            await pilot.pause()
            assert app.query_one("#delete-name", Input).value == "videos"

    asyncio.run(scenario())


def test_tui_delete_tool_confirms_before_dropping(monkeypatch):
    """Delete puts the modal up first, and only a Yes reaches drop_table."""
    conn = _delete_tool(monkeypatch, ["videos", "images"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            app.query_one("#delete-name", Input).value = "videos"
            await _press_delete(app, pilot)

            # the modal names the table, and nothing is dropped while it is up
            assert isinstance(app.screen, ConfirmDelete)
            question = str(app.screen.query_one("#confirm-question", Static)._content)
            assert question == "Are you sure you want to delete videos?"
            assert conn.dropped == []

            app.screen.query_one("#confirm-yes", Button).press()
            for _ in range(50):
                await pilot.pause(0.1)
                if conn.dropped:
                    break
            assert conn.dropped == ["videos"]
            status = _status(app)
            assert "dropped videos" in status

    asyncio.run(scenario())


def test_tui_delete_tool_cancel_drops_nothing(monkeypatch):
    conn = _delete_tool(monkeypatch, ["videos"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            app.query_one("#delete-name", Input).value = "videos"
            await _press_delete(app, pilot)
            app.screen.query_one("#confirm-no", Button).press()
            await pilot.pause()

            assert not isinstance(app.screen, ConfirmDelete)
            assert conn.dropped == []

    asyncio.run(scenario())


def test_tui_delete_tool_refuses_a_system_table(monkeypatch):
    """Typing a system table's name is refused before the modal is even raised."""
    conn = _delete_tool(monkeypatch, ["videos"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            app.query_one("#delete-name", Input).value = "geneva_errors"
            app.query_one("#delete-go", Button).press()
            await pilot.pause()

            assert not isinstance(app.screen, ConfirmDelete)
            status = _status(app)
            assert "geneva_errors is a geneva system table" in status
            assert conn.dropped == []

    asyncio.run(scenario())


def test_tui_delete_tool_refuses_an_unknown_table(monkeypatch):
    conn = _delete_tool(monkeypatch, ["videos"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            app.query_one("#delete-name", Input).value = "nope"
            await _press_delete(app, pilot)

            assert not isinstance(app.screen, ConfirmDelete)
            status = _status(app)
            assert "no table named 'nope'" in status
            assert conn.dropped == []

            # and an empty box just asks for a name
            app.query_one("#delete-name", Input).value = "   "
            app.query_one("#delete-go", Button).press()
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmDelete)
            assert conn.dropped == []

    asyncio.run(scenario())


def test_tui_delete_tool_sees_a_table_recreated_since_the_last_refresh(monkeypatch):
    """Delete → recreate → delete again, without touching Refresh.

    Regression: the gate used to be the pane's cached listing, so a table
    recreated behind the tool's back — `ingest-images` in a terminal, or this
    app's own Examples runner, which shells out to the step CLI — was refused
    as "no table named …" while the Tables section plainly listed it. The
    check reads the backend now, and repaints the list from that same read.
    """
    from _fakes import FakeTable

    conn = _delete_tool(monkeypatch, ["images"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)

            app.query_one("#delete-name", Input).value = "images"
            await _press_delete(app, pilot)
            app.screen.query_one("#confirm-yes", Button).press()
            for _ in range(50):
                await pilot.pause(0.1)
                if conn.dropped:
                    break
            assert conn.dropped == ["images"]
            assert app._delete_names == []  # gone from the pane, correctly

            # recreated behind the tool's back, with the pane never refreshed
            conn._tables["images"] = FakeTable()
            assert app._delete_names == []  # the snapshot is now stale

            app.query_one("#delete-name", Input).value = "images"
            await _press_delete(app, pilot)

            # the live re-check finds it, so the modal comes up as it should
            assert isinstance(app.screen, ConfirmDelete)
            app.screen.query_one("#confirm-yes", Button).press()
            for _ in range(50):
                await pilot.pause(0.1)
                if len(conn.dropped) == 2:
                    break
            assert conn.dropped == ["images", "images"]

    asyncio.run(scenario())


def test_tui_delete_tool_is_relisted_when_the_backend_changes(monkeypatch):
    """The tool's listing belongs to one database, like every other listing.

    A name typed against local mode must not survive into enterprise, where it
    may not exist — or worse, may name a different table.
    """
    _delete_tool(monkeypatch, ["videos"])

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _open_delete_tool(app, pilot)
            app.query_one("#delete-name", Input).value = "videos"

            # Hold the re-listing so the cleared state is observable; the switch
            # would otherwise immediately repopulate from the (same) fake conn.
            relisted: list = []
            app._list_delete_tables = lambda cfg, epoch=0: relisted.append(cfg)
            app.query_one("#mode", Select).value = "enterprise"
            await pilot.pause()

            assert app._delete_names == []
            assert app.query_one("#delete-name", Input).value == ""
            assert app.query_one("#delete-list", OptionList).option_count == 0
            assert len(relisted) == 1  # re-listed for the newly selected backend

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
