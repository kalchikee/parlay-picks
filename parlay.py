"""
Parlay Pick Engine
------------------
Morning mode (--morning):
  - Scans all Kalshi sports markets
  - Picks the 3 best legs (different sports, 62–80c sweet spot)
  - Saves picks to data/picks_YYYY-MM-DD.json
  - Sends Discord embed

Recap mode (--recap):
  - Loads today's saved picks
  - Checks each leg result via Kalshi API
  - Updates data/tally.json (running W/L record)
  - Sends Discord recap embed
"""

import argparse
import base64
import datetime
import json
import math
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY_ID      = os.environ["KALSHI_API_KEY_ID"]
PRIVATE_KEY_PEM = os.environ["KALSHI_PRIVATE_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BASE_URL        = "https://api.elections.kalshi.com/trade-api/v2"
API_PREFIX      = "/trade-api/v2"
DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# All sport series to scan on Kalshi (game + spread markets)
SERIES = [
    "KXMLBGAME",    "KXMLBSPREAD",
    "KXNBAGAME",    "KXNBASPREAD",
    "KXNHLGAME",    "KXNHLSPREAD",
    "KXMLSGAME",
    "KXNFLGAME",    "KXNFLSPREAD",
    "KXUFCFIGHT",
    "KXNCAAMGAME",  "KXNCAAMBSPREAD",
    "KXNCAAFGAME",
    "KXEPLGAME",
    "KXWNBAGAME",
]

SPORT_LABELS = {
    "KXMLBGAME": "MLB", "KXMLBSPREAD": "MLB Spread",
    "KXNBAGAME": "NBA", "KXNBASPREAD": "NBA Spread",
    "KXNHLGAME": "NHL", "KXNHLSPREAD": "NHL Spread",
    "KXMLSGAME": "MLS",
    "KXNFLGAME": "NFL", "KXNFLSPREAD": "NFL Spread",
    "KXUFCFIGHT": "UFC",
    "KXNCAAMGAME": "NCAAM", "KXNCAAMBSPREAD": "NCAAM Spread",
    "KXNCAAFGAME": "NCAAF",
    "KXEPLGAME": "EPL",
    "KXWNBAGAME": "WNBA",
}

# Map series to base sport (for "different sports" dedup in parlay)
SPORT_GROUP = {
    "KXMLBGAME": "MLB", "KXMLBSPREAD": "MLB",
    "KXNBAGAME": "NBA", "KXNBASPREAD": "NBA",
    "KXNHLGAME": "NHL", "KXNHLSPREAD": "NHL",
    "KXMLSGAME": "MLS",
    "KXNFLGAME": "NFL", "KXNFLSPREAD": "NFL",
    "KXUFCFIGHT": "UFC",
    "KXNCAAMGAME": "NCAAM", "KXNCAAMBSPREAD": "NCAAM",
    "KXNCAAFGAME": "NCAAF",
    "KXEPLGAME": "EPL",
    "KXWNBAGAME": "WNBA",
}

# ── Kalshi Auth ───────────────────────────────────────────────────────────────

def _get_headers(method: str, path: str) -> dict:
    key = serialization.load_pem_private_key(PRIVATE_KEY_PEM.encode(), password=None)
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + API_PREFIX + path).encode()
    sig = key.sign(msg, padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    ), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }


