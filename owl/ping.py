"""Probe sending and reflecting: the measurement loops behind `owl ping`, `owl reflect` and `owl daemon`.

ProbeEngine sends probes on one socket and matches three things per probe: its
own TX timestamp (from the error queue), the reflector's report carrying the RX
timestamp, and the local arrival time of that report. These can arrive in any
order, so each probe stays pending until all are in or it times out. One engine
can probe any number of peers (the daemon); Pinger drives one engine against a
single peer (`owl ping`).

    one-way latency = reflector RX timestamp - our TX timestamp
    rtt             = report arrival here - our TX timestamp
                      (includes the reflector's turnaround time; a sanity check only)
"""

from __future__ import annotations

import errno
import os
import selectors
import socket
import statistics
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from . import packets
from .sockets import (
    TimestampedSocket,
    TxToken,
    enable_hw_timestamping,
    iface_for_addr,
    source_sockaddr_for,
)

DEFAULT_PORT = 49005

# Result.status values
OK = "ok"
LOST = "lost"  # no report before the timeout
NO_TX_TS = "no_tx_ts"  # the local kernel/NIC never produced a TX timestamp
NO_RX_TS = "no_rx_ts"  # the reflector had no RX timestamp of the requested kind
SEND_FAILED = "send_failed"
STATUSES = (OK, LOST, NO_TX_TS, NO_RX_TS, SEND_FAILED)


class SetupError(Exception):
    """A configuration problem the user can fix; the message says how."""


class Result(NamedTuple):
    seq: int
    status: str
    sent_unix_ns: int  # wall-clock time the probe was sent (for humans and logs)
    tx_ns: int = 0
    rx_ns: int = 0
    owl_ns: Optional[int] = None
    rtt_ns: Optional[int] = None
    error: str = ""
    # The reflector's own clock check, from its report: None = it doesn't check (or no report).
    remote_clock_ok: Optional[bool] = None


def remote_clock_ok(flags: int) -> Optional[bool]:
    if not flags & packets.FLAG_CLOCK_CHECKED:
        return None
    return not flags & packets.FLAG_CLOCK_BAD


def resolve(host: str, port: int, family: int = 0) -> Tuple[int, tuple]:
    """Resolve ``host`` to (address family, sockaddr)."""
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    except socket.gaierror as e:
        raise SetupError(f"cannot resolve {host}: {e.strerror}") from None
    fam, _type, _proto, _canon, sockaddr = infos[0]
    return fam, sockaddr


def setup_hw_timestamping(iface: str) -> None:
    """Enable NIC timestamping, turning the usual failures into actionable messages."""
    try:
        enable_hw_timestamping(iface)
    except PermissionError:
        raise SetupError(
            "enabling NIC hardware timestamping needs root or CAP_NET_ADMIN "
            "(run with sudo, or use --clock sw)"
        ) from None
    except OSError as e:
        raise SetupError(
            f"interface {iface} refused hardware timestamping ({e.strerror}); "
            "use --clock sw"
        ) from None


# --- Sending probes ---------------------------------------------------------------------


class _Pending:
    __slots__ = ("seq", "target", "deadline", "sent_unix_ns", "token", "tx_ns", "report", "report_arrival_ns")

    def __init__(self, seq: int, target: Any, deadline: float, sent_unix_ns: int, token: Optional[TxToken]):
        self.seq = seq
        self.target = target
        self.deadline = deadline
        self.sent_unix_ns = sent_unix_ns
        self.token = token
        self.tx_ns: Optional[int] = None  # None = not yet seen, 0 = seen but empty
        self.report: Optional[packets.Report] = None
        self.report_arrival_ns = 0


Completed = Tuple[Any, Result]  # (target passed to send(), its result)


