"""End-to-end ping/reflect over loopback with kernel software timestamps (Linux only).

Hardware timestamps need a PTP-capable NIC and are tested on FABRIC, not here.
"""

import socket
import sys
import threading
import unittest

from owl import ping


@unittest.skipUnless(sys.platform.startswith("linux"), "SO_TIMESTAMPING is Linux-only")
class TestLoopback(unittest.TestCase):
    def _run(self, family, addr):
        try:
            reflector = ping.Reflector([(family, (addr, 0))])
        except OSError as e:
            self.skipTest(f"cannot bind {addr}: {e}")
        port = reflector.socks[0].sockname[1]
        stop = threading.Event()
        t = threading.Thread(target=reflector.serve, args=(stop,), daemon=True)
        t.start()
        try:
            pinger = ping.Pinger(addr, port=port, count=5, interval=0.05, timeout=1.0, hw=False)
            try:
                results = list(pinger.results())
            finally:
                pinger.close()
        finally:
            stop.set()
            t.join(2)
            reflector.close()

        self.assertEqual([r.seq for r in results], [0, 1, 2, 3, 4])
        for r in results:
            self.assertEqual(r.status, ping.OK, r)
            self.assertGreater(r.tx_ns, 0)
            self.assertGreaterEqual(r.owl_ns, 0)
            self.assertLess(r.owl_ns, 100_000_000)  # well under 100 ms on loopback
            self.assertIsNotNone(r.rtt_ns)
            self.assertGreaterEqual(r.rtt_ns, r.owl_ns)
        self.assertEqual(reflector.probes, 5)
        summary = ping.summarize(results)
        self.assertEqual((summary["sent"], summary["ok"], summary["lost"]), (5, 5, 0))

    def test_ipv4(self):
        self._run(socket.AF_INET, "127.0.0.1")

    def test_ipv6(self):
        self._run(socket.AF_INET6, "::1")

    def test_lost_when_nobody_listens(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # bound but never answers
            pinger = ping.Pinger("127.0.0.1", port=port, count=2, interval=0.05, timeout=0.2, hw=False)
            try:
                results = list(pinger.results())
            finally:
                pinger.close()
        self.assertEqual([r.status for r in results], [ping.LOST, ping.LOST])
        self.assertTrue(all(r.tx_ns > 0 for r in results), "TX timestamps should still arrive")

    def test_hw_probe_to_sw_only_reflector(self):
        """A reflector without NIC timestamps must answer rx_ns=0, not a software time."""
        reflector = ping.Reflector([(socket.AF_INET, ("127.0.0.1", 0))])
        port = reflector.socks[0].sockname[1]
        stop = threading.Event()
        t = threading.Thread(target=reflector.serve, args=(stop,), daemon=True)
        t.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.0)
                s.sendto(ping.packets.pack_probe(9, 1, ping.packets.FLAG_HW), ("127.0.0.1", port))
                data, _ = s.recvfrom(64)
        finally:
            stop.set()
            t.join(2)
            reflector.close()
        report = ping.packets.unpack(data)
        self.assertEqual((report.session, report.seq, report.rx_ns), (9, 1, 0))
        self.assertTrue(report.flags & ping.packets.FLAG_HW)


if __name__ == "__main__":
    unittest.main()
