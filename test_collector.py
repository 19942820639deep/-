import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock

import collector
from collector import (CollectorError, ema, fetch_hithink_price_cross, get_ths_json,
                       limit_price, market_summary, midday_close_profile,
                       price_limit_ratio, reprice_cached_sector_breadth,
                       thscode_to_tx, tx_to_thscode)


class CollectorLogicTests(unittest.TestCase):
    def test_symbol_conversion_for_all_a_share_venues(self):
        self.assertEqual(tx_to_thscode("sh600000"), "600000.SH")
        self.assertEqual(tx_to_thscode("sz000001"), "000001.SZ")
        self.assertEqual(tx_to_thscode("bj920001"), "920001.BJ")
        self.assertEqual(thscode_to_tx("688001.SH"), "sh688001")

    def test_hithink_key_is_header_only(self):
        class FakeClient:
            def __init__(self): self.call = None
            def get(self, url, params=None, timeout=25, referer=None, headers=None):
                self.call = {"url":url,"params":params,"headers":headers}
                return b'{"code":0,"message":"ok","data":{"item":[]}}'
        client=FakeClient()
        data=get_ths_json(client,"/api/test",{"ticker":"600000"},"secret-value",retries=1)
        self.assertEqual(data["item"],[])
        self.assertEqual(client.call["headers"]["X-api-key"],"secret-value")
        self.assertNotIn("secret-value",client.call["url"])
        self.assertNotIn("secret-value",json.dumps(client.call["params"]))

    def test_hithink_price_cross_requires_thirty_matching_quotes(self):
        candidates=[{"symbol":f"sh{600000+i:06d}","last":10+i/100} for i in range(30)]
        rows=[{"thscode":tx_to_thscode(x["symbol"]),"last_price":x["last"]} for x in candidates]
        with mock.patch.object(collector,"ths_snapshot_batches",return_value=(rows,[1])):
            result=fetch_hithink_price_cross(mock.Mock(),"key",candidates)
        self.assertTrue(result["pass"])
        self.assertEqual(result["matches"],30)
        self.assertEqual(result["price_diff_p95"],0.0)

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

    def test_cached_sector_memberships_are_repriced_with_live_quotes(self):
        previous={"trade_date":"2026-08-26","generated_at":"2026-08-26T12:00:00+08:00",
                  "sector_data":{"top_sectors":[]}}
        stocks=[]
        for sector_index in range(6):
            members=[]
            for member_index in range(3):
                code=f"60{sector_index}{member_index:03d}"; symbol=f"sh{code}"
                members.append({"symbol":symbol,"code":code,"name":f"股票{sector_index}-{member_index}"})
                stocks.append({"symbol":symbol,"code":code,"name":f"股票{sector_index}-{member_index}",
                               "pct":float(sector_index+member_index+1),"amount":1e9+member_index,"last":10.0})
            previous["sector_data"]["top_sectors"].append(
                {"thscode":f"884{sector_index:03d}.TI","name":f"板块{sector_index}",
                 "tag":"industry","constituents":members})
        result=reprice_cached_sector_breadth(previous,stocks,"2026-08-26")
        self.assertTrue(result["pass"])
        self.assertEqual(result["verified_count"],6)
        self.assertFalse(result["supports_new_sector_discovery"])
        self.assertEqual(result["top_sectors"][0]["name"],"板块5")
        self.assertEqual(result["top_sectors"][0]["priced_count"],3)

    def test_cached_sector_fallback_rejects_other_trade_day(self):
        previous={"trade_date":"2026-08-25","sector_data":{"top_sectors":[]}}
        result=reprice_cached_sector_breadth(previous,[],"2026-08-26")
        self.assertFalse(result["pass"])
        self.assertEqual(result["status"],"cache_unavailable")

    def test_midday_accepts_tencent_clock_advance_when_sina_anchors_close(self):
        tz = ZoneInfo("Asia/Shanghai")
        generated = datetime(2026, 8, 18, 11, 55, tzinfo=tz)
        primary_latest = datetime(2026, 8, 18, 11, 56, 58, tzinfo=tz)
        secondary_anchor = datetime(2026, 8, 18, 11, 30, tzinfo=tz)
        self.assertTrue(midday_close_profile(generated, primary_latest, secondary_anchor, True, True))

    def test_midday_accepts_delayed_action_after_noon(self):
        tz = ZoneInfo("Asia/Shanghai")
        generated = datetime(2026, 8, 18, 12, 5, tzinfo=tz)
        primary_latest = datetime(2026, 8, 18, 12, 4, 58, tzinfo=tz)
        secondary_anchor = datetime(2026, 8, 18, 11, 30, tzinfo=tz)
        self.assertTrue(midday_close_profile(generated, primary_latest, secondary_anchor, True, True))

    def test_midday_rejects_previous_trade_day_anchor(self):
        tz = ZoneInfo("Asia/Shanghai")
        generated = datetime(2026, 8, 18, 12, 0, tzinfo=tz)
        primary_latest = datetime(2026, 8, 18, 11, 59, tzinfo=tz)
        stale_anchor = datetime(2026, 8, 17, 11, 30, tzinfo=tz)
        self.assertFalse(midday_close_profile(generated, primary_latest, stale_anchor, True, True))

    def test_midday_rejects_cross_source_price_mismatch(self):
        tz = ZoneInfo("Asia/Shanghai")
        generated = datetime(2026, 8, 18, 12, 0, tzinfo=tz)
        primary_latest = datetime(2026, 8, 18, 11, 59, tzinfo=tz)
        secondary_anchor = datetime(2026, 8, 18, 11, 30, tzinfo=tz)
        self.assertFalse(midday_close_profile(generated, primary_latest, secondary_anchor, True, False))

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
