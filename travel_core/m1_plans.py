"""
M1 Singapore travel connectivity plans (roaming, prepaid packs, tourist SIMs).

Travel Buddy recommends ONLY M1 Singapore plans for SIM / eSIM / roaming / data questions.
Details were taken from M1's website on AS_OF; every result links back to the M1 page so the
traveller can confirm the latest price before buying.
"""

from __future__ import annotations

from .airports import search_airports

AS_OF = "2026-11-13"
SRC = {
    "prepaid_roaming": "https://www.m1.com.sg/travel/going-overseas/prepaid-roaming",
    "data_passport": "https://www.m1.com.sg/mobile/mobile-add-ons/roaming/data-passport",
    "daily_passport": "https://www.m1.com.sg/mobile/mobile-add-ons/roaming/daily-passport",
    "tourist_sim": "https://www.m1.com.sg/mobile/prepaid-plans/tourist-sim",
    "roaming": "https://www.m1.com.sg/roaming",
}

ALIASES = {"uk": "United Kingdom", "united kingdom": "United Kingdom", "england": "United Kingdom", "britain": "United Kingdom",
           "usa": "USA", "us": "USA", "united states": "USA", "america": "USA", "korea": "South Korea", "south korea": "South Korea",
           "uae": "United Arab Emirates", "dubai": "United Arab Emirates", "hong kong sar": "Hong Kong", "macao": "Macau",
           "viet nam": "Vietnam", "lao": "Laos", "burma": "Myanmar", "the philippines": "Philippines", "brunei darussalam": "Brunei"}
ASEAN = ["Brunei", "Cambodia", "Indonesia", "Laos", "Malaysia", "Myanmar", "Philippines", "Singapore", "Thailand", "Vietnam"]

PREPAID_15 = ["Australia", "Bangladesh", "Cambodia", "Hong Kong", "Indonesia", "Macau", "Malaysia", "Sri Lanka", "Taiwan",
              "Thailand", "United Kingdom", "USA"]
PREPAID_28 = PREPAID_15 + ["Japan", "New Zealand", "Philippines", "Saudi Arabia", "South Korea"]
ROAM_LIKE_HOME = ["Malaysia", "Indonesia", "Hong Kong", "Macau", "Taiwan"]

DAILY_TIERS = [
    (2.95, ["Malaysia", "Thailand", "Indonesia"]),
    (3.95, ["Bangladesh", "Cambodia", "China", "Hong Kong", "Japan", "Macau", "South Korea", "Sri Lanka", "Taiwan"]),
    (5.95, ["Australia", "Brunei", "Canada", "India", "Kuwait", "Mongolia", "Philippines", "Russia", "Saudi Arabia", "USA", "Vietnam"]),
    (8.95, ["Laos", "Myanmar", "United Kingdom", "United Arab Emirates", "New Zealand", "Maldives", "France", "Germany", "Italy",
            "Spain", "Netherlands", "Switzerland"]),            # $8.95 tier covers 60+ destinations incl. Europe
]
DAILY_SUBSCRIPTION = 4.95

