"""Generate the OpenVid chunker memory analysis report PDF."""

import os

from _report_common import register_fonts
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "openvid_chunker_memory_analysis.pdf",
)

# Embed real TrueType fonts so the report renders identically in every viewer
# (Arial for prose, Courier New for inline identifiers/metrics).
register_fonts()

doc = SimpleDocTemplate(
    OUT,
    pagesize=letter,
    leftMargin=0.75 * inch,
    rightMargin=0.75 * inch,
    topMargin=0.7 * inch,
    bottomMargin=0.6 * inch,
    title="OpenVid Chunker Memory Analysis",
    author="geneva-tools",
)

styles = getSampleStyleSheet()

H1 = ParagraphStyle(
    "H1",
    parent=styles["Title"],
    fontName="Body-Bold",
    fontSize=17,
    spaceAfter=2,
    leading=20,
)
SUB = ParagraphStyle(
    "SUB",
    parent=styles["Normal"],
    fontName="Body",
    fontSize=9,
    textColor=colors.HexColor("#666666"),
    spaceAfter=6,
)
H2 = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontName="Body-Bold",
    fontSize=11.5,
    spaceBefore=10,
    spaceAfter=3,
    textColor=colors.HexColor("#1a3c5e"),
)
BODY = ParagraphStyle(
    "BODY",
    parent=styles["Normal"],
    fontName="Body",
    fontSize=9.3,
    leading=12.6,
    spaceAfter=5,
)
BULLET = ParagraphStyle(
    "BULLET",
    parent=BODY,
    leftIndent=12,
    bulletIndent=2,
    spaceAfter=2.5,
)
CODE = ParagraphStyle(
    "CODE",
    parent=styles["Code"],
    fontName="Mono",
    fontSize=8,
    leading=10,
    textColor=colors.HexColor("#333333"),
)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.3, leading=10.5, spaceAfter=0)
CELLH = ParagraphStyle(
    "CELLH",
    parent=CELL,
    textColor=colors.white,
    fontName="Body-Bold",
)


def P(t, s=BODY):
    return Paragraph(t, s)


def bullet(t):
    return Paragraph(f"•  {t}", BULLET)


story = []

story.append(P("OpenVid Video Chunker: Memory Pile-Up Analysis", H1))
story.append(P("geneva-tools / Geneva quickstart &nbsp;|&nbsp; 2026-06-09", SUB))
story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a3c5e")))
story.append(Spacer(1, 6))

# --- Summary ---
story.append(P("Summary", H2))
story.append(
    P(
        "The OpenVid chunking refresh drove worker RAM upward without bound, "
        "concentrating on a handful of nodes while the rest sat idle and the GPUs "
        "went unused. The cause is structural, not a leak: a Geneva chunker-backed "
        "materialized view executes each work item by <b>buffering its entire "
        "expanded output in memory on a single actor before returning anything</b>. "
        "With a full-resolution PNG stored on every one-second clip, that per-actor "
        "buffer grew into the tens of gigabytes. A stalled refresh (29 minutes, zero "
        "rows produced) confirmed actors were accumulating output and never "
        "completing a batch. The fix shrinks the per-row payload and the per-clip "
        "decode cost; the orphaned compute was stopped on both the Geneva and Ray "
        "layers.",
        BODY,
    )
)

# --- Evidence ---
story.append(P("Observed Symptoms", H2))
story.append(
    bullet(
        "RAM heavily skewed: a few workers at 12&ndash;31&nbsp;GB, most near "
        "0.8&nbsp;GB; GPUs 0% utilized (the work is CPU-only decode/remux)."
    )
)
story.append(
    bullet(
        "The running refresh job reported <font face='Mono'>batches 0/10</font> "
        "and <font face='Mono'>rows_produced 0/0</font> after ~29 minutes "
        "&mdash; 10 actors busy, none having finished a single work item."
    )
)
story.append(
    bullet(
        "Dozens of terminal Ray <i>client-server driver</i> jobs lingered from prior "
        "connections, the documented failure mode where a cancelled Geneva job leaves "
        "its driver (and actors) holding RAM."
    )
)

