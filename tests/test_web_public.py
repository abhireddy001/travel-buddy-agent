"""Public web app: no key needed, real client IP, rate limits (no AWS)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app_mod():
    sys.path[:0] = [str(ROOT / "web"), str(ROOT / "agents")]
    import app  # noqa: PLC0415
    return app


def req(xff="", host="10.0.0.1"):
    return SimpleNamespace(headers={"x-forwarded-for": xff} if xff else {}, client=SimpleNamespace(host=host), cookies={})


def test_public_by_default(app_mod):
    assert app_mod.REQUIRE_KEY is False and app_mod.authorized(req()) is True


def test_client_ip_ignores_spoofed_entries(app_mod):
    # client-supplied "1.2.3.4", CloudFront appends the real viewer, nginx appends the edge
    assert app_mod.client_ip(req("1.2.3.4, 203.0.113.9, 130.176.1.1")) == "203.0.113.9"
    assert app_mod.client_ip(req("203.0.113.9, 130.176.1.1")) == "203.0.113.9"
    assert app_mod.client_ip(req()) == "10.0.0.1"


def test_rate_limits(app_mod, monkeypatch):
    lim = app_mod.RateLimiter()
    monkeypatch.setattr(app_mod, "BURST_LIMIT", 10**6)                   # isolate the 10-minute limit
    for _ in range(app_mod.PER_IP_LIMIT):
        assert lim.check("198.51.100.7") is None
    assert "breather" in lim.check("198.51.100.7")
    assert lim.check("198.51.100.8") is None              # other visitors unaffected
    monkeypatch.setattr(app_mod, "DAILY_LIMIT", 3)
    lim2 = app_mod.RateLimiter()
    assert all(lim2.check(f"192.0.2.{i}") is None for i in range(3))
    assert "come back tomorrow" in lim2.check("192.0.2.99")



def test_spam_more_than_5_per_minute_is_too_fast(app_mod):
    lim = app_mod.RateLimiter()
    for _ in range(5):
        assert lim.check("203.0.113.50") is None
    assert lim.check("203.0.113.50") == "You're too fast, please try again."
    assert lim.check("203.0.113.51") is None                               # other visitors unaffected


def test_spam_more_than_5_new_chats_per_minute(app_mod):
    lim = app_mod.RateLimiter()
    for _ in range(5):
        assert lim.check_new_session("198.51.100.20") is None
    assert lim.check_new_session("198.51.100.20") == "You're too fast, please try again."
