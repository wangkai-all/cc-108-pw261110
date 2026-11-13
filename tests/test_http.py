"""HTTP 端到端测试：在随机端口启动真实服务，走完整 REST 链路。"""
import json
import os
import sys
import threading
import time
import urllib.request
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import make_server


def _request(method, url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _wait_status(base, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, raw = _request("GET", f"{base}/api/runs/{run_id}/status")
        payload = json.loads(raw)
        if payload["status"] != "running":
            return payload
        time.sleep(0.05)
    raise AssertionError("运行超时未结束")


class TestHttpEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _submit(self, payload):
        status, raw = _request("POST", f"{self.base}/api/runs", payload)
        self.assertEqual(status, 202)
        return json.loads(raw)["run_id"]

    def test_config_and_health(self):
        status, raw = _request("GET", f"{self.base}/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        status, raw = _request("GET", f"{self.base}/api/config")
        self.assertEqual(status, 200)
        self.assertIn("customers", json.loads(raw)["sources"])

    def test_compare_run_download_and_failure_409(self):
        run_id = self._submit({
            "mode": "compare", "batch_size": 250,
            "select_columns": {"customers": ["region"], "orders": ["amount"]},
            "filters": [
                {"source": "customers", "column": "region", "op": "eq", "value": "华东"},
                {"source": "orders", "column": "status", "op": "eq", "value": "completed"},
            ],
        })
        payload = _wait_status(self.base, run_id)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["downloadable"])
        self.assertTrue(payload["result"]["comparison"]["rows_identical"])

        status, raw = _request("GET", f"{self.base}/api/runs/{run_id}/download")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # UTF-8 BOM

    def test_failed_batch_not_downloadable(self):
        run_id = self._submit({
            "mode": "pushdown", "batch_size": 200,
            "select_columns": {"customers": ["customer_id"], "orders": ["order_id"]},
            "filters": [], "fail_batch_no": {"orders": 1},
        })
        payload = _wait_status(self.base, run_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_kind"], "BatchFailed")
        self.assertFalse(payload["downloadable"])
        status, _ = _request("GET", f"{self.base}/api/runs/{run_id}/download")
        self.assertEqual(status, 409)

    def test_cancel_ends_run_without_result(self):
        run_id = self._submit({
            "mode": "pushdown", "batch_size": 100,
            "select_columns": {"customers": ["customer_id"], "orders": ["order_id"]},
            "filters": [],
            "injected_delays_ms": {"orders": 400},
            "timeouts_ms": {"orders": 30000},
        })
        time.sleep(0.2)
        status, _ = _request("POST", f"{self.base}/api/runs/{run_id}/cancel")
        self.assertEqual(status, 200)
        payload = _wait_status(self.base, run_id)
        self.assertEqual(payload["status"], "cancelled")
        self.assertFalse(payload["downloadable"])
        self.assertNotIn("result", payload)

    def test_bad_mode_returns_400(self):
        status, _ = _request("POST", f"{self.base}/api/runs", {"mode": "nope"})
        self.assertEqual(status, 400)

    def test_unknown_run_returns_404(self):
        status, _ = _request("GET", f"{self.base}/api/runs/ghost/status")
        self.assertEqual(status, 404)

    def test_path_traversal_blocked(self):
        for evil in ("/../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd"):
            status, _ = _request("GET", self.base + evil)
            self.assertIn(status, (403, 404))


if __name__ == "__main__":
    unittest.main(verbosity=2)
