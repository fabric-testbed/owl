# owl2 — Architecture & Implementation Plan

**Created:** 2026-11-24
**Last updated:** 2026-11-25 (Phase 2: systemd unit and zipapp)

This document supersedes `.claude/owl-redesign-plan.md` (2026-04) as the top-level plan. That file is still the detailed reference for the measurement core (socket setup, event loop, `MSG_ERRQUEUE` handling, hardware-timestamp filter). owl2 keeps that core and changes everything around it. Where the two conflict, this file wins.

## Context

OWL measures one-way latency between FABRIC VMs using PTP-synced clocks. The current stack is fragile and heavy:
- Docker per sender/receiver pair, about 400 containers for 20 sites.
- A tcpdump → rotating PCAP → `tail -f | tcpdump -A` → regex → InfluxDB pipeline.
- A C extension that finds the PTP device by grepping `ps`.
- IPv4 only.

Site names are attached only inside the dashboard, using a hand-written `slice.csv`. The monitoring slice has been rebuilt with new IPs at least three times (2025-08, 2026-07, 2026-09), so data keyed by IP is ambiguous across rebuilds. There is no API. Users have asked for an on-demand "latency between SITE1 and SITE2" answer from a standby slice, triggered from the testbed GUI.

### Platform facts that shape the design

These come from exploring fabrictestbed-extensions, ControlFramework, MeasurementFramework, mflib, jupyter-examples and loomai.
- **Management NIC:** reachable inbound only via bastion SSH, plus ICMP. Outbound works over IPv6 with DNS64/NAT64, which is how nodes reach InfluxDB today. So any service-to-VM control channel would have to SSH through the bastion. Answering on-demand from continuous data avoids that.
- **Lease:** the default is 24h and the maximum is about 2 weeks. A `Slice.NoLimitLifetime` project tag allows up to 26 weeks; we can get it.
- **PTP-capable sites:** fablib's `site['ptp_capable']`. VM-side PTP is set up with the `fabric-testbed/ptp` Ansible playbook; see the reference sequence in FABlib's `fablib/crease/crinkle.py:93-131`.
- **Current OWL users:** `mflib/mflib/owl.py` runs the Docker image with hardcoded paths, IPv4 and the `rocky` user. Several notebooks (KNIT6, MF OWL service) are already broken. All of these get cleaned up at the end.
- **A public-API pattern to follow:** FABRIC's `infrastructure-metrics.fabric-testbed.net`.

## Big picture

```
 repo: owl  (generic, FABRIC-agnostic, pure Python stdlib, Py ≥3.9)
 ┌──────────────── VM at site A ────────────────┐        ┌── VM at site B ──┐
 │ owl daemon (systemd)                          │ probe  │ owl daemon       │
 │  • reflects incoming probes (rx_hw_ns back)   │──────▶ │  stamps RX in HW │
 │  • probes every peer every N s                │◀────── │  report(rx_hw)   │
 │  • latency = rx_hw − tx_hw (on sender)        │ report │                  │
 │  • sinks: stdout/JSONL file | HTTP line-proto │        └──────────────────┘
 └───────────────┬───────────────────────────────┘
                 │ outbound HTTPS over mgmt NIC (IPv6/NAT64), batched line protocol,
                 │ tags already carry site names (from config labels)
                 ▼
 repo: fabric-latency (grows out of latency-dashboard; FABRIC-specific)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ InfluxDB (self-hosted v2)  bucket owl_raw (~7 d retention)           │
 │                            bucket owl_1m  (1-min summaries, keep)    │
 │ API  /api/v1/sites /latest /latency /matrix  (only DB reader)        │
 │ Dashboard (Dash) → calls the API, no direct Flux                     │
 │ ── on a separate admin host ──                                       │
 │ slice manager (FABlib, cron): reconcile 1 VM per PTP site, install   │
 │   owl + PTP, generate daemon config (peer IP + site label),          │
 │   publish sites.json to the API host                                 │
 │ legacy backfill: dated slice.csv files → add site tags to old data   │
 └──────────────────────────────────────────────────────────────────────┘
 Portal / LoomAI "latency SITE1→SITE2" button → GET /api/v1/latest
```

## Decisions (2026-11-24)

- **Repos:**
  - `owl` is a generic, FABRIC-agnostic tool.
  - A separate FABRIC repo, grown from `latency-dashboard`, holds the slice manager, API, dashboard and backfill.
  - owl2 lives in this repo as v2.0. Tag the last old commit `v1-final`, build on a branch, then merge.
