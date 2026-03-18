# Flight Price Alert Bot

Monitors one-way flight prices and sends Discord alerts when cheap fares appear.

## Features

- Checks prices for any number of routes you configure
- Alerts when price is **at or below your max price** threshold
- Alerts when price **drops by X%** from the previously seen baseline
- Sends rich embedded messages to a **Discord channel**
- Runs on a configurable schedule (default: every 6 hours)
- Tracks price history locally in `price_history.json`

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get an Amadeus API key (free)

1. Sign up at <https://developers.amadeus.com>
2. Create an app — you'll get a **Client ID** and **Client Secret**
3. The free sandbox tier covers all the searches this bot needs

### 3. Create a Discord webhook

1. In Discord, open **Server Settings → Integrations → Webhooks**
2. Click **New Webhook**, choose a channel, copy the URL

### 4. Configure `config.yaml`

Edit `config.yaml` with your credentials and routes:

```yaml
amadeus:
  client_id: abc123...
  client_secret: xyz789...

discord:
  webhook_url: https://discord.com/api/webhooks/...

origin: JFK                  # Your default departure airport (IATA code)
alert_threshold_percent: 10  # Alert if price drops ≥ 10% from baseline

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
