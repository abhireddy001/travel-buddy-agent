"""
Fare-trend analysis and rendering for the Travel Agent.

- `cheapest_dates`  : lowest fare per departure day across a flexible window
- `price_trends`    : daily series + stats + weekday pattern + booking-window advice
- `render_trend_html`: self-contained interactive HTML report (no external assets)
- `sparkline`        : compact text trend for terminal / chat output
"""

from __future__ import annotations

import html
import json
from datetime import date, timedelta
from pathlib import Path

from .airports import resolve_airport
from .provider import (
    FlightError,
    SimulatedFlightProvider,
    lead_time_factor,
    normalize_cabin,
    parse_date,
)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_WINDOW_DAYS = 120
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"


def _window(provider: SimulatedFlightProvider, start: str | date, days: int) -> list[date]:
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise FlightError(f"Window must be between 1 and {MAX_WINDOW_DAYS} days.")
    first = parse_date(start)
    if first < provider.today:
        raise FlightError(f"Start date {first} is in the past (today is {provider.today}).")
    return [first + timedelta(days=i) for i in range(days)]


def daily_lowest_fares(provider: SimulatedFlightProvider, origin: str, destination: str,
                       start: str | date, days: int, cabin: str = "economy",
                       max_stops: int | None = None) -> list[dict]:
    """Lowest available fare for each departure date in the window."""
    cabin = normalize_cabin(cabin)
    rows = []
    for d in _window(provider, start, days):
        try:
            best = provider.cheapest_fare(origin, destination, d, cabin, max_stops)
        except FlightError:
            best = None          # e.g. beyond schedule horizon
        if best is None:
            continue
        rows.append({
            "date": d.isoformat(),
            "weekday": WEEKDAYS[d.weekday()],
            "lowest_fare": best.price_per_passenger,
            "flight_id": best.flight_id,
            "stops": best.stops,
            "carriers": best.carriers,
            "duration_minutes": best.total_duration_minutes,
        })
    if not rows:
        raise FlightError("No bookable flights found in that window.")
    return rows


def cheapest_dates(provider: SimulatedFlightProvider, origin: str, destination: str,
                   start: str | date, days: int = 14, cabin: str = "economy",
                   max_stops: int | None = None, top_n: int = 5) -> dict:
    rows = daily_lowest_fares(provider, origin, destination, start, days, cabin, max_stops)
    ranked = sorted(rows, key=lambda r: r["lowest_fare"])
    avg = sum(r["lowest_fare"] for r in rows) / len(rows)
    return {
        "route": f"{resolve_airport(origin).code}-{resolve_airport(destination).code}",
        "cabin": normalize_cabin(cabin),
        "currency": provider.currency,
        "window": {"start": rows[0]["date"], "end": rows[-1]["date"], "days_searched": len(rows)},
        "average_lowest_fare": round(avg, 2),
        "cheapest_dates": [
            {**r, "savings_vs_window_average": round(avg - r["lowest_fare"], 2)}
            for r in ranked[:top_n]
        ],
        "most_expensive_date": ranked[-1],
    }


def _moving_average(values: list[float], window: int = 7) -> list[float]:
    half = window // 2
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - half): i + half + 1]
        out.append(round(sum(chunk) / len(chunk), 2))
    return out


