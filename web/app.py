"""
Browser chat UI for the Travel Agent (Starlette + uvicorn, no extra dependencies).

    python travel-agent/web/app.py --port 8501

Each browser session gets its own TravelAgent (Gateway + Guardrail, same as the CLI).
Access requires a key: open  <base>/?key=<KEY>  once and a cookie keeps you signed in.
The key comes from $TRAVEL_WEB_KEY or is generated into travel-agent/data/web-access-key.txt.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from strands.hooks import AfterToolCallEvent  # noqa: E402

from travel_agent import TravelAgent, _parse_json_text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status as status_mod  # noqa: E402
import voice as voice_mod  # noqa: E402

log = logging.getLogger("travel-web")
OUTPUT_DIR = ROOT / "output"
KEY_FILE = ROOT / "data" / "web-access-key.txt"
COOKIE = "travel_agent_key"
MAX_SESSIONS = 60
SESSION_IDLE_SECONDS = 1200
# Public by default. Set TRAVEL_WEB_REQUIRE_KEY=1 to bring back the access-key gate.
REQUIRE_KEY = os.environ.get("TRAVEL_WEB_REQUIRE_KEY", "0") == "1"
PER_IP_LIMIT, PER_IP_WINDOW = 30, 600          # chat messages per visitor per 10 minutes
BURST_LIMIT, BURST_WINDOW = 5, 60              # spam: at most 5 requests / 5 new chats per IP per minute
TOO_FAST = "You're too fast, please try again."
DAILY_LIMIT = int(os.environ.get("TRAVEL_WEB_DAILY_LIMIT", "400"))   # all visitors, rolling 24 h
CHART_RE = re.compile(r"fare-trends-[A-Za-z0-9_.-]+\.html")


def access_key() -> str:
    if os.environ.get("TRAVEL_WEB_KEY"):
        return os.environ["TRAVEL_WEB_KEY"]
    if KEY_FILE.is_file():
        return KEY_FILE.read_text().strip()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(18)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")
    return key


KEY = access_key()


# =============================================================================
# Sessions
# =============================================================================

class Session:
    def __init__(self):
        self.agent = TravelAgent(interface="web")
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.tools_this_turn: list[str] = []
        self.attachments: list[dict] = []
        self.charts_this_turn: set[str] = set()
        self.agent.agent.hooks.add_callback(AfterToolCallEvent, self._on_tool)

    def _on_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"].split("___")[-1]
        self.tools_this_turn.append(name)
        if event.result.get("status") != "success":
            return
        for block in event.result.get("content", []):
            payload = _parse_json_text(block.get("text"))
            if not payload or "error" in payload:
                continue
            if name == "get_price_trends" and payload.get("chart_file"):
                # Runs before ChartDownloadHook rewrites s3:// -> local; the local file uses the same name.
                chart = Path(str(payload["chart_file"])).name
                if CHART_RE.fullmatch(chart):
                    self.charts_this_turn.add(chart)
            elif name in ATTACHMENT_TOOLS and payload.get("kind") in ATTACHMENT_TOOLS.values():
                self.attachments.append(_attachment(payload))

    def chat(self, message: str) -> dict:
        with self.lock:
            self.last_used = time.time()
            self.tools_this_turn, self.attachments, self.charts_this_turn = [], [], set()
            reply = self.agent(message)
            mentioned = set(CHART_RE.findall(reply))
            charts = sorted(c for c in (mentioned | self.charts_this_turn) if (OUTPUT_DIR / c).is_file())
            return {
                "reply": reply,
                "guardrail_intervened": self.agent.guardrail_intervened,
                "tools": self.tools_this_turn,
                "charts": charts,
                "attachments": self.attachments,
                "traveller_name": self.agent.traveller_name,
                "profile": self.agent.profile,
            }

    def close(self):
        self.agent.cleanup()


ATTACHMENT_TOOLS = {"search_hotels": "hotels", "explore_places": "places", "find_ground_transport": "transport",
                    "find_social_videos": "social_videos", "get_m1_travel_plans": "m1_plans"}
_KEEP = {
    "hotels": ("kind", "location", "check_in_date", "check_out_date", "nights", "adults", "source", "retrieved_at",
               "hotels", "map", "google_hotels_url"),
    "places": ("kind", "location", "category", "source", "retrieved_at", "places", "map"),
    "social_videos": ("kind", "place", "location", "platform", "fallback_used", "source", "retrieved_at", "videos",
                      "search_link"),
    "m1_plans": ("kind", "provider", "destination", "country", "customer_type", "trip_days", "plans", "as_of", "links"),
    "transport": ("kind", "from", "to", "source", "retrieved_at", "options", "taxi_ride_estimate", "taxi_companies",
                  "directions_link", "map", "note"),
}


def _attachment(payload: dict) -> dict:
    """Only the fields the UI renders (data is rendered with textContent / https-only URLs client-side)."""
    return {k: payload.get(k) for k in _KEEP[payload["kind"]] if k in payload}


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(sid: str) -> Session:
    with SESSIONS_LOCK:
        now = time.time()
        for k in [k for k, s in SESSIONS.items() if now - s.last_used > SESSION_IDLE_SECONDS]:
            SESSIONS.pop(k).close()
        if sid not in SESSIONS:
            if len(SESSIONS) >= MAX_SESSIONS:
                oldest = min(SESSIONS, key=lambda k: SESSIONS[k].last_used)
                SESSIONS.pop(oldest).close()
            SESSIONS[sid] = Session()
        return SESSIONS[sid]


# =============================================================================
# Routes
# =============================================================================

def authorized(request: Request) -> bool:
    if not REQUIRE_KEY:
        return True
    supplied = request.cookies.get(COOKIE) or request.headers.get("x-access-key") or ""
    return hmac.compare_digest(supplied, KEY)


def client_ip(request: Request) -> str:
    """
    Real visitor IP. CloudFront appends the viewer's IP to X-Forwarded-For and nginx then appends the
    CloudFront edge IP, so the viewer is the second entry from the right (entries further left are
    client-supplied and can be spoofed).
    """
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else (request.client.host if request.client else "unknown")


class RateLimiter:
    """Sliding-window limits: per visitor IP, plus one global daily cap protecting the API quota."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ip: dict[str, list[float]] = {}
        self.burst: dict[str, list[float]] = {}
        self.new_sessions: dict[str, list[float]] = {}
        self.all: list[float] = []

    @staticmethod
    def _window(store: dict, ip: str, now: float, window: float) -> list[float]:
        hits = [t for t in store.get(ip, []) if now - t < window]
        store[ip] = hits
        return hits

    def check_new_session(self, ip: str) -> str | None:
        """Spam: more than BURST_LIMIT new chat sessions from one IP within a minute."""
        now = time.time()
        with self.lock:
            hits = self._window(self.new_sessions, ip, now, BURST_WINDOW)
            if len(hits) >= BURST_LIMIT:
                return TOO_FAST
            hits.append(now)
            return None

    def check(self, ip: str) -> str | None:
        now = time.time()
        with self.lock:
            self.all = [t for t in self.all if now - t < 86400]
            if len(self.all) >= DAILY_LIMIT:
                return ("Travel Buddy has had a very busy day and is resting its wings 🛫 "
                        "Please come back tomorrow!")
            burst = self._window(self.burst, ip, now, BURST_WINDOW)
            if len(burst) >= BURST_LIMIT:                    # spam: more than 5 requests in a minute
                return TOO_FAST
            hits = [t for t in self.by_ip.get(ip, []) if now - t < PER_IP_WINDOW]
            if len(hits) >= PER_IP_LIMIT:
                wait = int(PER_IP_WINDOW - (now - hits[0])) // 60 + 1
                return f"Whoa, you're quick! 🏃 Take a breather and try again in about {wait} min."
            hits.append(now)
            burst.append(now)
            self.by_ip[ip] = hits
            self.all.append(now)
            if len(self.by_ip) > 5000:      # keep memory bounded
                self.by_ip = {k: v for k, v in self.by_ip.items() if v and now - v[-1] < PER_IP_WINDOW}
                self.burst = {k: v for k, v in self.burst.items() if v and now - v[-1] < BURST_WINDOW}
                self.new_sessions = {k: v for k, v in self.new_sessions.items() if v and now - v[-1] < BURST_WINDOW}
            return None


