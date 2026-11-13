"""
Voice mode for Travel Buddy: browser mic <-> WebSocket <-> Amazon Nova 2.5 Sonic (speech-to-speech).

Nova Sonic listens and speaks. It has two tools:
  travel_buddy(request)      -> forwards the traveller's request to the SAME text TravelAgent the chat uses
                                (name, interests, guardrail, 13 live tools). The full result (cards, maps,
                                charts, reels) is pushed to the browser and rendered in the chat; Nova speaks a
                                short summary.
  ui_action(action, target)  -> drives the page by voice: show/scroll to hotels, places, map, chart, videos;
                                play a video; click a suggestion; scroll; new chat; open status.

Browser protocol (JSON text frames):
  in : {"type": "audio", "data": <base64 16 kHz 16-bit mono PCM>} | {"type": "text", "text": "..."} | {"type": "stop"}
  out: {"type": "ready"} | {"type": "audio", "data": <base64 PCM 16 kHz>} | {"type": "interrupt"}
       {"type": "transcript", "role": "user"|"assistant", "text": "...", "final": bool}
       {"type": "thinking", "request": "..."} | {"type": "chat", "request": "...", "data": {chat response}}
       {"type": "ui", "action": "...", "target": "..."} | {"type": "error", "message": "..."} | {"type": "closed", "reason": "..."}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from strands import tool
from strands.experimental.bidi.agent.agent import BidiAgent
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel
from strands.experimental.bidi.types.events import BidiAudioInputEvent

log = logging.getLogger("travel-voice")

MODEL_ID = "amazon.nova-2-5-sonic"
VOICE = "tiffany"
MAX_CONCURRENT = 6
MAX_SECONDS = 8 * 60
UI_ACTIONS = ("show", "scroll", "play_video", "open_chart", "click_suggestion", "new_chat", "open_status")
_active = 0
_active_lock = asyncio.Lock()

# ---------------------------------------------------------------------------- speech guard
# Nova must speak ONLY the answer. Segments that narrate actions are muted before they reach the
# speaker (Nova sends each segment's text just before its audio); next-step offers are stripped
# from tool results before Nova sees them.
NARRATION = re.compile(
    r"\b(let me|let's (?:see|check|look|find|pull)|i'?ll (?:check|look|find|search|pull|scroll|show|open|play|get|grab|go)|"
    r"i will|i'?m going to|i am going to|going to (?:check|look|search|scroll|show|open|play)|one (?:moment|sec(?:ond)?)|"
    r"give me a (?:sec|second|moment)|hang on|hold on|just a (?:sec|second|moment)|bear with me|"
    r"(?:checking|looking (?:that )?up|searching|pulling (?:that |those |it )?up|fetching|scrolling|opening|loading|playing)\b|"
    r"on it\b|working on (?:it|that))", re.I)
NEXT_STEP = re.compile(
    r"(?:^|(?<=[.!?]))\s*[^.!?]*\b(want me to|shall i|should i|would you like (?:me )?to|do you want (?:me )?to|"
    r"need me to|let me know if|happy to (?:also )?(?:find|check|look|help)|i can also|next,? i can)\b[^.!?]*[.!?]?", re.I)


def is_narration(text: str) -> bool:
    """True for a spoken segment that announces/describes an action instead of giving the answer."""
    t = (text or "").strip()
    return bool(t) and bool(NARRATION.search(t)) and len(t) < 220


def strip_next_actions(text: str) -> str:
    """Remove next-step offers and follow-up questions ("Want me to find hotels?") from a reply."""
    out = NEXT_STEP.sub("", text or "")
    return re.sub(r"\s{2,}", " ", out).strip()


_guard = {"cfg": None}


def check_guardrail(text: str) -> tuple[bool, str]:
    """ApplyGuardrail (INPUT) with the deployed Travel Buddy guardrail. Returns (blocked, message)."""
    import boto3
    from pathlib import Path

    try:
        if _guard["cfg"] is None:
            d = json.loads((Path(__file__).resolve().parents[1] / "config" / "deployment.json").read_text())
            _guard["cfg"] = (d["region"], d["guardrail"]["guardrail_id"], d["guardrail"]["guardrail_version"])
        region, gid, ver = _guard["cfg"]
        r = boto3.client("bedrock-runtime", region_name=region).apply_guardrail(
            guardrailIdentifier=gid, guardrailVersion=ver, source="INPUT", content=[{"text": {"text": text[:2000]}}])
        if r.get("action") == "GUARDRAIL_INTERVENED":
            return True, (r.get("outputs") or [{}])[0].get("text") or "Sorry, I can't help with that."
    except Exception:
        log.warning("guardrail check failed", exc_info=True)
    return False, ""


def voice_prompt(name: str | None, profile: dict) -> str:
    known = {k: v for k, v in (profile or {}).items() if v}
    who = f"The traveller's name is {name}. Use it now and then." if name else (
        "You don't know the traveller's name yet: in your first reply, greet them and ask how you may address them; "
        "when they tell you, pass it on with travel_buddy (e.g. \"My name is Priya\").")
    likes = ("What you know they like: " + "; ".join(f"{k.replace('_', ' ')}: {', '.join(v) if isinstance(v, list) else v}"
                                                     for k, v in known.items()) + ".") if known else ""
    return f"""You are Travel Buddy, a cheerful, friendly tour guide speaking out loud. Motto: "Pack your bags and follow with Abhi".
