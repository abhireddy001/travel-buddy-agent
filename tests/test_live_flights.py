"""Live (SerpApi Google Flights) provider tests with a fake transport - no network."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_core.live_flights import GoogleFlightsLive, is_live_id  # noqa: E402
from travel_core.provider import FlightError  # noqa: E402
from travel_core.tools import TravelTools  # noqa: E402

TODAY = date(2026, 11, 12)


def leg(fn, airline, frm, to, dep, arr):
    return {"flight_number": fn, "airline": airline, "airplane": "Boeing 777",
            "departure_airport": {"id": frm, "time": dep}, "arrival_airport": {"id": to, "time": arr}}


SEARCH = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x=1"},
    "best_flights": [{"flights": [leg("BA 178", "British Airways", "JFK", "LHR", "2027-01-20 18:30", "2027-01-21 06:35")],
                      "layovers": [], "total_duration": 425, "price": 512, "booking_token": "TOKEN-BA",
                      "carbon_emissions": {"this_flight": 410000}}],
    "other_flights": [{"flights": [leg("AY 6", "Finnair", "JFK", "HEL", "2027-01-20 17:00", "2027-01-21 08:10"),
                                   leg("AY 1331", "Finnair", "HEL", "LHR", "2027-01-21 09:30", "2027-01-21 11:05")],
                       "layovers": [{"id": "HEL", "duration": 80}], "total_duration": 785, "price": 431,
                       "booking_token": "TOKEN-AY"}],
    "price_insights": {"lowest_price": 431, "price_level": "low", "typical_price_range": [480, 700],
                       "price_history": [[1760000000 + i * 86400, 600 - i * 3] for i in range(40)]},
}
BOOKING = {"booking_options": [
    {"together": {"book_with": "Expedia", "price": 505, "airline": False,
                  "booking_request": {"url": "https://www.google.com/travel/clk/f", "post_data": "u=abc"}}},
    {"together": {"book_with": "British Airways", "price": 512, "airline": True, "extensions": ["1 carry-on"],
                  "booking_request": {"url": "https://www.google.com/travel/clk/f", "post_data": "u=def"}}},
]}


class FakeLive(GoogleFlightsLive):
    def __init__(self):
        super().__init__("test-key", today=TODAY)
        self.calls = []

    def _get(self, params):
        self.calls.append(params)
        return BOOKING if "booking_token" in params else SEARCH


@pytest.fixture
def tools():
    return TravelTools(live_client=FakeLive())


def test_live_search_is_labelled_and_sorted(tools):
    r = json.loads(tools.search_flights("JFK", "LHR", "2027-01-20"))
    assert r["source"].startswith("Google Flights (live")
    assert [o["total_price"] for o in r["results"]] == [431, 512]
    assert all(is_live_id(o["flight_id"]) for o in r["results"])
    assert r["results"][0]["via"] == ["HEL"] and r["price_insights"]["price_level"] == "low"
    params = tools.live.calls[0]
    assert params["type"] == 2 and params["travel_class"] == 1 and params["outbound_date"] == "2027-01-20"


def test_nonstop_filter_maps_to_serpapi_stops(tools):
    tools.search_flights("JFK", "LHR", "2027-01-20", max_stops=0, cabin="business")
    assert tools.live.calls[-1]["stops"] == 1 and tools.live.calls[-1]["travel_class"] == 3


def test_booking_options_lists_sellers_cheapest_first(tools):
    fid = json.loads(tools.search_flights("JFK", "LHR", "2027-01-20"))["results"][1]["flight_id"]
    r = json.loads(tools.get_booking_options(fid))
    assert [s["seller"] for s in r["sellers"]] == ["Expedia", "British Airways"]
    assert r["sellers"][1]["is_airline"] and r["sellers"][0]["booking_link"].endswith("?u=abc")
    assert tools.live.calls[-1]["booking_token"] == "TOKEN-BA"


def test_live_offers_cannot_be_booked_in_app(tools):
    fid = json.loads(tools.search_flights("JFK", "LHR", "2027-01-20"))["results"][0]["flight_id"]
    assert "seller's own website" in json.loads(tools.book_flight(fid, ["A B"], "a@b.com"))["error"]
    assert "expired or is unknown" in json.loads(tools.get_booking_options("GF-0000000000"))["error"]


def test_live_cheapest_dates_capped_at_7(tools):
    r = json.loads(tools.find_cheapest_dates("JFK", "LHR", "2027-01-20", days=30))
    assert r["window"]["days_searched"] == 7 and r["cheapest_dates"][0]["lowest_fare"] == 431


def test_live_price_history_trend_and_chart(tools, tmp_path):
    tools.chart_dir = tmp_path
    r = json.loads(tools.get_price_trends("JFK", "LHR", "2027-01-20"))
    assert r["kind"] == "history" and r["summary"]["trend_direction"] == "falling"
    assert r["summary"]["typical_price_range"] == [480, 700]
    page = Path(r["chart_file"]).read_text()
    assert "fare-trends-live-JFK-LHR" in r["chart_file"] and '"kind": "history"' in page


def test_simulated_mode_is_labelled(monkeypatch, tmp_path):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_SECRET_ID", raising=False)
    from travel_core.provider import SimulatedFlightProvider
    t = TravelTools(provider=SimulatedFlightProvider(bookings_file=tmp_path / "b.json", today=TODAY))
    r = json.loads(t.search_flights("JFK", "LHR", "2027-01-20"))
    assert "SIMULATED" in r["note"]


def test_past_dates_rejected():
    with pytest.raises(FlightError):
        FakeLive().search("JFK", "LHR", "2026-01-01")
