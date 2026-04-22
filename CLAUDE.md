# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OWL** (One-Way Latency) is a Python 3.11 tool for measuring one-way network latency between nodes on the FABRIC Testbed (and general networks). It uses PTP (Precision Time Protocol) for nanosecond-accurate timestamps and stores results in PCAP files or InfluxDB.

## Setup & Build

```bash
# Compile the required C extension for PTP device access
gcc -fPIC -shared -o owl/sock_ops/ptp_time.so owl/sock_ops/ptp_time.c

# Create virtualenv and install deps
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or via Docker (recommended):
```bash
docker build -t fabrictestbed/owl .
```

There is no test suite or linter configured in this project.

## Running the Tools

All four scripts are invoked directly with `sudo` (raw socket / tcpdump access required):

```bash
# On sender node — periodically sends UDP probes stamped with PTP time
sudo python owl/sock_ops/owl_sender.py --dest-ip <IP> --dest-port <PORT> \
    --frequency <Hz> --duration <sec> --seq-n <start>

# On receiver node — captures incoming probes to a rotating PCAP
sudo python owl/sock_ops/owl_capturer.py --ip <local-IP> --port <PORT> \
    --outfile <path/to/output.pcap> --pcap-sec <rotation-interval>

# Live: tail PCAP → parse → push to InfluxDB
python owl/data_ops/send_data.py --pcapfile <path> \
    --token <token> --org <org> --url <url> --desttype cloud|local --bucket <bucket>

# Batch: convert a complete PCAP to CSV
python owl/data_ops/pcap_to_csv.py --pcapfile <path> --outfile <path>
```

## Architecture

```
owl/
├── sock_ops/           # Network measurement layer
│   ├── owl_sender.py   # Periodic UDP probe sender (custom threading.Timer loop)
│   ├── owl_capturer.py # tcpdump subprocess wrapper → PCAP output
│   ├── ptp.py          # Python wrapper around the C extension
│   └── ptp_time.c      # C extension: reads /dev/ptp* via ioctl for PTP hardware time
│
└── data_ops/           # Post-capture processing layer
    ├── send_data.py    # Live: tail -f | tcpdump pipeline → parse → InfluxDB write
    └── pcap_to_csv.py  # Batch: Scapy reads PCAP → CSV with computed latency
```

### Data Flow

1. **Sender** stamps each UDP packet payload as `"<ptp_timestamp_ns>,<seq_number>"` and sends at a configured frequency.
2. **Receiver** runs `tcpdump` in a subprocess with nanosecond precision (`--time-stamp-precision nano`, `-j adapter_unsynced`) and writes to a rotating PCAP.
3. **Processing** parses the payload, extracts the send timestamp, and computes latency as `recv_time − send_time` using `decimal.Decimal` to avoid floating-point loss.

### Key Design Details

- **PTP device discovery**: `ptp.py` runs `ps -ef | grep phc2sys` and parses the process arguments to find which `/dev/ptp*` device is in use. If none is found, it exits (non-recoverable).
- **System clock fallback**: Pass `--sys-clock` to `owl_sender.py` to use `time.time_ns()` instead of PTP hardware time (for testing without a PTP device).
- **Interface discovery**: `psutil.net_if_addrs()` maps the user-supplied IP to a network interface name; falls back to `'any'` if not found.
- **InfluxDB dual-client**: `send_data.py` branches on `--desttype` between `influxdb_client` (v2 local) and `influxdb_client_3` (v3 cloud), writing `latency`, `seq`, `send_time`, and `recv_time` fields with `sender`/`receiver` tags.
- **Subprocess management**: `owl_capturer.py` tracks the tcpdump PID explicitly and sends SIGTERM on duration expiry. `send_data.py` uses a `tail -f | tcpdump -r` pipeline for live streaming.

### System Prerequisites

PTP synchronization (`phc2sys`, `ptp4l`), `tcpdump`, and `gcc` must be installed on the host. These are not managed by this Python project.
