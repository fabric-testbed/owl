# OWL Simplified Daemon — Design Plan

**Created:** 2026-04-22
**Last updated:** 2026-04-22

## Context

The current design runs one Docker container per sender-destination pair (400 containers for 20 nodes) and routes data through a fragile `tail -f | tcpdump` subprocess chain with regex parsing. The goal is a single daemon per node that works like a hardware-timestamped ping: one probe out, one report back, latency computed entirely on the sender and written to a local CSV file.

---

## Core Concept

```
Sender                                  Receiver
  │                                         │
  ├─── probe {seq_n} ──────────────────────►│
  │    poll error queue → TX_hw_ns          │  SO_TIMESTAMPING → RX_hw_ns
  │    store {seq_n: TX_hw_ns}              │
  │                                         │
  │◄─────────── report {seq_n, RX_hw_ns} ───┤
  │                                         │
  ├── lookup TX_hw_ns[seq_n]               │
  ├── latency = RX_hw_ns − TX_hw_ns        │
  └── append row to CSV                    │
```

- `TX_hw_ns`: NIC hardware timestamp when probe leaves sender (`MSG_ERRQUEUE`)
- `RX_hw_ns`: NIC hardware timestamp when probe arrives at receiver (`SO_TIMESTAMPING` ancdata)
- Both are raw PTP clock values (`SOF_TIMESTAMPING_RAW_HARDWARE`) — directly comparable across nodes synced to the same grandmaster
- The report's own travel time does not enter the latency calculation
- The receiver is **stateless** — no storage, no database connection

---

## What Gets Eliminated

| Current | New |
|---|---|
| 400 Docker containers for 20 nodes | one process per node, no Docker |
| `tail -f \| tcpdump` subprocess chain | Plain UDP socket with `SO_TIMESTAMPING` |
| Rotating PCAP files | No intermediate files |
| Regex ASCII packet parsing | `struct.unpack` on fixed-format payloads |
| Separate `send_data.py` process | Moved into optional shipper layer (decoupled from measurement path) |
| C extension `ptp_time.c` + `phc2sys` grep | Kernel `SO_TIMESTAMPING` API |
| All heavy deps (scapy, pandas, numpy, pyarrow, reactivex) | Removed. InfluxDB clients kept only as an optional `shipper` extra |

---

## Packet Format

```
Probe  (sender → receiver):  [0x01][seq_n: uint64 BE]                    =  9 bytes
Report (receiver → sender):  [0x02][seq_n: uint64 BE][rx_hw_ns: uint64 BE] = 17 bytes
```

Both packet types travel on the same port (default 49005 for `owl-node`, 49006 for `owl-ping`). The type byte lets the receiver loop distinguish probes from reports.

---

## Architecture

Each node runs **one daemon** with one UDP socket bound to `node_ip:49005`. A single thread drives a `selectors` loop with two event sources:

- a periodic timer that sends one round of probes (one probe to each peer);
- `EVENT_READ` on the socket for incoming packets — probes from peers (which we reply to) and reports that answer probes we sent.

TX hardware timestamps are drained from the socket's error queue (`MSG_ERRQUEUE`) immediately after each probe is sent, so the `tx_record` entry is complete before the matching report can come back.

