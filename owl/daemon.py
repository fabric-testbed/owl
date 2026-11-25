"""`owl daemon`: reflect probes from peers and probe every peer at a fixed interval.

One process, one thread, one selector loop:

- a Reflector on the listen port (default: all IPv4 and IPv6 addresses) answers
  probes from other daemons and from `owl ping`;
- one ProbeEngine per address family sends probes from an ephemeral port and
  turns each into a Result;
- every Result becomes a "probe" record for the sinks, and every
  ``stats_interval`` seconds a "stats" record with the counters is emitted and
  logged to stderr (journald under systemd).

Peers are probed ``interval`` seconds apart, staggered evenly across the
interval so a large mesh doesn't send in bursts. Labels are free-form strings
from the config, written into every record, which is how site names get into
the data without owl knowing anything about FABRIC.

Config (JSON; only "label" and "peers" are required):

    {
      "label": "STAR",
      "peers": [{"addr": "10.140.135.2", "label": "PSC"}, {"addr": "2602:fcfb:1::2", "label": "PSC"}],
      "clock": "hw", "port": 49005, "interval": 5.0, "timeout": 2.0, "size": 16,
      "ifaces": ["ens7"], "listen": ["0.0.0.0", "::"], "stats_interval": 60.0,
      "sinks": [{"type": "jsonl", "path": "-"}]
    }

Probe record fields:

    type="probe", time_ns (wall clock when sent), src_label, dst_label, src_ip,
    dst_ip, af (4 or 6), clock ("hw"/"sw"), seq, status, owl_ns, rtt_ns, tx_ns,
    rx_ns, and error (only for send_failed)
"""

from __future__ import annotations

import json
import selectors
import socket
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import packets
from .ping import (
    DEFAULT_PORT,
    STATUSES,
    ProbeEngine,
    Result,
    SetupError,
    default_listen,
    open_reflector,
    resolve,
)
from .sinks import Sink, validate_spec
from .sockets import TimestampedSocket, iface_for_addr, source_sockaddr_for


STARTUP_DELAY = 0.2  # seconds between opening the sockets and the first probe (see Daemon.run)


class ConfigError(SetupError):
    """The daemon config is invalid; the message says where."""


# --- Config --------------------------------------------------------------------------------


@dataclass
class PeerConfig:
    addr: str
    label: str


@dataclass
class Config:
    label: str
    peers: List[PeerConfig]
    clock: str = "hw"
    port: int = DEFAULT_PORT
    interval: float = 5.0
    timeout: float = 2.0
    size: int = packets.HEADER_LEN
    ifaces: Optional[List[str]] = None
    listen: Optional[List[str]] = None
    stats_interval: float = 60.0
    sinks: List[Dict[str, Any]] = field(default_factory=lambda: [{"type": "jsonl", "path": "-"}])


_TOP_KEYS = {"label", "peers", "clock", "port", "interval", "timeout", "size", "ifaces", "listen",
             "stats_interval", "sinks"}
_PEER_KEYS = {"addr", "label"}


