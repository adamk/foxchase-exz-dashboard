"""Synthetic GET fixtures exercise the real historical connector and transport."""
from datetime import date, timedelta
from io import BytesIO
import json
from unittest.mock import patch

import pytest

import local_connector
import zwap_client


DAY = date(2024, 10, 1)


def bar(day, hour, minute, price):
    return {"t": zwap_client._utc_at(day, hour, minute), "o": price,
            "h": price + .1, "l": price - .1, "c": price + .02, "v": 100, "n": 5, "vw": price}


class Market:
    """No network: different historical raw/all observations expose the bug."""
    def __init__(self, day=DAY, spot=573.4):
        self.day = day
        self.prior_day = day - timedelta(days=1)
        while self.prior_day.weekday() >= 5:
            self.prior_day -= timedelta(days=1)
        self.spot = spot
        self.calls = []
        self.raw_rows = [bar(day, 9, 30, spot), bar(day, 9, 31, spot + 1)]
        self.chain = None
        self.pages = None

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        current = params["start"][:10] == self.day.isoformat()
        if path == "/v2/stocks/SPY/bars":
            if not current:
                return {"bars": [bar(self.prior_day, 15, 59, self.spot - 1)]}
            rows = self.raw_rows if params.get("adjustment") == "raw" else [bar(self.day, 9, 30, self.spot - 13)]
            return {"bars": rows, "next_page_token": None}
        if path == "/v1beta1/options/bars":
            if current and self.pages is not None:
                return self.pages.get(params.get("page_token"), self.pages[None])
            if current and self.chain is not None:
                return {"bars": self.chain, "next_page_token": None}
            symbols = params["symbols"].split(",")
            when = self.day if current else self.prior_day
            return {"bars": {s: [bar(when, 9, 30, 2.0)] for s in reversed(symbols)}, "next_page_token": None}
        raise AssertionError(f"Unexpected provider path: {path}")


def request(day, *, offset=0, live=False):
    handler = local_connector.Handler.__new__(local_connector.Handler)
    handler.path = f"/api/session?date={day.isoformat()}&offset={offset}" + ("&live=1" if live else "")
    responses = []
    handler._json = lambda payload, status=200: responses.append((status, payload))
    handler.send_error = lambda status: responses.append((status, {}))
    handler.do_GET()
    assert len(responses) == 1
    return responses[0]


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ZWAP_CACHE_DIR", str(tmp_path))
    local_connector._payload_cache.clear()
    yield tmp_path
    local_connector._payload_cache.clear()


def run_market(market, offset=0):
    with patch.object(zwap_client, "_get", side_effect=market.get):
        return request(market.day, offset=offset)


def test_historical_atm_uses_nominal_open_instead_of_adjusted_price():
    market = Market()
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_symbol"] == "SPY241001C00573000"
    assert payload["option_strike"] == 573
    assert payload["spy_bars"][0]["o"] == 573.4
    diagnostic = payload["historical_atm"]
    assert diagnostic["reference_timestamp"] == "2024-10-01T13:30:00Z"
    assert diagnostic["reference_price"] == 573.4
    assert diagnostic["expiration"] == "2024-10-01"
    assert diagnostic["distance_from_spot"] == pytest.approx(.4)


@pytest.mark.parametrize("spot,want", [(573.2, 573), (573.6, 574), (573.5, 574)])
def test_between_strikes_and_half_up_tie_are_deterministic(spot, want):
    status, payload = run_market(Market(spot=spot))
    assert status == 200
    assert payload["option_strike"] == want


@pytest.mark.parametrize("order", [[572, 573, 574], [574, 573, 572], [573, 574, 572]])
def test_contract_input_order_does_not_change_atm(order):
    market = Market()
    market.chain = {f"SPY241001C{k * 1000:08d}": [bar(DAY, 9, 30, 2)] for k in order}
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


def test_unsorted_stock_rows_use_exact_open_not_later_price():
    market = Market()
    market.raw_rows.reverse()
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


@pytest.mark.parametrize("chain", [None, {"not-occ": [bar(DAY, 9, 30, 2)]},
                                   {"SPY241002C00573000": [bar(DAY, 9, 30, 2)]},
                                   {"SPY241001P00573000": [bar(DAY, 9, 30, 2)]}])
def test_missing_or_malformed_historical_chain_fails_visibly(chain):
    market = Market()
    market.chain = {} if chain is None else chain
    status, payload = run_market(market)
    assert status == 422
    assert "historical_" in payload["code"]
    assert payload["error"]


def test_missing_nearest_quote_cannot_silently_substitute_other_strike():
    market = Market()
    market.chain = {"SPY241001C00575000": [bar(DAY, 9, 30, 2)]}
    status, payload = run_market(market)
    assert status == 422
    assert payload["code"] == "historical_options_unavailable"


@pytest.mark.parametrize("rows", [[], [bar(DAY, 9, 31, 573.4)], [bar(date(2026, 10, 5), 9, 30, 773)]])
def test_missing_reference_has_no_later_or_current_price_fallback(rows):
    market = Market()
    market.raw_rows = rows
    status, payload = run_market(market)
    assert status == 422
    assert payload["code"] == "historical_reference_unavailable"