def _slope_pct(values: list[float]) -> float:
    """Least-squares slope across the window, as % change from start to end of the fit line."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mx, my = (n - 1) / 2, sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den
    start_fit = my - slope * mx
    return round((slope * (n - 1)) / start_fit * 100, 1) if start_fit else 0.0


def booking_window_advice(provider: SimulatedFlightProvider, departure: date) -> dict:
    """Where on the lead-time curve this departure sits, and the cheapest booking window."""
    days_out = (departure - provider.today).days
    curve = {k: lead_time_factor(k) for k in range(0, 181)}
    best = min(curve.values())
    best_days = [k for k, v in curve.items() if v == best]
    current = curve.get(days_out, lead_time_factor(days_out))
    return {
        "days_until_first_departure": days_out,
        "cheapest_booking_window_days_before": [min(best_days), max(best_days)],
        "current_premium_vs_best_window_pct": round((current / best - 1) * 100, 1),
        "last_minute_premium_pct": round((curve[3] / best - 1) * 100, 1),
    }


def price_trends(provider: SimulatedFlightProvider, origin: str, destination: str,
                 start: str | date, days: int = 30, cabin: str = "economy",
                 max_stops: int | None = None) -> dict:
    rows = daily_lowest_fares(provider, origin, destination, start, days, cabin, max_stops)
    fares = [r["lowest_fare"] for r in rows]
    ma = _moving_average(fares)
    for r, m in zip(rows, ma):
        r["moving_avg_7d"] = m

    by_wd: dict[str, list[float]] = {w: [] for w in WEEKDAYS}
    for r in rows:
        by_wd[r["weekday"]].append(r["lowest_fare"])
    weekday_avg = {w: round(sum(v) / len(v), 2) for w, v in by_wd.items() if v}

    cheapest = min(rows, key=lambda r: r["lowest_fare"])
    priciest = max(rows, key=lambda r: r["lowest_fare"])
    avg = sum(fares) / len(fares)
    slope = _slope_pct(fares)
    direction = "rising" if slope > 3 else "falling" if slope < -3 else "flat"
    o, d = resolve_airport(origin), resolve_airport(destination)

    return {
        "route": f"{o.code}-{d.code}",
        "route_label": f"{o.label()} to {d.label()}",
        "cabin": normalize_cabin(cabin),
        "currency": provider.currency,
        "as_of": provider.today.isoformat(),
        "window": {"start": rows[0]["date"], "end": rows[-1]["date"], "days": len(rows)},
        "summary": {
            "average_lowest_fare": round(avg, 2),
            "cheapest": {k: cheapest[k] for k in ("date", "weekday", "lowest_fare", "flight_id")},
            "most_expensive": {k: priciest[k] for k in ("date", "weekday", "lowest_fare", "flight_id")},
            "spread_pct": round((priciest["lowest_fare"] / cheapest["lowest_fare"] - 1) * 100, 1),
            "trend_direction": direction,
            "trend_change_pct": slope,
        },
        "weekday_average": weekday_avg,
        "cheapest_weekday": min(weekday_avg, key=weekday_avg.get),
        "booking_window": booking_window_advice(provider, parse_date(rows[0]["date"])),
        "daily": rows,
    }


# =============================================================================
# Rendering
# =============================================================================

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(_BLOCKS[int((v - lo) / span * (len(_BLOCKS) - 1))] for v in values)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fare trends · __TITLE__</title>
<style>
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb; --surface-2: #f4f3f0;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77766f;
  --grid: #e6e5e1; --axis: #c9c8c2; --crosshair: #8d8c85;
  --series-1: #2a78d6; --series-2: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --surface-2: #242423;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
    --grid: #2f2f2d; --axis: #4a4a46; --crosshair: #8d8c85;
    --series-1: #3987e5; --series-2: #d95926;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19; --surface-2: #242423;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
  --grid: #2f2f2d; --axis: #4a4a46; --crosshair: #8d8c85;
  --series-1: #3987e5; --series-2: #d95926;
}
html, body { margin: 0; }
.viz-root { background: var(--surface-1); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; min-height: 100vh;
  padding: 28px clamp(16px, 4vw, 48px); box-sizing: border-box; }
h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
.sub { color: var(--text-secondary); margin: 0 0 20px; }
.note { color: var(--text-muted); font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 24px; }
.tile { background: var(--surface-2); border-radius: 10px; padding: 12px 14px; }
.tile .k { color: var(--text-secondary); font-size: 12px; }
.tile .v { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .d { color: var(--text-secondary); font-size: 12px; }
.card { background: var(--surface-1); border: 1px solid var(--grid); border-radius: 12px; padding: 16px; margin-bottom: 20px; position: relative; }
.legend { display: flex; gap: 16px; margin: 6px 0 4px; color: var(--text-secondary); font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 16px; height: 0; border-top: 2px solid; display: inline-block; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .lbl { fill: var(--text-primary); font-size: 11px; font-weight: 600; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--axis); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 14px rgba(0,0,0,.12); display: none; white-space: nowrap; z-index: 2; }
.tip .row { display: flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
.tip .t { font-weight: 600; margin-bottom: 4px; }
.bar { cursor: default; }
.bar:hover, .bar:focus { opacity: .8; outline: none; }
.bar:focus-visible { stroke: var(--text-primary); stroke-width: 2px; }
details { margin-top: 8px; }
summary { cursor: pointer; color: var(--text-secondary); }
table { border-collapse: collapse; margin-top: 8px; font-variant-numeric: tabular-nums; font-size: 13px; }
th, td { text-align: left; padding: 4px 12px 4px 0; border-bottom: 1px solid var(--grid); }
th { color: var(--text-secondary); font-weight: 500; }
td.n, th.n { text-align: right; }
</style>
</head>
<body>
<div class="viz-root">
  <h1 id="title"></h1>
  <p class="sub" id="subtitle"></p>
  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2 id="lineTitle">Lowest fare by departure date</h2>
    <div class="legend" id="legend1"></div>
    <svg id="line" viewBox="0 0 900 320" role="img" aria-labelledby="lineDesc"><desc id="lineDesc"></desc></svg>
    <div class="tip" id="tip1"></div>
  </div>

  <div class="card" id="barCard">
    <h2>Average lowest fare by departure weekday</h2>
    <p class="note" id="wdNote"></p>
    <svg id="bars" viewBox="0 0 900 240" role="img" aria-labelledby="barDesc"><desc id="barDesc"></desc></svg>
    <div class="tip" id="tip2"></div>
  </div>

  <details>
    <summary>Show data table</summary>
    <table id="table"></table>
  </details>
  <p class="note" id="sourceNote">Simulated fares from the Travel Agent's demo flight market. Not live airline inventory.</p>
</div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const cur = DATA.currency;
const fmt = v => new Intl.NumberFormat("en-US", {style: "currency", currency: cur, maximumFractionDigits: 0}).format(v);
const fmtDate = s => new Date(s + "T00:00:00").toLocaleDateString("en-US", {month: "short", day: "numeric"});
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (parent) parent.appendChild(e); return e; };
const div = (cls, text, parent) => { const d = document.createElement("div"); if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text; if (parent) parent.appendChild(d); return d; };

// ---------- header + tiles
const S = DATA.summary;
document.getElementById("title").textContent = DATA.route_label + " · fare trends";
const HISTORY = DATA.kind === "history";
const tiles = document.getElementById("tiles");
const tile = (k, v, d) => { const t = div("tile", undefined, tiles); div("k", k, t); div("v", v, t); div("d", d, t); };
if (HISTORY) {
  document.getElementById("title").textContent = DATA.route_label + " · live price history";
  document.getElementById("subtitle").textContent =
    `${DATA.cabin.replace("_", " ")} · departing ${fmtDate(DATA.departure_date)} · lowest Google Flights price recorded each day, ${cur} per passenger, as of ${DATA.as_of}`;
  document.getElementById("lineTitle").textContent = `Lowest price for the ${fmtDate(DATA.departure_date)} departure, by date checked`;
  document.getElementById("barCard").remove();
  document.getElementById("sourceNote").textContent = `Live data: ${DATA.source}. Prices are confirmed by the seller at checkout.`;
  const tr = S.typical_price_range;
  tile("Lowest price now", S.current_lowest_price != null ? fmt(S.current_lowest_price) : "n/a",
       S.price_level ? `Google rates this ${S.price_level}` : "");
  tile("Typical range", tr ? `${fmt(tr[0])}–${fmt(tr[1])}` : "n/a", "for this route and date");
  tile("History low", fmt(S.history_low.lowest_fare), `seen ${fmtDate(S.history_low.date)}`);
  tile("History high", fmt(S.history_high.lowest_fare), `seen ${fmtDate(S.history_high.date)}`);
  tile("Change over period", `${S.change_over_history_pct > 0 ? "+" : ""}${S.change_over_history_pct}%`,
       `prices ${S.trend_direction} over ${DATA.window.days} observations`);
} else {
document.getElementById("subtitle").textContent =
  `${DATA.cabin.replace("_", " ")} · ${fmtDate(DATA.window.start)} – ${fmtDate(DATA.window.end)} departures · prices per passenger in ${cur}, as of ${DATA.as_of}`;
tile("Cheapest day", fmt(S.cheapest.lowest_fare), `${S.cheapest.weekday} ${fmtDate(S.cheapest.date)}`);
tile("Window average", fmt(S.average_lowest_fare), `${DATA.window.days} departure days`);
tile("Most expensive", fmt(S.most_expensive.lowest_fare), `${S.most_expensive.weekday} ${fmtDate(S.most_expensive.date)} · +${S.spread_pct}% vs cheapest`);
tile("Trend across window", `${S.trend_change_pct > 0 ? "+" : ""}${S.trend_change_pct}%`,
     `fares ${S.trend_direction} from first to last departure`);
const bw = DATA.booking_window;
tile("Best time to book", `${bw.cheapest_booking_window_days_before[0]}–${bw.cheapest_booking_window_days_before[1]} days`,
     `before departure · last-minute costs +${bw.last_minute_premium_pct}%`);
}

// ---------- line chart
(function lineChart() {
  const svg = document.getElementById("line"), tip = document.getElementById("tip1");
  const rows = DATA.daily, W = 900, H = 320, m = {l: 56, r: 92, t: 16, b: 34};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const vals = rows.flatMap(r => [r.lowest_fare, r.moving_avg_7d]);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.12 || 10;
  lo = Math.max(0, lo - pad); hi = hi + pad;
  const x = i => m.l + (rows.length === 1 ? iw / 2 : i * iw / (rows.length - 1));
  const y = v => m.t + ih - (v - lo) / (hi - lo) * ih;
  document.getElementById("lineDesc").textContent =
    `Line chart of the lowest ${DATA.cabin} fare for each departure date from ${DATA.window.start} to ${DATA.window.end}, with a 7-day average.`;

  // grid + y ticks
  const step = niceStep((hi - lo) / 5);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1}, svg);
    el("text", {x: m.l - 8, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = fmt(v);
  }
  el("line", {x1: m.l, x2: m.l + iw, y1: m.t + ih, y2: m.t + ih, stroke: "var(--axis)"}, svg);
  // x ticks - roughly 7 labels
  const every = Math.max(1, Math.round(rows.length / 7));
  rows.forEach((r, i) => { if ((i % every === 0 && rows.length - 1 - i >= every * 0.6) || i === rows.length - 1)
    el("text", {x: x(i), y: H - 12, "text-anchor": "middle"}, svg).textContent = fmtDate(r.date); });

  const path = key => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`).join("");
  el("path", {d: path("moving_avg_7d"), fill: "none", stroke: "var(--series-2)", "stroke-width": 2,
    "stroke-dasharray": "6 4", "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
  el("path", {d: path("lowest_fare"), fill: "none", stroke: "var(--series-1)", "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);

  // direct labels at line ends
  const last = rows.length - 1;
  const yA = y(rows[last].lowest_fare), yB = y(rows[last].moving_avg_7d);
  const [la, lb] = Math.abs(yA - yB) < 14 ? (yA < yB ? [yA - 7, yB + 7] : [yA + 7, yB - 7]) : [yA, yB];
  el("text", {x: x(last) + 8, y: la + 4, class: "lbl"}, svg).textContent = "Lowest fare";
  el("text", {x: x(last) + 8, y: lb + 4}, svg).textContent = "7-day avg";

  // cheapest-day marker + label
  const lowDate = HISTORY ? S.history_low.date : S.cheapest.date;
  const ci = Math.max(0, rows.findIndex(r => r.date === lowDate));
  el("circle", {cx: x(ci), cy: y(rows[ci].lowest_fare), r: 5, fill: "var(--series-1)",
    stroke: "var(--surface-1)", "stroke-width": 2}, svg);
  el("text", ci > rows.length * 0.85 ? {x: x(ci) - 10, y: y(rows[ci].lowest_fare) + 20, "text-anchor": "end", class: "lbl"} : {x: x(ci), y: y(rows[ci].lowest_fare) + 20, "text-anchor": "middle", class: "lbl"}, svg)
    .textContent = `${HISTORY ? "Lowest" : "Cheapest"} ${fmt(rows[ci].lowest_fare)}`;

  // legend
  const lg = document.getElementById("legend1");
  [["Lowest fare", "var(--series-1)", "solid"], ["7-day average", "var(--series-2)", "dashed"]].forEach(([t, c, s]) => {
    const sp = document.createElement("span"); const sw = document.createElement("i"); sw.className = "sw";
    sw.style.borderTopColor = c; sw.style.borderTopStyle = s; sp.appendChild(sw);
    sp.appendChild(document.createTextNode(t)); lg.appendChild(sp); });

  // crosshair + tooltip
  const cross = el("line", {y1: m.t, y2: m.t + ih, stroke: "var(--crosshair)", "stroke-width": 1, visibility: "hidden"}, svg);
  const dotA = el("circle", {r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden"}, svg);
  const dotB = el("circle", {r: 4, fill: "var(--series-2)", stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden"}, svg);
  const hit = el("rect", {x: m.l - 10, y: m.t, width: iw + 20, height: ih, fill: "transparent", tabindex: 0,
    "aria-label": "Fare chart; use arrow keys to step through dates"}, svg);
  let idx = -1;
  function show(i, clientX, clientY) {
    idx = i; const r = rows[i];
    for (const e of [cross, dotA, dotB]) e.setAttribute("visibility", "visible");
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    dotA.setAttribute("cx", x(i)); dotA.setAttribute("cy", y(r.lowest_fare));
    dotB.setAttribute("cx", x(i)); dotB.setAttribute("cy", y(r.moving_avg_7d));
    tip.replaceChildren();
    div("t", `${r.weekday} ${fmtDate(r.date)}`, tip);
    const line = (c, label, v) => { const row = div("row", undefined, tip); const sw = document.createElement("i");
      sw.className = "sw"; sw.style.borderTopColor = c; row.appendChild(sw); row.appendChild(document.createTextNode(`${label}: ${v}`)); };
    line("var(--series-1)", "Lowest fare", fmt(r.lowest_fare));
    line("var(--series-2)", "7-day avg", fmt(r.moving_avg_7d));
    if (!HISTORY) {
      div("row", `${r.stops ? r.stops + " stop" : "Nonstop"} · ${r.carriers.join(", ")}`, tip);
      div("row", `Flight ID ${r.flight_id}`, tip);
    }
    tip.style.display = "block";
    const card = svg.parentElement.getBoundingClientRect(), box = svg.getBoundingClientRect();
    const px = box.left - card.left + x(i) * box.width / W;
    tip.style.left = Math.min(px + 14, card.width - tip.offsetWidth - 8) + "px";
    tip.style.top = (box.top - card.top + m.t * box.height / H + 8) + "px";
  }
  function hide() { for (const e of [cross, dotA, dotB]) e.setAttribute("visibility", "hidden"); tip.style.display = "none"; }
  hit.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect(); const sx = (ev.clientX - box.left) * W / box.width;
    show(Math.max(0, Math.min(rows.length - 1, Math.round((sx - m.l) / (iw / Math.max(1, rows.length - 1)))))); });
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("blur", hide);
  hit.addEventListener("keydown", ev => {
    if (ev.key === "ArrowRight") { show(Math.min(rows.length - 1, idx + 1)); ev.preventDefault(); }
    if (ev.key === "ArrowLeft") { show(Math.max(0, idx - 1)); ev.preventDefault(); }
    if (ev.key === "Escape") hide(); });
  hit.addEventListener("focus", () => show(Math.max(0, idx)));
})();

// ---------- weekday bars
(function barChart() {
  if (HISTORY) return;
  const svg = document.getElementById("bars"), tip = document.getElementById("tip2");
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].filter(d => d in DATA.weekday_average);
  const W = 900, H = 240, m = {l: 56, r: 16, t: 24, b: 30}, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const hi = Math.max(...days.map(d => DATA.weekday_average[d])) * 1.12;
  const step = niceStep(hi / 4), y = v => m.t + ih - v / hi * ih;
  document.getElementById("barDesc").textContent = "Bar chart of average lowest fare by departure weekday.";
  document.getElementById("wdNote").textContent =
    `Departing on ${DATA.cheapest_weekday} is cheapest on average for this route in this window.`;
  for (let v = 0; v <= hi; v += step) {
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), stroke: v ? "var(--grid)" : "var(--axis)"}, svg);
    el("text", {x: m.l - 8, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = fmt(v);
  }
  const band = iw / days.length, bwid = Math.min(56, band * 0.6);
  days.forEach((d, i) => {
    const v = DATA.weekday_average[d], cx = m.l + band * (i + .5), top = y(v), h = m.t + ih - top;
    // bar with 4px rounded top, square at baseline
    const r = Math.min(4, h), x0 = cx - bwid / 2, x1 = cx + bwid / 2, base = m.t + ih;
    const bar = el("path", {class: "bar", tabindex: 0, fill: "var(--series-1)",
      d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x1 - r}Q${x1},${top} ${x1},${top + r}V${base}Z`,
      "aria-label": `${d}: ${fmt(v)}`}, svg);
    el("text", {x: cx, y: H - 10, "text-anchor": "middle"}, svg).textContent = d;
    if (d === DATA.cheapest_weekday)
      el("text", {x: cx, y: top - 8, "text-anchor": "middle", class: "lbl"}, svg).textContent = `Cheapest ${fmt(v)}`;
    const show = () => { tip.replaceChildren(); div("t", d, tip);
      div("row", `Average lowest fare: ${fmt(v)}`, tip);
      div("row", `${DATA.daily.filter(r => r.weekday === d).length} departure days`, tip);
      tip.style.display = "block";
      const card = svg.parentElement.getBoundingClientRect(), box = svg.getBoundingClientRect();
      tip.style.left = Math.min(box.left - card.left + cx * box.width / W + 12, card.width - tip.offsetWidth - 8) + "px";
      tip.style.top = (box.top - card.top + top * box.height / H) + "px"; };
    bar.addEventListener("pointerenter", show); bar.addEventListener("focus", show);
    bar.addEventListener("pointerleave", () => tip.style.display = "none");
    bar.addEventListener("blur", () => tip.style.display = "none");
  });
})();

// ---------- table view
(function table() {
  const t = document.getElementById("table");
  const head = t.createTHead().insertRow();
  (HISTORY ? [["Date checked", ""], ["Day", ""], ["Lowest price", "n"], ["7-day avg", "n"]]
            : [["Date", ""], ["Day", ""], ["Lowest fare", "n"], ["7-day avg", "n"], ["Stops", "n"], ["Carriers", ""], ["Flight ID", ""]])
    .forEach(([h, c]) => { const th = document.createElement("th"); th.textContent = h; if (c) th.className = c; head.appendChild(th); });
  const body = t.createTBody();
  DATA.daily.forEach(r => { const tr = body.insertRow();
    (HISTORY ? [[r.date], [r.weekday], [fmt(r.lowest_fare), "n"], [fmt(r.moving_avg_7d), "n"]]
             : [[r.date], [r.weekday], [fmt(r.lowest_fare), "n"], [fmt(r.moving_avg_7d), "n"], [r.stops, "n"], [r.carriers.join(", ")], [r.flight_id]])
      .forEach(([v, c]) => { const td = tr.insertCell(); td.textContent = v; if (c) td.className = c; }); });
})();

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1))), f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}
</script>
</body>
</html>
"""


def render_trend_html(trends: dict, output_dir: Path | None = None) -> Path:
    """Write an interactive, self-contained HTML trend report and return its path."""
    out = Path(output_dir or OUTPUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    if trends.get("kind") == "history":
        name = f"fare-trends-live-{trends['route']}-{trends['cabin']}-{trends['departure_date']}.html"
    else:
        name = (f"fare-trends-{trends['route']}-{trends['cabin']}-"
                f"{trends['window']['start']}-{trends['window']['days']}d.html")
    path = out / name
    data_json = json.dumps(trends).replace("<", "\\u003c")   # safe inside <script>
    page = (
        _HTML_TEMPLATE
        .replace("__TITLE__", html.escape(trends["route_label"]))
        .replace("__DATA__", data_json)
    )
    path.write_text(page, encoding="utf-8")
    return path