### Event Loop
```
sock.setblocking(False)                                # non-blocking; see note on drain_errqueue below

sel = selectors.DefaultSelector()
sel.register(sock, selectors.EVENT_READ)

next_probe = time.monotonic() + interval_sec
seq_n      = 0
tx_record  = {}   # seq_n → (peer_ip, tx_hw_ns)

while True:
    timeout = max(0, next_probe - time.monotonic())

    for _ in sel.select(timeout):
        drain_errqueue(sock)                           # discard stray TX timestamps; see note below

        try:
            data, ancdata, _, addr = sock.recvmsg(64, 1024)
        except BlockingIOError:
            continue                                   # woke on POLLERR only; drain_errqueue handled it
        hw_ts = parse_scm_timestamping(ancdata)        # ts[2] = raw HW clock

        if data[0] == PROBE:                           # incoming probe: reply
            seq = struct.unpack('!Q', data[1:9])[0]
            if not hw_ts:                              # zero → HW timestamp missing; skip report
                log.warning("rx hw_ts=0 for probe seq=%d from %s; dropping", seq, addr[0])
                rx_zero_counter += 1
            else:
                sock.sendto(struct.pack('!BQQ', REPORT, seq, hw_ts), (addr[0], port))

        elif data[0] == REPORT:                        # reply to one of ours
            seq, rx_hw_ns = struct.unpack('!QQ', data[1:17])
            rec = tx_record.pop(seq, None)
            if rec:
                peer_ip, tx_hw_ns = rec
                if not rx_hw_ns:                       # zero → receiver had no HW timestamp; skip row
                    log.warning("rx_hw_ns=0 in report seq=%d from %s; dropping", seq, peer_ip)
                    rx_zero_counter += 1
                else:
                    latency_ns = rx_hw_ns - tx_hw_ns
                    if latency_ns < 0:                 # negative → PTP sync degraded; write row as diagnostic signal
                        log.warning("negative latency %d ns seq=%d %s→%s; PTP sync may be degraded",
                                    latency_ns, seq, node_ip, peer_ip)
                    csv_writer.writerow(time.time_ns(), node_ip, peer_ip, seq,
                                        tx_hw_ns, rx_hw_ns, latency_ns)

    if time.monotonic() >= next_probe:                 # timer fires
        set_hwtstamp(iface, rx_filter=HWTSTAMP_FILTER_ALL, tx_type=HWTSTAMP_TX_ON)  # re-assert each round; ptp4l restart can reset the filter
        for peer_ip in peers:
            sock.sendto(struct.pack('!BQ', PROBE, seq_n), (peer_ip, port))
            tx_hw_ns = wait_tx_timestamp(sock)         # POLLERR, then MSG_ERRQUEUE; None on timeout
            if tx_hw_ns is not None:                   # skip tx_record entry on send failure
                tx_record[seq_n] = (peer_ip, tx_hw_ns)
            seq_n += 1
        reap_stale(tx_record, older_than=5 * interval_sec)
        next_probe += interval_sec
```

`wait_tx_timestamp(sock)` does a short `select.poll()` for `POLLERR` (small budget, e.g. 50 ms), then reads the `MSG_ERRQUEUE` cmsg and returns the raw-hardware timespec from `SCM_TIMESTAMPING`. Per the kernel's `networking/timestamping.rst`, error-queue reads are always non-blocking — waiting for `POLLERR` first is the correct way to let the NIC finish stamping before we read. On timeout (POLLERR never arrives — NIC didn't produce a TX timestamp, or `set_hwtstamp` wasn't applied), `wait_tx_timestamp` logs a warning, increments a send-failure counter, and returns `None`. The caller skips storing that seq_n in `tx_record` entirely — no report matching it will ever produce a valid row. The probe is treated as a send failure, equivalent to a lost packet.

`drain_errqueue(sock)` non-blockingly pulls and discards every pending entry from the error queue. Because `TX_HARDWARE` is enabled on the socket, *every* `sendto` produces one — including the report replies we send to incoming probes, whose TX timestamps we don't need. Python's `selectors` maps `POLLERR` to `EVENT_READ`, so leftover entries would make `sel.select()` wake up with nothing but error-queue data, and the regular `recvmsg` would then hang (blocking socket) or raise `BlockingIOError` (non-blocking socket). Draining first keeps the regular read path unambiguous. The probe TX timestamps we *do* care about are captured synchronously in the timer branch via `wait_tx_timestamp` and never reach here.

