#!/usr/bin/env python3
"""Coles + Woolies Half-Price -> Grocy shopping list (weekly hermes cron job).

Purpose:
    Once a week, fetch all half-price specials from Coles and Woolworths Australia,
    filter to items the user actually shops for (matched against the Grocy product
    catalogue), dedupe against a 7-day seen.json + the existing Grocy list, and push
    matches into the "Coles & Woolies Half Price" Grocy shopping list. Stdout
    summary is relayed by the hermes LLM gateway to WhatsApp.

Schedule:
    Wed 7 AM AEST (cron `0 7 * * 3`) when AU supermarket catalogues refresh.
    Wired via ~/.hermes/cron/jobs.json -> hermes-gateway.service.

Smoke test (does NOT push to Grocy):
    cd ~/.hermes/scripts/hermes-shopping-assistant
    python3 halfprice_to_grocy.py --dry-run --max-pages 0
    python3 halfprice_to_grocy.py --dry-run --max-pages 1 --retailer woolworths

Output contract:
    Stdout = single short summary block (or "[SILENT]" for no-news weeks).
    Exit codes: 0 = success, 1 = block/HTTP, 2 = Grocy error.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from grocy_client import GrocyClient, GrocyError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIST_NAME = "Coles & Woolies Half Price"
SEEN_PATH = SCRIPT_DIR / "halfprice_seen.json"
LOG_DIR = SCRIPT_DIR / "logs"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

WW_LANDING_URL = "https://www.woolworths.com.au/shop/browse/specials/half-price"
WW_API_URL = "https://www.woolworths.com.au/apis/ui/browse/category"
WW_CATEGORIES_URL = "https://www.woolworths.com.au/apis/ui/PiesCategoriesWithSpecials"
WW_PAGE_SIZE = 36

COLES_LANDING_URL = "https://www.coles.com.au/on-special?filter_Special=halfprice"
COLES_BLOCK_MARKERS = ("Pardon Our Interruption", "Just a moment", "Cloudflare")
COLES_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# Coles WAF rejects bare `requests` fingerprints; these client hints (verified
# 2026-05-04 on a previously-blocked IP) make the WAF treat us as a real Chrome.
COLES_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not.A/Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Dnt": "1",
}
COLES_JSON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not.A/Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Referer": COLES_LANDING_URL,
    "x-nextjs-data": "1",
}

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "halfprice_to_grocy.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ColesBlockedError(Exception):
    """Raised when Coles WAF/Cloudflare returns an interstitial."""

    pass


class ColesBuildIdError(Exception):
    """Raised when the Coles __NEXT_DATA__ buildId can't be extracted."""

    pass


class WoolworthsBlockedError(Exception):
    """Raised when Woolworths Akamai returns a bot challenge."""

    pass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Special:
    retailer: str
    sku: str
    name: str
    brand: str | None
    size: str | None
    price_now: float
    price_was: float | None
    saving: float | None
    url: str


# ---------------------------------------------------------------------------
# Woolworths fetcher
# ---------------------------------------------------------------------------


