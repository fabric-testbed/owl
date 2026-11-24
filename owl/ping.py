"""Probe sender (Pinger) and reflector: the measurement loops behind `owl ping` and `owl reflect`.

Pinger sends probes from an ephemeral port and matches three things per probe:
its own TX timestamp (from the error queue), the reflector's report carrying
the RX timestamp, and the local arrival time of that report. These can arrive
in any order, so each probe stays pending until all are in or it times out.

    one-way latency = reflector RX timestamp - our TX timestamp
    rtt             = report arrival here - our TX timestamp
                      (includes the reflector's turnaround time; a sanity check only)
"""

from __future__ import annotations

import os
import selectors
import socket
import statistics
import threading
import time
from typing import Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

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


class _Pending:
    __slots__ = ("seq", "deadline", "sent_unix_ns", "token", "tx_ns", "report", "report_arrival_ns")

    def __init__(self, seq: int, deadline: float, sent_unix_ns: int, token: Optional[TxToken]):
        self.seq = seq
        self.deadline = deadline
        self.sent_unix_ns = sent_unix_ns
        self.token = token
        self.tx_ns: Optional[int] = None  # None = not yet seen, 0 = seen but empty
        self.report: Optional[packets.Report] = None
        self.report_arrival_ns = 0


class Pinger:
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
        self.timeout = timeout
        self.hw = hw
        self.size = size
        self.session = int.from_bytes(os.urandom(4), "big")
        self._flags = packets.FLAG_HW if hw else 0

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

        self.sock = TimestampedSocket(self.family, bind_sockaddr, tx_timestamps=True, hw=hw)

    def close(self) -> None:
        self.sock.close()

    def _pick(self, ts: Optional[packets.Timestamps]) -> int:
        if ts is None:
            return 0
        return ts.hw_ns if self.hw else ts.sw_ns

    def results(self) -> Iterator[Result]:
        """Send probes and yield one Result per probe as each completes or times out.

        Runs until ``count`` probes have all been resolved, or forever if count is
        None. Stop early by breaking out of the loop or with KeyboardInterrupt.
        """
        sel = selectors.DefaultSelector()
        sel.register(self.sock.fileno(), selectors.EVENT_READ)
        pending: Dict[int, _Pending] = {}
        by_token: Dict[TxToken, int] = {}
        done: List[Result] = []
        seq = 0
        next_send = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                more = self.count is None or seq < self.count
                if more and now >= next_send:
                    self._send_probe(seq, pending, by_token, done)
                    seq += 1
                    next_send = max(next_send + self.interval, now)  # don't burst after a stall
                    more = self.count is None or seq < self.count

                for p in [p for p in pending.values() if now >= p.deadline]:
                    self._finish(p, pending, by_token, done, expired=True)

                yield from done
                done.clear()
                if not more and not pending:
                    return

                wake = [p.deadline for p in pending.values()]
                if more:
                    wake.append(next_send)
                if sel.select(max(0.0, min(wake) - time.monotonic())):
                    self._drain(pending, by_token, done)
                yield from done
                done.clear()
        finally:
            sel.close()

    def _send_probe(self, seq, pending, by_token, done) -> None:
        data = packets.pack_probe(self.session, seq, self._flags, self.size)
        sent_unix_ns = time.time_ns()
        try:
            token = self.sock.send(data, self.peer_sockaddr)
        except OSError as e:
            done.append(Result(seq, SEND_FAILED, sent_unix_ns, error=e.strerror or str(e)))
            return
        pending[seq] = _Pending(seq, time.monotonic() + self.timeout, sent_unix_ns, token)
        if token is not None:
            by_token[token] = seq

    def _drain(self, pending, by_token, done) -> None:
        while True:
            item = self.sock.recv_tx_timestamp()
            if item is None:
                break
            token, ts = item
            seq = by_token.pop(token, None) if token is not None else None
            p = pending.get(seq) if seq is not None else None
            if p is not None:
                p.tx_ns = self._pick(ts)
                self._finish(p, pending, by_token, done)

        while True:
            item = self.sock.recv()
            if item is None:
                break
            data, _addr, ts = item
            msg = packets.unpack(data)
            if not isinstance(msg, packets.Report) or msg.session != self.session:
                continue
            p = pending.get(msg.seq)
            if p is None or p.report is not None:
                continue  # late or duplicate
            p.report = msg
            p.report_arrival_ns = self._pick(ts)
            self._finish(p, pending, by_token, done)

    def _finish(self, p: _Pending, pending, by_token, done, expired: bool = False) -> None:
        """Emit a Result once a probe is complete, or when it times out."""
        if not expired and (p.tx_ns is None or p.report is None):
            return
        del pending[p.seq]
        if p.token is not None:
            by_token.pop(p.token, None)

        r = p.report
        if r is None:
            done.append(Result(p.seq, LOST, p.sent_unix_ns, tx_ns=p.tx_ns or 0))
        elif not p.tx_ns:
            done.append(Result(p.seq, NO_TX_TS, p.sent_unix_ns, rx_ns=r.rx_ns))
        elif r.rx_ns == 0 or bool(r.flags & packets.FLAG_HW) != self.hw:
            done.append(Result(p.seq, NO_RX_TS, p.sent_unix_ns, tx_ns=p.tx_ns))
        else:
            rtt = p.report_arrival_ns - p.tx_ns if p.report_arrival_ns else None
            done.append(
                Result(p.seq, OK, p.sent_unix_ns, p.tx_ns, r.rx_ns, r.rx_ns - p.tx_ns, rtt)
            )


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


class Reflector:
    """Answers probes with a report carrying the probe's RX timestamp.

    Listens on one or more sockets (by default all IPv4 and IPv6 addresses).
    Reports go back to the probe's source address and port.
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
                    self._drain(key.data, on_probe)
        finally:
            sel.close()

    def _drain(self, s: TimestampedSocket, on_probe) -> None:
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
                s.send(packets.pack_report(msg.session, msg.seq, msg.flags & packets.FLAG_HW, rx_ns), addr)
            except OSError:
                self.errors += 1
                continue
            if on_probe:
                on_probe(addr, msg, rx_ns)