`reap_stale` drops `tx_record` entries whose reports never came back (lost probe, peer daemon down) so the dict stays bounded. At the end of each probe round, a summary log line is emitted with cumulative counters: `probes_sent`, `send_failures` (TX timestamp timeouts), `rx_hw_ts_zero` (receiver had no hardware timestamp), and `probes_reaped` (reports never received). No sidecar file — counters are log-only and inspectable via `journalctl`. Keep the reap threshold at `5 × interval_sec` — tightening it risks falsely reaping entries during the probe-sending stall (see below).

> **Sequential send and the event-loop stall:** during the probe-sending loop, `sel.select()` is not running, so incoming probes from peers sit in the kernel buffer until the loop finishes. This does **not** inflate measured latency — `rx_hw_ns` is stamped by the receiver's NIC when the packet crosses the wire, independently of when the application reads it. The only risk is a dropped measurement: if the total round-trip time (true latency + stall) exceeds the reap window, `reap_stale` discards the `tx_record` entry before the report arrives. At the target load (≥ 30 s interval, ≤ 30 peers) the worst-case stall is ~1.5 s (30 peers × 50 ms POLLERR budget, in practice sub-millisecond per probe) against a 150 s reap window — the risk is negligible. The sequential design is validated for this load.

> **Note for later:** if we ever want to batch sends (send all probes in a round, then drain the error queue once) instead of the send-then-drain-one-at-a-time pattern above, we'll need `SOF_TIMESTAMPING_OPT_ID` so the error-queue cmsg carries a per-socket send counter we can use to correlate each TX timestamp back to its probe. Not needed for the current sequential design.

### Socket Setup
```python
SOF_TIMESTAMPING_TX_HARDWARE  = (1 << 2)
SOF_TIMESTAMPING_RX_HARDWARE  = (1 << 3)
SOF_TIMESTAMPING_RAW_HARDWARE = (1 << 6)
flags = SOF_TIMESTAMPING_TX_HARDWARE | SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE

# Widen the NIC's hardware timestamp filter so plain UDP probes (not just
# PTP event packets) are stamped. See Prerequisite > "Hardware timestamp filter".
# set_hwtstamp issues SIOCSHWTSTAMP, which is a NIC-wide setting — any open
# socket fd works for the ioctl, it is not tied to the measurement socket.
# Called here (before bind) so the filter is in place before the first packet
# can arrive. Also re-asserted at the start of each probe round in case ptp4l
# restarts and resets the filter to its own narrower HWTSTAMP_FILTER_PTP_V2_EVENT.
set_hwtstamp(iface, rx_filter=HWTSTAMP_FILTER_ALL, tx_type=HWTSTAMP_TX_ON)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_TIMESTAMPING, flags)
sock.bind((node_ip, port))
sock.setblocking(False)                                # required: drain_errqueue and recvmsg both rely on non-blocking behaviour
```

`set_hwtstamp()` is a small helper that packs a `struct hwtstamp_config` into a `struct ifreq` and issues `fcntl.ioctl(sock.fileno(), SIOCSHWTSTAMP, ifr)`. The interface name comes from `psutil.net_if_addrs()` — same lookup the current `owl_capturer.py` already does.

---

## CSV Output

Written by the event loop on the **sender node** only. The receiver node writes nothing.

**File path**: configured via `output.path`, e.g. `/owl_output/owl_10.10.1.1.csv`  
**Default**: continuous append to a single file — the shipper depends on stable offset-based tailing and is incompatible with rotation. Rotation (new file per run with a timestamp suffix) is an opt-in config flag for standalone use cases where no shipper is running.  
**Header**:
```
timestamp_ns,sender_ip,receiver_ip,seq_n,tx_hw_ns,rx_hw_ns,latency_ns
```

**Example rows**:
```
1714000000000000000,10.10.1.1,10.10.1.2,1001,1714000000001234567,1714000000002345678,1111111
1714000000000000000,10.10.1.1,10.10.1.3,1002,1714000000001234567,1714000000002456789,1222222
```

`timestamp_ns` is wall-clock time of the measurement (from `time.time_ns()` on sender, for human reference). `tx_hw_ns` and `rx_hw_ns` are the raw PTP hardware clock values; `latency_ns = rx_hw_ns - tx_hw_ns` is the derived one-way latency.

