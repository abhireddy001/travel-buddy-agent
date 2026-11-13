"""
Tool implementations shared by the local MCP server and the AgentCore Gateway Lambda.

Every tool returns a JSON string. Errors come back as {"error": "..."} so the
agent can explain them instead of crashing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import metrics
from . import trends as trend_lib
from .airports import UnknownAirportError
from .live_places import LivePlaces
from .m1_plans import m1_travel_plans
from .live_flights import GoogleFlightsLive, MemoryOfferCache, OfferCache, is_live_id, resolve_api_key
from .airports import search_airports as _search_airports
from .provider import FlightError, FlightOption, SimulatedFlightProvider

# A chart writer receives (trends, html_path_on_disk) and returns a link/path for the user.
ChartPublisher = Callable[[dict, Path], str]

SORT_KEYS = {
    "price": lambda f: (f.price_per_passenger, f.total_duration_minutes),
    "duration": lambda f: (f.total_duration_minutes, f.price_per_passenger),
    "departure": lambda f: f.segments[0].depart_local,
}


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _ok(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _err(e: Exception) -> str:
    return _ok({"error": str(e)})


def _summarize(f: FlightOption, passengers: int = 1) -> dict:
    return {
        "flight_id": f.flight_id,
        "price_per_passenger": f.price_per_passenger,
        "total_price": round(f.price_per_passenger * passengers, 2),
        "currency": f.currency,
        "cabin": f.cabin,
        "stops": f.stops,
        "duration": _hm(f.total_duration_minutes),
        "departs": f"{f.segments[0].depart_local} {f.origin}",
        "arrives": f"{f.segments[-1].arrive_local} {f.destination}",
        "via": [s.destination for s in f.segments[:-1]],
        "flights": [f"{s.flight_number} ({s.carrier}) {s.origin}->{s.destination}" for s in f.segments],
        "seats_left": f.seats_left,
    }


def _itinerary_text(it: dict) -> list[str]:
    return [
        f"{s['flight_number']} {s['carrier']}: {s['origin']} {s['depart_local']} -> "
        f"{s['destination']} {s['arrive_local']} ({_hm(s['duration_minutes'])})"
        for s in it["segments"]
    ]


def local_chart_publisher(_trends: dict, path: Path) -> str:
    return str(path)


class TravelTools:
    """The eight travel tools, bound to a provider and a chart publisher."""

    def __init__(self, provider: SimulatedFlightProvider | None = None,
                 chart_publisher: ChartPublisher = local_chart_publisher,
                 chart_dir: Path | None = None, offer_cache: OfferCache | None = None,
                 live_client: GoogleFlightsLive | None = None):
        self.provider = provider or SimulatedFlightProvider()
        self.publish_chart = chart_publisher
        self.chart_dir = chart_dir
        self.offer_cache = offer_cache or MemoryOfferCache()
        self._live_override = live_client
        self._live: GoogleFlightsLive | None = None

    @property
    def live(self) -> GoogleFlightsLive | None:
        """Live Google Flights client when a SerpApi key is configured, else None (simulated data)."""
        if self._live_override is not None:
            return self._live_override
        key = resolve_api_key()
        if not key:
            return None
        if self._live is None or self._live.api_key != key:
            self._live = GoogleFlightsLive(key, self.offer_cache)
        return self._live

    def _simulated_note(self) -> str:
        return ("SIMULATED fares (no live data key configured). Tell the user these are demo prices, "
                "not real airline prices.")

    # ------------------------------------------------ destination planning (live)
    def _places(self) -> LivePlaces:
        live = self.live
        if not live:
            raise FlightError("Hotels, places and transport need live data (SerpApi key). It isn't configured, "
                              "so tell the user these recommendations are unavailable rather than inventing them.")
        return LivePlaces(live)

    def search_hotels(self, location: str, check_in_date: str, check_out_date: str, adults: int = 2,
                      max_price: int | None = None, min_rating: float | None = None,
                      sort_by: str = "relevance", limit: int = 6) -> str:
        try:
            return _ok(self._places().hotels(location, check_in_date, check_out_date, adults, max_price,
                                             min_rating, sort_by, limit))
        except FlightError as e:
            return _err(e)

    def explore_places(self, location: str, category: str = "attractions", query: str | None = None,
                       limit: int = 8) -> str:
        try:
            return _ok(self._places().places(location, category, query, limit))
        except FlightError as e:
            return _err(e)

    def find_social_videos(self, place: str, location: str | None = None, platform: str = "auto",
                           limit: int = 6) -> str:
        try:
            return _ok(self._places().social_videos(place, location, platform, limit))
        except FlightError as e:
            return _err(e)

    def get_m1_travel_plans(self, destination: str | None = None, customer_type: str = "any",
                            trip_days: int | None = None) -> str:
        """M1 Singapore roaming / prepaid / tourist-SIM plans (no live search needed)."""
        return _ok(m1_travel_plans(destination, customer_type, trip_days))

    def find_ground_transport(self, from_location: str, to_location: str, include_taxis: bool = True) -> str:
        try:
            return _ok(self._places().transport(from_location, to_location, include_taxis))
        except FlightError as e:
            return _err(e)

    def search_airports(self, query: str) -> str:
        results = _search_airports(query)
        if not results and self.live and len(query.strip()) == 3 and query.strip().isalpha():
            return _ok({"query": query, "airports": [{"code": query.strip().upper()}],
                        "note": "Not in the local airport list, but live search accepts any IATA code."})
        if not results:
            return _ok({"query": query, "airports": [], "note": "No supported airport matched."})
        return _ok({
            "query": query,
            "airports": [
                {"code": a.code, "name": a.name, "city": a.city, "country": a.country, "region": a.region}
                for a in results
            ],
        })

    def search_flights(self, origin: str, destination: str, departure_date: str, passengers: int = 1,
                       cabin: str = "economy", max_stops: int | None = None, sort_by: str = "price",
                       limit: int = 8) -> str:
        if sort_by not in SORT_KEYS:
            return _err(FlightError(f"sort_by must be one of {', '.join(SORT_KEYS)}."))
        live = self.live
        if live:
            try:
                r = live.search(origin, destination, departure_date, passengers, cabin, max_stops)
            except (FlightError, UnknownAirportError) as e:
                return _err(e)
            offers = r.pop("offers")
            r.pop("price_history", None)
            key = {"price": lambda o: o["total_price"],
                   "duration": lambda o: _minutes(o["duration"]),
                   "departure": lambda o: o["departs"] or ""}[sort_by]
            offers.sort(key=key)
            if not offers:
                return _ok({**r, "results": [], "note": "No live fares found for these filters."})
            return _ok({
                **r,
                "results_found": len(offers),
                "cheapest_flight_id": min(offers, key=lambda o: o["total_price"])["flight_id"],
                "fastest_flight_id": min(offers, key=lambda o: _minutes(o["duration"]))["flight_id"],
                "results": offers[: max(1, limit)],
                "note": "LIVE prices from Google Flights (airlines + travel agencies), total for all passengers. "
                        "Times are local. Use get_booking_options(flight_id) to compare sellers and get booking links.",
            })
        try:
            options = self.provider.search_flights(origin, destination, departure_date, passengers, cabin, max_stops)
            options.sort(key=SORT_KEYS[sort_by])  # validated above
            if not options:
                return _ok({"results": [], "note": "No flights with enough seats match these filters."})
            cheapest = min(options, key=lambda f: f.price_per_passenger)
            fastest = min(options, key=lambda f: f.total_duration_minutes)
            return _ok({
                "route": f"{options[0].origin}-{options[0].destination}",
                "departure_date": options[0].departure_date,
                "passengers": passengers,
                "results_found": len(options),
                "cheapest_flight_id": cheapest.flight_id,
                "fastest_flight_id": fastest.flight_id,
                "results": [_summarize(f, passengers) for f in options[: max(1, limit)]],
                "note": "Times are local to each airport. " + self._simulated_note(),
            })
        except (FlightError, UnknownAirportError) as e:
            return _err(e)

    def get_booking_options(self, flight_id: str) -> str:
        if not is_live_id(flight_id):
            return self.check_availability(flight_id)
        live = self.live
        if not live:
            return _err(FlightError("Live data is not configured, so live offers can't be priced."))
        try:
            return _ok(live.booking_options(flight_id))
        except FlightError as e:
            return _err(e)

    def check_availability(self, flight_id: str, cabin: str = "economy", passengers: int = 1) -> str:
        if is_live_id(flight_id):
            return self.get_booking_options(flight_id)
        try:
            flight = self.provider.get_flight(flight_id, cabin)
            other = {}
            for c in ("economy", "premium_economy", "business", "first"):
                f = self.provider.get_flight(flight_id, c)
                other[c] = {"seats_left": f.seats_left, "price_per_passenger": f.price_per_passenger}
            return _ok({
                "flight": _summarize(flight, passengers),
                "requested_passengers": passengers,
                "available": flight.seats_left >= passengers,
                "all_cabins": other,
                "segments": [s.__dict__ for s in flight.segments],
            })
        except (FlightError, UnknownAirportError) as e:
            return _err(e)

    def find_cheapest_dates(self, origin: str, destination: str, start_date: str, days: int = 14,
                            cabin: str = "economy", max_stops: int | None = None) -> str:
        live = self.live
        if live:
            try:
                return _ok(live.cheapest_dates(origin, destination, start_date, min(days, 7), cabin, max_stops)
                           | {"note": "LIVE Google Flights prices. Live scans cover at most 7 days per call; "
                                      "call again with a later start_date to scan further."})
            except (FlightError, UnknownAirportError) as e:
                return _err(e)
        try:
            return _ok(trend_lib.cheapest_dates(self.provider, origin, destination, start_date, days, cabin, max_stops))
        except (FlightError, UnknownAirportError) as e:
            return _err(e)

    def get_price_trends(self, origin: str, destination: str, start_date: str, days: int = 30,
                         cabin: str = "economy", max_stops: int | None = None) -> str:
        live = self.live
        if live:
            try:
                t = live.price_history_trend(origin, destination, start_date, cabin, max_stops)
                chart_link = self.publish_chart(t, trend_lib.render_trend_html(t, self.chart_dir))
                daily = t.pop("daily")
                fares = [r["lowest_fare"] for r in daily]
                return _ok({**t,
                            "sparkline": trend_lib.sparkline(fares),
                            "sparkline_range": f"{t['window']['start']} .. {t['window']['end']} (date checked; "
                                               f"low {min(fares):.0f} / high {max(fares):.0f} {t['currency']})",
                            "chart_file": chart_link,
                            "note": "LIVE: Google's recorded lowest price for this departure date over recent days, "
                                    "plus whether today's price is low/typical/high. For cheapest departure "
                                    "dates use find_cheapest_dates."})
            except (FlightError, UnknownAirportError) as e:
                return _err(e)
        try:
            t = trend_lib.price_trends(self.provider, origin, destination, start_date, days, cabin, max_stops)
            chart_path = trend_lib.render_trend_html(t, self.chart_dir)
            chart_link = self.publish_chart(t, chart_path)
            daily = t.pop("daily")
            fares = [r["lowest_fare"] for r in daily]
            return _ok({
                **t,
                "sparkline": trend_lib.sparkline(fares),
                "sparkline_range": f"{t['window']['start']} .. {t['window']['end']}  "
                                   f"(low {min(fares):.0f} / high {max(fares):.0f} {t['currency']})",
                "weekly_lows": [
                    min(daily[i:i + 7], key=lambda r: r["lowest_fare"]) | {"week_of": daily[i]["date"]}
                    for i in range(0, len(daily), 7)
                ],
                "chart_file": chart_link,
                "note": self._simulated_note(),
            })
        except (FlightError, UnknownAirportError) as e:
            return _err(e)

    def book_flight(self, flight_id: str, passenger_names: list[str], contact_email: str,
                    cabin: str = "economy") -> str:
        if is_live_id(flight_id):
            return _err(FlightError(
                "Live Google Flights fares are booked and paid on the seller's own website (airline or travel "
                "agency). Call get_booking_options for this flight_id and give the user the booking_link of "
                "their chosen seller."))
        try:
            b = self.provider.book(flight_id, cabin, passenger_names, contact_email)
            metrics.booking_event("BookingsCreated")
            return _ok({
                "status": b.status,
                "booking_ref": b.booking_ref,
                "passengers": b.passengers,
                "contact_email": b.contact_email,
                "cabin": b.cabin,
                "total_price": b.total_price,
                "currency": b.currency,
                "itinerary": _itinerary_text(b.itinerary),
            })
        except (FlightError, UnknownAirportError) as e:
            metrics.booking_event("BookingFailures")
            return _err(e)

    def get_booking(self, booking_ref: str) -> str:
        try:
            b = self.provider.get_booking(booking_ref)
            return _ok({**{k: v for k, v in b.to_dict().items() if k != "itinerary"},
                        "itinerary": _itinerary_text(b.itinerary)})
        except FlightError as e:
            return _err(e)

    def cancel_booking(self, booking_ref: str) -> str:
        try:
            b = self.provider.cancel_booking(booking_ref)
            metrics.booking_event("BookingsCancelled")
            return _ok({"booking_ref": b.booking_ref, "status": b.status, "seats_released": len(b.passengers)})
        except FlightError as e:
            return _err(e)


def _minutes(hm: str) -> int:
    try:
        h, m = hm.rstrip("m").split("h")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 10**6


TOOL_NAMES = [
    "search_airports", "search_flights", "check_availability", "get_booking_options", "find_cheapest_dates",
    "get_price_trends", "book_flight", "get_booking", "cancel_booking",
    "search_hotels", "explore_places", "find_ground_transport", "find_social_videos", "get_m1_travel_plans",
]
