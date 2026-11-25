"""Where the daemon's records go.

A record is a flat dict with a "type" key ("probe" or "stats"); see
owl/daemon.py for the fields. Sinks must never block measurement for long and
must never raise out of emit(): a sink that can't deliver drops the record and
counts it in ``dropped``.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Dict, List

SINK_TYPES = ("jsonl",)


class Sink:
    """Base class: receives records, one at a time."""

    def __init__(self) -> None:
        self.dropped = 0

    def emit(self, record: Dict[str, Any]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.flush()

    def describe(self) -> str:
        return type(self).__name__


class JsonlSink(Sink):
    """One JSON object per line, to a file (appended) or to stdout (path "-")."""

    def __init__(self, path: str = "-") -> None:
        super().__init__()
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

    def __init__(self) -> None:
        super().__init__()
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: Dict[str, Any]) -> None:
        self.records.append(record)


def make_sink(spec: Dict[str, Any]) -> Sink:
    """Build a sink from a validated config entry (see daemon.parse_config)."""
    kind = spec["type"]
    if kind == "jsonl":
        return JsonlSink(spec.get("path", "-"))
    raise ValueError(f"unknown sink type {kind!r}")
