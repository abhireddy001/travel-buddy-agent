# Travel Agent ✈️

A conversational flight-booking agent that works across the world. It can:

- **Search flights** between 76 major airports on six continents (nonstop and one-stop via global hubs)
- **Check availability**: seats left and the current fare in every cabin
- **Find the cheapest options**: cheapest flight on a date, and cheapest departure days in a flexible window
- **Show fare trends**: trend direction, cheapest weekday, price spread, best booking window, a text sparkline, and an **interactive HTML chart**
- **Book, look up, and cancel** bookings, always after explicit user confirmation

It is protected by **Amazon Bedrock Guardrails** and reaches its tools through an **Amazon Bedrock AgentCore Gateway**.

## Architecture

Full diagram: [docs/architecture.png](docs/architecture.png) (editable [SVG](docs/architecture.svg), components, Mermaid and request flow in [docs/README.md](docs/README.md); regenerate with `docs/generate_architecture.py`).

```
 traveller (phone · tablet · laptop) ── public link, no login ── CloudFront ── nginx /ports/8502/
          │
          ▼
 Travel Buddy web app (Starlette · systemd travel-buddy · rate limits · Mia · For you · /status)
          │ typed chat                                     │ 🎙️ voice (WebSocket /ws/voice)
          ▼                                                ▼
 Travel Buddy agent (Strands, one per chat) ◄─travel_buddy── Voice bridge ◄──► Amazon Nova 2.5 Sonic
   │  name + interest profile                               (ui_action drives the screen · speech guard)
   │  Claude on Bedrock ◄── Bedrock Guardrail (card details only)
   │  MCP over HTTPS + Cognito JWT
   ▼
 AgentCore Gateway "travel-agent-gateway" (CUSTOM_JWT) ── target FlightTools (14 tools)
   ▼
 Lambda "travel-agent-flight-tools" (travel_core)
   ├─► SerpApi: Google Flights · Hotels · Maps · Directions · Videos · TikTok Discover
   ├─► M1 Singapore plans (dated catalogue from m1.com.sg)
   ├─► DynamoDB (bookings, offer cache) · S3 (trend charts) · Secrets Manager (SerpApi key)
   └─► CloudWatch (EMF metrics, dashboard, alarms, logs)
 In the browser: Instagram/TikTok embeds · OpenStreetMap tiles · seller booking links
```

Local mode (`--local`) swaps the Gateway for a stdio MCP server running the same `travel_core` code. Gateway tool schemas are generated from the same MCP tool definitions, so both modes expose identical tools.

## Run it

From the repository root, using the workshop `.venv`:

```bash
# 1. Deploy (or update) the AWS backend - about 2 minutes, safe to re-run
.venv/bin/python travel-agent/deploy/deploy.py --region us-east-1

# 2. Chat (uses the Gateway + Guardrail automatically once deployed)
.venv/bin/python travel-agent/agents/travel_agent.py
.venv/bin/python travel-agent/agents/travel_agent.py -q "Price trend SYD to LHR in December?"

# Options
#   --local           tools via the local stdio MCP server instead of the Gateway
#   --no-guardrail    turn the Bedrock Guardrail off (for comparison / debugging)
#   --model-id ID     override the Bedrock model (or set TRAVEL_AGENT_MODEL_ID)
```

The startup line shows the active setup, for example `mode=gateway · region=us-east-1 · guardrail 0ptr… v2 · 8 tools`. When the guardrail blocks a turn, the CLI prints `🛡️ (Bedrock Guardrail intervened on this turn)`.

### Example prompts

- "Find me a business-class flight from Dubai to Singapore on 2027-03-04, nonstop only."
- "I want to visit Tokyo in February. What's the cheapest week to fly from San Francisco?"
- "Show me the price trend for Sydney to London over the next 60 days."
- "Book it for Jane Doe and John Doe, contact jane@example.com." (the agent shows a summary and waits for your "yes")
- "Compare all the booking sites for that flight." (live mode: airline vs agencies, with booking links)
- "Cancel booking 7FCAC5."

## Safety: Bedrock Guardrail and spam guard

**Bedrock Guardrail** `travel-agent-guardrail`, applied to every Claude call (input and output):

| Policy | Configuration |
|---|---|
| **PII** (BLOCK) | Card number, CVV, expiry · US bank account, IBAN · SSN, US tax ID, US passport, UK NI, CA SIN · **Singapore NRIC/FIN** (custom regex `[STFGM]1234567X`) · PIN, password · AWS keys. Names, emails, phone numbers and addresses stay allowed, because bookings, greetings, cab firms and hotels need them |
| **Harmful content** | Sexual and hate at HIGH; violence, insults and misconduct at MEDIUM (input and output) |
| **Prompt attacks** | HIGH on the traveller's latest message (jailbreaks, prompt injection, system-prompt extraction) |
| **Denied topics** | Travel-document fraud; evading aviation security |