def _check_keys(obj: dict, allowed: set, where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _number(value: Any, where: str, *, integer: bool = False, low: float = 0, low_ok: bool = False,
            high: Optional[float] = None) -> Any:
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ConfigError(f"{where} must be {'an integer' if integer else 'a number'}")
    if value < low or (value == low and not low_ok) or (high is not None and value > high):
        bound = f"{'>=' if low_ok else '>'} {low}" + (f" and <= {high}" if high is not None else "")
        raise ConfigError(f"{where} must be {bound}")
    return value


def _string_list(value: Any, where: str) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return [_string(v, f"{where}[{i}]") for i, v in enumerate(value)]


def parse_config(obj: Any) -> Config:
    """Validate a decoded JSON config. Raises ConfigError naming the bad field."""
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    _check_keys(obj, _TOP_KEYS, "config")
    if "label" not in obj or "peers" not in obj:
        raise ConfigError('config needs "label" (this node) and "peers"')

    peers_in = obj["peers"]
    if not isinstance(peers_in, list) or not peers_in:
        raise ConfigError("peers must be a non-empty list")
    peers: List[PeerConfig] = []
    seen = set()
    for i, p in enumerate(peers_in):
        where = f"peers[{i}]"
        if not isinstance(p, dict):
            raise ConfigError(f'{where} must be an object like {{"addr": "10.0.0.2", "label": "SITE"}}')
        _check_keys(p, _PEER_KEYS, where)
        addr = _string(p.get("addr"), f"{where}.addr")
        if addr in seen:
            raise ConfigError(f"{where}.addr: {addr} is listed twice")
        seen.add(addr)
        peers.append(PeerConfig(addr, _string(p.get("label", addr), f"{where}.label")))

    cfg = Config(label=_string(obj["label"], "label"), peers=peers)
    if "clock" in obj:
        if obj["clock"] not in ("hw", "sw"):
            raise ConfigError('clock must be "hw" or "sw"')
        cfg.clock = obj["clock"]
    if "port" in obj:
        cfg.port = _number(obj["port"], "port", integer=True, low=1, low_ok=True, high=65535)
    for key in ("interval", "timeout", "stats_interval"):
        if key in obj:
            setattr(cfg, key, float(_number(obj[key], key)))
    if "size" in obj:
        cfg.size = _number(obj["size"], "size", integer=True, low=packets.HEADER_LEN, low_ok=True,
                           high=packets.MAX_PROBE_LEN)
    if "ifaces" in obj:
        cfg.ifaces = _string_list(obj["ifaces"], "ifaces")
    if "listen" in obj:
        cfg.listen = _string_list(obj["listen"], "listen")
    if "sinks" in obj:
        sinks = obj["sinks"]
        if not isinstance(sinks, list) or not sinks:
            raise ConfigError("sinks must be a non-empty list")
        for i, s in enumerate(sinks):
            try:
                validate_spec(s)
            except ValueError as e:
                raise ConfigError(f"sinks[{i}]: {e}") from None
        cfg.sinks = sinks
    return cfg


def load_config(path: str) -> Config:
    try:
        with open(path) as f:
            obj = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from None
    return parse_config(obj)


# --- Daemon --------------------------------------------------------------------------------


class _Peer:
    __slots__ = ("label", "addr", "family", "sockaddr", "src_ip", "next_send")

    def __init__(self, label: str, addr: str, family: int, sockaddr: tuple, src_ip: str):
        self.label = label
        self.addr = addr
        self.family = family
        self.sockaddr = sockaddr
        self.src_ip = src_ip
        self.next_send = 0.0


def log_stderr(msg: str) -> None:
    print(f"owl daemon: {msg}", file=sys.stderr, flush=True)


class Daemon:
    """The daemon's sockets and loop. Construct, then ``run()``; ``close()`` when done."""

    def __init__(self, cfg: Config, sinks: List[Sink], log: Callable[[str], None] = log_stderr) -> None:
        self.cfg = cfg
        self.sinks = sinks
        self.log = log
        self.hw = cfg.clock == "hw"
        self.counters: Counter = Counter()
        self.started = time.monotonic()
        self.peers = [self._resolve_peer(p) for p in cfg.peers]

        self.ifaces: List[str] = []
        if self.hw:
            self.ifaces = list(cfg.ifaces or self._discover_ifaces())
            if not self.ifaces:
                raise SetupError('cannot find the interfaces that reach the peers; set "ifaces" in the config')

        if cfg.listen:
            binds = [resolve(a, cfg.port) for a in cfg.listen]
        else:
            binds = default_listen(cfg.port)
        # The reflector also enables NIC timestamping on self.ifaces, which the engines rely on.
        self.reflector = open_reflector(binds, self.ifaces, defaulted=not cfg.listen)

        self.engines: Dict[int, ProbeEngine] = {}
        try:
            for family in sorted({p.family for p in self.peers}):
                wildcard = ("0.0.0.0", 0) if family == socket.AF_INET else ("::", 0, 0, 0)
                sock = TimestampedSocket(family, wildcard, tx_timestamps=True, hw=self.hw)
                self.engines[family] = ProbeEngine(sock, hw=self.hw, timeout=cfg.timeout, size=cfg.size)
        except BaseException:
            self.close()
            raise

    def _resolve_peer(self, p: PeerConfig) -> _Peer:
        family, sockaddr = resolve(p.addr, self.cfg.port)
        try:
            src_ip = source_sockaddr_for(sockaddr, family)[0]
        except OSError as e:
            # Keep going: the route may come back. Sends fail and are counted until it does.
            self.log(f"warning: no route to {p.label} ({sockaddr[0]}): {e.strerror}")
            src_ip = ""
        return _Peer(p.label, p.addr, family, sockaddr, src_ip)

    def _discover_ifaces(self) -> List[str]:
        found: List[str] = []
        for p in self.peers:
            iface = iface_for_addr(p.src_ip) if p.src_ip else None
            if iface and iface not in found:
                found.append(iface)
        return found

    # -- output --

    def _emit(self, record: Dict[str, Any]) -> None:
        for sink in self.sinks:
            if not sink.wants(record):
                continue
            try:
                sink.emit(record)
            except Exception as e:  # a sink must never stop measurement
                sink.dropped += 1
                if not self.counters["sink_exceptions"]:
                    self.log(f"warning: sink {sink.describe()} failed: {e!r}")
                self.counters["sink_exceptions"] += 1

    def _emit_result(self, peer: _Peer, r: Result) -> None:
        self.counters[r.status] += 1
        record = {
            "type": "probe",
            "time_ns": r.sent_unix_ns,
            "src_label": self.cfg.label,
            "dst_label": peer.label,
            "src_ip": peer.src_ip,
            "dst_ip": peer.sockaddr[0],
            "af": 6 if peer.family == socket.AF_INET6 else 4,
            "clock": self.cfg.clock,
            "seq": r.seq,
            "status": r.status,
            "owl_ns": r.owl_ns,
            "rtt_ns": r.rtt_ns,
            "tx_ns": r.tx_ns,
            "rx_ns": r.rx_ns,
        }
        if r.error:
            record["error"] = r.error
        self._emit(record)

    def stats(self) -> Dict[str, Any]:
        """Cumulative counters since start."""
        c = self.counters
        return {
            "type": "stats",
            "time_ns": time.time_ns(),
            "src_label": self.cfg.label,
            "uptime_s": round(time.monotonic() - self.started, 1),
            "sent": c["sent"],
            **{s: c[s] for s in STATUSES},
            "pending": sum(e.pending for e in self.engines.values()),
            "reflected": self.reflector.probes,
            "reflect_errors": self.reflector.errors,
            **self._sink_stats(),
        }

    def _sink_stats(self) -> Dict[str, int]:
        totals: Counter = Counter()
        for s in self.sinks:
            totals.update(s.stats())
        totals.setdefault("sink_dropped", 0)
        return dict(totals)

    def log_stats(self) -> None:
        st = self.stats()
        self._emit(st)
        for s in self.sinks:
            s.flush()
        skip = {"type", "time_ns", "src_label"}
        self.log("stats: " + " ".join(f"{k}={v}" for k, v in st.items() if k not in skip))

    def describe(self) -> str:
        where = ", ".join(f"[{s.sockname[0]}]:{s.sockname[1]}" for s in self.reflector.socks)
        hw = f"clock hw on {', '.join(self.ifaces)}" if self.hw else "clock sw"
        return (f"label {self.cfg.label}, {len(self.peers)} peers every {self.cfg.interval:g} s, {hw}, "
                f"listening on {where}, sinks: {', '.join(s.describe() for s in self.sinks)}")

    # -- loop --

    def run(self, stop: Optional[threading.Event] = None) -> None:
        """Probe and reflect until ``stop`` is set or KeyboardInterrupt (SIGTERM in the CLI)."""
        cfg = self.cfg
        sel = selectors.DefaultSelector()
        for engine in self.engines.values():
            sel.register(engine.fileno(), selectors.EVENT_READ, engine)
        for s in self.reflector.socks:
            sel.register(s.fileno(), selectors.EVENT_READ, s)

        # The first socket on a host to request software RX timestamps makes the kernel turn them
        # on through deferred work (net_enable_timestamp), so packets in the next few ms can arrive
        # unstamped; probing ourselves right away gave an occasional no_rx_ts on seq 0. Wait briefly.
        start = time.monotonic() + STARTUP_DELAY
        for i, p in enumerate(self.peers):
            p.next_send = start + i * cfg.interval / len(self.peers)
        next_stats = start + cfg.stats_interval
        try:
            while stop is None or not stop.is_set():
                now = time.monotonic()
                for p in self.peers:
                    if now >= p.next_send:
                        self.counters["sent"] += 1
                        failed = self.engines[p.family].send(p.sockaddr, p)
                        if failed:
                            self._emit_result(*failed)
                        p.next_send += cfg.interval
                        if p.next_send <= now:  # fell behind (stall): skip the missed rounds, don't burst
                            p.next_send = now + cfg.interval

                for engine in self.engines.values():
                    for peer, r in engine.expire(now):
                        self._emit_result(peer, r)

                if now >= next_stats:
                    self.log_stats()
                    next_stats += cfg.stats_interval
                    if next_stats <= now:
                        next_stats = now + cfg.stats_interval

                wake = [next_stats] + [p.next_send for p in self.peers]
                wake += [d for d in (e.next_deadline() for e in self.engines.values()) if d is not None]
                timeout = min(max(0.0, min(wake) - time.monotonic()), 0.5)  # 0.5 s: notice `stop`
                for key, _ in sel.select(timeout):
                    if isinstance(key.data, ProbeEngine):
                        for peer, r in key.data.handle_readable():
                            self._emit_result(peer, r)
                    else:
                        self.reflector.handle_readable(key.data)
        finally:
            sel.close()

    def close(self) -> None:
        for engine in getattr(self, "engines", {}).values():
            engine.sock.close()
        if getattr(self, "reflector", None) is not None:
            self.reflector.close()
        for s in self.sinks:
            try:
                s.close()
            except Exception:
                pass
