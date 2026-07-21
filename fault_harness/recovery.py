"""Real fail -> resume recovery demo for the video chunker (standalone).

Proves the recovery story with **no simulation**: it runs a real Geneva
materialized-view chunk refresh in a **child process**, then **SIGKILLs that
process mid-run** (a genuine killed worker — not a raised exception), cleans up
the orphaned Ray, and **resumes** by reopening the same view and refreshing
again. Geneva loads the fragments that had already checkpointed to disk and only
recomputes what the kill interrupted; the resumed table is verified equal to a
clean, no-fault baseline.

Local mode (on-disk Geneva DB + local Ray); the killed child and the resume
share the same on-disk tables and checkpoints. Standalone integration tool,
outside ``geneva_examples``.

    uv run --with reportlab python fault_harness/recovery.py

Output (git-ignored): ``fault_harness/recovery_report.json`` +
``fault_harness/chunker_recovery_real.pdf``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from geneva_examples.core.common import (  # noqa: E402
    connect,
    local_concurrency,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import load_config  # noqa: E402
from geneva_examples.core.utils.retry import retry_io  # noqa: E402
from geneva_examples.examples.video.chunk_faults import (  # noqa: E402
    _error_classes,
    _make_garbage,
    _make_mp4,
)
from geneva_examples.examples.video.chunkers import chunk_blob_video_udtf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("recovery")

DATA_DIR = os.path.join(ROOT, "recovery_demo_data")
DATASET = os.path.join(DATA_DIR, "train.lance")
VIDEOS = "videos_recovery"
CLIPS = "video_clips_recovery"
BASELINE = "video_clips_recovery_baseline"
GOOD = 8  # enough good videos that a sequential refresh can be caught mid-run
EXPECTED_ERRORS = {"garbage": {"decode_failed"}, "too-long": {"skipped"}}


def _cfg():
    return load_config(None, mode_override="local")


def build_corpus(cfg) -> list[tuple[str, int]]:
    import lance
    import pyarrow as pa

    os.makedirs(DATA_DIR, exist_ok=True)
    corpus = [(f"good-{i}", _make_mp4(2.0)) for i in range(GOOD)]
    corpus += [("garbage", _make_garbage()), ("too-long", _make_mp4(6.0))]
    blob = pa.field(
        "video_blob", pa.large_binary(), metadata={"lance-encoding:blob": "true"}
    )
    schema = pa.schema([pa.field("video_id", pa.string()), blob])
    lance.write_dataset(
        pa.table(
            {"video_id": [v for v, _ in corpus], "video_blob": [b for _, b in corpus]},
            schema=schema,
        ),
        DATASET,
        mode="overwrite",
    )
    ds = lance.dataset(DATASET)
    pointers: list[tuple[str, int]] = []
    for b in ds.scanner(columns=["video_id"], with_row_id=True).to_batches():
        pointers += list(
            zip(b["video_id"].to_pylist(), b["_rowid"].to_pylist(), strict=True)
        )
    conn = connect(cfg)
    for t in (VIDEOS, CLIPS, BASELINE):
        try:
            conn.drop_table(t)
        except Exception:
            pass
    ptr_schema = pa.schema(
        [pa.field("video_id", pa.string()), pa.field("openvid_rowid", pa.int64())]
    )
    retry_io(
        "create_recovery_videos",
        lambda: conn.create_table(
            VIDEOS,
            data=pa.table(
                {
                    "video_id": [v for v, _ in pointers],
                    "openvid_rowid": [r for _, r in pointers],
                },
                schema=ptr_schema,
            ),
        ),
    )
    return pointers


def _make_udtf(cfg):
    num_cpus, num_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=1.0, num_gpus=0.0, memory_gib=1
    )
    return chunk_blob_video_udtf(
        source_uri=DATASET,
        blob_column="video_blob",
        pointer_column="openvid_rowid",
        chunk_seconds=1.0,
        manifest=None,
        max_video_s=5.0,
        num_cpus=num_cpus,
        num_gpus=num_gpus or 0.0,
        memory_bytes=memory_bytes,
    )


def refresh(cfg, clips_table: str, *, create: bool, concurrency: int) -> None:
    conn = connect(cfg)
    if create:
        src = conn.open_table(VIDEOS)
        view = retry_io(
            "create_recovery_view",
            lambda: conn.create_udtf_view(
                clips_table,
                source=src.search(None).select(["video_id", "openvid_rowid"]),
                udtf=_make_udtf(cfg),
            ),
        )
    else:
        view = conn.open_table(clips_table)
    kwargs: dict = {}
    if cfg.is_local:
        concurrency = local_concurrency(concurrency)
        kwargs["_admission_check"] = False
    with runtime_session(conn, cfg):
        view.refresh(
            concurrency=concurrency,
            max_rows_per_fragment=2,
            source_task_size=1,
            **kwargs,
        )
    view.checkout_latest()


def summarize(cfg, clips_table: str) -> dict[str, dict]:
    conn = connect(cfg)
    view = conn.open_table(clips_table)
    view.checkout_latest()
    rows = (
        view.search()
        .select(["video_id", "chunk_id", "clip_bytes", "errors"])
        .limit(10_000)
        .to_list()
    )
    by: dict[str, dict] = {}
    for r in rows:
        e = by.setdefault(r["video_id"], {"clips": 0, "rows": []})
        e["rows"].append(r)
        if r.get("clip_bytes"):
            e["clips"] += 1
    for e in by.values():
        e["classes"] = _error_classes(e["rows"])
    return by


def videos_with_clips(cfg, clips_table: str) -> int:
    """How many source videos already have at least one real clip (read-only)."""
    try:
        by = summarize(cfg, clips_table)
    except Exception:
        return 0
    return sum(1 for e in by.values() if e["clips"] > 0)


def _ray_stop() -> None:
    """Kill any Ray processes orphaned by the SIGKILLed child before resuming."""
    ray_bin = os.path.join(os.path.dirname(sys.executable), "ray")
    subprocess.run(
        [ray_bin if os.path.exists(ray_bin) else "ray", "stop", "--force"],
        capture_output=True,
        check=False,
    )
    time.sleep(1.0)


# --------------------------------------------------------------------------- #
# Child process: run one real refresh that the parent will kill.
# --------------------------------------------------------------------------- #
def _refresh_worker() -> int:
    refresh(_cfg(), CLIPS, create=True, concurrency=1)  # sequential -> catchable
    return 0


def main() -> int:
    cfg = _cfg()
    pointers = build_corpus(cfg)
    total = len(pointers)
    log.info("recovery_corpus sources=%d db=%s", total, cfg.local_db_path)

    # 1) Clean baseline (the no-fault target the resume must match).
    refresh(cfg, BASELINE, create=True, concurrency=4)
    baseline = summarize(cfg, BASELINE)
    log.info("baseline clips=%d", sum(v["clips"] for v in baseline.values()))

    # 2) Start a real refresh in a child process and SIGKILL it partway.
    log.info("starting kill-run child (will be SIGKILLed mid-refresh)")
    child = subprocess.Popen([sys.executable, __file__, "--refresh-worker"])
    killed = False
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(0.5)
        if child.poll() is not None:
            break  # finished before we could catch it mid-run
        done = videos_with_clips(cfg, CLIPS)
        if 1 <= done < total:
            os.kill(child.pid, signal.SIGKILL)
            killed = True
            log.info("SIGKILLed child mid-refresh at %d/%d videos done", done, total)
            break
    child.wait()
    _ray_stop()
    after_kill = videos_with_clips(cfg, CLIPS)
    log.info("after_kill videos_with_clips=%d/%d", after_kill, total)

    # 3) Resume: reopen the same view and finish from on-disk checkpoints.
    log.info("resuming (reopen view + refresh)")
    refresh(cfg, CLIPS, create=False, concurrency=4)
    recovery = summarize(cfg, CLIPS)

    # 4) Verify recovery == baseline; errors only where expected.
    failures: list[str] = []
    if not killed:
        failures.append("kill-run finished before it could be killed (raise GOOD?)")
    if killed and not (0 < after_kill < total):
        failures.append(
            f"no partial progress checkpointed before kill ({after_kill}/{total})"
        )
    diff_rows = []
    for vid in sorted(set(baseline) | set(recovery)):
        b = baseline.get(vid, {"clips": 0, "classes": set()})
        r = recovery.get(vid, {"clips": 0, "classes": set()})
        match = b["clips"] == r["clips"] and b["classes"] == r["classes"]
        diff_rows.append(
            {
                "video_id": vid,
                "baseline_clips": b["clips"],
                "recovery_clips": r["clips"],
                "classes": "/".join(sorted(r.get("classes", set()))) or "clean",
                "match": "ok" if match else "MISMATCH",
            }
        )
        if not match:
            failures.append(
                f"{vid}: baseline(clips={b['clips']},{sorted(b['classes'])}) != "
                f"recovery(clips={r['clips']},{sorted(r['classes'])})"
            )
    for vid, classes in EXPECTED_ERRORS.items():
        obs = recovery.get(vid, {}).get("classes", set())
        if not (obs & classes):
            failures.append(
                f"{vid}: expected {sorted(classes)}, observed {sorted(obs)}"
            )

    import geneva

    report = {
        "geneva_version": geneva.__version__,
        "sources": total,
        "baseline_clips": sum(v["clips"] for v in baseline.values()),
        "killed_mid_run": killed,
        "checkpointed_before_kill": after_kill,
        "diff": diff_rows,
        "ok": not failures,
    }
    with open(os.path.join(HERE, "recovery_report.json"), "w") as fh:
        json.dump(report, fh, indent=2)

    if failures:
        for f in failures:
            log.error("CHECK FAILED: %s", f)
        return 1
    log.info("real_recovery_ok — killed mid-run, resumed, baseline == recovery")
    return 0


if __name__ == "__main__":
    if "--refresh-worker" in sys.argv:
        raise SystemExit(_refresh_worker())
    raise SystemExit(main())
