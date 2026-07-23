"""Pilot test for the Textual TUI.

Drives the app headless via Textual's ``run_test`` harness (using ``asyncio.run``
so no pytest-asyncio plugin is required): mount, confirm the example tree and the
auto-selected step's form, and that re-selecting a step rebuilds the form.
"""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Select, Tree

from geneva_examples.tui.app import GenevaTUI


def test_tui_mounts_examples_and_tables_sections():
    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            tree = app.query_one("#nav", Tree)
            sections = [n.label.plain for n in tree.root.children]
            assert sections == ["Examples", "Tables"]  # two top-level sections
            assert app.query_one("#mode", Select).value == "local"  # local default
            assert not app.query_one("#table-filter", Input).display  # hidden
            examples_node = tree.root.children[0]
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


def test_tui_table_viewer_populates_grid():
    """The table viewer helpers fill the tree + data grid from fetched rows."""

    async def scenario() -> None:
        app = GenevaTUI()
        async with app.run_test() as pilot:
            await pilot.pause()

            # listing tables adds a leaf per name under the Tables section
            app._set_table_names(["images", "pdfs"], ["geneva_errors"], None)
            await pilot.pause()
            tables_node = app.query_one("#nav", Tree).root.children[1]
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

    asyncio.run(scenario())


def test_tui_run_builds_argv_and_dispatches():
    """Pressing Run turns the form + global controls into the step CLI's argv."""

    async def scenario() -> None:
        app = GenevaTUI()
        captured: dict = {}
        async with app.run_test() as pilot:
            await pilot.pause()
            example = app._current[0]
            await app._select(example, example.step("lightweight"))
            await pilot.pause()

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


def test_tui_run_refreshes_table_in_table_view():
    """In the Tables view the Run action re-loads the table, not a step's UDF."""

    async def scenario() -> None:
        app = GenevaTUI()
        loaded: list[str] = []
        ran: list = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._load_table = lambda cfg, name, system=False, job_id=None: (
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

    errtable = FakeTable(names=["error_type", "job_id", "error_message"])
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
            assert errtable.wheres == ["job_id = 'j-123'"]
            await pilot.pause()
            info = str(app.query_one("#table-info").render())
            assert "where job_id = 'j-123'" in info
            # job_id is promoted to the first column on system tables
            grid = app.query_one("#table-view", DataTable)
            labels = [str(c.label) for c in grid.columns.values()]
            assert labels == ["job_id", "error_type", "error_message"]

    asyncio.run(scenario())


def test_tui_job_id_filter_only_reads_on_system_tables():
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
