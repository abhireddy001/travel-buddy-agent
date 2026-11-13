"""M1 Singapore travel plans (no network)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_core.m1_plans import m1_travel_plans, resolve_country  # noqa: E402
from travel_core.tools import TravelTools  # noqa: E402


def test_city_resolves_to_country():
    assert resolve_country("Bali") == "Indonesia" and resolve_country("Phuket") == "Thailand"
    assert resolve_country("uk") == "United Kingdom" and resolve_country("Vietnam") == "Vietnam"


def test_prepaid_bali_prefers_free_roam_like_home():
    r = m1_travel_plans("Bali", "prepaid", 5)
    names = [p["name"] for p in r["plans"]]
    assert names[0] == "Roam Like Home" and "$15 Roaming Pack" in names
    assert next(p for p in r["plans"] if p["recommended"])["name"] == "Roam Like Home"
    assert r["provider"] == "M1 Singapore" and all("m1.com.sg" in p["link"] for p in r["plans"])


def test_postpaid_vietnam_compares_daily_vs_asean_passport():
    short = m1_travel_plans("Vietnam", "postpaid", 3)
    daily = next(p for p in short["plans"] if p["name"].startswith("Daily Passport"))
    assert daily["price_sgd"] == 5.95 and daily["trip_total_sgd"] == round(5.95 * 3 + 4.95, 2)
    assert next(p for p in short["plans"] if p["recommended"])["name"].startswith("Daily Passport")
    long = m1_travel_plans("Vietnam", "postpaid", 10)                       # 10 days: $64.45 vs $35 monthly
    assert next(p for p in long["plans"] if p["recommended"])["name"] == "$35 ASEAN Data Passport"


def test_visitor_to_singapore_gets_tourist_sims():
    r = m1_travel_plans("Singapore", "visitor")
    assert r["plans"] and all(p["group"].startswith("Tourist SIM") for p in r["plans"])
    assert min(p["price_sgd"] for p in r["plans"]) == 12


def test_tool_is_registered_and_only_m1():
    t = TravelTools()
    r = json.loads(t.get_m1_travel_plans("Bangkok", "any", 4))
    assert r["kind"] == "m1_plans" and "Only M1 Singapore plans" in r["note"]
    text = json.dumps(r).lower()
    for other in ("singtel", "starhub", "airalo", "holafly"):
        assert other not in text
