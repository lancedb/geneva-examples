"""Generate the Geneva chunker-MV fix specification PDF."""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
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
    "geneva_chunker_mv_fix.pdf",
)

_SUP = "/System/Library/Fonts/Supplemental"
pdfmetrics.registerFont(TTFont("Body", f"{_SUP}/Arial.ttf"))
pdfmetrics.registerFont(TTFont("Body-Bold", f"{_SUP}/Arial Bold.ttf"))
pdfmetrics.registerFont(TTFont("Body-Italic", f"{_SUP}/Arial Italic.ttf"))
pdfmetrics.registerFont(TTFont("Body-BoldItalic", f"{_SUP}/Arial Bold Italic.ttf"))
pdfmetrics.registerFont(TTFont("Mono", f"{_SUP}/Courier New.ttf"))
registerFontFamily(
    "Body",
    normal="Body",
    bold="Body-Bold",
    italic="Body-Italic",
    boldItalic="Body-BoldItalic",
)

doc = SimpleDocTemplate(
    OUT,
    pagesize=letter,
    leftMargin=0.75 * inch,
    rightMargin=0.75 * inch,
    topMargin=0.7 * inch,
    bottomMargin=0.6 * inch,
    title="Geneva Chunker-MV Refresh: Memory Fix Specification",
    author="geneva-tools",
)

styles = getSampleStyleSheet()
H1 = ParagraphStyle(
    "H1",
    parent=styles["Title"],
    fontName="Body-Bold",
    fontSize=16,
    spaceAfter=2,
    leading=19,
)
SUB = ParagraphStyle(
    "SUB",
    parent=styles["Normal"],
    fontName="Body",
    fontSize=8.5,
    textColor=colors.HexColor("#666666"),
    spaceAfter=6,
)
H2 = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontName="Body-Bold",
    fontSize=11,
    spaceBefore=9,
    spaceAfter=3,
    textColor=colors.HexColor("#1a3c5e"),
)
BODY = ParagraphStyle(
    "BODY",
    parent=styles["Normal"],
    fontName="Body",
    fontSize=9,
    leading=12,
    spaceAfter=4,
)
BULLET = ParagraphStyle(
    "BULLET", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=3
)
NUM = ParagraphStyle("NUM", parent=BODY, leftIndent=14, bulletIndent=2, spaceAfter=3)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8, leading=10, spaceAfter=0)
CELLH = ParagraphStyle(
    "CELLH", parent=CELL, textColor=colors.white, fontName="Body-Bold"
)
MONO = ParagraphStyle("MONO", parent=CELL, fontName="Mono", fontSize=7.6, leading=10)


def P(t, s=BODY):
    return Paragraph(t, s)


def bullet(t, s=BULLET):
    return Paragraph(f"•  {t}", s)


story = []
story.append(P("Geneva Chunker-MV Refresh: Memory Fix Specification", H1))
story.append(
    P(
        "geneva-tools &nbsp;|&nbsp; geneva==0.13.0b9 &nbsp;|&nbsp; "
        "file: geneva/runners/ray/pipeline.py (unless noted) &nbsp;|&nbsp; "
        "2026-06-09",
        SUB,
    )
)
story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a3c5e")))
story.append(Spacer(1, 5))

# Defect
story.append(P("The defect", H2))
story.append(
    P(
        "A chunker materialized-view refresh expands each work item <b>entirely in "
        "actor memory before any output is emitted</b>. The per-actor peak is the "
        "full expansion of up to 1024 source rows, held simultaneously in three "
        "representations:",
        BODY,
    )
)
story.append(
    bullet(
        "<b>Python lists for the whole batch</b> &mdash; "
        "<font face='Mono'>Chunker._execute_scalar</font> accumulates every output "
        "row into <font face='Mono'>output_cols</font> across all source rows, then "
        "builds one RecordBatch (transformer.py:2374; loop :2409; batch :2440)."
    )
)
story.append(
    bullet(
        "<b>One returned Arrow batch</b> &mdash; "
        "<font face='Mono'>execute_on_record_batch</font> returns that single batch "
        "with all rows (transformer.py:2351)."
    )
)
story.append(
    bullet(
        "<b>One serialized IPC blob</b> &mdash; "
        "<font face='Mono'>ChunkerExpandActor.expand_batch</font> rebuilds it as "
        "<font face='Mono'>result_batch</font> and serializes the whole thing to a "
        "byte buffer (pipeline.py:3791; build :3849; serialize :3855&ndash;3862)."
    )
)
story.append(
    P(
        "The work-item size is the parallelism unit <i>and</i> the expansion unit, "
        "fixed at <font face='Mono'>row_id_batch_size = 1024</font> (:3959; batched "
        ":3988). Peak actor RAM = <b>1024 &times; fanout &times; bytes-per-output-row</b>, "
        "independent of every currently exposed tunable.",
        BODY,
    )
)

# Why the knob fails
story.append(P("Why max_rows_per_fragment does not bound it", H2))
story.append(
    P(
        "<font face='Mono'>max_rows_per_fragment</font> (exposed to us as "
        "<font face='Mono'>checkpoint_size</font>) is applied <b>only on the driver, "
        "after the actor returns the whole blob</b>: the driver reads the full result "
        "back (<font face='Mono'>reader.read_all()</font>, :4099) and then slices it "
        "into fragments (:4110&ndash;4118). It governs output-fragment granularity, "
        "not actor memory; nothing on the actor path consults it.",
        BODY,
    )
)

