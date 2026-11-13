"""
Flight data provider for the Travel Agent.

`FlightProvider` is the interface the MCP server talks to. The shipped
implementation, `SimulatedFlightProvider`, generates a *deterministic* global
flight market: the same route + date always yields the same itineraries, so
search -> availability -> booking stays consistent across separate tool calls
without needing a database. Swap it for a real GDS / NDC / Amadeus-style
provider by implementing the same methods.

Fare model (all multiplicative):
    distance base  x cabin  x season  x day-of-week  x booking lead time
    x carrier positioning  x stops discount  x seat scarcity  x daily noise
"""

from __future__ import annotations

import hashlib
import os
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .airports import AIRPORTS, HUBS, Airport, distance_km, resolve_airport
from .booking_store import BookingStore, BookingStoreError, JsonBookingStore

CABINS = {
    # cabin: (price multiplier, seats per flight)
    "economy": (1.0, 180),
    "premium_economy": (1.65, 28),
    "business": (3.4, 32),
    "first": (5.8, 8),
}

# Fictional carriers - this is simulated data, not real airline inventory.
CARRIERS = [
    # code, name, price positioning (1.0 = market average)
    ("SB", "SkyBridge Airways", 1.00),
    ("AQ", "Aquila Air", 1.12),
    ("NV", "Nova Jet", 0.82),
    ("MR", "Meridian International", 1.06),
    ("ZP", "Zephyr Airlines", 0.90),
    ("OC", "Orion Continental", 1.18),
    ("HL", "Horizon Link", 0.95),
]
CARRIER_BY_CODE = {c[0]: c for c in CARRIERS}

CRUISE_KMH = 830.0
MAX_NONSTOP_KM = 15_500.0
MAX_SEARCH_DAYS_AHEAD = 330


class FlightError(ValueError):
    """Raised for invalid search / booking requests (surfaced to the agent as text)."""


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class FlightSegment:
    flight_number: str
    carrier: str
    origin: str
    destination: str
    depart_local: str      # ISO local time at origin
    arrive_local: str      # ISO local time at destination
    duration_minutes: int


@dataclass
class FlightOption:
    flight_id: str
    origin: str
    destination: str
    departure_date: str
    segments: list[FlightSegment]
    stops: int
    total_duration_minutes: int
    cabin: str
    price_per_passenger: float
    currency: str
    seats_left: int
    carriers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Booking:
    booking_ref: str
    flight_id: str
    cabin: str
    passengers: list[str]
    contact_email: str
    total_price: float
    currency: str
    status: str
    created_at: str
    itinerary: dict

    def to_dict(self) -> dict:
        return asdict(self)


class FlightProvider(Protocol):
    """Interface a real flight API integration must implement."""

    def search_flights(self, origin: str, destination: str, departure_date: date,
                       passengers: int = 1, cabin: str = "economy",
                       max_stops: int | None = None) -> list[FlightOption]: ...

    def get_flight(self, flight_id: str, cabin: str = "economy") -> FlightOption: ...

    def book(self, flight_id: str, cabin: str, passengers: list[str],
             contact_email: str) -> Booking: ...

    def get_booking(self, booking_ref: str) -> Booking: ...

    def cancel_booking(self, booking_ref: str) -> Booking: ...


# =============================================================================
# Helpers
# =============================================================================

def _seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


def _rng(*parts: object) -> random.Random:
    return random.Random(_seed(*parts))


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value.strip())
    except ValueError as e:
        raise FlightError(f"Invalid date '{value}'. Use YYYY-MM-DD.") from e


def normalize_cabin(cabin: str) -> str:
    key = cabin.strip().lower().replace(" ", "_").replace("-", "_")
    if key == "premium":
        key = "premium_economy"
    if key not in CABINS:
        raise FlightError(f"Unknown cabin '{cabin}'. Choose one of: {', '.join(CABINS)}.")
    return key


def season_factor(d: date) -> float:
    """Peak summer + year-end holidays are expensive; late Jan-Feb and Nov are cheap."""
    month_factor = {
        1: 0.92, 2: 0.88, 3: 0.97, 4: 1.02, 5: 1.0, 6: 1.15,
        7: 1.25, 8: 1.20, 9: 0.96, 10: 0.98, 11: 0.90, 12: 1.08,
    }[d.month]
    if (d.month == 12 and d.day >= 18) or (d.month == 1 and d.day <= 4):
        month_factor *= 1.22   # holiday peak
    return month_factor


