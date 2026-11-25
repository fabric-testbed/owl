"""Where the daemon's records go.

A record is a flat dict with a "type" key ("probe" or "stats"); see
owl/daemon.py for the fields. Sinks must never block measurement for long and
must never raise out of emit(): a sink that can't deliver drops the record and
counts it in ``dropped``.

Sink config entries (validated by validate_spec):

    {"type": "jsonl", "path": "-"}                       stdout, or a file to append to
    {"type": "http-lineprotocol",
     "url": "https://influx.example:8086/api/v2/write?org=ORG&bucket=BUCKET&precision=ns",
     "token_file": "/etc/owl/influx-token",              or "token": "..."; sent as "Authorization: Token ..."
     "measurement": "owl", "summary_measurement": "owl_summary", "stats_measurement": "owl_stats",
     "batch_size": 5000, "flush_interval": 10, "max_buffer": 100000, "timeout": 10, "gzip": true}

Either kind takes "records": ["probe", "summary", "stats"] to choose which record types
it gets (default: all), e.g. to send raw probes and summaries to different buckets.
"""

from __future__ import annotations

import collections
import gzip as gziplib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import IO, Any, Callable, Deque, Dict, List, Optional

RECORD_TYPES = ("probe", "summary", "stats")

# Line protocol layout of each record type: which keys become tags. Every other
# non-None value becomes a field; "type" and "time_ns" are never written as either.
_TAGS = {
    "probe": ("af", "clock", "dst_ip", "dst_label", "src_ip", "src_label", "status"),  # sorted: faster ingest
    "summary": ("af", "clock", "dst_ip", "dst_label", "src_ip", "src_label"),
    "stats": ("src_label",),
}

_SPECS: Dict[str, Dict[str, Any]] = {
    # key -> (kind, default); kind: "str", "int+", "num+", "bool", "records"
    "jsonl": {"path": ("str", "-")},
    "http-lineprotocol": {
        "url": ("str", None),
        "token": ("str", None),
        "token_file": ("str", None),
        "measurement": ("str", "owl"),
        "summary_measurement": ("str", "owl_summary"),
        "stats_measurement": ("str", "owl_stats"),
        "batch_size": ("int+", 5000),
        "flush_interval": ("num+", 10.0),
        "max_buffer": ("int+", 100_000),
        "timeout": ("num+", 10.0),
        "gzip": ("bool", True),
    },
}
SINK_TYPES = tuple(_SPECS)


def validate_spec(spec: Any) -> Dict[str, Any]:
    """Check one sink config entry; return it with defaults filled in. Raises ValueError."""
    if not isinstance(spec, dict) or spec.get("type") not in _SPECS:
        raise ValueError(f"must be an object with type one of: {', '.join(SINK_TYPES)}")
    kind = spec["type"]
    allowed = _SPECS[kind]
    unknown = sorted(set(spec) - set(allowed) - {"type", "records"})
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)} "
                         f"(allowed: {', '.join(['type', 'records', *sorted(allowed)])})")
    out: Dict[str, Any] = {"type": kind}
    for key, (check, default) in allowed.items():
        value = spec.get(key, default)
        if value is None:
            out[key] = None
            continue
        if check == "str" and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
        if check in ("int+", "num+"):
            kinds = (int,) if check == "int+" else (int, float)
            if isinstance(value, bool) or not isinstance(value, kinds) or value <= 0:
                raise ValueError(f"{key} must be {'an integer' if check == 'int+' else 'a number'} > 0")
        if check == "bool" and not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        out[key] = value
    records = spec.get("records", list(RECORD_TYPES))
    if not isinstance(records, list) or not records or any(r not in RECORD_TYPES for r in records):
        raise ValueError(f"records must be a non-empty list drawn from: {', '.join(RECORD_TYPES)}")
    out["records"] = records
    if kind == "http-lineprotocol":
        if not out["url"]:
            raise ValueError("url is required")
        if not out["url"].startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if out["token"] and out["token_file"]:
            raise ValueError("give token or token_file, not both")
        if out["batch_size"] > out["max_buffer"]:
            raise ValueError("batch_size must not exceed max_buffer")
    return out


