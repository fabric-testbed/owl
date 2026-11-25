# OWL (One-Way Latency)

`owl` measures **one-way** network latency between two Linux hosts, the way
`ping` measures round-trip time. Each probe is timestamped by the kernel or the
NIC as it leaves the sender and again as it arrives at the receiver, so the
result is the actual time spent on the wire in one direction.

It was built for the [FABRIC testbed](https://fabric-testbed.net/) but has no
FABRIC-specific code. It uses only the Python standard library, so there is
nothing to install besides Python 3.9+.

> **Status:** owl2 rewrite in progress (branch `owl2`). `owl ping`,
> `owl reflect` and `owl daemon` (JSON Lines and InfluxDB output) work.
> Per-peer summaries and a clock-health flag are next. v1 (Docker + tcpdump +
> PCAP) is preserved at tag `v1-final`.

## Requirements

- **Linux.** owl uses `SO_TIMESTAMPING`.
- **Python 3.9 or newer.**
- **Synchronized clocks on both hosts.** One-way latency is only as accurate as
  the clock agreement between sender and receiver. In practice this means PTP
  (`ptp4l` / `phc2sys`). owl does not synchronize clocks itself.
- **For hardware timestamps (`--clock hw`, the default):** a NIC that supports
  them (check with `ethtool -T <iface>`), plus root or `CAP_NET_ADMIN` so owl can
  tell the NIC to timestamp all packets.

## Install

```bash
# from a clone
pip install .

# or straight from GitHub, as an isolated tool
uv tool install git+https://github.com/fabric-testbed/owl@owl2
pipx install git+https://github.com/fabric-testbed/owl@owl2

# or run without installing
python3 -m owl --help
```

## Quick start

On the **receiving** host, start a reflector. It answers each probe with the
time the probe arrived:

```bash
sudo owl reflect --iface enp7s0          # hardware timestamps on the dataplane NIC
owl reflect                              # software timestamps only, no root needed
```

On the **sending** host (example output; the numbers are illustrative):

```bash
$ sudo owl ping -c 5 10.140.135.2
OWL PING 10.140.135.2 (10.140.135.2) port 49005 from 10.129.138.2: clock hw, iface enp7s0
seq=0 owl=24.412 ms rtt=48.951 ms
seq=1 owl=24.408 ms rtt=48.903 ms
...
--- owl ping 10.140.135.2 statistics ---
5 probes sent, 5 measured, 0.0% loss
owl min/avg/max/mdev = 24.401/24.409/24.415/0.005 ms
rtt min/avg/max/mdev = 48.870/48.912/48.951/0.030 ms
```

- **`owl`** is the one-way latency: the reflector's RX timestamp minus the sender's TX timestamp.
- **`rtt`** is the probe's TX time to the report's arrival back at the sender. It includes the
  reflector's turnaround time, so it is only a sanity check. On a symmetric path, `owl` ≈ `rtt / 2`.

### `owl ping` options

| Option | Meaning |
|---|---|
| `-c COUNT` | stop after COUNT probes (default: until Ctrl-C) |
| `-i SEC` | interval between probes (default 1) |
| `-W SEC` | how long to wait for each report (default 2) |
| `-p PORT` | reflector port (default 49005) |
| `-S ADDR` | source address (default: chosen by routing) |
| `-I IFACE` | interface for hardware timestamps (default: the one owning the source address) |
| `-s SIZE` | probe payload size in bytes, 16 to 65507 (default 16) |
| `-4` / `-6` | force IPv4 / IPv6 |
| `--clock hw\|sw` | NIC hardware timestamps (default) or kernel software timestamps |
| `--json` | one JSON object per probe plus a summary, for scripts |
| `-q` | print only the summary |

Exit status is 0 if at least one probe was measured, 1 if none were, and 2 on a setup error.

### `owl reflect` options

| Option | Meaning |
|---|---|
| `-p PORT` | port to listen on (default 49005) |
| `-B ADDR` | address to listen on; repeatable (default: all IPv4 and IPv6) |
| `-I IFACE` | enable NIC hardware timestamping on IFACE; repeatable |
| `--no-hw` | leave NIC settings alone (software timestamps only) |
| `-v` | print each probe answered |

If the reflector has no hardware timestamp for a probe that asked for one, it
answers with "no timestamp" rather than substituting a software time.
`owl ping` reports that as `reflector returned no hw RX timestamp`.

## Running as a daemon

`owl daemon` is for continuous monitoring of a set of peers, for example one
host per site, each running the daemon and listing the others:

```bash
owl daemon -c examples/owl-daemon.json --check   # validate the config, open nothing
sudo owl daemon -c examples/owl-daemon.json
```

The daemon does both jobs in one process:
- it answers probes on port 49005, like `owl reflect`, so other daemons and `owl ping` can probe it;
- it probes every peer once per `interval`, spread evenly across the interval rather than in bursts.

Each probe becomes one JSON line in the sink (values here are illustrative). The `src_label` and `dst_label` fields come
straight from the config, so you can use site names, hostnames, or anything else:

```json
{"type":"probe","time_ns":1795644249182783846,"src_label":"STAR","dst_label":"PSC","src_ip":"10.129.138.2","dst_ip":"10.140.135.2","af":4,"clock":"hw","seq":42,"status":"ok","owl_ns":24408113,"rtt_ns":48903220,"tx_ns":1795644249182790000,"rx_ns":1795644249207198113}
```

Every `stats_interval` seconds it also writes a `"type":"stats"` record with
cumulative counters (probes sent, each status, reflected, sink drops) and logs the
same counters to stderr, which systemd sends to the journal. SIGTERM stops it cleanly.

Config keys (only `label` and `peers` are required):

| Key | Default | Meaning |
|---|---|---|
| `label` | — | this host's label, written as `src_label` |
| `peers` | — | list of `{"addr": ..., "label": ...}`; `label` defaults to the address. List a peer's IPv4 and IPv6 addresses separately to measure both. |
| `clock` | `"hw"` | `"hw"` or `"sw"`, as for `owl ping` |
| `port` | 49005 | port to listen on and to probe peers on |
| `interval` | 5 | seconds between probes to each peer |
| `timeout` | 2 | seconds to wait for each report before counting the probe lost |
| `size` | 16 | probe payload size in bytes |
| `ifaces` | found from the routes to the peers | interfaces to enable hardware timestamping on |
| `listen` | all IPv4 and IPv6 addresses | addresses to listen on |
| `stats_interval` | 60 | seconds between stats records and log lines |
| `sinks` | `[{"type": "jsonl", "path": "-"}]` | where records go (see below) |

Unknown keys are rejected, so a typo fails `--check` instead of being silently ignored.

### Sinks

**`jsonl`**: one JSON object per line. `"path": "-"` (the default) is stdout; any
other path is appended to.

**`http-lineprotocol`**: batches records as InfluxDB line protocol and POSTs them.
It works with InfluxDB v2 and v3, Telegraf, VictoriaMetrics, or anything else that
accepts line protocol over HTTP:

```json
{"type": "http-lineprotocol",
 "url": "https://influx.example.org:8086/api/v2/write?org=fabric&bucket=owl_raw&precision=ns",
 "token_file": "/etc/owl/influx-token"}
```

| Key | Default | Meaning |
|---|---|---|
| `url` | — | write endpoint, including org/bucket query parameters for InfluxDB v2 |
| `token` / `token_file` | none | sent as `Authorization: Token …`; prefer a file readable only by root |
| `measurement` | `owl` | measurement for probe records |
| `stats_measurement` | `owl_stats` | measurement for stats records |
| `batch_size` | 5000 | most lines per POST |
| `flush_interval` | 10 | seconds between POSTs when no full batch is waiting |
| `max_buffer` | 100000 | lines held in memory while the endpoint is unreachable |
| `timeout` | 10 | seconds per HTTP request |
| `gzip` | true | compress request bodies |

Any sink also takes `"records": ["probe"]` or `["stats"]` to receive only one kind,
for example to send stats to a different bucket.

Probe records are written as measurement `owl` with tags `src_label`, `dst_label`,
`src_ip`, `dst_ip`, `af`, `clock` and `status`, and integer fields `seq`, `owl_ns`,
`rtt_ns`, `tx_ns` and `rx_ns`. A lost probe is still written, but with no `owl_ns`
or `rtt_ns`, so loss can be counted from the `status` tag.

Sending happens on a background thread, so a slow or unreachable endpoint never
delays probing. While the endpoint is down, lines wait in memory and are retried
in order, with backoff up to 60 s. If the buffer fills, the oldest lines are
dropped and counted. A 4xx response other than 429 (for example a bad token) means
retrying can't help: that batch is dropped and the error is logged. The stats
records count drops (`sink_dropped`), the backlog (`sink_buffered`) and failed
attempts (`sink_failed_posts`).

## Hardware vs software timestamps

| | `--clock hw` | `--clock sw` |
|---|---|---|
| Where the time is taken | in the NIC as the packet crosses the wire | in the kernel: TX as the packet is queued to the device, RX as it enters the stack |
| Clock | the NIC's PTP hardware clock (often TAI; UTC on FABRIC) | `CLOCK_REALTIME` (UTC) |
| Distance from the wire | nanoseconds | µs to tens of µs (more in VMs), so latencies read slightly high |
| Needs | NIC support + root/`CAP_NET_ADMIN` | nothing |

Both ends must use the same kind, and owl enforces this. Neither kind depends
on Python's speed: the timestamps are recorded by the kernel or NIC, and owl
only reads them afterwards. In both modes the result is only as good as the
synchronization between the two machines' clocks, which owl does not manage.

[docs/clock-chain.md](docs/clock-chain.md) explains which clocks a measurement
depends on, especially on FABRIC VMs, where the NIC's clock must be steered
inside the VM. It also covers what can go wrong and what has been measured.

`--clock hw` changes a NIC-wide setting: it turns on TX timestamps and sets the
RX filter to "all packets" (`SIOCSHWTSTAMP`, the same thing
`tcpdump -j adapter_unsynced` does). That filter is a superset of what `ptp4l`
needs, so `ptp4l` keeps working.

## How it works

```
sender                                        reflector
  │── probe {session, seq} ────────────────────▶│  RX timestamp taken by kernel/NIC
  │   TX timestamp → error queue (OPT_ID key)   │
  │◀──────────── report {session, seq, rx_ns} ──│
  │
  └─ one-way latency = rx_ns − tx_ns
```

- Probes are 16-byte UDP payloads, optionally padded with `-s`. Reports are 24 bytes.
  The wire format is documented in [`owl/packets.py`](owl/packets.py).
- The sender's TX timestamp comes back on the socket's error queue tagged with a
  per-socket counter (`SOF_TIMESTAMPING_OPT_ID`). That lets owl match timestamps to
  probes even when they arrive out of order.
- Reports go back to whatever address and port the probe came from. `owl ping` can
  therefore use an ephemeral port and run alongside a reflector on the same host.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The packet and control-message tests run anywhere. The loopback tests run the
real ping/reflect path with software timestamps and need Linux. On macOS, use a
container:

```bash
docker run --rm --sysctl net.ipv6.conf.all.disable_ipv6=0 -v "$PWD":/src:ro -w /src \
  python:3.9-slim python -B -m unittest discover -s tests -v
```

Hardware timestamps can only be tested on hosts with a suitable NIC, such as two
FABRIC VMs at PTP-capable sites.

## License

MIT. See [LICENSE](LICENSE).