- **Language:** Python 3.9+, standard library only, shipped as a single-file zipapp. Precision comes from the NIC's hardware timestamps, which every language reads the same way, so it doesn't depend on the language. The C extension goes away.
- **No Docker:** run under systemd on the VM.
- **Ingest:** the daemon batches line protocol and POSTs it directly to InfluxDB with a write-only token scoped to the bucket. There is no separate shipper process.
- **Site names:** written at the source. The slice manager writes peer labels (site codes) into each daemon's config, and the daemon emits them as tags.
- **On-demand:** answered from recent continuous data (≤ about 5 s old). No fresh-probe trigger channel.
- **Slice:** one no-limit-lifetime slice for both monitoring and on-demand.
  - Probe interval 5 s.
  - Raw samples kept about 7 days; 1-minute summaries kept indefinitely.
  - FABNetv4 only at first; FABNetv6 later. The tool supports both from day one.
- **API:** public and read-only, with rate limiting. It runs as Flask routes under `/api/v1` on the Dash server (`app.server`), so there is one deployable unit.
- **InfluxDB:** stay on self-hosted v2. Only the API's query module would change for a future v3 move.
- **Hosting:**
  - API and dashboard run on the InfluxDB host.
  - The reconcile cron job runs on a separate admin machine, so the FABRIC token and bastion key stay off the public host.
- **Compatibility:** no backward compatibility, except a one-time backfill that maps old IP-tagged data to sites.

## Repo 1: `owl` (generic tool)

**Measurement core.** Keep the 2026-04 design from `owl-redesign-plan.md`:
- Probe/report packets.
- `SO_TIMESTAMPING` with RAW_HARDWARE.
- `SIOCSHWTSTAMP` with `HWTSTAMP_FILTER_ALL`.
- `MSG_ERRQUEUE` to read the TX timestamp.
- A single-thread selector loop and `reap_stale`.

Changes from 2026-04:
- **IPv4 and IPv6:** resolve with `getaddrinfo`, use `AF_INET6` sockets, and parse the cmsg the same way for both. One socket per address family in use.
- **Packet:** add a version byte so the format can change later: `[ver][type][seq]…`.
- **No third-party dependencies:**
  - Replace `psutil` with `socket.if_nameindex()` plus an address lookup via ioctl or `ip -j addr`.
  - Replace YAML config with a JSON file and CLI flags.
- **Distribution:** single-file zipapp `owl.pyz`. It is also installable with pip or `uv tool install` from git. A systemd unit file ships in the repo.
- **Commands:**
  - `owl ping <peer>`: ping-like per-probe lines plus a summary, and `--json` output.
  - `owl daemon -c config.json`: all-to-all prober and reflector.
- **Labels:**
  - Config: `{"label": "STAR", "peers": [{"addr": "10.140.135.2", "label": "PSC"}, ...]}`.
  - Labels are generic strings emitted as output tags.
- **Sinks, in process and pluggable:**
  - `stdout` / `jsonl` file, the default.
  - `http-lineprotocol`: batched POST via `urllib` to any line-protocol endpoint (InfluxDB v2/v3, Telegraf, VictoriaMetrics). It keeps a bounded in-memory retry buffer. If the endpoint is down, measurement continues; on overflow the oldest points are dropped and counted.
  - **Summaries:** every `summary_sec` (default 60), emit a per-peer summary (count, lost, min, p50, mean, max, stddev) alongside the raw samples. Raw and summary samples can be routed to different buckets or measurements.
- **Clock-health guard:** each round, compare the dataplane NIC's PHC with `CLOCK_REALTIME`, using the tightest of N `clock_gettime` triples on the `/dev/ptpN` fd, or the `PTP_SYS_OFFSET` ioctl. Mark samples `ptp_ok=false` rather than dropping them. The FABRIC test (2026-11-24) showed this is essential: without `phc2sys@<iface>` the PHC drifts by ppm and is off by hundreds of ms.
- **Counters:** probes sent, TX-timestamp failures, zero RX timestamps, reaped and sink drops, logged each round to journald.

## Repo 2: FABRIC latency service (`latency-dashboard`, possibly renamed `fabric-latency`)

