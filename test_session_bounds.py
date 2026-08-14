from datetime import date
from unittest.mock import patch

import zwap_client


def test_download_requests_complete_session():
    day = date(2026, 8, 11)
    calls = []

    def fake_get(path, params):
        calls.append((path, params.copy()))
        if path == "/v2/stocks/SPY/bars" and params["start"].startswith("2026-08-11"):
            return {"bars": []}
        return {"bars": []}

    with patch.object(zwap_client, "_get", side_effect=fake_get):
        try:
            zwap_client._download_payload(day, 1, use_cache=False)
        except RuntimeError as exc:
            assert "no SPY bars" in str(exc)

    _, params = calls[0]
    assert params["start"] == "2026-08-11T08:00:00Z"  # 04:00 ET
    assert params["end"] == "2026-08-11T20:00:00Z"  # 16:00 ET


def test_atm_uses_latest_regular_session_close():
    day = date(2026, 8, 11)
    bars = [
        {"t": "2026-08-11T13:30:00Z", "o": 770.10, "c": 770.25},
        {"t": "2026-08-11T15:00:00Z", "o": 773.10, "c": 773.62},
        # An after-hours value must not change the option strike selection.
        {"t": "2026-08-11T20:05:00Z", "o": 780.00, "c": 780.25},
    ]
    assert zwap_client._latest_atm_strike(day, bars) == 774


if __name__ == "__main__":
    test_download_requests_complete_session()
    test_atm_uses_latest_regular_session_close()