DATA_PASSPORTS = [
    ("$15 Data Passport", 15, ["Bangladesh", "Cambodia", "Fiji", "Hong Kong", "Indonesia", "Isle of Man", "Laos", "Macau",
                               "Malaysia", "Saudi Arabia", "Sri Lanka", "Taiwan", "Thailand"]),
    ("$35 ASEAN Data Passport", 35, ["Brunei", "Cambodia", "Indonesia", "Laos", "Malaysia", "Myanmar", "Philippines", "Thailand", "Vietnam"]),
    ("$35 Data Passport", 35, ["Australia", "Bahrain", "Brazil", "Canada", "China", "Germany", "India", "Italy", "Japan", "Kuwait",
                               "Maldives", "Mauritius", "Mongolia", "Netherlands", "New Zealand", "Nigeria", "Peru", "Russia",
                               "South Africa", "South Korea", "Spain", "United Arab Emirates", "United Kingdom", "USA"]),
    ("$65 Asia Data Passport", 65, ["Bangladesh", "Brunei", "Cambodia", "China", "Hong Kong", "India", "Indonesia", "Israel", "Japan",
                                    "Laos", "Macau", "Malaysia", "Maldives", "Mongolia", "Myanmar", "Nepal", "Pakistan", "Philippines",
                                    "Qatar", "Saudi Arabia", "South Korea", "Sri Lanka", "Taiwan", "Thailand", "United Arab Emirates", "Vietnam"]),
    ("$65 Europe Data Passport", 65, ["United Kingdom", "France", "Germany", "Italy", "Spain", "Netherlands", "Switzerland", "Portugal",
                                      "Greece", "Austria", "Belgium", "Ireland", "Sweden", "Norway", "Denmark", "Finland", "Poland"]),
]

TOURIST_SIMS = [
    {"name": "$12 500GB All-in-One Tourist SIM", "price": 12, "data": "500GB across SG, MY, ID, HK, MO, TW & TH + 20GB APAC roaming + 7GB international roaming",
     "validity": "30 days", "calls_sms": "500 local mins, unlimited incoming, 100 SMS, 500 IDD 033 mins to APAC"},
    {"name": "$13 500GB Data-only Tourist SIM / eSIM", "price": 13, "data": "500GB across SG, MY, ID, HK, MO, TW & TH + 20GB APAC + 7GB international",
     "validity": "30 days", "calls_sms": "Data only (no calls or SMS)"},
    {"name": "$14 600GB All-in-One Tourist SIM", "price": 14, "data": "600GB across SG, MY, ID, HK, MO, TW & TH + 20GB APAC + 8GB international",
     "validity": "30 days", "calls_sms": "500 local mins, unlimited incoming, 50 SMS, 500 IDD 033 mins to APAC"},
    {"name": "$25 750GB All-in-One Tourist SIM", "price": 25, "data": "750GB across SG, MY, ID, HK, MO, TW & TH + 30GB APAC + 15GB international",
     "validity": "30 days", "calls_sms": "5,000 local mins, unlimited incoming, 400 SMS, 700 IDD 033 mins"},
    {"name": "$30 1000GB All-in-One Tourist SIM", "price": 30, "data": "1000GB across SG, MY, ID, HK, MO, TW & TH + 35GB APAC + 18GB international",
     "validity": "30 days", "calls_sms": "3,000 local mins, 1,000 SMS, 900 IDD 033 mins to APAC"},
]
TOURIST_WHERE = "M1 Shops, Changi Airport (Meet & Greet T1-T4, Changi Recommends T1 & T3), selected 7-Eleven and Cheers, M1 retailers, or online"


def resolve_country(destination: str | None) -> str | None:
    if not destination:
        return None
    d = destination.strip()
    if d.lower() in ALIASES:
        return ALIASES[d.lower()]
    known = {c.lower(): c for c in PREPAID_28 + ASEAN + [n for _, cs in DAILY_TIERS for n in cs] + [n for _, _, cs in DATA_PASSPORTS for n in cs]}
    if d.lower() in known:
        return known[d.lower()]
    hits = search_airports(d, limit=1)            # a city ("Bali", "Phuket") -> its country
    if hits:
        c = hits[0].country
        return ALIASES.get(c.lower(), c)
    return d.title()


