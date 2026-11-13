"""Tests for gateway-mode pieces that don't need AWS: schema derivation, chart hook parsing, store contract."""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "deploy"), str(ROOT / "agents")]

import deploy  # noqa: E402
from travel_core.booking_store import BookingStoreError, JsonBookingStore  # noqa: E402
from travel_core.tools import TOOL_NAMES  # noqa: E402


def test_gateway_schemas_match_mcp_tools_and_are_clean():
    schemas = deploy.tool_schemas()
    assert sorted(s["name"] for s in schemas) == sorted(TOOL_NAMES)
    allowed = {"type", "description", "properties", "items", "required"}

    def walk(node):
        assert set(node) <= allowed, node
        for child in node.get("properties", {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    for s in schemas:
        walk(s["inputSchema"])
    flights = next(s for s in schemas if s["name"] == "search_flights")["inputSchema"]
    assert flights["properties"]["max_stops"]["type"] == "integer"     # Optional[int] flattened
    assert flights["required"] == ["origin", "destination", "departure_date"]


def test_standard_guardrails():
    cfg = deploy.GUARDRAIL_CONFIG
    pii = {e["type"]: e["action"] for e in cfg["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]}
    for t in ("CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV", "US_PASSPORT_NUMBER", "PASSWORD", "AWS_SECRET_KEY"):
        assert pii[t] == "BLOCK"
    assert not {"NAME", "EMAIL", "PHONE", "ADDRESS"} & set(pii)          # needed for bookings and places
    nric = cfg["sensitiveInformationPolicyConfig"]["regexesConfig"][0]
    import re
    assert re.search(nric["pattern"], "my NRIC is S1234567D") and not re.search(nric["pattern"], "flight SB1234 at gate D12")
    filters = {f["type"]: f for f in cfg["contentPolicyConfig"]["filtersConfig"]}
    assert filters["PROMPT_ATTACK"]["inputStrength"] == "HIGH"
    assert {"SEXUAL", "HATE", "VIOLENCE", "INSULTS", "MISCONDUCT"} <= set(filters)
    assert {t["name"] for t in cfg["topicPolicyConfig"]["topicsConfig"]} == {"TravelDocumentFraud", "EvadingAviationSecurity"}

def test_parse_json_text_handles_double_encoding():
    from travel_agent import _parse_json_text

    obj = {"chart_file": "s3://b/charts/x.html"}
    assert _parse_json_text(json.dumps(obj)) == obj
    assert _parse_json_text(json.dumps(json.dumps(obj))) == obj
    assert _parse_json_text("not json") is None


def test_json_store_enforces_capacity(tmp_path):
    store = JsonBookingStore(tmp_path / "b.json")
    base = {"flight_id": "F", "cabin": "economy", "status": "CONFIRMED"}
    store.create({**base, "booking_ref": "A", "passengers": ["x", "y"]}, seats_available_before_bookings=3)
    with pytest.raises(BookingStoreError):
        store.create({**base, "booking_ref": "B", "passengers": ["x", "y"]}, seats_available_before_bookings=3)
    assert store.seats_booked("F", "economy") == 2
    store.cancel("A")
    assert store.seats_booked("F", "economy") == 0
