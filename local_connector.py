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

from zwap_client import _download_payload
from datetime import date as date_type, datetime
from zoneinfo import ZoneInfo


_cache_lock = threading.Lock()
_payload_cache: dict[tuple[str, int, bool], dict] = {}
_date_pattern = re.compile(r"^data_(\d{4}-\d{2}-\d{2})_meta\.json$")


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
        for session_date, _offset in _payload_cache:
            sessions.setdefault(session_date, "UNKNOWN")
    return [{"date": day, "regime": sessions[day]} for day in sorted(sessions)]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json({"ok": True, "service": "zwap-local-data-connector"})
            return
        if parsed.path == "/api/sessions":
            self._json({"sessions": _cached_sessions()})
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
            offset = max(-10, min(10, int(query.get("offset", ["1"])[0])))
            cache_key = (session_date.isoformat(), offset, live)
            payload = None
            if not live:
                with _cache_lock:
                    payload = _payload_cache.get(cache_key)
            if payload is None:
                payload = _download_payload(session_date, offset, use_cache=not live)
                if not live:
                    with _cache_lock:
                        _payload_cache[cache_key] = payload
            self._json(payload)
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
        server.server_close()


if __name__ == "__main__":
    main()
