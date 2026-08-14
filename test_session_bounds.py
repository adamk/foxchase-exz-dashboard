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


def test_download_excludes_end_boundary_bar():
    day = date(2026, 8, 11)
    captured = {}

    def fake_get(path, params):
        if path == "/v2/stocks/SPY/bars" and params["start"].startswith("2026-08-11"):
            return {"bars": [
                {"t": "2026-08-11T13:30:00Z", "o": 773.0, "c": 773.1},
                {"t": "2026-08-11T19:59:00Z", "o": 774.0, "c": 774.1},
                {"t": "2026-08-11T20:00:00Z", "o": 775.0, "c": 775.1},
            ]}
        if path == "/v1beta1/options/bars" and "," in params.get("symbols", ""):
            symbol = params["symbols"].split(",")[0]
            return {"bars": {symbol: [{"t": "2026-08-11T19:59:00Z", "c": 1.0}]}}
        if path == "/v2/stocks/SPY/bars":
            return {"bars": [{"t": "2026-08-08T19:59:00Z", "o": 770.0, "c": 770.1}]}
        if path == "/v1beta1/options/bars":
            return {"bars": {params["symbols"]: [{"t": "2026-08-08T19:59:00Z", "c": 1.0}]}}
        return {"bars": []}

    with patch.object(zwap_client, "_get", side_effect=fake_get):
        captured.update(zwap_client._download_payload(day, 0, use_cache=False))

    assert [row["t"] for row in captured["spy_bars"]] == [
        "2026-08-11T13:30:00Z", "2026-08-11T19:59:00Z"
    ]
    # Strike remains anchored to the 09:30 opening price, not the later close.
    assert captured["option_strike"] == 773


if __name__ == "__main__":
    test_download_requests_complete_session()
    test_download_excludes_end_boundary_bar()
