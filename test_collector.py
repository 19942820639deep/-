import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collector
from collector import CollectorError, ema, limit_price, market_summary, price_limit_ratio


class CollectorLogicTests(unittest.TestCase):
    def test_price_limits(self):
        self.assertEqual(limit_price(10.01, price_limit_ratio("600000"), True), 11.01)
        self.assertEqual(limit_price(10.01, price_limit_ratio("300001"), True), 12.01)
        self.assertEqual(limit_price(10.01, price_limit_ratio("830001"), True), 13.01)

    def test_ema_length_and_direction(self):
        values = list(range(1, 61))
        ema12 = ema(values, 12)
        ema50 = ema(values, 50)
        self.assertEqual(len(ema12), len(values))
        self.assertGreater(ema12[-1], ema50[-1])

    def test_market_summary_excludes_st(self):
        stocks = [
            {"code": "600001", "name": "示例A", "last": 11.0, "prev_close": 10.0, "pct": 10.0, "high": 11.0, "low": 10.0, "amount": 1e9},
            {"code": "300001", "name": "示例B", "last": 12.0, "prev_close": 10.0, "pct": 20.0, "high": 12.0, "low": 10.0, "amount": 2e9},
            {"code": "600002", "name": "ST示例", "last": 10.5, "prev_close": 10.0, "pct": 5.0, "high": 10.5, "low": 10.0, "amount": 1e8},
        ]
        summary = market_summary(stocks)
        self.assertEqual(summary["non_st_count"], 2)
        self.assertEqual(summary["limit_up_non_st"], 2)
        self.assertEqual(summary["turnover_amount"], 3.1e9)

    def test_failure_writes_fresh_status_without_overwriting_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            latest_path = Path(tmp) / "latest.json"
            latest_path.write_text('{"generated_at":"old","trade_date":"2026-08-17"}', encoding="utf-8")
            with mock.patch.object(collector, "STATUS_PATH", status_path), \
                 mock.patch.object(collector, "LATEST_PATH", latest_path), \
                 mock.patch.object(collector, "build_snapshot", side_effect=CollectorError("Tencent rank failed")):
                self.assertEqual(collector.main(), 1)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["run_status"], "collector_failed")
            self.assertFalse(status["snapshot_updated"])
            self.assertEqual(status["previous_snapshot_generated_at"], "old")
            self.assertIn("Tencent rank failed", status["error"])
            self.assertEqual(json.loads(latest_path.read_text(encoding="utf-8"))["generated_at"], "old")


if __name__ == "__main__":
    unittest.main()
