"""Exercise the public transport with synthetic frames and in-memory sockets."""

import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import msgpack

from alpaca_live_stream import AlpacaLiveStream, StreamError, subscription
from live_runtime import LiveRuntime
from live_stream_adapter import LiveBarAdapter
from test_live_stream_adapter import BASE, payload


SYMBOL = payload()["option_symbol"]
AUTHENTICATED = {"T": "success", "msg": "authenticated"}
RECEIVED = "2026-09-08T13:34:00Z"


def acknowledgement(feed):
    channels = subscription(feed, [SYMBOL]).copy()
    del channels["action"]
    return {"T": "subscription", **channels}


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.actions_at_receive = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, raw):
        message = json.loads(raw) if isinstance(raw, str) else msgpack.unpackb(raw)
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        self.actions_at_receive.append([m["action"] for m in self.sent])
        raw = next(self.frames, None)
        if raw is None:
            raise StopAsyncIteration
        return raw


class AlpacaStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_documented_sip_trade_acknowledgement(self):
        # Verbatim request and response from Alpaca's Real-time Stock Data, Trade Corrections.
        request = {"action": "subscribe", "trades": ["AAPL"]}
        reply = ('[{"T":"subscription","trades":["AAPL"],"quotes":[],"bars":[],"updatedBars":[],'
                 '"dailyBars":[],"statuses":[],"lulds":[],"corrections":["AAPL"],"cancelErrors":["AAPL"]}]')
        trade = {"T": "t", "S": "AAPL", "p": 100, "s": 1}
        socket = Socket([json.dumps([AUTHENTICATED]), reply, reply, json.dumps([trade])])
        statuses, received = [], []
        stream = AlpacaLiveStream("test", "test", lambda _, m, __: received.append(m),
                                  lambda _, e, __: statuses.append(e))
        with patch("alpaca_live_stream.subscription", return_value=request), patch("websockets.connect", return_value=socket):
            await stream.run_once("sip", [], lambda: RECEIVED)
        self.assertEqual(socket.sent[1], {"action": "subscribe", "trades": ["AAPL"]})
        self.assertEqual(statuses, ["authenticated", "subscription_sent", "subscribed"])
        self.assertEqual(received, [trade])

    async def test_sip_rejects_unrequested_symbols_or_channels_on_reacknowledgement(self):
        ack = dict(acknowledgement("sip"), corrections=["SPY"], cancelErrors=["SPY"])
        for update in ({"corrections": ["SPY", "AAPL"]}, {"cancelErrors": ["*"]},
                       {"corrections": ["SPY", "SPY"]}, {"cancelErrors": "SPY"}, {"lulds": ["SPY"]}):
            with self.subTest(update=update):
                socket = Socket([json.dumps([AUTHENTICATED]), json.dumps([ack]), json.dumps([{**ack, **update}])])
                statuses = []
                stream = AlpacaLiveStream("test", "test", lambda *_: self.fail("unexpected market event"),
                                          lambda _, e, __: statuses.append(e))
                with patch("websockets.connect", return_value=socket), self.assertRaises(StreamError):
                    await stream.run_once("sip", [], lambda: RECEIVED)
                self.assertIn("subscribed", statuses)

    async def test_paired_feeds_confirm_and_finalize_amended_minute(self):
        adapter = LiveBarAdapter(settle_seconds=5)
        adapter.warm_start(payload(), offset=1, cutoff=BASE + timedelta(minutes=1))
        runtime = LiveRuntime(lambda *_: payload())
        runtime._adapter = adapter
        quote = {"T": "q", "bp": 2.4, "ap": 2.6, "bs": 1, "as": 2, "bx": "X", "ax": "Y"}
        prior = "2026-09-08T13:31:10Z"
        target = "2026-09-08T13:32:00Z"
        later = "2026-09-08T13:33:06.123456789Z"
        bar = {"T": "b", "S": "SPY", "t": target, "o": 500, "h": 502, "l": 499, "c": 500, "v": 10}
        trade = {"T": "t", "S": SYMBOL, "s": 1}
        second = int((BASE + timedelta(minutes=2)).timestamp())
        last_quote_second = second + 66
        events = {
            "sip": [
                dict(quote, S="SPY", t=prior, c=["R"]),
                bar, dict(bar, T="u", c=501), dict(quote, S="SPY", t=later, c=["R"])
            ],
            "opra": [
                dict(quote, S=SYMBOL, t=prior, c="A"),
                dict(trade, p=3, t=msgpack.Timestamp(second, 999)),
                dict(trade, p=2, t=msgpack.Timestamp(second, 1)),
                dict(quote, S=SYMBOL, t=msgpack.Timestamp(last_quote_second, 123456789), c="A")
            ],
        }
        sockets = {}
        for feed, data in events.items():
            encode = json.dumps if feed == "sip" else msgpack.packb
            sockets[feed] = Socket([
                encode([{"T": "success", "msg": "connected"}, AUTHENTICATED]),
                encode([acknowledgement(feed), data[0]]),
                encode(data[1:]),
            ])

        def connect(url, *, additional_headers=None, **kwargs):
            if url == AlpacaLiveStream.STOCK_URL:
                self.assertIsNone(additional_headers)
                return sockets["sip"]
            self.assertEqual(url, AlpacaLiveStream.OPTION_URL)
            self.assertEqual(additional_headers, {"Content-Type": "application/msgpack"})
            return sockets["opra"]

        def run(feed):
            stream = AlpacaLiveStream("test", "test", runtime._handle, runtime._stream_status)
            return stream.run_once(feed, [SYMBOL], lambda: RECEIVED)

        with patch("websockets.connect", connect):
            await asyncio.gather(run("sip"), run("opra"))

        for feed, socket in sockets.items():
            self.assertEqual(socket.actions_at_receive[:2], [["auth"], ["auth", "subscribe"]])
            self.assertEqual(socket.sent[1], subscription(feed, [SYMBOL]))
        self.assertEqual(sockets["sip"].sent[1]["updatedBars"], ["SPY"])
        self.assertEqual(adapter.connected, {"sip": True, "opra": True})
        self.assertEqual(adapter.sync_payload()["option_bars"][-1], {
            "t": target, "o": 2, "h": 3, "l": 2, "c": 3, "v": 2,
        })
        self.assertEqual(adapter.sync_payload()["spy_bars"][-1]["c"], 501)
        self.assertEqual(adapter.quotes["SPY"].conditions, ["R"])
        quote_result = adapter.quote_result()
        self.assertEqual(quote_result["provider_timestamp"], later)
        self.assertEqual(quote_result["conditions"], "A")
        self.assertEqual((quote_result["bid_exchange"], quote_result["ask_exchange"]), ("X", "Y"))

    async def test_rejected_frames_never_reach_market_handler(self):
        ack = acknowledgement("opra")
        cases = [
            ([[AUTHENTICATED], [dict(ack, corrections=[SYMBOL])]], "unexpected subscription channel", False),
            ([[AUTHENTICATED], [dict(ack, cancelErrors=[SYMBOL])]], "unexpected subscription channel", False),
            ([[{"T": "error", "code": 406, "msg": "untrusted-text"}]], "provider error 406", False),
            ([[AUTHENTICATED], [dict(ack, quotes=["WRONG"])]], "subscription mismatch", False),
            ([[AUTHENTICATED], [dict(ack, bars=["*"])]], "unexpected subscription channel", False),
            ([[AUTHENTICATED], [dict(ack, trades=[SYMBOL, SYMBOL])]], "subscription mismatch", False),
            ([[AUTHENTICATED], [dict(ack, quotes=SYMBOL)]], "invalid subscription", False),
            ([[AUTHENTICATED], [{"T": "q"}]], "missing subscription", False),
            ([[AUTHENTICATED], [ack], [{"T": "error", "code": "untrusted-text"}]], "provider error unknown", True),
        ]
        for frames, diagnostic, subscribed in cases:
            with self.subTest(diagnostic=diagnostic):
                socket = Socket(msgpack.packb(frame) for frame in frames)
                statuses, received = [], []
                stream = AlpacaLiveStream("test", "test", lambda *m: received.append(m),
                                          lambda _, event, __: statuses.append(event))

                def connect(url, *, extra_headers=None, **kwargs):
                    self.assertEqual(extra_headers, {"Content-Type": "application/msgpack"})
                    return socket

                with patch("websockets.connect", connect), self.assertRaisesRegex(StreamError, diagnostic) as error:
                    await stream.run_once("opra", [SYMBOL], lambda: RECEIVED)
                self.assertNotIn("untrusted-text", str(error.exception))
                self.assertEqual("subscribed" in statuses, subscribed)
                self.assertEqual(received, [])

    async def test_post_ack_errors_expose_only_the_failed_stage(self):
        def fail_handler(*args):
            raise ValueError("untrusted-text")

        for frame, diagnostic in ((b"\xc1", "frame decode failed"),
                                  (msgpack.packb([{"T": "q"}]), "market data failed")):
            with self.subTest(diagnostic=diagnostic):
                socket = Socket([msgpack.packb([AUTHENTICATED]), msgpack.packb([acknowledgement("opra")]), frame])
                with patch("websockets.connect", return_value=socket), self.assertRaises(StreamError) as error:
                    await AlpacaLiveStream("test", "test", fail_handler).run_once("opra", [SYMBOL], lambda: RECEIVED)
                self.assertEqual(str(error.exception), diagnostic)

    async def test_runtime_marks_only_the_disconnected_feed_stale(self):
        runtime = LiveRuntime(lambda *_: payload())
        adapter = LiveBarAdapter()
        adapter.warm_start(payload(), offset=1, cutoff=BASE + timedelta(minutes=1))
        adapter.mark_connection("sip", True)
        runtime._adapter = adapter
        runtime._stop_event = asyncio.Event()
        socket = Socket(msgpack.packb([frame]) for frame in (
            AUTHENTICATED, acknowledgement("opra"), {"T": "error", "code": 407, "msg": "untrusted-text"},
        ))

        def credentials():
            runtime._stop_event.set()  # End the retry loop after this attempt.
            return "test", "test"

        with patch.object(runtime, "_credentials", credentials), patch("websockets.connect", return_value=socket), \
                patch("asyncio.sleep", new_callable=AsyncMock):
            await runtime._feed_loop("opra")

        status = runtime.status()
        self.assertEqual(adapter.connected, {"sip": True, "opra": False})
        self.assertEqual(adapter.stale, {"sip": False, "opra": True})
        self.assertIn("provider error 407", status["last_error"])
        self.assertNotIn("untrusted-text", repr(status))

    def test_malformed_quote_does_not_advance_either_feed(self):
        adapter = LiveBarAdapter()
        adapter.warm_start(payload(), offset=1, cutoff=BASE + timedelta(minutes=1))
        for feed, symbol, conditions in (("sip", "SPY", "R"), ("opra", SYMBOL, ["A"])):
            with self.subTest(feed=feed), self.assertRaisesRegex(ValueError, "conditions"):
                adapter.handle(feed, {"T": "q", "S": symbol, "t": RECEIVED, "c": conditions}, RECEIVED)
        self.assertEqual(adapter.watermark, {"sip": None, "opra": None})
        self.assertFalse(adapter.quotes)

    def test_wildcard_is_not_an_explicit_option_contract(self):
        with self.assertRaises(ValueError):
            subscription("opra", ["*"])

    def test_timestamp_extension_scope_and_precision(self):
        for seconds, ns in ((0, 0), (1, 7), (-1, 999999999)):
            stamp = msgpack.Timestamp(seconds, ns)
            decoded = AlpacaLiveStream.decode(msgpack.packb({"T": "t", "t": stamp}))
            self.assertTrue(decoded["t"].endswith(f".{ns:09d}Z"))
        stamp = msgpack.Timestamp(1, 1)
        for event in ({"T": "q", "c": stamp}, {"T": "q", "nested": [{"T": "t", "t": stamp}]},
                      {"T": "success", "t": stamp}, {"T": "t", "t": msgpack.ExtType(1, b"x")}):
            with self.subTest(event=event), self.assertRaises(StreamError):
                AlpacaLiveStream.decode(msgpack.packb(event))


if __name__ == "__main__":
    unittest.main()
