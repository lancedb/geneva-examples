"""UDF Studio: a Gradio sandbox for prototyping Geneva UDFs / chunkers.

Pick a starting template, point it at sample data on disk (images / videos /
audio / text — see ``studio_data/README.md``), and run your ``transform`` /
``chunk`` function **locally on the driver** to see its output before wiring it
into a stage. Work in progress can be saved to a local LanceDB library.

This app never submits to the cluster or builds a Geneva manifest — it executes
your edited code in this process so the iterate loop is fast. Promoting a
finished function into a real ``@geneva.udf`` factory under ``geneva_examples/udfs/`` and a
stage CLI stays a manual step.

.. warning::
    The editor runs arbitrary Python **in this process with no sandbox** — that
    is the whole point of a local prototyping tool. Keep it bound to loopback
    (the default ``127.0.0.1``). Passing ``--host 0.0.0.0`` or ``--share`` hands
    anyone who can reach the server remote code execution on this machine; only
    do that on a network you fully trust.

Examples
--------
    # launch on http://localhost:7860, reading samples from ./studio_data
    udf-studio

    # custom data dir + library location (still bound to localhost)
    udf-studio --data-dir ~/my-samples --library ~/udf-library
"""

from __future__ import annotations

import logging

import typer

from geneva_examples.apps.studio import library, runner, samples
from geneva_examples.apps.studio.templates import DEFAULT_TEMPLATE, TEMPLATES
from geneva_examples.core.common import setup_logging

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

DEFAULT_DATA_DIR = "studio_data"
DEFAULT_LIBRARY = "udf_library"

# Hosts that keep the server private to this machine. Binding anywhere else (or
# using --share) exposes the in-process code executor to the network.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def _describe(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return f"{len(value)} bytes"
    text = str(value)
    return f'"{text[:60]}…"' if len(text) > 60 else f'"{text}"'


def _rows_to_df(rows: list[dict]):
    import pandas as pd

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _saved_names(library_path: str) -> list[str]:
    try:
        return [r["name"] for r in library.list_udfs(library_path)]
    except Exception as e:  # noqa: BLE001 (surface in UI, never crash launch)
        logger.warning("could not list UDF library at %s: %s", library_path, e)
        return []


