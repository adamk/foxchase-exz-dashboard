"""Local-only Alpaca SIP/OPRA WebSocket transport for EXZ Live."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any


MessageHandler = Callable[[str, dict[str, Any], str], Awaitable[None] | None]


def subscription(feed: str, option_symbols: list[str]) -> dict[str, Any]:
    symbols = sorted({str(value).strip().upper() for value in option_symbols if str(value).strip()})
    if feed == "sip":
        return {"action": "subscribe", "bars": ["SPY"], "quotes": ["SPY"], "trades": ["SPY"]}
    if feed == "opra":
        if not symbols or len(symbols) > 5:
            raise ValueError("OPRA stream requires one to five explicit option symbols")
        return {"action": "subscribe", "quotes": symbols, "trades": symbols}
    raise ValueError("feed must be sip or opra")


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
            raise RuntimeError("install the websockets dependency for EXZ Live") from exc
        url = self.STOCK_URL if feed == "sip" else self.OPTION_URL
        async with websockets.connect(url, ping_interval=20, ping_timeout=30, max_size=2**20) as socket:
            auth = {"action": "auth", "key": self._key, "secret": self._secret}
            subscribe = subscription(feed, option_symbols)
            if feed == "opra":
                try:
                    import msgpack
                except ImportError as exc:
                    raise RuntimeError("install msgpack for the OPRA stream") from exc
                await socket.send(msgpack.packb(auth, use_bin_type=True))
                await socket.send(msgpack.packb(subscribe, use_bin_type=True))
            else:
                await socket.send(json.dumps(auth))
                await socket.send(json.dumps(subscribe))
            if self._status_handler is not None:
                result = self._status_handler(feed, "subscription_sent", received_timestamp())
                if result is not None:
                    await result
            async for raw in socket:
                decoded = self.decode(raw)
                messages = decoded if isinstance(decoded, list) else [decoded]
                for message in messages:
                    if isinstance(message, dict):
                        result = self._message_handler(feed, message, received_timestamp())
                        if result is not None:
                            await result

    @staticmethod
    def decode(raw: Any) -> Any:
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, bytes):
            try:
                import msgpack
            except ImportError as exc:
                raise RuntimeError("install msgpack for the OPRA stream") from exc
            return msgpack.unpackb(raw, raw=False, timestamp=3)
        raise ValueError("unexpected Alpaca WebSocket message type")