# The fix
story.append(P("The fix", H2))
story.append(
    P(
        "<b>Bound the actor's expansion unit by output-row count and flush "
        "incrementally</b>, instead of expanding the whole work item and returning "
        "one blob. Two coordinated changes:",
        BODY,
    )
)
story.append(
    P(
        "<b>1. Add a streaming execution path to <font face='Mono'>Chunker</font></b> "
        "(transformer.py). Introduce "
        "<font face='Mono'>execute_on_record_batch_iter(record_batch, max_rows)</font>"
        " &rarr; <font face='Mono'>Iterator[RecordBatch]</font>: the existing "
        "per-source-row loop (:2409), modified to <b>yield the accumulated sub-batch "
        "whenever the output-row count reaches max_rows</b>, then reset and continue. "
        "The current <font face='Mono'>execute_on_record_batch</font> becomes a thin "
        "wrapper that concatenates the iterator, preserving the existing API.",
        NUM,
    )
)
story.append(
    P(
        "<b>2. Make <font face='Mono'>expand_batch</font> write fragments "
        "incrementally and return metadata</b> (pipeline.py:3791). Replace the "
        "single-shot expand/serialize with a loop over the iterator from (1): for each "
        "bounded sub-batch, do the inherited-column join (logic now at :3826&ndash;3851) "
        "and <b>write a destination fragment directly</b> via "
        "<font face='Mono'>LanceFragment.create()</font>, accumulating only lightweight "
        "<font face='Mono'>FragmentMetadata</font>. Return the metadata list instead of "
        "<font face='Mono'>result_ipc</font>; the driver collect loop "
        "(:4084&ndash;4118) gathers metadata and commits rather than reading blobs back "
        "and slicing.",
        NUM,
    )
)
story.append(
    P(
        "<b>Precedent in-tree:</b> the non-chunker UDTF path "
        "(<font face='Mono'>_refresh_udtf_matview</font> / "
        "<font face='Mono'>_run_udtf_refresh</font>) already has actors write data "
        "files via <font face='Mono'>LanceFragment.create()</font> and return "
        "FragmentMetadata JSON, with the driver doing a single "
        "<font face='Mono'>LanceOperation.Overwrite</font> + commit. The chunker path "
        "should adopt the same shape.",
        BODY,
    )
)
story.append(
    P(
        "<b>Net effect:</b> peak actor RAM drops from "
        "<font face='Mono'>1024 &times; fanout &times; S</font> to "
        "<font face='Mono'>max_rows &times; S</font> plus one source row's transient "
        "decode working set &mdash; bounded by construction, for any payload size, "
        "using the knob that already exists.",
        BODY,
    )
)

# Alternatives
story.append(P("Smaller-diff alternative &amp; an orthogonal knob", H2))
story.append(
    bullet(
        "<b>Streaming-generator variant.</b> Keep the driver as writer but convert "
        "<font face='Mono'>expand_batch</font> to a Ray streaming generator "
        "(<font face='Mono'>num_returns=\"streaming\"</font>), yielding one "
        "&le;max_rows IPC sub-batch at a time; the driver "
        "<font face='Mono'>add()</font>s each as it arrives. Bounds the actor buffer "
        "to one sub-batch, but bytes still transit the driver serially &mdash; "
        "strictly weaker than writing fragments from the actor, smaller change."
    )
)
story.append(
    bullet(
        "<b>Expose <font face='Mono'>row_id_batch_size</font>.</b> Surface the "
        "hard-coded 1024 (:3959) as a refresh parameter. Not a fix on its own, but "
        "lets operators shrink the work item B to gain parallelism and lower memory "
        "as a stopgap &mdash; a one-line plumbing change."
    )
)

# Correctness
story.append(P("Correctness considerations", H2))
ctab = [
    [P("Concern", CELLH), P("Handling", CELLH)],
    [
        P("Ordering / IDs", CELL),
        P(
            "<font face='Mono'>__source_row_id</font> / "
            "<font face='Mono'>__child_index</font> are assigned per source row "
            "(transformer.py:2411). Flushing on output count may split one source row's "
            "children across fragments &mdash; safe; rows are independent and the only "
            "guarantee is deterministic order within a partition, preserved by "
            "processing row_ids in order.",
            CELL,
        ),
    ],
    [
        P("output_limit", CELL),
        P(
            "Currently enforced during the driver slice (:4103&ndash;4108); must move "
            "into the streaming consumer so it can stop mid-iteration.",
            CELL,
        ),
    ],
    [
        P("Checkpoint / resume", CELL),
        P(
            "Today a work item checkpoints as a unit. N fragments per work item changes "
            "the resume granularity; <font face='Mono'>format_udtf_fragment_key</font> "
            "and the driver's metadata collection must account for multiple fragments "
            "per item.",
            CELL,
        ),
    ],
    [
        P("Error handling", CELL),
        P(
            "The <font face='Mono'>skip_on_error</font> path (:2426) stays per source "
            "row &mdash; unaffected.",
            CELL,
        ),
    ],
    [
        P("Empty results", CELL),
        P(
            "Preserve the <font face='Mono'>out_rows == 0 &rarr; (None, ...)</font> "
            "short-circuit (:3824) so all-empty work items commit nothing.",
            CELL,
        ),
    ],
]
t = Table(ctab, colWidths=[1.4 * inch, 5.6 * inch])
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
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )
)
story.append(t)

doc.build(story)
print(f"wrote {OUT}")
