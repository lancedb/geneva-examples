"""Generate the 'why media, not text' chunker-memory explainer PDF."""

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
    "chunker_memory_media_vs_text.pdf",
)

# Embed real TrueType fonts so the report renders identically in every viewer.
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
    title="Chunker Memory: Why Media Pays and Text Doesn't",
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
    "BULLET", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=2.5
)
EQ = ParagraphStyle(
    "EQ",
    parent=BODY,
    fontName="Body-Bold",
    fontSize=10.5,
    alignment=1,
    spaceBefore=3,
    spaceAfter=6,
    textColor=colors.HexColor("#1a3c5e"),
)
CELL = ParagraphStyle("CELL", parent=BODY, fontSize=8.3, leading=10.5, spaceAfter=0)
CELLH = ParagraphStyle(
    "CELLH", parent=CELL, textColor=colors.white, fontName="Body-Bold"
)


def P(t, s=BODY):
    return Paragraph(t, s)


def bullet(t):
    return Paragraph(f"•  {t}", BULLET)


story = []
story.append(P("Chunker Memory: Why Media Pays and Text Doesn't", H1))
story.append(P("geneva-tools / Geneva quickstart &nbsp;|&nbsp; 2026-06-09", SUB))
story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a3c5e")))
story.append(Spacer(1, 6))

story.append(P("The shared mechanism", H2))
story.append(
    P(
        "A Geneva chunker actor materializes the <b>full expansion of its work "
        "item</b> &mdash; up to 1024 source rows and every output row they produce "
        "&mdash; in one in-memory Arrow table before it returns "
        "(<font face='Mono'>ChunkerExpandActor.expand_batch</font>). The execution "
        "path is identical for every workload. Whether that buffer is negligible or "
        "catastrophic comes down to a <b>single term: the number of bytes carried on "
        "each output row</b>. That term is roughly the same for any text chunker and "
        "roughly a thousand times larger for a video or large-image chunker, which is "
        "the whole story.",
        BODY,
    )
)

story.append(P("The governing relationship", H2))
story.append(
    P(
        "Peak per-actor RAM &nbsp;&asymp;&nbsp; B &nbsp;&times;&nbsp; F "
        "&nbsp;&times;&nbsp; S",
        EQ,
    )
)
story.append(
    bullet(
        "<b>B</b> = source rows per work item &mdash; fixed at "
        "<b>1024</b> in this Geneva build, identical for all workloads."
    )
)
story.append(
    bullet(
        "<b>F</b> = output rows per source row (the chunker's "
        "fan-out) &mdash; comparable in range for text and media; "
        "if anything text fans out <i>more</i>."
    )
)
story.append(
    bullet(
        "<b>S</b> = bytes per output row (the payload) &mdash; the "
        "one term that swings by ~1000&times; between text and media."
    )
)
story.append(
    P(
        "Because B is constant and F is similar, the memory difference is driven "
        "almost entirely by S.",
        BODY,
    )
)

story.append(P("Where text and media diverge", H2))
data = [
    [
        P("Dimension", CELLH),
        P("Text chunking", CELLH),
        P("Video / large-image chunking", CELLH),
    ],
    [
        P("Payload per output row (S)", CELL),
        P(
            "A passage or token span: ~0.2&ndash;2&nbsp;KB of UTF-8 plus light "
            "metadata.",
            CELL,
        ),
        P(
            "<font face='Mono'>clip_bytes</font> (a 1-sec mp4, ~0.1&ndash;1&nbsp;MB) "
            "<i>plus</i> a frame image (full-res PNG, 1&ndash;3&nbsp;MB) &rarr; "
            "<b>~1&ndash;3&nbsp;MB</b>.",
            CELL,
        ),
    ],
    [
        P("Arrow representation", CELL),
        P(
            "Compact strings; in-memory size &asymp; stored size; compresses well.",
            CELL,
        ),
        P(
            "<font face='Mono'>large_binary</font> blobs; no cross-row dedup, poor "
            "compression; in-memory &asymp; raw bytes.",
            CELL,
        ),
    ],
    [
        P("Transient / decode memory", CELL),
        P(
            "None &mdash; chunking is string slicing. Nothing is decoded or copied.",
            CELL,
        ),
        P(
            "Per clip: the whole source video copied into a buffer (&times;2&ndash;3), "
            "a raw decoded frame (1080p RGB &asymp; 6&nbsp;MB), and off-heap "
            "ffmpeg/PyAV buffers the GC never sees.",
            CELL,
        ),
    ],
    [
        P("Released incrementally?", CELL),
        P("Moot &mdash; the buffer is tiny to begin with.", CELL),
        P(
            "No. <font face='Mono'>expand_batch</font> returns one blob, so nothing "
            "frees until the entire 1024-row batch finishes; peak = sum over the "
            "batch.",
            CELL,
        ),
    ],
]
tbl = Table(data, colWidths=[1.45 * inch, 2.4 * inch, 3.15 * inch])
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

