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
| 400 Docker containers for 20 nodes | 20 containers (one per node) |
| `tail -f \| tcpdump` subprocess chain | Plain UDP socket with `SO_TIMESTAMPING` |
| Rotating PCAP files | No intermediate files |
| Regex ASCII packet parsing | `struct.unpack` on fixed-format payloads |
| Separate `send_data.py` process | Integrated into receiver loop |
| C extension `ptp_time.c` + `phc2sys` grep | Kernel `SO_TIMESTAMPING` API |
| All heavy deps (scapy, pandas, numpy, pyarrow, reactivex, influxdb clients) | Removed for now |

---

## Packet Format

```
Probe  (sender → receiver):  [0x01][seq_n: uint64 BE]                    =  9 bytes
Report (receiver → sender):  [0x02][seq_n: uint64 BE][rx_hw_ns: uint64 BE] = 17 bytes
```

Both packet types travel on the same port (default 5005). The type byte lets the receiver loop distinguish probes from reports.

---

## Architecture

Each node runs **one daemon** with one UDP socket bound to `node_ip:5005`. Two threads share the socket:

### SenderThread
```
loop every interval_sec:
    for each peer_ip:
        sendmsg(probe {0x01, seq_n}, dest=peer_ip:5005)
        recvmsg(MSG_ERRQUEUE) → TX_hw_ns      # poll immediately after send
        tx_record[seq_n] = (peer_ip, TX_hw_ns)
        seq_n += 1                            # single global counter across all peers
```

### ReceiverThread
```
loop:
    data, ancdata, addr = sock.recvmsg(64, 1024)
    hw_ts = parse_scm_timestamping(ancdata)   # index [2] = raw hardware clock timespec

    if data[0] == PROBE:
        seq_n = struct.unpack('!Q', data[1:9])[0]
        sock.sendto(struct.pack('!BQQ', REPORT, seq_n, hw_ts), addr)

    elif data[0] == REPORT:
        seq_n, rx_hw_ns = struct.unpack('!QQ', data[1:17])
        peer_ip, tx_hw_ns = tx_record.pop(seq_n)
        latency_ns = rx_hw_ns - tx_hw_ns
        csv_writer.writerow(time.time_ns(), node_ip, peer_ip, seq_n,
                            tx_hw_ns, rx_hw_ns, latency_ns)
```

### Socket Setup
```python
SOF_TIMESTAMPING_TX_HARDWARE  = (1 << 2)
SOF_TIMESTAMPING_RX_HARDWARE  = (1 << 3)
SOF_TIMESTAMPING_RAW_HARDWARE = (1 << 6)
flags = SOF_TIMESTAMPING_TX_HARDWARE | SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_TIMESTAMPING, flags)
sock.bind((node_ip, port))
```

---

## CSV Output

Written by the ReceiverThread on the **sender node** only. The receiver node writes nothing.

**File path**: configured via `output.path`, e.g. `/owl_output/owl_10.10.1.1.csv`  
**Rotation**: new file each run (timestamp suffix), or continuous append — configurable  
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
├── owl_node.py         # entry point: loads config, creates socket, starts threads (daemon)
├── cli.py              # entry point: owl-ping — ping-like ad-hoc mode
├── sender.py           # SenderThread: probe loop + MSG_ERRQUEUE poll
├── receiver.py         # ReceiverThread: dispatch on type, report back or write CSV
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
  port: 5005

peers:
  - 10.10.1.2
  - 10.10.1.3

measurement:
  interval_sec: 1.0

output:
  path: /owl_output/owl.csv   # appended to; filename can include {node_ip}
```

---

## Docker

```bash
docker run -d \
  --network=host \
  --privileged \
  -v /etc/owl/config.yaml:/etc/owl/config.yaml \
  -v /owl_output:/owl_output \
  fabrictestbed/owl /etc/owl/config.yaml
```

`--pid=host` no longer needed. 20 nodes = 20 containers, each producing its own CSV.

---

## CLI (`owl-ping`)

A ping/iperf-style CLI for ad-hoc one-way latency measurement between two nodes — no config file, positional target, stdout output, summary on exit. Intended for debugging, connectivity checks, and casual use; the daemon (`owl-node`) remains the right choice for continuous multi-peer monitoring.

```bash
$ owl-ping 10.10.1.2
owl-ping 10.10.1.2: hardware-timestamped one-way latency (port 5005)
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
- `-p PORT` — UDP port (default: 5005)
- `-I IFACE_IP` — bind to this local IP (default: first non-loopback)