- **Slice manager** (`slice/`, FABlib): an idempotent `reconcile`, run hourly by cron on the admin host.
  1. Find active sites where `site['ptp_capable']` is true.
  2. Make sure each has exactly one VM with a FABNetv4 NIC.
  3. Run the `fabric-testbed/ptp` playbook.
  4. Install `owl.pyz` and its systemd unit.
  5. Write the daemon config with every peer's IP and site label plus the write-only InfluxDB token.
  6. Restart the daemon only if the config changed.
  7. Renew the slice if needed.
  8. Write `sites.json` (site, VM, IPs, lat/lon, `since`) and publish it to the API host.
- **Data model (InfluxDB v2):** what owl's `http-lineprotocol` sink writes (revised 2026-11-25, when the sink was built):
  - Measurement `owl` for probes and `owl_stats` for counters.
  - Probe tags: `src_label, dst_label` (the slice manager sets them to site names), `src_ip, dst_ip, af` (address family, 4 or 6), `clock`, `status`.
  - Probe fields: `seq, owl_ns, rtt_ns, tx_ns, rx_ns`, plus `ptp_ok` once the clock-health guard exists.
  - Lost probes are written too, without `owl_ns`/`rtt_ns`, so loss is queryable by `status`.
  - Changes from the original draft: `src_site/dst_site` became `src_label/dst_label`, because owl stays FABRIC-agnostic. `latency_ns` became `owl_ns`, matching `owl ping`. `tx_hw_ns/rx_hw_ns` became `tx_ns/rx_ns`, because they are software timestamps in sw mode and the `clock` tag says which.
  - Raw samples go to `owl_raw` (~7 d retention); summaries go to `owl_1m` (kept indefinitely).
  - Rough load: 30 sites × 29 peers / 5 s ≈ 174 points/s raw, ≈ 15 points/s summaries.
- **API:** Flask routes on the Dash server. It is the only component that knows Flux, and all queries are parameterized. Endpoints:
  - `GET /api/v1/sites`
  - `GET /api/v1/latest?src=&dst=`: last N raw samples plus their age. This is the on-demand answer.
  - `GET /api/v1/latency?src=&dst=&start=&end=&step=`: reads `owl_1m`, or raw for short windows.
  - `GET /api/v1/matrix`: latest all-pairs grid.
- **Dashboard:** keep Dash but make it a client of the API.
  - Drop `slice.csv` and the `.str.contains` site matching.
  - Add a latest matrix/map view and an on-demand "SITE1 → SITE2" form.
  - Remove `debug=True` and stop printing the token.
- **Legacy backfill:** a one-time script that applies the dated `data_*/slice.csv` files as time-ranged IP→site maps and rewrites old points with site tags into the new schema.
- **Portal/LoomAI integration:** both call `GET /api/v1/latest`. No inbound access to VMs is needed.

## Cleanup (last phase)

- **mflib:** replace `mflib/owl.py` and `owl_data.py` with a thin "install owl.pyz + run `owl ping --json`" helper, or remove them.
- **jupyter-examples:** remove or rewrite the KNIT6, KNIT7 and `complex_recipes/owl` notebooks.
- **MeasurementFramework:** delete the stale `instrumentize/experiment_bootstrap/service_scripts/owl.py`.
- **Docker Hub:** retire the `fabrictestbed/owl` image.
- **Docs:** update `documentation-v2-dev` `docs/guides/monitoring/owl-latency.md`.

## Still open (decide during implementation)

- The name of repo 2 (`fabric-latency`, or keep `latency-dashboard`).
- The VM image for the slice: Ubuntu 24.04 (Python 3.12) vs Rocky, whichever the `fabric-testbed/ptp` playbook supports best.

## Verification

- `owl ping` between two PTP VMs:
  - Latency is roughly RTT/2 on symmetric paths.
  - There are no zero timestamps.
  - It works over FABNetv4 and FABNetv6.
- The daemon on 3 VMs produces all 6 directed pairs, and summaries reach `owl_1m` with correct site tags.
- Stop InfluxDB for 5 minutes: measurement continues, the buffer drains on recovery, and the drop counter is logged.
- Reconcile is idempotent: running it twice changes nothing. Removing a site makes the peers drop it.
- API: `/latest` returns samples less than 2 intervals old, and `/latency` agrees with the raw data.
- Backfill: old IP-tagged series appear under the correct sites for each dated range.

## Phase 1 implementation notes (2026-11-24)

