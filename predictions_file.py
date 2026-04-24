"""Writes Parlay Pick predictions to predictions/YYYY-MM-DD.json.

The kalshi-safety service fetches this file via GitHub raw URL to
decide which picks to back on Kalshi. This module only emits the
JSON — it does not place any bets.

Parlay Pick generates a single composite daily parlay (usually 3 legs
across different sports). We emit the parlay as one pick whose modelProb
is the combined leg probability (product of leg mids). The per-leg
details live in the `extra` field so downstream consumers can still
inspect the individual legs.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PREDICTIONS_DIR = os.path.join(HERE, "predictions")

MIN_PROB = float(os.environ.get("KALSHI_MIN_PROB", "0.58"))


def _combined_prob(legs: list) -> float:
    p = 1.0
    for leg in legs:
        p *= float(leg.get("mid", 0.0))
    return p


def write_predictions_file(date: str, legs: list) -> str:
    """Write predictions/<date>.json in the kalshi-safety schema.

    `date` is expected in YYYY-MM-DD form.
    `legs` is the list produced by `build_parlay`, each a dict with
    at minimum: ticker, title, sport, mid, event.
    """
    os.makedirs(PREDICTIONS_DIR, exist_ok=True)
    out_path = os.path.join(PREDICTIONS_DIR, f"{date}.json")

    picks: list[dict] = []
    combined = _combined_prob(legs) if legs else 0.0

    if legs and combined >= MIN_PROB:
        first = legs[0]
        picks.append({
            "gameId": f"parlay-{date}",
            "home": first.get("sport", "PARLAY"),
            "away": "FIELD",
            "pickedTeam": "PARLAY_YES",
            "pickedSide": "home",
            "modelProb": round(combined, 4),
            "extra": {
                "legs": [
                    {
                        "ticker": leg.get("ticker"),
                        "title": leg.get("title"),
                        "sport": leg.get("sport"),
                        "event": leg.get("event"),
                        "mid": leg.get("mid"),
                        "bid": leg.get("bid"),
                        "ask": leg.get("ask"),
                        "volume": leg.get("volume"),
                    }
                    for leg in legs
                ],
                "combinedOdds": round(combined, 4),
            },
        })

    payload = {
        "sport": "PARLAY",
        "date": date,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "picks": picks,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return out_path
