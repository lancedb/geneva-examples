"""Shared setup for the report-generation scripts in this directory.

The ``build_report*.py`` scripts render PDFs with reportlab, embedding TrueType
fonts from the macOS system font directory. They therefore run as-is only on
macOS with reportlab installed; see ``reports/README.md``. This module holds the
one piece every script duplicated verbatim — the font registration — so the
fragile hardcoded font paths live in a single place.
"""

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont

# macOS "Supplemental" fonts: Arial for prose, Courier New for inline
# identifiers/metrics.
FONT_DIR = "/System/Library/Fonts/Supplemental"


def register_fonts() -> None:
    """Embed real TrueType fonts so reports render identically in every viewer.

    Non-embedded base-14 Helvetica is substituted inconsistently by some
    rasterizers; embedding Arial/Courier New avoids that.
    """
    pdfmetrics.registerFont(TTFont("Body", f"{FONT_DIR}/Arial.ttf"))
    pdfmetrics.registerFont(TTFont("Body-Bold", f"{FONT_DIR}/Arial Bold.ttf"))
    pdfmetrics.registerFont(TTFont("Body-Italic", f"{FONT_DIR}/Arial Italic.ttf"))
    pdfmetrics.registerFont(
        TTFont("Body-BoldItalic", f"{FONT_DIR}/Arial Bold Italic.ttf")
    )
    pdfmetrics.registerFont(TTFont("Mono", f"{FONT_DIR}/Courier New.ttf"))
    registerFontFamily(
        "Body",
        normal="Body",
        bold="Body-Bold",
        italic="Body-Italic",
        boldItalic="Body-BoldItalic",
    )
