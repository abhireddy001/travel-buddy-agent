"""
Real-time flight prices from Google Flights via SerpApi (https://serpapi.com/google-flights-api).

Google Flights aggregates fares from airline websites and online travel agencies,
so one search returns live prices across carriers, and the booking-options call
lists every seller (airline direct + OTAs) with their price.

Key resolution: $SERPAPI_API_KEY, else the Secrets Manager secret named by
$SERPAPI_SECRET_ID (used by the Gateway Lambda).

Offers are referenced by short ids ("GF-xxxxxxxxxx"); the long Google booking
tokens behind them are kept in an OfferCache (memory locally, DynamoDB in Lambda).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from . import metrics
from .airports import resolve_airport
from .provider import FlightError, normalize_cabin, parse_date

SERPAPI_URL = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
OFFER_TTL_SECONDS = 6 * 3600
MAX_LIVE_SCAN_DAYS = 7
LIVE_PREFIX = "GF-"
QUOTA_CHECK_SECONDS = 900
_quota_checked_at = 0.0
_quota_lock = threading.Lock()


def is_live_id(flight_id: str) -> bool:
    return flight_id.strip().upper().startswith(LIVE_PREFIX)


# =============================================================================
# Offer cache
# =============================================================================

class OfferCache(Protocol):
    def put(self, offer_id: str, data: dict) -> None: ...
    def get(self, offer_id: str) -> dict | None: ...


class MemoryOfferCache:
    def __init__(self):
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, offer_id: str, data: dict) -> None:
        with self._lock:
            self._data[offer_id] = {**data, "_expires": time.time() + OFFER_TTL_SECONDS}

    def get(self, offer_id: str) -> dict | None:
        item = self._data.get(offer_id)
        return item if item and item["_expires"] > time.time() else None


class DynamoOfferCache:
    """Stores offers in the bookings table under pk = OFFER#<id>."""

    def __init__(self, table_name: str, region: str | None = None):
        import boto3

        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, offer_id: str, data: dict) -> None:
        self.table.put_item(Item={"pk": f"OFFER#{offer_id}", "doc": json.dumps(data),
                                  "expires_at": int(time.time() + OFFER_TTL_SECONDS)})

    def get(self, offer_id: str) -> dict | None:
        item = self.table.get_item(Key={"pk": f"OFFER#{offer_id}"}).get("Item")
        if not item or int(item["expires_at"]) < time.time():
            return None
        return json.loads(item["doc"])


# =============================================================================
# API key
# =============================================================================

_key_cache: dict[str, tuple[str | None, float]] = {}


def resolve_api_key() -> str | None:
    if os.environ.get("SERPAPI_API_KEY"):
        return os.environ["SERPAPI_API_KEY"].strip()
    secret_id = os.environ.get("SERPAPI_SECRET_ID")
    if not secret_id:
        return None
    cached = _key_cache.get(secret_id)
    if cached and cached[1] > time.time():
        return cached[0]
    import boto3

    try:
        value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"].strip()
    except Exception:
        value = None          # secret not set yet -> simulated mode
    _key_cache[secret_id] = (value, time.time() + 300)
    return value


# =============================================================================
# Client
# =============================================================================

def _hm(minutes: int | None) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes else "?"


