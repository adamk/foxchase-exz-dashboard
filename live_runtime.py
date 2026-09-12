"""Supervise the local EXZ Live SIP/OPRA bridge and bounded REST recovery."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from alpaca_live_stream import AlpacaLiveStream
from live_stream_adapter import LiveBarAdapter, iso_time
from zwap_client import _rvol_series


PayloadLoader = Callable[[date, int], dict[str, Any]]


class LiveRuntime:
    """Single local live session; a date/contract change replaces it atomically."""

    def __init__(self, payload_loader: PayloadLoader, *, settle_seconds: float = 10.0) -> None:
        self._payload_loader = payload_loader
        self._settle_seconds = settle_seconds
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._adapter: LiveBarAdapter | None = None
        self._day: date | None = None
        self._offset: int | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._transition_pending = False
        self._repairing = False
        self._generation_token = ""
        self._last_error: str | None = None
        self._events: list[dict[str, Any]] = []

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _record(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._events.append({"event": event, "timestamp": self._now(), **fields})
            self._events = self._events[-100:]

    def ensure(self, day: date, offset: int) -> None:
        offset = max(-10, min(10, int(offset)))
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive() and self._day == day and self._offset == offset:
                return
            self._stop_locked()
            payload = self._payload_loader(day, offset)
            adapter = LiveBarAdapter(settle_seconds=self._settle_seconds)
            adapter.warm_start(payload, offset=offset)
            with self._lock:
                self._adapter = adapter
                self._day = day
                self._offset = offset
                self._transition_pending = True
                self._last_error = None
                self._events = [{
                    "event": "warm_start",
                    "timestamp": self._now(),
                    "generation": adapter.generation,
                    "option_symbol": adapter.option_symbol,
                    "last_applied": adapter.status()["last_applied"],
                }]
                self._generation_token = uuid.uuid4().hex
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main, name="exz-live-stream", daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                raise RuntimeError("live stream supervisor did not start")

    def stop(self) -> None:
        with self._lifecycle:
            self._stop_locked()

    def snapshot(self, day: date, offset: int) -> dict[str, Any]:
        """Return the requested contract atomically across a runtime switch."""
        with self._lifecycle:
            self.ensure(day, offset)
            return self.payload()

    def _stop_locked(self) -> None:
        thread = self._thread
        loop = self._loop
        stop_event = self._stop_event
        if thread is not None and thread.is_alive() and loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
            thread.join(timeout=10)
        self._thread = None
        self._loop = None
        self._stop_event = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:240]
            self._record("supervisor_error", error=str(exc)[:240])

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._ready.set()
        tasks = [
            asyncio.create_task(self._feed_loop("sip"), name="exz-live-sip"),
            asyncio.create_task(self._feed_loop("opra"), name="exz-live-opra"),
            asyncio.create_task(self._transition_loop(), name="exz-live-repair"),
        ]
        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _credentials(self) -> tuple[str, str]:
        key = os.getenv("APCA_API_KEY_ID", "")
        secret = os.getenv("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError("Alpaca credentials are not available to the local connector")
        return key, secret

    async def _feed_loop(self, feed: str) -> None:
        attempt = 0
        while self._stop_event is not None and not self._stop_event.is_set():
            attempt += 1
            try:
                key, secret = self._credentials()
                with self._lock:
                    adapter = self._adapter
                    symbols = [adapter.option_symbol] if adapter is not None else []
                await AlpacaLiveStream(key, secret, self._handle, self._stream_status).run_once(
                    feed, symbols, self._now
                )
                raise RuntimeError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._lock:
                    was_connected = bool(
                        self._adapter is not None and self._adapter.connected.get(feed)
                    )
                    if self._adapter is not None:
                        self._adapter.mark_connection(feed, False)
                    self._last_error = f"{feed}: {str(exc)[:200]}"
                self._record("stale", feed=feed, attempt=attempt, error=str(exc)[:200])
                if was_connected:
                    await self._bounded_repair(f"{feed}_reconnect")
                await asyncio.sleep(min(10.0, max(1.0, float(attempt))))

    async def _handle(self, feed: str, message: dict[str, Any], received: str) -> None:
        kind = str(message.get("T", ""))
        with self._lock:
            adapter = self._adapter
            if adapter is None:
                return
            if kind == "subscription":
                adapter.mark_connection(feed, True)
                self._last_error = None
                self._events.append({"event": "connected", "feed": feed, "timestamp": received})
                self._events = self._events[-100:]
            adapter.handle(feed, message, received)

    async def _stream_status(self, feed: str, event: str, received: str) -> None:
        self._record(event, feed=feed, received=received)

    async def _bounded_repair(self, reason: str) -> None:
        with self._lock:
            if self._repairing or self._day is None or self._offset is None:
                return
            self._repairing = True
            day, offset = self._day, self._offset
        try:
            payload = await asyncio.to_thread(self._payload_loader, day, offset)
            cutoff = LiveBarAdapter.stable_cutoff()
            with self._lock:
                if self._adapter is None or self._day != day or self._offset != offset:
                    return
                self._adapter.rest_repair(payload, reason=reason, cutoff=cutoff)
                self._transition_pending = True
                self._generation_token = uuid.uuid4().hex
            self._record("rest_gap_repair", reason=reason, cutoff=iso_time(cutoff))
        except Exception as exc:
            with self._lock:
                self._last_error = f"REST repair: {str(exc)[:200]}"
            self._record("rest_repair_failed", reason=reason, error=str(exc)[:200])
        finally:
            with self._lock:
                self._repairing = False

    async def _transition_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            day: date | None = None
            offset: int | None = None
            cutoff: datetime | None = None
            with self._lock:
                adapter = self._adapter
                if adapter is not None and self._transition_pending and not self._repairing:
                    starts = list(adapter.coverage_start.values())
                    watermarks = list(adapter.watermark.values())
                    if all(value is not None for value in starts + watermarks):
                        coverage = max(value for value in starts if value is not None)
                        ready = coverage + timedelta(seconds=adapter.settle_seconds)
                        if min(value for value in watermarks if value is not None) >= ready:
                            day, offset = self._day, self._offset
                            cutoff = coverage - timedelta(minutes=1)
                            self._repairing = True
            if day is not None and offset is not None and cutoff is not None:
                try:
                    payload = await asyncio.to_thread(self._payload_loader, day, offset)
                    with self._lock:
                        if self._adapter is not None and self._day == day and self._offset == offset:
                            self._adapter.rest_repair(
                                payload,
                                reason="warm_to_stream_transition",
                                cutoff=cutoff,
                                reset_stream_coverage=False,
                            )
                            self._transition_pending = False
                            self._generation_token = uuid.uuid4().hex
                    self._record("warm_transition_repair", cutoff=iso_time(cutoff))
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"transition repair: {str(exc)[:180]}"
                    self._record("transition_repair_failed", error=str(exc)[:180])
                finally:
                    with self._lock:
                        self._repairing = False
            await asyncio.sleep(0.25)

    def payload(self) -> dict[str, Any]:
        with self._lock:
            if self._adapter is None:
                raise RuntimeError("live stream is not initialized")
            payload = self._adapter.sync_payload()
            payload["_incremental"]["generation"] = self._generation_token
            spy_bars = deepcopy(payload["spy_bars"])
            payload["stream_status"] = self._status_locked()
        payload["rvol_series"] = _rvol_series(date.fromisoformat(payload["session_date"]), spy_bars)
        payload["snapshot_timestamp"] = self._now()
        return payload

    def quote(self) -> dict[str, Any]:
        with self._lock:
            if self._adapter is None:
                raise RuntimeError("live stream is not initialized")
            return {
                "quote": self._adapter.quote_result(),
                "stream_status": self._status_locked(),
                "response_timestamp": self._now(),
            }

    def _status_locked(self) -> dict[str, Any]:
        adapter_status = self._adapter.status() if self._adapter is not None else {"mode": "stopped"}
        adapter_status.update({
            "supervisor_alive": bool(self._thread and self._thread.is_alive()),
            "last_error": self._last_error,
            "transition_pending": self._transition_pending,
            "remote_generation": self._generation_token,
            "recent_events": deepcopy(self._events[-12:]),
        })
        return adapter_status

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()
