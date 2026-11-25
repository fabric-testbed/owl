"""The daemon end to end over loopback, probing itself (Linux only, software timestamps)."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from owl import ping
from owl.daemon import Daemon, parse_config
from owl.sinks import JsonlSink, ListSink


def free_port() -> int:
    """A port that is currently free for UDP on both IPv4 and IPv6 (best effort)."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s4:
            s4.bind(("0.0.0.0", 0))
            port = s4.getsockname()[1]
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s6:
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    s6.bind(("::", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_TIMESTAMPING is Linux-only")
class TestDaemonLoopback(unittest.TestCase):
    def make(self, **overrides):
        cfg = {
            "label": "self",
            "peers": [{"addr": "127.0.0.1", "label": "lo4"}, {"addr": "::1", "label": "lo6"}],
            "clock": "sw", "port": free_port(), "interval": 0.05, "timeout": 0.5, "stats_interval": 0.2,
        }
        cfg.update(overrides)
        sink = ListSink()
        logs = []
        try:
            daemon = Daemon(parse_config(cfg), [sink], log=logs.append)
        except OSError as e:
            self.skipTest(f"cannot open sockets: {e}")
        return daemon, sink, logs

    def run_for(self, daemon, seconds):
        stop = threading.Event()
        t = threading.Thread(target=daemon.run, args=(stop,), daemon=True)
        t.start()
        time.sleep(seconds)
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive(), "daemon did not stop")

    def test_probes_both_families_and_reflects(self):
        daemon, sink, logs = self.make()
        try:
            self.run_for(daemon, 0.7)
            daemon.log_stats()
        finally:
            daemon.close()

        probes = [r for r in sink.records if r["type"] == "probe"]
        for label, af, dst in (("lo4", 4, "127.0.0.1"), ("lo6", 6, "::1")):
            mine = [r for r in probes if r["dst_label"] == label]
            self.assertGreaterEqual(len(mine), 5, f"{label}: {len(mine)} probes")
            for r in mine:
                self.assertEqual(r["status"], ping.OK, r)
                self.assertEqual((r["src_label"], r["af"], r["dst_ip"], r["clock"]), ("self", af, dst, "sw"))
                self.assertEqual(r["src_ip"], dst)  # loopback talks to itself
                self.assertGreater(r["tx_ns"], 0)
                self.assertTrue(0 <= r["owl_ns"] < 100_000_000, r)
                self.assertGreaterEqual(r["rtt_ns"], r["owl_ns"])
            seqs = [r["seq"] for r in mine]
            self.assertEqual(seqs, sorted(seqs))

        stats = [r for r in sink.records if r["type"] == "stats"]
        self.assertGreaterEqual(len(stats), 2, "periodic stats plus the final one")
        last = stats[-1]
        self.assertEqual(last["ok"], len(probes))
        self.assertEqual(last["sent"], last["ok"] + last["pending"])
        self.assertGreaterEqual(last["reflected"], len(probes), "the daemon answered its own probes")
        self.assertEqual((last["lost"], last["sink_dropped"]), (0, 0))
        self.assertTrue(any(line.startswith("stats: ") for line in logs))

    def test_stagger(self):
        """Peers are spread across the interval, not sent in a burst."""
        daemon, sink, _ = self.make(interval=0.4, stats_interval=10)
        try:
            self.run_for(daemon, 0.6)  # first probe at 0.2 s (STARTUP_DELAY), second peer at 0.4 s
        finally:
            daemon.close()
        t = {r["dst_label"]: r["time_ns"] for r in sink.records if r["type"] == "probe" and r["seq"] == 0}
        self.assertEqual(set(t), {"lo4", "lo6"})
        self.assertGreater(abs(t["lo6"] - t["lo4"]), 150_000_000, "second peer ~0.2 s after the first")

    def test_unanswered_peer_is_lost(self):
        """A peer with nothing listening shows up as lost, and the others keep working."""
        # The daemon listens on 127.0.0.1 only, so nothing answers on 127.0.0.2.
        daemon, sink, _ = self.make(
            peers=[{"addr": "127.0.0.1", "label": "lo4"}, {"addr": "127.0.0.2", "label": "nobody"}],
            listen=["127.0.0.1"], timeout=0.2,
        )
        try:
            self.run_for(daemon, 0.6)
        finally:
            daemon.close()
        by = {}
        for r in sink.records:
            if r["type"] == "probe":
                by.setdefault(r["dst_label"], set()).add(r["status"])
        self.assertEqual(by.get("lo4"), {ping.OK})
        self.assertEqual(by.get("nobody"), {ping.LOST})

    def test_cli_check_and_jsonl_sink(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "owl.jsonl")
            cfg_path = os.path.join(d, "owl.json")
            with open(cfg_path, "w") as f:
                json.dump({"label": "self", "peers": [{"addr": "127.0.0.1", "label": "lo4"}], "clock": "sw",
                           "port": free_port(), "interval": 0.05, "sinks": [{"type": "jsonl", "path": out}]}, f)
            check = subprocess.run([sys.executable, "-m", "owl", "daemon", "-c", cfg_path, "--check"],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn("OK (label self, 1 peers", check.stdout)

            proc = subprocess.Popen([sys.executable, "-m", "owl", "daemon", "-c", cfg_path],
                                    stderr=subprocess.PIPE, text=True)
            time.sleep(0.8)
            proc.terminate()  # SIGTERM: clean stop with final stats
            _, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn("started: label self", err)
            self.assertIn("stopped", err)
            with open(out) as f:
                records = [json.loads(line) for line in f]
        probes = [r for r in records if r["type"] == "probe"]
        self.assertGreaterEqual(len(probes), 5)
        self.assertTrue(all(r["status"] == ping.OK for r in probes))
        self.assertEqual(records[-1]["type"], "stats")


class TestJsonlSink(unittest.TestCase):
    def test_appends_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.jsonl")
            for n in (1, 2):
                s = JsonlSink(path)
                s.emit({"type": "probe", "n": n})
                s.close()
            with open(path) as f:
                self.assertEqual([json.loads(l)["n"] for l in f], [1, 2])


if __name__ == "__main__":
    unittest.main()
