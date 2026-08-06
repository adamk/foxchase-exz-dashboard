"""User-side Alpaca adapter for the ZWAP calculation API.

This file contains data transport only. It fetches bars with the user's own
Alpaca credentials, sends normalized bars to EC2, and prints the derived
response. It does not contain the ZWAP calculation logic.

Example:
  python3 zwap_client.py --date 2026-08-04 --offset 1 \
    --api-url https://exz-api.foxchasetrading.com/api/public/exz/historical
"""

from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from datetime import date as date_type, datetime, time as time_type, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def _cache_root() -> Path:
    return Path(os.getenv(
        "ZWAP_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "zwap_live_dashboard"),
    ))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _cached_payload(day: date_type, offset: int) -> dict | None:
    """Load an existing local session cache before making Alpaca requests."""
    root = _cache_root()
    day_text = day.isoformat()
    spy_data = _read_json(root / f"data_{day_text}_spy_bars.json")
    previous_spy_data = _read_json(root / f"data_{day_text}_previous_spy_bars.json")
    if not isinstance(spy_data, dict) or not isinstance(previous_spy_data, dict):
        return None
    spy_bars = spy_data.get("bars", [])
    previous_spy_bars = previous_spy_data.get("bars", [])
    if not isinstance(spy_bars, list) or not isinstance(previous_spy_bars, list):
        return None

    metadata = _read_json(root / f"data_{day_text}_meta.json") or {}
    try:
        atm = float(metadata["atm_strike"])
    except (KeyError, TypeError, ValueError):
        opening = next((row for row in spy_bars if row.get("t", "") >= _utc_at(day, 9, 30)), None)
        if not opening:
            return None
        opening_spot = float(opening.get("o") or opening.get("c"))
        atm = math.floor(opening_spot + 0.5)
    target = int(atm) + int(offset)

    candidates: list[tuple[str, list]] = []
    for path in root.glob(f"data_{day_text}_SPY{day:%y%m%d}C*_option_bars.json"):
        if "_previous_option_bars.json" in path.name:
            continue
        payload = _read_json(path)
        bars = payload.get("bars") if isinstance(payload, dict) else None
        if not isinstance(bars, dict):
            continue
        for symbol, rows in bars.items():
            if isinstance(rows, list) and rows:
                candidates.append((symbol, rows))
    base_payload = _read_json(root / f"data_{day_text}_option_bars.json")
    if isinstance(base_payload, dict) and isinstance(base_payload.get("bars"), dict):
        for symbol, rows in base_payload["bars"].items():
            if isinstance(rows, list) and rows:
                candidates.append((symbol, rows))
    if not candidates:
        return None
    symbol, option_rows = min(
        candidates,
        key=lambda item: (abs(int(item[0][-8:]) / 1000 - target), -len(item[1])),
    )

    previous_options = _read_json(root / f"data_{day_text}_{symbol}_previous_option_bars.json")
    if not isinstance(previous_options, dict):
        previous_options = _read_json(root / f"data_{day_text}_previous_option_bars.json")
    previous_by_symbol = previous_options.get("bars", {}) if isinstance(previous_options, dict) else {}
    previous_option_bars = previous_by_symbol.get(symbol, []) if isinstance(previous_by_symbol, dict) else []
    if not isinstance(previous_option_bars, list) or not previous_option_bars:
        return None
    return {
        "session_date": day_text,
        "option_symbol": symbol,
        "option_strike": int(int(symbol[-8:]) / 1000),
        "spy_bars": spy_bars,
        "option_bars": option_rows,
        "previous_spy_bars": previous_spy_bars,
        "previous_option_bars": previous_option_bars,
    }