Decisions made while building, which refine the design above:

- **Wire format.**
  - Probe: 16 bytes, `ver, type, flags, reserved, session:u32, seq:u64`, optionally padded with `-s`.
  - Report: 24 bytes, the header plus `rx_ns:u64`.
  - `session` is random for each sender run, so stale or foreign reports are ignored.
  - `FLAG_HW` in a probe asks for the hardware RX timestamp; without it the reflector returns the software one. The reflector returns `rx_ns=0` rather than substituting a timestamp of the other kind.
- **TX timestamps are matched with `SOF_TIMESTAMPING_OPT_ID` keys**, not by "send, then read the next error-queue entry" as in the April plan. Report sends and driver delays can interleave error-queue entries, so ordering isn't reliable.
  - After a failed `sendto()`, the socket resets the kernel counter (toggling SO_TIMESTAMPING) and starts a new token generation, so matching can never drift off by one.
  - This also makes the daemon's batched sends (Phase 2) straightforward: no blocking wait after each probe.
- **Reports go back to the probe's source address and port.** `owl ping` uses an ephemeral port, so it can probe a host running the daemon on 49005, even from a host that runs the daemon itself. The single default port is 49005 (49006 from the April plan is dropped).
- **`owl reflect`** is a standalone reflector, like `iperf -s`, so ad-hoc tests don't need the daemon.
- **`--clock hw|sw`.**
  - Software timestamps (kernel, `CLOCK_REALTIME`) are a first-class mode, not just a test fallback.
  - Hardware timestamps are in the NIC PHC's timebase, often TAI, about 37 s ahead of UTC. Never mix the two kinds.
- **The April plan's `SOF_TIMESTAMPING_*` bit values were wrong** (TX_HARDWARE is `1<<0`, not `1<<2`). `owl/sockets.py` has the correct values from `linux/net_tstamp.h`.

### FABRIC two-node hardware test results (2026-11-24)

Run with `dev/fabric_two_node_test.ipynb`.
- **Setup:** UCSD ↔ TOKY, `default_ubuntu_22` (kernel 5.15.0-185), dataplane NICs are ConnectX VFs (`mlx5_core`, fw 20.43), owl at `ae80b6f`.
- **Earlier open risk:** whether a VF's NIC hardware clock (PHC) is disciplined at all. Answer: only if the PTP playbook's `phc2sys` services run.

**Clocks.**
- **Before PTP was set up correctly,** each NIC's PHC was free-running: 0.3–0.7 s off the host clock, drifting 1.7–5.8 ppm. Raw `--clock hw` across sites would have been meaningless.
- **The `fabric-testbed/ptp` playbook does sync it.** Besides `phc2sys@CLOCK_REALTIME`, it starts `phc2sys@<iface>` for every interface with a PHC. That service steers the NIC's PHC from `ptp_kvm` with `-O +0`, so the PHC runs on UTC, not TAI. A VF *is* allowed to adjust its PHC.
- **Measured with those services running:** system clock vs `ptp_kvm` within 0.4–0.5 µs, and NIC PHC vs system clock within 0.4–0.65 µs. Drift is under 0.06 ppm.
- **One clock per card:** both VFs on a VM (`/dev/ptp0`, `/dev/ptp1`) read identically, so they are probably one card clock steered by two `phc2sys` instances. That caused no visible problem. If hw results ever look noisier than expected, run one `phc2sys` per physical clock.

**`owl ping --clock hw`: works.** 50/50 probes in every run.

| | UCSD→TOKY min | TOKY→UCSD min | rtt min | rtt − sum |
|---|---|---|---|---|
| IPv4 | 54.331 ms | 62.498 ms | 116.898 ms | 69 µs |
| IPv6 | 62.499 ms | 62.500 ms | 125.077 ms | 78 µs |

- **Clock agreement:** over IPv6 the two directions differ by 0.2 µs, which bounds the clock offset between the VMs.
- **Reflector turnaround:** `rtt − sum`, 69–78 µs. It does not affect `owl`.
- **IPv4 routing is asymmetric:** the two directions differ by 8.2 ms. That is real (the clocks are proven consistent), and it is exactly what RTT/2 hides.
- **Jitter:** average − min is 13–37 µs.

**`--clock sw`: broken on these VMs (fixed 2026-11-25, see below).**
- `mlx5_core` on 5.15 produces no software TX timestamps; `ethtool -T` lists only `hardware-*` capabilities. Loopback works on the same VM, and running as root makes no difference.
- Every sw run is `no_tx_ts`.

