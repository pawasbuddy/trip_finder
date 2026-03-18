#!/usr/bin/env python3
"""
Flight Price Alert Bot
Monitors one-way flight prices via Amadeus API and sends Discord alerts
when prices drop below a max threshold or fall by a configured percentage.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml
from amadeus import Client, ResponseError
from apscheduler.schedulers.blocking import BlockingScheduler

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
HISTORY_PATH = BASE_DIR / "price_history.json"


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Price history (baseline tracking) ────────────────────────────────────────

def load_history() -> dict:
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return {}


def save_history(history: dict) -> None:
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def route_key(origin: str, destination: str, travel_date: str) -> str:
    return f"{origin.upper()}-{destination.upper()}-{travel_date}"


# ── Amadeus flight search ─────────────────────────────────────────────────────

def get_cheapest_price(
    client: Client,
    origin: str,
    destination: str,
    travel_date: str,
    adults: int,
    currency: str,
) -> Optional[float]:
    """Return the cheapest one-way fare or None on failure."""
    try:
        response = client.shopping.flight_offers_search.get(
            originLocationCode=origin.upper(),
            destinationLocationCode=destination.upper(),
            departureDate=travel_date,
            adults=adults,
            currencyCode=currency,
            nonStop=False,
            max=5,
        )
        offers = response.data
        if not offers:
            log.warning("No offers returned for %s → %s on %s", origin, destination, travel_date)
            return None
        prices = [float(o["price"]["grandTotal"]) for o in offers]
        return min(prices)
    except ResponseError as e:
        log.error("Amadeus API error for %s → %s: %s", origin, destination, e)
        return None


# ── Discord notification ──────────────────────────────────────────────────────

def send_discord_alert(webhook_url: str, embeds: list[dict]) -> None:
    payload = {"embeds": embeds}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Discord alert sent (%d embed(s))", len(embeds))
    except requests.RequestException as e:
        log.error("Failed to send Discord alert: %s", e)


def build_embed(
    origin: str,
    destination: str,
    travel_date: str,
    currency: str,
    current_price: float,
    max_price: float,
    baseline_price: Optional[float],
    drop_pct: Optional[float],
    reasons: list[str],
) -> dict:
    lines = [f"**Route:** `{origin.upper()} → {destination.upper()}`"]
    lines.append(f"**Date:** {travel_date}")
    lines.append(f"**Price:** {currency} {current_price:,.2f}")
    if baseline_price is not None and drop_pct is not None:
        lines.append(f"**Prev. baseline:** {currency} {baseline_price:,.2f}  ↓ {drop_pct:.1f}%")
    lines.append(f"**Max threshold:** {currency} {max_price:,.2f}")
    lines.append(f"**Triggered by:** {', '.join(reasons)}")
    lines.append(f"**Checked at:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    return {
        "title": f"✈️  Cheap flight alert: {origin.upper()} → {destination.upper()}",
        "description": "\n".join(lines),
        "color": 0x00B4D8,  # sky blue
    }


# ── Core check logic ──────────────────────────────────────────────────────────

def check_routes(config: dict) -> None:
    log.info("Starting price check run …")

    amadeus = Client(
        client_id=config["amadeus"]["client_id"],
        client_secret=config["amadeus"]["client_secret"],
    )

    webhook_url: str = config["discord"]["webhook_url"]
    default_origin: str = config.get("origin", "")
    drop_threshold: float = float(config.get("alert_threshold_percent", 10))
    history = load_history()
    alert_embeds: list[dict] = []

    for route in config.get("routes", []):
        origin: str = route.get("origin", default_origin)
        destination: str = route["destination"]
        travel_date: str = route["travel_date"]
        max_price: float = float(route["max_price"])
        currency: str = route.get("currency", "USD")
        adults: int = int(route.get("adults", 1))

        log.info("Checking %s → %s on %s …", origin, destination, travel_date)

        current_price = get_cheapest_price(
            amadeus, origin, destination, travel_date, adults, currency
        )
        if current_price is None:
            continue

        key = route_key(origin, destination, travel_date)
        baseline_price: Optional[float] = history.get(key)
        reasons: list[str] = []
        drop_pct: Optional[float] = None

        # Trigger: price at or below max threshold
        if current_price <= max_price:
            reasons.append(f"price ≤ max ({currency} {max_price:,.0f})")

        # Trigger: price dropped by >= threshold % from baseline
        if baseline_price is not None:
            drop_pct = (baseline_price - current_price) / baseline_price * 100
            if drop_pct >= drop_threshold:
                reasons.append(f"dropped {drop_pct:.1f}% from baseline")

        if reasons:
            log.info(
                "ALERT for %s → %s: %s %.2f (%s)",
                origin, destination, currency, current_price, ", ".join(reasons),
            )
            alert_embeds.append(
                build_embed(
                    origin, destination, travel_date, currency,
                    current_price, max_price, baseline_price, drop_pct, reasons,
                )
            )
        else:
            log.info(
                "%s → %s: %s %.2f – no alert triggered",
                origin, destination, currency, current_price,
            )

        # Update baseline to cheapest price seen so far
        if baseline_price is None or current_price < baseline_price:
            history[key] = current_price

    save_history(history)

    if alert_embeds:
        # Discord allows max 10 embeds per message; chunk if needed
        for i in range(0, len(alert_embeds), 10):
            send_discord_alert(webhook_url, alert_embeds[i : i + 10])
    else:
        log.info("No alerts triggered this run.")

    log.info("Price check run complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()

    # Validate required fields
    if config["amadeus"]["client_id"].startswith("YOUR_"):
        sys.exit("ERROR: Set your Amadeus credentials in config.yaml before running.")
    if config["discord"]["webhook_url"].startswith("YOUR_"):
        sys.exit("ERROR: Set your Discord webhook URL in config.yaml before running.")

    interval_hours: int = int(config.get("schedule", {}).get("interval_hours", 6))

    log.info("Flight Price Alert Bot starting — checking every %d hour(s)", interval_hours)

    # Run immediately on startup, then on schedule
    check_routes(config)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        check_routes,
        "interval",
        args=[config],
        hours=interval_hours,
        id="price_check",
        name="Flight price check",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()
