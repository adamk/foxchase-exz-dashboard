"""Local-only Alpaca data adapter for the ZWAP web dashboard.

This process keeps the user's Alpaca keys on the user's computer. It has no
ZWAP calculation logic; it only returns the user's requested normalized bars
to the browser so the browser can submit them to the private EC2 calculator.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import re

from zwap_client import (_download_payload, _historical_reference, _occ_symbol,
                         HistoricalDataError, HISTORICAL_OPTIONS_START)
from datetime import date as date_type, datetime
from zoneinfo import ZoneInfo
from live_runtime import LiveRuntime


_cache_lock = threading.Lock()
_payload_cache: dict[tuple[str, int, bool], dict] = {}
_date_pattern = re.compile(r"^data_(\d{4}-\d{2}-\d{2})_meta\.json$")


def _live_payload_loader(day: date_type, offset: int) -> dict:
    return _download_payload(day, offset, use_cache=False, include_rvol=False)


_live_runtime = LiveRuntime(_live_payload_loader)


def _cached_sessions() -> list[dict]:
    """Return locally cached date/regime metadata; never reads market bars."""
    cache_root = Path(os.getenv(
        "ZWAP_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent / "zwap_live_dashboard"),
    ))
    sessions: dict[str, str] = {}
    try:
        for path in cache_root.glob("data_*_meta.json"):
            match = _date_pattern.match(path.name)
            if not match:
                continue
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                metadata = {}
            sessions[match.group(1)] = str(metadata.get("regime", "UNKNOWN"))
    except OSError:
        pass
    with _cache_lock:
        for session_date, _offset, _live in _payload_cache:
            sessions.setdefault(session_date, "UNKNOWN")
    return [{"date": day, "regime": sessions[day]} for day in sorted(sessions)]


def _valid_historical_cache(payload, day: date_type, offset: int) -> bool:
    """Reject legacy/mixed-date packets instead of trusting cached ATM labels."""
    if not isinstance(payload, dict) or day < HISTORICAL_OPTIONS_START:
        return False
    diagnostic = payload.get("historical_atm")
    if not isinstance(diagnostic, dict) or diagnostic.get("schema_version") != 1:
        return False
    try:
        stamp, price = _historical_reference(payload.get("spy_bars", []), day)
        atm = int(price + .5)
        target = atm + offset
        return (payload.get("session_date") == day.isoformat()
                and diagnostic.get("historical_date") == day.isoformat()
                and diagnostic.get("price_adjustment") == "raw"
                and diagnostic.get("reference_timestamp") == stamp
                and diagnostic.get("reference_price") == price
                and diagnostic.get("offset") == offset
                and diagnostic.get("atm_strike") == atm
                and diagnostic.get("selected_strike") == target
                and payload.get("option_strike") == target
                and payload.get("option_symbol") == _occ_symbol(day, target))
    except (HistoricalDataError, TypeError, ValueError):
        return False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json({
                "ok": True,
                "service": "zwap-local-data-connector",
                "live": _live_runtime.status(),
            })
            return
        if parsed.path == "/api/sessions":
            self._json({"sessions": _cached_sessions(), "historical_options_start": HISTORICAL_OPTIONS_START.isoformat()})
            return
        if parsed.path == "/api/live/status":
            self._json(_live_runtime.status())
            return
        if parsed.path == "/api/live/quote":
            try:
                self._json(_live_runtime.quote())
            except Exception as exc:
                self._json({"error": str(exc)[:240]}, status=503)
            return
        if parsed.path != "/api/session":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        try:
            session_date = date_type.fromisoformat(query.get("date", [""])[0])
            today = datetime.now(ZoneInfo("America/New_York")).date()
            live = query.get("live", ["0"])[0] == "1"
            if session_date > today or (session_date == today and not live):
                self._json({"error": "current-day and future sessions require live access"}, status=402)
                return
            # A historical date never selects the current-day stream, even
            # when a caller supplies a stale live query flag.
            live = live and session_date == today
            offset = max(-10, min(10, int(query.get("offset", ["1"])[0])))
            cache_key = (session_date.isoformat(), offset, live)
            if live:
                payload = _live_runtime.snapshot(session_date, offset)
            else:
                with _cache_lock:
                    payload = _payload_cache.get(cache_key)
                if payload is not None and not _valid_historical_cache(payload, session_date, offset):
                    payload = None
                if payload is None:
                    payload = _download_payload(session_date, offset, use_cache=True, historical=True)
                    with _cache_lock:
                        _payload_cache[cache_key] = payload
            self._json(payload)
        except HistoricalDataError as exc:
            self._json({"error": str(exc), "code": exc.code,
                "historical_options_start": HISTORICAL_OPTIONS_START.isoformat()}, status=422)
        except Exception as exc:  # local diagnostics only; no credentials included
            self._json({"error": str(exc)[:240]}, status=502)

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8791")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def main() -> None:
    host = os.getenv("ZWAP_CONNECTOR_HOST", "127.0.0.1")
    port = int(os.getenv("ZWAP_CONNECTOR_PORT", "8789"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ZWAP local data connector: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _live_runtime.stop()
        server.server_close()


if __name__ == "__main__":
    main()