### Second FABRIC run (2026-11-25): CLEM ↔ NCSA

Same notebook and image, owl at `0a61f76`, plus a TX timestamp diagnostic that sends one packet per timestamp kind from each VM.

**TX timestamp kinds on `mlx5_core` VFs** (both VMs, IPv4 and IPv6 alike):

| flags | to the FABNet peer | to loopback |
|---|---|---|
| `TX_SOFTWARE` (with or without OPT_ID) | nothing | SND |
| `TX_HARDWARE` | SND, hw time | nothing |
| `TX_SCHED` | SCHED, sw time | SCHED |

**Decision: `--clock sw` now uses `TX_SCHED` everywhere, not `TX_SOFTWARE`.**
- It is taken in `__dev_queue_xmit()` for every device, so sw mode means the same thing on every driver.
- It gives one error-queue entry per packet, so OPT_ID matching is unchanged.
- It is earlier than the driver's timestamp by the time spent in the qdisc and driver, usually a few µs. That is small next to the sw RX timestamp's own interrupt and softirq latency.
- Preferring SND and falling back to SCHED was rejected: a run could mix the two kinds, and matching would have to wait for a second entry that may never come.

**PTP failed on one VM (owl-a at CLEM)**, even though FABRIC lists CLEM as PTP-capable.
- `phc2sys@*` inactive, `/dev/ptp_kvm` present: the system clock was 0.9 ms off `ptp_kvm` and drifting 6.6 ppm, and the NIC PHC was 0.88 s off and drifting 10.5 ppm. The cause is not known yet.
- The hw runs still completed: A→B 903.93 ms, B→A −858.55 ms. The verdict said BROKEN. The apparent offset (881.24 ms) matches the clock check's PHC offset (0.8815 s), and sum vs rtt still agreed (turnaround 0.28 ms), because the offset cancels in the sum. That validates both the clock check and the verdict logic.
- `ptp_capable` therefore does not guarantee working PTP on a given VM. The notebook now prints the `phc2sys` status, journal and playbook errors when the service isn't running, and warns before measuring. The Phase 3 slice manager must check the clocks itself and treat a failed site as down.

**Consequences for later phases:**
- **`--clock hw` stays the default and is the FABRIC mode.** Its timebase there is UTC.
- **Clock-health guard (Phase 2):** judge each dataplane NIC's PHC and `CLOCK_REALTIME` against `/dev/ptp_kvm` when it exists (against each other otherwise), as the notebook's clock check does since 2026-11-25. Offsets should be within a few µs and drift under 1 ppm (drift over 5 s is noise below ~0.2 ppm). Mark samples `ptp_ok=false` otherwise. Losing `phc2sys@<iface>` silently corrupts hw data, and that was the first failure seen here.
- **Slice manager (Phase 3):** must verify that `phc2sys@CLOCK_REALTIME` *and* `phc2sys@<dataplane iface>` are active.
- **Don't install Ubuntu's `linuxptp` package before the playbook.** The playbook then skips installing gcc/make, its linuxptp 4.4 build fails, and no `phc2sys` units are created.
- **FABlib pitfall:** `node.execute(timeout=N)` runs `sudo timeout N <cmd>`, which breaks commands that start with a shell builtin such as `cd`.
- **Image:** `default_ubuntu_22` works with the playbook. This partly settles the image question in "Still open".

## Phase 2 implementation notes (2026-11-25)

