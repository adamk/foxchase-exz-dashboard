"""Normalize live SIP/OPRA events without containing EXZ calculation logic.

Only SIP minute bars and selected-contract OPRA trades populate calculation
bars. Quotes are retained separately for the immediate midpoint display.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("provider timestamp is required")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def minute_time(value: Any) -> datetime:
    return parse_time(value).replace(second=0, microsecond=0)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def row_time(row: dict[str, Any]) -> datetime:
    return parse_time(row.get("t", row.get("timestamp")))


def rows_through(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    return [deepcopy(row) for row in rows if isinstance(row, dict) and row_time(row) <= cutoff]


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    provider_timestamp: str
    received_timestamp: str

    @property
    def midpoint(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid < 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2.0


class TradeMinuteBook:
    """Aggregate OPRA trades; deduplicate only when the provider supplies an ID."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
        self._seen_ids: set[tuple[str, str]] = set()
        self._arrival = 0

    @staticmethod
    def _provider_id(message: dict[str, Any]) -> str | None:
        for name in ("i", "id", "sequence", "seq"):
            value = message.get(name)
            if value is not None and str(value).strip():
                return f"{name}:{value}"
        return None

    def add(self, message: dict[str, Any]) -> tuple[datetime, bool]:
        symbol = str(message.get("S", "")).strip().upper()
        if not symbol:
            raise ValueError("trade symbol is required")
        stamp = parse_time(message.get("t"))
        price = float(message["p"])
        size = float(message["s"])
        if price < 0 or size < 0:
            raise ValueError("negative trade price or size")
        minute = stamp.replace(second=0, microsecond=0)
        provider_id = self._provider_id(message)
        identity = (symbol, provider_id) if provider_id is not None else None
        if identity is not None and identity in self._seen_ids:
            return minute, False
        if identity is not None:
            self._seen_ids.add(identity)
        self._arrival += 1
        self._events.setdefault((symbol, minute), []).append({
            "timestamp": stamp,
            "price": price,
            "size": size,
            "arrival": self._arrival,
        })
        return minute, True

    def bar(self, symbol: str, minute: datetime) -> dict[str, Any] | None:
        events = self._events.get((symbol.strip().upper(), minute), [])
        if not events:
            return None
        ordered = sorted(events, key=lambda row: (row["timestamp"], row["arrival"]))
        prices = [row["price"] for row in ordered]
        return {
            "t": iso_time(minute),
            "o": prices[0],
            "h": max(prices),
            "l": min(prices),
            "c": prices[-1],
            "v": sum(row["size"] for row in ordered),
        }

    def minutes(self, symbol: str) -> set[datetime]:
        normalized = symbol.strip().upper()
        return {minute for candidate, minute in self._events if candidate == normalized}

    def clear(self) -> None:
        self._events.clear()
        self._seen_ids.clear()
        self._arrival = 0


