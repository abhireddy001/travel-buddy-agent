"""
Destination planning with live Google data via SerpApi:

- Hotels             engine=google_hotels            prices, ratings, photos, amenities
- Places to explore  engine=google_maps (search)     attractions, shopping, food, nightlife ... with photos
- Ground transport   engine=google_maps_directions   airport/hotel transfers by car, transit, walking
                     + engine=google_maps "taxi"     local taxi / cab companies (rating, phone, open now)

Every result includes a "map" block (markers with lat/lng) so the chat UI can draw
a map with numbered pins matching the cards.

Note: SerpApi has no real-time ride-hailing (Uber/Lyft/Grab) availability API; cab
results are local taxi companies plus the driving time and distance for the trip.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import quote

from .live_flights import GoogleFlightsLive
from .provider import FlightError, parse_date

CATEGORIES = {
    "attractions": "top tourist attractions",
    "shopping": "best shopping malls and markets",
    "markets": "local markets",
    "restaurants": "best restaurants",
    "cafes": "best cafes",
    "nightlife": "best bars and nightlife",
    "museums": "museums",
    "parks": "parks and gardens",
    "beaches": "beaches",
    "kids": "family activities for kids",
}
IG_POST = re.compile(r"^https://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(reel|reels|p|tv)/([A-Za-z0-9_-]{5,40})/?")
TT_VIDEO = re.compile(r"^https://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,40})/video/(\d{8,25})")
# TikTok Discover returns trending posts, not just travel ones: rank travel content first, drop obvious hoaxes.
TRAVEL_WORDS = ("travel", "guide", "tips", "visit", "things to do", "itinerary", "view", "sunset", "trip", "tour",
                "hotel", "worth it", "must see", "must-see", "hidden gem", "day in", "days in", "vlog", "explore",
                "#travel", "bucket list", "how to", "best", "where to")
HOAX_WORDS = ("burned down", "burnt down", "fact check", "fact-check", "hoax", "fake", "ai generated",
              "ai-generated", "#aiart", "#ai ", "conspiracy", "collapsed")
HOTEL_SORT = {"relevance": None, "lowest_price": 3, "highest_rating": 8, "most_reviewed": 13}


def _https(url: str | None) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _maps_link(place_id: str | None, name: str, lat: float | None, lng: float | None) -> str:
    if place_id:
        return f"https://www.google.com/maps/search/?api=1&query={quote(name)}&query_place_id={place_id}"
    if lat is not None and lng is not None:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}"


def _rating(v) -> float | None:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _gps(obj: dict) -> tuple[float | None, float | None]:
    g = obj.get("gps_coordinates") or {}
    return g.get("latitude"), g.get("longitude")


def _map(markers: list[dict], title: str) -> dict | None:
    pts = [m for m in markers if m.get("lat") is not None and m.get("lng") is not None]
    return {"title": title, "markers": pts} if pts else None


class LivePlaces:
    def __init__(self, client: GoogleFlightsLive):
        self.client = client

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # ------------------------------------------------------------------ hotels
    def hotels(self, location: str, check_in_date: str, check_out_date: str, adults: int = 2,
               max_price: int | None = None, min_rating: float | None = None,
               sort_by: str = "relevance", limit: int = 6) -> dict:
        ci, co = parse_date(check_in_date), parse_date(check_out_date)
        if ci < self.client.today:
            raise FlightError(f"Check-in {ci} is in the past.")
        if co <= ci:
            raise FlightError("Check-out must be after check-in.")
        if sort_by not in HOTEL_SORT:
            raise FlightError(f"sort_by must be one of {', '.join(HOTEL_SORT)}.")
        params = {"q": location, "check_in_date": ci.isoformat(), "check_out_date": co.isoformat(),
                  "adults": adults, "sort_by": HOTEL_SORT[sort_by], "max_price": max_price}
        if min_rating:
            params["rating"] = 9 if min_rating >= 4.5 else 8 if min_rating >= 4 else 7  # 4.5+ / 4+ / 3.5+
        data = self.client._get(params, engine="google_hotels")
        nights = (co - ci).days
        hotels = []
        for p in data.get("properties", []):
            rate = (p.get("rate_per_night") or {}).get("extracted_lowest")
            total = (p.get("total_rate") or {}).get("extracted_lowest")
            if rate is None:
                continue
            lat, lng = _gps(p)
            img = next((_https(i.get("thumbnail")) for i in p.get("images", []) if _https(i.get("thumbnail"))), None)
            hotels.append({
                "name": p.get("name"),
                "type": p.get("type"),
                "stars": p.get("extracted_hotel_class"),
                "rating": _rating(p.get("overall_rating")),
                "reviews": p.get("reviews"),
                "price_per_night": rate,
                "total_price": total or round(rate * nights),
                "currency": self.client.currency,
                "deal": p.get("deal"),
                "amenities": (p.get("amenities") or [])[:6],
                "nearby": [n.get("name") for n in (p.get("nearby_places") or [])[:3] if n.get("name")],
                "check_in_time": p.get("check_in_time"),
                "image": img,
                "website": _https(p.get("link")),
                "maps_link": _maps_link(None, p.get("name") or location, lat, lng),
                "lat": lat, "lng": lng,
            })
            if len(hotels) >= max(1, min(limit, 10)):
                break
        for i, h in enumerate(hotels, 1):
            h["pin"] = i
        return {
            "kind": "hotels",
            "location": location,
            "check_in_date": ci.isoformat(), "check_out_date": co.isoformat(), "nights": nights, "adults": adults,
            "source": "Google Hotels (live, via SerpApi)", "retrieved_at": self._stamp(),
            "hotels": hotels,
            "google_hotels_url": (data.get("search_metadata") or {}).get("google_hotels_url"),
            "map": _map([{"lat": h["lat"], "lng": h["lng"], "label": str(h["pin"]), "title": h["name"]} for h in hotels],
                        f"Hotels in {location}"),
            "note": "Prices are the lowest per night incl. taxes/fees where Google shows them; confirm on the booking site.",
        }

    # ------------------------------------------------------------------ places
    def places(self, location: str, category: str = "attractions", query: str | None = None, limit: int = 8) -> dict:
        if not query and category not in CATEGORIES:
            raise FlightError(f"category must be one of {', '.join(CATEGORIES)} (or pass a custom query).")
        q = f"{query or CATEGORIES[category]} in {location}"
        data = self.client._get({"type": "search", "q": q}, engine="google_maps")
        items = []
        for r in data.get("local_results", []) or ([data["place_results"]] if data.get("place_results") else []):
            lat, lng = _gps(r)
            items.append({
                "pin": len(items) + 1,
                "name": r.get("title"),
                "type": r.get("type"),
                "rating": _rating(r.get("rating")),
                "reviews": r.get("reviews"),
                "price_level": r.get("price"),
                "address": r.get("address"),
                "open_state": r.get("open_state") or r.get("hours"),
                "phone": r.get("phone"),
                "image": _https(r.get("thumbnail")),
                "website": _https(r.get("website")),
                "maps_link": _maps_link(r.get("place_id"), r.get("title") or q, lat, lng),
                "description": r.get("description"),
                "lat": lat, "lng": lng,
            })
            if len(items) >= max(1, min(limit, 12)):
                break
        return {
            "kind": "places",
            "location": location, "category": query or category, "query": q,
            "source": "Google Maps (live, via SerpApi)", "retrieved_at": self._stamp(),
            "places": items,
            "map": _map([{"lat": p["lat"], "lng": p["lng"], "label": str(p["pin"]), "title": p["name"]} for p in items],
                        f"{(query or category).title()} in {location}"),
        }

    # ------------------------------------------------- short videos (Instagram, then TikTok)
    def _tiktok_discover(self, keywords: list[str], place: str, location: str | None, limit: int) -> list[dict]:
        """SerpApi's native TikTok engine (TikTok Discover pages). Keyword pages exist for popular terms only."""
        tokens = {w for w in re.findall(r"[a-z0-9]{4,}", f"{place} {location or ''}".lower())} or {place.lower()}
        for kw in keywords:
            data = self.client._get({"q": kw, "num": 20}, engine="tiktok_discover")
            rows = []
            for r in data.get("video_results", []):
                desc = r.get("description") or ""
                low = desc.lower()
                vid, author = str(r.get("id") or ""), (r.get("author") or {})
                if not vid.isdigit() or not author.get("username"):
                    continue
                if not any(t in low for t in tokens) or any(h in low for h in HOAX_WORDS):
                    continue
                score = sum(w in low for w in TRAVEL_WORDS)
                secs = r.get("duration")
                rows.append((score, r.get("plays") or 0, {
                    "platform": "tiktok", "kind": "tiktok",
                    "title": desc.split("#")[0].strip()[:110] or f"{place} on TikTok",
                    "caption": desc[:220],
                    "author": f"@{author['username']}",
                    "author_name": author.get("name"),
                    "followers": author.get("followers"),
                    "duration": f"{secs // 60}:{secs % 60:02d}" if isinstance(secs, int) else None,
                    "plays": r.get("plays"), "likes": r.get("likes"),
                    "published": (r.get("published_date") or "")[:10] or None,
                    "thumbnail": _https(r.get("thumbnail")),
                    "url": f"https://www.tiktok.com/@{author['username']}/video/{vid}",
                    "embed_url": f"https://www.tiktok.com/player/v1/{vid}",
                }))
            if rows:
                rows.sort(key=lambda x: (-x[0], -x[1]))
                return [r for _, _, r in rows[: max(1, min(limit, 9))]]
        return []

    def _search_videos(self, subject: str, platform: str, limit: int) -> list[dict]:
        site = "instagram.com" if platform == "instagram" else "tiktok.com"
        data = self.client._get({"q": f"{subject} site:{site}"}, engine="google_videos")
        videos, seen = [], set()
        for r in data.get("video_results", []):
            link = r.get("link") or ""
            if platform == "instagram":
                m = IG_POST.match(link)
                if not m:
                    continue
                path = {"reel": "reel", "reels": "reel", "p": "p", "tv": "tv"}[m.group(1)]
                vid, author = m.group(2), None
                url, embed = f"https://www.instagram.com/{path}/{vid}/", f"https://www.instagram.com/{path}/{vid}/embed/"
                kind = "reel" if path == "reel" else "video"
            else:
                m = TT_VIDEO.match(link)
                if not m:
                    continue
                author, vid = m.group(1), m.group(2)
                url, embed, kind = f"https://www.tiktok.com/@{author}/video/{vid}", f"https://www.tiktok.com/player/v1/{vid}", "tiktok"
            if vid in seen:
                continue
            seen.add(vid)
            videos.append({
                "platform": platform, "kind": kind,
                "title": (r.get("title") or "").replace(" ...", "…").strip(),
                "caption": (r.get("snippet") or "")[:220],
                "author": f"@{author}" if author else None,
                "duration": r.get("duration"),
                "thumbnail": _https(r.get("thumbnail")),
                "url": url, "embed_url": embed,
            })
            if len(videos) >= max(1, min(limit, 9)):
                break
        return videos

    def social_videos(self, place: str, location: str | None = None, platform: str = "auto", limit: int = 6) -> dict:
        """
        Public short videos about a place: Instagram via Google Videos (site:instagram.com; SerpApi has no
        Instagram engine), and TikTok via SerpApi's native TikTok Discover engine, falling back to Google Videos
        (site:tiktok.com) when no Discover page exists. Returns official embed URLs.
        """
        platform = (platform or "auto").lower()
        if platform not in ("auto", "instagram", "tiktok"):
            raise FlightError("platform must be auto, instagram or tiktok.")
        subject = f"{place} {location}".strip() if location and location.lower() not in place.lower() else place
        tried, videos, used = [], [], None
        engine_used = None
        for p in (["instagram", "tiktok"] if platform == "auto" else [platform]):
            tried.append(p)
            if p == "tiktok":
                kws = list(dict.fromkeys(k.strip().lower() for k in (place, location) if k and k.strip()))[:2]
                videos = self._tiktok_discover(kws, place, location, limit)
                engine_used = "tiktok_discover"
                if not videos:   # Discover pages exist only for popular keywords
                    videos, engine_used = self._search_videos(subject, p, limit), "google_videos"
            else:
                videos, engine_used = self._search_videos(subject, p, limit), "google_videos"
            if videos:
                used = p
                break
        name = {"instagram": "Instagram", "tiktok": "TikTok"}
        return {
            "kind": "social_videos",
            "place": place, "location": location,
            "platform": used, "platforms_tried": tried,
            "fallback_used": platform == "auto" and used == "tiktok",
            "source": ("TikTok (live, SerpApi TikTok Discover)" if engine_used == "tiktok_discover" and used == "tiktok"
                       else f"{name.get(used, 'Instagram/TikTok')} via Google Videos (live, via SerpApi)"),
            "retrieved_at": self._stamp(),
            "videos": videos,
            "search_link": ("https://www.tiktok.com/search?q=" if used == "tiktok"
                            else "https://www.instagram.com/explore/search/keyword/?q=") + quote(subject),
            "note": "Public posts found by Google; content is user-generated. The chat plays them with the "
                    "platform's official embed player when the user taps one."
                    + (" No Instagram results, so TikTok was searched instead." if platform == "auto" and used == "tiktok" else ""),
        }

    # -------------------------------------------------------------- transport
    def transport(self, from_location: str, to_location: str, include_taxis: bool = True) -> dict:
        data = self.client._get({"start_addr": from_location, "end_addr": to_location},
                                engine="google_maps_directions")
        options, seen = [], set()
        for d in data.get("directions", []):
            mode = d.get("travel_mode")
            key = (mode, d.get("via"))
            if not mode or key in seen:
                continue
            seen.add(key)
            options.append({
                "mode": mode,
                "via": d.get("via"),
                "duration": d.get("formatted_duration"),
                "duration_minutes": round(d["duration"] / 60) if d.get("duration") else None,
                "typical_range": d.get("typical_duration_range"),
                "distance": d.get("formatted_distance"),
                "distance_km": round(d["distance"] / 1000, 1) if d.get("distance") else None,
                "fare": f"{d['cost']} {d.get('currency') or ''}".strip() if d.get("cost") else None,
            })
        info = data.get("places_info") or []
        start = info[0] if info else {}
        end = info[-1] if len(info) > 1 else {}
        s_lat, s_lng = _gps(start)
        e_lat, e_lng = _gps(end)
        markers = [m for m in (
            {"lat": s_lat, "lng": s_lng, "label": "A", "title": start.get("address") or from_location},
            {"lat": e_lat, "lng": e_lng, "label": "B", "title": end.get("address") or to_location},
        ) if m["lat"] is not None]

        taxis = []
        if include_taxis:
            near = f" near {start.get('address') or from_location}"
            tdata = self.client._get({"type": "search", "q": f"taxi service{near}"}, engine="google_maps")
            for r in (tdata.get("local_results") or [])[:5]:
                lat, lng = _gps(r)
                taxis.append({
                    "name": r.get("title"), "type": r.get("type"), "rating": _rating(r.get("rating")),
                    "reviews": r.get("reviews"), "phone": r.get("phone"),
                    "open_state": r.get("open_state") or r.get("hours"),
                    "website": _https(r.get("website")),
                    "maps_link": _maps_link(r.get("place_id"), r.get("title") or "taxi", lat, lng),
                })
        drive = next((o for o in options if o["mode"] == "Driving"), None)
        return {
            "kind": "transport",
            "from": start.get("address") or from_location, "to": end.get("address") or to_location,
            "source": "Google Maps (live, via SerpApi)", "retrieved_at": self._stamp(),
            "options": options[:6],
            "taxi_ride_estimate": {"duration": drive["duration"], "typical_range": drive["typical_range"],
                                   "distance": drive["distance"]} if drive else None,
            "taxi_companies": taxis,
            "directions_link": "https://www.google.com/maps/dir/?api=1&origin=" + quote(from_location)
                               + "&destination=" + quote(to_location),
            "map": _map(markers, f"{from_location} to {to_location}"),
            "note": "Taxi/cab companies are from Google Maps (rating, phone, open now). Real-time ride-hailing "
                    "availability (Uber, Lyft, Grab, Bolt) isn't available via this API - suggest the user checks "
                    "those apps; the driving time above is a good estimate for any cab ride.",
        }