def m1_travel_plans(destination: str | None = None, customer_type: str = "any", trip_days: int | None = None) -> dict:
    """Best-fit M1 Singapore plans for a trip. customer_type: prepaid | postpaid | visitor | any."""
    ct = (customer_type or "any").lower()
    if ct not in ("prepaid", "postpaid", "visitor", "any"):
        ct = "any"
    country = resolve_country(destination)
    days = max(1, min(int(trip_days), 60)) if trip_days else None
    plans: list[dict] = []

    if ct in ("visitor", "any") and (country in (None, "Singapore") or ct == "visitor"):
        for t in TOURIST_SIMS:
            plans.append({"group": "Tourist SIM (visiting Singapore)", "name": t["name"], "price_sgd": t["price"], "data": t["data"],
                          "validity": t["validity"], "extras": t["calls_sms"], "how_to_buy": TOURIST_WHERE,
                          "note": "Passport-registered SIMs are valid for 30 days.", "link": SRC["tourist_sim"]})

    if country and country != "Singapore":
        if ct in ("prepaid", "any"):
            if country in ROAM_LIKE_HOME:
                plans.append({"group": "Prepaid (M Card)", "name": "Roam Like Home", "price_sgd": 0,
                              "data": "Use your local Singapore prepaid data", "validity": "While your plan is active",
                              "extras": "No extra charge", "how_to_buy": "M1 Prepaid App / Portal, or dial #100*8*1#",
                              "link": SRC["prepaid_roaming"], "best_for": "Cheapest: free in " + country})
            for name, price, countries, validity in (("$15 Roaming Pack", 15, PREPAID_15, "10 days"), ("$28 Roaming Pack", 28, PREPAID_28, "10 days")):
                if country in countries:
                    plans.append({"group": "Prepaid (M Card)", "name": name, "price_sgd": price, "data": "2GB",
                                  "validity": validity, "extras": "10 mins outgoing calls", "how_to_buy": "M1 Prepaid App or Prepaid Portal (pick your activation date)",
                                  "link": SRC["prepaid_roaming"]})
                    break                                    # the cheaper pack that covers the country
        if ct in ("postpaid", "any"):
            tier = next((p for p, cs in DAILY_TIERS if country in cs), None)
            if tier:
                total = round(tier * days + DAILY_SUBSCRIPTION, 2) if days else None
                plans.append({"group": "Postpaid (Bespoke)", "name": f"Daily Passport (${tier:.2f}/day)", "price_sgd": tier,
                              "data": "3GB per 24 hours", "validity": "Per day, auto-activates on arrival",
                              "extras": f"+ ${DAILY_SUBSCRIPTION:.2f} per bill cycle in the month you travel",
                              "trip_total_sgd": total, "how_to_buy": "My M1+ App → Roaming → choose destination",
                              "link": SRC["daily_passport"]})
            for name, price, countries in DATA_PASSPORTS:
                if country in countries:
                    plans.append({"group": "Postpaid (Bespoke)", "name": name, "price_sgd": price,
                                  "data": "Uses your local plan data (over 15GB: $12.22/GB)", "validity": "Monthly, recurring",
                                  "extras": "One-time activation fee may apply", "trip_total_sgd": price,
                                  "how_to_buy": "My M1+ App → Roaming → Data Passport", "link": SRC["data_passport"]})
                    break

    # pick a headline recommendation per traveller type
    def cost(p):
        return p.get("trip_total_sgd", p["price_sgd"]) if p.get("trip_total_sgd") is not None else p["price_sgd"]
    best = {}
    for p in plans:
        g = p["group"]
        if g not in best or cost(p) < cost(best[g]):
            best[g] = p
    for p in plans:
        p["recommended"] = best.get(p["group"]) is p

    return {
        "kind": "m1_plans",
        "provider": "M1 Singapore",
        "destination": destination, "country": country, "customer_type": ct, "trip_days": days,
        "is_asean": country in ASEAN if country else None,
        "plans": plans,
        "as_of": AS_OF,
        "source": "m1.com.sg",
        "links": SRC,
        "note": ("Only M1 Singapore plans are recommended. Prices are from m1.com.sg as of " + AS_OF +
                 "; confirm on the M1 page before buying." + ("" if plans else
                 " No listed M1 plan matched this destination: suggest checking M1's roaming page or Pay-as-you-go Roaming.")),
    }