def _ww_resolve_halfprice_node_id(session: requests.Session) -> str:
    """Walk the WW category tree for the Half Price node.

    Resolves dynamically (vs hard-coding) so the script self-heals if WW
    renumbers the category. Verified live 2026-05-04: NodeId='specialsgroup.3676'.
    """
    resp = session.get(WW_CATEGORIES_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    def walk(node):
        nid = node.get("NodeId", "") or ""
        desc = node.get("Description", "") or ""
        url = node.get("UrlFriendlyName", "") or ""
        if "half" in (desc + url).lower() and nid.startswith("specialsgroup."):
            return nid
        for child in node.get("Children", []) or []:
            found = walk(child)
            if found:
                return found
        return None

    for cat in data.get("Categories", []) or []:
        node_id = walk(cat)
        if node_id:
            return node_id
    raise WoolworthsBlockedError(
        "PiesCategoriesWithSpecials: no half-price NodeId found"
    )


def _ww_request_body(page_number: int, category_id: str) -> dict:
    """Return the JSON body for the WW category API."""
    return {
        "categoryId": category_id,
        "pageNumber": page_number,
        "pageSize": WW_PAGE_SIZE,
        "sortType": "TraderRelevance",
        "url": "/shop/browse/specials/half-price",
        "location": "/shop/browse/specials/half-price",
        "formatObject": "{}",
        "isSpecial": True,
        "isBundle": False,
        "isMobile": False,
        "filters": [],
        "token": "",
        "gpBoost": 0,
        "isHideUnavailableProducts": False,
        "isRegisteredRewardCardPromotion": None,
        "enableAdReRanking": False,
        "groupEdmVariants": True,
        "categoryVersion": "v2",
    }


def _ww_post_page(
    session: requests.Session, page_number: int, category_id: str
) -> dict:
    """POST a single page; raise WoolworthsBlockedError on Akamai challenge."""
    resp = session.post(
        WW_API_URL,
        json=_ww_request_body(page_number, category_id),
        headers={"Referer": WW_LANDING_URL},
        timeout=20,
    )
    if resp.status_code == 403 and (
        "_abck" in resp.text or "Just a moment" in resp.text
    ):
        raise WoolworthsBlockedError(resp.text[:200])
    if resp.status_code >= 400:
        resp.raise_for_status()
    return resp.json()


def _ww_parse_products(data: dict) -> list[Special]:
    """Flatten Bundles[*].Products[*] into Special records (IsHalfPrice only)."""
    specials: list[Special] = []
    for bundle in data.get("Bundles", []) or []:
        for product in bundle.get("Products", []) or []:
            if not product.get("IsHalfPrice"):
                continue
            stockcode = product.get("Stockcode")
            if stockcode is None:
                continue
            was = product.get("WasPrice")
            save = product.get("SavingsAmount")
            specials.append(
                Special(
                    retailer="woolworths",
                    sku=str(stockcode),
                    name=(product.get("DisplayName") or product.get("Name") or ""),
                    brand=product.get("Brand"),
                    size=product.get("PackageSize"),
                    price_now=float(product["Price"]),
                    price_was=float(was) if was else None,
                    saving=float(save) if save else None,
                    url=f"https://www.woolworths.com.au/shop/productdetails/{stockcode}",
                )
            )
    return specials


def fetch_woolworths_half_price(max_pages: int | None = None) -> list[Special]:
    """Fetch Woolworths half-price specials.

    Bootstraps the landing page to warm Akamai cookies, then paginates the
    category API. Raises WoolworthsBlockedError on 403 + bot-challenge body.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    # Bootstrap: warm _abck / bm_sz cookies. Discard body.
    try:
        boot = session.get(WW_LANDING_URL, timeout=20)
        if boot.status_code != 200:
            log.warning(
                "woolworths bootstrap returned status=%d (continuing)",
                boot.status_code,
            )
    except requests.RequestException as e:
        log.warning("woolworths bootstrap raised %s (continuing)", e)

    # Resolve the half-price categoryId dynamically (self-heals if WW renumbers).
    category_id = _ww_resolve_halfprice_node_id(session)
    log.info("woolworths: resolved half-price categoryId=%s", category_id)

    # Page 1 — read total count.
    data = _ww_post_page(session, 1, category_id)
    total = int(data.get("TotalRecordCount") or 0)
    total_pages = (total + WW_PAGE_SIZE - 1) // WW_PAGE_SIZE if total else 1
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    specials: list[Special] = []
    specials.extend(_ww_parse_products(data))

    for page in range(2, total_pages + 1):
        time.sleep(1.0 + random.uniform(0, 0.3))
        data = _ww_post_page(session, page, category_id)
        specials.extend(_ww_parse_products(data))

    log.info(
        "woolworths: %d pages, %d half-price items", total_pages, len(specials)
    )
    return specials


# ---------------------------------------------------------------------------
# Coles fetcher (Phase 3 — still a stub)
# ---------------------------------------------------------------------------


def _coles_check_blocked(text: str, status: int) -> None:
    """Raise ColesBlockedError if response looks like a WAF interstitial."""
    if status in (403, 429) or any(m in text for m in COLES_BLOCK_MARKERS):
        snippet = text[:200].replace("\n", " ")
        raise ColesBlockedError(f"status={status} snippet={snippet!r}")


def _coles_scrape_build_id(session: requests.Session) -> str:
    """Scrape buildId from __NEXT_DATA__ on the half-price landing page.

    The buildId rotates per Coles deploy — never hard-code. Verified live
    2026-05-04: `20260422.4-9bb38fe7a51d8dfe1ae900e28c3a6e9ec1537cd6`.
    """
    resp = session.get(
        COLES_LANDING_URL,
        timeout=25,
        headers=COLES_HTML_HEADERS,
    )
    _coles_check_blocked(resp.text, resp.status_code)
    if resp.status_code != 200:
        raise ColesBuildIdError(
            f"landing returned status={resp.status_code}"
        )
    m = COLES_NEXT_DATA_RE.search(resp.text)
    if not m:
        raise ColesBuildIdError("__NEXT_DATA__ script tag not found")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ColesBuildIdError(f"__NEXT_DATA__ JSON parse: {e}") from e
    build_id = data.get("buildId")
    if not build_id:
        raise ColesBuildIdError("buildId field absent")
    return build_id


def _coles_get_page(
    session: requests.Session, build_id: str, page_number: int
) -> dict:
    """GET a single page of Coles half-price JSON."""
    url = (
        f"https://www.coles.com.au/_next/data/{build_id}/en/on-special.json"
        f"?filter_Special=halfprice&page={page_number}"
    )
    resp = session.get(url, timeout=25, headers=COLES_JSON_HEADERS)
    _coles_check_blocked(resp.text, resp.status_code)
    if resp.status_code >= 400:
        resp.raise_for_status()
    return resp.json()


def _coles_parse_results(data: dict) -> list[Special]:
    """Extract half-price PRODUCT entries into Special records."""
    specials: list[Special] = []
    results = (
        data.get("pageProps", {})
        .get("searchResults", {})
        .get("results", [])
    )
    for entry in results:
        if entry.get("_type") != "PRODUCT":
            continue
        pricing = entry.get("pricing") or {}
        if pricing.get("promotionType") != "SPECIAL":
            continue
        if (
            pricing.get("specialType") != "PERCENT_OFF"
            and pricing.get("priceDescription") != "1/2 Price"
        ):
            continue
        product_code = entry.get("productCode") or entry.get("id")
        if product_code is None:
            continue
        was = pricing.get("was")
        save = pricing.get("saveAmount")
        specials.append(
            Special(
                retailer="coles",
                sku=str(product_code),
                name=entry.get("name", ""),
                brand=entry.get("brand"),
                size=entry.get("size"),
                price_now=float(pricing["now"]),
                price_was=float(was) if was else None,
                saving=float(save) if save else None,
                url=f"https://www.coles.com.au/product/{product_code}",
            )
        )
    return specials


def fetch_coles_half_price(max_pages: int | None = None) -> list[Special]:
    """Fetch Coles half-price specials.

    Two-step: scrape buildId from the landing page, then paginate the Next.js
    data endpoint. Raises ColesBlockedError on WAF interstitial,
    ColesBuildIdError if __NEXT_DATA__ can't be parsed.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-AU,en;q=0.9",
        }
    )

    build_id = _coles_scrape_build_id(session)
    log.info("coles: buildId=%s", build_id)

    # Page 1 — read total count.
    data = _coles_get_page(session, build_id, 1)
    sr = data.get("pageProps", {}).get("searchResults", {})
    total = int(sr.get("noOfResults") or 0)
    page_size = int(sr.get("pageSize") or 48)
    total_pages = (total + page_size - 1) // page_size if total else 1
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    specials: list[Special] = []
    specials.extend(_coles_parse_results(data))

    for page in range(2, total_pages + 1):
        time.sleep(2.0 + random.uniform(0, 0.5))
        data = _coles_get_page(session, build_id, page)
        specials.extend(_coles_parse_results(data))

    log.info("coles: %d pages, %d half-price items", total_pages, len(specials))
    return specials


