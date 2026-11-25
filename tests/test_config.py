"""Daemon config validation (portable: no sockets are opened)."""

import json
import os
import tempfile
import unittest

from owl.daemon import ConfigError, load_config, parse_config

MINIMAL = {"label": "STAR", "peers": [{"addr": "10.0.0.2", "label": "PSC"}]}


def with_(**changes):
    cfg = json.loads(json.dumps(MINIMAL))
    cfg.update(changes)
    return cfg


class TestParseConfig(unittest.TestCase):
    def test_minimal_gets_defaults(self):
        cfg = parse_config(MINIMAL)
        self.assertEqual(cfg.label, "STAR")
        self.assertEqual([(p.addr, p.label) for p in cfg.peers], [("10.0.0.2", "PSC")])
        self.assertEqual((cfg.clock, cfg.port, cfg.interval, cfg.timeout, cfg.size), ("hw", 49005, 5.0, 2.0, 16))
        self.assertIsNone(cfg.ifaces)
        self.assertIsNone(cfg.listen)
        self.assertEqual(cfg.sinks, [{"type": "jsonl", "path": "-"}])

    def test_full(self):
        cfg = parse_config(with_(
            clock="sw", port=50000, interval=1, timeout=0.5, size=64, ifaces=["ens7"],
            listen=["10.0.0.1"], stats_interval=30, sinks=[{"type": "jsonl", "path": "/tmp/x.jsonl"}],
            peers=[{"addr": "10.0.0.2", "label": "PSC"}, {"addr": "fd00::2"}],
        ))
        self.assertEqual((cfg.clock, cfg.port, cfg.interval, cfg.timeout, cfg.size), ("sw", 50000, 1.0, 0.5, 64))
        self.assertEqual(cfg.peers[1].label, "fd00::2", "label defaults to the address")
        self.assertEqual((cfg.ifaces, cfg.listen, cfg.stats_interval), (["ens7"], ["10.0.0.1"], 30.0))

    def assertRejected(self, obj, fragment):
        with self.assertRaises(ConfigError) as cm:
            parse_config(obj)
        self.assertIn(fragment, str(cm.exception))

    def test_rejects(self):
        self.assertRejected([], "JSON object")
        self.assertRejected({"label": "X"}, '"peers"')
        self.assertRejected(with_(peers=[]), "non-empty list")
        self.assertRejected(with_(peers=["10.0.0.2"]), "peers[0] must be an object")
        self.assertRejected(with_(peers=[{"addr": ""}]), "peers[0].addr")
        self.assertRejected(with_(peers=[{"addr": "a"}, {"addr": "a"}]), "listed twice")
        self.assertRejected(with_(peers=[{"addr": "a", "site": "X"}]), "unknown key(s) site")
        self.assertRejected(with_(intervall=5), "unknown key(s) intervall")
        self.assertRejected(with_(label=""), "label")
        self.assertRejected(with_(clock="ptp"), "clock")
        self.assertRejected(with_(interval=0), "interval must be > 0")
        self.assertRejected(with_(timeout="2"), "timeout must be a number")
        self.assertRejected(with_(port=70000), "port")
        self.assertRejected(with_(port=True), "port must be an integer")
        self.assertRejected(with_(size=8), "size")
        self.assertRejected(with_(ifaces="ens7"), "ifaces")
        self.assertRejected(with_(sinks=[{"type": "influx"}]), "sinks[0]")
        self.assertRejected(with_(sinks=[{"type": "jsonl", "file": "x"}]), "unknown key(s) file")

    def test_load_config_errors(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "owl.json")
            with self.assertRaises(ConfigError):
                load_config(path)
            with open(path, "w") as f:
                f.write("{not json")
            with self.assertRaises(ConfigError) as cm:
                load_config(path)
            self.assertIn("not valid JSON", str(cm.exception))
            with open(path, "w") as f:
                json.dump(MINIMAL, f)
            self.assertEqual(load_config(path).label, "STAR")


if __name__ == "__main__":
    unittest.main()
