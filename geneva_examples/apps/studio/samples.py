"""Load sample inputs for UDF Studio from a local data directory.

The directory holds one source per modality so users can drop in their own data
without touching a cluster:

    <data-dir>/
      images/      image files  -> each sample is the file's raw bytes
      videos/      video files  -> each sample is the file's raw bytes
      audio/       audio files  -> each sample is the file's raw bytes
      input.csv    text rows    -> each sample is a cell from a chosen column

See ``studio_data/README.md`` for the layout shipped with the repo.
"""

from __future__ import annotations

import csv
from pathlib import Path

MODALITIES = ["image", "video", "audio", "text"]
MODALITY_SUBDIR = {"image": "images", "video": "videos", "audio": "audio"}
TEXT_CSV = "input.csv"

# Recognized extensions per modality. If a folder has none of these but does
# hold other files, we read those anyway (the user knows their own data).
_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"},
    "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"},
}


def _list_files(folder: Path, modality: str) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [
        p
        for p in sorted(folder.iterdir())
        if p.is_file() and not p.name.startswith(".")
    ]
    typed = [p for p in files if p.suffix.lower() in _EXTS.get(modality, set())]
    return typed or files


def csv_columns(data_dir: str | Path) -> list[str]:
    """Header columns of ``input.csv`` (empty list if it's missing)."""
    path = Path(data_dir).expanduser() / TEXT_CSV
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(next(csv.reader(f), []))


def sample(
    data_dir: str | Path,
    modality: str,
    n: int = 4,
    csv_column: str | None = None,
) -> dict:
    """Return up to ``n`` sample values for ``modality`` from ``data_dir``.

    Result keys: ``values`` (list of bytes/str), ``labels`` (per-sample names),
    ``modality``, and ``detail`` (a human-readable one-liner). Raises with a
    pointed message if the source is missing so the UI can show it.
    """
    base = Path(data_dir).expanduser()
    n = max(1, int(n))

    if modality == "text":
        path = base / TEXT_CSV
        if not path.exists():
            raise FileNotFoundError(f"no {TEXT_CSV} in {base}")
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            col = csv_column or (cols[0] if cols else None)
            if not col:
                raise ValueError(f"{TEXT_CSV} has no columns")
            if col not in cols:
                raise ValueError(f"column {col!r} not in {cols}")
            values, labels = [], []
            for row in reader:
                values.append(row.get(col) or "")
                labels.append(f"row {len(values)}")
                if len(values) >= n:
                    break
        return {
            "values": values,
            "labels": labels,
            "modality": "text",
            "detail": f"{len(values)} row(s) from {TEXT_CSV} column {col!r}",
        }

    sub = MODALITY_SUBDIR.get(modality)
    if sub is None:
        raise ValueError(f"unknown modality {modality!r}")
    folder = base / sub
    files = _list_files(folder, modality)[:n]
    if not files:
        raise FileNotFoundError(
            f"no files in {folder} — add some {modality} files there"
        )
    return {
        "values": [p.read_bytes() for p in files],
        "labels": [p.name for p in files],
        "modality": modality,
        "detail": f"{len(files)} file(s) from {sub}/",
    }