# ---------------------------------------------------------------------------
# Grocy product matching + dedup
# ---------------------------------------------------------------------------

# Tokens that add no signal — dropped before matching.
_NOISE_TOKENS = frozenset(
    {
        "the", "a", "an", "and", "or", "for", "with", "of", "to",
        "pack", "size", "free", "new", "by", "in", "from",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# Trailing tokens that are pure size/quantity (drop from "primary noun" check).
_SIZE_TOKEN_RE = re.compile(r"^(\d+(\.\d+)?)?(g|kg|ml|l|cm|mm|pk|pack|each|x\d+)$")


def _tokenize(name: str) -> list[str]:
    """Lowercase, split on non-alnum, drop noise + ≤2-char tokens."""
    if not name:
        return []
    tokens = _TOKEN_RE.findall(name.lower())
    return [t for t in tokens if len(t) > 2 and t not in _NOISE_TOKENS]


def _content_tokens(name: str) -> list[str]:
    """Like _tokenize but also strip trailing size/pack tokens (so 'Milk 2L' → ['milk'])."""
    toks = _tokenize(name)
    while toks and _SIZE_TOKEN_RE.match(toks[-1]):
        toks.pop()
    return toks


def load_grocy_product_index(client: GrocyClient) -> list[tuple[list, dict]]:
    """Return list of (token_list, product_dict). One bulk fetch.

    Token list (not set) preserves order so we can recover phrase semantics.
    """
    products = client.get_products(limit=2000)
    index: list[tuple[list, dict]] = []
    for p in products:
        toks = _tokenize(p.get("name", ""))
        if toks:
            index.append((toks, p))
    return index


def _matches_grocy(gtoks: list[str], special_tokens: list[str]) -> bool:
    """Decide whether a Grocy product matches a special.

    Multi-token Grocy name: all tokens must appear in special (subset rule).
    Single-token Grocy name: token must be one of the LAST 2 content tokens
    of the special, where content = name minus trailing size/pack markers.
    This kills false positives like 'Milk' matching 'Cadbury Dairy Milk Chocolate'.
    """
    gset = set(gtoks)
    sset = set(special_tokens)
    if not gset.issubset(sset):
        return False
    if len(gtoks) == 1:
        # Single-token Grocy: must be the LAST content token (head noun position).
        # Kills 'Milk' matching 'Cadbury Dairy Milk Chocolate' (chocolate is head),
        # kills 'Salt' matching 'Salt & Vinegar Chips' (vinegar/chips are head).
        return special_tokens[-1] == gtoks[0]
    return True


def match_to_grocy(
    specials: list[Special],
    grocy_index: list[tuple[list, dict]],
    no_filter: bool = False,
) -> list[tuple[Special, dict | None]]:
    """For each special, find a matching Grocy product.

    Multi-token Grocy names (e.g. 'Olive oil') match if all tokens appear
    in the special. Single-token Grocy names (e.g. 'Milk') only match if
    the token is the primary noun (one of the last 2 content tokens).

    With no_filter=True, every special is paired with None (Grocy-side
    creation handled later by --create-missing).
    """
    if no_filter:
        return [(s, None) for s in specials]

    matches: list[tuple[Special, dict | None]] = []
    for s in specials:
        special_tokens = _content_tokens(f"{s.brand or ''} {s.name}")
        if not special_tokens:
            continue
        for gtoks, gproduct in grocy_index:
            if _matches_grocy(gtoks, special_tokens):
                matches.append((s, gproduct))
                break
    return matches


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: Path, data) -> None:
    """Atomic write via tmp + os.replace (pattern from check_deals.py:53-57)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_seen(max_age_days: int = 7) -> dict[str, int]:
    """Load seen-key map, pruning entries older than max_age_days."""
    seen = _load_json(SEEN_PATH, {})
    if not isinstance(seen, dict):
        return {}
    cutoff = int(time.time()) - max_age_days * 86400
    return {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > cutoff}


def filter_unseen(
    matches: list[tuple[Special, dict | None]],
    seen: dict[str, int],
) -> list[tuple[Special, dict | None]]:
    """Drop matches where (retailer:sku) was added in the last 7 days."""
    out = []
    for s, g in matches:
        if f"{s.retailer}:{s.sku}" not in seen:
            out.append((s, g))
    return out


def also_dedup_against_grocy_list(
    matches: list[tuple[Special, dict | None]],
    client: GrocyClient,
    list_id: int,
) -> list[tuple[Special, dict | None]]:
    """Drop matches whose Grocy product is already on the target list."""
    if not matches:
        return matches
    items = client.get_shopping_list_items(list_id)
    on_list = {int(i["product_id"]) for i in items}
    out = []
    for s, g in matches:
        if g is not None and int(g["id"]) in on_list:
            continue
        out.append((s, g))
    return out


def cap_per_product(
    matches: list[tuple[Special, dict | None]], max_per_product: int
) -> list[tuple[Special, dict | None]]:
    """Cap matches per Grocy product, keeping the biggest-savings ones first.

    Without a cap, 'Toothpaste' might match 30 different Oral-B variants and
    spam the list. Default is 3 per product. Specials with no Grocy match
    (no_filter or create_missing path) are passed through unchanged.
    """
    if max_per_product <= 0:
        return matches
    # Stable-sort by savings desc, then bucket by Grocy product id.
    sorted_m = sorted(
        matches,
        key=lambda sg: (sg[0].saving or 0),
        reverse=True,
    )
    counts: dict[int, int] = {}
    out: list[tuple[Special, dict | None]] = []
    for s, g in sorted_m:
        if g is None:
            out.append((s, g))
            continue
        gid = int(g["id"])
        if counts.get(gid, 0) >= max_per_product:
            continue
        counts[gid] = counts.get(gid, 0) + 1
        out.append((s, g))
    return out


# ---------------------------------------------------------------------------
# Gemini LLM validation
# ---------------------------------------------------------------------------

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite-preview:generateContent"
)
GEMINI_BATCH_SIZE = 30
VALIDATION_CACHE_PATH = SCRIPT_DIR / "halfprice_validation_cache.json"


def _gemini_prompt(pairs: list[tuple[str, str]]) -> str:
    """Build the validation prompt. Each pair is (grocy_name, special_name)."""
    lines = [
        "You validate matches between supermarket specials and a user's home "
        "grocery inventory. The user wants to know when items they actually "
        "buy are on half-price special.",
        "",
        "For each pair, decide if the special is a reasonable substitute "
        "purchase for the grocery item. REJECT pairs where the words overlap "
        "but the products are different categories — e.g. 'Honey' the "
        "cosmetic-shade vs honey the food; 'Passion fruit' body wash vs "
        "passionfruit the food; 'Cream' fragrance vs cream cheese; 'Apple' "
        "tech vs apples the fruit.",
        "",
        "Pairs:",
    ]
    for i, (grocy, special) in enumerate(pairs, 1):
        lines.append(f"{i}. Grocy {grocy!r} | Special {special!r}")
    lines.append("")
    lines.append(
        "Respond ONLY with a JSON array of booleans, one per pair, in order."
    )
    return "\n".join(lines)


def _gemini_call(api_key: str, pairs: list[tuple[str, str]]) -> list[bool]:
    """Call Gemini once for a batch of up to GEMINI_BATCH_SIZE pairs."""
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": _gemini_prompt(pairs)}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {"type": "array", "items": {"type": "boolean"}},
            "temperature": 0,
        },
    }
    resp = requests.post(
        f"{GEMINI_URL}?key={api_key}", json=body, timeout=30
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    decisions = json.loads(text)
    if not isinstance(decisions, list) or len(decisions) != len(pairs):
        raise ValueError(
            f"gemini returned {len(decisions) if isinstance(decisions, list) else type(decisions).__name__}, expected {len(pairs)}"
        )
    return [bool(d) for d in decisions]


def validate_with_gemini(
    matches: list[tuple[Special, dict | None]],
) -> list[tuple[Special, dict | None]]:
    """Filter matches via Gemini semantic check. Cache decisions on disk.

    Pairs without a Grocy product (e.g. --no-filter / --create-missing path)
    are passed through unchanged. Failures fail OPEN — keep the match and
    warn — so a Gemini outage doesn't silently empty the list.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log.warning("GEMINI_API_KEY not set; skipping LLM validation")
        return matches

    cache: dict[str, bool] = _load_json(VALIDATION_CACHE_PATH, {})
    if not isinstance(cache, dict):
        cache = {}

    # Partition: passthrough (no Grocy product), cached, or needs query.
    out: list[tuple[Special, dict | None]] = []
    to_query: list[tuple[int, Special, dict]] = []  # (out_index_placeholder, s, g)
    placeholders: list[int] = []  # indexes in `out` to fill after query

    for s, g in matches:
        if g is None:
            out.append((s, g))
            continue
        cache_key = f"{s.retailer}:{s.sku}|grocy:{g['id']}"
        if cache_key in cache:
            if cache[cache_key]:
                out.append((s, g))
            # else: dropped silently
            continue
        # Defer to batch query.
        out.append((s, g))  # tentative — may be removed if Gemini says no
        placeholders.append(len(out) - 1)
        to_query.append((len(out) - 1, s, g))

    if not to_query:
        log.info("gemini: all %d matches resolved from cache", len(matches))
        return out

    log.info(
        "gemini: %d cached, %d new queries (batched %d/req)",
        len(matches) - len(to_query) - sum(1 for s, g in matches if g is None),
        len(to_query),
        GEMINI_BATCH_SIZE,
    )

    drop_indexes: set[int] = set()
    for batch_start in range(0, len(to_query), GEMINI_BATCH_SIZE):
        batch = to_query[batch_start : batch_start + GEMINI_BATCH_SIZE]
        pairs = [(g["name"], s.name) for _, s, g in batch]
        try:
            decisions = _gemini_call(api_key, pairs)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning(
                "gemini batch failed (%d items, fail-open): %s", len(batch), e
            )
            continue  # leave tentative-keep entries in `out`
        for (placeholder_idx, s, g), keep in zip(batch, decisions):
            cache_key = f"{s.retailer}:{s.sku}|grocy:{g['id']}"
            cache[cache_key] = keep
            if not keep:
                drop_indexes.add(placeholder_idx)

    # Persist cache after each successful batch (durability).
    _save_json_atomic(VALIDATION_CACHE_PATH, cache)

    # Rebuild output, dropping rejected indexes.
    final = [item for i, item in enumerate(out) if i not in drop_indexes]
    log.info(
        "gemini: kept %d/%d after validation (%d dropped)",
        len(final),
        len(out),
        len(drop_indexes),
    )
    return final


# ---------------------------------------------------------------------------
# Grocy push
# ---------------------------------------------------------------------------


def ensure_list(client: GrocyClient) -> int:
    """Find or create the target shopping list. Returns its id."""
    existing = client.find_shopping_list(LIST_NAME)
    if existing is not None:
        return int(existing["id"])
    created = client.create_shopping_list(
        LIST_NAME,
        description="Auto-populated weekly by halfprice_to_grocy.py",
    )
    new_id = int(created["created_object_id"])
    log.info("created Grocy shopping list %r id=%d", LIST_NAME, new_id)
    return new_id


def _build_note(s: Special) -> str:
    """Compact one-liner shown next to the item in Grocy."""
    bits = [f"{s.retailer.title()} ½ price"]
    bits.append(f"${s.price_now:.2f}")
    if s.price_was is not None:
        bits.append(f"(was ${s.price_was:.2f}")
        if s.saving is not None:
            bits.append(f"save ${s.saving:.2f})")
        else:
            bits[-1] = bits[-1] + ")"
    return " ".join(bits)


def push_matches(
    client: GrocyClient,
    list_id: int,
    matches: list[tuple[Special, dict | None]],
    dry_run: bool,
    create_missing: bool,
) -> dict:
    """Push matched specials into the Grocy list. Returns counters."""
    counts = {
        "added": 0,
        "skipped_no_product": 0,
        "errors": 0,
        "by_retailer": {"coles": 0, "woolworths": 0},
        "added_specials": [],  # for seen.json update + summary
    }
    for special, gproduct in matches:
        if gproduct is None:
            if create_missing:
                try:
                    desc_bits = [b for b in (special.brand, special.size) if b]
                    desc = (
                        f"{' '.join(desc_bits)} (auto-created from {special.retailer})"
                        if desc_bits
                        else f"Auto-created from {special.retailer}"
                    )
                    if dry_run:
                        log.info("would create product %r", special.name)
                        gproduct = {"id": -1, "name": special.name}
                    else:
                        gproduct = client.get_or_create_product(
                            name=special.name, description=desc
                        )
                except GrocyError as e:
                    log.warning(
                        "create_product %r failed: %s", special.name, e
                    )
                    counts["errors"] += 1
                    continue
            else:
                counts["skipped_no_product"] += 1
                continue

        note = _build_note(special)
        try:
            if dry_run:
                log.info(
                    "DRY: would add product_id=%s '%s' note=%s",
                    gproduct.get("id"),
                    special.name[:40],
                    note[:80],
                )
            else:
                result = client.add_to_shopping_list(
                    list_id=list_id,
                    product_id=int(gproduct["id"]),
                    amount=1,
                    note=note,
                )
                if result and "created_object_id" in result:
                    _iid = int(result["created_object_id"])
                    try:
                        client.set_userfield_values(
                            "shopping_list", _iid, {"store_url": special.url}
                        )
                    except Exception as _uf_e:
                        log.warning("set store_url %s: %s", special.sku, _uf_e)
            counts["added"] += 1
            counts["by_retailer"][special.retailer] += 1
            counts["added_specials"].append(special)
        except GrocyError as e:
            log.warning("add_to_shopping_list failed for %s: %s", special.sku, e)
            counts["errors"] += 1
    return counts


def update_seen(specials: list[Special], seen: dict[str, int]) -> None:
    """Mark successfully-added specials as seen NOW."""
    now = int(time.time())
    for s in specials:
        seen[f"{s.retailer}:{s.sku}"] = now
    _save_json_atomic(SEEN_PATH, seen)


def _format_summary(counts: dict, scraped_total: int) -> str:
    """3-5 line summary for the LLM relay (or [SILENT] for no-news weeks)."""
    if counts["added"] == 0:
        return "[SILENT]"
    added = counts["added"]
    by = counts["by_retailer"]
    lines = [f"Added {added} half-price specials to your Coles & Woolies list:"]
    if by["coles"]:
        lines.append(f"• Coles: {by['coles']} items")
    if by["woolworths"]:
        lines.append(f"• Woolworths: {by['woolworths']} items")
    # Top saving
    added_specs = counts["added_specials"]
    with_saving = [s for s in added_specs if s.saving is not None]
    if with_saving:
        top = max(with_saving, key=lambda s: s.saving)
        lines.append(
            f"• Top saving: ${top.saving:.2f} on {top.name} ({top.retailer.title()})"
        )
        total_saving = sum(s.saving for s in with_saving)
        lines.append(f"• Total potential savings: ${total_saving:.2f}")
    if counts["errors"]:
        lines.append(
            f"⚠ {counts['errors']} errors — check logs/halfprice_to_grocy.log"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _print_dry_run_preview(retailer: str, specials: list[Special]) -> None:
    print(f"{retailer}: {len(specials)} specials")
    for s in specials[:3]:
        was = f"${s.price_was:.2f}" if s.price_was is not None else "n/a"
        save = f"${s.saving:.2f}" if s.saving is not None else "n/a"
        print(
            f"  • [{s.retailer}] {s.name} — ${s.price_now:.2f} "
            f"(was {was}, save {save})"
        )


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Coles + Woolworths half-price specials and push matches "
        "into the 'Coles & Woolies Half Price' Grocy shopping list.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan, skip Grocy writes.",
    )
    parser.add_argument(
        "--retailer",
        choices=["coles", "woolworths", "both"],
        default="both",
        help="Which retailer(s) to fetch (default: both).",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Auto-create Grocy products for unmatched specials (default: off).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap pages per retailer (default: unlimited; useful for smoke tests).",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Push every half-price item, ignoring Grocy product list (default: off).",
    )
    parser.add_argument(
        "--max-per-product",
        type=int,
        default=3,
        help="Max specials per Grocy product, biggest-savings first (default: 3, 0 = unlimited).",
    )
    parser.add_argument(
        "--no-llm-validate",
        action="store_true",
        help="Skip Gemini semantic validation (default: validate if GEMINI_API_KEY is set).",
    )
    args = parser.parse_args(argv)

    try:
        client = GrocyClient()
        info = client.system_info()
        log.info("Connected to Grocy %s", info["grocy_version"]["Version"])
    except GrocyError as e:
        log.error("Grocy connectivity failed: %s", e)
        print(f"ERROR: Grocy unreachable — {e}", file=sys.stderr)
        return 2

    # Fetch retailers in parallel (fits inside 120s gateway timeout).
    fetchers: dict[str, callable] = {}
    if args.retailer in {"both", "woolworths"}:
        fetchers["woolworths"] = lambda: fetch_woolworths_half_price(args.max_pages)
    if args.retailer in {"both", "coles"}:
        fetchers["coles"] = lambda: fetch_coles_half_price(args.max_pages)

    results: dict[str, list[Special]] = {}
    try:
        with ThreadPoolExecutor(max_workers=max(len(fetchers), 1)) as pool:
            futures = {pool.submit(fn): name for name, fn in fetchers.items()}
            for fut in futures:
                results[futures[fut]] = fut.result()
    except (ColesBlockedError, ColesBuildIdError, WoolworthsBlockedError) as e:
        log.error("retailer fetch blocked/failed: %s: %s", type(e).__name__, e)
        print(f"ERROR: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        log.error("network error: %s", e)
        print(f"ERROR: network — {e}", file=sys.stderr)
        return 1

    ww_specials = results.get("woolworths", [])
    coles_specials = results.get("coles", [])
    all_specials = ww_specials + coles_specials

    if args.dry_run:
        if "woolworths" in fetchers:
            _print_dry_run_preview("woolworths", ww_specials)
        if "coles" in fetchers:
            _print_dry_run_preview("coles", coles_specials)

    # Match against Grocy catalogue + dedup.
    try:
        grocy_index = load_grocy_product_index(client)
        log.info("grocy: %d products in catalogue", len(grocy_index))
        matches = match_to_grocy(all_specials, grocy_index, no_filter=args.no_filter)
        log.info(
            "matched %d/%d specials to Grocy products",
            len(matches),
            len(all_specials),
        )

        # Gemini semantic validation BEFORE cap so true positives aren't
        # displaced by false-positive matches with bigger savings.
        if not args.no_llm_validate:
            before = len(matches)
            matches = validate_with_gemini(matches)
            log.info("after gemini validation: %d -> %d", before, len(matches))

        seen = load_seen()
        matches = filter_unseen(matches, seen)
        log.info("after seen-filter: %d", len(matches))

        # Ensure list exists (creates if missing — write op, skipped in dry-run)
        if args.dry_run:
            existing = client.find_shopping_list(LIST_NAME)
            list_id = int(existing["id"]) if existing else -1
        else:
            list_id = ensure_list(client)

        if list_id != -1:
            matches = also_dedup_against_grocy_list(matches, client, list_id)
            log.info("after list-dedup: %d", len(matches))

        # Cap per Grocy product (avoid spamming the list with 30 toothpaste variants).
        before = len(matches)
        matches = cap_per_product(matches, args.max_per_product)
        log.info("after per-product-cap (%d): %d -> %d", args.max_per_product, before, len(matches))

        # Push (or dry-run preview).
        counts = push_matches(
            client,
            list_id,
            matches,
            dry_run=args.dry_run,
            create_missing=args.create_missing,
        )
    except GrocyError as e:
        log.error("Grocy operation failed: %s", e)
        print(f"ERROR: Grocy — {e}", file=sys.stderr)
        return 2

    # Update seen.json for successful adds (skipped in dry-run).
    if not args.dry_run and counts["added_specials"]:
        update_seen(counts["added_specials"], seen)

    if args.dry_run:
        print(f"\n{len(all_specials)} scraped → {len(matches)} would be pushed")
        for s, g in matches[:10]:
            tag = f"grocy:{g['id']} {g['name']!r}" if g else "no-Grocy-product"
            print(f"  MATCH: {s.retailer}:{s.sku} '{s.name[:55]}' → {tag}")
        if len(matches) > 10:
            print(f"  ... +{len(matches) - 10} more")
        print()

    # Stdout summary (relayed by hermes LLM gateway to WhatsApp).
    print(_format_summary(counts, len(all_specials)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