class Sink:
    """Base class: receives records, one at a time."""

    def __init__(self, records=RECORD_TYPES) -> None:
        self.dropped = 0
        self.record_types = set(records)

    def wants(self, record: Dict[str, Any]) -> bool:
        return record.get("type") in self.record_types

    def emit(self, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.flush()

    def describe(self) -> str:
        return type(self).__name__

    def stats(self) -> Dict[str, int]:
        """Counters merged into the daemon's stats record (summed over sinks)."""
        return {"sink_dropped": self.dropped}


class JsonlSink(Sink):
    """One JSON object per line, to a file (appended) or to stdout (path "-")."""

    def __init__(self, path: str = "-", records=RECORD_TYPES) -> None:
        super().__init__(records)
        self.path = path
        self._own = path != "-"
        self._f: IO[str] = open(path, "a", buffering=1) if self._own else sys.stdout

    def emit(self, record: Dict[str, Any]) -> None:
        try:
            self._f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except (OSError, ValueError):
            self.dropped += 1

    def flush(self) -> None:
        try:
            self._f.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self.flush()
        if self._own:
            self._f.close()

    def describe(self) -> str:
        return f"jsonl to {'stdout' if self.path == '-' else self.path}"


class ListSink(Sink):
    """Keeps records in memory (tests)."""

    def __init__(self, records=RECORD_TYPES) -> None:
        super().__init__(records)
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: Dict[str, Any]) -> None:
        self.records.append(record)


# --- InfluxDB line protocol ------------------------------------------------------------------