def _get(path: str) -> dict:
    try:
        r = requests.get(BASE_URL + path, headers=_get_headers("GET", path), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  [WARN] GET {path} failed: {e}")
    return {}

# ── Market Scanning ───────────────────────────────────────────────────────────

def _fetch_series(series: str) -> list:
    data = _get(f"/markets?series_ticker={series}&status=open&limit=200")
    return data.get("markets", [])


def _score(m: dict) -> float:
    """Score a market for parlay worthiness. Returns 0 if not suitable."""
    bid = float(m.get("yes_bid_dollars") or 0)
    ask = float(m.get("yes_ask_dollars") or 0)
    if bid <= 0 or ask <= 0:
        return 0.0
    mid    = (bid + ask) / 2
    spread = ask - bid
    vol    = float(m.get("volume_fp") or 0)

    # 55–85c YES markets (widened from 62–80c for playoff season coverage)
    if mid < 0.55 or mid > 0.85:
        return 0.0
    # Skip illiquid markets (spread > 10c)
    if spread > 0.10:
        return 0.0

    # Sweet spot peaks at ~68c
    sweet     = 1.0 - abs(mid - 0.68) / 0.15
    tight     = 1.0 - (spread / 0.08)
    vol_score = min(1.0, math.log10(max(1, vol)) / 6)

    return sweet * 0.50 + tight * 0.30 + vol_score * 0.20


def _game_key(event_ticker: str) -> str:
    """Extract a game-unique key from Kalshi event_ticker that matches across
    GAME and SPREAD markets. Kalshi tickers have format:
      KXNBAGAME-26APR18MINDEN-DEN
      KXNBASPREAD-26APR18MINDEN-DEN-5.5
    The second segment (date+teams) is the same for both, so we use that
    as the game identity to prevent 2 bets on the same matchup."""
    parts = event_ticker.split("-")
    if len(parts) >= 2:
        return parts[1]  # date+teams segment, shared across GAME/SPREAD
    return event_ticker


def scan_all_markets() -> list:
    """Return scored candidates from all series, one per event, sorted best first."""
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4))).strftime("%Y-%m-%d")

    candidates = []
    for series in SERIES:
        print(f"  Scanning {series}...")
        markets = _fetch_series(series)
        seen_events: set = set()
        for m in markets:
            event = m.get("event_ticker", m.get("ticker", ""))
            if event in seen_events:
                continue

            # Only include games expiring today (ET)
            exp = m.get("expected_expiration_time", "") or m.get("close_time", "")
            if exp[:10] != today:
                continue

            s = _score(m)
            if s > 0:
                bid = float(m.get("yes_bid_dollars") or 0)
                ask = float(m.get("yes_ask_dollars") or 0)
                candidates.append({
                    "ticker":  m.get("ticker"),
                    "title":   m.get("title", ""),
                    "series":  series,
                    "sport":   SPORT_LABELS.get(series, series),
                    "event":   event,
                    # Game-level identity (matches across GAME/SPREAD markets)
                    "game_key": _game_key(event),
                    "mid":     round((bid + ask) / 2, 2),
                    "bid":     bid,
                    "ask":     ask,
                    "volume":  float(m.get("volume_fp") or 0),
                    "score":   s,
                    "close":   m.get("expected_expiration_time", ""),
                    "result":  m.get("result", ""),
                })
                seen_events.add(event)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def build_parlay(candidates: list, max_legs: int = 3) -> list:
    """Pick top legs from different sports (game + spread count as same sport).
    Allows multiple legs from the same sport if needed to reach 3,
    but prefers diversity. Never puts two bets on the same game."""
    legs = []
    used_sports: set = set()
    used_events: set = set()

    # Pass 1: one leg per sport (prefer diversity)
    for c in candidates:
        sport = SPORT_GROUP.get(c["series"], c["series"])
        if sport not in used_sports and c["game_key"] not in used_events:
            legs.append(c)
            used_sports.add(sport)
            used_events.add(c["game_key"])
        if len(legs) >= max_legs:
            break

    # Pass 2: if < 3 legs, allow same sport but different game
    if len(legs) < max_legs:
        for c in candidates:
            if c["game_key"] not in used_events:
                legs.append(c)
                used_events.add(c["game_key"])
            if len(legs) >= max_legs:
                break

    return legs

# ── Data Persistence ──────────────────────────────────────────────────────────

def _picks_path(date: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"picks_{date}.json")


def _tally_path() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "tally.json")


def load_tally() -> dict:
    path = _tally_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"wins": 0, "losses": 0, "pushes": 0, "history": []}


def save_tally(tally: dict):
    with open(_tally_path(), "w") as f:
        json.dump(tally, f, indent=2)

# ── Discord ───────────────────────────────────────────────────────────────────

def _send_discord(payload: dict):
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    if r.status_code not in (200, 204):
        print(f"  [WARN] Discord returned {r.status_code}: {r.text[:200]}")
    else:
        print("  Discord message sent.")


