import socket
import struct
import unittest

from owl import packets


class TestWireFormat(unittest.TestCase):
    def test_probe_roundtrip(self):
        data = packets.pack_probe(0xDEADBEEF, 42, packets.FLAG_HW)
        self.assertEqual(len(data), packets.HEADER_LEN)
        self.assertEqual(packets.unpack(data), packets.Probe(0xDEADBEEF, 42, packets.FLAG_HW))

    def test_probe_padding(self):
        data = packets.pack_probe(1, 2, size=100)
        self.assertEqual(len(data), 100)
        self.assertEqual(packets.unpack(data), packets.Probe(1, 2, 0))

    def test_probe_too_large(self):
        with self.assertRaises(ValueError):
            packets.pack_probe(1, 2, size=packets.MAX_PROBE_LEN + 1)

    def test_report_roundtrip(self):
        rx = 1_714_000_000_123_456_789
        data = packets.pack_report(7, 2**63, packets.FLAG_HW, rx)
        self.assertEqual(len(data), packets.REPORT_LEN)
        self.assertEqual(packets.unpack(data), packets.Report(7, 2**63, packets.FLAG_HW, rx))

    def test_rejects_garbage(self):
        self.assertIsNone(packets.unpack(b""))
        self.assertIsNone(packets.unpack(b"\x01" * 15))
        wrong_version = bytes([packets.VERSION + 1]) + packets.pack_probe(1, 1)[1:]
        self.assertIsNone(packets.unpack(wrong_version))
        unknown_type = packets.pack_probe(1, 1)[:1] + b"\x09" + packets.pack_probe(1, 1)[2:]
        self.assertIsNone(packets.unpack(unknown_type))
        truncated_report = packets.pack_report(1, 1, 0, 5)[:-1]
        self.assertIsNone(packets.unpack(truncated_report))

    def test_v1_ascii_payload_is_ignored(self):
        self.assertIsNone(packets.unpack(b"1661899028.5274663,1877"))


class TestControlMessages(unittest.TestCase):
    def _scm(self, sw, hw):
        ts = struct.pack("=qqqqqq", sw // 10**9, sw % 10**9, 0, 0, hw // 10**9, hw % 10**9)
        return (socket.SOL_SOCKET, packets.SCM_TIMESTAMPING, ts)

    def _ext_err(self, origin, key, level=socket.IPPROTO_IP, ctype=11):
        return (level, ctype, struct.pack("=IBBBBII", 42, origin, 0, 0, 0, 0, key) + bytes(16))

    def test_timestamping(self):
        anc = [(socket.IPPROTO_IP, 99, b"x"), self._scm(1_700_000_000_000_000_001, 37_000_000_005)]
        self.assertEqual(
            packets.parse_timestamping(anc),
            packets.Timestamps(1_700_000_000_000_000_001, 37_000_000_005),
        )

    def test_timestamping_missing(self):
        self.assertIsNone(packets.parse_timestamping([]))
        self.assertIsNone(packets.parse_timestamping([(socket.SOL_SOCKET, packets.SCM_TIMESTAMPING, b"short")]))

    def test_tx_key_ipv4_and_ipv6(self):
        self.assertEqual(packets.parse_tx_key([self._ext_err(packets.SO_EE_ORIGIN_TIMESTAMPING, 12345)]), 12345)
        v6 = self._ext_err(packets.SO_EE_ORIGIN_TIMESTAMPING, 7, level=socket.IPPROTO_IPV6, ctype=25)
        self.assertEqual(packets.parse_tx_key([v6]), 7)

    def test_tx_key_ignores_other_origins(self):
        icmp_error = self._ext_err(2, 99)  # SO_EE_ORIGIN_ICMP
        self.assertIsNone(packets.parse_tx_key([icmp_error]))


if __name__ == "__main__":
    unittest.main()