@pytest.mark.parametrize("opening", [None, 0, float("nan"), float("inf")])
def test_invalid_reference_does_not_fall_back_to_bar_close(opening):
    market = Market()
    market.raw_rows[0]["o"] = opening
    market.raw_rows[0]["c"] = 573.4
    status, payload = run_market(market)
    assert status == 422
    assert payload["code"] == "historical_reference_unavailable"


def test_stale_disk_atm_metadata_is_not_authoritative(isolated_cache):
    market = Market()
    symbol = "SPY241001C00800000"
    files = {"spy_bars": {"bars": market.raw_rows},
             "previous_spy_bars": {"bars": [bar(DAY - timedelta(days=1), 15, 59, 572)]},
             "meta": {"atm_strike": 800},
             "option_bars": {"bars": {symbol: [bar(DAY, 9, 30, 2)]}},
             "previous_option_bars": {"bars": {symbol: [bar(DAY - timedelta(days=1), 15, 59, 2)]}}}
    for suffix, data in files.items():
        (isolated_cache / f"data_2024-10-01_{suffix}.json").write_text(json.dumps(data))
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


def test_legacy_server_cache_cannot_override_corrected_history():
    market = Market()
    local_connector._payload_cache[("2024-10-01", 0, False)] = {"option_strike": 800}
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


@pytest.mark.parametrize("diagnostic", [None, {"schema_version": 1, "historical_date": "2024-10-08", "price_adjustment": "all"}])
def test_malformed_or_wrong_date_server_cache_is_refetched(diagnostic):
    market = Market()
    local_connector._payload_cache[("2024-10-01", 0, False)] = {"option_strike": 800, "historical_atm": diagnostic}
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


def test_supported_early_2024_session_uses_correct_price():
    market = Market(day=date(2024, 1, 31), spot=488.62)
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_symbol"] == "SPY240131C00489000"


def test_pre_availability_session_rejects_before_market_data_requests():
    market = Market(day=date(2023, 12, 29), spot=476.49)
    status, payload = run_market(market)
    assert status == 422
    assert payload["code"] == "historical_options_unavailable"
    assert "2024-01-18" in payload["error"]
    assert not market.calls


def test_offset_is_exact_and_does_not_move_atm_anchor():
    status, payload = run_market(Market(), offset=2)
    assert status == 200
    assert payload["option_strike"] == 575
    assert payload["historical_atm"]["atm_strike"] == 573


def test_complete_option_pages_before_selecting_atm():
    market = Market()
    market.pages = {None: {"bars": {"SPY241001C00575000": [bar(DAY, 9, 30, 2)]}, "next_page_token": "next"},
                    "next": {"bars": {"SPY241001C00573000": [bar(DAY, 9, 30, 2)]}, "next_page_token": None}}
    status, payload = run_market(market)
    assert status == 200
    assert payload["option_strike"] == 573


def test_repeated_option_page_token_fails_instead_of_spinning():
    market = Market()
    page = {"bars": {"SPY241001C00575000": [bar(DAY, 9, 30, 2)]}, "next_page_token": "repeat"}
    market.pages = {None: page, "repeat": page}
    status, payload = run_market(market)
    assert status == 422
    assert payload["code"] == "historical_data_incomplete"


def test_current_day_live_route_retains_its_runtime_and_payload():
    today = local_connector.datetime.now(local_connector.ZoneInfo("America/New_York")).date()
    expected = {"session_date": today.isoformat(), "option_strike": 773, "live": "unchanged"}
    with patch.object(local_connector._live_runtime, "snapshot", return_value=expected):
        status, payload = request(today, live=True)
    assert status == 200
    assert payload == expected


def test_current_day_historical_request_retains_existing_access_gate():
    today = local_connector.datetime.now(local_connector.ZoneInfo("America/New_York")).date()
    status, payload = request(today)
    assert status == 402
    assert "require live access" in payload["error"]


def test_live_transport_keeps_adjusted_prices_and_original_payload_shape():
    today = local_connector.datetime.now(local_connector.ZoneInfo("America/New_York")).date()
    market = Market(day=today, spot=786)
    with patch.object(zwap_client, "_get", side_effect=market.get):
        payload = zwap_client._download_payload(today, 0, use_cache=False)
    assert payload["option_strike"] == 773
    assert payload["spy_bars"][0]["o"] == 773
    assert set(payload) == {"session_date", "option_symbol", "option_strike", "spy_bars", "option_bars", "previous_spy_bars", "previous_option_bars"}


def test_cli_excludes_local_diagnostics_from_private_calculation_payload(monkeypatch):
    market = Market()
    uploaded = []

    class Reply(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def calculate(request, **kwargs):
        body = json.loads(request.data)
        uploaded.append(body)
        return Reply(json.dumps({"option_symbol": body["option_symbol"], "series": []}).encode())

    monkeypatch.setattr("sys.argv", ["zwap_client.py", "--date", "2024-10-01", "--offset", "0"])
    with patch.object(zwap_client, "_get", side_effect=market.get), patch.object(zwap_client, "urlopen", side_effect=calculate):
        assert zwap_client.main() == 0
    assert uploaded[0]["option_symbol"] == "SPY241001C00573000"
    assert "historical_atm" not in uploaded[0]
