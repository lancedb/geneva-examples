# reports/

One-off analysis write-ups about the Geneva chunker's memory behavior, generated
as PDFs with [`reportlab`](https://pypi.org/project/reportlab/).

## Scripts

| Script | Output PDF |
| ------ | ---------- |
| `build_report.py` | `openvid_chunker_memory_analysis.pdf` |
| `build_report_geneva_fix.py` | `geneva_chunker_mv_fix.pdf` |
| `build_report_media_vs_text.py` | `chunker_memory_media_vs_text.pdf` |

Each script builds its own document; the shared font registration lives in
[`_report_common.py`](_report_common.py) (`register_fonts()`).

For the chunker **fault-injection & recovery** report (rendered from real runs
against LocalStack), see [`../fault_harness/`](../fault_harness/) instead.

## Regenerating

The generated PDFs are **not** committed (they're large binaries that churn on
every edit) — regenerate them on demand:

```bash
uv run --with reportlab python reports/build_report.py
```

> **Note:** these scripts embed TrueType fonts from the macOS system font
> directory (`/System/Library/Fonts/Supplemental`), so they run as-is only on
> **macOS**. On other platforms, point `FONT_DIR` in `_report_common.py` at a
> directory containing `Arial*.ttf` and `Courier New.ttf` (or substitute your own
> fonts). They are author tools, not part of the package, and are excluded from the
> test suite.