story.append(P("The same batch, two workloads", H2))
story.append(
    P(
        "Take one work item (B&nbsp;=&nbsp;1024 source rows) through both cases:",
        BODY,
    )
)
story.append(
    bullet(
        "<b>Text</b> &mdash; 1024 documents &times; 50 chunks each &times; 1&nbsp;KB "
        "&asymp; <b>~50&nbsp;MB</b> per actor. Comfortable, even with a high fan-out."
    )
)
story.append(
    bullet(
        "<b>Video</b> &mdash; 1024 videos &times; 5 clips each &times; 2&nbsp;MB "
        "&asymp; <b>~10&nbsp;GB</b> per actor; across 10 actors, <b>~100&nbsp;GB</b> "
        "of fleet RAM &mdash; the observed pile-up."
    )
)
story.append(
    P(
        "Note the text case fans out <i>10&times; more rows</i> yet uses ~200&times; "
        "<i>less</i> memory, purely because each row is ~2000&times; smaller. Fan-out "
        "is not the problem; payload is.",
        BODY,
    )
)

story.append(P("Why the design is sound for text", H2))
story.append(
    P(
        "“Expand the whole batch, then write” implicitly assumes output rows "
        "are small and uniform &mdash; true for the canonical UDTF workloads it was "
        "built for: sentence/window splitting, tokenization, field extraction, "
        "dedup. There, a high row count is cheap because each row is a short string, "
        "and one IPC round-trip per work item is a worthwhile simplification. The "
        "pattern only turns into a liability when a row's payload is a media blob "
        "&mdash; the video/image case &mdash; where S is large enough to push "
        "B&nbsp;&times;&nbsp;F&nbsp;&times;&nbsp;S from megabytes into gigabytes.",
        BODY,
    )
)

story.append(P("Recommended solution", H2))
story.append(
    P(
        "Because B is fixed and F is dictated by the chunking semantics, the durable "
        "answer is to stop the large bytes from ever sitting in the per-actor buffer. "
        "Three tiers, in increasing order of robustness:",
        BODY,
    )
)
story.append(
    bullet(
        "<b>1. Shrink the payload (applied).</b> Store the frame as a 512&nbsp;px "
        "JPEG instead of a full-res PNG &mdash; the S term drops ~10&ndash;30&times; "
        "with no dependency changes. This keeps current runs healthy but only "
        "<i>reduces</i> the exposure; B&nbsp;&times;&nbsp;F&nbsp;&times;&nbsp;S still "
        "scales with media size."
    )
)
story.append(
    bullet(
        "<b>2. Move the bytes out of the row (recommended next step, within our "
        "control).</b> Have the chunker write each <font face='Mono'>clip_bytes</font>"
        " / frame to blob storage and emit only a lightweight <b>reference</b> "
        "(row-id or URI) &mdash; exactly the reference-only pattern the "
        "<font face='Mono'>videos</font> table already uses with "
        "<font face='Mono'>take_blobs</font>. The output row collapses to "
        "pointer-sized, so S returns to text scale and the buffer stays in megabytes "
        "regardless of media size; downstream stages fetch the bytes on demand on the "
        "worker. This <i>removes</i> the exposure rather than reducing it, with no "
        "Geneva change."
    )
)
story.append(
    bullet(
        "<b>3. Bound the batch upstream (the proper Geneva fix).</b> Raise either with "
        "the Geneva team: (a) make <font face='Mono'>ChunkerExpandActor</font> "
        "<b>flush expanded rows in <font face='Mono'>max_rows_per_fragment</font>-sized "
        "increments</b> instead of returning the whole work item as one IPC blob &mdash; "
        "so the knob that already exists actually bounds actor RAM; or (b) expose the "
        "hard-coded <font face='Mono'>row_id_batch_size</font> (1024) as a refresh "
        "parameter so media UDTFs can shrink B and gain parallelism. Either caps memory "
        "by construction for <i>all</i> media workloads, not just this one."
    )
)
story.append(
    P(
        "Tier&nbsp;1 is done; tier&nbsp;2 is the recommended next change in this repo; "
        "tier&nbsp;3 is the lasting fix and is worth filing upstream.",
        BODY,
    )
)

doc.build(story)
print(f"wrote {OUT}")