The file is opened once at startup and flushed after each row so data is not lost on crash.

---

## File Layout

```
owl/
├── owl_node.py         # entry point + event loop (daemon mode)
├── cli.py              # entry point + event loop (owl-ping ad-hoc mode)
├── sockets.py          # shared helpers: SO_TIMESTAMPING setup, SIOCSHWTSTAMP, MSG_ERRQUEUE drain
├── packets.py          # pack/unpack helpers, type constants, SCM_TIMESTAMPING parser
├── config.py           # YAML config loading and validation
└── config_example.yaml
```

**Deleted entirely**: `owl_sender.py`, `owl_capturer.py`, `send_data.py`, `pcap_to_csv.py`, `ptp.py`, `ptp_time.c`

---

## Config (`config_example.yaml`)

```yaml
node:
  ip: 10.10.1.1
  port: 49005

peers:
  - 10.10.1.2
  - 10.10.1.3

measurement:
  interval_sec: 1.0

output:
  path: /owl_output/owl.csv   # appended to; filename can include {node_ip}
```

---

## Deployment

Each node runs two processes. Systemd is recommended for production; direct invocation is fine for testing.

### Core daemon

Requires `CAP_NET_ADMIN` for `SIOCSHWTSTAMP` (widening the NIC timestamp filter).

```bash
# Testing — simplest
sudo owl-node /etc/owl/config.yaml

# Production — grant capability once so sudo is not needed at runtime
sudo setcap cap_net_admin+eip $(which owl-node)
owl-node /etc/owl/config.yaml
```

### Shipper

No elevated privileges needed.

```bash
owl-shipper /etc/owl/shipper.yaml
```

### systemd units (recommended)

```ini
# /etc/systemd/system/owl-node.service
[Unit]
Description=OWL measurement daemon
After=network.target ptp4l.service phc2sys.service

[Service]
ExecStart=/usr/local/bin/owl-node /etc/owl/config.yaml
Restart=on-failure
AmbientCapabilities=CAP_NET_ADMIN

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/owl-shipper.service
[Unit]
Description=OWL InfluxDB shipper
After=owl-node.service

[Service]
ExecStart=/usr/local/bin/owl-shipper /etc/owl/shipper.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## CLI (`owl-ping`)

A ping/iperf-style CLI for ad-hoc one-way latency measurement between two nodes — no config file, positional target, stdout output, summary on exit. Intended for debugging, connectivity checks, and casual use; the daemon (`owl-node`) remains the right choice for continuous multi-peer monitoring.

```bash
$ owl-ping 10.10.1.2
owl-ping 10.10.1.2: hardware-timestamped one-way latency (port 49006)
seq=0   latency=0.412 ms   tx_hw=1714000000001234567   rx_hw=1714000000001646567
seq=1   latency=0.408 ms   tx_hw=1714000000002234567   rx_hw=1714000000002642567
seq=2   latency=0.415 ms   tx_hw=1714000000003234567   rx_hw=1714000000003649567
^C
--- owl-ping 10.10.1.2 statistics ---
3 probes, 3 reports, 0% loss
latency min/avg/max/stddev = 0.408/0.412/0.415/0.003 ms
```

### Flags (ping-compatible where sensible)
- `<peer_ip>` — positional, required
- `-c COUNT` — stop after N probes (default: run until SIGINT)
- `-i INTERVAL` — seconds between probes (default: 1.0)
- `-p PORT` — UDP port (default: 49006)
- `-I IFACE_IP` — bind to this local IP (default: first non-loopback)

`owl-ping` defaults to port **49006** (not 49005) so it can run on the same host as `owl-node` without an `EADDRINUSE` conflict. To probe a deployment node's `owl-node` daemon directly, pass `-p 49005`.

### Requires the other end to be listening
`owl-ping` also **answers** incoming probes (reflects reports) on the bound port, so running it on both ends is self-sufficient:

```
hostA$ owl-ping 10.10.1.B
hostB$ owl-ping 10.10.1.A
```

Alternatively, if `owl-node` is already running on the peer (topology deployment), `owl-ping <peer_ip>` from anywhere on the network works on its own.

### Shared code
`cli.py` imports from `packets.py` and `sockets.py` and runs the same single-thread selector loop as `owl_node.py`: timer fires → send probe → wait on `POLLERR` → record `tx_hw_ns`; `EVENT_READ` fires → dispatch on type byte (reply to probes, print + tally reports). No CSV, no YAML.

### Entry points
Declared in `pyproject.toml` (see Dependencies & Packaging):
```toml
[project.scripts]
owl-node    = "owl.owl_node:main"
owl-ping    = "owl.cli:main"
owl-shipper = "owl.shipper:main"   # shipper layer, optional extra
```

---

## Dependencies & Packaging

Switch from `requirements.txt` to **`uv` + `pyproject.toml`** for both layers (one project, two layers declared as extras).

```toml
# pyproject.toml (sketch)
[project]
name = "owl"
requires-python = ">=3.11"
dependencies = [
  "psutil",      # interface lookup
  "pyyaml",      # config
]

