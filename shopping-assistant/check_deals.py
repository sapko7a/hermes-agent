#!/usr/bin/env python3
"""
OzBargain Deal Checker for Hermes Shopping Assistant.

Combined watchlist + hot deal mode in a single run.
Outputs WhatsApp-formatted alerts to stdout for Hermes cron delivery.
Produces no output (empty stdout) when there are no qualifying deals,
so the Hermes agent can respond with [SILENT].
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.json")
SEEN_DEALS_PATH = os.path.join(SCRIPT_DIR, "seen_deals.json")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "check_deals.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

FEED_URL = "https://www.ozbargain.com.au/deals/feed"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"}
OZB_NS = {"ozb": "https://www.ozbargain.com.au"}

AMAZON_SEARCH_URL = "https://www.amazon.com.au/s?k={query}"

# ── Helpers ──────────────────────────────────────────────────────────────

def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default if default is not None else {}


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def parse_price(text: str) -> Optional[float]:
    m = re.search(r"\$\s*([\d]+(?:,\d{3})*\.?\d*)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def keywords_match(title_lower: str, search_query: str) -> bool:
    if not search_query.strip():
        return True
    keywords = search_query.lower().split()
    matches = sum(1 for kw in keywords if kw in title_lower)
    threshold = max(2, int(len(keywords) * 0.6)) if len(keywords) > 1 else 1
    return matches >= threshold


def amazon_search_url(query: str) -> str:
    return AMAZON_SEARCH_URL.format(query=quote_plus(query))


# ── Dedup state ──────────────────────────────────────────────────────────

def load_seen(max_age_days: int = 30, max_entries: int = 5000) -> Dict[str, float]:
    seen = load_json(SEEN_DEALS_PATH, {})
    cutoff = time.time() - (max_age_days * 86400)
    pruned = {url: ts for url, ts in seen.items() if ts > cutoff}
    if len(pruned) > max_entries:
        sorted_items = sorted(pruned.items(), key=lambda x: x[1], reverse=True)
        pruned = dict(sorted_items[:max_entries])
    if len(pruned) != len(seen):
        log.info("Pruned seen_deals: %d -> %d entries", len(seen), len(pruned))
    return pruned


# ── RSS fetch ────────────────────────────────────────────────────────────

def fetch_ozbargain_feed() -> List[Dict[str, Any]]:
    deals = []
    resp = requests.get(FEED_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is None or link_el is None:
            continue
        title = title_el.text or ""
        link = link_el.text or ""
        votes = 0
        meta_el = item.find("ozb:meta", OZB_NS)
        if meta_el is not None:
            try:
                votes = int(meta_el.get("votes-pos", 0))
            except (ValueError, TypeError):
                pass
        deals.append({
            "title": title,
            "url": link,
            "price": parse_price(title),
            "votes": votes,
        })
    return deals


# ── Matching ─────────────────────────────────────────────────────────────

def match_watchlist(deals: List[Dict], watchlist: List[Dict], seen: Dict[str, float]) -> List[Dict]:
    matches = []
    for item in watchlist:
        for deal in deals:
            if deal["url"] in seen:
                continue
            if not keywords_match(deal["title"].lower(), item["search_query"]):
                continue
            if deal["price"] is None or deal["price"] > item["notify_below_price"]:
                continue
            matches.append({
                "type": "watchlist",
                "watchlist_item": item["item"],
                "title": deal["title"],
                "url": deal["url"],
                "price": deal["price"],
                "votes": deal["votes"],
                "amazon_url": amazon_search_url(item["amazon_search"]),
                "avg_price": item["avg_price_paid"],
                "threshold": item["notify_below_price"],
            })
            seen[deal["url"]] = time.time()
    return matches


def match_hot_deals(deals: List[Dict], min_votes: int, seen: Dict[str, float]) -> List[Dict]:
    matches = []
    for deal in deals:
        if deal["url"] in seen:
            continue
        if deal["votes"] < min_votes:
            continue
        matches.append({
            "type": "hot",
            "watchlist_item": f"Hot Deal ({deal['votes']} votes)",
            "title": deal["title"],
            "url": deal["url"],
            "price": deal["price"],
            "votes": deal["votes"],
        })
        seen[deal["url"]] = time.time()
    return matches


# ── Formatting ───────────────────────────────────────────────────────────

SEPARATOR = "───────────────"


def format_watchlist_deal(m: Dict, idx: int, total: int) -> str:
    saving = m["avg_price"] - m["price"]
    saving_pct = (saving / m["avg_price"]) * 100

    lines = [
        f"*{idx}/{total} — {m['watchlist_item']}*",
        "",
        f"  {m['title']}",
        f"  {m['url']}",
        f"  *${m['price']:.2f}* (you usually pay ${m['avg_price']:.2f} — save ${saving:.2f} / {saving_pct:.0f}%)",
        f"  {m['votes']} votes",
        "",
        f"  Amazon: {m['amazon_url']}",
        "",
        SEPARATOR,
        "",
    ]
    return "\n".join(lines)


def format_hot_deal(m: Dict, idx: int, total: int) -> str:
    lines = [
        f"*{idx}/{total} — {m['votes']} votes*",
        "",
        f"  {m['title']}",
        f"  {m['url']}",
    ]
    if m["price"]:
        lines.append(f"  *${m['price']:.2f}*")
    lines.extend(["", SEPARATOR, ""])
    return "\n".join(lines)


def format_output(watchlist_matches: List[Dict], hot_matches: List[Dict]) -> str:
    now = datetime.now().strftime("%a %d %b, %I:%M %p")
    parts = []

    if watchlist_matches:
        parts.append(f"*OzBargain Watchlist Alert*  {now}")
        parts.append(SEPARATOR)
        parts.append("")
        for i, m in enumerate(watchlist_matches, 1):
            parts.append(format_watchlist_deal(m, i, len(watchlist_matches)))

    if hot_matches:
        if watchlist_matches:
            parts.append("")
        parts.append(f"*Hot Deals*  {now}")
        parts.append(SEPARATOR)
        parts.append("")
        for i, m in enumerate(hot_matches, 1):
            parts.append(format_hot_deal(m, i, len(hot_matches)))

    return "\n".join(parts).strip()


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="OzBargain deal checker")
    parser.add_argument("--min-votes", type=int, default=15,
                        help="Minimum votes for hot deal alerts (default: 15)")
    parser.add_argument("--watchlist-only", action="store_true",
                        help="Skip hot deal scanning")
    parser.add_argument("--hot-only", action="store_true",
                        help="Skip watchlist scanning")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {})
    seen_cfg = config.get("seen_deals", {})
    seen = load_seen(
        max_age_days=seen_cfg.get("max_age_days", 30),
        max_entries=seen_cfg.get("max_entries", 5000),
    )

    try:
        feed_deals = fetch_ozbargain_feed()
    except Exception as e:
        log.error("Feed fetch failed: %s", e)
        print(f"ERROR: OzBargain feed fetch failed — {e}", file=sys.stderr)
        sys.exit(1)

    log.info("Fetched %d deals from OzBargain RSS", len(feed_deals))

    watchlist_matches = []
    hot_matches = []

    if not args.hot_only:
        watchlist = load_json(WATCHLIST_PATH, [])
        watchlist_matches = match_watchlist(feed_deals, watchlist, seen)
        log.info("Watchlist: %d matches", len(watchlist_matches))

    if not args.watchlist_only:
        hot_matches = match_hot_deals(feed_deals, args.min_votes, seen)
        log.info("Hot deals: %d matches (>=%d votes)", len(hot_matches), args.min_votes)

    save_json(SEEN_DEALS_PATH, seen)

    if watchlist_matches or hot_matches:
        output = format_output(watchlist_matches, hot_matches)
        print(output)
    else:
        log.info("No qualifying deals this run.")


if __name__ == "__main__":
    main()
