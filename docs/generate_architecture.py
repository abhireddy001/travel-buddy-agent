#!/usr/bin/env python3
"""
Generate docs/architecture.svg (editable vector). Render a PNG with any browser, e.g.:
  chrome-headless-shell --screenshot=docs/architecture.png --window-size=1700,1300 \
      --force-device-scale-factor=1.5 file://$PWD/docs/architecture.svg
"""

import html
from pathlib import Path

W, H = 1700, 1300
out: list[str] = []
E = html.escape


def a(s: str) -> None:
    out.append(s)


STY = {  # fill, stroke, title colour
    "gray": ("#F2F3F3", "#879196", "#232F3E"),
    "net": ("#FCEFF7", "#C7317D", "#232F3E"),
    "app": ("#FFFFFF", "#7B3FE4", "#232F3E"),
    "voice": ("#EAF8EF", "#2E9E5B", "#1E6E3F"),
    "guard": ("#FDF0F0", "#D13212", "#D13212"),
    "model": ("#E9F7F3", "#01A88D", "#232F3E"),
    "sec": ("#FDF0F0", "#DD344C", "#232F3E"),
    "agc": ("#F7F3FE", "#7B3FE4", "#232F3E"),
    "comp": ("#FFF4E5", "#ED7100", "#232F3E"),
    "data": ("#E9F7F3", "#3F8624", "#232F3E"),
    "ext": ("#FFFFFF", "#1F6FB2", "#1F6FB2"),
    "store": ("#FFFBF2", "#ED7100", "#232F3E"),
}


def box(x, y, w, h, style, title, lines=(), small=()):
    f, s, tc = STY[style]
    a(f'<rect class="box" x="{x}" y="{y}" width="{w}" height="{h}" fill="{f}" stroke="{s}"/>')
    a(f'<text class="t" x="{x + 14}" y="{y + 24}" style="fill:{tc}">{E(title)}</text>')
    yy = y + 44
    for line in lines:
        a(f'<text class="s" x="{x + 14}" y="{yy}">{E(line)}</text>')
        yy += 17
    for line in small:
        a(f'<text class="xs" x="{x + 14}" y="{yy}">{E(line)}</text>')
        yy += 15


def badge(x, y, n, color="#232F3E"):
    a(f'<g class="badge"><circle cx="{x}" cy="{y}" r="10" style="fill:{color}"/><text x="{x}" y="{y + 4}">{n}</text></g>')


def path(d, cls="ln", marker="arr", dash=False):
    a(f'<path class="{cls}{" dash" if dash else ""}" d="{d}" marker-end="url(#{marker})"/>')


def label(x, y, t, anchor="start"):
    a(f'<text class="lbl" x="{x}" y="{y}" text-anchor="{anchor}">{E(t)}</text>')