You're in a voice call; the traveller also sees a chat screen where full results appear as cards, maps, charts and videos.

How to talk:
- Speak like a happy local guide: warm, playful, SHORT. One to three short sentences per turn. No lists, no markdown, no URLs, no long numbers read out digit by digit.
- SPEAK ONLY THE ANSWER (this is a strict rule):
  - Never announce or describe what you are about to do or are doing: no "let me check", "one moment", "I'll look that up", "I'm scrolling", "pulling that up".
  - Call tools silently. After a screen action (ui_action) say nothing at all.
  - Never offer next steps or ask follow-up questions ("want me to…?", "shall I…?", "would you like…?"). The ONLY exception is asking for information you truly need: their name, destination or dates.
- Never read out every result; give the top one or two highlights from the result.

What to do:
- For ANY travel request or personal detail (flights, hotels, cabs, transfers, places, food, shopping, videos, price trends, bookings, their name, interests or budget), call travel_buddy right away and silently, with the request in the traveller's own words plus any context from the conversation (destination, dates). Then speak only the answer from its result.
- When they ask to see, open, play, scroll or go somewhere on the screen ("show me the hotels", "play the first video", "scroll up", "open the chart", "start over"), call ui_action. "Scroll to / go to / take me to / show me the X" means action show with target X (hotels, places, transport, videos, chart, map); plain "scroll up/down" means action scroll. actions: show (target: hotels | places | transport | videos | chart | map | latest), scroll (target: up | down | top | bottom), play_video (target: 1, 2, ...), open_chart, click_suggestion (target: 1, 2, 3), new_chat, open_status.
- Default trip ideas focus on Southeast Asia (ASEAN): Bali, Bangkok, Phuket, Hanoi, Siem Reap and so on.
- SIM, eSIM, roaming or data-plan questions: pass them to travel_buddy and speak ONLY the M1 Singapore plans it returns; never name other telcos or eSIM apps.
- After planning a trip, you may add one short line with the M1 roaming best pick from travel_buddy's result, if it included one.
- Facts, prices and availability ONLY come from travel_buddy's result. If it returns an error, say so kindly and suggest what to try.
- Never repeat card numbers, CVVs or passwords; if someone says one, tell them kindly that payment details are never taken in chat.
{who}
{likes}""".strip()


class VoiceSession:
    def __init__(self, ws: WebSocket, chat_session, client_ip: str, limiter):
        self.ws, self.chat_session, self.ip, self.limiter = ws, chat_session, client_ip, limiter
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        self.closed = False
        self.agent: BidiAgent | None = None
        self.started = time.time()
        self.mute_segment = False        # current spoken segment is narration -> drop its audio
        self.tool_pending = 0            # Travel Buddy lookups in flight -> hold Nova's audio
        self.silent_until_user = False   # after a screen action, stay quiet until the traveller speaks
        self.blocked_turn = False        # the guardrail blocked what the traveller just said -> stay silent

    # ---------------------------------------------------------------- browser I/O
    async def send(self, msg: dict) -> None:
        if self.closed or self.ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            self.closed = True

    async def read_browser(self) -> None:
        try:
            while not self.closed:
                msg = json.loads(await self.ws.receive_text())
                kind = msg.get("type")
                if kind == "audio" and isinstance(msg.get("data"), str) and len(msg["data"]) < 200_000:
                    await self.agent.send(BidiAudioInputEvent(audio=msg["data"], format="pcm", sample_rate=16000, channels=1))
                elif kind == "text" and str(msg.get("text", "")).strip():
                    await self.agent.send(str(msg["text"])[:2000])
                elif kind == "stop":
                    break
                if time.time() - self.started > MAX_SECONDS:
                    await self.send({"type": "closed", "reason": "Voice chats last up to 8 minutes. Tap the mic to start a new one!"})
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.closed = True

    async def forward_model(self) -> None:
        try:
            async for ev in self.agent.receive():
                if self.closed:
                    break
                t = ev.get("type")
                if t == "bidi_audio_stream":
                    if self.mute_segment or self.tool_pending or self.silent_until_user or self.blocked_turn:
                        continue                                    # speech guard: only the answer is spoken
                    await self.send({"type": "audio", "data": ev["audio"]})
                elif t == "bidi_transcript_stream" and ev.get("text"):
                    role = ev.get("role", "assistant")
                    if role == "user":
                        self.silent_until_user = False
                        if ev.get("is_final"):
                            self.blocked_turn = False
                            await self.send({"type": "transcript", "role": "user", "text": ev["text"], "final": True})
                            asyncio.create_task(self.guard_speech(ev["text"]))
                        continue
                    if not ev.get("is_final"):                      # Nova's text for the segment it's about to speak
                        self.mute_segment = is_narration(ev["text"])
                    if self.mute_segment or self.tool_pending or self.silent_until_user or self.blocked_turn:
                        continue
                    await self.send({"type": "transcript", "role": "assistant", "text": ev["text"], "final": bool(ev.get("is_final"))})
                elif t == "bidi_interruption":
                    await self.send({"type": "interrupt"})
                elif t == "bidi_error":
                    log.warning("nova sonic error: %s", str(ev)[:300])
                    await self.send({"type": "error", "message": "Voice hiccup! Please tap the mic and try again."})
                    break
                elif t == "bidi_connection_close":
                    break
        except Exception:
            log.exception("voice stream failed")
            await self.send({"type": "error", "message": "Voice connection dropped. Tap the mic to reconnect."})
        finally:
            self.closed = True

    async def guard_speech(self, text: str) -> None:
        """Run the Bedrock Guardrail on what the traveller said (Nova Sonic itself isn't guardrailed)."""
        blocked, message = await asyncio.to_thread(check_guardrail, text)
        if blocked:
            self.blocked_turn = True                    # silence Nova's reply to this turn
            await self.send({"type": "interrupt"})
            await self.send({"type": "guard", "message": message})

    # ---------------------------------------------------------------- tools for Nova
    def tools(self):
        session, send, limiter, ip, vs = self.chat_session, self.send, self.limiter, self.ip, self

        @tool
        async def travel_buddy(request: str) -> str:
            """
            Ask Travel Buddy (the full trip-planning agent with live flights, hotels, cabs, places, videos, price
            trends and bookings) to handle the traveller's request. Pass the request in the traveller's words plus
            useful context. Results are shown on the traveller's screen; you get a short summary to speak.
            """
            request = (request or "").strip()[:1500]
            if not request:
                return "No request given."
            if vs.blocked_turn:
                return "Blocked by the safety guardrail. Say nothing."
            if (limited := limiter.check(ip)):
                return limited
            await send({"type": "thinking", "request": request})
            vs.tool_pending += 1
            try:
                data = await asyncio.to_thread(session.chat, request)
            except Exception as e:  # noqa: BLE001
                log.exception("travel_buddy failed")
                return f"Travel Buddy hit an error ({type(e).__name__}). Apologise briefly."
            finally:
                vs.tool_pending = max(0, vs.tool_pending - 1)
            await send({"type": "chat", "request": request, "data": data})
            shown = []
            for a in data.get("attachments", []):
                items = a.get("hotels") or a.get("places") or a.get("videos") or a.get("options") or []
                top = ", ".join(str(x.get("name") or x.get("title") or x.get("mode") or "")[:50] for x in items[:3])
                shown.append(f"{a.get('kind')}: {len(items)} on screen" + (f" (top: {top})" if top else ""))
            if data.get("charts"):
                shown.append("a price-trend chart is on screen")
            summary = strip_next_actions(data.get("reply") or "")[:900]
            return json.dumps({"reply_text": summary, "on_screen": shown,
                               "guardrail_blocked": bool(data.get("guardrail_intervened")),
                               "traveller_name": data.get("traveller_name")}, ensure_ascii=False)

        @tool
        async def ui_action(action: str, target: str = "") -> str:
            """
            Control the traveller's chat screen. action: show (target: hotels | places | transport | videos | chart |
            map | latest), scroll (target: up | down | top | bottom), play_video (target: 1, 2, ...), open_chart,
            click_suggestion (target: 1, 2, 3), new_chat, open_status.
            """
            action = (action or "").strip().lower()
            if action not in UI_ACTIONS:
                return f"Unknown action. Use one of: {', '.join(UI_ACTIONS)}."
            await send({"type": "ui", "action": action, "target": str(target or "")[:40]})
            vs.silent_until_user = True                       # speech guard: no "done, scrolling…" narration
            return "Done. Say nothing about it."

        return [travel_buddy, ui_action]

    # ---------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        model = BidiNovaSonicModel(model_id=MODEL_ID, client_config={"region": "us-east-1"},
                                   provider_config={"audio": {"voice": VOICE}})
        agent_state = self.chat_session.agent
        self.agent = BidiAgent(model=model, tools=self.tools(),
                               system_prompt=voice_prompt(agent_state.traveller_name, agent_state.profile))
        await self.agent.start()
        await self.send({"type": "ready"})
        try:
            await asyncio.gather(self.read_browser(), self.forward_model())
        finally:
            self.closed = True
            try:
                await self.agent.stop()
            except Exception:
                pass