- **History:** blocked input is redacted from the conversation, so the data never reaches later model calls.
- **Voice:** each spoken request is checked with `ApplyGuardrail`. If it's blocked, Nova's reply is silenced and the guardrail message is shown.
- **Verified with `ApplyGuardrail`:** 10 of 10 harmful or PII prompts blocked; 8 of 8 normal travel messages and 5 of 5 typical replies passed (place names, the War Remnants Museum, emails, booking IDs, cab phone numbers, hotel addresses, map pins).

**Spam guard** (web app):
- More than **5 requests**, or more than **5 new chat sessions**, from the same IP within **1 minute** returns HTTP 429: **"You're too fast, please try again."** This also applies to voice calls and voice lookups.
- The longer limits remain: 30 messages per 10 minutes per visitor and 400 per day overall.
- The visitor's IP is taken from CloudFront's `X-Forwarded-For` entry, so it can't be spoofed.

## AgentCore Gateway

- **Authentication:** `CUSTOM_JWT` with a Cognito user pool. The agent uses the OAuth client-credentials flow. Its client secret is read from Cognito at runtime with your AWS credentials and is never written to disk. Tokens last 60 minutes and refresh automatically.
- **Unauthorized calls:** requests without a valid token get HTTP 401.
- **Tool access:** the Gateway calls the Lambda through its own IAM role, which may only invoke that one function.
- **Lambda permissions:** read/write on the bookings table and `PutObject` on `charts/*`, nothing else.
- **Booking safety:** each booking is a DynamoDB transaction with a capacity condition, so concurrent bookings cannot oversell a flight. Cancelling cannot run twice.

## Live prices: Google Flights via SerpApi

When a SerpApi key is configured, the tools return **real-time Google Flights prices**. Google Flights aggregates airline websites and online travel agencies.

| Tool | Live behaviour |
|---|---|
| `search_flights` | Live offers across airlines (`flight_id` starts with `GF-`), plus Google's price level (low/typical/high) and typical range |
| `get_booking_options` | Every seller of that flight, airline direct and travel agencies, with price, fare details, baggage and a booking link |
| `find_cheapest_dates` | Live scan of up to 7 departure days per call (one search per day, run in parallel) |
| `get_price_trends` | Google's recorded price history for that departure date, with an interactive chart |
| `book_flight` | Not used for live offers. Booking and payment happen on the seller's site through the booking link |

Set it up once:

1. Get a key at https://serpapi.com/manage-api-key (the free tier is limited per month).
2. Run `.venv/bin/python travel-agent/deploy/set_serpapi_key.py`. This prompts for the key without showing it, validates it, and stores it in Secrets Manager as `travel-agent/serpapi-api-key`.
3. Click **New chat** in the web app. The Lambda picks the key up within about 5 minutes.

For local mode (`--local`), export `SERPAPI_API_KEY` instead.

Without a key, the tools fall back to the deterministic **simulated** market (fictional carriers). Every simulated result is labelled `SIMULATED`, and the agent says so.

Each live search uses one SerpApi search from your quota. Identical searches within about an hour are served from SerpApi's cache and are free.

## Public web app

Open **https://d11xq6hty0qqxr.cloudfront.net/ports/8502/** on any phone, tablet or laptop. No login or key is needed.

