"""
Flight Booking MCP Server for the Travel Agent (local / stdio mode).

The tool signatures + docstrings here are also the source of the tool schemas
registered on the AgentCore Gateway (see deploy/deploy.py), so local and
gateway modes expose exactly the same tools.

Run with: python flight_mcp_server.py   (stdio transport)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from travel_core.tools import TravelTools  # noqa: E402

mcp = FastMCP("flight-service", log_level="WARNING")
tools = TravelTools()


@mcp.tool()
def search_airports(query: str) -> str:
    """
    Find supported airports by IATA code, city, airport name or country.
    Use this to resolve a city like "Tokyo" to codes (NRT, HND) before searching flights.
    """
    return tools.search_airports(query)


@mcp.tool()
def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    passengers: int = 1,
    cabin: str = "economy",
    max_stops: int | None = None,
    sort_by: str = "price",
    limit: int = 8,
) -> str:
    """
    Search flights between two airports on a date. Returns LIVE Google Flights prices across airlines and
    travel agencies when live data is configured (flight_id starts with "GF-"), otherwise simulated fares. Args: origin / destination are IATA codes
    (e.g. "JFK") or unambiguous city names; departure_date is YYYY-MM-DD; passengers 1-9 (only
    flights with enough seats are returned); cabin is economy | premium_economy | business | first;
    max_stops 0 = nonstop only, 1 = allow one stop, omit for any; sort_by is price | duration |
    departure; limit is the max number of results.
    """
    return tools.search_flights(origin, destination, departure_date, passengers, cabin, max_stops, sort_by, limit)


@mcp.tool()
def get_booking_options(flight_id: str) -> str:
    """
    For a live offer (flight_id "GF-..."): list every seller of that flight - airline websites and online travel
    agencies - with their current price, fare details, baggage info and a booking_link to complete the purchase
    on the seller's site. Use this to compare booking sites and to hand the user off to book.
    """
    return tools.get_booking_options(flight_id)


@mcp.tool()
def check_availability(flight_id: str, cabin: str = "economy", passengers: int = 1) -> str:
    """
    Check seats left and the current fare on a specific flight (flight_id from search_flights).
    Also reports availability in the other cabins on the same flight. For live "GF-" offers this returns the
    current sellers and prices (same as get_booking_options).
    """
    return tools.check_availability(flight_id, cabin, passengers)


@mcp.tool()
def find_cheapest_dates(
    origin: str,
    destination: str,
    start_date: str,
    days: int = 14,
    cabin: str = "economy",
    max_stops: int | None = None,
) -> str:
    """
    For flexible travellers: find the cheapest departure dates in a window starting at
    start_date (YYYY-MM-DD) and spanning `days` days (max 120 simulated; max 7 live, since each live day is
    one real-time search). Returns the top 5 cheapest days with their cheapest flight_id.
    """
    return tools.find_cheapest_dates(origin, destination, start_date, days, cabin, max_stops)


@mcp.tool()
def get_price_trends(
    origin: str,
    destination: str,
    start_date: str,
    days: int = 30,
    cabin: str = "economy",
    max_stops: int | None = None,
) -> str:
    """
    Analyse fare trends. LIVE mode: start_date is the departure date; returns Google's recorded price history
    for that date, today's price level (low/typical/high) and the typical price range. Simulated mode: trends
    across a window of departure dates starting at start_date (max 120 days).
    Returns summary stats (cheapest / most expensive day, average, trend direction), average
    fare by weekday, best booking window, a text sparkline, and chart_file: a path or URL of
    an interactive HTML chart report the user can open in a browser.
    """
    return tools.get_price_trends(origin, destination, start_date, days, cabin, max_stops)


@mcp.tool()
def book_flight(flight_id: str, passenger_names: list[str], contact_email: str, cabin: str = "economy") -> str:
    """
    Book seats on a SIMULATED flight (in-app booking). Live "GF-" offers can't be booked here: use
    get_booking_options and give the user the seller's booking_link. ONLY call after the user has explicitly confirmed the flight, cabin,
    passenger names, contact email and total price. passenger_names: full names, one per
    traveller. cabin: economy | premium_economy | business | first.
    """
    return tools.book_flight(flight_id, passenger_names, contact_email, cabin)


@mcp.tool()
def search_hotels(
    location: str,
    check_in_date: str,
    check_out_date: str,
    adults: int = 2,
    max_price: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "relevance",
    limit: int = 6,
) -> str:
    """
    LIVE hotel recommendations from Google Hotels for a city, neighbourhood or landmark (e.g. "Paris",
    "Shinjuku Tokyo", "near Louvre Museum"). Dates are YYYY-MM-DD. Returns name, stars, guest rating, price per
    night and total, amenities, photo, website and map pins. max_price is per night in USD; min_rating 3.5 / 4 /
    4.5; sort_by is relevance | lowest_price | highest_rating | most_reviewed. The chat shows hotel cards + a map.
    """
    return tools.search_hotels(location, check_in_date, check_out_date, adults, max_price, min_rating, sort_by, limit)


@mcp.tool()
def explore_places(location: str, category: str = "attractions", query: str | None = None, limit: int = 8) -> str:
    """
    LIVE places to explore at a destination from Google Maps, with photos, ratings, opening state, address and
    map pins. category: attractions | shopping | markets | restaurants | cafes | nightlife | museums | parks |
    beaches | kids. Use category "shopping" (or "markets") for shopping recommendations. Pass query for anything
    specific (e.g. "street food", "luxury boutiques", "souvenir shops"). The chat shows place cards + a map.
    """
    return tools.explore_places(location, category, query, limit)


@mcp.tool()
def find_social_videos(place: str, location: str | None = None, platform: str = "auto", limit: int = 6) -> str:
    """
    LIVE public short videos about a specific attraction, landmark, neighbourhood or city (e.g. place="Eiffel
    Tower", location="Paris") so the traveller can see what it really looks like. platform: auto (Instagram
    Reels first; if none are found, TikTok) | instagram | tiktok. Returns title, caption, creator, duration,
    thumbnail and the official embed link; the chat shows playable video cards. Use it when the user asks about
    a specific place or attraction, or wants to see / visualise somewhere.
    """
    return tools.find_social_videos(place, location, platform, limit)


@mcp.tool()
def get_m1_travel_plans(destination: str | None = None, customer_type: str = "any", trip_days: int | None = None) -> str:
    """
    M1 Singapore mobile plans for travel: roaming data, prepaid roaming packs, Roam Like Home, Daily/Data Passport
    and Tourist SIMs. Use for ANY question about SIMs, eSIMs, roaming, data or prepaid plans for a trip; recommend
    ONLY these M1 plans. destination: city or country (e.g. "Bali", "Vietnam"); customer_type: prepaid (M1 M Card) |
    postpaid (M1 Bespoke) | visitor (coming to Singapore) | any; trip_days: length of the trip, for totals.
    """
    return tools.get_m1_travel_plans(destination, customer_type, trip_days)


@mcp.tool()
def find_ground_transport(from_location: str, to_location: str, include_taxis: bool = True) -> str:
    """
    LIVE ground transport between two places (e.g. airport to hotel) from Google Maps: driving (taxi ride time
    and distance), public transit (with fare when known) and walking options, plus local taxi / cab companies
    near the start point with rating, phone and whether they're open now. Use addresses, landmarks or
    "<airport name> airport". Real-time ride-hailing (Uber/Lyft/Grab) availability is not available.
    """
    return tools.find_ground_transport(from_location, to_location, include_taxis)


@mcp.tool()
def get_booking(booking_ref: str) -> str:
    """Retrieve an existing booking by its 6-character reference."""
    return tools.get_booking(booking_ref)


@mcp.tool()
def cancel_booking(booking_ref: str) -> str:
    """Cancel a booking. ONLY call after the user explicitly confirms the cancellation."""
    return tools.cancel_booking(booking_ref)


if __name__ == "__main__":
    mcp.run()