async def voice_endpoint(ws: WebSocket, get_session, client_ip_of, limiter) -> None:
    global _active
    await ws.accept()
    ip = client_ip_of(ws)
    if (limited := limiter.check(ip)) or (limited := limiter.check_new_session(ip)):
        await ws.send_text(json.dumps({"type": "error", "message": limited}))     # spam: "You're too fast..."
        await ws.close()
        return
    sid = ws.query_params.get("sid", "")
    if not sid or not sid.replace("-", "").isalnum() or not 8 <= len(sid) <= 64:
        await ws.send_text(json.dumps({"type": "error", "message": "Missing chat session."}))
        await ws.close()
        return
    async with _active_lock:
        if _active >= MAX_CONCURRENT:
            await ws.send_text(json.dumps({"type": "error", "message": "Lots of travellers are talking right now! Please type, or try voice again in a minute."}))
            await ws.close()
            return
        _active += 1
    try:
        chat_session = await asyncio.to_thread(get_session, sid)
        await VoiceSession(ws, chat_session, client_ip_of(ws), limiter).run()
    except Exception:
        log.exception("voice session failed")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": "Voice isn't available right now. Please type instead."}))
        except Exception:
            pass
    finally:
        async with _active_lock:
            _active -= 1
        try:
            await ws.close()
        except Exception:
            pass