[project.optional-dependencies]
shipper = [
  "influxdb_client",     # v2 / on-prem
  "influxdb_client_3",   # v3 / cloud
]

[project.scripts]
owl-node    = "owl.owl_node:main"
owl-ping    = "owl.cli:main"
owl-shipper = "owl.shipper:main"
```

- Dev: `uv sync` (core) or `uv sync --extra shipper` (core + shipper).
- Production: `uv sync --frozen` (core) or `uv sync --frozen --extra shipper` on nodes that also run the shipper.
- `uv.lock` is committed for reproducibility.

**Removed entirely**: scapy, pandas, numpy, pyarrow, reactivex, requests (and the C extension).

---

## Prerequisite

### NIC
Hardware timestamping support. Verified on FABRIC `enp7s0`:
```
hardware-transmit ✓   hardware-receive ✓   hardware-raw-clock ✓
PTP Hardware Clock: 1
```

### PTP daemons
- `ptp4l` must be running and disciplining the NIC PHC to the PTP grandmaster.
- `phc2sys` must be running as `phc2sys -s /dev/ptpN -c CLOCK_REALTIME` (PHC is the source, system clock is the target). This is what the current FABRIC deployment does, and the redesign depends on it: since `ptp4l` locks the PHC to the GM, `SOF_TIMESTAMPING_RAW_HARDWARE` values taken on different nodes are directly comparable. If `phc2sys` were instead run the other direction (system → PHC), the PHC would be free-running relative to the GM and cross-node latency would be meaningless.

### Hardware timestamp filter
On startup the daemon calls `SIOCSHWTSTAMP` on the bound interface with `rx_filter=HWTSTAMP_FILTER_ALL` and `tx_type=HWTSTAMP_TX_ON`. Without this step, `ptp4l`'s narrower filter (typically `HWTSTAMP_FILTER_PTP_V2_EVENT`) would cause the kernel to skip hardware timestamping for OWL's plain UDP probes — `ts[2]` would arrive as zero and no TX cmsg would appear on the error queue. Requires `CAP_NET_ADMIN` (already granted by `--privileged` in the Docker run).

Empirical confirmation that this works on FABRIC: the current `owl_capturer.py` runs `tcpdump -j adapter_unsynced`, which asks libpcap to set exactly this filter configuration, and it succeeds on `enp7s0` without disturbing `ptp4l`.

---

## Why pure Python is enough

All precision-critical work happens in the kernel and on the NIC, not in Python:

- `SOF_TIMESTAMPING_{TX,RX}_HARDWARE` stamps are taken by the NIC as the packet crosses the wire. Python just reads the already-recorded value afterwards — its scheduling latency and GC behavior can delay *when a probe is dispatched*, but not *the accuracy of any measurement*.
- Throughput is trivial: ~19 probes/sec/node at the planned 1 Hz interval; plenty of headroom for higher rates.
- Per message: 9- or 17-byte `struct.unpack`, one dict lookup, one CSV row write. Microseconds.
- The existing C extension (`ptp_time.c`) only exists because the old design reads `/dev/ptp*` via ioctl from userspace. `SO_TIMESTAMPING` supersedes that — the redesign **removes** C, it doesn't add new.

C would start to matter at kernel-bypass (DPDK/AF_XDP) rates, Mpps packet volumes, or sub-microsecond probe pacing — none apply here. All three entry points (`owl-node`, `owl-ping`, `owl-shipper`) are pure Python, no ctypes.

---

## Verification

1. **Two-node smoke test**: run `owl_node.py` on two nodes. After 30s confirm both CSVs have rows for both directed pairs (A→B, B→A), seq_n has no gaps, `latency_ns` values are sub-millisecond for local links.
2. **Sanity check**: `latency_ns` should be roughly `ping RTT / 2`.
3. **20-node test**: one process per node, all 380 directed pairs appear across the 20 CSV files.

---

## InfluxDB Shipper (optional second layer)

The core daemon writes CSV and nothing else. A separate **shipper** process reads the CSV and forwards rows to InfluxDB. This keeps the measurement path free of network dependencies — an InfluxDB outage cannot affect probe scheduling or timestamp capture, and the CSV serves as a durable buffer the shipper replays from.

This layer exists **only for the fixed-topology deployment** that backs the web frontend. A user running OWL as a CLI ping replacement does not need it.

### Responsibilities
- Tail the node's CSV from a checkpointed offset so restarts neither re-ship nor drop rows.
- Batch rows and write them to InfluxDB as points with `sender`/`receiver` tags and `seq_n` / `tx_hw_ns` / `rx_hw_ns` / `latency_ns` fields. Point time = `timestamp_ns`.
- Support InfluxDB v2 (`influxdb_client`) and v3 cloud (`influxdb_client_3`), preserving the existing dual-backend behavior from `send_data.py`.

### Tailing strategy
Plain loop: `seek` to saved offset, `readline` until EOF, sleep briefly, repeat. No `watchdog`/inotify dependency. Offset is persisted to a small file (e.g. `/var/lib/owl/shipper.offset`) after each successful batch. The shipper requires the daemon to run in continuous-append mode (the default) — file rotation is incompatible with offset-based tailing and must not be enabled on nodes running the shipper.

### Batching
Flush on whichever comes first: N rows (e.g. 500) or T seconds (e.g. 10). At 20 nodes × 19 peers × 1/60 s the shipper sees ~6 rows/sec topology-wide, ~0.3/sec per node — batching is mostly about bounding HTTP overhead, not throughput.

### File layout (additions)
```
owl/
├── shipper.py              # entry point: load config, open CSV, run batch/ship loop
├── csv_tailer.py           # offset-checkpointed line reader
├── influx_writer.py        # v2/v3 abstraction, batching, retries
└── shipper_config_example.yaml
```

The core daemon (`owl_node.py`, `sockets.py`, `packets.py`, `config.py`) has no import relationship to the shipper — they only share the CSV format as a contract.

### Config (`shipper_config_example.yaml`)
```yaml
source:
  csv_path: /owl_output/owl.csv
  checkpoint_path: /var/lib/owl/shipper.offset