class LiveBarAdapter:
    """Own warm REST state plus stream-driven completed/amended minute bars."""

    def __init__(self, *, settle_seconds: float = 10.0) -> None:
        self.settle_seconds = float(settle_seconds)
        self.session_date = ""
        self.option_symbol = ""
        self.option_strike = 0.0
        self.offset = 0
        self.generation = 0
        self.base_payload: dict[str, Any] = {}
        self.spy_by_time: dict[datetime, dict[str, Any]] = {}
        self.option_by_time: dict[datetime, dict[str, Any]] = {}
        self.stream_spy: dict[datetime, dict[str, Any]] = {}
        self.option_trades = TradeMinuteBook()
        self.quotes: dict[str, LiveQuote] = {}
        self.connected = {"sip": False, "opra": False}
        self.stale = {"sip": True, "opra": True}
        self.coverage_start: dict[str, datetime | None] = {"sip": None, "opra": None}
        self.watermark: dict[str, datetime | None] = {"sip": None, "opra": None}
        self.finalized_spy: set[datetime] = set()
        self.finalized_option: set[datetime] = set()
        self.amended_spy: set[datetime] = set()
        self.amended_option: set[datetime] = set()
        self.last_applied: datetime | None = None
        self.rest_repairs = 0
        self.duplicate_events = 0
        self.out_of_order_events = 0
        self.late_trade_amendments = 0
        self.quote_updates = 0
        self.forming_events = 0
        self.last_repair_reason: str | None = None

    @staticmethod
    def stable_cutoff(now: datetime | None = None) -> datetime:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return current.replace(second=0, microsecond=0) - timedelta(minutes=1)

    @staticmethod
    def identity(payload: dict[str, Any]) -> tuple[str, str, float]:
        day = str(payload.get("session_date", ""))
        symbol = str(payload.get("option_symbol", "")).replace(" ", "").upper()
        try:
            strike = float(payload["option_strike"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("option_strike is required") from exc
        if not day or not symbol:
            raise ValueError("session_date and option_symbol are required")
        return day, symbol, strike

    def warm_start(self, payload: dict[str, Any], *, offset: int, cutoff: datetime | None = None) -> None:
        cutoff = cutoff or self.stable_cutoff()
        day, symbol, strike = self.identity(payload)
        self.session_date = day
        self.option_symbol = symbol
        self.option_strike = strike
        self.offset = int(offset)
        self.generation += 1
        self.base_payload = {
            key: deepcopy(value)
            for key, value in payload.items()
            if key not in {"spy_bars", "option_bars", "rvol_series", "_incremental"}
        }
        self.spy_by_time = {row_time(row): deepcopy(row) for row in rows_through(payload.get("spy_bars", []), cutoff)}
        self.option_by_time = {row_time(row): deepcopy(row) for row in rows_through(payload.get("option_bars", []), cutoff)}
        self.last_applied = max(set(self.spy_by_time) & set(self.option_by_time), default=None)
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        self.stream_spy.clear()
        self.option_trades.clear()
        self.quotes.clear()
        self.finalized_spy.clear()
        self.finalized_option.clear()
        self.amended_spy.clear()
        self.amended_option.clear()
        self.coverage_start = {"sip": None, "opra": None}
        self.watermark = {"sip": None, "opra": None}

    def mark_connection(self, feed: str, connected: bool) -> None:
        if feed not in self.connected:
            raise ValueError("feed must be sip or opra")
        self.connected[feed] = bool(connected)
        self.stale[feed] = not connected
        if not connected:
            self.coverage_start[feed] = None

    def _advance_watermark(self, feed: str, stamp: datetime) -> None:
        prior = self.watermark[feed]
        if prior is not None and stamp < prior:
            self.out_of_order_events += 1
        if prior is None or stamp > prior:
            self.watermark[feed] = stamp
        if self.coverage_start[feed] is None:
            self.coverage_start[feed] = stamp.replace(second=0, microsecond=0) + timedelta(minutes=1)

    @staticmethod
    def _stock_bar(message: dict[str, Any]) -> dict[str, Any]:
        row = {name: message[name] for name in ("t", "o", "h", "l", "c", "v") if name in message}
        if set(row) != {"t", "o", "h", "l", "c", "v"}:
            raise ValueError("SIP minute bar is incomplete")
        row["t"] = iso_time(minute_time(row["t"]))
        for name in ("o", "h", "l", "c", "v"):
            row[name] = float(row[name])
        return row

    def handle(self, feed: str, message: dict[str, Any], received_timestamp: Any) -> list[datetime]:
        kind = str(message.get("T", ""))
        symbol = str(message.get("S", "")).strip().upper()
        provider_value = message.get("t")
        relevant = feed == "sip" or (feed == "opra" and symbol == self.option_symbol)
        provider_time = parse_time(provider_value) if provider_value is not None else None
        if provider_time is not None and relevant:
            self._advance_watermark(feed, provider_time)
        if kind == "q":
            if not symbol or provider_time is None:
                raise ValueError("quote symbol and timestamp are required")
            quote = LiveQuote(
                symbol=symbol,
                bid=float(message["bp"]) if message.get("bp") is not None else None,
                ask=float(message["ap"]) if message.get("ap") is not None else None,
                bid_size=int(message["bs"]) if message.get("bs") is not None else None,
                ask_size=int(message["as"]) if message.get("as") is not None else None,
                provider_timestamp=iso_time(provider_time),
                received_timestamp=iso_time(parse_time(received_timestamp)),
            )
            self.quotes[symbol] = quote
            self.quote_updates += 1
            return self.drain_stabilized()
        if feed == "sip" and kind in {"b", "u"} and symbol == "SPY":
            row = self._stock_bar(message)
            minute = row_time(row)
            if self.stream_spy.get(minute) == row:
                self.duplicate_events += 1
            else:
                self.stream_spy[minute] = row
                if minute in self.finalized_spy:
                    self.amended_spy.add(minute)
            return self.drain_stabilized()
        if feed == "sip" and kind == "t":
            self.forming_events += 1
            return self.drain_stabilized()
        if feed == "opra" and kind == "t" and symbol == self.option_symbol:
            minute, added = self.option_trades.add(message)
            if not added:
                self.duplicate_events += 1
            elif minute in self.finalized_option:
                self.amended_option.add(minute)
                self.late_trade_amendments += 1
            else:
                self.forming_events += 1
            return self.drain_stabilized()
        return self.drain_stabilized()

    def _eligible(self, minute: datetime) -> bool:
        starts = list(self.coverage_start.values())
        watermarks = list(self.watermark.values())
        if any(value is None for value in starts + watermarks):
            return False
        if minute < max(value for value in starts if value is not None):
            return False
        stable_after = minute + timedelta(minutes=1, seconds=self.settle_seconds)
        return min(value for value in watermarks if value is not None) >= stable_after

    def drain_stabilized(self) -> list[datetime]:
        if any(self.stale.values()):
            return []
        changed: list[datetime] = []
        minutes = sorted(
            set(self.stream_spy)
            | self.option_trades.minutes(self.option_symbol)
            | self.amended_spy
            | self.amended_option
        )
        for minute in minutes:
            spy_amend = minute in self.amended_spy
            option_amend = minute in self.amended_option
            spy_new = minute in self.stream_spy and minute not in self.finalized_spy
            option_new = self.option_trades.bar(self.option_symbol, minute) is not None and minute not in self.finalized_option
            if not (spy_amend or option_amend or spy_new or option_new):
                continue
            if not (spy_amend or option_amend) and not self._eligible(minute):
                continue
            if spy_amend or spy_new:
                row = deepcopy(self.stream_spy[minute])
                if self.spy_by_time.get(minute) != row:
                    self.spy_by_time[minute] = row
                    changed.append(minute)
                self.finalized_spy.add(minute)
                self.amended_spy.discard(minute)
            if option_amend or option_new:
                row = self.option_trades.bar(self.option_symbol, minute)
                if row is not None and self.option_by_time.get(minute) != row:
                    self.option_by_time[minute] = row
                    changed.append(minute)
                self.finalized_option.add(minute)
                self.amended_option.discard(minute)
        joined = set(self.spy_by_time) & set(self.option_by_time)
        self.last_applied = max(joined, default=self.last_applied)
        return sorted(set(changed))

    def rest_repair(self, payload: dict[str, Any], *, reason: str, cutoff: datetime | None = None, reset_stream_coverage: bool = True) -> None:
        cutoff = cutoff or self.stable_cutoff()
        if self.identity(payload) != (self.session_date, self.option_symbol, self.option_strike):
            raise ValueError("REST repair contract identity mismatch")
        self.base_payload = {
            key: deepcopy(value)
            for key, value in payload.items()
            if key not in {"spy_bars", "option_bars", "rvol_series", "_incremental"}
        }
        self.spy_by_time = {row_time(row): deepcopy(row) for row in rows_through(payload.get("spy_bars", []), cutoff)}
        self.option_by_time = {row_time(row): deepcopy(row) for row in rows_through(payload.get("option_bars", []), cutoff)}
        self.last_applied = max(set(self.spy_by_time) & set(self.option_by_time), default=None)
        # A REST repair replaces the authoritative current-bar population.
        # Force the remote session to reinitialize so a removed/amended REST
        # minute cannot survive in its prior in-memory index.
        self.generation += 1
        if reset_stream_coverage:
            self._reset_stream_state()
        self.rest_repairs += 1
        self.last_repair_reason = str(reason)

    def sync_payload(self) -> dict[str, Any]:
        payload = deepcopy(self.base_payload)
        payload.update({
            "session_date": self.session_date,
            "option_symbol": self.option_symbol,
            "option_strike": self.option_strike,
            "spy_bars": [deepcopy(self.spy_by_time[key]) for key in sorted(self.spy_by_time)],
            "option_bars": [deepcopy(self.option_by_time[key]) for key in sorted(self.option_by_time)],
            "_incremental": {"action": "synchronize", "generation": self.generation},
        })
        return payload

    def quote_result(self) -> dict[str, Any] | None:
        quote = self.quotes.get(self.option_symbol)
        if quote is None:
            return None
        result = asdict(quote)
        result["midpoint"] = quote.midpoint
        return result

    def status(self) -> dict[str, Any]:
        return {
            "mode": "live_incremental",
            "generation": self.generation,
            "session_date": self.session_date,
            "option_symbol": self.option_symbol,
            "option_strike": self.option_strike,
            "offset": self.offset,
            "connected": dict(self.connected),
            "stale": dict(self.stale),
            "watermark": {key: iso_time(value) if value else None for key, value in self.watermark.items()},
            "last_applied": iso_time(self.last_applied) if self.last_applied else None,
            "rest_repairs": self.rest_repairs,
            "last_repair_reason": self.last_repair_reason,
            "quote_updates": self.quote_updates,
            "forming_events": self.forming_events,
            "late_trade_amendments": self.late_trade_amendments,
        }
