# owl2 — Architecture & Implementation Plan

**Created:** 2026-11-24
**Last updated:** 2026-11-24

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
- **Clock-health guard:** each round, check that the PHC is locked, e.g. with the `PTP_SYS_OFFSET` ioctl on `/dev/ptpN` comparing PHC to `CLOCK_REALTIME`. Mark samples `ptp_ok=false` rather than dropping them.
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
- **Data model (InfluxDB v2):**
  - Measurement `owl`.
  - Tags: `src_site, dst_site, src_ip, dst_ip, af`.
  - Fields: `latency_ns, seq, tx_hw_ns, rx_hw_ns, ptp_ok`.
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

**Open risk the FABRIC hardware test must answer:** FABRIC VMs get time from the host through `ptp_kvm`, and `phc2sys` disciplines the VM's `CLOCK_REALTIME`. It is not known whether the VM's dataplane NIC hardware clock (a ConnectX VF) is itself disciplined to the grandmaster.
- If it is not, hardware timestamps from different sites are not comparable, and `--clock sw` (kernel timestamps on the PTP-synced system clock, µs-level) becomes the default for FABRIC.
- A later option is to translate hardware timestamps into system time with `PTP_SYS_OFFSET`.

What to check, on two VMs at different PTP sites:
1. `ethtool -T <dataplane iface>` on both.
2. `owl ping --clock hw` vs `--clock sw` between them: compare `owl` against `rtt/2`, and check that `owl` doesn't jump by about 37 s or drift.
3. Both directions (A→B and B→A).
4. Repeat over FABNetv6.

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
- [ ] Two-node hardware test on FABRIC, over FABNetv4 and FABNetv6. **Run this before Phase 2.** It also settles the clock-domain question in "Phase 1 implementation notes" below. Notebook: `dev/fabric_two_node_test.ipynb` (2026-11-24): measures system−PHC offset and drift for `ptp_kvm` and each NIC PHC, runs both directions × v4/v6 × hw/sw, prints a verdict and saves a JSON file.

### Phase 2: owl daemon (this repo)
- [ ] JSON config with labels; the daemon event loop; counters
- [ ] Sinks: stdout/jsonl, `http-lineprotocol` with a bounded retry buffer
- [ ] Per-peer summaries; clock-health guard (`ptp_ok`)
- [ ] systemd unit, zipapp build, README
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
