#!/usr/bin/env python3
"""
Flight Price Alert Bot
Monitors one-way flight prices via Amadeus or Google Flights (SerpAPI) and
sends Discord alerts when prices drop below a max threshold or fall by a
configured percentage from the stored baseline.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

# ── Logging ───────────────────────────────────────────────────────────────────

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

PROVIDER_AMADEUS = "amadeus"
PROVIDER_GOOGLE = "google_flights"


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


# ── Provider: Amadeus ─────────────────────────────────────────────────────────

def get_cheapest_amadeus(
    config: dict,
    origin: str,
    destination: str,
    travel_date: str,
    adults: int,
    currency: str,
) -> Optional[float]:
    """Fetch cheapest one-way fare from Amadeus Flight Offers Search."""
    try:
        from amadeus import Client, ResponseError
    except ImportError:
        log.error("amadeus package not installed. Run: pip install amadeus")
        return None

    try:
        client = Client(
            client_id=config["amadeus"]["client_id"],
            client_secret=config["amadeus"]["client_secret"],
        )
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
            log.warning("Amadeus: no offers for %s → %s on %s", origin, destination, travel_date)
            return None
        return min(float(o["price"]["grandTotal"]) for o in offers)
    except ResponseError as e:
        log.error("Amadeus API error for %s → %s: %s", origin, destination, e)
        return None


# ── Provider: Google Flights (SerpAPI) ───────────────────────────────────────

def get_cheapest_google(
    config: dict,
    origin: str,
    destination: str,
    travel_date: str,
    adults: int,
    currency: str,
) -> Optional[float]:
    """Fetch cheapest one-way fare from Google Flights via SerpAPI."""
    api_key = config.get("serpapi", {}).get("api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        log.error("SerpAPI key not configured. Set serpapi.api_key in config.yaml.")
        return None

    params = {
        "engine": "google_flights",
        "departure_id": origin.upper(),
        "arrival_id": destination.upper(),
        "outbound_date": travel_date,
        "currency": currency,
        "adults": adults,
        "type": "2",          # 1 = round-trip, 2 = one-way
        "api_key": api_key,
    }

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        prices: list[float] = []
        for group in ("best_flights", "other_flights"):
            for flight in data.get(group, []):
                if "price" in flight:
                    prices.append(float(flight["price"]))

        if not prices:
            log.warning("Google Flights: no results for %s → %s on %s", origin, destination, travel_date)
            return None
        return min(prices)
    except requests.RequestException as e:
        log.error("SerpAPI request error for %s → %s: %s", origin, destination, e)
        return None


# ── Provider dispatcher ───────────────────────────────────────────────────────

def get_cheapest_price(
    config: dict,
    provider: str,
    origin: str,
    destination: str,
    travel_date: str,
    adults: int,
    currency: str,
) -> Optional[float]:
    if provider == PROVIDER_GOOGLE:
        return get_cheapest_google(config, origin, destination, travel_date, adults, currency)
    return get_cheapest_amadeus(config, origin, destination, travel_date, adults, currency)


# ── Discord notification ──────────────────────────────────────────────────────

def send_discord_alert(webhook_url: str, embeds: list[dict]) -> None:
    try:
        resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
        resp.raise_for_status()
        log.info("Discord alert sent (%d embed(s))", len(embeds))
    except requests.RequestException as e:
        log.error("Failed to send Discord alert: %s", e)


PROVIDER_LABELS = {
    PROVIDER_AMADEUS: "Amadeus",
    PROVIDER_GOOGLE: "Google Flights",
}

PROVIDER_COLORS = {
    PROVIDER_AMADEUS: 0x00B4D8,   # sky blue
    PROVIDER_GOOGLE: 0x4285F4,    # Google blue
}


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
    provider: str,
) -> dict:
    lines = [f"**Route:** `{origin.upper()} → {destination.upper()}`"]
    lines.append(f"**Date:** {travel_date}")
    lines.append(f"**Price:** {currency} {current_price:,.2f}")
    if baseline_price is not None and drop_pct is not None:
        lines.append(f"**Prev. baseline:** {currency} {baseline_price:,.2f}  ↓ {drop_pct:.1f}%")
    lines.append(f"**Max threshold:** {currency} {max_price:,.2f}")
    lines.append(f"**Triggered by:** {', '.join(reasons)}")
    lines.append(f"**Source:** {PROVIDER_LABELS.get(provider, provider)}")
    lines.append(f"**Checked at:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    return {
        "title": f"✈️  Cheap flight: {origin.upper()} → {destination.upper()}",
        "description": "\n".join(lines),
        "color": PROVIDER_COLORS.get(provider, 0x00B4D8),
    }


# ── Core check logic ──────────────────────────────────────────────────────────

def check_routes(config: dict) -> None:
    log.info("Starting price check run …")

    webhook_url: str = config["discord"]["webhook_url"]
    default_origin: str = config.get("origin", "")
    default_provider: str = config.get("provider", PROVIDER_AMADEUS)
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
        provider: str = route.get("provider", default_provider)

        log.info(
            "Checking %s → %s on %s via %s …",
            origin, destination, travel_date, PROVIDER_LABELS.get(provider, provider),
        )

        current_price = get_cheapest_price(
            config, provider, origin, destination, travel_date, adults, currency
        )
        if current_price is None:
            continue

        key = route_key(origin, destination, travel_date)
        baseline_price: Optional[float] = history.get(key)
        reasons: list[str] = []
        drop_pct: Optional[float] = None

        if current_price <= max_price:
            reasons.append(f"price ≤ max ({currency} {max_price:,.0f})")

        if baseline_price is not None:
            drop_pct = (baseline_price - current_price) / baseline_price * 100
            if drop_pct >= drop_threshold:
                reasons.append(f"dropped {drop_pct:.1f}% from baseline")

        if reasons:
            log.info(
                "ALERT %s → %s: %s %.2f (%s)",
                origin, destination, currency, current_price, ", ".join(reasons),
            )
            alert_embeds.append(
                build_embed(
                    origin, destination, travel_date, currency,
                    current_price, max_price, baseline_price, drop_pct, reasons, provider,
                )
            )
        else:
            log.info(
                "%s → %s: %s %.2f — no alert",
                origin, destination, currency, current_price,
            )

        if baseline_price is None or current_price < baseline_price:
            history[key] = current_price

    save_history(history)

    if alert_embeds:
        for i in range(0, len(alert_embeds), 10):
            send_discord_alert(webhook_url, alert_embeds[i : i + 10])
    else:
        log.info("No alerts triggered this run.")

    log.info("Price check run complete.")


# ── Startup validation ────────────────────────────────────────────────────────

def validate_config(config: dict) -> None:
    if config["discord"]["webhook_url"].startswith("YOUR_"):
        sys.exit("ERROR: Set your Discord webhook URL in config.yaml.")

    provider = config.get("provider", PROVIDER_AMADEUS)
    if provider == PROVIDER_AMADEUS:
        if config.get("amadeus", {}).get("client_id", "").startswith("YOUR_"):
            sys.exit("ERROR: Set your Amadeus credentials in config.yaml.")
    elif provider == PROVIDER_GOOGLE:
        if config.get("serpapi", {}).get("api_key", "").startswith("YOUR_"):
            sys.exit("ERROR: Set your SerpAPI key in config.yaml.")

    # Also check per-route providers
    for route in config.get("routes", []):
        rp = route.get("provider", provider)
        if rp == PROVIDER_AMADEUS and config.get("amadeus", {}).get("client_id", "").startswith("YOUR_"):
            sys.exit(f"ERROR: Route {route['destination']} uses Amadeus but credentials are not set.")
        if rp == PROVIDER_GOOGLE and config.get("serpapi", {}).get("api_key", "").startswith("YOUR_"):
            sys.exit(f"ERROR: Route {route['destination']} uses Google Flights but SerpAPI key is not set.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    validate_config(config)

    interval_hours: int = int(config.get("schedule", {}).get("interval_hours", 6))
    provider = config.get("provider", PROVIDER_AMADEUS)
    log.info(
        "Flight Price Alert Bot starting — provider: %s, interval: %dh",
        PROVIDER_LABELS.get(provider, provider), interval_hours,
    )

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