def build_ui(*, data_dir: str, library_path: str):
    import gradio as gr

    default = TEMPLATES[DEFAULT_TEMPLATE]

    with gr.Blocks(title="UDF Studio", fill_height=True) as demo:
        gr.Markdown(
            "# UDF Studio\n"
            "Prototype a Geneva **UDF** (`transform(value)`) or **chunker** "
            "(`chunk(value)` that yields rows) and run it locally against your own "
            "sample data. Module-level code runs once per Run, so load models there."
        )

        with gr.Row():
            kind = gr.Radio(
                ["udf", "chunker"],
                value=default["kind"],
                label="Function kind",
                scale=1,
            )
            modality = gr.Radio(
                samples.MODALITIES,
                value=default["modality"],
                label="Input modality",
                scale=2,
            )

        with gr.Row():
            template = gr.Dropdown(
                choices=list(TEMPLATES),
                value=DEFAULT_TEMPLATE,
                label="Template",
                scale=3,
            )
            load_tpl = gr.Button("Load template", scale=1)

        code = gr.Code(
            value=default["code"], language="python", lines=22, label="Function code"
        )

        # ---- Sample data ------------------------------------------------- #
        with gr.Accordion("Sample data", open=True):
            with gr.Row():
                data_dir_tb = gr.Textbox(
                    value=data_dir, label="Data directory", scale=3
                )
                n_in = gr.Number(value=4, precision=0, label="Samples", scale=1)
                csv_col = gr.Dropdown(
                    choices=[], label="CSV column (text)", visible=False, scale=2
                )
            sample_btn = gr.Button("Load samples", variant="secondary")
            sample_status = gr.Markdown()
        samples_state = gr.State([])

        # ---- Run --------------------------------------------------------- #
        run_btn = gr.Button("Run locally", variant="primary")
        summary = gr.Markdown()
        result_df = gr.Dataframe(label="Output", wrap=True)
        error_box = gr.Textbox(label="Errors / traceback", lines=8, visible=False)

        # ---- Library ----------------------------------------------------- #
        with gr.Accordion("UDF library (local LanceDB)", open=False):
            lib_path_tb = gr.Textbox(value=library_path, label="Library path")
            with gr.Row():
                save_name = gr.Textbox(label="Name", scale=3)
                save_btn = gr.Button("Save current", variant="secondary", scale=1)
            with gr.Row():
                saved = gr.Dropdown(
                    choices=_saved_names(library_path), label="Saved", scale=3
                )
                refresh_btn = gr.Button("Refresh", scale=1)
                load_btn = gr.Button("Load", scale=1)
                delete_btn = gr.Button("Delete", scale=1)
            lib_status = gr.Markdown()

        # ---- Handlers ---------------------------------------------------- #
        def on_load_template(name):
            t = TEMPLATES[name]
            return t["code"], t["kind"], t["modality"]

        def on_modality_change(mod, ddir):
            cols = samples.csv_columns(ddir) if mod == "text" else []
            return gr.update(
                choices=cols,
                value=(cols[0] if cols else None),
                visible=(mod == "text"),
            )

        def on_sample(ddir, mod, n, col):
            try:
                res = samples.sample(ddir, mod, n, col or None)
            except Exception as e:  # noqa: BLE001
                return f"❌ {e}", []
            lines = "\n".join(
                f"- `{lbl}` — {_describe(v)}"
                for lbl, v in zip(res["labels"], res["values"], strict=False)
            )
            return f"✅ {res['detail']}\n\n{lines}", res["values"]

        def on_run(k, src, values):
            res = runner.run(k, src, values)
            # Only surface the error box when there's actually an error.
            return (
                res["summary"],
                _rows_to_df(res["rows"]),
                gr.update(value=res["error"], visible=bool(res["error"])),
            )

        def on_save(lib, name, k, mod, src):
            try:
                library.save_udf(lib, name, k, mod, src)
            except Exception as e:  # noqa: BLE001
                return f"❌ {e}", gr.update()
            return f"✅ saved `{name}`", gr.update(
                choices=_saved_names(lib), value=name
            )

        def on_refresh(lib):
            names = _saved_names(lib)
            return gr.update(choices=names), f"{len(names)} saved function(s)"

        def on_load_saved(lib, name):
            if not name:
                return (
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    "Pick a saved function first.",
                )
            try:
                r = library.load_udf(lib, name)
            except Exception as e:  # noqa: BLE001
                return gr.update(), gr.update(), gr.update(), f"❌ {e}"
            return r["code"], r["kind"], r["modality"], f"✅ loaded `{name}`"

        def on_delete(lib, name):
            if not name:
                return gr.update(), "Pick a saved function first."
            library.delete_udf(lib, name)
            return gr.update(
                choices=_saved_names(lib), value=None
            ), f"🗑️ deleted `{name}`"

        # ---- Wiring ------------------------------------------------------ #
        load_tpl.click(
            on_load_template, inputs=template, outputs=[code, kind, modality]
        ).then(on_modality_change, inputs=[modality, data_dir_tb], outputs=csv_col)
        modality.change(
            on_modality_change, inputs=[modality, data_dir_tb], outputs=csv_col
        )
        data_dir_tb.change(
            on_modality_change, inputs=[modality, data_dir_tb], outputs=csv_col
        )
        sample_btn.click(
            on_sample,
            inputs=[data_dir_tb, modality, n_in, csv_col],
            outputs=[sample_status, samples_state],
        )
        run_btn.click(
            on_run,
            inputs=[kind, code, samples_state],
            outputs=[summary, result_df, error_box],
        )

        save_btn.click(
            on_save,
            inputs=[lib_path_tb, save_name, kind, modality, code],
            outputs=[lib_status, saved],
        )
        refresh_btn.click(on_refresh, inputs=lib_path_tb, outputs=[saved, lib_status])
        load_btn.click(
            on_load_saved,
            inputs=[lib_path_tb, saved],
            outputs=[code, kind, modality, lib_status],
        )
        delete_btn.click(
            on_delete, inputs=[lib_path_tb, saved], outputs=[saved, lib_status]
        )

    return demo


@app.command()
def run(
    host: str = typer.Option("127.0.0.1", help="Bind address for the Gradio server."),
    port: int = typer.Option(7860, help="Port for the Gradio server."),
    data_dir: str = typer.Option(
        DEFAULT_DATA_DIR, help="Directory holding images/ videos/ audio/ and input.csv."
    ),
    library: str = typer.Option(
        DEFAULT_LIBRARY, help="Local LanceDB path for saved UDFs."
    ),
    share: bool = typer.Option(False, help="Create a public Gradio share link."),
    log_level: str = typer.Option("INFO", help="Logging level."),
) -> None:
    """Launch the UDF Studio prototyping app."""
    setup_logging(log_level)
    if share or host not in _LOOPBACK_HOSTS:
        logger.warning(
            "SECURITY: UDF Studio executes editor code in-process with no "
            "sandbox. Exposing it (host=%s, share=%s) grants remote code "
            "execution to anyone who can reach it — only do this on a trusted "
            "network.",
            host,
            share,
        )
    demo = build_ui(data_dir=data_dir, library_path=library)
    demo.launch(server_name=host, server_port=port, share=share)


if __name__ == "__main__":
    app()