def weekday_factor(d: date) -> float:
    # Mon .. Sun - Tue/Wed are cheapest, Fri/Sun most expensive.
    return (1.0, 0.91, 0.90, 0.98, 1.12, 1.02, 1.10)[d.weekday()]


def lead_time_factor(days_out: int) -> float:
    """
    Fares are lowest roughly 3-10 weeks before departure, rise sharply inside
    two weeks and drift up slightly for very early bookings.
    """
    if days_out < 0:
        raise FlightError("Departure date is in the past.")
    if days_out <= 3:
        return 1.55
    if days_out <= 7:
        return 1.38
    if days_out <= 14:
        return 1.20
    if days_out <= 21:
        return 1.06
    if days_out <= 70:
        return 0.94
    if days_out <= 120:
        return 0.99
    return 1.04


def distance_base_fare(km: float) -> float:
    """Economy base fare in USD - tapering per-km rate, like real markets."""
    # ~$170 @1,000 km, ~$575 @5,500 km (NYC-LON), ~$1,360 @16,000 km
    return 45.0 + 0.35 * km ** 0.85


# =============================================================================
# Simulated provider
# =============================================================================

class SimulatedFlightProvider:
    """Deterministic simulated flight market covering the airports in `airports.py`."""

    currency = "USD"

    def __init__(self, bookings_file: str | Path | None = None, today: date | None = None,
                 store: BookingStore | None = None):
        if store is None:
            default_file = Path(__file__).resolve().parents[1] / "data" / "bookings.json"
            store = JsonBookingStore(bookings_file or os.environ.get("TRAVEL_BOOKINGS_FILE", default_file))
        self.store = store
        self._today = today

    # ---------------------------------------------------------------- dates
    @property
    def today(self) -> date:
        return self._today or date.today()

    def _validate_departure(self, d: date) -> None:
        if d < self.today:
            raise FlightError(f"Departure date {d} is in the past (today is {self.today}).")
        if (d - self.today).days > MAX_SEARCH_DAYS_AHEAD:
            raise FlightError(
                f"Schedules are only published {MAX_SEARCH_DAYS_AHEAD} days ahead "
                f"(latest: {self.today + timedelta(days=MAX_SEARCH_DAYS_AHEAD)})."
            )

    # ------------------------------------------------------------ itineraries
    def _route_carriers(self, o: Airport, d: Airport) -> list[tuple]:
        """Carriers serving a route are fixed per (unordered) airport pair."""
        rng = _rng("carriers", *sorted((o.code, d.code)))
        return rng.sample(CARRIERS, k=rng.randint(2, 4))

    def _hubs_for(self, o: Airport, d: Airport, direct_km: float) -> list[Airport]:
        options = []
        for code in HUBS:
            if code in (o.code, d.code):
                continue
            hub = AIRPORTS[code]
            leg1, leg2 = distance_km(o, hub), distance_km(hub, d)
            if leg1 < 250 or leg2 < 250 or max(leg1, leg2) > MAX_NONSTOP_KM:
                continue
            detour = (leg1 + leg2) / max(direct_km, 1.0)
            if detour <= 1.35:
                options.append((detour, hub))
        options.sort(key=lambda x: x[0])
        return [h for _, h in options[:3]]

    def _build_itineraries(self, o: Airport, d: Airport, dep: date) -> list[dict]:
        """Raw schedule for a route/date: list of {segments: [(carrier, flight_no, from, to, dep_dt_utc, minutes)], km}."""
        direct_km = distance_km(o, d)
        if direct_km < 150:
            raise FlightError(f"{o.code} and {d.code} are too close for scheduled flights.")
        carriers = self._route_carriers(o, d)
        rng = _rng("schedule", o.code, d.code, dep.isoformat())
        o_tz = ZoneInfo(o.tz)
        itineraries: list[dict] = []

        def leg_minutes(km: float) -> int:
            return int(round((km / CRUISE_KMH) * 60 + 35))

        def local_departure(hour: int, minute: int) -> datetime:
            return datetime(dep.year, dep.month, dep.day, hour, minute, tzinfo=o_tz)

        # Nonstops: each route carrier has 1-2 daily nonstops if in range.
        if direct_km <= MAX_NONSTOP_KM:
            for code, _, _ in carriers[: rng.randint(1, len(carriers))]:
                for _ in range(1 if direct_km > 6000 else rng.randint(1, 2)):
                    dep_local = local_departure(rng.randint(6, 22), rng.choice((0, 15, 30, 45)))
                    itineraries.append({
                        "km": direct_km,
                        "legs": [(code, rng.randint(100, 989), o, d, dep_local, leg_minutes(direct_km))],
                    })

        # One-stop connections via hubs.
        for hub in self._hubs_for(o, d, direct_km):
            for _ in range(rng.randint(1, 2)):
                c1 = rng.choice(carriers)[0]
                c2 = c1 if rng.random() < 0.7 else rng.choice(CARRIERS)[0]
                dep_local = local_departure(rng.randint(5, 23), rng.choice((0, 10, 25, 40, 55)))
                m1 = leg_minutes(distance_km(o, hub))
                layover = rng.randint(70, 330)
                leg2_dep = (dep_local + timedelta(minutes=m1 + layover)).astimezone(ZoneInfo(hub.tz))
                itineraries.append({
                    "km": distance_km(o, hub) + distance_km(hub, d),
                    "legs": [
                        (c1, rng.randint(100, 989), o, hub, dep_local, m1),
                        (c2, rng.randint(1000, 1989), hub, d, leg2_dep, leg_minutes(distance_km(hub, d))),
                    ],
                })

        if not itineraries:
            raise FlightError(f"No scheduled service found between {o.code} and {d.code}.")
        itineraries.sort(key=lambda it: it["legs"][0][4])
        return itineraries

    # ---------------------------------------------------------------- pricing
    def _load_factor(self, flight_id: str, cabin: str, dep: date) -> float:
        """Share of seats already sold (before our own bookings)."""
        days_out = (dep - self.today).days
        base = 0.84 - min(days_out, 200) / 300          # fuller as departure nears
        base += (season_factor(dep) - 1.0) * 0.35       # busier in peak season
        noise = _rng("load", flight_id, cabin).uniform(-0.10, 0.10)
        return max(0.05, min(0.985, base + noise))

    def _price(self, flight_id: str, itinerary: dict, cabin: str, dep: date, load: float) -> float:
        days_out = (dep - self.today).days
        carrier_code = itinerary["legs"][0][0]
        positioning = CARRIER_BY_CODE[carrier_code][2]
        stops = len(itinerary["legs"]) - 1
        scarcity = 1.0 + max(0.0, load - 0.7) * 1.3
        daily = _rng("daily", itinerary["legs"][0][2].code, itinerary["legs"][-1][3].code,
                     dep.isoformat()).uniform(0.93, 1.08)
        noise = _rng("fare", flight_id, cabin).uniform(0.94, 1.07)
        fare = (
            distance_base_fare(itinerary["km"])
            * CABINS[cabin][0]
            * season_factor(dep)
            * weekday_factor(dep)
            * lead_time_factor(days_out)
            * positioning
            * (0.84 if stops else 1.0)
            * scarcity
            * daily
            * noise
        )
        return round(max(fare, 39.0), 2)

    def _base_seats(self, flight_id: str, cabin: str, dep: date) -> int:
        """Seats left before any bookings made through this agency."""
        return max(0, int(CABINS[cabin][1] * (1 - self._load_factor(flight_id, cabin, dep))))

    def _to_option(self, o: Airport, d: Airport, dep: date, idx: int,
                   itinerary: dict, cabin: str) -> FlightOption:
        flight_id = f"{o.code}-{d.code}-{dep:%Y%m%d}-{idx}"
        load = self._load_factor(flight_id, cabin, dep)
        seats_left = max(0, self._base_seats(flight_id, cabin, dep) - self.store.seats_booked(flight_id, cabin))
        segments = []
        for carrier, number, frm, to, dep_dt, minutes in itinerary["legs"]:
            arr_dt = (dep_dt + timedelta(minutes=minutes)).astimezone(ZoneInfo(to.tz))
            segments.append(FlightSegment(
                flight_number=f"{carrier}{number}",
                carrier=CARRIER_BY_CODE[carrier][1],
                origin=frm.code,
                destination=to.code,
                depart_local=dep_dt.strftime("%Y-%m-%d %H:%M"),
                arrive_local=arr_dt.strftime("%Y-%m-%d %H:%M"),
                duration_minutes=minutes,
            ))
        first_dep = itinerary["legs"][0][4]
        last = itinerary["legs"][-1]
        total = int(((last[4] + timedelta(minutes=last[5])) - first_dep).total_seconds() // 60)
        return FlightOption(
            flight_id=flight_id,
            origin=o.code,
            destination=d.code,
            departure_date=dep.isoformat(),
            segments=segments,
            stops=len(segments) - 1,
            total_duration_minutes=total,
            cabin=cabin,
            price_per_passenger=self._price(flight_id, itinerary, cabin, dep, load),
            currency=self.currency,
            seats_left=seats_left,
            carriers=sorted({s.carrier for s in segments}),
        )

    # ----------------------------------------------------------- public API
    def search_flights(self, origin: str, destination: str, departure_date: str | date,
                       passengers: int = 1, cabin: str = "economy",
                       max_stops: int | None = None) -> list[FlightOption]:
        o, d = resolve_airport(origin), resolve_airport(destination)
        if o.code == d.code:
            raise FlightError("Origin and destination must be different.")
        if passengers < 1 or passengers > 9:
            raise FlightError("Passengers must be between 1 and 9.")
        dep = parse_date(departure_date)
        self._validate_departure(dep)
        cabin = normalize_cabin(cabin)
        options = [
            self._to_option(o, d, dep, idx, it, cabin)
            for idx, it in enumerate(self._build_itineraries(o, d, dep))
        ]
        if max_stops is not None:
            options = [f for f in options if f.stops <= max_stops]
        return [f for f in options if f.seats_left >= passengers]

    def get_flight(self, flight_id: str, cabin: str = "economy") -> FlightOption:
        try:
            o_code, d_code, day, idx = flight_id.strip().upper().split("-")
            dep = datetime.strptime(day, "%Y%m%d").date()
            idx_i = int(idx)
        except ValueError as e:
            raise FlightError(f"Malformed flight_id '{flight_id}'.") from e
        o, d = resolve_airport(o_code), resolve_airport(d_code)
        self._validate_departure(dep)
        itineraries = self._build_itineraries(o, d, dep)
        if not 0 <= idx_i < len(itineraries):
            raise FlightError(f"Flight '{flight_id}' does not exist.")
        return self._to_option(o, d, dep, idx_i, itineraries[idx_i], normalize_cabin(cabin))

    def cheapest_fare(self, origin: str, destination: str, departure_date: date,
                      cabin: str = "economy", max_stops: int | None = None) -> FlightOption | None:
        options = self.search_flights(origin, destination, departure_date, 1, cabin, max_stops)
        return min(options, key=lambda f: f.price_per_passenger) if options else None

    # -------------------------------------------------------------- bookings
    def book(self, flight_id: str, cabin: str, passengers: list[str],
             contact_email: str) -> Booking:
        names = [p.strip() for p in passengers if p and p.strip()]
        if not names:
            raise FlightError("At least one passenger name is required.")
        if len(names) > 9:
            raise FlightError("A single booking can hold at most 9 passengers.")
        if "@" not in contact_email or "." not in contact_email.split("@")[-1]:
            raise FlightError(f"Invalid contact email '{contact_email}'.")
        cabin = normalize_cabin(cabin)
        flight = self.get_flight(flight_id, cabin)
        if flight.seats_left < len(names):
            raise FlightError(
                f"Only {flight.seats_left} {cabin} seat(s) left on {flight.flight_id}; "
                f"cannot book {len(names)}."
            )
        booking = Booking(
            booking_ref=uuid.uuid4().hex[:6].upper(),
            flight_id=flight.flight_id,
            cabin=cabin,
            passengers=names,
            contact_email=contact_email.strip(),
            total_price=round(flight.price_per_passenger * len(names), 2),
            currency=flight.currency,
            status="CONFIRMED",
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            itinerary=flight.to_dict(),
        )
        dep = parse_date(flight.departure_date)
        try:
            self.store.create(booking.to_dict(), self._base_seats(flight.flight_id, cabin, dep))
        except BookingStoreError as e:
            raise FlightError(str(e)) from e
        return booking

    def get_booking(self, booking_ref: str) -> Booking:
        data = self.store.get(booking_ref.strip().upper())
        if not data:
            raise FlightError(f"No booking found with reference '{booking_ref}'.")
        return Booking(**data)

    def cancel_booking(self, booking_ref: str) -> Booking:
        try:
            return Booking(**self.store.cancel(booking_ref.strip().upper()))
        except BookingStoreError as e:
            raise FlightError(str(e)) from e