LIMITER = RateLimiter()


async def index(request: Request):
    if (k := request.query_params.get("key")) is not None and not REQUIRE_KEY:
        return RedirectResponse("./", status_code=303)        # old shared links keep working
    if (k := request.query_params.get("key")) is not None:
        if not hmac.compare_digest(k, KEY):
            return PlainTextResponse("Invalid access key.", status_code=403)
        # Strip the key from the URL; relative redirect keeps the /ports/8501/ prefix.
        resp = RedirectResponse("./", status_code=303)
        resp.set_cookie(COOKIE, KEY, httponly=True, secure=True, samesite="strict", max_age=7 * 86400)
        return resp
    if not authorized(request):
        return PlainTextResponse("Access key required: open this page with ?key=<access key>.", status_code=401)
    return HTMLResponse((Path(__file__).parent / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


async def chat(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    message = str(body.get("message", "")).strip()
    sid = str(body.get("session_id", ""))
    if not message or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid):
        return JSONResponse({"error": "message and a valid session_id are required"}, status_code=400)
    if len(message) > 4000:
        return JSONResponse({"error": "Message too long (max 4000 characters)."}, status_code=400)
    ip = client_ip(request)
    if (limited := LIMITER.check(ip)):
        return JSONResponse({"error": limited, "rate_limited": True}, status_code=429)
    if sid not in SESSIONS and (limited := LIMITER.check_new_session(ip)):
        return JSONResponse({"error": limited, "rate_limited": True}, status_code=429)
    try:
        session = await run_in_threadpool(get_session, sid)
        return JSONResponse(await run_in_threadpool(session.chat, message))
    except Exception as e:
        log.exception("chat failed")
        return JSONResponse({"error": f"The agent hit an error ({type(e).__name__}). Please try again."},
                            status_code=500)


async def reset(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sid = str((await request.json()).get("session_id", ""))
    with SESSIONS_LOCK:
        s = SESSIONS.pop(sid, None)
    if s:
        await run_in_threadpool(s.close)
    return JSONResponse({"ok": True})


async def chart(request: Request):
    if not authorized(request):
        return PlainTextResponse("unauthorized", status_code=401)
    name = request.path_params["name"]
    if not CHART_RE.fullmatch(name) or not (OUTPUT_DIR / name).is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(OUTPUT_DIR / name, media_type="text/html")


async def status_page(request: Request):
    if not authorized(request):
        return PlainTextResponse("Access key required: open the chat with ?key=<access key> first.", status_code=401)
    return HTMLResponse((Path(__file__).parent / "status.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


async def status_api(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(await run_in_threadpool(status_mod.collect), headers={"Cache-Control": "no-store"})
    except Exception as e:
        log.exception("status failed")
        return JSONResponse({"error": f"status unavailable ({type(e).__name__})"}, status_code=500)


async def voice_ws(ws: WebSocket):
    """Voice mode: browser mic <-> Amazon Nova 2.5 Sonic, sharing the chat session's Travel Buddy agent."""
    await voice_mod.voice_endpoint(ws, get_session, client_ip, LIMITER)


async def health(_request: Request):
    return JSONResponse({"status": "ok", "sessions": len(SESSIONS)})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/chat", chat, methods=["POST"]),
    Route("/api/reset", reset, methods=["POST"]),
    Route("/charts/{name}", chart),
    Route("/status", status_page),
    Route("/api/status", status_api),
    Route("/health", health),
    WebSocketRoute("/ws/voice", voice_ws),
])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
