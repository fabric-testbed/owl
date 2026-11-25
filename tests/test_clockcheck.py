"""Clock-health check: offset measurement, the healthy rule, and state changes."""

import sys
import time
import unittest

from owl import clockcheck
from owl.clockcheck import TAI_UTC_NS, ClockGuard, healthy, measure_offset


class TestMeasure(unittest.TestCase):
    def test_same_clock_is_zero(self):
        self.assertLess(abs(measure_offset(time.CLOCK_REALTIME, time.CLOCK_REALTIME)), 50_000)

    def test_different_clocks(self):
        # CLOCK_MONOTONIC counts from boot, so it is decades behind the wall clock
        self.assertGreater(measure_offset(time.CLOCK_REALTIME, time.CLOCK_MONOTONIC), 10**15)


class TestHealthy(unittest.TestCase):
    def test_rule(self):
        self.assertTrue(healthy(0, 10_000))
        self.assertTrue(healthy(-9_000, 10_000))
        self.assertFalse(healthy(20_000, 10_000))
        self.assertTrue(healthy(-TAI_UTC_NS + 3_000, 10_000), "a NIC clock on TAI is consistent")
        self.assertFalse(healthy(-TAI_UTC_NS + 30_000, 10_000))
        self.assertFalse(healthy(881_000_000, 10_000), "CLEM 2026-11-25")


class TestGuard(unittest.TestCase):
    def guard_with(self, offsets):
        logs = []
        seq = iter(offsets)
        g = ClockGuard(hw=False, ifaces=[], log=logs.append, measure=lambda ref, clk: next(seq))
        # Point it at two fake clocks, as on a VM: system clock and a NIC clock vs ptp_kvm.
        g.reference = ("/dev/ptp_kvm", 1)
        g.targets = [("CLOCK_REALTIME", 2), ("/dev/ptp0 (ens7)", 3)]
        return g, logs

    def test_transitions_are_logged_once(self):
        g, logs = self.guard_with([500, 120, 600, 880_000_000, 400, 900_000_000, 300, 150])
        logs.clear()
        self.assertIsNone(g.ok)
        self.assertTrue(g.check())
        self.assertFalse(g.check())
        self.assertEqual(g.worst_offset_ns(), 880_000_000)
        self.assertFalse(g.check())
        self.assertTrue(g.check())
        self.assertEqual(len(logs), 3, logs)  # OK, NOT SYNCHRONIZED, OK again
        self.assertIn("NOT SYNCHRONIZED", logs[1])
        self.assertIn("/dev/ptp0 (ens7) is +880.000 ms", logs[1])

    @unittest.skipIf(clockcheck.os.path.exists(clockcheck.PTP_KVM), "this host has /dev/ptp_kvm")
    def test_sw_without_ptp_kvm_is_disabled(self):
        logs = []
        g = ClockGuard(hw=False, ifaces=[], log=logs.append)
        self.assertFalse(g.enabled)
        self.assertIsNone(g.check())
        self.assertIn("needs /dev/ptp_kvm", g.describe())
        self.assertTrue(logs)


@unittest.skipUnless(sys.platform.startswith("linux"), "ethtool ioctl is Linux-only")
class TestPhcIndex(unittest.TestCase):
    def test_loopback_has_no_phc(self):
        from owl.sockets import phc_index
        self.assertIsNone(phc_index("lo"))

    def test_hw_guard_without_phc_is_disabled(self):
        g = ClockGuard(hw=True, ifaces=["lo"])
        self.assertFalse(g.enabled)
        self.assertIsNone(g.check())
        g.close()


if __name__ == "__main__":
    unittest.main()