- **`ProbeEngine` (in `ping.py`)** now holds the probe/TX-timestamp/report matching that was inside `Pinger`. `send(sockaddr, target)` returns the target with each Result, so one engine probes many peers. `Pinger` is a single-peer loop around it, and `owl ping` output is unchanged.
- **Sockets:** the daemon probes from one wildcard-bound socket per address family on an ephemeral port, and reflects on the listen port (default 49005, all addresses), using the same `Reflector` as `owl reflect`. Each peer's source IP for the records is looked up from the routing table at startup. A peer with no route only logs a warning, and its sends are counted as `send_failed` until the route returns.
- **hw interfaces** are found from the routes to the peers unless the config sets `ifaces`.
- **Scheduling:** each peer is probed every `interval`, staggered evenly across it. After a stall, missed rounds are skipped rather than sent in a burst.
- **Startup delay:** probing starts 0.2 s after the sockets open. The first socket on a host to ask for software RX timestamps makes the kernel enable them through deferred work (`net_enable_timestamp`), and in testing about 1 in 3 fresh starts got `no_rx_ts` on seq 0 without the delay.
- **Records:** `probe` records carry `time_ns, src_label, dst_label, src_ip, dst_ip, af, clock, seq, status, owl_ns, rtt_ns, tx_ns, rx_ns` (and `error`). `stats` records carry the cumulative counters (`sent`, each status, `pending`, `reflected`, `reflect_errors`, `sink_dropped`). The stats are also logged to stderr every `stats_interval`.
- **Config** is strict: unknown keys are errors, so typos fail `owl daemon --check`, which the slice manager can run before restarting the service.
- **`http-lineprotocol` sink:**
  - `emit()` only appends to a bounded deque, and a sender thread POSTs batches (gzip, `Authorization: Token`). A slow endpoint can't stall the selector loop, which would also make probes expire as falsely `lost`.
  - A failed batch (network error, 429, 5xx) is retried before anything newer, with backoff of 1 s doubling to 60 s. New records arriving during an outage do not cut the backoff short.
  - Other 4xx: the batch is dropped as rejected and logged.
  - When full, the buffer drops the oldest lines. `close()` spends up to 5 s delivering what is left.
  - Sinks take `records: [...]` to receive only probes or only stats.
- **Verified against InfluxDB 2.7 in Docker (2026-11-25):** daemon at 0.2 s interval with two peers over IPv4 and IPv6, InfluxDB stopped from about 6 s to 14 s. Measurement was unaffected (297/297 ok). Delivery resumed 15 s after the first failure, and InfluxDB held 149 + 148 = 297 probe points exactly once, plus 15 stats points. Labels with spaces were escaped correctly.
- **Summaries:**
  - Windows of `summary_interval` (default 60 s) aligned to the wall clock, keyed by each probe's send time.
  - A window is emitted once no more results can arrive (window end + timeout + 1 s). Shutdown emits the partial windows.
  - Fields: `sent, ok, lost (incl. send_failed), invalid, owl_{min,p50,mean,max,stddev}_ns, rtt_{min,p50}_ns, ptp_bad`, and `ptp_ok` when any probe's clock state was known.
  - Latency statistics include bad-clock probes; `ptp_ok`/`ptp_bad` let the dashboard grey them out instead of hiding them.
  - Measurement `owl_summary`; sinks can take `records: ["summary"]` to send them to their own bucket.
- **Clock check (`clockcheck.py`, `ClockGuard`):**
  - The reference is `/dev/ptp_kvm` if present, else the system clock (hw only).
  - Checked: the PHC of each probing NIC in hw mode (index via the `ETHTOOL_GET_TS_INFO` ioctl, no `ethtool` binary), or the system clock in sw mode.
  - Tightest of 100 ref/clock/ref triples every `clock_check_interval` (10 s). Healthy within `clock_max_offset_us` (10 µs) of 0 or of 37 s (a TAI NIC clock).
  - Without a reference, for example sw mode on a host without `ptp_kvm`, the check is disabled and state stays unknown.
- **Remote clock state (new report flags):** `FLAG_CLOCK_CHECKED` (0x02) and `FLAG_CLOCK_BAD` (0x04) in reports. The daemon's reflector sets them from its own check. `owl reflect` sets neither, so its state is "unknown".
  - Probe records carry `local_clock_ok`, `remote_clock_ok` and `ptp_ok` (false if either is false, true if both are true, omitted otherwise).
  - This closes the gap that a sender could only vouch for its own clock. Backward compatible, because readers ignore unknown flag bits.
  - `owl ping` prints a note when the reflector reports a bad clock, and `--json` includes `remote_clock_ok`.
- **Packaging:**
  - `scripts/build_pyz.py` writes `dist/owl.pyz`, about 31 KB. It embeds `owl/_build.py` with the git commit (`+dirty` if `owl/` has uncommitted changes), so `owl --version` and the daemon's startup line say exactly what's deployed; the slice manager can compare that with what it means to deploy.
  - `contrib/owl.service`:
    - runs `/usr/bin/python3 /opt/owl/owl.pyz daemon -c /etc/owl/owl.json`, with `--check` as ExecStartPre;
    - `Restart=on-failure` after 10 s, and `TimeoutStopSec=30` (the http sink may spend about 15 s flushing);
    - root with `CapabilityBoundingSet=CAP_NET_ADMIN`, `ProtectSystem=strict`, and `LogsDirectory=owl` for jsonl output.
  - `ProtectClock=` and `PrivateDevices=` are deliberately absent: both restrict device access and would block `/dev/ptp*`. `systemd-analyze security` rates the unit 4.5 (OK).
