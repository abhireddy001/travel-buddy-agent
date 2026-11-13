"""Unit tests for the Travel Agent core (no AWS / LLM calls)."""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_core.airports import UnknownAirportError, resolve_airport, search_airports  # noqa: E402
from travel_core.provider import FlightError, SimulatedFlightProvider, lead_time_factor  # noqa: E402
from travel_core import trends  # noqa: E402

TODAY = date(2026, 11, 12)


@pytest.fixture
def provider(tmp_path):
    return SimulatedFlightProvider(bookings_file=tmp_path / "bookings.json", today=TODAY)


def test_airport_lookup():
    assert resolve_airport("jfk").code == "JFK"
    assert resolve_airport("Singapore").code == "SIN"
    assert {a.code for a in search_airports("tokyo")} == {"NRT", "HND"}
    with pytest.raises(UnknownAirportError):
        resolve_airport("Tokyo")        # ambiguous
    with pytest.raises(UnknownAirportError):
        resolve_airport("XXX")


def test_search_is_deterministic_and_global(provider):
    d = TODAY + timedelta(days=45)
    for o, de in [("JFK", "LHR"), ("SYD", "LHR"), ("GRU", "JNB"), ("DEL", "BOM"), ("LAX", "AKL")]:
        a = provider.search_flights(o, de, d)
        b = provider.search_flights(o, de, d)
        assert a, f"no flights {o}-{de}"
        assert [f.to_dict() for f in a] == [f.to_dict() for f in b]


def test_ultra_long_haul_requires_connection(provider):
    flights = provider.search_flights("SYD", "LHR", TODAY + timedelta(days=45))
    assert all(f.stops == 1 for f in flights)
    assert all(f.segments[0].destination == f.segments[1].origin for f in flights)


def test_filters_and_validation(provider):
    d = TODAY + timedelta(days=30)
    assert all(f.stops == 0 for f in provider.search_flights("JFK", "LHR", d, max_stops=0))
    biz = provider.search_flights("JFK", "LHR", d, cabin="business")
    eco = provider.search_flights("JFK", "LHR", d)
    assert min(f.price_per_passenger for f in biz) > min(f.price_per_passenger for f in eco) * 2
    with pytest.raises(FlightError):
        provider.search_flights("JFK", "LHR", TODAY - timedelta(days=1))
    with pytest.raises(FlightError):
        provider.search_flights("JFK", "JFK", d)
    with pytest.raises(FlightError):
        provider.search_flights("JFK", "LHR", d, cabin="luxury")


def test_get_flight_matches_search(provider):
    f = provider.search_flights("CDG", "NRT", TODAY + timedelta(days=50))[0]
    assert provider.get_flight(f.flight_id).to_dict() == f.to_dict()
    with pytest.raises(FlightError):
        provider.get_flight("CDG-NRT-20270101-99")


def test_booking_lifecycle_updates_seats(provider):
    f = provider.search_flights("JFK", "LHR", TODAY + timedelta(days=40))[0]
    b = provider.book(f.flight_id, "economy", ["Ada Lovelace", "Alan Turing"], "ada@example.com")
    assert b.status == "CONFIRMED" and b.total_price == round(f.price_per_passenger * 2, 2)
    assert provider.get_flight(f.flight_id).seats_left == f.seats_left - 2
    assert provider.get_booking(b.booking_ref.lower()).booking_ref == b.booking_ref
    assert provider.cancel_booking(b.booking_ref).status == "CANCELLED"
    assert provider.get_flight(f.flight_id).seats_left == f.seats_left
    with pytest.raises(FlightError):
        provider.cancel_booking(b.booking_ref)


def test_booking_rejects_bad_input(provider):
    f = provider.search_flights("JFK", "LHR", TODAY + timedelta(days=40))[0]
    with pytest.raises(FlightError):
        provider.book(f.flight_id, "economy", [], "a@b.com")
    with pytest.raises(FlightError):
        provider.book(f.flight_id, "economy", ["A B"], "not-an-email")
    with pytest.raises(FlightError):
        provider.book(f.flight_id, "economy", ["P"] * (f.seats_left + 1 if f.seats_left < 9 else 10), "a@b.com")


def test_lead_time_curve_shape():
    assert lead_time_factor(2) > lead_time_factor(10) > lead_time_factor(40)
    assert lead_time_factor(200) > lead_time_factor(40)


def test_cheapest_dates_ranked(provider):
    res = trends.cheapest_dates(provider, "LHR", "DXB", TODAY + timedelta(days=20), days=21)
    fares = [r["lowest_fare"] for r in res["cheapest_dates"]]
    assert fares == sorted(fares) and len(fares) == 5
    assert res["most_expensive_date"]["lowest_fare"] >= fares[-1]


def test_price_trends_and_html(provider, tmp_path):
    t = trends.price_trends(provider, "JFK", "LHR", TODAY + timedelta(days=7), days=60)
    assert len(t["daily"]) == 60
    assert t["summary"]["cheapest"]["lowest_fare"] == min(r["lowest_fare"] for r in t["daily"])
    assert set(t["weekday_average"]) == set(trends.WEEKDAYS)
    assert t["booking_window"]["cheapest_booking_window_days_before"] == [22, 70]
    path = trends.render_trend_html(t, output_dir=tmp_path)
    page = path.read_text()
    assert "__DATA__" not in page and "JFK-LHR" in page
    embedded = page.split("const DATA = ", 1)[1].split(";\nconst NS", 1)[0]
    assert json.loads(embedded)["route"] == "JFK-LHR"
    assert len(trends.sparkline([1, 2, 3])) == 3
    with pytest.raises(FlightError):
        trends.price_trends(provider, "JFK", "LHR", TODAY, days=500)