def _escape_key(s: str) -> str:
    """Escape a measurement name, tag key, tag value or field key."""
    return s.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _escape_measurement(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _field_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_line_protocol(record: Dict[str, Any], measurement: str) -> Optional[str]:
    """One record as an InfluxDB line protocol line (ns timestamp), or None if it has no fields."""
    tag_keys = _TAGS.get(record.get("type"), ())
    tags = []
    for k in tag_keys:
        v = record.get(k)
        if v is not None and str(v) != "":  # line protocol has no empty tag values
            tags.append(f"{k}={_escape_key(str(v))}")
    fields = [
        f"{_escape_key(k)}={_field_value(v)}"
        for k, v in record.items()
        if k not in tag_keys and k not in ("type", "time_ns") and v is not None
    ]
    if not fields:
        return None
    head = ",".join([_escape_measurement(measurement)] + tags)
    return f"{head} {','.join(fields)} {int(record['time_ns'])}"


class HttpLineProtocolSink(Sink):
    """Batches records as line protocol and POSTs them from a background thread.

    emit() only appends to a bounded in-memory buffer, so a slow or unreachable
    endpoint never stalls measurement. The sender thread posts up to
    ``batch_size`` lines every ``flush_interval`` seconds (sooner when a full
    batch is waiting). A batch that fails with a network error, 429 or 5xx is
    retried, with exponential backoff, before anything newer is sent; a 4xx
    other than 429 means the endpoint rejects the data (bad token, bad bucket,
    malformed lines), so retrying can't help: the batch is dropped, counted and
    logged. When the buffer is full, the oldest lines are dropped and counted.
    """

    retry_initial = 1.0  # seconds; doubled after each consecutive failure
    retry_max = 60.0
    close_timeout = 5.0  # seconds close() spends trying to deliver what is left

    def __init__(
        self,
        url: str,
        *,
        token: Optional[str] = None,
        measurement: str = "owl",
        summary_measurement: str = "owl_summary",
        stats_measurement: str = "owl_stats",
        batch_size: int = 5000,
        flush_interval: float = 10.0,
        max_buffer: int = 100_000,
        timeout: float = 10.0,
        gzip: bool = True,
        records=RECORD_TYPES,
        log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
    ) -> None:
        super().__init__(records)
        self.url = url
        self.token = token
        self.measurements = {"probe": measurement, "summary": summary_measurement, "stats": stats_measurement}
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self.timeout = timeout
        self.gzip = gzip
        self.log = log

        self.sent_lines = 0  # delivered
        self.failed_posts = 0  # attempts that will be retried
        self.rejected = 0  # lines the endpoint refused (4xx), not retried

        self._buf: Deque[str] = collections.deque()
        self._batch: List[str] = []  # being delivered; owned by the sender thread
        self._cond = threading.Condition()
        self._closing = False
        self._down_since: Optional[float] = None
        self._thread = threading.Thread(target=self._run, name="owl-http-sink", daemon=True)
        self._thread.start()

    def describe(self) -> str:
        return f"http-lineprotocol to {self.url.split('?', 1)[0]}"

    def stats(self) -> Dict[str, int]:
        with self._cond:
            buffered = len(self._buf) + len(self._batch)
        return {"sink_dropped": self.dropped + self.rejected, "sink_buffered": buffered,
                "sink_failed_posts": self.failed_posts}

    def emit(self, record: Dict[str, Any]) -> None:
        line = to_line_protocol(record, self.measurements.get(record.get("type"), "owl"))
        if line is None:
            return
        with self._cond:
            if len(self._buf) + len(self._batch) >= self.max_buffer:
                self._buf.popleft()  # oldest first: recent data is worth more
                self.dropped += 1
            self._buf.append(line)
            if len(self._buf) >= self.batch_size:
                self._cond.notify()

    def flush(self) -> None:
        with self._cond:
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify()
        self._thread.join(self.close_timeout + self.timeout + 1)
        with self._cond:
            left = len(self._buf) + len(self._batch)
        if left:
            self.dropped += left
            self.log(f"http sink: {left} lines not delivered at shutdown")

    # -- sender thread --

    def _run(self) -> None:
        backoff = 0.0
        give_up_at: Optional[float] = None  # set once closing: stop retrying after close_timeout
        while True:
            with self._cond:
                if not self._batch:
                    # Send when a full batch is waiting, every flush_interval, on flush(), or on close.
                    if not self._closing and len(self._buf) < self.batch_size:
                        self._cond.wait(self.flush_interval)
                    n = min(self.batch_size, len(self._buf))
                    self._batch = [self._buf.popleft() for _ in range(n)]
                closing = self._closing
                if not self._batch:
                    if closing:
                        return
                    continue
            if closing and give_up_at is None:
                give_up_at = time.monotonic() + self.close_timeout
            if self._post(self._batch):
                with self._cond:
                    self._batch = []
                backoff = 0.0
                continue
            if give_up_at is not None and time.monotonic() >= give_up_at:
                return
            backoff = min(self.retry_max, backoff * 2 if backoff else self.retry_initial)
            # Sleep out the backoff. New records keep arriving during an outage and notify the
            # condition; only close() may cut the wait short.
            retry_at = time.monotonic() + (min(backoff, 0.5) if closing else backoff)
            with self._cond:
                while not (self._closing and not closing):
                    remaining = retry_at - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)

    def _post(self, lines: List[str]) -> bool:
        """POST one batch. True if it is done with (delivered or rejected), False to retry."""
        body = ("\n".join(lines) + "\n").encode()
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self.gzip:
            body = gziplib.compress(body)
            headers["Content-Encoding"] = "gzip"
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode(errors="replace").strip()
            if e.code == 429 or e.code >= 500:
                return self._failed(f"HTTP {e.code} {detail}")
            self.rejected += len(lines)
            self.log(f"http sink: endpoint rejected {len(lines)} lines with HTTP {e.code}: {detail}")
            self._recovered()
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            return self._failed(str(reason))
        self.sent_lines += len(lines)
        self._recovered()
        return True

    def _failed(self, why: str) -> bool:
        self.failed_posts += 1
        if self._down_since is None:
            self._down_since = time.monotonic()
            self.log(f"http sink: delivery failing, buffering and retrying: {why}")
        return False

    def _recovered(self) -> None:
        if self._down_since is not None:
            self.log(f"http sink: delivering again after {time.monotonic() - self._down_since:.0f} s")
            self._down_since = None


def make_sink(spec: Dict[str, Any], log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> Sink:
    """Build a sink from a config entry (validated here too). Raises ValueError or OSError."""
    spec = validate_spec(spec)
    kind = spec["type"]
    if kind == "jsonl":
        return JsonlSink(spec["path"], records=spec["records"])
    token = spec["token"]
    if spec["token_file"]:
        with open(spec["token_file"]) as f:
            token = f.read().strip()
        if not token:
            raise ValueError(f"token_file {spec['token_file']} is empty")
    return HttpLineProtocolSink(
        spec["url"], token=token, measurement=spec["measurement"],
        summary_measurement=spec["summary_measurement"], stats_measurement=spec["stats_measurement"],
        batch_size=spec["batch_size"],
        flush_interval=float(spec["flush_interval"]), max_buffer=spec["max_buffer"],
        timeout=float(spec["timeout"]), gzip=spec["gzip"], records=spec["records"], log=log,
    )
