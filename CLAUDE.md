# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Redesign

This project is being rewritten as **owl2** (v2.0, developed on the `owl2` branch; the last v1 commit is tagged `v1-final`). The architecture, decisions, and step-by-step implementation checklist are in **`.claude/owl2-plan.md`** — read that first before making any changes. The checklist at the bottom of that file tracks which steps are done and what comes next.

`.claude/owl-redesign-plan.md` (2026-04) is superseded as the top-level plan but remains background on the measurement core. Where the two conflict, `owl2-plan.md` and the code win (its `SOF_TIMESTAMPING_*` bit values are wrong; use `owl/sockets.py`).

## Project Overview

**OWL** (One-Way Latency) measures one-way network latency between Linux hosts, like `ping` but one-way. Probes are timestamped by the NIC (`--clock hw`) or kernel (`--clock sw`) via `SO_TIMESTAMPING`; accuracy depends on PTP-synchronized clocks on both ends, which owl does not manage. Built for the FABRIC testbed but deliberately FABRIC-agnostic — FABRIC-specific deployment, InfluxDB schema, API and dashboard live in the separate latency-dashboard repo.

Hard constraints: **Linux only, Python ≥ 3.9, standard library only** (no third-party runtime dependencies — don't add any). Keep 3.9 compatibility: no `match`, no runtime `X | Y` unions (annotations are fine with `from __future__ import annotations`).

## Commands

```bash
pip install .                      # installs the `owl` console script
python3 -m owl ping --help         # or run without installing

# Tests: packets tests run anywhere; loopback tests need Linux.
python3 -m unittest discover -s tests -v
# On macOS, run the full suite in a Linux container:
docker run --rm --sysctl net.ipv6.conf.all.disable_ipv6=0 -v "$PWD":/src:ro -w /src \
  python:3.9-slim python -B -m unittest discover -s tests -v
```

Docker is only a local test harness; owl itself is not deployed in Docker. Hardware timestamps can only be verified on real NICs (e.g. FABRIC VMs at PTP sites). No linter is configured.

## Architecture

```
owl/
├── packets.py   # wire format (16-byte probe, 24-byte report) + SCM_TIMESTAMPING / sock_extended_err parsing; pure, no Linux deps
├── sockets.py   # TimestampedSocket (SO_TIMESTAMPING, OPT_ID TX-timestamp tokens), SIOCSHWTSTAMP, iface lookup via `ip -j addr`
├── ping.py      # ProbeEngine (send + match TX ts/report → Result), Pinger (one peer), Reflector, summarize()
├── daemon.py    # `owl daemon`: JSON config (parse_config), Daemon selector loop (reflector + one engine per family), counters, per-peer summary windows
├── clockcheck.py # ClockGuard: NIC PHC (hw) or system clock (sw) vs /dev/ptp_kvm; verdict → record fields + report flags
├── sinks.py     # record sinks: JsonlSink, HttpLineProtocolSink (background sender thread, bounded buffer, retry/backoff), to_line_protocol(), validate_spec()/make_sink()
├── cli.py       # argparse: `owl ping`, `owl reflect`, `owl daemon`
└── __main__.py  # python -m owl
tests/           # unittest: test_packets.py, test_config.py, test_sinks.py, test_clockcheck.py (portable); test_loopback.py, test_daemon.py (Linux)
examples/        # owl-daemon.json example config
docs/            # clock-chain.md: which clocks a measurement depends on (FABRIC host/ptp_kvm/VF PHC), failure modes, test results; keep it current when clock handling changes
dev/             # FABRIC validation notebooks (not part of the package), e.g. the two-node hw test
```

### Key design points

- **One-way latency** = reflector's RX timestamp (carried back in the report) − sender's TX timestamp. The report's own travel time doesn't enter the calculation. `rtt` is printed only as a sanity check (includes reflector turnaround).
- **TX timestamp matching**: `SOF_TIMESTAMPING_OPT_ID | OPT_TSONLY`. `TimestampedSocket.send()` returns a `(generation, key)` token; error-queue entries come back with the same token. On a failed `sendto()` the socket resets the kernel counter (toggle SO_TIMESTAMPING) and bumps the generation, so tokens can never be off by one.
- **ProbeEngine** keeps each probe pending until it has both the TX timestamp and the report (either order), or the timeout expires. Statuses: `ok`, `lost`, `no_tx_ts`, `no_rx_ts`, `send_failed`. `send(sockaddr, target)` takes an opaque target that comes back with the Result, so one engine serves many peers; the caller owns the event loop (`handle_readable`, `expire`, `next_deadline`).
- **Daemon**: one thread, one selector. Reflector sockets on the listen port plus one wildcard-bound ProbeEngine socket per address family (ephemeral port). Peers are staggered across `interval`; missed rounds after a stall are skipped, not burst. Probing starts 0.2 s after the sockets open (kernel enables sw RX timestamps asynchronously). Sinks must never raise; drops are counted. Config validation rejects unknown keys.
- **Reflector** replies to the probe's source address/port, and returns the timestamp kind the probe asked for (`FLAG_HW`) — `rx_ns=0` if unavailable, never a substitute. In the daemon it also sets `FLAG_CLOCK_CHECKED`/`FLAG_CLOCK_BAD` on reports from its clock check, which become `Result.remote_clock_ok`.
- **session** (random u32 per sender run) filters stale or foreign reports.
- **`--clock sw`** TX timestamps are `SOF_TIMESTAMPING_TX_SCHED` (taken at queueing, works on every driver), not `TX_SOFTWARE`, which ConnectX VFs on FABRIC never produce.
- **`--clock hw`** calls `SIOCSHWTSTAMP` (TX on, RX filter ALL) — NIC-wide, needs `CAP_NET_ADMIN`. HW timestamps are in the NIC's PHC timebase (often TAI); SW timestamps are CLOCK_REALTIME. Never mix.
- Default port **49005**. `owl ping` uses an ephemeral source port.
- SIGTERM is treated like Ctrl-C (clean shutdown with summary).
