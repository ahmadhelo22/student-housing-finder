# Property Scraper Bot

A Telegram bot (with an optional Flask HTTP endpoint) that finds rental rooms and units in Malaysia.
The user describes what they want in free text; Claude extracts the search filters, and the scraper
searches PropertyGuru, iProperty, SPEEDHOME and Mudah in parallel, merges the results, removes
duplicates, and returns the cheapest listings first.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot: listing cards, "Show more" details, "Show next" pagination |
| `flask_app.py` | HTTP API (`POST /chat`, `POST /reset`) using the same logic as the bot |
| `core.py` | Shared logic: LLM field extraction, session merging, scraper calls, reply formatting |
| `search_pipeline.py` | Single scraper entry point: `search(SEARCH_INPUT)` |
| `Scrapper_2.py` | Per-platform scrapers, detail pages, cross-portal merge and deduplication |
| `location_filter.py` | Optional distance filter around a lat/lng point |
| `stress_test.py` | Load test against the live sites |
| `stress_test_offline.py` | Stress test against local mock sites (no network) |
| `SCRAPER_INPUT_SPEC.md` | Scraper input/output spec |

All modules sit in the project root; `core.py` imports `search_pipeline` from its own folder.

## Installation

Requires Python 3.10+ (developed on 3.14). There is no `requirements.txt`; install the third-party packages directly:

```bash
python3 -m venv .venv
.venv/bin/pip install anthropic python-dotenv pyTelegramBotAPI flask curl_cffi selectolax
```

## Configuration

Copy `.env.example` to `.env` and fill in:

- `TELEGRAM_BOT_TOKEN` – Telegram bot token (required by `bot.py`)
- `ANTHROPIC_API_KEY` – used by `core.py` to extract search filters from user messages

## Running

```bash
.venv/bin/python bot.py                       # Telegram bot (long polling)
.venv/bin/python flask_app.py                 # Flask dev server on http://127.0.0.1:5000
.venv/bin/python search_pipeline.py           # one example search, prints the results
```

Flask example:

```bash
curl -X POST http://127.0.0.1:5000/chat -H "Content-Type: application/json" \
  -d '{"session_id": "demo", "message": "studio in Cheras under RM1500"}'
```

The Flask app has no authentication and runs with `debug=True`; keep it local or put it behind auth before exposing it.

## Tests

```bash
.venv/bin/python stress_test_offline.py [ramp|bigdata|faults|fuzz|all]   # offline, mock sites
.venv/bin/python stress_test.py                                         # hits the real sites
```

`all` runs a ramp up to 3200 concurrent searches and takes a long time; `fuzz` is the quickest.

## Scraper input

Searches take a `SEARCH_INPUT` dict: `area` and `max_price` are required, plus optional filters
(housing type, price range, bedrooms, floor level, furnishing, MRT distance, and so on) and an optional
`location` point with `radius_km`. `core.py` maps the bot's collected fields to this format
(`to_scraper_input`). See [SCRAPER_INPUT_SPEC.md](SCRAPER_INPUT_SPEC.md) for every field, the
accepted aliases, and the output formats.