class ProbeEngine:
    """Sends probes on one TimestampedSocket and turns each into exactly one Result.

    ``send()`` takes an opaque ``target`` (e.g. a peer object) that comes back
    with the probe's Result, so one engine can serve many peers. Sequence
    numbers are per engine, and ``session`` is random per engine, so reports
    for another engine or an earlier run are ignored.

    The caller owns the event loop: call ``handle_readable()`` when the socket is
    readable and ``expire()`` at or after ``next_deadline()``.
    """

    def __init__(self, sock: TimestampedSocket, *, hw: bool, timeout: float, size: int = 0) -> None:
        self.sock = sock
        self.hw = hw
        self.timeout = timeout
        self.size = size
        self.session = int.from_bytes(os.urandom(4), "big")
        self._flags = packets.FLAG_HW if hw else 0
        self._next_seq = 0
        self._pending: Dict[int, _Pending] = {}
        self._by_token: Dict[TxToken, int] = {}

    def fileno(self) -> int:
        return self.sock.fileno()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def next_deadline(self) -> Optional[float]:
        return min((p.deadline for p in self._pending.values()), default=None)

    def _pick(self, ts: Optional[packets.Timestamps]) -> int:
        if ts is None:
            return 0
        return ts.hw_ns if self.hw else ts.sw_ns

    def send(self, sockaddr: tuple, target: Any = None) -> Optional[Completed]:
        """Send one probe. Returns its (target, Result) at once only if sendto() failed."""
        seq = self._next_seq
        self._next_seq += 1
        data = packets.pack_probe(self.session, seq, self._flags, self.size)
        sent_unix_ns = time.time_ns()
        try:
            token = self.sock.send(data, sockaddr)
        except OSError as e:
            return target, Result(seq, SEND_FAILED, sent_unix_ns, error=e.strerror or str(e))
        self._pending[seq] = _Pending(seq, target, time.monotonic() + self.timeout, sent_unix_ns, token)
        if token is not None:
            self._by_token[token] = seq
        return None

    def handle_readable(self) -> List[Completed]:
        """Read everything waiting on the socket; return the probes that completed."""
        done: List[Completed] = []
        while True:
            item = self.sock.recv_tx_timestamp()
            if item is None:
                break
            token, ts = item
            seq = self._by_token.pop(token, None) if token is not None else None
            p = self._pending.get(seq) if seq is not None else None
            if p is not None:
                p.tx_ns = self._pick(ts)
                self._finish(p, done)

        while True:
            item = self.sock.recv()
            if item is None:
                break
            data, _addr, ts = item
            msg = packets.unpack(data)
            if not isinstance(msg, packets.Report) or msg.session != self.session:
                continue
            p = self._pending.get(msg.seq)
            if p is None or p.report is not None:
                continue  # late or duplicate
            p.report = msg
            p.report_arrival_ns = self._pick(ts)
            self._finish(p, done)
        return done

    def expire(self, now: Optional[float] = None) -> List[Completed]:
        """Resolve every probe whose timeout has passed."""
        now = time.monotonic() if now is None else now
        done: List[Completed] = []
        for p in [p for p in self._pending.values() if now >= p.deadline]:
            self._finish(p, done, expired=True)
        return done

    def _finish(self, p: _Pending, done: List[Completed], expired: bool = False) -> None:
        """Emit a Result once a probe is complete, or when it times out."""
        if not expired and (p.tx_ns is None or p.report is None):
            return
        del self._pending[p.seq]
        if p.token is not None:
            self._by_token.pop(p.token, None)

        r = p.report
        if r is None:
            result = Result(p.seq, LOST, p.sent_unix_ns, tx_ns=p.tx_ns or 0)
        else:
            remote = remote_clock_ok(r.flags)
            if not p.tx_ns:
                result = Result(p.seq, NO_TX_TS, p.sent_unix_ns, rx_ns=r.rx_ns, remote_clock_ok=remote)
            elif r.rx_ns == 0 or bool(r.flags & packets.FLAG_HW) != self.hw:
                result = Result(p.seq, NO_RX_TS, p.sent_unix_ns, tx_ns=p.tx_ns, remote_clock_ok=remote)
            else:
                rtt = p.report_arrival_ns - p.tx_ns if p.report_arrival_ns else None
                result = Result(p.seq, OK, p.sent_unix_ns, p.tx_ns, r.rx_ns, r.rx_ns - p.tx_ns, rtt,
                                remote_clock_ok=remote)
        done.append((p.target, result))


class Pinger:
    """Probes one peer at a fixed interval (`owl ping`)."""

    def __init__(
        self,
        peer: str,
        *,
        port: int = DEFAULT_PORT,
        family: int = 0,
        count: Optional[int] = None,
        interval: float = 1.0,
        timeout: float = 2.0,
        source: Optional[str] = None,
        iface: Optional[str] = None,
        hw: bool = True,
        size: int = 0,
    ) -> None:
        self.peer = peer
        self.family, self.peer_sockaddr = resolve(peer, port, family)
        self.count = count
        self.interval = interval

        if source:
            _fam, bind_sockaddr = resolve(source, 0, self.family)
        else:
            bind_sockaddr = source_sockaddr_for(self.peer_sockaddr, self.family)
        self.source = bind_sockaddr[0]

        self.iface = iface
        if hw:
            self.iface = iface or iface_for_addr(self.source)
            if not self.iface:
                raise SetupError(f"cannot find the interface that owns {self.source}; pass --iface")
            setup_hw_timestamping(self.iface)

        sock = TimestampedSocket(self.family, bind_sockaddr, tx_timestamps=True, hw=hw)
        self.engine = ProbeEngine(sock, hw=hw, timeout=timeout, size=size)
        self.session = self.engine.session

    def close(self) -> None:
        self.engine.sock.close()

    def results(self) -> Iterator[Result]:
        """Send probes and yield one Result per probe as each completes or times out.

        Runs until ``count`` probes have all been resolved, or forever if count is
        None. Stop early by breaking out of the loop or with KeyboardInterrupt.
        """
        engine = self.engine
        sel = selectors.DefaultSelector()
        sel.register(engine.fileno(), selectors.EVENT_READ)
        sent = 0
        next_send = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                more = self.count is None or sent < self.count
                if more and now >= next_send:
                    failed = engine.send(self.peer_sockaddr)
                    if failed:
                        yield failed[1]
                    sent += 1
                    next_send = max(next_send + self.interval, now)  # don't burst after a stall
                    more = self.count is None or sent < self.count

                for _t, r in engine.expire(now):
                    yield r
                if not more and not engine.pending:
                    return

                wake = [d for d in (engine.next_deadline(), next_send if more else None) if d is not None]
                if sel.select(max(0.0, min(wake) - time.monotonic())):
                    for _t, r in engine.handle_readable():
                        yield r
        finally:
            sel.close()