# --- Root cause ---
story.append(P("Root Cause", H2))
story.append(
    P(
        "The view is a <b>chunker materialized view</b> "
        "(<font face='Mono'>create_udtf_view</font> + "
        "<font face='Mono'>@chunker</font>). Its refresh runs through Geneva's "
        "<font face='Mono'>_append_expanded_fragments</font> path, where:",
        BODY,
    )
)
story.append(
    bullet(
        "Work is split into items of up to <b>1024 source rows</b> "
        "(<font face='Mono'>row_id_batch_size</font>, hard-coded). The number of "
        "parallel actors is <font face='Mono'>min(ceil(videos/1024), "
        "concurrency)</font> &mdash; so ~10k videos yields 10 actors and "
        "<font face='Mono'>concurrency=48</font> is capped to 10 by the data."
    )
)
story.append(
    bullet(
        "Each actor's <font face='Mono'>expand_batch</font> <b>materializes the "
        "full expansion for its whole work item</b> (up to 1024 videos &times; "
        "clips/video &times; bytes/clip) as one in-memory Arrow table, then ships it "
        "back over IPC. This buffer is the pile-up."
    )
)
story.append(
    bullet(
        "<font face='Mono'>max_rows_per_fragment</font> (our "
        "<font face='Mono'>checkpoint_size</font>) is applied <i>on the driver "
        "afterward</i>, only to slice the finished result into output fragments. "
        "<b>It does not bound actor memory</b> &mdash; correcting an earlier "
        "assumption."
    )
)
story.append(
    bullet(
        "The dominant per-row term was a <b>full-resolution lossless PNG</b> of each "
        "clip's first frame. Downstream, CLIP/BLIP downsize to &le;336&nbsp;px and "
        "OpenPose rescales internally, so that resolution was largely wasted."
    )
)
story.append(
    bullet(
        "Levers that do <b>not</b> help: <font face='Mono'>partition_by</font> "
        "(ignored on the chunker path) and source-table fragment size set by ingest "
        "(all new row-ids are re-batched into 1024-row work items regardless)."
    )
)

story.append(Spacer(1, 2))

# --- Changes table ---
story.append(P("Changes Applied", H2))

data = [
    [P("Action", CELLH), P("Change", CELLH), P("Effect", CELLH)],
    [
        P("Stop compute", CELL),
        P(
            "Cancelled the running Geneva refresh; killed the orphaned Ray "
            "client-server driver on the head. Verified 0 active on both layers.",
            CELL,
        ),
        P("Frees actors/RAM held by the stalled job and stale drivers.", CELL),
    ],
    [
        P("Frame payload", CELL),
        P(
            "Store a 512&nbsp;px JPEG (q85, longest side, LANCZOS) instead of a "
            "full-res PNG. Column stays <font face='Mono'>large_binary</font>; "
            "consumers use <font face='Mono'>Image.open()</font>, so no schema or "
            "downstream change.",
            CELL,
        ),
        P(
            "~10&ndash;30&times; smaller per-row frame &mdash; the main lever on "
            "per-actor buffer size. Near-lossless for CLIP/BLIP/OpenPose.",
            CELL,
        ),
    ],
    [
        P("Per-clip decode", CELL),
        P(
            "Open each clip's container <b>once</b>: decode the start frame and remux "
            "the [start,&nbsp;end) packets from the same handle (was two opens + two "
            "full-byte copies per clip).",
            CELL,
        ),
        P("Halves per-clip decode setup and transient byte-buffer churn.", CELL),
    ],
    [
        P("Fragment size", CELL),
        P("<font face='Mono'>checkpoint_size</font> 256 &rarr; 32.", CELL),
        P(
            "Healthier output fragments and smaller driver-side commits (secondary; "
            "not the memory fix).",
            CELL,
        ),
    ],
    [
        P("Concurrency", CELL),
        P("Left at 48 (per request).", CELL),
        P(
            "Effective parallelism is bounded by work-item count (~10), not this "
            "value, for the current dataset.",
            CELL,
        ),
    ],
]

tbl = Table(data, colWidths=[0.95 * inch, 3.5 * inch, 2.55 * inch])
tbl.setStyle(
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
story.append(tbl)

# --- Expected outcome / next steps ---
story.append(P("Expected Outcome &amp; Validation", H2))
story.append(
    P(
        "With the frame reduced to a 512&nbsp;px JPEG, the dominant per-row payload "
        "becomes the one-second stream-copied clip (tens to hundreds of KB), so each "
        "actor's buffered expansion table shrinks by roughly an order of magnitude. "
        "On the next refresh, confirm success by watching "
        "<font face='Mono'>rows_produced</font> advance steadily and per-worker "
        "RAM stay flat rather than climb.",
        BODY,
    )
)
story.append(
    P(
        "<b>If deeper parallelism or a hard memory ceiling is needed later:</b> the "
        "1024-row work-item size is fixed in this Geneva build, so the remaining "
        "options are (a) refresh in smaller source waves to cap videos-per-item at "
        "the cost of fewer concurrent actors, or (b) raise the work-item count by "
        "scaling total video volume. Neither is required to resolve the current "
        "pile-up.",
        BODY,
    )
)

doc.build(story)
print(f"wrote {OUT}")