- **Hosting:** the app runs as the `travel-buddy` systemd service (starts on boot, restarts on crash), behind nginx (`/ports/8502/`) and Amazon CloudFront (all edge locations, no country restrictions, IPv6, HTTP/2 and HTTP/3).
- **Abuse protection:** each visitor (real IP from CloudFront's `X-Forwarded-For` entry) can send 30 messages per 10 minutes, and all visitors together 400 messages a day (set with `TRAVEL_WEB_DAILY_LIMIT`). Both limits show a friendly message. Up to 60 chats can run at once; idle chats close after 20 minutes.
- **Status page:** `/status` is public and read-only. It doesn't show the AWS account ID or the Gateway endpoint.
- **Locking it again:** set `TRAVEL_WEB_REQUIRE_KEY=1` in the service file to bring back the access key (stored in `data/web-access-key.txt`).
- **Commands:** `sudo systemctl restart travel-buddy` to restart; logs are in `travel-agent/data/web-8502.log`.

## Voice mode: talk to Travel Buddy (Amazon Nova 2.5 Sonic)

Tap the **🎙️ mic** next to the send button, on a phone, tablet or laptop, and talk. Your speech streams over a WebSocket (`/ws/voice`, through CloudFront as `wss://`) to **Amazon Nova 2.5 Sonic** (`amazon.nova-2-5-sonic`), a speech-to-speech model. It listens, replies out loud (voice "tiffany") and calls tools mid-conversation:

| Tool | What it does |
|---|---|
| `travel_buddy(request)` | Forwards the request to **the same Travel Buddy agent the text chat uses**, so the name, interests, guardrail and all 13 live tools carry over. The full result (cards, maps, charts, reels) renders in the chat, and Nova speaks a short summary |
| `ui_action(action, target)` | Drives the page by voice: `show` hotels / places / transport / videos / chart / map (scrolls there and highlights it), `scroll` up / down / top / bottom, `play_video` N, `click_suggestion` N, `open_chart`, `new_chat`, `open_status` |

- **Browser:** the browser captures the mic with echo cancellation and noise suppression and downsamples it to 16 kHz PCM in an AudioWorklet. Nova's 16 kHz speech plays back gaplessly; talking over it stops playback (barge-in).
- **On screen:** your words appear as 🎙️ messages and Nova's as a live caption. Mia reacts: curious while you speak, waiting during lookups, excited when results land.
- **Speech guard (answer only):** Nova is instructed to speak only the answer, and the server enforces it:
  - Segments that narrate actions ("let me check", "one moment", "I'll scroll…") are muted before they reach the speaker or the caption.
  - Audio is held while a Travel Buddy lookup runs, and Nova stays silent after screen actions until the traveller speaks.
  - Next-step offers ("Want me to…?", "Shall I…?") are stripped from results before Nova sees them.
  - The only questions allowed are for essential details: name, destination, dates.
- **Limits:** 6 voice calls at once, 8 minutes per call, and the same per-visitor and daily rate limits as typed chat. Payment details are never taken; the text agent's guardrail still blocks card numbers.
- **Requirements:** `strands-agents` voice extras: `aws-sdk-bedrock-runtime==0.9.0` (the newest release compatible with Strands 1.34; 0.10+ renamed the client) and `smithy-aws-core`. Browsers need HTTPS and microphone permission. Works on current Chrome, Edge, Safari (iOS 14.5+) and Firefox.
- **Tested** end to end through the public `wss://` link with recorded speech: "My name is Maya", "Show me the best beaches in Bali" and "Scroll to the places on my screen" were transcribed, the name was saved, 8 beach cards with a map were rendered, the UI scrolled to them, and about 23 s of spoken replies came back.

## Greeting and name memory

Every chat opens with Travel Buddy's greeting and **"How may I address you?"**.
- **Saving the name:** when the traveller answers, the agent calls a local `remember_traveller_name` tool. The name is checked (letters, spaces, `. ' -`, at most 40 characters) and written into the agent's instructions for the rest of the conversation, so it isn't lost in long chats.
- **In the chat:** the UI shows the name on the traveller's messages, in the chat header and in the side panel.
- **If the first message is a question:** Travel Buddy answers it and then asks for the name once.
- **Starting over:** **New chat** and page reloads start a fresh conversation.

## Mia, the cheerful beach companion

Mia is a cheerful, original animated cartoon girl in a straw sun hat and polka-dot sundress. She lives in the beach scene and is always excited:

| When | Mia |
|---|---|
| You type, or Travel Buddy is responding | Runs to the **chat window**. On wide screens she stands beside it next to the message box; on narrower screens she perches on its header. She's **curious** while you type ("Ooh, where to? 👀") and **waits** with thinking dots while Travel Buddy works |
| Results arrive | **Excited** jump with sparkles, naming the find (top attraction and rating, hotels, taxi time, videos, price chart, flights) |
| A card number is blocked | **Protective**, with a shield: "Keep card numbers secret 🛡️" |
| You're idle | Strolls along the sand, walking behind the chat to switch sides, and reacts to the parasailing, balloons, jet ski, shacks and plane. She also **recommends attractions from your results**; tap her bubble to ask Travel Buddy about one |
| Idle for 2 minutes | Dozes off (z z z) and cheers when you come back |

She appears on every device. On phones and tablets without beach margins she perches on the chat header, in the space between the title and **New chat**, and still reacts from there. She respects reduced-motion settings. Her bubble is an `aria-live` status, and all text comes from the UI or real results.

## Default destinations: ASEAN

The built-in suggestions focus on **ASEAN countries**: the top-bar examples, the "ASEAN escapes" list (Singapore to Bali, Kuala Lumpur to Bangkok, Manila to Siem Reap, Jakarta to Da Nang, Ho Chi Minh City to Cebu), the welcome chips and Mia's tips. The system prompt also tells Travel Buddy to suggest ASEAN destinations by default, with regional tips (Grab, seasons, ferries), while still helping with anywhere in the world. 18 ASEAN leisure airports (DPS, HKT, CNX, SGN, HAN, DAD, SAI, LPQ, CEB, PEN, LGK and more) were added to the airport list, so city names resolve.

## Recommendations that follow the traveller's interests

As the chat goes on, Travel Buddy picks up interests, budget, travel style, destinations and dislikes ("I love street food", "on a budget with my partner", "hate crowds"). It saves them with a local `remember_traveller_preferences` tool, which accepts short phrases only, de-duplicates, follows the newest interests, and lets a new like override an old dislike. The profile is written into the agent's instructions, so every recommendation fits it:
- **Places:** `explore_places` searches use matching categories and queries (temples and street food for a food-and-culture fan).
- **Hotels:** matched to the budget.
- **Suggestions:** each comes with a short reason.

The chat UI shows **"✨ For you"** chips after each reply (for example "🍜 Best street food in Bangkok", "🏯 Temples and culture in Bangkok", "💸 Budget hotels in Bangkok", "💞 Romantic spots in Bangkok"), built from the profile and the current destination. Mia's idle tips favour real places from the results that match those interests. **New chat** starts a fresh profile.

## Mobile plans for the trip: M1 Singapore only

For any SIM, eSIM, roaming, data or prepaid-plan question, Travel Buddy calls `get_m1_travel_plans` and recommends **only M1 Singapore plans**. It never names or compares other telcos or eSIM apps, and declines politely if asked.

| Traveller | M1 options (from m1.com.sg, as of 2026-11-13) |
|---|---|
| Prepaid (M Card) | Roam Like Home (free in MY, ID, HK, MO, TW); $15 Roaming Pack (2GB, 10 days, 12 countries); $28 Roaming Pack (2GB, 10 days, 17 countries) |
| Postpaid (Bespoke) | Daily Passport: 3GB per 24 h at $2.95, $3.95, $5.95 or $8.95 a day depending on the country, plus $4.95 per bill cycle. Data Passport (monthly): $15, $35 ASEAN, $35, $65 Asia, $65 Europe |
| Visitors to Singapore | Tourist SIMs from $12 (500GB, 30 days) to $30 (1000GB); sold at Changi Airport, M1 Shops, 7-Eleven and Cheers |

- **Matching:** cities resolve to countries (Bali → Indonesia).
- **Best pick:** for each traveller type, the tool picks the cheapest fitting plan and computes the trip total (Daily Passport × days + $4.95, compared with a monthly Data Passport).
- **Chat card:** an **M1 travel plans** card with a ⭐ Best pick badge and "View on m1.com.sg" links.
- **Suggested everywhere:**
  - A **Mobile plans** top-bar button and a **📶 Stay connected with M1** side-panel section (prepaid Bali, postpaid ASEAN Data Passport, Daily Passport Bangkok, tourist SIM).
  - A welcome chip, and a "📶 M1 roaming plan for <destination>" chip in **For you**.
  - Mia's "Got data sorted?" tips.
  - A **Stay connected** line at the end of every full trip plan.
- **Accuracy:** prices are a dated snapshot in `travel_core/m1_plans.py` with source links, and the traveller is always told to confirm on the M1 page. Update that file when M1 changes its plans.

## Trip planner: hotels, getting around, places and shopping

Travel Buddy plans whole trips. Ask "Plan 4 days in Paris from New York, 21–25 Jan". Using live Google data through SerpApi, it builds a day-by-day itinerary with a budget, and the chat shows **cards with photos and a map with numbered pins** for each section.

| Tool | SerpApi engine | What the chat shows |
|---|---|---|
| `search_flights`, `get_price_trends` | `google_flights` | Flight table and an inline price-history chart |
| `search_hotels` | `google_hotels` | Hotel cards (photo, stars, rating, price per night and total, amenities, website) and a map |
| `find_ground_transport` | `google_maps_directions` + `google_maps` | Car/taxi, transit (with fare) and walking times; taxi-ride estimate; local cab companies (rating, phone, open now); map A to B |
| `explore_places` | `google_maps` | Cards for attractions, shopping, markets, restaurants, cafes, nightlife, museums, parks, beaches or kids' activities, and a map |
| `find_social_videos` | Instagram: `google_videos` + `site:instagram.com`. TikTok: native `tiktok_discover`, then `google_videos` + `site:tiktok.com` | Playable Instagram Reels, or TikToks when Instagram has none (or when asked), shown as video cards that play in the chat via each platform's official embed player |

Notes:

- **Cabs:** SerpApi has no real-time ride-hailing availability (Uber, Lyft, Grab, Bolt). Travel Buddy shows the driving time as the taxi estimate, plus local taxi companies, and says so.
- **Maps:** drawn in the browser from **OpenStreetMap** tiles with attribution, with no API key or map library. Google Static Maps would need a separate paid Google Maps key.
- **Data integrity:** cards are built from the tool results, not from the model's text, so names, prices, ratings and links are the real API data. Text and URLs are rendered safely (text nodes, `https://` links only).
- **Instagram / TikTok:** SerpApi has no Instagram service, so Google Videos is searched with `site:instagram.com`. TikTok uses SerpApi's native **TikTok Discover** engine (plays, likes, creator, date). Discover pages exist only for popular keywords, so it tries the place, then the city, then falls back to Google Videos with `site:tiktok.com`. Discover results are trending, so travel content is ranked first and obvious hoaxes or fact-checks are dropped. Players load only when the user taps a video. The content comes from creators and isn't verified. The TikTok fallback costs a second search only when Instagram returns nothing.
- **Quota:** a full plan uses about 6–9 SerpApi searches. The transport lookup uses 2, one for the route and one for cab companies.

## Monitoring (Amazon CloudWatch)

`deploy.py` creates the **TravelAgent** dashboard:
https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/TravelAgent

| Area | Metrics | Source |
|---|---|---|
| Agent (web / CLI) | Turns, errors, turn latency p50/p90, guardrail interventions, tool calls per turn, input/output tokens | `TravelAgent` namespace, `PutMetricData` per turn (background thread; `TRAVEL_AGENT_METRICS=off` disables) |
| Tools (Lambda) | Invocations, errors and failures, latency per tool; live vs simulated calls | `TravelAgent` namespace, Embedded Metric Format log lines (no extra IAM) |
| Live data | SerpApi searches, booking-option lookups, errors, latency, **searches left** this month | `TravelAgent` namespace, EMF (quota checked at most every 15 min via SerpApi's free account API) |
| Bookings | Created, cancelled, failed | `TravelAgent` namespace, EMF |
| Bedrock | Model invocations and latency, guardrail evaluations and interventions | `AWS/Bedrock`, `AWS/Bedrock/Guardrails` (native) |
| Gateway | Requests, user/system errors, throttles, per-tool latency, JWT auth success/failure | `AWS/Bedrock-AgentCore` (native) |
| Infrastructure | Lambda invocations, errors, throttles, concurrency; DynamoDB capacity and throttles | `AWS/Lambda`, `AWS/DynamoDB` (native) |
| Logs | Recent Lambda errors (Logs Insights widget); Gateway application logs | `/aws/lambda/travel-agent-flight-tools`, `/aws/vendedlogs/bedrock-agentcore/gateway/travel-agent-gateway` (30-day retention) |

Alarms (prefix `travel-agent-`):

| Alarm | Fires when |
|---|---|
| `lambda-errors` | The Lambda reports any error in 5 minutes |
| `tool-failures` | A tool raises an unhandled exception |
| `serpapi-errors` | 3 or more live lookups fail in 15 minutes |
| `serpapi-quota-low` | Fewer than 25 SerpApi searches are left |
| `gateway-system-errors` | The Gateway returns any system error |
| `agent-errors` | Web chat turns fail |
| `agent-latency-p90` | p90 web turn latency exceeds 60 seconds over 15 minutes |

The alarms have **no notification actions**, because the workshop IAM role cannot create SNS topics. They show state on the dashboard and in the CloudWatch console. To get email or Slack alerts, create an SNS topic with an account that has permission, then add its ARN as an alarm action.

Logs record tool names and argument names only, never passenger names, emails or card data.

## Tests and cleanup

```bash
.venv/bin/python -m pytest -q travel-agent/tests          # 22 tests, no AWS or SerpApi calls
.venv/bin/python travel-agent/deploy/cleanup.py           # deletes all deployed resources (asks first)
```

The workshop IAM role cannot tag DynamoDB or Cognito resources, and can only delete S3 buckets named `ecommerce-workshop-*`. That's why those resources are untagged and the chart bucket uses that prefix.
