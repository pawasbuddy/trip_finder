#!/usr/bin/env python3
"""
Flight Price Alert Bot
Monitors one-way flight prices via Amadeus or Google Flights (SerpAPI) and
sends Discord alerts when prices drop below a max threshold or fall by a
configured percentage from the stored baseline.

Slash commands (requires bot_token in config.yaml):
  /route                  – manage routes in natural language
  /list_routes            – show all monitored routes
  /add_route              – add a new route
  /remove_route           – remove a route
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; use env vars directly

import anthropic
from pydantic import BaseModel
import discord
from discord import app_commands
from discord.ext import tasks
import requests
import yaml

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

PROVIDER_LABELS = {
    PROVIDER_AMADEUS: "Amadeus",
    PROVIDER_GOOGLE: "Google Flights",
}

PROVIDER_COLORS = {
    PROVIDER_AMADEUS: 0x00B4D8,
    PROVIDER_GOOGLE: 0x4285F4,
}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    # Allow env vars to override config file values
    if os.environ.get("SERPAPI_API_KEY"):
        config.setdefault("serpapi", {})["api_key"] = os.environ["SERPAPI_API_KEY"]
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        config.setdefault("discord", {})["webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
    if os.environ.get("DISCORD_BOT_TOKEN"):
        config.setdefault("discord", {})["bot_token"] = os.environ["DISCORD_BOT_TOKEN"]
    if os.environ.get("AMADEUS_CLIENT_ID"):
        config.setdefault("amadeus", {})["client_id"] = os.environ["AMADEUS_CLIENT_ID"]
    if os.environ.get("AMADEUS_CLIENT_SECRET"):
        config.setdefault("amadeus", {})["client_secret"] = os.environ["AMADEUS_CLIENT_SECRET"]
    return config


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


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
        "type": "2",
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

def check_routes() -> None:
    config = load_config()
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

    for route in config.get("routes", []):
        rp = route.get("provider", provider)
        if rp == PROVIDER_AMADEUS and config.get("amadeus", {}).get("client_id", "").startswith("YOUR_"):
            sys.exit(f"ERROR: Route {route['destination']} uses Amadeus but credentials are not set.")
        if rp == PROVIDER_GOOGLE and config.get("serpapi", {}).get("api_key", "").startswith("YOUR_"):
            sys.exit(f"ERROR: Route {route['destination']} uses Google Flights but SerpAPI key is not set.")


# ── Discord bot + slash commands ──────────────────────────────────────────────

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready() -> None:
    await tree.sync()
    config = load_config()
    interval_hours: int = int(config.get("schedule", {}).get("interval_hours", 6))
    price_check_loop.change_interval(hours=interval_hours)
    price_check_loop.start()
    log.info("Logged in as %s — slash commands synced, price check every %dh", bot.user, interval_hours)


@tasks.loop(hours=6)
async def price_check_loop() -> None:
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, check_routes)


# ── Natural language route model ─────────────────────────────────────────────

class RouteAction(BaseModel):
    action: Literal["add", "remove", "list"]
    destination: Optional[str] = None      # IATA code e.g. LHR
    origin: Optional[str] = None           # IATA code, overrides default
    max_price: Optional[float] = None
    travel_date: Optional[str] = None      # YYYY-MM-DD
    currency: Optional[str] = None
    adults: Optional[int] = None
    error: Optional[str] = None            # set if Claude couldn't parse


def parse_route_request(text: str, default_origin: str) -> RouteAction:
    """Use Claude to parse a natural language route management request."""
    ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    response = ai.messages.parse(
        model="claude-opus-4-6",
        max_tokens=512,
        system=(
            f"You parse flight route management requests into structured data.\n"
            f"Today's date is {datetime.utcnow().strftime('%Y-%m-%d')}.\n"
            f"The default origin airport is {default_origin}.\n"
            "Use IATA airport codes (3 letters). "
            "If the user mentions a city, convert it to the main IATA code (e.g. London→LHR, Tokyo→NRT, Paris→CDG, NYC→JFK). "
            "Dates should be YYYY-MM-DD. "
            "If you cannot determine a required field, set error to explain what's missing."
        ),
        messages=[{"role": "user", "content": text}],
        output_format=RouteAction,
    )
    return response.parsed_output


@tree.command(name="route", description="Manage routes in natural language")
@app_commands.describe(request='e.g. "add JFK to London in June under $400" or "remove Tokyo" or "show routes"')
async def route_natural(interaction: discord.Interaction, request: str) -> None:
    await interaction.response.defer(ephemeral=True)
    config = load_config()
    default_origin = config.get("origin", "JFK")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key or anthropic_key.startswith("your_"):
        await interaction.followup.send(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file.", ephemeral=True
        )
        return

    try:
        action = parse_route_request(request, default_origin)
    except Exception as e:
        await interaction.followup.send(f"Could not parse request: {e}", ephemeral=True)
        return

    if action.error:
        await interaction.followup.send(f"Could not understand request: {action.error}", ephemeral=True)
        return

    if action.action == "list":
        routes = config.get("routes", [])
        if not routes:
            await interaction.followup.send("No routes configured.", ephemeral=True)
            return
        lines = []
        for i, r in enumerate(routes, 1):
            origin = r.get("origin", default_origin)
            lines.append(
                f"**{i}.** `{origin} → {r['destination']}`  |  "
                f"{r.get('currency','USD')} {r['max_price']}  |  "
                f"{r['travel_date']}  |  {r.get('adults',1)} adult(s)"
            )
        embed = discord.Embed(title="Monitored Routes", description="\n".join(lines), color=0x4285F4)
        await interaction.followup.send(embed=embed, ephemeral=True)

    elif action.action == "add":
        missing = [f for f in ("destination", "max_price", "travel_date") if not getattr(action, f)]
        if missing:
            await interaction.followup.send(
                f"Missing details: {', '.join(missing)}. Please include them in your request.",
                ephemeral=True,
            )
            return
        new_route: dict = {
            "destination": action.destination.upper(),
            "max_price": action.max_price,
            "travel_date": action.travel_date,
            "currency": (action.currency or "USD").upper(),
            "adults": action.adults or 1,
        }
        if action.origin:
            new_route["origin"] = action.origin.upper()
        config.setdefault("routes", []).append(new_route)
        save_config(config)
        effective_origin = (action.origin or default_origin).upper()
        log.info("Route added via /route: %s → %s on %s", effective_origin, action.destination.upper(), action.travel_date)
        await interaction.followup.send(
            f"Added route `{effective_origin} → {action.destination.upper()}` on {action.travel_date} "
            f"(max {new_route['currency']} {action.max_price}).",
            ephemeral=True,
        )

    elif action.action == "remove":
        if not action.destination:
            await interaction.followup.send("Please specify which route to remove.", ephemeral=True)
            return
        routes = config.get("routes", [])
        updated = [
            r for r in routes
            if not (
                r["destination"].upper() == action.destination.upper()
                and (not action.travel_date or r["travel_date"] == action.travel_date)
            )
        ]
        if len(updated) == len(routes):
            await interaction.followup.send(
                f"No route found for `{action.destination.upper()}`.", ephemeral=True
            )
            return
        config["routes"] = updated
        save_config(config)
        log.info("Route removed via /route: %s", action.destination.upper())
        await interaction.followup.send(
            f"Removed route `{action.destination.upper()}`.", ephemeral=True
        )


@tree.command(name="list_routes", description="Show all monitored flight routes")
async def list_routes(interaction: discord.Interaction) -> None:
    config = load_config()
    routes = config.get("routes", [])
    default_origin = config.get("origin", "?")

    if not routes:
        await interaction.response.send_message("No routes configured.", ephemeral=True)
        return

    lines = []
    for i, r in enumerate(routes, 1):
        origin = r.get("origin", default_origin)
        lines.append(
            f"**{i}.** `{origin} → {r['destination']}`  |  "
            f"{r.get('currency','USD')} {r['max_price']}  |  "
            f"{r['travel_date']}  |  "
            f"{r.get('adults',1)} adult(s)"
        )

    embed = discord.Embed(
        title="Monitored Routes",
        description="\n".join(lines),
        color=0x4285F4,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="add_route", description="Add a flight route to monitor")
@app_commands.describe(
    destination="Destination airport IATA code (e.g. LHR)",
    max_price="Alert if price drops to or below this amount",
    travel_date="Departure date (YYYY-MM-DD)",
    origin="Origin airport IATA code — overrides default",
    currency="Currency code (default: USD)",
    adults="Number of adult passengers (default: 1)",
)
async def add_route(
    interaction: discord.Interaction,
    destination: str,
    max_price: float,
    travel_date: str,
    origin: str = "",
    currency: str = "USD",
    adults: int = 1,
) -> None:
    config = load_config()
    new_route: dict = {
        "destination": destination.upper(),
        "max_price": max_price,
        "travel_date": travel_date,
        "currency": currency.upper(),
        "adults": adults,
    }
    if origin:
        new_route["origin"] = origin.upper()

    config.setdefault("routes", []).append(new_route)
    save_config(config)

    effective_origin = origin.upper() if origin else config.get("origin", "?")
    log.info("Route added via slash command: %s → %s on %s", effective_origin, destination.upper(), travel_date)
    await interaction.response.send_message(
        f"Added route `{effective_origin} → {destination.upper()}` on {travel_date} "
        f"(max {currency.upper()} {max_price}).",
        ephemeral=True,
    )


@tree.command(name="remove_route", description="Remove a monitored flight route")
@app_commands.describe(
    destination="Destination airport IATA code (e.g. LHR)",
    travel_date="Departure date (YYYY-MM-DD)",
)
async def remove_route(
    interaction: discord.Interaction,
    destination: str,
    travel_date: str,
) -> None:
    config = load_config()
    routes = config.get("routes", [])
    updated = [
        r for r in routes
        if not (r["destination"].upper() == destination.upper() and r["travel_date"] == travel_date)
    ]

    if len(updated) == len(routes):
        await interaction.response.send_message(
            f"No route found for `{destination.upper()}` on {travel_date}.",
            ephemeral=True,
        )
        return

    config["routes"] = updated
    save_config(config)
    log.info("Route removed via slash command: %s on %s", destination.upper(), travel_date)
    await interaction.response.send_message(
        f"Removed route `{destination.upper()}` on {travel_date}.",
        ephemeral=True,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    validate_config(config)

    bot_token: str = (
        os.environ.get("DISCORD_BOT_TOKEN")
        or config.get("discord", {}).get("bot_token", "")
    )
    if not bot_token or bot_token.startswith("YOUR_"):
        # Fall back to webhook-only mode with APScheduler
        log.warning("No bot_token set — running in webhook-only mode (no slash commands).")
        from apscheduler.schedulers.blocking import BlockingScheduler

        interval_hours: int = int(config.get("schedule", {}).get("interval_hours", 6))
        provider = config.get("provider", PROVIDER_AMADEUS)
        log.info(
            "Flight Price Alert Bot starting — provider: %s, interval: %dh",
            PROVIDER_LABELS.get(provider, provider), interval_hours,
        )

        check_routes()

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            check_routes,
            "interval",
            hours=interval_hours,
            id="price_check",
            name="Flight price check",
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Bot stopped.")
    else:
        provider = config.get("provider", PROVIDER_AMADEUS)
        log.info(
            "Flight Price Alert Bot starting — provider: %s, slash commands enabled",
            PROVIDER_LABELS.get(provider, provider),
        )
        bot.run(bot_token)


if __name__ == "__main__":
    main()