- **Tested under systemd 252 (Debian 12 container, 2026-11-25):** `systemd-analyze verify` passes; enable --now starts it after the config check; stats appear in the journal; `systemctl stop` is clean (final summaries written, 28/28 ok); jsonl lands in `/var/log/owl`. A config typo fails the start with the bad key named in the journal, and systemd retries every 10 s.
- **Not yet:** config reload (the slice manager restarts the service instead). Publishing `owl.pyz` (a GitHub release asset, or the slice manager builds it from a pinned commit) is for Phase 3 to decide.

## Implementation checklist

### Phase 0: housekeeping
- [x] Save this plan to `.claude/owl2-plan.md` (2026-11-24)
- [x] Tag the last v1 commit `v1-final` (c9e84a0); create the `owl2` branch (2026-11-24)
- [x] Point CLAUDE.md at this plan (2026-11-24)

### Phase 1: owl core (this repo)
- [x] Scaffolding: `pyproject.toml` with no dependencies, the `owl/` package, delete the v1 code, `Dockerfile` and `requirements.txt` (2026-11-24)
- [x] `packets.py`: versioned probe/report pack and unpack, the `SCM_TIMESTAMPING` / `sock_extended_err` parsers (offline unit tests) (2026-11-24)
- [x] `sockets.py`: v4/v6 `TimestampedSocket` with OPT_ID TX tokens, `SIOCSHWTSTAMP`, interface lookup via `ip -j addr` (2026-11-24)
- [x] `owl ping` (per-probe output, summary, `--json`) and `owl reflect`. Loopback tests pass with software timestamps on Linux, Python 3.9, IPv4 and IPv6 (2026-11-24)
- [x] Two-node hardware test on FABRIC, over FABNetv4 and FABNetv6. Notebook: `dev/fabric_two_node_test.ipynb`. **hw passes; sw has no TX timestamps on mlx5 VFs.** See "FABRIC two-node hardware test results" (2026-11-24).
- [x] `--clock sw` on drivers without software TX timestamps: sw mode uses `SOF_TIMESTAMPING_TX_SCHED`, which works on FABRIC's mlx5 VFs. See "Second FABRIC run" (2026-11-25)
- [ ] Rerun the two-node test with PTP working on both VMs to confirm `--clock sw` end to end on FABRIC

### Phase 2: owl daemon (this repo)
- [x] JSON config with labels; the daemon event loop; counters; `jsonl` sink (2026-11-25). See "Phase 2 implementation notes"
- [x] Sinks: stdout/jsonl, `http-lineprotocol` with a bounded retry buffer (2026-11-25). Verified against a real InfluxDB 2.7 with an 8 s outage: all 297 probes stored once, in order
- [x] Per-peer summaries; clock-health guard (`ptp_ok`), with the peer's verdict carried in report flags (2026-11-25)
- [ ] Validate the clock check on FABRIC: the notebook's new daemon section (phc_index ioctl on mlx5 VFs, `/dev/ptp_kvm` reads, flags between two VMs; ideally once with a broken-PTP VM)
- [x] systemd unit, zipapp build, README (2026-11-25). Tested under real systemd (Debian 12 container): config check, start, journal, clean stop, bad-config failure
- [ ] 3-node test with InfluxDB, including an outage test

### Phase 3: FABRIC service (dashboard repo)
- [ ] Copy this plan into that repo's `.claude/`
- [ ] Slice manager `reconcile` plus `sites.json` publishing
- [ ] InfluxDB buckets, retention and write-only token
- [ ] API routes (`/sites`, `/latest`, `/latency`, `/matrix`), parameterized queries, rate limiting
- [ ] Dashboard refactor onto the API; matrix/map view; on-demand form
- [ ] Legacy backfill script

### Phase 4: integration and cleanup
- [ ] Portal/LoomAI hook calling `/api/v1/latest`
- [ ] mflib, jupyter-examples, MeasurementFramework and docs cleanup; retire the Docker image