def _combined_odds(legs: list) -> float:
    p = 1.0
    for leg in legs:
        p *= leg["mid"]
    return round(p, 3)


def send_morning_discord(date: str, legs: list, tally: dict):
    combined = _combined_odds(legs)
    payout   = round(1 / combined, 2) if combined > 0 else 0
    wins     = tally["wins"]
    losses   = tally["losses"]
    total    = wins + losses
    pct      = f"{round(wins/total*100)}%" if total > 0 else "—"

    leg_lines = []
    for i, leg in enumerate(legs, 1):
        mid_pct = int(leg["mid"] * 100)
        leg_lines.append({
            "name":   f"Leg {i} — {leg['sport']}",
            "value":  f"**{leg['title']}**\nKalshi YES: **{mid_pct}¢**  |  Vol: {int(leg['volume']):,}",
            "inline": False,
        })

    payload = {
        "embeds": [{
            "title":       f"🎯  TODAY'S PARLAY  —  {date}",
            "description": (
                f"**{len(legs)}-leg parlay**  |  Combined odds: **{int(combined*100)}¢**  "
                f"→  pays **{payout}x**\n"
                f"📊 All-time record: **{wins}W – {losses}L** ({pct})"
            ),
            "color":       0x2ECC71,
            "fields":      leg_lines,
            "footer":      {"text": "Kalchi Parlay Engine • picks close at game time"},
        }]
    }
    _send_discord(payload)


def send_recap_discord(
    date: str,
    legs: list,
    results: list,
    parlay_hit: bool,
    tally: dict,
    clv_values: list | None = None,
    avg_clv: float | None = None,
):
    wins   = tally["wins"]
    losses = tally["losses"]
    total  = wins + losses
    pct    = f"{round(wins/total*100)}%" if total > 0 else "—"

    leg_lines = []
    for i, (leg, res) in enumerate(zip(legs, results)):
        icon = "✅" if res == "win" else ("❌" if res == "loss" else "⏳")
        clv = clv_values[i] if clv_values and i < len(clv_values) else None
        clv_str = f"  |  CLV: **{clv:+.0%}**" if clv is not None else ""
        leg_lines.append({
            "name":   f"{icon} {leg['sport']}",
            "value":  f"{leg['title']} → **{res.upper()}**  (picked @ {int(leg['mid']*100)}¢{clv_str})",
            "inline": False,
        })

    result_str = "✅ PARLAY HIT" if parlay_hit else "❌ PARLAY MISS"
    color      = 0x2ECC71 if parlay_hit else 0xE74C3C

    clv_line = ""
    if avg_clv is not None:
        clv_emoji = "📈" if avg_clv > 0 else ("📉" if avg_clv < 0 else "➡️")
        clv_line = f"\n{clv_emoji} Avg CLV: **{avg_clv:+.0%}** (closing vs morning price)"

    payload = {
        "embeds": [{
            "title":       f"📊  PARLAY RECAP  —  {date}",
            "description": (
                f"**{result_str}**\n"
                f"📈 All-time record: **{wins}W – {losses}L** ({pct})"
                f"{clv_line}"
            ),
            "color":       color,
            "fields":      leg_lines,
            "footer":      {"text": "Kalchi Parlay Engine  ·  CLV = closing line value (positive = market agreed with our pick)"},
        }]
    }
    _send_discord(payload)

# ── Morning Logic ─────────────────────────────────────────────────────────────

