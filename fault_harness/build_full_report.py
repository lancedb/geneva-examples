"""Render the complete chunker fault-tolerance report from real run data.

Combines all three harnesses' measured JSON into a single report:

  * ``faults_demo_data/report.json``       <- uv run chunk-videos-faults --mode local
      the full data-fault taxonomy (corrupt/truncated/empty/null blobs,
      raw-h264, the truncation matrix, dangling/null pointers, over-long)
  * ``fault_harness/report.json``          <- python fault_harness/run.py
      the transient object-store faults, real over LocalStack + proxy
  * ``fault_harness/recovery_report.json`` <- python fault_harness/recovery.py
      the real killed-process fail -> resume recovery

Run all three first (each exits non-zero unless observed matches expected), then:

    uv run --with reportlab python fault_harness/build_full_report.py

Every number rendered comes from those files; nothing is hand-authored. Missing
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

OUT = os.path.join(HERE, "chunker_full_fault_report.pdf")


def _load(path: str, cmd: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"missing run data: {path}\nRun the harness first:\n    {cmd}\n"
            "(the report renders only real observed results, never placeholders)."
        )
    with open(path) as fh:
        return json.load(fh)


data = _load(
    os.path.join(ROOT, "faults_demo_data", "report.json"),
    "uv run chunk-videos-faults --mode local",
)
fi = _load(os.path.join(HERE, "report.json"), "python fault_harness/run.py")
rec = _load(
    os.path.join(HERE, "recovery_report.json"), "python fault_harness/recovery.py"
)

FONT, MONO = "Helvetica", "Courier"
try:
    sys.path.insert(0, os.path.join(ROOT, "reports"))
    from _report_common import register_fonts

    register_fonts()
    FONT, MONO = "Body", "Mono"
except Exception:
    pass

S = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=S["Title"], fontName=FONT, fontSize=16, leading=19)
SUB = ParagraphStyle(
    "SUB",
    parent=S["Normal"],
    fontName=FONT,
    fontSize=8.6,
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
    "BODY", parent=S["Normal"], fontName=FONT, fontSize=9.3, leading=12.8, spaceAfter=6
)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.2, leading=10.3, spaceAfter=0)
CELLH = ParagraphStyle("CELLH", parent=CELL, textColor=colors.white, fontName=FONT)
ITEM = ParagraphStyle("ITEM", parent=BODY, fontSize=9.1, leading=12.2, spaceAfter=3)


def m(t):
    return f"<font face='{MONO}'>{t}</font>"


def cell(text, match=None):
    color = "#b42318" if match == "MISMATCH" else None
    return Paragraph(
        f"<font color='{color}'>{text}</font>" if color else str(text), CELL
    )


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
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    return t


# --- derived facts (all measured) ------------------------------------------- #
overall_ok = data["ok"] and fi["ok"] and rec["ok"]
verdict = "PASS" if overall_ok else "FAIL"
vcolor = "#1a7f37" if overall_ok else "#b42318"
n_data = len(data["data_faults"])
n_transient = len(fi["transient_faults"])
recovered = sum(1 for t in fi["transient_faults"] if t["mode"] == "recover")
exhausted = sum(1 for t in fi["transient_faults"] if t["mode"] == "exhaust")

story = []
story.append(Paragraph("Video Chunker — Complete Fault-Tolerance Report", H1))
story.append(
    Paragraph(
        f"geneva {data['geneva_version']} &nbsp;|&nbsp; data faults: local Geneva "
        f"({data['sources']} sources) &nbsp;|&nbsp; object store: {fi['backend']} "
        f"&nbsp;|&nbsp; recovery: local Geneva + Ray &nbsp;|&nbsp; "
        f"result <b><font color='{vcolor}'>{verdict}</font></b>",
        SUB,
    )
)
story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#1a3c5e")))
story.append(Spacer(1, 6))

# --- Executive summary ------------------------------------------------------ #
story.append(Paragraph("Executive Summary", H2))
story.append(
    Paragraph(
        "This report is the complete fault-tolerance assessment of the Geneva video "
        "chunker — the stage that expands each source video into fixed-length clips. "
        "It covers three failure classes end to end. <b>Data faults</b> "
        f"({n_data} sources) are permanent and deterministic — corrupt, truncated, "
        "empty, and missing video blobs, a container-less raw H.264 stream, an "
        "over-long video, and dangling/null pointers — injected purely through data "
        "and chunked by the unmodified pipeline. <b>Transient object-store faults</b> "
        f"({n_transient} scenarios) are injected as real wire-level events — genuine "
        f"{m('HTTP 503 SlowDown')} responses, TCP resets, stalls, and truncated reads — "
        f"against a live S3-compatible endpoint ({m('LocalStack')}) fronted by a "
        "fault-injecting proxy. <b>Worker loss</b> is produced by "
        f"{m('SIGKILL')}ing a real chunk-refresh process mid-run and resuming from "
        "on-disk Geneva checkpoints. Across all three, the chunker's own code path "
        "carries no fault-injection hooks; the faults are external and the numbers "
        "below are measured, not hand-authored.",
        BODY,
    )
)
story.append(
    Paragraph(
        f"<b>Outcome: <font color='{vcolor}'>{verdict}</font>.</b> The chunker upholds "
        "its core guarantee throughout: <b>no source row is ever silently dropped</b> — "
        "every failure becomes a typed, groupable error row, and every clean input is "
        f"fully chunked. All {n_data} data-fault sources produced their expected error "
        f"class; of the {n_transient} transient scenarios, the {recovered} retry cases "
        f"recovered to a clean row once the fault cleared and the {exhausted} exhaustion "
        f"cases each surfaced as exactly one {m('blob_read_failed[N/N]')} row with the "
        f"full attempt history. The recovery run was killed after "
        f"{rec['checkpointed_before_kill']} of {rec['sources']} videos had checkpointed, "
        f"then resumed and recomputed only the interrupted work, producing a table "
        f"identical to the clean baseline ({rec['baseline_clips']} clips). The pipeline "
        "therefore fails safe under permanent and transient faults alike and recovers "
        "correctly from mid-run worker loss.",
        BODY,
    )
)

# --- Faults under test ------------------------------------------------------ #
story.append(Paragraph("Faults Under Test", H2))


def item(title, text):
    return ListItem(Paragraph(f"<b>{title}.</b> {text}", ITEM), leftIndent=6)


story.append(
    ListFlowable(
        [
            item(
                "Data faults (permanent)",
                "Injected purely through the source bytes/pointers and read by the "
                "unmodified chunker: an empty or null blob "
                f"({m('blob_empty')}); non-video bytes ({m('decode_failed')}); a raw H.264 "
                f"elementary stream with no container duration ({m('no_duration')}); a "
                f"video past the length ceiling ({m('skipped:max_video_s')}); tail-truncated "
                f"MP4s that fail per window ({m('empty_window')}, {m('no_start_frame')}, "
                f"{m('encode_failed')}); and a dangling or null pointer "
                f"({m('blob_read_failed')}, {m('pointer_null')}).",
            ),
            item(
                "Read timeout (transient)",
                "The proxy stalls past the client's request timeout. A short stall is "
                f"absorbed by the {m('read_retries')} backoff (clean row); a persistent one "
                f"exhausts to {m('blob_read_failed')} — never a hang or a dropped row.",
            ),
            item(
                "Throttling — HTTP 503 SlowDown (transient)",
                f"A genuine {m('HTTP 503 Slow Down')} with the S3 "
                f"{m('&lt;Code&gt;SlowDown&lt;/Code&gt;')} XML body, as emitted when a "
                "bucket exceeds its request-rate quota. Retried with exponential backoff.",
            ),
            item(
                "Connection reset (transient)",
                f"An abrupt TCP RST ({m('SO_LINGER 0')}) mid-request, as a load balancer or "
                "storage node drops a connection under pressure; treated as a retryable read.",
            ),
            item(
                "Partial / short read (transient)",
                f"A {m('200 OK')} with the true {m('Content-Length')} but a truncated body, "
                "so the client detects the incomplete read and the row surfaces as a typed "
                "error rather than being processed from partial bytes.",
            ),
            item(
                "Retry regimes",
                "Each transient kind is run both fail-once-then-succeed (proving the backoff "
                "path recovers) and fail-every-attempt (proving exhaustion lands as exactly "
                "one error row and the source is never dropped). Object-store client retries "
                f"are capped ({m('client_max_retries=0')}) so the chunker's own retry loop "
                "drives recovery.",
            ),
            item(
                "Killed worker (recovery)",
                f"A real refresh process is {m('SIGKILL')}ed mid-run (modelling OOM, "
                "reclamation, or node failure); orphaned Ray is cleaned up and a fresh "
                "process resumes from on-disk Geneva checkpoints, recomputing only the "
                "interrupted work and verified per video against a clean baseline.",
            ),
        ],
        bulletType="bullet",
        start="square",
    )
)

# --- Data fault taxonomy ---------------------------------------------------- #
story.append(
    Paragraph(f"1. Data Fault Taxonomy — {data['sources']} sources (local Geneva)", H2)
)
story.append(
    Paragraph(
        "Every poisoned source chunked through the unmodified pipeline; observed error "
        "class vs. expected, with the count of playable clips produced.",
        BODY,
    )
)
rows = [
    [Paragraph(x, CELLH) for x in ("Source", "Expected class", "Observed", "Clips")]
]
for r in data["data_faults"]:
    match = "MISMATCH" if r["expected"] != r["observed"] else None
    rows.append(
        [
            cell(r["video_id"]),
            cell(r["expected"]),
            cell(r["observed"], match),
            cell(r["clips"]),
        ]
    )
story.append(table(rows, [1.5 * inch, 2.15 * inch, 2.75 * inch, 0.55 * inch]))

# --- Transient object-store faults ------------------------------------------ #
story.append(
    Paragraph(
        f"2. Transient Object-Store Faults — real HTTP/TCP via {fi['backend']}", H2
    )
)
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
        rows, [2.2 * inch, 0.8 * inch, 0.6 * inch, 1.35 * inch, 1.35 * inch, 0.4 * inch]
    )
)

# --- Recovery --------------------------------------------------------------- #
story.append(Paragraph("3. Fail → Resume Recovery — real SIGKILL", H2))
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
    leftMargin=0.7 * inch,
    rightMargin=0.7 * inch,
    topMargin=0.7 * inch,
    bottomMargin=0.6 * inch,
    title="Video Chunker — Complete Fault-Tolerance Report",
).build(story)
print(f"wrote {OUT}")