def _utc_at(day: date_type, hour: int, minute: int = 0) -> str:
    eastern = ZoneInfo("America/New_York")
    value = datetime.combine(day, time_type(hour, minute), tzinfo=eastern).astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _get(path: str, params: dict) -> dict:
    key = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or os.getenv("ALPACA_KEY")
    secret = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET")
              or os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_SECRET"))
    if not key or not secret:
        raise RuntimeError("set your own APCA_API_KEY_ID and APCA_API_SECRET_KEY first")
    base = os.getenv("APCA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    request = Request(
        f"{base}{path}?{urlencode(params)}",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                 "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"Alpaca request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Alpaca request failed: {exc.reason}") from exc


def _occ_symbol(day: date_type, strike: int) -> str:
    return f"SPY{day:%y%m%d}C{strike * 1000:08d}"


def _download_payload(day: date_type, offset: int, use_cache: bool = True) -> dict:
    if use_cache:
        cached = _cached_payload(day, offset)
        if cached is not None:
            return cached
    stock_start, stock_end = _utc_at(day, 4), _utc_at(day, 12)
    stock = _get("/v2/stocks/SPY/bars", {
        "timeframe": "1Min", "start": stock_start, "end": stock_end,
        "limit": 10000, "feed": "sip", "adjustment": "raw",
    }).get("bars", [])
    if not stock:
        raise RuntimeError(f"no SPY bars returned for {day}")
    opening = [row for row in stock if row.get("t", "") >= _utc_at(day, 9, 30)]
    if not opening:
        raise RuntimeError(f"no regular-session opening bar returned for {day}")
    opening_spot = float(opening[0].get("o") or opening[0].get("c"))
    atm = math.floor(opening_spot + 0.5)
    target = atm + int(offset)
    symbols = [_occ_symbol(day, target + step) for step in (0, 1, -1, 2, -2)]
    options = _get("/v1beta1/options/bars", {
        "symbols": ",".join(symbols), "timeframe": "1Min",
        "start": stock_start, "end": stock_end, "limit": 10000,
    }).get("bars", {})
    available = [(symbol, rows) for symbol, rows in options.items() if rows]
    if not available:
        raise RuntimeError(f"no option bars returned near {target}C for {day}")
    symbol, option_rows = min(available, key=lambda item: (abs(int(item[0][-8:]) / 1000 - target),
                                                           -len(item[1])))

    prior_start, prior_end = _utc_at(day - timedelta(days=7), 9), _utc_at(day, 9)
    prior_stock_all = _get("/v2/stocks/SPY/bars", {
        "timeframe": "1Min", "start": prior_start, "end": prior_end,
        "limit": 10000, "feed": "sip", "adjustment": "raw",
    }).get("bars", [])
    prior_dates = sorted({row.get("t", "")[:10] for row in prior_stock_all
                          if row.get("t") and row["t"][:10] < day.isoformat()})
    if not prior_dates:
        raise RuntimeError(f"no prior SPY session returned for {day}")
    prior_date = prior_dates[-1]
    prior_stock = [row for row in prior_stock_all if row.get("t", "").startswith(prior_date)]
    prior_options = _get("/v1beta1/options/bars", {
        "symbols": symbol, "timeframe": "1Min",
        "start": _utc_at(date_type.fromisoformat(prior_date), 9),
        "end": _utc_at(date_type.fromisoformat(prior_date), 16), "limit": 10000,
    }).get("bars", {}).get(symbol, [])

    return {
        "session_date": day.isoformat(),
        "option_symbol": symbol,
        "option_strike": int(int(symbol[-8:]) / 1000),
        "spy_bars": stock,
        "option_bars": option_rows,
        "previous_spy_bars": prior_stock,
        "previous_option_bars": prior_options,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit user-owned Alpaca bars to the ZWAP compute API")
    parser.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    parser.add_argument("--offset", type=int, default=1, help="strike offset from opening ATM")
    parser.add_argument("--api-url", default=os.getenv(
        "ZWAP_API_URL",
        "https://exz-api.foxchasetrading.com/api/public/exz/historical",
    ))
    parser.add_argument("--token", default=os.getenv("ZWAP_HISTORICAL_TOKEN", ""))
    parser.add_argument("--output", type=Path, help="optional local JSON output path")
    args = parser.parse_args()
    public_historical = "/api/public/exz/historical" in args.api_url
    if not public_historical and not args.token:
        raise SystemExit("set --token or ZWAP_HISTORICAL_TOKEN for a private endpoint")
    payload = _download_payload(date_type.fromisoformat(args.date), max(-10, min(10, args.offset)))
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-ZWAP-Session": os.getenv("ZWAP_SESSION_ID", uuid.uuid4().hex),
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    request = Request(args.api_url, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        print(exc.read().decode("utf-8", errors="replace"), flush=True)
        return 1
    if args.output:
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "session_date": payload["session_date"],
        "option_symbol": result.get("option_symbol"),
        "series_bars": len(result.get("series", [])),
        "warmup_bars": result.get("warmup_bars"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
