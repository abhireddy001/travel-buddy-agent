"""Destination-planning tools against trimmed real SerpApi responses (no network)."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_core.live_flights import GoogleFlightsLive  # noqa: E402
from travel_core.tools import TravelTools  # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "serpapi_places.json").read_text())


class FakeLive(GoogleFlightsLive):
    def __init__(self):
        super().__init__("test-key", today=date(2026, 11, 13))
        self.calls = []

    def _get(self, params, engine="google_flights"):
        self.calls.append((engine, params))
        return FIX[engine]


@pytest.fixture
def tools():
    return TravelTools(live_client=FakeLive())


def test_hotels_cards_and_map(tools):
    r = json.loads(tools.search_hotels("Paris", "2027-01-21", "2027-01-24", sort_by="lowest_price", min_rating=4))
    assert r["kind"] == "hotels" and r["nights"] == 3 and r["hotels"]
    h = r["hotels"][0]
    assert h["name"] and h["price_per_night"] > 0 and h["image"].startswith("https://") and h["pin"] == 1
    assert r["map"]["markers"][0]["label"] == "1"
    engine, params = tools.live.calls[-1]
    assert engine == "google_hotels" and params["sort_by"] == 3 and params["rating"] == 8


def test_hotels_validate_dates(tools):
    assert "after check-in" in json.loads(tools.search_hotels("Paris", "2027-01-24", "2027-01-21"))["error"]
    assert "past" in json.loads(tools.search_hotels("Paris", "2025-01-01", "2025-01-03"))["error"]


def test_shopping_places(tools):
    r = json.loads(tools.explore_places("Paris", category="shopping"))
    assert r["kind"] == "places" and r["places"][0]["name"]
    assert r["places"][0]["maps_link"].startswith("https://www.google.com/maps/search/")
    assert tools.live.calls[-1] == ("google_maps", {"type": "search", "q": "best shopping malls and markets in Paris"})
    assert "category must be" in json.loads(tools.explore_places("Paris", category="spaceports"))["error"]


def test_transport_options_and_taxis(tools):
    r = json.loads(tools.find_ground_transport("Charles de Gaulle Airport", "Louvre Museum Paris"))
    modes = {o["mode"] for o in r["options"]}
    assert {"Driving", "Transit"} <= modes
    assert r["taxi_ride_estimate"]["duration"] and [m["label"] for m in r["map"]["markers"]] == ["A", "B"]
    assert r["taxi_companies"] and "ride-hailing" in r["note"]
    assert [c[0] for c in tools.live.calls[-2:]] == ["google_maps_directions", "google_maps"]


def test_planning_tools_refuse_without_live_data(monkeypatch, tmp_path):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_SECRET_ID", raising=False)
    t = TravelTools()
    assert "need live data" in json.loads(t.explore_places("Paris"))["error"]


def test_instagram_reels_with_embed_links(tools):
    r = json.loads(tools.find_social_videos("Eiffel Tower", "Paris"))
    assert r["kind"] == "social_videos" and r["platform"] == "instagram" and not r["fallback_used"] and r["videos"]
    v = r["videos"][0]
    assert v["url"].startswith("https://www.instagram.com/") and v["embed_url"].endswith("/embed/")
    assert v["thumbnail"].startswith("https://")
    assert len({x["url"] for x in r["videos"]}) == len(r["videos"])          # de-duplicated
    assert tools.live.calls[-1] == ("google_videos", {"q": "Eiffel Tower Paris site:instagram.com"})


class NoInstagramLive(FakeLive):
    """No Instagram results and no TikTok Discover page -> Google Videos site:tiktok.com."""
    def _get(self, params, engine="google_flights"):
        self.calls.append((engine, params))
        if engine == "tiktok_discover":
            return {"video_results": []}
        if engine == "google_videos":
            return {"video_results": []} if "instagram.com" in params["q"] else FIX["google_videos_tiktok"]
        return FIX[engine]


class DiscoverLive(FakeLive):
    def _get(self, params, engine="google_flights"):
        self.calls.append((engine, params))
        return {"video_results": []} if engine == "google_videos" else FIX[engine]


def test_falls_back_to_tiktok_when_no_instagram():
    t = TravelTools(live_client=NoInstagramLive())
    r = json.loads(t.find_social_videos("Eiffel Tower", "Paris"))
    assert r["platform"] == "tiktok" and r["fallback_used"] and r["platforms_tried"] == ["instagram", "tiktok"]
    v = r["videos"][0]
    assert v["author"].startswith("@") and v["embed_url"].startswith("https://www.tiktok.com/player/v1/")
    engines = [c[0] for c in t.live.calls]
    assert engines == ["google_videos", "tiktok_discover", "tiktok_discover", "google_videos"]
    assert t.live.calls[-1][1]["q"].endswith("site:tiktok.com")


def test_explicit_tiktok_only_searches_tiktok():
    t = TravelTools(live_client=NoInstagramLive())
    r = json.loads(t.find_social_videos("Eiffel Tower", "Paris", platform="tiktok"))
    assert r["platform"] == "tiktok" and [c[0] for c in t.live.calls] == ["tiktok_discover", "tiktok_discover", "google_videos"]
    assert "platform must be" in json.loads(t.find_social_videos("X", platform="snapchat"))["error"]


def test_tiktok_discover_native_engine_ranks_travel_and_drops_hoaxes():
    t = TravelTools(live_client=DiscoverLive())
    r = json.loads(t.find_social_videos("Eiffel Tower", "Paris", platform="tiktok"))
    assert r["source"].startswith("TikTok (live, SerpApi TikTok Discover)")
    assert t.live.calls[0] == ("tiktok_discover", {"q": "eiffel tower", "num": 20})
    caps = [v["caption"].lower() for v in r["videos"]]
    assert caps and not any("burned down" in c or "fact check" in c for c in caps)
    v = r["videos"][0]
    assert v["embed_url"].startswith("https://www.tiktok.com/player/v1/") and v["author"].startswith("@")
    assert isinstance(v["plays"], int) and v["published"]


def test_auto_prefers_instagram_then_tiktok_discover():
    t = TravelTools(live_client=DiscoverLive())     # google_videos (Instagram) returns nothing here
    r = json.loads(t.find_social_videos("Eiffel Tower", "Paris"))
    assert r["platform"] == "tiktok" and r["fallback_used"]
    assert [c[0] for c in t.live.calls] == ["google_videos", "tiktok_discover"]
