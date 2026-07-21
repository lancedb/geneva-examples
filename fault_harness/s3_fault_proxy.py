"""A fault-injecting S3 reverse proxy for the real-object-store fault harness.

Sits in front of a real S3-compatible endpoint (LocalStack) and injects genuine
HTTP/TCP faults on the requests that carry object bytes, while forwarding
everything else untouched so the client can still open the dataset. Unlike the
in-process ``GENEVA_BLOB_FAULT`` hook in the chunker (which raises a synthetic
exception), the faults here are real wire-level events the S3 client actually
observes:

  * ``throttle``    real ``HTTP 503 SlowDown`` with the S3 XML error body
  * ``reset``       an abrupt TCP RST (SO_LINGER 0) mid-request
  * ``timeout``     the socket stalls past the client's request timeout
  * ``short_read``  a ``200`` with the real Content-Length but a truncated body

Faults target only requests matching ``match`` (default: ``GET`` of a Lance
``/data/`` file — the blob byte reads), so dataset-open (bucket listing +
manifest) always succeeds. ``arm(kind, count=N)`` faults the first ``N`` matching
requests then passes through (fail-N-then-succeed); ``count=None`` faults every
one (exhaust-all-retries).

Standalone, stdlib-only, and deliberately outside ``geneva_examples`` — it is an
integration tool, not part of the example package.
"""

from __future__ import annotations

import http.client
import http.server
import socket
import socketserver
import struct
import threading
import time
from collections.abc import Callable

_SLOWDOWN_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<Error><Code>SlowDown</Code>"
    b"<Message>Please reduce your request rate.</Message></Error>"
)


def _is_blob_read(command: str, path: str) -> bool:
    """Default target: the ranged GETs of Lance data files (the blob bytes)."""
    return command == "GET" and "/data/" in path


class _ReusableServer(socketserver.ThreadingTCPServer):
    # Reuse the address so back-to-back runs don't fail binding while the
    # previous socket lingers in TIME_WAIT.
    allow_reuse_address = True
    daemon_threads = True


class S3FaultProxy:
    """Threaded reverse proxy to ``upstream`` with per-request fault injection."""

    def __init__(
        self,
        *,
        upstream_host: str = "localhost",
        upstream_port: int = 4566,
        listen_host: str = "127.0.0.1",
        listen_port: int = 4610,
        match: Callable[[str, str], bool] = _is_blob_read,
    ) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.listen = (listen_host, listen_port)
        self.match = match
        self._fault: dict = {"kind": None, "count": None, "hits": 0, "sleep": 3.0}
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.listen[0]}:{self.listen[1]}"

    def arm(self, kind: str, *, count: int | None = None, sleep: float = 3.0) -> None:
        """Inject ``kind`` on the next ``count`` matching requests (None = all)."""
        with self._lock:
            self._fault = {"kind": kind, "count": count, "hits": 0, "sleep": sleep}

    def clear(self) -> int:
        """Disarm; return how many requests were faulted since the last arm."""
        with self._lock:
            hits = self._fault["hits"]
            self._fault = {"kind": None, "count": None, "hits": 0, "sleep": 3.0}
            return hits

    def _take(self, command: str, path: str) -> str | None:
        """Return the fault kind to apply to this request, or None to forward."""
        if not self.match(command, path):
            return None
        with self._lock:
            f = self._fault
            if f["kind"] is None:
                return None
            if f["count"] is not None and f["hits"] >= f["count"]:
                return None
            f["hits"] += 1
            return f["kind"]

    def start(self) -> S3FaultProxy:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _forward(self, body: bytes | None) -> tuple[int, list, bytes]:
                conn = http.client.HTTPConnection(*proxy.upstream, timeout=30)
                headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, resp.getheaders(), data

            def _handle(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else None
                kind = proxy._take(self.command, self.path)

                if kind == "throttle":
                    self.send_response(503)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(_SLOWDOWN_XML)))
                    self.end_headers()
                    self.wfile.write(_SLOWDOWN_XML)
                    return
                if kind == "reset":
                    try:
                        self.connection.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_LINGER,
                            struct.pack("ii", 1, 0),
                        )
                        self.connection.close()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                if kind == "timeout":
                    time.sleep(proxy._fault["sleep"])
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                if kind == "short_read":
                    _status, _headers, data = self._forward(body)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[: max(1, len(data) // 3)])  # truncated body
                    self.close_connection = True
                    return

                status, headers, data = self._forward(body)
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                if data:
                    self.wfile.write(data)

            do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _handle

        self._server = _ReusableServer(self.listen, Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        time.sleep(0.3)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> S3FaultProxy:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()
