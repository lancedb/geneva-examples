# fault_harness/

A **real-object-store** fault-injection harness for the video chunker. It runs
the actual `chunk_blob_video_udtf` against a real S3 endpoint (LocalStack) with a
fault-injecting reverse proxy in front, so every fault is a genuine wire-level
event rather than a synthetic in-process exception.

This is an **integration tool**, deliberately kept outside the `geneva_examples`
package (it has infra dependencies and is not part of the examples).

## What it proves

For each object-store fault the chunker must survive, the harness measures what
the chunker *actually* observes reading a blob over `s3://`:

| Fault | How it's injected (real) | Recover (retry) | Exhaust |
| ----- | ------------------------ | --------------- | ------- |
| read timeout | proxy stalls past the client request timeout | clean | `blob_read_failed[N/N]` |
| throttling | real `HTTP 503 SlowDown` + S3 XML body | clean | `blob_read_failed[N/N]` |
| connection reset | abrupt TCP RST (`SO_LINGER 0`) | clean | `blob_read_failed[N/N]` |
| partial/short read | `200` with real Content-Length, truncated body | — | `blob_read_failed[N/N]` |

Plus data faults (corrupt bytes → `decode_failed`, over-long → `skipped`) read
for real over S3. Object-store client retries are capped
(`client_max_retries=0`) so the chunker's own `read_retries` drives recovery, and
the fault targets only the Lance `/data/` byte reads so dataset-open still works.

## Run it

Needs a container runtime (`podman` or `docker`). The script starts LocalStack
itself if it isn't already running (and stops it again if it did):

```bash
uv run --with boto3 --with reportlab python fault_harness/run.py
```

Exits non-zero if any observed outcome diverges from expected. Outputs (both
git-ignored, regenerated on demand):

- `fault_harness/report.json` — the measured results
- `fault_harness/chunker_fault_injection_real.pdf` — the rendered report

## Files

- `s3_fault_proxy.py` — the stdlib fault-injecting S3 reverse proxy (`S3FaultProxy`).
- `run.py` — orchestrator: manage LocalStack, write a corpus to real S3, run the
  chunker through the proxy per fault, verify, and render the report.

## Relationship to the in-package demos

`chunk-videos-faults` / `chunk-videos-recovery` (in `geneva_examples`) inject
faults **in-process** at the `take_blobs` boundary (`GENEVA_BLOB_FAULT`): fast,
no infra, runs in `--mode local` and CI, but the fault is a simulated exception.
This harness is the real-wire counterpart — slower and infra-dependent, but the
faults are genuine S3/TCP events.
