"""Command-line entry point: `owl ping`, `owl reflect` and `owl daemon`."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
from typing import List, Optional, Tuple

from . import packets, version_string
from .daemon import Daemon, load_config, log_stderr
from .ping import (
    DEFAULT_PORT,
    LOST,
    NO_RX_TS,
    NO_TX_TS,
    OK,
    SEND_FAILED,
    Pinger,
    Result,
    SetupError,
    default_listen,
    open_reflector,
    resolve,
    summarize,
)
from .sinks import make_sink
from .sockets import iface_for_addr


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _probe_size(text: str) -> int:
    value = int(text)
    if not packets.HEADER_LEN <= value <= packets.MAX_PROBE_LEN:
        raise argparse.ArgumentTypeError(
            f"must be between {packets.HEADER_LEN} and {packets.MAX_PROBE_LEN}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="owl",
        description="One-way latency measurement using kernel and NIC packet timestamps.",
    )
    parser.add_argument("--version", action="version", version=f"owl {version_string()}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser(
        "ping",
        help="measure one-way latency to a host running `owl reflect` or the owl daemon",
        description=(
            "Send timestamped probes to PEER and print the one-way latency of each. "
            "Both hosts' clocks must be synchronized (PTP)."
        ),
    )
    p.add_argument("peer", help="hostname or IPv4/IPv6 address of the reflector")
    p.add_argument("-c", "--count", type=_positive_int, help="stop after COUNT probes (default: run until Ctrl-C)")
    p.add_argument("-i", "--interval", type=_positive_float, default=1.0, help="seconds between probes (default: 1)")
    p.add_argument("-W", "--timeout", type=_positive_float, default=2.0, help="seconds to wait for each report (default: 2)")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"reflector UDP port (default: {DEFAULT_PORT})")
    p.add_argument("-S", "--source", help="local address to send from (default: chosen by routing)")
    p.add_argument("-I", "--iface", help="interface for hardware timestamps (default: the one that owns the source address)")
    p.add_argument("-s", "--size", type=_probe_size, default=packets.HEADER_LEN, help="probe payload size in bytes (default: 16)")
    fam = p.add_mutually_exclusive_group()
    fam.add_argument("-4", dest="family", action="store_const", const=socket.AF_INET, default=0, help="use IPv4 only")
    fam.add_argument("-6", dest="family", action="store_const", const=socket.AF_INET6, help="use IPv6 only")
    p.add_argument(
        "--clock",
        choices=("hw", "sw"),
        default="hw",
        help="hw: NIC hardware timestamps (needs root/CAP_NET_ADMIN); sw: kernel software timestamps (default: hw)",
    )
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print one JSON object per probe, then a summary object")
    out.add_argument("-q", "--quiet", action="store_true", help="print only the summary")
    p.set_defaults(func=cmd_ping)

    r = sub.add_parser(
        "reflect",
        help="answer probes from `owl ping`",
        description=(
            "Listen for OWL probes and answer each with its arrival timestamp. "
            "Software timestamps always work. For hardware timestamps, the NIC must be "
            "told to stamp all packets: pass --iface, or --bind an address so the "
            "interface can be found (needs root/CAP_NET_ADMIN)."
        ),
    )
    r.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"UDP port to listen on (default: {DEFAULT_PORT})")
    r.add_argument("-B", "--bind", action="append", metavar="ADDR", help="address to listen on; repeatable (default: all IPv4 and IPv6)")
    r.add_argument("-I", "--iface", action="append", default=[], help="enable NIC hardware timestamping on IFACE; repeatable")
    r.add_argument("--no-hw", action="store_true", help="don't change NIC timestamp settings (software timestamps only)")
    r.add_argument("-v", "--verbose", action="store_true", help="print each probe answered")
    r.set_defaults(func=cmd_reflect)

    d = sub.add_parser(
        "daemon",
        help="probe a list of peers continuously and answer their probes",
        description=(
            "Run as a long-lived service: reflect probes on the listen port, probe every peer in "
            "the config at a fixed interval, and write one record per probe (plus periodic stats) "
            "to the configured sinks. Counters are logged to stderr. See owl/daemon.py for the "
            "config format."
        ),
    )
    d.add_argument("-c", "--config", required=True, help="JSON config file")
    d.add_argument("--check", action="store_true", help="validate the config and exit without opening sockets")
    d.set_defaults(func=cmd_daemon)
    return parser


# --- owl ping -----------------------------------------------------------------------


def _ms(ns: int) -> str:
    return f"{ns / 1e6:.3f}"


def _describe(r: Result, args, hw_hint_shown: List[bool]) -> str:
    if r.status == OK:
        line = f"seq={r.seq} owl={_ms(r.owl_ns)} ms"
        if r.rtt_ns is not None:
            line += f" rtt={_ms(r.rtt_ns)} ms"
        if r.owl_ns < 0:
            line += "  [negative: are both clocks PTP-synchronized?]"
        if r.remote_clock_ok is False:
            line += "  [reflector reports its clock is not synchronized]"
        return line
    if r.status == LOST:
        return f"seq={r.seq} lost (no report within {args.timeout:g} s)"
    if r.status == NO_TX_TS:
        return f"seq={r.seq} no TX timestamp from the local {'NIC' if args.clock == 'hw' else 'kernel'}"
    if r.status == NO_RX_TS:
        line = f"seq={r.seq} reflector returned no {args.clock} RX timestamp"
        if args.clock == "hw" and not hw_hint_shown[0]:
            hw_hint_shown[0] = True
            line += " (start `owl reflect` with --iface <dataplane interface>, or use --clock sw)"
        return line
    if r.status == SEND_FAILED:
        return f"seq={r.seq} send failed: {r.error}"
    return f"seq={r.seq} {r.status}"


def _print_summary(args, summary: dict) -> None:
    print(f"\n--- owl ping {args.peer} statistics ---")
    line = f"{summary['sent']} probes sent, {summary['ok']} measured, {summary['loss_pct']:.1f}% loss"
    if summary["invalid"]:
        line += f", {summary['invalid']} without timestamps"
    print(line)
    for name in ("owl", "rtt"):
        s = summary.get(name)
        if s:
            print(
                f"{name} min/avg/max/mdev = {_ms(s['min_ns'])}/{_ms(s['avg_ns'])}/"
                f"{_ms(s['max_ns'])}/{_ms(s['mdev_ns'])} ms"
            )


def cmd_ping(args) -> int:
    pinger = Pinger(
        args.peer,
        port=args.port,
        family=args.family,
        count=args.count,
        interval=args.interval,
        timeout=args.timeout,
        source=args.source,
        iface=args.iface,
        hw=args.clock == "hw",
        size=args.size,
    )
    peer_ip = pinger.peer_sockaddr[0]
    meta = {"src": pinger.source, "dst": peer_ip, "port": args.port, "clock": args.clock}
    if args.json:
        print(json.dumps({"type": "start", **meta, "iface": pinger.iface, "session": pinger.session}), flush=True)
    else:
        iface = f", iface {pinger.iface}" if pinger.iface else ""
        print(f"OWL PING {args.peer} ({peer_ip}) port {args.port} from {pinger.source}: clock {args.clock}{iface}", flush=True)

    results: List[Result] = []
    hw_hint_shown = [False]
    try:
        for r in pinger.results():
            results.append(r)
            if args.json:
                print(json.dumps({"type": "probe", **meta, **r._asdict()}), flush=True)
            elif not args.quiet:
                print(_describe(r, args, hw_hint_shown), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        pinger.close()

    summary = summarize(results)
    if args.json:
        print(json.dumps({"type": "summary", **meta, **summary}), flush=True)
    else:
        _print_summary(args, summary)
    return 0 if summary["ok"] else 1


# --- owl reflect --------------------------------------------------------------------


def _reflect_binds(args) -> Tuple[List[Tuple[int, tuple]], List[str]]:
    """Work out the sockets to open and the interfaces to enable hardware timestamps on."""
    ifaces = [] if args.no_hw else list(args.iface)
    binds: List[Tuple[int, tuple]] = []
    if args.bind:
        for addr in args.bind:
            family, sockaddr = resolve(addr, args.port)
            binds.append((family, sockaddr))
            if not args.no_hw and not args.iface:
                iface = iface_for_addr(sockaddr[0])
                if iface and iface not in ifaces:
                    ifaces.append(iface)
    else:
        binds = default_listen(args.port)
    return binds, ifaces


def cmd_reflect(args) -> int:
    binds, ifaces = _reflect_binds(args)
    reflector = open_reflector(binds, ifaces, defaulted=not args.bind)

    where = ", ".join(f"[{s.sockname[0]}]:{s.sockname[1]}" for s in reflector.socks)
    hw = f"hardware timestamps on {', '.join(ifaces)}" if ifaces else "software timestamps only"
    print(f"owl reflect listening on {where} ({hw})", flush=True)
    if not ifaces and not args.no_hw:
        print("  note: pass --iface <dataplane interface> to answer `--clock hw` probes", flush=True)

    def log(addr, probe, rx_ns):
        print(f"{addr[0]} session={probe.session:08x} seq={probe.seq} rx_ns={rx_ns}", flush=True)

    try:
        reflector.serve(on_probe=log if args.verbose else None)
    except KeyboardInterrupt:
        pass
    finally:
        reflector.close()
    print(f"\n{reflector.probes} probes answered, {reflector.errors} send errors")
    return 0


# --- owl daemon ---------------------------------------------------------------------


def cmd_daemon(args) -> int:
    cfg = load_config(args.config)
    if args.check:
        print(f"{args.config}: OK (label {cfg.label}, {len(cfg.peers)} peers, clock {cfg.clock}, "
              f"interval {cfg.interval:g} s)")
        return 0
    sinks = []
    try:
        for i, spec in enumerate(cfg.sinks):
            try:
                sinks.append(make_sink(spec, log=log_stderr))
            except (OSError, ValueError) as e:
                raise SetupError(f"sinks[{i}] ({spec['type']}): {getattr(e, 'strerror', None) or e}") from None
        daemon = Daemon(cfg, sinks)
    except BaseException:
        for s in sinks:
            s.close()
        raise
    daemon.log(f"owl {version_string()} started: {daemon.describe()}")
    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.emit_summaries(final=True)
        daemon.log_stats()
        daemon.close()
    daemon.log("stopped")
    return 0


# --- main ------------------------------------------------------------------------------


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("owl: requires Linux (SO_TIMESTAMPING)", file=sys.stderr)
        return 2
    # Stop cleanly (with a summary) on SIGTERM too, e.g. from systemd or `kill`.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        return args.func(args)
    except SetupError as e:
        print(f"owl: {e}", file=sys.stderr)
        return 2