def summarize(results: Iterable[Result]) -> dict:
    results = list(results)
    ok = [r for r in results if r.status == OK]
    lost = sum(r.status in (LOST, SEND_FAILED) for r in results)
    summary = {
        "sent": len(results),
        "ok": len(ok),
        "lost": lost,
        "invalid": len(results) - len(ok) - lost,
        "loss_pct": 100.0 * lost / len(results) if results else 0.0,
    }
    for name, values in (
        ("owl", [r.owl_ns for r in ok]),
        ("rtt", [r.rtt_ns for r in ok if r.rtt_ns is not None]),
    ):
        if values:
            summary[name] = {
                "min_ns": min(values),
                "avg_ns": round(statistics.fmean(values)),
                "max_ns": max(values),
                "mdev_ns": round(statistics.pstdev(values)),
            }
    return summary


# --- Reflecting probes ---------------------------------------------------------------------


class Reflector:
    """Answers probes with a report carrying the probe's RX timestamp.

    Listens on one or more sockets (by default all IPv4 and IPv6 addresses).
    Reports go back to the probe's source address and port. Use ``serve()`` for
    a standalone loop, or register ``socks`` in your own selector and call
    ``handle_readable(sock)`` (the daemon does this).
    """

    def __init__(self, binds: List[Tuple[int, tuple]], ifaces: Iterable[str] = ()) -> None:
        for iface in ifaces:
            setup_hw_timestamping(iface)
        self.socks: List[TimestampedSocket] = []
        try:
            for family, sockaddr in binds:
                self.socks.append(TimestampedSocket(family, sockaddr))
        except BaseException:
            self.close()
            raise
        self.probes = 0
        self.errors = 0
        # OR'd into every report: FLAG_CLOCK_CHECKED [| FLAG_CLOCK_BAD], set by the daemon's clock check.
        self.clock_flags = 0

    def close(self) -> None:
        for s in self.socks:
            s.close()

    def serve(
        self,
        stop: Optional[threading.Event] = None,
        on_probe: Optional[Callable[[tuple, packets.Probe, int], None]] = None,
    ) -> None:
        """Reflect until ``stop`` is set (or forever). ``on_probe(addr, probe, rx_ns)`` is for logging."""
        sel = selectors.DefaultSelector()
        for s in self.socks:
            sel.register(s.fileno(), selectors.EVENT_READ, s)
        try:
            while stop is None or not stop.is_set():
                for key, _ in sel.select(0.5):
                    self.handle_readable(key.data, on_probe)
        finally:
            sel.close()

    def handle_readable(self, s: TimestampedSocket, on_probe=None) -> None:
        """Answer every probe waiting on ``s``."""
        while True:
            item = s.recv()
            if item is None:
                return
            data, addr, ts = item
            msg = packets.unpack(data)
            if not isinstance(msg, packets.Probe):
                continue
            want_hw = bool(msg.flags & packets.FLAG_HW)
            rx_ns = 0 if ts is None else (ts.hw_ns if want_hw else ts.sw_ns)
            self.probes += 1
            try:
                flags = (msg.flags & packets.FLAG_HW) | self.clock_flags
                s.send(packets.pack_report(msg.session, msg.seq, flags, rx_ns), addr)
            except OSError:
                self.errors += 1
                continue
            if on_probe:
                on_probe(addr, msg, rx_ns)


def default_listen(port: int) -> List[Tuple[int, tuple]]:
    """Wildcard binds on ``port`` for IPv4 and, if available, IPv6."""
    binds = [(socket.AF_INET, ("0.0.0.0", port))]
    if socket.has_ipv6:
        binds.append((socket.AF_INET6, ("::", port, 0, 0)))
    return binds


def open_reflector(binds: List[Tuple[int, tuple]], ifaces: Iterable[str], *, defaulted: bool) -> Reflector:
    """Open a Reflector; if the default binds were used and IPv6 is unavailable, fall back to IPv4."""
    ifaces = list(ifaces)
    try:
        return Reflector(binds, ifaces)
    except OSError as e:
        if defaulted and e.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
            return Reflector(binds[:1], ifaces)
        port = binds[0][1][1]
        raise SetupError(f"cannot listen on port {port}: {e.strerror}") from None
