# Flight Price Alert Bot

Monitors one-way flight prices and sends Discord alerts when cheap fares appear.

## Features

- Checks prices for any number of routes you configure
- Alerts when price is **at or below your max price** threshold
- Alerts when price **drops by X%** from the previously seen baseline
- Sends rich embedded messages to a **Discord channel**
- Supports **Google Flights (via SerpAPI)** and **Amadeus** as data sources
- Mix providers per-route — e.g. Google Flights for most routes, Amadeus for others
- Runs on a configurable schedule (default: every 6 hours)
- Tracks price history locally in `price_history.json`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Choose a flight data provider

#### Option A — Google Flights via SerpAPI (recommended)

Best coverage including budget airlines (Ryanair, EasyJet, etc.).

1. Sign up at <https://serpapi.com> — free tier gives 100 searches/month
2. Copy your **API Key** from the dashboard
3. Set `provider: google_flights` and fill in `serpapi.api_key` in `config.yaml`

#### Option B — Amadeus API

GDS-sourced fares from major airlines. No budget carriers.

1. Sign up at <https://developers.amadeus.com>
2. Create an app to get a **Client ID** and **Client Secret**
3. Set `provider: amadeus` and fill in `amadeus` credentials in `config.yaml`

> You can mix providers per-route — set a global `provider` and override it with `provider:` on individual routes.

### 3. Create a Discord webhook

1. In Discord, open **Server Settings → Integrations → Webhooks**
2. Click **New Webhook**, choose a channel, copy the URL

### 4. Configure `config.yaml`

```yaml
provider: google_flights      # or: amadeus

serpapi:
  api_key: your_serpapi_key   # needed if using google_flights

amadeus:                       # needed if using amadeus
  client_id: abc123...
  client_secret: xyz789...

discord:
  webhook_url: https://discord.com/api/webhooks/...

origin: JFK                   # default departure airport (IATA)
alert_threshold_percent: 10   # alert if price drops ≥ 10% from baseline

schedule:
  interval_hours: 6

routes:
  - destination: LHR
    max_price: 400
    currency: USD
    travel_date: "2026-06-01"
    adults: 1

  - destination: NRT
    max_price: 700
    currency: USD
    travel_date: "2026-07-15"
    adults: 1

  # Override provider for a specific route:
  - destination: DXB
    max_price: 600
    currency: USD
    travel_date: "2026-08-10"
    provider: amadeus
```

**Route fields:**

| Field | Required | Description |
|---|---|---|
| `destination` | Yes | IATA airport code (e.g. `LHR`, `NRT`) |
| `max_price` | Yes | Alert if price ≤ this value |
| `currency` | No | Currency code (default: `USD`) |
| `travel_date` | Yes | `YYYY-MM-DD` departure date |
| `adults` | No | Number of passengers (default: `1`) |
| `origin` | No | Override the default origin for this route |
| `provider` | No | `google_flights` or `amadeus` — overrides global provider |

### 5. Run the bot

```bash
python bot.py
```

The bot checks prices immediately on startup, then repeats every N hours.

To run it continuously in the background:

```bash
nohup python bot.py > bot.log 2>&1 &
```

Or use a cron job (runs every 6 hours, logging to a file):

```cron
0 */6 * * * cd /path/to/trip_finder && python bot.py --once >> bot.log 2>&1
```

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot script |
| `config.yaml` | Your configuration (edit this) |
| `requirements.txt` | Python dependencies |
| `price_history.json` | Auto-created; stores price baselines |
