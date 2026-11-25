"""Sinks: line protocol formatting, sink config validation, and the HTTP sink against a local server."""

import gzip
import http.server
import threading
import time
import unittest

from owl.sinks import HttpLineProtocolSink, make_sink, to_line_protocol, validate_spec

PROBE = {
    "type": "probe", "time_ns": 1795644249182783846, "src_label": "STAR", "dst_label": "PSC",
    "src_ip": "10.129.138.2", "dst_ip": "10.140.135.2", "af": 4, "clock": "hw", "seq": 42,
    "status": "ok", "owl_ns": 24408113, "rtt_ns": 48903220, "tx_ns": 1795644249182790000,
    "rx_ns": 1795644249207198113,
}


def probe(seq, **kw):
    return {**PROBE, "seq": seq, "time_ns": PROBE["time_ns"] + seq, **kw}


class TestLineProtocol(unittest.TestCase):
    def test_ok_probe(self):
        self.assertEqual(
            to_line_protocol(PROBE, "owl"),
            "owl,af=4,clock=hw,dst_ip=10.140.135.2,dst_label=PSC,src_ip=10.129.138.2,src_label=STAR,status=ok "
            "seq=42i,owl_ns=24408113i,rtt_ns=48903220i,tx_ns=1795644249182790000i,rx_ns=1795644249207198113i "
            "1795644249182783846",
        )

    def test_lost_probe_has_no_latency_fields(self):
        line = to_line_protocol(probe(1, status="lost", owl_ns=None, rtt_ns=None, rx_ns=0), "owl")
        self.assertIn("status=lost ", line)
        self.assertNotIn("owl_ns", line)
        self.assertNotIn("rtt_ns", line)

    def test_escaping_and_empty_tags(self):
        line = to_line_protocol(probe(1, src_label="My Site,1", dst_label="a=b", src_ip=""), "owl meas")
        self.assertTrue(line.startswith(r"owl\ meas,af=4,"), line)
        self.assertIn(r"dst_label=a\=b,", line)
        self.assertIn(r"src_label=My\ Site\,1,", line)
        self.assertNotIn("src_ip=", line)  # line protocol has no empty tag values

    def test_error_is_a_string_field(self):
        line = to_line_protocol(probe(1, status="send_failed", error='Network "is" unreachable'), "owl")
        self.assertIn(r'error="Network \"is\" unreachable"', line)

    def test_summary_and_booleans(self):
        rec = {"type": "summary", "time_ns": 60_000_000_000, "window_s": 60.0, "src_label": "STAR",
               "dst_label": "PSC", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "af": 4, "clock": "hw",
               "sent": 12, "ok": 12, "owl_min_ns": 5, "ptp_bad": 0, "ptp_ok": True}
        self.assertEqual(
            to_line_protocol(rec, "owl_summary"),
            "owl_summary,af=4,clock=hw,dst_ip=10.0.0.2,dst_label=PSC,src_ip=10.0.0.1,src_label=STAR "
            "window_s=60.0,sent=12i,ok=12i,owl_min_ns=5i,ptp_bad=0i,ptp_ok=true 60000000000",
        )

    def test_stats(self):
        line = to_line_protocol({"type": "stats", "time_ns": 5, "src_label": "STAR", "uptime_s": 1.5,
                                 "sent": 10, "ok": 9}, "owl_stats")
        self.assertEqual(line, "owl_stats,src_label=STAR uptime_s=1.5,sent=10i,ok=9i 5")


class TestValidateSpec(unittest.TestCase):
    def test_defaults(self):
        s = validate_spec({"type": "http-lineprotocol", "url": "http://h:8086/api/v2/write?bucket=b"})
        self.assertEqual((s["batch_size"], s["flush_interval"], s["max_buffer"], s["gzip"]), (5000, 10.0, 100000, True))
        self.assertEqual(s["records"], ["probe", "summary", "stats"])
        self.assertEqual(validate_spec({"type": "jsonl"})["path"], "-")

    def test_rejects(self):
        for spec, fragment in [
            ({"type": "http-lineprotocol"}, "url is required"),
            ({"type": "http-lineprotocol", "url": "influx:8086"}, "http://"),
            ({"type": "http-lineprotocol", "url": "http://h", "token": "a", "token_file": "b"}, "not both"),
            ({"type": "http-lineprotocol", "url": "http://h", "batch_size": 0}, "batch_size"),
            ({"type": "http-lineprotocol", "url": "http://h", "batch_size": 10, "max_buffer": 5}, "exceed"),
            ({"type": "http-lineprotocol", "url": "http://h", "gzip": "yes"}, "gzip"),
            ({"type": "http-lineprotocol", "url": "http://h", "bucket": "x"}, "unknown key(s) bucket"),
            ({"type": "jsonl", "records": ["traces"]}, "records"),
            ({"type": "kafka"}, "type one of"),
        ]:
            with self.subTest(spec=spec), self.assertRaises(ValueError) as cm:
                validate_spec(spec)
            self.assertIn(fragment, str(cm.exception))


