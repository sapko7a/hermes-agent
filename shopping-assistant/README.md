# Hermes OzBargain Amazon Cart Assistant

Automated deal-watching system that monitors OzBargain for household essentials and adds qualifying items to your Amazon AU cart.

## Architecture

**Execution model:** Hermes cron job with helper script (`--script` flag).

- `check_deals.py` runs first (Python script), fetches the OzBargain RSS feed, matches against the watchlist, manages deduplication state, and outputs qualifying deals.
- The Hermes cron agent receives the script output as context. If deals are found, it uses browser automation to search Amazon AU, verify matches, and add items to cart.
- Results are delivered to the configured WhatsApp group.

This split was chosen because:
1. OzBargain scraping is simple HTTP (no browser needed) - more reliable as a script
2. Amazon cart operations require authenticated browser sessions - handled by Hermes browser tools
3. State management (seen_deals.json) is more reliable in deterministic Python than LLM-driven file operations

## Files

| File | Purpose |
|------|---------|
| `check_deals.py` | OzBargain RSS feed scraper + state manager |
| `watchlist.json` | Items to watch with price thresholds |
| `config.json` | Runtime settings (dry_run, price guard, etc.) |
| `seen_deals.json` | Deduplication state (auto-managed) |
| `logs/` | Per-run log files from the scraper |
| `README.md` | This file |

## Configuration

### config.json

```json
{
  "dry_run": true,
  "max_results_per_item": 5,
  "duplicate_window_hours": 24,
  "amazon_price_guard_pct": 25
}
```

- **dry_run**: When `true`, the agent reports what it WOULD do but does not click Add to Cart
- **max_results_per_item**: Not currently used (RSS feed is global, not per-item search)
- **duplicate_window_hours**: How long to suppress re-alerting on the same deal URL
- **amazon_price_guard_pct**: Skip if Amazon price is this % above the average price paid

### watchlist.json

Array of items with fields:
- `item`: Human-readable name
- `search_query`: Keywords matched against OzBargain deal titles (60% keyword match threshold)
- `avg_price_paid`: Your typical price for this item
- `notify_below_price`: Only trigger if OzBargain price is below this
- `amazon_search`: Search query used on Amazon AU

To add/remove items, edit this file directly. Changes take effect on the next run.

## Cron Job

- **Name:** OzBargain Amazon Cart Assistant
- **Job ID:** `64005b253453`
- **Schedule:** Every 60 minutes
- **Delivery:** `whatsapp:120363407818696650@g.us` (WhatsApp group)
- **Script:** `/home/adminuser/hermes-shopping-assistant/check_deals.py`

### Common cron commands

```bash
# List jobs
hermes cron list

# Trigger a manual run
hermes cron run 64005b253453

# Pause the job
hermes cron pause 64005b253453

# Resume the job
hermes cron resume 64005b253453

# Edit schedule (e.g., every 2 hours)
hermes cron edit 64005b253453 --schedule "every 2h"

# Check cron status
hermes cron status
```

## Delivery

Messages are sent to WhatsApp group `120363407818696650@g.us` via the Hermes gateway.

The gateway runs as a systemd user service (`hermes-gateway.service`) with:
- Auto-restart on failure
- Lingering enabled (survives logout)
- Starts on boot

## Switching dry_run on/off

Edit `config.json`:

```bash
# Enable live add-to-cart
nano ~/hermes-shopping-assistant/config.json
# Change "dry_run": true to "dry_run": false
```

No restart needed - the script reads the config fresh each run.

## Amazon Login (One-Time Manual Step)

Before switching to `dry_run: false`, you must log into Amazon AU in the Hermes browser:

1. Start a Hermes chat session: `hermes`
2. Ask Hermes to open a browser: "Open https://www.amazon.com.au and let me log in"
3. Log into your Amazon account through the browser
4. The session/cookies will persist for future cron runs

If Amazon login expires, repeat this process.

## Inspecting Logs

```bash
# Latest scraper log
cat $(ls -t ~/hermes-shopping-assistant/logs/check_*.log | head -1)

# Cron job outputs
ls ~/.hermes/cron/output/64005b253453/
cat ~/.hermes/cron/output/64005b253453/<filename>.md

# Gateway logs
journalctl --user -u hermes-gateway --since "1 hour ago" --no-pager

# Seen deals state
cat ~/hermes-shopping-assistant/seen_deals.json
```

## Recovery

If the gateway stops:

```bash
# Check status
systemctl --user status hermes-gateway

# Restart
systemctl --user restart hermes-gateway

# Check cron still works
hermes cron status
hermes cron list
```

## OzBargain Scraping Approach

The script uses the OzBargain RSS feed (`/deals/feed`) rather than the search API because:
- The search endpoints (`/deals?q=` and `/search/node/`) return 403 from server IPs
- RSS feeds are designed for automated consumption
- Single HTTP request per cycle (efficient)
- Trade-off: only monitors the latest ~30 deals per cycle, not historical search

With hourly runs, this captures deals reliably since OzBargain's feed refreshes frequently.

## Backups

Config backups are stored in `~/.hermes-backups/` with timestamped directories.