def run_morning():
    date = datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=-4))  # EDT
    ).strftime("%Y-%m-%d")
    print(f"[Morning] Date: {date}")

    print("Scanning markets...")
    candidates = scan_all_markets()
    print(f"  {len(candidates)} candidates found")

    legs = build_parlay(candidates, max_legs=3)
    if len(legs) < 3:
        print(f"Only {len(legs)} legs found — need 3 for a parlay.")
        _send_discord({"content": f"⚠️ **{date}** — Only found {len(legs)} qualifying legs (need 3 for a parlay). Markets may be too extreme or not yet open."})
        return

    # Save picks
    picks_data = {"date": date, "legs": legs}
    with open(_picks_path(date), "w") as f:
        json.dump(picks_data, f, indent=2)
    print(f"  Saved {len(legs)} legs to picks_{date}.json")

    # Emit predictions JSON for kalshi-safety to consume.
    try:
        from predictions_file import write_predictions_file
        out_path = write_predictions_file(date, legs)
        print(f"  [kalshi] Wrote predictions file: {out_path}")
    except Exception as e:
        print(f"  [kalshi] Failed to write predictions file: {e}")

    tally = load_tally()
    send_morning_discord(date, legs, tally)
    print("[Morning] Done.")

# ── Recap Logic ───────────────────────────────────────────────────────────────

def _check_result(ticker: str) -> str:
    """Returns 'win', 'loss', or 'pending'."""
    data = _get(f"/markets/{ticker}")
    m = data.get("market", data)
    status = m.get("status", "")
    result = m.get("result", "")
    if status in ("finalized", "settled") or result:
        return "win" if result == "yes" else "loss"
    return "pending"


def _get_closing_price(ticker: str) -> float | None:
    """Returns the closing YES price (0.0–1.0) for CLV computation.
    For settled markets uses the result (1.0/0.0).
    For still-open markets uses current mid as the best available closing estimate."""
    data = _get(f"/markets/{ticker}")
    m = data.get("market", data)
    result = m.get("result", "")
    status = m.get("status", "")
    if status in ("finalized", "settled") or result:
        return 1.0 if result == "yes" else 0.0
    bid = float(m.get("yes_bid_dollars") or 0)
    ask = float(m.get("yes_ask_dollars") or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 3)
    return None


def run_recap():
    date = datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=-4))
    ).strftime("%Y-%m-%d")
    print(f"[Recap] Date: {date}")

    picks_file = _picks_path(date)
    if not os.path.exists(picks_file):
        print(f"  No picks file found for {date} — skipping recap.")
        _send_discord({"content": f"⚠️ No parlay picks found for {date} to recap."})
        return

    with open(picks_file) as f:
        picks_data = json.load(f)
    legs = picks_data["legs"]

    print("Checking results...")
    results = []
    clv_values = []
    for leg in legs:
        res = _check_result(leg["ticker"])
        print(f"  {leg['title'][:50]} -> {res}")
        results.append(res)

        # CLV = closing price − morning pick price
        closing = _get_closing_price(leg["ticker"])
        if closing is not None:
            clv = round(closing - leg["mid"], 3)
            clv_values.append(clv)
            print(f"    CLV: morning={leg['mid']:.2f}  closing={closing:.2f}  clv={clv:+.3f}")
        else:
            clv_values.append(None)

    # Only count if all legs settled
    pending = results.count("pending")
    if pending > 0:
        print(f"  {pending} leg(s) still pending — sending partial recap.")

    wins_count  = results.count("win")
    parlay_hit  = (wins_count == len(legs)) and pending == 0

    # Aggregate CLV stats (only for settled legs with valid CLV)
    valid_clvs = [c for c in clv_values if c is not None]
    avg_clv = round(sum(valid_clvs) / len(valid_clvs), 3) if valid_clvs else None

    # Update tally
    tally = load_tally()
    if pending == 0:
        if parlay_hit:
            tally["wins"] += 1
        else:
            tally["losses"] += 1
        tally["history"].append({
            "date":    date,
            "result":  "win" if parlay_hit else "loss",
            "legs":    [
                {"title": l["title"], "result": r, "clv": c}
                for l, r, c in zip(legs, results, clv_values)
            ],
            "avg_clv": avg_clv,
        })
        save_tally(tally)

    send_recap_discord(date, legs, results, parlay_hit, tally, clv_values, avg_clv)
    print("[Recap] Done.")

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--morning", action="store_true")
    parser.add_argument("--recap",   action="store_true")
    args = parser.parse_args()

    if args.morning:
        run_morning()
    elif args.recap:
        run_recap()
    else:
        print("Usage: parlay.py --morning | --recap")