class _Server:
    """Local HTTP endpoint. ``codes`` is a list of status codes to answer with, in order;
    when it runs out, every request gets 204. Received lines are kept in order."""

    def __init__(self, codes=()):
        self.codes = list(codes)
        self.lines = []
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                code = outer.codes.pop(0) if outer.codes else 204
                outer.requests.append((self.path, dict(self.headers), code))
                if code == 204:
                    outer.lines.extend(body.decode().splitlines())
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v2/write?org=o&bucket=b&precision=ns"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TestHttpSink(unittest.TestCase):
    def make(self, server, logs, **kw):
        sink = HttpLineProtocolSink(server.url, log=logs.append, **kw)
        sink.retry_initial = 0.05
        sink.retry_max = 0.2
        return sink

    def test_delivers_batches_with_token_and_gzip(self):
        server, logs = _Server(), []
        try:
            sink = self.make(server, logs, token="s3cret", batch_size=10, flush_interval=0.1)
            for i in range(25):
                sink.emit(probe(i))
            self.assertTrue(wait_for(lambda: len(server.lines) == 25), server.lines)
            sink.close()
        finally:
            server.close()
        self.assertEqual([int(l.rsplit(" ", 1)[1]) - PROBE["time_ns"] for l in server.lines], list(range(25)))
        path, headers, _ = server.requests[0]
        self.assertIn("bucket=b", path)
        self.assertEqual(headers["Authorization"], "Token s3cret")
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertLessEqual(max(len(r) for r in [server.lines[i:i + 10] for i in range(0, 25, 10)]), 10)
        self.assertEqual(sink.stats(), {"sink_dropped": 0, "sink_buffered": 0, "sink_failed_posts": 0})

    def test_outage_retries_in_order_and_recovers(self):
        server, logs = _Server(codes=[503, 503, 500]), []
        try:
            sink = self.make(server, logs, batch_size=5, flush_interval=0.05)
            for i in range(12):
                sink.emit(probe(i))
            self.assertTrue(wait_for(lambda: len(server.lines) == 12), server.lines)
            sink.close()
        finally:
            server.close()
        seqs = [int(l.split("seq=", 1)[1].split("i", 1)[0]) for l in server.lines]
        self.assertEqual(seqs, list(range(12)), "nothing lost, nothing duplicated, order kept")
        self.assertEqual(sink.failed_posts, 3)
        self.assertTrue(any("failing" in m for m in logs) and any("delivering again" in m for m in logs), logs)

    def test_unreachable_endpoint_does_not_block_emit(self):
        logs = []
        sink = HttpLineProtocolSink("http://127.0.0.1:9/write", log=logs.append, timeout=0.5,
                                    flush_interval=0.05, max_buffer=50, batch_size=10)
        sink.retry_initial, sink.retry_max, sink.close_timeout = 0.05, 0.1, 0.2
        t0 = time.monotonic()
        for i in range(200):
            sink.emit(probe(i))
        self.assertLess(time.monotonic() - t0, 0.5, "emit must only buffer")
        self.assertTrue(wait_for(lambda: sink.failed_posts >= 2))
        st = sink.stats()
        self.assertLessEqual(st["sink_buffered"], 50)
        self.assertEqual(st["sink_dropped"], 150, "oldest dropped when the buffer is full")
        sink.close()
        self.assertTrue(any("not delivered at shutdown" in m for m in logs), logs)

    def test_overflow_keeps_newest(self):
        server, logs = _Server(codes=[503] * 1000), []
        try:
            sink = self.make(server, logs, batch_size=5, flush_interval=0.05, max_buffer=20)
            for i in range(10):
                sink.emit(probe(i))
            self.assertTrue(wait_for(lambda: sink.failed_posts >= 1))
            for i in range(10, 40):
                sink.emit(probe(i))
            server.codes.clear()  # endpoint comes back
            self.assertTrue(wait_for(lambda: sink.stats()["sink_buffered"] == 0))
            sink.close()
        finally:
            server.close()
        seqs = [int(l.split("seq=", 1)[1].split("i", 1)[0]) for l in server.lines]
        self.assertEqual(len(seqs), 20)
        self.assertEqual(seqs[-15:], list(range(25, 40)), "the newest lines survive")
        self.assertEqual(sink.dropped, 20)

    def test_rejected_batch_is_not_retried(self):
        server, logs = _Server(codes=[401]), []
        try:
            sink = self.make(server, logs, batch_size=3, flush_interval=0.05)
            for i in range(6):
                sink.emit(probe(i))
            self.assertTrue(wait_for(lambda: len(server.lines) == 3))
            sink.close()
        finally:
            server.close()
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(sink.rejected, 3)
        self.assertEqual(sink.stats()["sink_dropped"], 3)
        self.assertTrue(any("HTTP 401" in m for m in logs), logs)

    def test_close_flushes_partial_batch(self):
        server, logs = _Server(), []
        try:
            sink = self.make(server, logs, batch_size=1000, flush_interval=60)
            for i in range(3):
                sink.emit(probe(i))
            sink.close()
            self.assertEqual(len(server.lines), 3)
        finally:
            server.close()

    def test_records_filter_and_make_sink_token_file(self):
        import os
        import tempfile
        server, logs = _Server(), []
        try:
            with tempfile.TemporaryDirectory() as d:
                tf = os.path.join(d, "token")
                with open(tf, "w") as f:
                    f.write("tok\n")
                sink = make_sink({"type": "http-lineprotocol", "url": server.url, "token_file": tf,
                                  "records": ["stats"], "flush_interval": 0.05}, log=logs.append)
            self.assertFalse(sink.wants(probe(1)))
            self.assertTrue(sink.wants({"type": "stats"}))
            sink.emit({"type": "stats", "time_ns": 1, "src_label": "X", "sent": 1})
            sink.close()
        finally:
            server.close()
        self.assertEqual(server.lines, ["owl_stats,src_label=X sent=1i 1"])
        self.assertEqual(server.requests[0][1]["Authorization"], "Token tok")


if __name__ == "__main__":
    unittest.main()
