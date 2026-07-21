"""Real-object-store fault harness for the video chunker (standalone).

Runs the **actual** ``chunk_blob_video_udtf`` against a **real** S3 endpoint
(LocalStack) with a fault-injecting proxy in front, so every fault is a genuine
wire-level event — not a synthetic exception. Exercises each object-store fault
the chunker must survive and records what it really observes, then renders a
PDF + JSON report from those measurements (no hand-authored numbers).

Prerequisites: a container runtime (``podman`` or ``docker``). The script starts
LocalStack itself if it is not already running, and stops it again if it did.

    uv run --with boto3 --with reportlab python fault_harness/run.py

Output (git-ignored): ``fault_harness/report.json`` and
``fault_harness/chunker_fault_injection_real.pdf``.

This lives outside ``geneva_examples`` on purpose: it is an integration tool
with infra dependencies, not part of the example package.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from s3_fault_proxy import S3FaultProxy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("fault_harness")

UPSTREAM_PORT = int(os.environ.get("LOCALSTACK_PORT", "4566"))
PROXY_PORT = int(os.environ.get("FAULT_PROXY_PORT", "4610"))
BUCKET = "geneva-faults"
DATASET = f"s3://{BUCKET}/train.lance"
IMAGE = "docker.io/localstack/localstack:3.8.1"
CONTAINER = "geneva-fault-localstack"
CREDS = {
    "aws_access_key_id": "test",
    "aws_secret_access_key": "test",
    "aws_region": "us-east-1",
}


# --------------------------------------------------------------------------- #
# LocalStack lifecycle
# --------------------------------------------------------------------------- #
def _healthy() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{UPSTREAM_PORT}/_localstack/health", timeout=2
        ) as r:
            return b'"s3"' in r.read()
    except Exception:
        return False


def ensure_localstack() -> tuple[bool, str | None]:
    """Return (started_by_us, runtime). Starts LocalStack if not already up."""
    if _healthy():
        log.info("localstack already running on :%d", UPSTREAM_PORT)
        return False, None
    runtime = shutil.which("podman") or shutil.which("docker")
    if not runtime:
        sys.exit("need podman or docker to run LocalStack (neither found on PATH)")
    log.info("starting localstack via %s", os.path.basename(runtime))
    subprocess.run([runtime, "rm", "-f", CONTAINER], capture_output=True, check=False)
    subprocess.run(
        [
            runtime,
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"{UPSTREAM_PORT}:4566",
            "-e",
            "SERVICES=s3",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    for _ in range(60):
        if _healthy():
            log.info("localstack ready")
            return True, runtime
        time.sleep(1)
    sys.exit("localstack did not become healthy in time")


def stop_localstack(runtime: str | None) -> None:
    if runtime:
        subprocess.run(
            [runtime, "rm", "-f", CONTAINER], capture_output=True, check=False
        )
        log.info("stopped localstack")


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #
def write_corpus() -> list[tuple[str, int]]:
    """Write a good + data-faulty corpus to real S3; return (video_id, rowid)."""
    import boto3
    import lance
    import pyarrow as pa

    from geneva_examples.examples.video.chunk_faults import _make_garbage, _make_mp4

    endpoint = f"http://localhost:{UPSTREAM_PORT}"
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception as e:
        log.debug("create_bucket: %s", e)

    corpus = [
        ("good", _make_mp4(2.0)),
        ("garbage", _make_garbage()),
        ("too-long", _make_mp4(6.0)),
    ]
    blob = pa.field(
        "video_blob", pa.large_binary(), metadata={"lance-encoding:blob": "true"}
    )
    schema = pa.schema([pa.field("video_id", pa.string()), blob])
    table = pa.table(
        {"video_id": [v for v, _ in corpus], "video_blob": [b for _, b in corpus]},
        schema=schema,
    )
    direct = {
        **CREDS,
        "aws_endpoint": endpoint,
        "allow_http": "true",
        "aws_virtual_hosted_style_request": "false",
    }
    lance.write_dataset(table, DATASET, storage_options=direct, mode="overwrite")
    ds = lance.dataset(DATASET, storage_options=direct)
    pointers: list[tuple[str, int]] = []
    for b in ds.scanner(columns=["video_id"], with_row_id=True).to_batches():
        pointers += list(
            zip(b["video_id"].to_pylist(), b["_rowid"].to_pylist(), strict=True)
        )
    log.info("wrote corpus to %s (%d sources)", DATASET, len(pointers))
    return pointers


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def _classes(rows: list[dict]) -> list[str]:
    tags: set[str] = set()
    for r in rows:
        errs = r.get("errors")
        if errs:
            tags |= {e.split(":")[0].split("[")[0] for e in errs}
        else:
            tags.add("clean")
    return sorted(tags)


def main() -> int:
    started, runtime = ensure_localstack()
    # Construct before the try but start inside it, so a bind failure still hits
    # `finally` and tears down the container we just started (no leaked LocalStack).
    proxy = S3FaultProxy(upstream_port=UPSTREAM_PORT, listen_port=PROXY_PORT)
    try:
        proxy.start()
        import geneva

        from geneva_examples.examples.video.chunkers import chunk_blob_video_udtf

        pointers = write_corpus()
        good_rid = next(r for v, r in pointers if v == "good")

        # The chunker reads s3:// through the PROXY; client_max_retries=0 makes
        # object-store surface each fault immediately so the chunker's own
        # read_retries drives recovery; short client timeout keeps `timeout` fast.
        storage = {
            **CREDS,
            "aws_endpoint": proxy.endpoint,
            "allow_http": "true",
            "aws_virtual_hosted_style_request": "false",
            "client_max_retries": "0",
            "timeout": "2s",
            "connect_timeout": "2s",
        }
        udtf = chunk_blob_video_udtf(
            source_uri=DATASET,
            blob_column="video_blob",
            pointer_column="openvid_rowid",
            chunk_seconds=1.0,
            manifest=None,
            max_video_s=5.0,  # the 6s corpus row demos `skipped:max_video_s`
            read_retries=3,
            read_retry_sleep_s=0.05,
            storage_options=storage,
        )

        def run_row(rid: int) -> list[dict]:
            return list(udtf.func(rid))

        failures: list[str] = []

        # 1) Data faults — real reads, no proxy fault. good=clean, others=typed.
        data_faults = []
        for vid, expected in (
            ("good", "clean"),
            ("garbage", "decode_failed"),
            ("too-long", "skipped"),
        ):
            rid = next(r for v, r in pointers if v == vid)
            proxy.clear()
            rows = run_row(rid)
            obs = _classes(rows)
            clips = sum(1 for r in rows if r.get("clip_bytes"))
            ok = obs == [expected]  # exact: no unexpected class snuck in
            if not ok:
                failures.append(f"data {vid}: expected {expected}, observed {obs}")
            data_faults.append(
                {
                    "video_id": vid,
                    "expected": expected,
                    "observed": "/".join(obs),
                    "clips": clips,
                    "match": "ok" if ok else "MISMATCH",
                }
            )

        # 2) Transient object-store faults — real HTTP/TCP events via the proxy.
        # (label, kind, count, mode-expected). count=1 -> recover; None -> exhaust.
        cases = [
            ("read timeout — recover", "timeout", 1, "clean"),
            ("read timeout — exhaust", "timeout", None, "blob_read_failed"),
            ("503 SlowDown — recover", "throttle", 1, "clean"),
            ("503 SlowDown — exhaust", "throttle", None, "blob_read_failed"),
            ("connection reset — recover", "reset", 1, "clean"),
            ("connection reset — exhaust", "reset", None, "blob_read_failed"),
            ("partial/short read — exhaust", "short_read", None, "blob_read_failed"),
        ]
        transient = []
        for label, kind, count, expected in cases:
            proxy.arm(kind, count=count, sleep=3.0)
            t0 = time.time()
            rows = run_row(good_rid)
            hits = proxy.clear()
            obs = _classes(rows)
            dt = time.time() - t0
            if expected == "clean":
                ok = obs == ["clean"]
            else:  # exhaust: a typed error row, playable clip absent, nothing dropped
                ok = (
                    expected in obs
                    and "clean" not in obs
                    and len(rows) == 1
                    and not rows[0].get("clip_bytes")
                )
            if not ok:
                failures.append(
                    f"transient {label}: expected {expected}, observed {obs} "
                    f"({len(rows)} rows, {hits} faults)"
                )
            transient.append(
                {
                    "fault": label,
                    "kind": kind,
                    "mode": "recover" if count is not None else "exhaust",
                    "faults_injected": hits,
                    "expected": expected,
                    "observed": "/".join(obs),
                    "seconds": round(dt, 1),
                    "match": "ok" if ok else "MISMATCH",
                }
            )
            log.info("%-32s hits=%s observed=%s (%.1fs)", label, hits, obs, dt)

        report = {
            "geneva_version": geneva.__version__,
            "backend": f"LocalStack S3 (:{UPSTREAM_PORT}) via fault proxy "
            f"(:{PROXY_PORT})",
            "sources": len(pointers),
            "data_faults": data_faults,
            "transient_faults": transient,
            "ok": not failures,
        }
        out_json = os.path.join(HERE, "report.json")
        with open(out_json, "w") as fh:
            json.dump(report, fh, indent=2)
        log.info("wrote %s", out_json)

        if failures:
            for f in failures:
                log.error("CHECK FAILED: %s", f)
            return 1
        log.info("real_fault_harness_ok — all observed outcomes matched")
        return 0
    finally:
        proxy.stop()
        if started:
            stop_localstack(runtime)


if __name__ == "__main__":
    raise SystemExit(main())