a(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Amazon Ember, Helvetica, Arial, sans-serif">
<title>Travel Buddy architecture: public chat and voice (Amazon Nova 2.5 Sonic), Strands agent on Bedrock with Guardrails, AgentCore Gateway, Lambda tools, live Google data via SerpApi and M1 plans</title>
<defs>
  <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#232F3E"/></marker>
  <marker id="arrP" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#7B3FE4"/></marker>
  <marker id="arrR" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#D13212"/></marker>
  <marker id="arrB" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#1F6FB2"/></marker>
  <marker id="arrG" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#2E9E5B"/></marker>
  <style>
    .t  {{ font-size: 14px; font-weight: 700; fill: #232F3E; }}
    .s  {{ font-size: 11.5px; fill: #3B4552; }}
    .xs {{ font-size: 10.5px; fill: #545B64; }}
    .h  {{ font-size: 15px; font-weight: 700; }}
    .box {{ rx: 8; ry: 8; stroke-width: 1.6; }}
    .ln  {{ fill: none; stroke: #232F3E; stroke-width: 1.6; }}
    .lnP {{ fill: none; stroke: #7B3FE4; stroke-width: 1.8; }}
    .lnR {{ fill: none; stroke: #D13212; stroke-width: 1.8; }}
    .lnB {{ fill: none; stroke: #1F6FB2; stroke-width: 1.8; }}
    .lnG {{ fill: none; stroke: #2E9E5B; stroke-width: 2.2; }}
    .dash {{ stroke-dasharray: 6 4; }}
    .badge text {{ font-size: 11px; font-weight: 700; fill: #fff; text-anchor: middle; }}
    .lbl {{ font-size: 10.5px; fill: #232F3E; font-style: italic; }}
  </style>
</defs>
<rect width="{W}" height="{H}" fill="#FFFFFF"/>
<text x="20" y="38" style="font-size:24px;font-weight:700;fill:#232F3E">Travel Buddy: chat + voice trip planner on Amazon Bedrock (Nova 2.5 Sonic, Claude, Guardrails, AgentCore Gateway)</text>
<text x="20" y="62" class="s" style="font-size:13px">Public on every device · live flights, hotels, cabs, places, reels and price trends via SerpApi · M1 Singapore travel plans · "Pack your bags and follow with Abhi"</text>''')

# ---------------- boundaries
a('<rect x="250" y="90" width="1085" height="800" rx="10" fill="none" stroke="#FF9900" stroke-width="2" stroke-dasharray="8 5"/>')
a('<rect x="250" y="90" width="250" height="26" fill="#FF9900"/><text x="262" y="108" style="font-size:13px;font-weight:700;fill:#fff">AWS Cloud · us-east-1</text>')
a('<rect x="265" y="222" width="485" height="478" rx="10" fill="#FAFAFA" stroke="#879196" stroke-width="1.6" stroke-dasharray="6 4"/>')
a('<text x="280" y="244" class="h" style="fill:#545B64">Code Editor instance (Amazon EC2)</text>')
a('<rect x="1355" y="90" width="330" height="800" rx="10" fill="#F4F8FC" stroke="#1F6FB2" stroke-width="2" stroke-dasharray="8 5"/>')
a('<text x="1372" y="116" class="h" style="fill:#1F6FB2">External services (internet)</text>')
a('<text x="20" y="114" class="h" style="fill:#545B64">Users</text>')

# ---------------- users
box(20, 130, 210, 112, "gray", "Traveller", ["Phone · tablet · laptop", "Type, or talk via the mic"],
    ["Public link, no login:", "…/ports/8502/", "Status page: …/status"])
box(20, 262, 210, 70, "gray", "Terminal CLI", ["agents/travel_agent.py"], ["-q one-shot · --local · --no-guardrail"])

# ---------------- edge
box(270, 130, 225, 72, "net", "Amazon CloudFront", ["All edge locations · no geo-block"], ["HTTPS + WSS · HTTP/2, HTTP/3, IPv6"])
box(515, 130, 225, 72, "net", "nginx", ["/ports/8502/ to localhost:8502"], ["HTTP + WebSocket upgrade"])

# ---------------- instance
box(285, 262, 445, 108, "app", "Travel Buddy web app (Starlette · systemd)",
    ["Spam guard: >5 requests or new chats/min/IP = too fast",
     "Chat UI: cards, OSM maps, charts, reels, M1 plans block"],
    ["Mia the beach companion · For-you chips from the interest profile",
     "Public read-only /status page · restarts on crash or boot"])
box(285, 392, 210, 150, "voice", "Voice bridge /ws/voice",
    ["One Nova Sonic session", "per call (max 6, 8 min)"],
    ["tools for Nova:", "  travel_buddy(request)", "  ui_action(show, scroll, play…)", "Speech guard: speaks only", "the answer (no narration)"])
box(510, 392, 220, 168, "app", "Travel Buddy agent (Strands)",
    ["One per chat (shared by voice)", "System prompt: tone, ASEAN,", "M1-only plans, booking rules"],
    ["remember_traveller_name", "remember_traveller_preferences", "MCPClient to Gateway (JWT)", "ChartDownloadHook: s3:// to file"])
box(285, 562, 210, 120, "gray", "Deploy tooling", ["deploy.py · cleanup.py", "set_serpapi_key.py"],
    ["generate_architecture.py", "config/deployment.json", "(identifiers only, no secrets)"])
box(510, 580, 220, 102, "store", "output/ trend charts", ["Interactive HTML reports", "Shown inline in the chat"], ["served at /charts/*"])

# ---------------- bedrock column
box(770, 130, 265, 112, "voice", "Amazon Nova 2.5 Sonic",
    ["amazon.nova-2-5-sonic", "Speech to speech, bidirectional", "stream · voice tiffany · barge-in"],
    ["Hears, speaks, calls tools mid-call"])
box(770, 262, 265, 110, "guard", "Amazon Bedrock Guardrails",
    ["PII: cards, bank, passport, SSN,", "NRIC/FIN, PINs, passwords, keys", "Harmful content · prompt attacks"],
    ["Travel-fraud and security topics · also checks voice"])
box(770, 392, 265, 72, "model", "Amazon Bedrock · Claude Sonnet", ["global.anthropic.claude-sonnet-5"], ["ConverseStream · tool use"])
box(770, 484, 265, 98, "sec", "Amazon Cognito",
    ["travel-agent-pool · M2M client", "client_credentials · scope invoke"],
    ["secret read at runtime, never on disk", "60-min access token, auto-refresh"])

# ---------------- agentcore + lambda column
box(1060, 130, 255, 116, "agc", "AgentCore Gateway",
    ["MCP over HTTPS", "CUSTOM_JWT authorizer (Cognito)", "Target FlightTools · 14 tools"], ["401 without a valid token"])
box(1060, 276, 255, 306, "comp", "AWS Lambda",
    ["travel-agent-flight-tools", "Python 3.12 · arm64 · travel_core"],
    ["Flights: search_flights · get_booking_options",
     "  check_availability · find_cheapest_dates",
     "  get_price_trends · search_airports",
     "  book_flight · get_booking · cancel_booking",
     "Trip: search_hotels · explore_places",
     "  find_ground_transport (routes + cabs)",
     "  find_social_videos (Instagram, TikTok)",
     "Mobile: get_m1_travel_plans (M1 only)",
     "",
     "LIVE via SerpApi · no key: simulated (labelled)",
     "Gateway invokes via IAM role (this fn only)",
     "Metrics as CloudWatch EMF log lines"])

# ---------------- data row
box(270, 740, 245, 112, "data", "Amazon DynamoDB", ["travel-agent-bookings"],
    ["bookings + atomic seat counters", "live offer cache (GF- id to token)", "transaction with capacity check"])
box(535, 740, 245, 112, "data", "Amazon S3", ["Trend-chart bucket"],
    ["HTML reports under charts/", "private · TLS-only · 7-day expiry", "Lambda: PutObject only"])
box(800, 740, 245, 112, "sec", "AWS Secrets Manager", ["travel-agent/serpapi-api-key"],
    ["set via set_serpapi_key.py", "Lambda: GetSecretValue (this", "secret only) · cached 5 min"])
box(1065, 740, 250, 130, "net", "Amazon CloudWatch", ["Dashboard TravelAgent · 7 alarms"],
    ["TravelAgent metrics: agent turns, tokens,", "guardrail, tools, SerpApi quota, bookings", "+ native Bedrock, Guardrail, Gateway,", "Lambda, DynamoDB · logs 30 days"])

# ---------------- external
box(1375, 130, 290, 112, "ext", "SerpApi", ["Live Google data (JSON)"],
    ["google_flights · google_hotels", "google_maps · google_maps_directions", "google_videos · tiktok_discover", "Free plan: 250 searches / month"])
box(1375, 262, 290, 82, "ext", "Google Flights · Hotels · Maps", ["Fares, sellers, price history,", "hotels, places, routes, cab firms"])
box(1375, 364, 290, 82, "ext", "Instagram · TikTok", ["Reels and videos about places"], ["Official embed players, loaded on tap"])
box(1375, 466, 290, 66, "ext", "OpenStreetMap", ["Map tiles drawn in the browser"])
box(1375, 552, 290, 82, "ext", "M1 Singapore (m1.com.sg)", ["Roaming, prepaid, tourist SIM plans"], ["Dated snapshot + links to buy"])
box(1375, 654, 290, 90, "ext", "Airlines & travel agencies", ["Aer Lingus · JetBlue · Expedia · …"], ["Booking and payment happen on", "the seller's own website"])

# ---------------- arrows: request path
path("M230,175 H270"); badge(250, 175, 1)
path("M495,166 H515")
path("M627,202 V262"); label(634, 238, "proxy :8502 (HTTP + WS)")
path("M620,370 V392"); badge(602, 377, 2)                                   # web app -> agent (typed chat)
path("M230,297 H252 V552 H510")                                             # CLI -> agent (below the voice bridge)

# voice path (green): web app -> voice bridge -> Nova Sonic, and bridge -> agent (travel_buddy)
path("M390,370 V392", "lnG", "arrG"); badge(372, 381, "V", "#2E9E5B")
path("M495,402 V388 H762 V186 H770", "lnG", "arrG"); label(775, 255, "audio stream, 16 kHz")
path("M495,470 H510", "lnG", "arrG")

# voice bridge -> guardrail: every spoken request is checked with ApplyGuardrail (red dashed)
a('<path class="lnR dash" d="M495,530 H502 V570 H742 V355 H770" marker-end="url(#arrR)"/>')
label(620, 567, "speech check (ApplyGuardrail)", "middle")
badge(742, 452, "S", "#D13212")

# agent -> guardrail -> claude (red)
path("M730,430 H750 V317 H770", "lnR", "arrR"); badge(750, 372, 3, "#D13212")
path("M902,372 V392", "lnR", "arrR")

# agent -> cognito, agent -> gateway (purple)
path("M730,540 H770", "lnP", "arrP"); badge(750, 540, 4, "#7B3FE4"); label(735, 556, "token")
path("M730,505 H754 V474 H1046 V188 H1060", "lnP", "arrP"); badge(900, 474, 5, "#7B3FE4")
a('<path class="lnP dash" d="M1060,232 H1053 V535 H1035" marker-end="url(#arrP)"/>'); label(1045, 600, "JWT check", "end")
path("M1187,246 V276", "lnP", "arrP"); badge(1206, 261, 6, "#7B3FE4")

# lambda -> serpapi -> google / tiktok (blue)
path("M1315,300 H1345 V176 H1375", "lnB", "arrB"); badge(1345, 238, 7, "#1F6FB2")
path("M1520,242 V262", "lnB", "arrB")
a('<path class="lnB dash" d="M1395,344 V364"/>')
a('<path class="ln dash" d="M1375,593 H1340 V560 H1315" marker-end="url(#arr)"/>'); label(1338, 616, "plan snapshot", "end")

# lambda -> data stores
a('<path class="ln" d="M1187,582 V722 H392"/>')
for xx in (392, 657, 922, 1190):
    path(f"M{xx},722 V740")
badge(1187, 650, 8); label(1180, 714, "bookings · offers · charts · key · metrics & logs", "end")
a('<path class="ln dash" d="M600,740 V682" marker-end="url(#arr)"/>'); badge(582, 711, 9); label(608, 711, "download")

# browser -> external (embeds, map tiles, booking links)
path("M20,205 H10 V912 H1600 V744", "lnB", "arrB"); badge(10, 800, 10, "#1F6FB2")
label(24, 905, "in the browser: booking links open the seller's site · reels play via official embeds · OSM map tiles")

# ---------------- request flow
a('<rect x="20" y="935" width="1665" height="345" rx="10" fill="#FBFBFB" stroke="#D5DBDB"/>')
a('<text x="40" y="962" class="h" style="fill:#232F3E">Request flow</text>')
steps = [
    "1  The traveller opens the public link on any device (no login). CloudFront and nginx reach the web app (rate-limited).",
    "2  Typed chat: the web app runs the chat's Travel Buddy agent, which keeps the traveller's name and interest profile.",
    "3  Every Claude call goes through the Bedrock Guardrail (PII incl. NRIC, harmful content, prompt attacks, travel-safety topics).",
    "4  The agent gets a Cognito client-credentials token (secret read at runtime, auto-refreshed).",
    "5  Claude picks tools; the agent calls them over MCP on the AgentCore Gateway with the JWT.",
    "6  The Gateway validates the token and invokes the Lambda target with its own IAM role.",
    "7  Lambda gets live Google data via SerpApi (flights, hotels, places, routes, Instagram/TikTok videos); M1 plans: dated catalogue.",
    "8  Bookings and offer tokens go to DynamoDB, charts to S3; the key is in Secrets Manager; metrics, logs and alarms are in CloudWatch.",
    "9  The agent hook downloads charts from S3; the UI shows cards, OSM maps, charts, playable reels and the M1 block, and Mia reacts.",
    "V  Voice: mic audio streams over WebSocket to Nova 2.5 Sonic. It calls travel_buddy (the same agent, steps 3 to 9) and ui_action to drive",
    "    the screen (show, scroll, play, click). The speech guard mutes narration, so only the answer is spoken.",
    "10 The traveller follows the seller's booking link to book and pay on the airline's or agency's own website.",
    "S  Safety: more than 5 requests or new chats per minute from one IP get \"You're too fast, please try again.\"",
    "    Spoken requests are checked with ApplyGuardrail (S); if blocked, the Nova reply is silenced.",
]
for i, line in enumerate(steps):
    col, rowi = divmod(i, 7)
    a(f'<text class="s" style="font-size:12.5px" x="{40 + col * 830}" y="{990 + rowi * 26}">{E(line)}</text>')
ly = 1196
items = [("ln", "", "Request / data"), ("lnR", "", "Guardrail check (model call / speech)"), ("lnP", "", "AgentCore / identity (MCP, JWT)"),
         ("lnG", "", "Voice (Nova Sonic, WebSocket)"), ("lnB", "", "Internet: live data, embeds, booking hand-off"), ("ln", "dash", "Async / return path")]
x = 40
for cls, dash, t in items:
    a(f'<line x1="{x}" y1="{ly}" x2="{x + 42}" y2="{ly}" class="{cls} {dash}"/>')
    a(f'<text class="xs" x="{x + 50}" y="{ly + 4}">{E(t)}</text>')
    x += 70 + len(t) * 6.2
a(f'<text class="xs" x="40" y="{ly + 40}">Without a SerpApi key the Lambda serves a deterministic simulated flight market (fictional carriers, labelled SIMULATED). Spoken requests are checked with ApplyGuardrail;</text>')
a(f'<text class="xs" x="40" y="{ly + 56}">if blocked, the Nova reply is silenced. Everything Nova passes to Travel Buddy is also guardrail-checked. All AWS resources come from deploy.py and are removed by cleanup.py.</text>')
a('</svg>')

target = Path(__file__).resolve().parent / "architecture.svg"
target.write_text("\n".join(out), encoding="utf-8")
print(f"wrote {target}")
