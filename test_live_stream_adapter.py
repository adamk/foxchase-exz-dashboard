from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from live_stream_adapter import LiveBarAdapter
from live_runtime import LiveRuntime


BASE = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)


def bar(stamp: datetime, close: float, volume: float = 100.0) -> dict:
    return {
        "t": stamp.isoformat().replace("+00:00", "Z"),
        "o": close, "h": close, "l": close, "c": close, "v": volume,
    }


def payload(symbol: str = "SPY260908C00501000", strike: float = 501.0) -> dict:
    prior = [BASE - timedelta(minutes=70 - index) for index in range(60)]
    current = [BASE + timedelta(minutes=index) for index in range(2)]
    return {
        "session_date": "2026-09-08",
        "option_symbol": symbol,
        "option_strike": strike,
        "previous_spy_bars": [bar(stamp, 500 + index / 100) for index, stamp in enumerate(prior)],
        "previous_option_bars": [bar(stamp, 2 + index / 1000) for index, stamp in enumerate(prior)],
        "spy_bars": [bar(stamp, 501 + index / 10, 1000) for index, stamp in enumerate(current)],
        "option_bars": [bar(stamp, 2.5 + index / 10, 10) for index, stamp in enumerate(current)],
    }


class LiveBarAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = LiveBarAdapter(settle_seconds=5)
        self.adapter.warm_start(payload(), offset=1, cutoff=BASE + timedelta(minutes=1))

    def connect(self) -> None:
        self.adapter.mark_connection("sip", True)
        self.adapter.mark_connection("opra", True)
        for feed in ("sip", "opra"):
            self.adapter.handle(feed, {
                "T": "q", "S": "SPY" if feed == "sip" else self.adapter.option_symbol,
                "t": "2026-09-08T13:31:05Z", "bp": 1, "ap": 2, "bs": 1, "as": 1,
            }, "2026-09-08T13:31:05.1Z")

    def add_minute(self) -> None:
        self.adapter.handle("sip", {
            "T": "b", "S": "SPY", "t": "2026-09-08T13:32:00Z",
            "o": 501.2, "h": 501.3, "l": 501.1, "c": 501.25, "v": 1100,
        }, "2026-09-08T13:33:00Z")
        self.adapter.handle("opra", {
            "T": "t", "S": self.adapter.option_symbol,
            "t": "2026-09-08T13:32:10Z", "p": 2.7, "s": 4,
        }, "2026-09-08T13:32:10.1Z")

    def advance(self) -> None:
        for feed in ("sip", "opra"):
            self.adapter.handle(feed, {
                "T": "q", "S": "SPY" if feed == "sip" else self.adapter.option_symbol,
                "t": "2026-09-08T13:33:06Z", "bp": 1, "ap": 2, "bs": 1, "as": 1,
            }, "2026-09-08T13:33:06Z")

    def test_warm_payload_is_exact_and_marked_incremental(self):
        result = self.adapter.sync_payload()
        self.assertEqual(result["spy_bars"], payload()["spy_bars"])
        self.assertEqual(result["option_bars"], payload()["option_bars"])
        self.assertEqual(result["_incremental"], {"action": "synchronize", "generation": 1})

    def test_quote_never_changes_calculation_bars(self):
        before = self.adapter.sync_payload()
        self.adapter.mark_connection("opra", True)
        self.adapter.handle("opra", {
            "T": "q", "S": self.adapter.option_symbol,
            "t": "2026-09-08T13:31:30Z", "bp": 2.4, "ap": 2.6, "bs": 8, "as": 9,
        }, "2026-09-08T13:31:30.1Z")
        after = self.adapter.sync_payload()
        self.assertEqual(after["spy_bars"], before["spy_bars"])
        self.assertEqual(after["option_bars"], before["option_bars"])
        self.assertEqual(self.adapter.quote_result()["midpoint"], 2.5)

    def test_forming_minute_waits_then_is_published(self):
        self.connect()
        self.add_minute()
        self.assertEqual(len(self.adapter.sync_payload()["option_bars"]), 2)
        self.advance()
        result = self.adapter.sync_payload()
        self.assertEqual(result["spy_bars"][-1]["t"], "2026-09-08T13:32:00Z")
        self.assertEqual(result["option_bars"][-1]["v"], 4.0)

    def test_provider_id_duplicate_is_noop_but_equal_unidentified_prints_count(self):
        self.connect()
        self.add_minute()
        identified = {
            "T": "t", "S": self.adapter.option_symbol,
            "t": "2026-09-08T13:32:20Z", "p": 2.8, "s": 3, "i": "exact-1",
        }
        self.adapter.handle("opra", identified, "2026-09-08T13:32:20.1Z")
        self.adapter.handle("opra", identified, "2026-09-08T13:32:20.2Z")
        unidentified = {
            "T": "t", "S": self.adapter.option_symbol,
            "t": "2026-09-08T13:32:30Z", "p": 2.9, "s": 2,
        }
        self.adapter.handle("opra", unidentified, "2026-09-08T13:32:30.1Z")
        self.adapter.handle("opra", unidentified, "2026-09-08T13:32:30.2Z")
        self.advance()
        self.assertEqual(self.adapter.sync_payload()["option_bars"][-1]["v"], 11.0)
        self.assertEqual(self.adapter.duplicate_events, 1)

    def test_late_trade_amends_published_minute(self):
        self.connect()
        self.add_minute()
        self.advance()
        self.adapter.handle("opra", {
            "T": "t", "S": self.adapter.option_symbol,
            "t": "2026-09-08T13:32:40Z", "p": 2.8, "s": 2,
        }, "2026-09-08T13:33:08Z")
        row = self.adapter.sync_payload()["option_bars"][-1]
        self.assertEqual((row["h"], row["c"], row["v"]), (2.8, 2.8, 6.0))
        self.assertEqual(self.adapter.late_trade_amendments, 1)

    def test_repair_is_exact_and_contract_drift_fails(self):
        repaired = payload()
        repaired["spy_bars"][-1] = dict(repaired["spy_bars"][-1], c=501.75)
        generation = self.adapter.generation
        self.adapter.rest_repair(repaired, reason="test", cutoff=BASE + timedelta(minutes=1))
        self.assertEqual(self.adapter.sync_payload()["spy_bars"][-1]["c"], 501.75)
        self.assertEqual(self.adapter.generation, generation + 1)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.adapter.rest_repair(
                payload("SPY260908C00502000", 502), reason="bad",
                cutoff=BASE + timedelta(minutes=1),
            )

    def test_live_responses_carry_fresh_observability_timestamps(self):
        runtime = LiveRuntime(lambda _day, _offset: payload())
        runtime._adapter = self.adapter

        with patch("live_runtime._rvol_series", return_value=[]):
            snapshot = runtime.payload()
        quote = runtime.quote()

        self.assertEqual(snapshot["snapshot_timestamp"][-1], "Z")
        self.assertEqual(quote["response_timestamp"][-1], "Z")
        datetime.fromisoformat(snapshot["snapshot_timestamp"].replace("Z", "+00:00"))
        datetime.fromisoformat(quote["response_timestamp"].replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
