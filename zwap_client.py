"""User-side Alpaca adapter for the ZWAP calculation API.

This file contains data transport only. It fetches bars with the user's own
Alpaca credentials, sends normalized bars to the private compute service, and prints the derived
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
import re
import sys
import uuid
from functools import lru_cache
from datetime import date as date_type, datetime, time as time_type, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


RVOL_LOOKBACK_SESSIONS = 10
RVOL_INTERVAL_MINUTES = 5
# Observed with this source's dated option-bar API: Jan 17 returned no bars;
# Jan 18 returned both same-day and continuing standard-contract bars.
# This is an observed coverage boundary, not a promise of gap-free sessions.
HISTORICAL_OPTIONS_START = date_type(2024, 1, 18)
RVOL_COLORS = (
    (2.2, "purple"),
    (1.8, "red"),
    (1.2, "orange"),
    (0.8, "green"),
    (0.4, "blue"),
    (-math.inf, "grey"),
)


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

    # A cached session must match the requested strike exactly.  Falling back
    # to the nearest cached contract here makes the strike buttons appear to
    # do nothing (for example, 4/17's -$1 request for 705C silently returned
    # the cached 706C).  If the exact contract is not cached, return None so
    # _download_payload() goes to Alpaca and requests it.
    target_symbol = _occ_symbol(day, target)
    exact = next(((symbol, rows) for symbol, rows in candidates
                  if symbol == target_symbol), None)
    if exact is None:
        return None
    symbol, option_rows = exact

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


def _latest_prior_rth_session(rows: list[dict], day: date_type) -> tuple[date_type, list[dict]] | None:
    """Select the latest prior weekday with an Eastern-time RTH bar.

    Alpaca timestamps are UTC.  Grouping on the timestamp's YYYY-MM-DD prefix
    can misclassify Friday evening extended-hours bars as a Saturday session.
    """
    eastern = ZoneInfo("America/New_York")
    localized: list[tuple[dict, datetime]] = []
    rth_dates: set[date_type] = set()
    for row in rows:
        try:
            stamp = str(row.get("t", "")).replace("Z", "+00:00")
            local = datetime.fromisoformat(stamp).astimezone(eastern)
        except (TypeError, ValueError):
            continue
        session = local.date()
        if session >= day or session.weekday() >= 5:
            continue
        localized.append((row, local))
        minute = local.hour * 60 + local.minute
        if 9 * 60 + 30 <= minute < 16 * 60:
            rth_dates.add(session)
    if not rth_dates:
        return None
    prior_date = max(rth_dates)
    return prior_date, [row for row, local in localized if local.date() == prior_date]


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


def _rvol_color(value: float) -> str:
    return next(color for threshold, color in RVOL_COLORS if value >= threshold)


def _eastern_bar(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York")
        )
    except (KeyError, TypeError, ValueError):
        return None


@lru_cache(maxsize=32)
def _rvol_baseline(day: date_type) -> dict[tuple[int, int], tuple[float, int]]:
    """Average each RTH five-minute SPY slot across ten prior sessions."""
    rows = _get("/v2/stocks/SPY/bars", {
        "timeframe": "1Min",
        "start": _utc_at(day - timedelta(days=24), 9, 30),
        "end": _utc_at(day, 9, 29),
        "limit": 10000,
        "feed": "sip",
        "adjustment": "all",
        # Alpaca includes extended-hours rows toward the 10,000-row limit.
        # Newest-first guarantees that the selected sessions are recent.
        "sort": "desc",
    }).get("bars", [])
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        stamp = _eastern_bar(row)
        if stamp is None or stamp.date() >= day or not time_type(9, 30) <= stamp.time() < time_type(16):
            continue
        try:
            parsed.append((stamp, float(row.get("v", 0))))
        except (TypeError, ValueError):
            continue
    sessions = sorted({stamp.date() for stamp, _ in parsed})[-RVOL_LOOKBACK_SESSIONS:]
    allowed = set(sessions)
    minute_rows: dict[tuple[date_type, int, int], list[float]] = {}
    for stamp, volume in parsed:
        if stamp.date() in allowed:
            minute_rows.setdefault(
                (stamp.date(), stamp.hour, stamp.minute // RVOL_INTERVAL_MINUTES * RVOL_INTERVAL_MINUTES), []
            ).append(volume)
    slots: dict[tuple[int, int], list[float]] = {}
    for (_session, hour, minute), volumes in minute_rows.items():
        if len(volumes) == RVOL_INTERVAL_MINUTES:
            slots.setdefault((hour, minute), []).append(sum(volumes))
    return {
        slot: (sum(samples) / len(samples), len(samples))
        for slot, samples in slots.items() if samples
    }


def _rvol_series(day: date_type, current_stock: list[dict]) -> list[dict]:
    """Return completed five-minute SPY rVol bars using user-owned Alpaca data."""
    baseline = _rvol_baseline(day)
    buckets: dict[tuple[int, int], list[tuple[datetime, float]]] = {}
    for row in current_stock:
        stamp = _eastern_bar(row)
        if stamp is None or stamp.date() != day or not time_type(9, 30) <= stamp.time() < time_type(16):
            continue
        try:
            volume = float(row.get("v", 0))
        except (TypeError, ValueError):
            continue
        buckets.setdefault(
            (stamp.hour, stamp.minute // RVOL_INTERVAL_MINUTES * RVOL_INTERVAL_MINUTES), []
        ).append((stamp, volume))
    result = []
    for slot, rows in sorted(buckets.items()):
        # Never classify a still-forming bucket as low volume.
        if len(rows) != RVOL_INTERVAL_MINUTES or slot not in baseline:
            continue
        historical_average, sample_count = baseline[slot]
        current_volume = sum(volume for _, volume in rows)
        if historical_average <= 0 or sample_count <= 0:
            continue
        # Match the indicator's realtime matrix behavior: the completed current
        # interval is present alongside the prior-session samples when the
        # column average is calculated. This differs slightly from a strictly
        # prior-only denominator near color boundaries (for example 0.80).
        comparison_average = (
            historical_average * sample_count + current_volume
        ) / (sample_count + 1)
        value = current_volume / comparison_average
        stamp = min(stamp for stamp, _ in rows)
        result.append({
            "timestamp": stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "rvol": value,
            "color": _rvol_color(value),
            "baseline_sessions": sample_count,
        })
    return result


class HistoricalDataError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _historical_bars(path: str, params: dict):
    """Complete historical pages without changing the live REST path."""
    stock = path.startswith("/v2/stocks/")
    result = [] if stock else {}
    seen = set()
    query = {**params, "sort": "asc"}
    for _ in range(100):
        response = _get(path, query)
        rows = response.get("bars") if isinstance(response, dict) else None
        if stock:
            if not isinstance(rows, list):
                raise HistoricalDataError("Historical SPY bars are malformed.", "historical_reference_unavailable")
            result.extend(rows)
        else:
            if not isinstance(rows, dict) or any(not isinstance(values, list) for values in rows.values()):
                raise HistoricalDataError("Historical option data is malformed.", "historical_option_chain_invalid")
            for symbol, values in rows.items():
                result.setdefault(symbol, []).extend(values)
        token = response.get("next_page_token")
        if not token:
            return result
        if not isinstance(token, str) or token in seen:
            raise HistoricalDataError("Historical data pagination is incomplete; no session was selected.", "historical_data_incomplete")
        seen.add(token)
        query = {**params, "sort": "asc", "page_token": token}
    raise HistoricalDataError("Historical data pagination exceeded its safety limit.", "historical_data_incomplete")


def _historical_timestamp(row: dict) -> datetime:
    try:
        stamp = row["t"]
        if not isinstance(stamp, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", stamp):
            raise ValueError("timezone missing")
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalDataError("Historical bar timestamp is invalid.", "historical_data_incomplete") from exc


def _historical_session_rows(rows: list, start: str, end: str) -> list:
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return sorted((row for row in rows if first <= _historical_timestamp(row) < last), key=_historical_timestamp)


def _historical_reference(stock: list, day: date_type) -> tuple[str, float]:
    timestamp = _utc_at(day, 9, 30)
    expected = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    opening = [row for row in stock if _historical_timestamp(row) == expected]
    try:
        if len(opening) != 1 or isinstance(opening[0].get("o"), bool):
            raise ValueError("exact opening missing or ambiguous")
        spot = float(opening[0]["o"])
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError("invalid opening price")
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalDataError(f"Historical SPY opening price is unavailable at {timestamp} (09:30 ET); no later/current price is substituted.", "historical_reference_unavailable") from exc
    return timestamp, spot


def _historical_contracts(options: dict, day: date_type, start: str, end: str) -> list:
    """Listed-contract evidence comes from the existing historical bars API."""
    available = []
    for symbol, rows in options.items():
        match = re.fullmatch(r"SPY(\d{6})C(\d{8})", symbol)
        if not match or match.group(1) != day.strftime("%y%m%d"):
            raise HistoricalDataError("Historical option response has an invalid SPY CALL symbol or expiration.", "historical_option_chain_invalid")
        strike = int(match.group(2)) / 1000
        if strike <= 0:
            raise HistoricalDataError("Historical option strike is invalid.", "historical_option_chain_invalid")
        selected_rows = _historical_session_rows(rows, start, end)
        if selected_rows:
            for row in selected_rows:
                try:
                    price = float(row["c"])
                    if not math.isfinite(price) or price < 0:
                        raise ValueError("invalid close")
                except (KeyError, TypeError, ValueError) as exc:
                    raise HistoricalDataError("Historical option bars have invalid prices.", "historical_option_chain_invalid") from exc
            available.append((strike, symbol, selected_rows))
    return sorted(available, key=lambda item: (item[0], item[1]))


def _download_historical_payload(day: date_type, offset: int, include_rvol: bool) -> dict:
    if day < HISTORICAL_OPTIONS_START:
        raise HistoricalDataError(f"Historical options data is unavailable for this date. Earliest verified Alpaca coverage: {HISTORICAL_OPTIONS_START}.", "historical_options_unavailable")
    start, end = _utc_at(day, 4), _utc_at(day, 16)
    stock = _historical_bars("/v2/stocks/SPY/bars", {
        "timeframe": "1Min", "start": start, "end": end,
        "limit": 10000, "feed": "sip", "adjustment": "raw",
    })
    stock = _historical_session_rows(stock, _utc_at(day, 4, 5), end)
    reference_timestamp, spot = _historical_reference(stock, day)
    nominal_atm = math.floor(spot + .5)
    target = nominal_atm + int(offset)
    # Preserve the existing SPY $1 neighborhood and same-session expiration.
    # Unlike the live path, missing nearest/requested data never substitutes
    # another strike. Unproven legacy disk ATM metadata is not consumed.
    symbols = [_occ_symbol(day, target + step) for step in (0, 1, -1, 2, -2)]
    options = _historical_bars("/v1beta1/options/bars", {
        "symbols": ",".join(symbols), "timeframe": "1Min",
        "start": start, "end": end, "limit": 10000,
    })
    available = _historical_contracts(options, day, start, end)
    reference = spot if int(offset) == 0 else target
    chosen = min(available, key=lambda item: (abs(item[0] - reference), -item[0], item[1])) if available else None
    if chosen is None or chosen[0] != target:
        raise HistoricalDataError(f"Historical options data is unavailable for the nearest/requested {target}C on {day}; no different strike is substituted.", "historical_options_unavailable")
    strike, symbol, option_rows = chosen
    prior_stock_all = _historical_bars("/v2/stocks/SPY/bars", {
        "timeframe": "1Min", "start": _utc_at(day - timedelta(days=7), 9),
        "end": _utc_at(day, 9), "limit": 10000, "feed": "sip", "adjustment": "raw",
    })
    prior = _latest_prior_rth_session(prior_stock_all, day)
    if prior is None:
        raise HistoricalDataError(f"Historical prior SPY session is unavailable for {day}.", "historical_reference_unavailable")
    prior_day, prior_stock = prior
    prior_options = _historical_bars("/v1beta1/options/bars", {
        "symbols": symbol, "timeframe": "1Min", "start": _utc_at(prior_day, 9),
        "end": _utc_at(prior_day, 16), "limit": 10000,
    }).get(symbol, [])
    diagnostic = {"schema_version": 1, "historical_date": day.isoformat(),
        "reference_timestamp": reference_timestamp, "reference_price": spot,
        "price_adjustment": "raw", "expiration": day.isoformat(),
        "available_strike_range": [available[0][0], available[-1][0]],
        "available_strikes": [item[0] for item in available],
        "chain_scope": "queried historical-bar neighborhood",
        "atm_strike": nominal_atm, "selected_strike": strike,
        "distance_from_spot": round(abs(spot - strike), 8), "offset": int(offset)}
    result = {"session_date": day.isoformat(), "option_symbol": symbol,
        "option_strike": int(strike), "spy_bars": stock, "option_bars": option_rows,
        "previous_spy_bars": prior_stock, "previous_option_bars": prior_options,
        "historical_atm": diagnostic}
    if include_rvol:
        result["rvol_series"] = _rvol_series(day, stock)
    return result


def _download_payload(day: date_type, offset: int, use_cache: bool = True,
                      include_rvol: bool = False, historical: bool = False) -> dict:
    if historical:
        return _download_historical_payload(day, offset, include_rvol)
    if use_cache:
        cached = _cached_payload(day, offset)
        if cached is not None:
            return cached
    # Request the complete market day. The previous noon endpoint caused the
    # live dashboard to appear frozen at 12:00 ET even though auto-refresh and
    # both local processes were still healthy.
    stock_start, stock_end = _utc_at(day, 4), _utc_at(day, 16)
    stock = _get("/v2/stocks/SPY/bars", {
        "timeframe": "1Min", "start": stock_start, "end": stock_end,
        "limit": 10000, "feed": "sip", "adjustment": "all",
    }).get("bars", [])
    # Alpaca can include a bar stamped exactly at the requested end time.
    # Keep the interval half-open so 04:00-16:00 contains at most 720 one-minute
    # bars and never trips the compute API's current-session safety limit.
    stock = [row for row in stock if stock_start <= row.get("t", "") < stock_end]
    if not stock:
        raise RuntimeError(f"no SPY bars returned for {day}")
    # Match production regime levels: fixed 04:00 ET premarket anchor, with
    # its first five minutes excluded (04:00-04:04 ET).  The 04:01 SIP bar on
    # 2026-09-10 otherwise supplied the non-canonical 764.60 PMH.
    warmup_end = _utc_at(day, 4, 5)
    stock = [row for row in stock if row.get("t", "") >= warmup_end]
    if not stock:
        raise RuntimeError(f"no SPY bars after premarket warm-up for {day}")
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
        "limit": 10000, "feed": "sip", "adjustment": "all",
    }).get("bars", [])
    prior_session = _latest_prior_rth_session(prior_stock_all, day)
    if prior_session is None:
        raise RuntimeError(f"no prior SPY session returned for {day}")
    prior_date_value, prior_stock = prior_session
    prior_date = prior_date_value.isoformat()
    prior_options = _get("/v1beta1/options/bars", {
        "symbols": symbol, "timeframe": "1Min",
        "start": _utc_at(date_type.fromisoformat(prior_date), 9),
        "end": _utc_at(date_type.fromisoformat(prior_date), 16), "limit": 10000,
    }).get("bars", {}).get(symbol, [])

    result = {
        "session_date": day.isoformat(),
        "option_symbol": symbol,
        "option_strike": int(int(symbol[-8:]) / 1000),
        "spy_bars": stock,
        "option_bars": option_rows,
        "previous_spy_bars": prior_stock,
        "previous_option_bars": prior_options,
    }
    if include_rvol:
        result["rvol_series"] = _rvol_series(day, stock)
    return result


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
    requested_day = date_type.fromisoformat(args.date)
    payload = _download_payload(requested_day, max(-10, min(10, args.offset)),
        historical=public_historical or requested_day < datetime.now(ZoneInfo("America/New_York")).date())
    diagnostic = payload.pop("historical_atm", None)
    if diagnostic:
        print("[EXZ Historical ATM] " + json.dumps(diagnostic), file=sys.stderr)
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