### Requires the other end to be listening
`owl-ping` also **answers** incoming probes (reflects reports) on the bound port, so running it on both ends is self-sufficient:

```
hostA$ owl-ping 10.10.1.B
hostB$ owl-ping 10.10.1.A
```

Alternatively, if `owl-node` is already running on the peer (topology deployment), `owl-ping <peer_ip>` from anywhere on the network works on its own.

### Shared code
`cli.py` imports from `packets.py` and uses the same `SO_TIMESTAMPING` socket setup as `owl_node.py`. One thread sends probes and polls the error queue for `tx_hw_ns`; another thread receives, dispatches on type byte, replies to probes, prints + tallies reports. No CSV, no YAML.

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
owl-shipper = "owl.shipper:main"
```

- Dev: `uv sync` (core) or `uv sync --extra shipper` (core + shipper).
- Docker: `uv sync --frozen --extra shipper` in the builder layer so the one image covers both entry points.
- `uv.lock` is committed for reproducibility.

**Removed entirely**: scapy, pandas, numpy, pyarrow, reactivex, requests (and the C extension).

---

## Prerequisite

NIC must support hardware timestamping. Verified on FABRIC `enp7s0`:
```
hardware-transmit ✓   hardware-receive ✓   hardware-raw-clock ✓
PTP Hardware Clock: 1
```

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
3. **20-node test**: one container per node, all 380 directed pairs appear across the 20 CSV files.

---

## InfluxDB Shipper (optional second layer)

The core daemon writes CSV and nothing else. A separate **shipper** process reads the CSV and forwards rows to InfluxDB. This keeps the measurement path free of network dependencies — an InfluxDB outage cannot affect probe scheduling or timestamp capture, and the CSV serves as a durable buffer the shipper replays from.

This layer exists **only for the fixed-topology deployment** that backs the web frontend. A user running OWL as a CLI ping replacement does not need it.

### Responsibilities
- Tail the node's CSV from a checkpointed offset so restarts neither re-ship nor drop rows.
- Batch rows and write them to InfluxDB as points with `sender`/`receiver` tags and `seq_n` / `tx_hw_ns` / `rx_hw_ns` / `latency_ns` fields. Point time = `timestamp_ns`.
- Support InfluxDB v2 (`influxdb_client`) and v3 cloud (`influxdb_client_3`), preserving the existing dual-backend behavior from `send_data.py`.

### Tailing strategy
Plain loop: `seek` to saved offset, `readline` until EOF, sleep briefly, repeat. No `watchdog`/inotify dependency. Offset is persisted to a small file (e.g. `/var/lib/owl/shipper.offset`) after each successful batch.

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

The core daemon (`owl_node.py`, `sender.py`, `receiver.py`, `packets.py`, `config.py`) has no import relationship to the shipper — they only share the CSV format as a contract.

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

### Docker
```bash
# Measurement daemon (core)
docker run -d --network=host --privileged \
  -v /etc/owl/config.yaml:/etc/owl/config.yaml \
  -v /owl_output:/owl_output \
  fabrictestbed/owl python -m owl.owl_node /etc/owl/config.yaml

# Shipper (same image, different command; runs alongside)
docker run -d \
  -v /etc/owl/shipper.yaml:/etc/owl/shipper.yaml \
  -v /owl_output:/owl_output \
  -v /var/lib/owl:/var/lib/owl \
  fabrictestbed/owl python -m owl.shipper /etc/owl/shipper.yaml
```

One image, two entry points. Nodes that don't need InfluxDB run only the daemon.

### Dependencies (shipper only)
Declared as the `shipper` extra in `pyproject.toml` (see Dependencies & Packaging). `uv sync --extra shipper` pulls in `influxdb_client` + `influxdb_client_3`; core daemon installs stay at `psutil` + `pyyaml`.

### Verification
1. Run the daemon on a node; confirm CSV grows.
2. Start the shipper against that CSV; confirm InfluxDB receives points with correct timestamps and tags.
3. Stop the shipper mid-run, restart: no duplicates, no gap (checkpoint works).
4. Briefly drop InfluxDB connectivity; shipper retries and catches up from its checkpoint without losing rows.
