# -*- coding: utf-8 -*-
"""Tests for otel_sink. Run: python -m unittest"""
import json
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import otel_sink as os_  # noqa: E402


def event(session, cost, inp, cache, name="api_request"):
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [
        {"key": "event.name", "value": {"stringValue": name}},
        {"key": "session.id", "value": {"stringValue": session}},
        {"key": "cost_usd", "value": {"doubleValue": cost}},
        {"key": "input_tokens", "value": {"intValue": str(inp)}},
        {"key": "cache_read_tokens", "value": {"intValue": str(cache)}}]}]}]}]}


class OtelSinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_dir = os_.DIR
        os_.DIR = pathlib.Path(tempfile.mkdtemp(prefix="otel_sink_test_"))
        s = socket.socket()
        s.bind((os_.HOST, 0))
        cls.port = s.getsockname()[1]
        s.close()
        threading.Thread(target=os_.serve, args=(cls.port,), daemon=True).start()
        for _ in range(100):
            if os_.is_up(cls.port):
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(os_.DIR, ignore_errors=True)
        os_.DIR = cls.old_dir

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request("http://%s:%d%s" % (os_.HOST, self.port, path), data=body,
                                     headers={"Content-Type": ctype})
        return urllib.request.urlopen(req, timeout=5)

    def logs_file(self):
        return os_.DIR / ("%s_logs.jsonl" % time.strftime("%Y-%m-%d"))

    def test_1_receives_writes_and_sums(self):
        self.assertTrue(os_.is_up(self.port))
        for _ in range(2):
            self.assertEqual(self.post("/v1/logs", json.dumps(event("s1", 0.25, 10, 1000)).encode()).status, 200)
        self.post("/v1/logs", json.dumps(event("s1", 9.0, 1, 1, name="tool_result")).encode())
        r = self.post("/v1/logs", b"\x0a\x00protobuf", "application/x-protobuf")
        self.assertEqual(r.status, 200)                      # garbage is answered 200 and dropped
        self.post("/v1/metrics", json.dumps({"resourceMetrics": []}).encode())
        self.assertEqual(len(self.logs_file().read_text(encoding="utf-8").splitlines()), 3)
        s = os_.summary()["s1"]
        self.assertAlmostEqual(s["cost_usd"], 0.5)
        self.assertEqual((s["input"], s["cache_read"], s["requests"]), (20, 2000, 2))
        self.assertTrue((os_.DIR / ("%s_metrics.jsonl" % time.strftime("%Y-%m-%d"))).exists())

    def test_2_unknown_path_is_not_written(self):
        before = len(list(os_.DIR.glob("*.jsonl")))
        self.post("/v1/other", b"{}")
        self.assertEqual(len(list(os_.DIR.glob("*.jsonl"))), before)

    def test_3_ensure_does_nothing_when_up_and_serve_yields_busy_port(self):
        old = os_.PORT
        os_.PORT = self.port
        try:
            self.assertEqual(os_.ensure(), 0)
        finally:
            os_.PORT = old
        self.assertEqual(os_.serve(self.port), 0)           # port taken: returns instead of raising
        with self.assertRaises(OSError):                     # and a second server cannot bind it at all
            os_._ExclusiveServer((os_.HOST, self.port), os_._H).server_close()

    def test_4_summary_of_missing_day_is_empty(self):
        self.assertEqual(os_.summary("1999-01-01"), {})

    def test_5_negative_control_filter_matters(self):
        # if the summary did not filter by event.name, the tool_result event (cost 9.0) would be counted
        orig = os_._attrs
        os_._attrs = lambda items: dict(orig(items), **{"event.name": "api_request"})
        try:
            self.assertNotAlmostEqual(os_.summary().get("s1", {}).get("cost_usd", 0), 0.5)
        finally:
            os_._attrs = orig


if __name__ == "__main__":
    unittest.main()
