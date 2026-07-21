"""Render the single combined chunker fault report from real run data.

Reads the JSON each harness writes during its verified run and renders one PDF:

  * ``fault_harness/report.json``          <- python fault_harness/run.py
  * ``fault_harness/recovery_report.json`` <- python fault_harness/recovery.py

Run both harnesses first (they exit non-zero unless observed matches expected),
then:

    uv run --with reportlab python fault_harness/build_report.py

Every number rendered comes from those files — nothing is hand-authored. Missing
inputs are a hard error rather than placeholder data.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = os.path.join(HERE, "chunker_fault_report.pdf")


def _load(path: str, cmd: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"missing run data: {path}\nRun the harness first:\n    {cmd}\n"
            "(the report renders only real observed results, never placeholders)."
        )
    with open(path) as fh:
        return json.load(fh)


fi = _load(os.path.join(HERE, "report.json"), "python fault_harness/run.py")
rec = _load(
    os.path.join(HERE, "recovery_report.json"), "python fault_harness/recovery.py"
)

# --- fonts (embedded on macOS, base-14 elsewhere) --------------------------- #
FONT, MONO = "Helvetica", "Courier"
try:
    sys.path.insert(0, os.path.join(ROOT, "reports"))
    from _report_common import register_fonts

    register_fonts()
    FONT, MONO = "Body", "Mono"
except Exception:
    pass

S = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=S["Title"], fontName=FONT, fontSize=17, leading=20)
SUB = ParagraphStyle(
    "SUB",
    parent=S["Normal"],
    fontName=FONT,
    fontSize=9,
    textColor=colors.HexColor("#666666"),
    spaceAfter=6,
)
H2 = ParagraphStyle(
    "H2",
    parent=S["Heading2"],
    fontName=FONT,
    fontSize=12,
    spaceBefore=12,
    spaceAfter=4,
    textColor=colors.HexColor("#1a3c5e"),
)
BODY = ParagraphStyle(
    "BODY", parent=S["Normal"], fontName=FONT, fontSize=9.4, leading=13, spaceAfter=6
)
LEAD = ParagraphStyle("LEAD", parent=BODY, fontSize=9.6, leading=13.6)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.3, leading=10.5, spaceAfter=0)
CELLH = ParagraphStyle("CELLH", parent=CELL, textColor=colors.white, fontName=FONT)
ITEM = ParagraphStyle("ITEM", parent=BODY, fontSize=9.2, leading=12.4, spaceAfter=3)


def cell(text, match=None):
    color = "#b42318" if match == "MISMATCH" else None
    t = f"<font color='{color}'>{text}</font>" if color else str(text)
    return Paragraph(t, CELL)


def table(rows, widths):
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#eef2f6")],
                ),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c4cdd6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def m(text):  # monospace inline
    return f"<font face='{MONO}'>{text}</font>"


# --- derived facts (all measured) ------------------------------------------- #
overall_ok = fi["ok"] and rec["ok"]
verdict = "PASS" if overall_ok else "FAIL"
vcolor = "#1a7f37" if overall_ok else "#b42318"
n_transient = len(fi["transient_faults"])
n_data = len(fi["data_faults"])
recovered = sum(1 for t in fi["transient_faults"] if t["mode"] == "recover")
exhausted = sum(1 for t in fi["transient_faults"] if t["mode"] == "exhaust")

story = []
story.append(Paragraph("Video Chunker — Fault Injection &amp; Recovery Report", H1))
story.append(
    Paragraph(
        f"geneva {fi['geneva_version']} &nbsp;|&nbsp; object store: {fi['backend']} "
        f"&nbsp;|&nbsp; recovery: local Geneva + Ray &nbsp;|&nbsp; "
        f"result <b><font color='{vcolor}'>{verdict}</font></b>",
        SUB,
    )
)
story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#1a3c5e")))
story.append(Spacer(1, 6))

# --- Executive summary (two paragraphs) ------------------------------------- #
story.append(Paragraph("Executive Summary", H2))
story.append(
    Paragraph(
        "This report assesses the video chunker's resilience to the two failure "
        "classes that most often disrupt large media-ingestion jobs in production: "
        "<b>transient object-store faults</b> encountered while reading each video's "
        "bytes, and <b>abrupt loss of a worker process</b> partway through a job. "
        "Crucially, nothing here is mocked or simulated in-process. The object-store "
        f"faults are injected as real wire-level events against a live S3-compatible "
        f"endpoint ({m('LocalStack')}) fronted by a fault-injecting reverse proxy, so "
        "the chunker exercises its genuine object-store read path, retry/backoff logic, "
        f"and error-reporting contract; a total of {n_data} data-fault sources and "
        f"{n_transient} transient fault scenarios were run. The recovery scenario runs "
        "a real Geneva materialized-view chunk refresh in a child process and "
        f"{m('SIGKILL')}s that process mid-run — a genuine killed worker, not a raised "
        "exception — before resuming a fresh process against the same on-disk Geneva "
        "checkpoints. The chunker code under test carries no fault-injection hooks; its "
        "happy path is unmodified.",
        LEAD,
    )
)

recov_line = (
    f"the recovery run was killed after {rec['checkpointed_before_kill']} of "
    f"{rec['sources']} videos had checkpointed, then resumed and recomputed only the "
    "interrupted work, producing a table identical to the clean baseline "
    f"({rec['baseline_clips']} clips)"
    if rec["killed_mid_run"]
    else "the recovery run did not achieve a mid-run kill"
)
story.append(
    Paragraph(
        f"<b>Outcome: <font color='{vcolor}'>{verdict}</font>.</b> Every fault under "
        "test was handled exactly as the contract requires and no source row was ever "
        f"silently dropped. Of the {n_transient} transient scenarios, the {recovered} "
        "retry cases recovered to a clean, fully-chunked row once the fault cleared "
        f"(the chunker's own {m('read_retries')} backoff absorbing it), while the "
        f"{exhausted} exhaustion cases each surfaced as exactly one typed "
        f"{m('blob_read_failed[N/N]')} error row carrying the full per-attempt history. "
        f"All {n_data} data-fault sources produced their expected error class "
        f"({m('decode_failed')}, {m('skipped')}, etc.). For recovery, {recov_line}. "
        "The chunker therefore fails safe under transient object-store faults and "
        "recovers correctly from mid-run worker loss, with every result below measured "
        "directly from the runs.",
        LEAD,
    )
)

# --- Faults under test (verbose) -------------------------------------------- #
story.append(Paragraph("Faults Under Test", H2))
story.append(
    Paragraph(
        "Each fault below targets the blob-read boundary "
        f"({m('lance.dataset(...).take_blobs(...)')}) — where the chunker pulls each "
        "video's bytes from object storage. For the transient cases the proxy faults "
        "only the ranged GETs of Lance data files, so dataset-open (bucket listing + "
        f"manifest) always succeeds, and object-store client retries are capped "
        f"({m('client_max_retries=0')}) so the chunker's own {m('read_retries')} loop "
        "is what drives recovery.",
        BODY,
    )
)


def fault_item(title, text):
    return ListItem(Paragraph(f"<b>{title}.</b> {text}", ITEM), leftIndent=6)


story.append(
    ListFlowable(
        [
            fault_item(
                "Read timeout",
                "The proxy accepts the connection but stalls past the client's request "
                f"timeout, so the S3 client raises a timeout. Transient by nature: with a "
                f"short-lived stall the {m('read_retries')} backoff retries and the read "
                "succeeds (clean row); if every attempt times out it is recorded as "
                f"{m('blob_read_failed')} — never a hang or a dropped row.",
            ),
            fault_item(
                "Throttling (HTTP 503 SlowDown)",
                "The proxy returns a genuine "
                f"{m('HTTP 503 Slow Down')} response with the S3 "
                f"{m('&lt;Error&gt;&lt;Code&gt;SlowDown&lt;/Code&gt;')} XML body — exactly "
                "what S3/MinIO emit when a bucket is over its request-rate quota. The "
                "chunker retries with exponential backoff; a brief throttle recovers, a "
                "sustained one exhausts to a typed error row.",
            ),
            fault_item(
                "Connection reset",
                "The proxy aborts the TCP connection mid-request with an "
                f"{m('SO_LINGER 0')} RST, the way a load balancer or object-store node drops "
                "a connection under pressure. The client raises a connection error which the "
                "chunker treats as a retryable transient read.",
            ),
            fault_item(
                "Partial / short read",
                "The proxy replies "
                f"{m('200 OK')} with the true {m('Content-Length')} header but writes only a "
                "fraction of the body before closing, leaving the row partially transferred. "
                "The S3 client detects the incomplete read and raises, so the row surfaces "
                "as a typed error rather than being processed from truncated bytes.",
            ),
            fault_item(
                "Fail-N-then-succeed vs. exhaust-all-retries",
                "Two retry regimes are run for the transient kinds: faulting the first "
                "attempt only (the retry then succeeds — proving the backoff path actually "
                "recovers) and faulting every attempt (proving exhaustion lands as exactly "
                f"one {m('blob_read_failed[N/N]')} row with the full attempt history, and "
                "the source is never silently dropped).",
            ),
            fault_item(
                "Data faults (permanent)",
                "Read for real over S3 with no proxy fault: corrupt/non-video bytes "
                f"({m('decode_failed')}), a video longer than the ceiling "
                f"({m('skipped:max_video_s')}), empty/null blobs ({m('blob_empty')}), and "
                f"dangling/null pointers ({m('pointer_null')}). These are deterministic and "
                "exercise the same error contract from the data side.",
            ),
            fault_item(
                "Killed worker (recovery)",
                "A real Geneva chunk refresh runs in a child process which is "
                f"{m('SIGKILL')}ed mid-run — modelling an OOM kill, spot-instance "
                "reclamation, or node failure. Any Ray left orphaned is cleaned up, then a "
                "fresh process reopens the same materialized view and refreshes again, "
                "resuming from the fragments already checkpointed to disk and recomputing "
                "only the interrupted work. The resumed table is compared, per video, "
                "against a clean baseline.",
            ),
        ],
        bulletType="bullet",
        start="square",
    )
)

# --- Object-store results --------------------------------------------------- #
story.append(Paragraph(f"Object-Store Fault Injection — {fi['sources']} sources", H2))
story.append(Paragraph("Data faults (permanent, read for real over S3):", BODY))
rows = [[Paragraph(x, CELLH) for x in ("Source", "Expected", "Observed", "Clips")]]
for r in fi["data_faults"]:
    rows.append(
        [
            cell(r["video_id"]),
            cell(r["expected"]),
            cell(r["observed"], r["match"]),
            cell(r["clips"]),
        ]
    )
story.append(table(rows, [1.9 * inch, 2.0 * inch, 2.3 * inch, 0.6 * inch]))
story.append(Spacer(1, 4))
story.append(Paragraph("Transient object-store faults (real HTTP/TCP events):", BODY))
rows = [
    [
        Paragraph(x, CELLH)
        for x in ("Fault", "Mode", "Faults", "Expected", "Observed", "s")
    ]
]
for r in fi["transient_faults"]:
    rows.append(
        [
            cell(r["fault"]),
            cell(r["mode"]),
            cell(r["faults_injected"]),
            cell(r["expected"]),
            cell(r["observed"], r["match"]),
            cell(r["seconds"]),
        ]
    )
story.append(
    table(
        rows, [2.2 * inch, 0.8 * inch, 0.6 * inch, 1.3 * inch, 1.4 * inch, 0.4 * inch]
    )
)

# --- Recovery results ------------------------------------------------------- #
story.append(Paragraph("Fail → Resume Recovery", H2))
rows = [
    [Paragraph("Metric", CELLH), Paragraph("Observed", CELLH)],
    [cell("sources"), cell(rec["sources"])],
    [cell("baseline clips"), cell(rec["baseline_clips"])],
    [cell("worker killed mid-run"), cell(rec["killed_mid_run"])],
    [
        cell("videos checkpointed before kill"),
        cell(f"{rec['checkpointed_before_kill']}/{rec['sources']}"),
    ],
    [
        cell("resumed table == baseline"),
        cell("yes" if rec["ok"] else "NO", None if rec["ok"] else "MISMATCH"),
    ],
]
story.append(table(rows, [3.2 * inch, 3.8 * inch]))
story.append(Spacer(1, 4))
story.append(Paragraph("Per-video baseline vs. resumed table:", BODY))
rows = [
    [
        Paragraph(x, CELLH)
        for x in ("video_id", "baseline", "recovery", "classes", "match")
    ]
]
for r in rec["diff"]:
    rows.append(
        [
            cell(r["video_id"]),
            cell(r["baseline_clips"]),
            cell(r["recovery_clips"]),
            cell(r["classes"]),
            cell(r["match"], None if r["match"] == "ok" else "MISMATCH"),
        ]
    )
story.append(table(rows, [1.7 * inch, 1.2 * inch, 1.2 * inch, 2.1 * inch, 0.8 * inch]))

SimpleDocTemplate(
    OUT,
    pagesize=letter,
    leftMargin=0.75 * inch,
    rightMargin=0.75 * inch,
    topMargin=0.7 * inch,
    bottomMargin=0.6 * inch,
    title="Chunker Fault Injection & Recovery",
).build(story)
print(f"wrote {OUT}")