influxdb:
  type: cloud            # cloud | local
  url: https://...
  token: <token>
  org: <org>
  bucket: <bucket>

# IP → site-code map (used to enrich InfluxDB points; see Site enrichment below)
sites:
  10.10.1.1: SLT
  10.10.1.2: UCSD
  10.10.1.3: STAR
  # ... all nodes in the topology

batch:
  max_rows: 500
  max_seconds: 10
```

### Site name enrichment
Each node's IP maps to a short site code (e.g. `SLT`, `UCSD`) that the frontend wants to show instead of raw IPs. Site codes don't live on the nodes themselves, but the orchestrator that deploys OWL knows the full topology mapping. Putting the map on the **shipper** is the right fit because:

- The core daemon stays IP-only — the CSV contract and its config don't need to change.
- Each shipper needs the *full* map (its own IP for the sender tag plus every peer IP for the receiver tag), so putting site names only in the core daemon's `peers:` list wouldn't cover the receiver side.
- Enrichment sits exactly where the IP→InfluxDB translation already happens.

On each row the shipper looks up both IPs in `sites:` and writes four tags on the InfluxDB point: `sender`, `receiver` (IPs, kept for backward compatibility with the current frontend), `sender_site`, `receiver_site`. An IP missing from the map falls back to tag value `unknown` — rows are never dropped.

If the map gets long or you prefer a single source of truth across all shipper containers, factor it out:

```yaml
# shipper_config.yaml
sites_path: /etc/owl/sites.yaml     # instead of an inline sites: block
```

The orchestrator then writes `sites.yaml` once and mounts it into every shipper. The core daemon is unaffected either way.

### Dependencies (shipper only)
Declared as the `shipper` extra in `pyproject.toml` (see Dependencies & Packaging). `uv sync --extra shipper` pulls in `influxdb_client` + `influxdb_client_3`; core daemon installs stay at `psutil` + `pyyaml`.

### Verification
1. Run the daemon on a node; confirm CSV grows.
2. Start the shipper against that CSV; confirm InfluxDB receives points with correct timestamps and tags.
3. Stop the shipper mid-run, restart: no duplicates, no gap (checkpoint works).
4. Briefly drop InfluxDB connectivity; shipper retries and catches up from its checkpoint without losing rows.

---

## Implementation Plan

Shipper is out of scope for now. Steps proceed from scaffolding → core modules → daemon → two-node test → CLI. Each step ends with a README update and a commit.

- [ ] **1. Project scaffolding** — Add `pyproject.toml`, initialise `uv` (`uv sync`), create `owl/` package skeleton (empty `__init__.py`). Delete old files: `owl_sender.py`, `owl_capturer.py`, `send_data.py`, `pcap_to_csv.py`, `ptp.py`, `ptp_time.c`, `requirements.txt`. Update README to reflect new layout and setup steps. Commit.

- [ ] **2. `packets.py`** — `PROBE`/`REPORT` byte constants, `pack_probe`, `pack_report`, `unpack_probe`, `unpack_report`, `parse_scm_timestamping`. No hardware required — can be verified offline. Update README. Commit.

- [ ] **3. `sockets.py`** — `set_hwtstamp` (SIOCSHWTSTAMP ioctl), `make_timestamped_socket` (creates socket, sets `SO_TIMESTAMPING` flags, binds, sets non-blocking), `drain_errqueue`, `wait_tx_timestamp`. Update README. Commit.

- [ ] **4. `config.py`** — YAML loading and validation for the `owl-node` config schema (`node`, `peers`, `measurement`, `output`). Clear error messages for missing or invalid fields. Update README. Commit.

- [ ] **5. `owl_node.py`** — Full event loop: probe timer, `set_hwtstamp` re-assertion, TX timestamp capture, report handling, zero/negative latency guards, CSV writer, per-round counter log line, `reap_stale`. Update README with daemon usage. Commit.

- [ ] **6. Two-node smoke test script** — `scripts/smoke_test.py`: standalone script (no config file) that accepts `--peer-ip` and `--local-ip`, runs the probe/report cycle for 10 rounds, and prints hardware timestamps and computed latency to stdout. Used to verify the full timestamp path between two nodes before running the daemon. Update README with smoke test instructions. Commit.

- [ ] **7. `cli.py` (`owl-ping`)** — Single-peer ad-hoc measurement, stdout output, per-probe lines, summary stats on SIGINT. Shares `packets.py` and `sockets.py` with the daemon. Update README with `owl-ping` usage. Commit.
