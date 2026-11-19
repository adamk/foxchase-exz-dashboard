"""Local-only Alpaca SIP/OPRA WebSocket transport for EXZ Live."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any


MessageHandler = Callable[[str, dict[str, Any], str], Awaitable[None] | None]


def subscription(feed: str, option_symbols: list[str]) -> dict[str, Any]:
    symbols = sorted({str(value).strip().upper() for value in option_symbols if str(value).strip()})
    if feed == "sip":
        return {
            "action": "subscribe", "bars": ["SPY"], "updatedBars": ["SPY"],
            "quotes": ["SPY"], "trades": ["SPY"],
        }
    if feed == "opra":
        if not symbols or len(symbols) > 5 or "*" in symbols:
            raise ValueError("OPRA stream requires one to five explicit option symbols")
        return {"action": "subscribe", "quotes": symbols, "trades": symbols}
    raise ValueError("feed must be sip or opra")


class StreamError(RuntimeError):
    """A stream diagnostic safe to include in dashboard status."""


def _check_subscription(feed, message, requested):
    expected = requested.copy()
    if feed == "sip":
        # Alpaca includes these channels automatically for subscribed stock trades.
        for channel in ("corrections", "cancelErrors"):
            if message.get(channel, []) != []:
                expected[channel] = requested.get("trades", [])
    if any(value for name, value in message.items() if name != "T" and name not in expected):
        raise StreamError("unexpected subscription channel")
    for channel, symbols in expected.items():
        if channel == "action":
            continue
        actual = message.get(channel)
        if not isinstance(actual, list) or not all(isinstance(s, str) for s in actual):
            raise StreamError(f"invalid subscription acknowledgement for {channel}")
        if sorted(actual) != symbols:
            raise StreamError(f"subscription mismatch for {channel}")


def _normalize_msgpack_event(event, timestamp_type):
    if not isinstance(event, dict):
        raise StreamError("invalid stream message")

    def check_value(value):
        if isinstance(value, timestamp_type):
            raise StreamError("timestamp outside market event time field")
        if isinstance(value, (list, dict)):
            for child in (value.values() if isinstance(value, dict) else value):
                check_value(child)

    for field, value in event.items():
        if field == "t" and event.get("T") in ("q", "t", "b", "u") and isinstance(value, timestamp_type):
            stamp = datetime.fromtimestamp(value.seconds, timezone.utc)
            event[field] = f"{stamp:%Y-%m-%dT%H:%M:%S}.{value.nanoseconds:09d}Z"
        else:
            check_value(value)
    return event


class AlpacaLiveStream:
    STOCK_URL = "wss://stream.data.alpaca.markets/v2/sip"
    OPTION_URL = "wss://stream.data.alpaca.markets/v1beta1/opra"

    def __init__(self, key: str, secret: str, message_handler: MessageHandler, status_handler=None):
        if not key or not secret:
            raise ValueError("Alpaca credentials are required locally")
        self._key = key
        self._secret = secret
        self._message_handler = message_handler
        self._status_handler = status_handler

    async def run_once(self, feed: str, option_symbols: list[str], received_timestamp: Callable[[], str]):
        try:
            import websockets
        except ImportError as exc:
            raise StreamError("install the websockets dependency for EXZ Live") from exc
        requested = subscription(feed, option_symbols)
        url = self.STOCK_URL if feed == "sip" else self.OPTION_URL
        kwargs = {"ping_interval": 20, "ping_timeout": 30, "max_size": 2**20, "open_timeout": 10}
        encode = json.dumps
        if feed == "opra":
            try:
                import msgpack
            except ImportError as exc:
                raise StreamError("install msgpack for the OPRA stream") from exc

            parameters = inspect.signature(websockets.connect).parameters
            header = "additional_headers" if "additional_headers" in parameters else "extra_headers"
            kwargs[header] = {"Content-Type": "application/msgpack"}
            encode = msgpack.packb

        async def status(event):
            if self._status_handler is not None:
                result = self._status_handler(feed, event, received_timestamp())
                if result is not None:
                    await result

        async def authenticate(messages):
            async for message in messages:
                if message.get("T") == "success":
                    if message.get("msg") == "connected":
                        continue
                    if message.get("msg") == "authenticated":
                        return
                raise StreamError("unexpected authentication acknowledgement")
            raise StreamError("stream ended before authentication")

        stage = "connection"
        try:
            async with websockets.connect(url, **kwargs) as socket:
                messages = self._messages(socket)
                stage = "authentication"
                await socket.send(encode({"action": "auth", "key": self._key, "secret": self._secret}))
                await asyncio.wait_for(authenticate(messages), timeout=10)
                await status("authenticated")

                stage = "subscription"
                await socket.send(encode(requested))
                await status("subscription_sent")
                acknowledgement = await asyncio.wait_for(anext(messages), timeout=10)
                if acknowledgement.get("T") != "subscription":
                    raise StreamError("missing subscription acknowledgement")
                _check_subscription(feed, acknowledgement, requested)
                await status("subscribed")

                stage = "market data"
                async for message in messages:
                    if message.get("T") == "subscription":
                        _check_subscription(feed, message, requested)
                        continue
                    result = self._message_handler(feed, message, received_timestamp())
                    if result is not None:
                        await result
        except StreamError:
            raise
        except asyncio.TimeoutError:
            raise StreamError(f"{stage} timed out") from None
        except Exception:
            # Transport, parser and handler errors may contain provider payloads.
            raise StreamError(f"{stage} failed") from None

    async def _messages(self, socket):
        async for raw in socket:
            try:
                decoded = self.decode(raw)
            except StreamError:
                raise
            except Exception:
                raise StreamError("frame decode failed") from None
            batch = decoded if isinstance(decoded, list) else [decoded]
            for message in batch:
                if not isinstance(message, dict) or not isinstance(message.get("T"), str):
                    raise StreamError("invalid stream message")
                if message["T"] == "error":
                    code = message.get("code")
                    safe_code = code if type(code) is int and 100 <= code <= 999 else "unknown"
                    raise StreamError(f"provider error {safe_code}")
                yield message

    @staticmethod
    def decode(raw: Any) -> Any:
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, bytes):
            import msgpack

            def reject_extension(_code, _data):
                raise StreamError("unsupported MessagePack extension")

            decoded = msgpack.unpackb(raw, raw=False, timestamp=0, ext_hook=reject_extension)
            if isinstance(decoded, list):
                return [_normalize_msgpack_event(event, msgpack.Timestamp) for event in decoded]
            return _normalize_msgpack_event(decoded, msgpack.Timestamp)
        raise ValueError("unexpected Alpaca WebSocket message type")
