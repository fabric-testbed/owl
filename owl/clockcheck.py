"""Clock-health check: is the clock our timestamps come from synchronized?

owl doesn't synchronize clocks, but a latency is only as good as the two
clocks behind it, so the daemon checks its own every few seconds and marks
its records and reports accordingly (see docs/clock-chain.md).

What is checked against what:

- reference: /dev/ptp_kvm when it exists (a VM: the host's PTP-synchronized
  clock, which both VM clocks are steered from). Otherwise the system clock,
  for hw mode only.
- checked clocks: in hw mode, the PTP hardware clock (/dev/ptpN) of each NIC
  that sends probes; in sw mode, the system clock (only possible with ptp_kvm).

A clock is healthy when its offset from the reference is within
``max_offset_ns``. An offset of about 37 s (TAI vs UTC) also counts as healthy,
because many PTP setups keep NIC clocks on TAI; all hosts in a mesh must agree,
which the recorded offset makes visible. Without a usable reference the check is
disabled and state stays unknown (None).

Offsets are measured as the tightest of N reference/clock/reference
clock_gettime triples. Reading a NIC clock crosses PCIe (around a microsecond),
so this resolves offsets to about half a microsecond.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from .sockets import phc_index

PTP_KVM = "/dev/ptp_kvm"
SYSTEM = "CLOCK_REALTIME"
TAI_UTC_NS = 37_000_000_000
CLOCK_REALTIME = time.CLOCK_REALTIME


def _fd_clock_id(fd: int) -> int:
    """FD_TO_CLOCKID from the kernel's posix-timers: a dynamic clock for an open /dev/ptpN."""
    return ((~fd) << 3) | 3


def measure_offset(ref: int, clock: int, samples: int = 100) -> int:
    """Return ref - clock in ns, from the tightest of ``samples`` ref/clock/ref readings."""
    g = time.clock_gettime_ns
    best_width, best = None, 0
    for _ in range(samples):
        a = g(ref)
        c = g(clock)
        b = g(ref)
        if best_width is None or b - a < best_width:
            best_width, best = b - a, (a + b) // 2 - c
    return best


def healthy(offset_ns: int, max_offset_ns: int) -> bool:
    return abs(offset_ns) <= max_offset_ns or abs(abs(offset_ns) - TAI_UTC_NS) <= max_offset_ns


class ClockGuard:
    """Measures the timestamp clocks against the reference; ``ok`` is the latest verdict.

    ``ok`` is None when the check is disabled or hasn't run, True when every
    checked clock is within ``max_offset_ns``, False otherwise. ``offsets``
    holds the latest ref - clock offset of each checked clock, by name.
    """

    def __init__(
        self,
        hw: bool,
        ifaces: List[str],
        *,
        max_offset_ns: int = 10_000,
        log: Callable[[str], None] = lambda m: None,
        measure: Callable[[int, int], int] = measure_offset,
    ) -> None:
        self.max_offset_ns = max_offset_ns
        self.log = log
        self._measure = measure
        self.ok: Optional[bool] = None
        self.offsets: Dict[str, int] = {}
        self.disabled_reason = ""
        self._fds: List[int] = []
        self.reference: Optional[Tuple[str, int]] = None
        self.targets: List[Tuple[str, int]] = []
        try:
            self._setup(hw, ifaces)
        except BaseException:
            self.close()
            raise
        if not self.reference or not self.targets:
            self.disabled_reason = self.disabled_reason or "nothing to check"
            self.log(f"clock check disabled: {self.disabled_reason}")

    def _open(self, path: str) -> int:
        fd = os.open(path, os.O_RDONLY)
        self._fds.append(fd)
        return _fd_clock_id(fd)

    def _setup(self, hw: bool, ifaces: List[str]) -> None:
        if os.path.exists(PTP_KVM):
            try:
                self.reference = (PTP_KVM, self._open(PTP_KVM))
            except OSError as e:
                self.disabled_reason = f"cannot open {PTP_KVM}: {e.strerror}"
                return
        elif hw:
            self.reference = (SYSTEM, CLOCK_REALTIME)
        else:
            self.disabled_reason = f"sw mode needs {PTP_KVM} as a reference"
            return

        if not hw:
            self.targets = [(SYSTEM, CLOCK_REALTIME)]
            return
        seen = set()
        for iface in ifaces:
            try:
                idx = phc_index(iface)
            except OSError as e:
                self.log(f"clock check: cannot query {iface}'s hardware clock: {e.strerror}")
                continue
            if idx is None:
                self.log(f"clock check: {iface} has no PTP hardware clock")
                continue
            path = f"/dev/ptp{idx}"
            if path in seen:
                continue
            seen.add(path)
            try:
                self.targets.append((f"{path} ({iface})", self._open(path)))
            except OSError as e:
                self.log(f"clock check: cannot open {path}: {e.strerror}")
        if not self.targets:
            self.disabled_reason = "no NIC hardware clock could be opened"

    @property
    def enabled(self) -> bool:
        return bool(self.reference and self.targets)

    def describe(self) -> str:
        if not self.enabled:
            return f"clock check off ({self.disabled_reason})"
        names = ", ".join(n for n, _ in self.targets)
        return f"clock check: {names} vs {self.reference[0]}, max offset {self.max_offset_ns / 1000:g} µs"

    def check(self) -> Optional[bool]:
        """Measure every checked clock now; update and return ``ok``. Logs changes of state."""
        if not self.enabled:
            return None
        bad = []
        for name, clk in self.targets:
            try:
                off = self._measure(self.reference[1], clk)
            except OSError as e:
                off = None
                bad.append(f"{name}: cannot read ({e.strerror})")
            if off is not None:
                self.offsets[name] = off
                if not healthy(off, self.max_offset_ns):
                    bad.append(f"{name} is {off / 1e6:+.3f} ms from {self.reference[0]}")
        ok = not bad
        if ok != self.ok:
            if ok:
                worst = max(abs(o) for o in self.offsets.values())
                self.log(f"clock check: OK (worst offset {worst / 1000:.1f} µs from {self.reference[0]})")
            else:
                self.log("clock check: NOT SYNCHRONIZED, records marked ptp_ok=false: " + "; ".join(bad))
        self.ok = ok
        return ok

    def worst_offset_ns(self) -> Optional[int]:
        """The checked clock offset furthest from healthy (signed), or None."""
        if not self.offsets:
            return None
        def badness(o: int) -> int:
            return min(abs(o), abs(abs(o) - TAI_UTC_NS))
        return max(self.offsets.values(), key=badness)

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