class GoogleFlightsLive:
    """Live Google Flights search through SerpApi."""

    def __init__(self, api_key: str, cache: OfferCache | None = None, currency: str = "USD",
                 timeout: int = 40, today: date | None = None):
        self.api_key = api_key
        self.cache = cache or MemoryOfferCache()
        self.currency = currency
        self.timeout = timeout
        self._today = today

    @property
    def today(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    # ------------------------------------------------------------- transport
    def _report_quota(self) -> None:
        """Publish remaining SerpApi searches (account.json is free) at most every 15 minutes."""
        global _quota_checked_at
        if not metrics.in_lambda():
            return
        with _quota_lock:
            if time.time() - _quota_checked_at < QUOTA_CHECK_SECONDS:
                return
            _quota_checked_at = time.time()
        try:
            url = "https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": self.api_key})
            with urllib.request.urlopen(url, timeout=5) as resp:
                acct = json.loads(resp.read().decode("utf-8"))
            left = acct.get("total_searches_left", acct.get("plan_searches_left"))
            if left is not None:
                metrics.serpapi_quota(float(left))
        except Exception:
            pass

    def _get(self, params: dict, engine: str = "google_flights") -> dict:
        if engine == "google_flights":
            kind = "booking_options" if "booking_token" in params else "search"
        else:
            kind = engine.removeprefix("google_")
        started = time.perf_counter()
        ok = False
        try:
            data = self._get_raw(params, engine)
            ok = True
            return data
        finally:
            metrics.serpapi_request((time.perf_counter() - started) * 1000, ok, kind)
            self._report_quota()

    def _get_raw(self, params: dict, engine: str = "google_flights") -> dict:
        query = {"engine": engine, "hl": "en", "gl": "us", "currency": self.currency,
                 **{k: v for k, v in params.items() if v is not None}, "api_key": self.api_key}
        url = f"{SERPAPI_URL}?{urllib.parse.urlencode(query)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except Exception:
                msg = str(e)
            raise FlightError(f"Live lookup failed: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise FlightError(f"Live data service unreachable: {e}") from None
        if data.get("error"):
            # "Google Flights hasn't returned any results" is a normal empty search.
            if "hasn't returned any results" in data["error"]:
                return {**data, "best_flights": [], "other_flights": []}
            raise FlightError(f"Live lookup failed: {data['error']}")
        return data

    def _search_params(self, origin: str, destination: str, dep: date, passengers: int,
                       cabin: str, max_stops: int | None) -> dict:
        stops = None if max_stops is None else {0: 1, 1: 2}.get(max_stops, 3)
        return {
            "departure_id": origin, "arrival_id": destination, "outbound_date": dep.isoformat(),
            "type": 2,  # one-way
            "adults": passengers, "travel_class": TRAVEL_CLASS[cabin], "stops": stops,
        }

    def _validate(self, origin: str, destination: str, departure_date, passengers: int, cabin: str):
        o = resolve_airport(origin).code if len(origin.strip()) != 3 else origin.strip().upper()
        d = resolve_airport(destination).code if len(destination.strip()) != 3 else destination.strip().upper()
        if o == d:
            raise FlightError("Origin and destination must be different.")
        if not 1 <= passengers <= 9:
            raise FlightError("Passengers must be between 1 and 9.")
        dep = parse_date(departure_date)
        if dep < self.today:
            raise FlightError(f"Departure date {dep} is in the past (today is {self.today}).")
        return o, d, dep, normalize_cabin(cabin)

    # --------------------------------------------------------------- parsing
    def _offer(self, raw: dict, params: dict, cabin: str, passengers: int, label: str) -> dict:
        legs = raw.get("flights", [])
        token = raw.get("booking_token")
        key = token or json.dumps([l.get("flight_number") for l in legs]) + params["outbound_date"]
        offer_id = LIVE_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
        price = raw.get("price")
        offer = {
            "flight_id": offer_id,
            "source": "google_flights_live",
            "category": label,
            "price_per_passenger": round(price / passengers, 2) if price else None,
            "total_price": price,
            "currency": self.currency,
            "cabin": cabin,
            "stops": max(0, len(legs) - 1),
            "duration": _hm(raw.get("total_duration")),
            "departs": f"{legs[0]['departure_airport'].get('time')} {legs[0]['departure_airport'].get('id')}" if legs else None,
            "arrives": f"{legs[-1]['arrival_airport'].get('time')} {legs[-1]['arrival_airport'].get('id')}" if legs else None,
            "via": [l.get("id") for l in raw.get("layovers", [])],
            "layovers": [f"{l.get('id')} {_hm(l.get('duration'))}{' (overnight)' if l.get('overnight') else ''}"
                         for l in raw.get("layovers", [])],
            "airlines": sorted({l.get("airline") for l in legs if l.get("airline")}),
            "flights": [f"{l.get('flight_number')} ({l.get('airline')}) {l['departure_airport'].get('id')}->"
                        f"{l['arrival_airport'].get('id')} {l.get('airplane') or ''}".strip() for l in legs],
            "carbon_kg": round(raw["carbon_emissions"]["this_flight"] / 1000) if raw.get("carbon_emissions", {}).get("this_flight") else None,
        }
        if token:
            self.cache.put(offer_id, {"booking_token": token, "params": params, "summary": offer})
        return offer

    # ------------------------------------------------------------ public API
    def search(self, origin: str, destination: str, departure_date, passengers: int = 1,
               cabin: str = "economy", max_stops: int | None = None) -> dict:
        o, d, dep, cabin = self._validate(origin, destination, departure_date, passengers, cabin)
        params = self._search_params(o, d, dep, passengers, cabin, max_stops)
        data = self._get(params)
        offers = [self._offer(f, params, cabin, passengers, "best") for f in data.get("best_flights", [])]
        offers += [self._offer(f, params, cabin, passengers, "other") for f in data.get("other_flights", [])]
        offers = [x for x in offers if x["total_price"]]
        insights = data.get("price_insights") or {}
        return {
            "route": f"{o}-{d}",
            "departure_date": dep.isoformat(),
            "passengers": passengers,
            "source": "Google Flights (live, via SerpApi)",
            "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "offers": offers,
            "price_insights": {
                "lowest_price": insights.get("lowest_price"),
                "price_level": insights.get("price_level"),
                "typical_price_range": insights.get("typical_price_range"),
            } if insights else None,
            "price_history": insights.get("price_history", []),
            "google_flights_url": (data.get("search_metadata") or {}).get("google_flights_url"),
        }

    def booking_options(self, flight_id: str) -> dict:
        cached = self.cache.get(flight_id.strip().upper())
        if not cached:
            raise FlightError(f"Live offer {flight_id} has expired or is unknown. Please search again.")
        data = self._get({**cached["params"], "booking_token": cached["booking_token"]})
        sellers = []
        for opt in data.get("booking_options", []):
            part = opt.get("together") or opt.get("departing") or {}
            req = part.get("booking_request") or {}
            link = None
            if req.get("url") and req.get("post_data"):
                link = f"{req['url']}?{req['post_data']}"
            elif req.get("url"):
                link = req["url"]
            sellers.append({
                "seller": part.get("book_with"),
                "is_airline": bool(part.get("airline")),
                "price": part.get("price"),
                "currency": self.currency,
                "fare_details": part.get("extensions", [])[:6],
                "baggage": part.get("baggage_prices", [])[:3],
                "booking_link": link,
                "phone": part.get("booking_phone"),
            })
        sellers.sort(key=lambda s: (s["price"] is None, s["price"] or 0))
        return {
            "flight": cached["summary"],
            "source": "Google Flights (live, via SerpApi)",
            "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "sellers": sellers,
            "cheapest_seller": sellers[0]["seller"] if sellers else None,
            "google_flights_url": (data.get("search_metadata") or {}).get("google_flights_url"),
            "note": "Seats and price are confirmed by the seller at checkout. Booking and payment happen on the "
                    "seller's website.",
        }

    def cheapest_dates(self, origin: str, destination: str, start_date, days: int = 7,
                       cabin: str = "economy", max_stops: int | None = None) -> dict:
        if not 1 <= days <= MAX_LIVE_SCAN_DAYS:
            raise FlightError(f"Live date scans cover 1-{MAX_LIVE_SCAN_DAYS} days per request "
                              "(each day is one live search).")
        first = parse_date(start_date)
        dates = [first + timedelta(days=i) for i in range(days)]

        def one(d: date):
            try:
                r = self.search(origin, destination, d, 1, cabin, max_stops)
            except FlightError as e:
                return {"date": d.isoformat(), "error": str(e)}
            if not r["offers"]:
                return {"date": d.isoformat(), "lowest_fare": None}
            best = min(r["offers"], key=lambda x: x["total_price"])
            return {"date": d.isoformat(), "weekday": d.strftime("%a"), "lowest_fare": best["total_price"],
                    "flight_id": best["flight_id"], "airlines": best["airlines"], "stops": best["stops"],
                    "duration": best["duration"], "price_level": (r["price_insights"] or {}).get("price_level")}

        with ThreadPoolExecutor(max_workers=min(days, 7)) as pool:
            rows = list(pool.map(one, dates))
        priced = sorted([r for r in rows if r.get("lowest_fare")], key=lambda r: r["lowest_fare"])
        if not priced:
            raise FlightError("No live fares found in that window.")
        avg = sum(r["lowest_fare"] for r in priced) / len(priced)
        return {
            "route": f"{origin.upper()}-{destination.upper()}",
            "cabin": normalize_cabin(cabin),
            "currency": self.currency,
            "source": "Google Flights (live, via SerpApi)",
            "window": {"start": dates[0].isoformat(), "end": dates[-1].isoformat(), "days_searched": days},
            "average_lowest_fare": round(avg, 2),
            "cheapest_dates": [{**r, "savings_vs_window_average": round(avg - r["lowest_fare"], 2)} for r in priced[:5]],
            "most_expensive_date": priced[-1],
            "days": rows,
        }

    def price_history_trend(self, origin: str, destination: str, departure_date,
                            cabin: str = "economy", max_stops: int | None = None) -> dict:
        """Google's recorded lowest price for this departure date over the past ~60 days."""
        r = self.search(origin, destination, departure_date, 1, cabin, max_stops)
        history = r.get("price_history") or []
        if len(history) < 2:
            raise FlightError("Google Flights has no price history for this route/date yet.")
        o, d = r["route"].split("-")
        rows = []
        for ts, price in history:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            rows.append({"date": day.isoformat(), "weekday": day.strftime("%a"), "lowest_fare": float(price)})
        prices = [x["lowest_fare"] for x in rows]
        half = 3
        for i, row in enumerate(rows):
            chunk = prices[max(0, i - half): i + half + 1]
            row["moving_avg_7d"] = round(sum(chunk) / len(chunk), 2)
        low = min(rows, key=lambda x: x["lowest_fare"])
        high = max(rows, key=lambda x: x["lowest_fare"])
        current = r["price_insights"] or {}
        first, last = prices[0], prices[-1]
        change = round((last / first - 1) * 100, 1) if first else 0.0
        offers = sorted(r["offers"], key=lambda x: x["total_price"])
        return {
            "kind": "history",
            "route": r["route"],
            "route_label": f"{o} to {d}",
            "cabin": normalize_cabin(cabin),
            "currency": self.currency,
            "as_of": r["retrieved_at"][:10],
            "departure_date": r["departure_date"],
            "source": r["source"],
            "window": {"start": rows[0]["date"], "end": rows[-1]["date"], "days": len(rows)},
            "summary": {
                "current_lowest_price": current.get("lowest_price") or (offers[0]["total_price"] if offers else None),
                "price_level": current.get("price_level"),
                "typical_price_range": current.get("typical_price_range"),
                "history_low": {"date": low["date"], "lowest_fare": low["lowest_fare"]},
                "history_high": {"date": high["date"], "lowest_fare": high["lowest_fare"]},
                "change_over_history_pct": change,
                "trend_direction": "rising" if change > 3 else "falling" if change < -3 else "flat",
            },
            "cheapest_offers_now": offers[:3],
            "google_flights_url": r["google_flights_url"],
            "daily": rows,
        }
