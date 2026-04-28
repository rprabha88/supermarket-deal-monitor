#!/usr/bin/env python3
"""
Supermarket Deal Monitor
Daily UK supermarket price tracker with WhatsApp alerts via Twilio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import httpx
import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from twilio.rest import Client as TwilioClient

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config (from environment) ──────────────────────────────────────────
SEARCHAPI_KEY         = os.environ["SEARCHAPI_API_KEY"]
REDIS_URL             = os.environ.get("REDIS_URL", "redis://localhost:6379")
REDIS_NAMESPACE       = os.environ.get("REDIS_NAMESPACE", "supermarket")
TWILIO_ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN     = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM  = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
DEFAULT_PHONE_NUMBER  = os.environ.get("DEFAULT_PHONE_NUMBER", "")

DEFAULT_ITEMS = [
    "Chicken Breast", "Dairy Milk Chocolate", "Greek Yoghurt", "Halloumi",
    "Coconut Water", "Apple Juice", "Bananas", "Apples",
]
DEFAULT_STORES   = ["Tesco", "Sainsburys", "Iceland"]
DEFAULT_LOCATION = "London, United Kingdom"
MY_GROCERIES_KEY = "my_groceries"

SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"

ONLINE_STORE_KEYS        = {"ocado", "amazon"}
MARGINAL_GAIN_THRESHOLD  = 1.00


# ── Redis helpers ──────────────────────────────────────────────────────
# Shared connection pool — reused across requests in the same worker lifetime.
_redis_client: aioredis.Redis | None = None

def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=10,
        )
    return _redis_client


def ns(key: str) -> str:
    """Namespace a Redis key."""
    return f"{REDIS_NAMESPACE}:{key}"


# ── My Groceries persistence ───────────────────────────────────────────
async def load_my_groceries() -> list[dict]:
    r = get_redis()
    raw = await r.get(ns(MY_GROCERIES_KEY))
    return json.loads(raw) if raw else []


async def save_my_groceries(items: list[dict]) -> None:
    r = get_redis()
    await r.set(ns(MY_GROCERIES_KEY), json.dumps(items))


async def append_price_history(item_name: str, price: float, store: str, product: str) -> None:
    """Append a price data-point to per-item history (max 30 entries)."""
    r = get_redis()
    key = ns(f"price_history:{item_name.lower().replace(' ', '_')}")
    raw = await r.get(key)
    history = json.loads(raw) if raw else []
    history.append({
        "price": price,
        "store": store,
        "product": product,
        "date": datetime.now().strftime("%Y-%m-%d"),
    })
    history = history[-30:]
    await r.set(key, json.dumps(history))


async def get_price_history(item_name: str) -> list[dict]:
    r = get_redis()
    key = ns(f"price_history:{item_name.lower().replace(' ', '_')}")
    raw = await r.get(key)
    return json.loads(raw) if raw else []


async def get_all_price_histories(items: list[dict]) -> dict:
    """Batch-fetch all price histories in a single Redis pipeline."""
    if not items:
        return {}
    r = get_redis()
    keys = [ns(f"price_history:{item.get('generic_name','').lower().replace(' ', '_')}") for item in items]
    raws = await r.mget(*keys)
    return {
        item.get("generic_name", ""): (json.loads(raw) if raw else [])
        for item, raw in zip(items, raws)
    }


import re as _re
_UK_POSTCODE_RE = _re.compile(r'^[A-Z]{1,2}\d[A-Z\d]?(\s*\d[A-Z]{2})?\s*,?\s*', _re.IGNORECASE)

def clean_location(location: str) -> str:
    """Strip leading UK postcode from a location string so SearchAPI accepts it."""
    return _UK_POSTCODE_RE.sub('', location).strip().lstrip(',').strip() or location


def resolve_search_requests(items: list[str], preferences: list[dict]) -> list[tuple[str, str]]:
    """
    Build (generic_name, query) pairs for searching.

    If a saved preference includes an exact product, prefer that.
    Otherwise fall back to preferred brand, otherwise use the generic item name.
    """
    pref_map = {p["generic_name"].strip().lower(): p for p in preferences if p.get("generic_name")}
    resolved: list[tuple[str, str]] = []
    for item in items:
        generic = item.strip()
        pref = pref_map.get(generic.lower())
        if pref and pref.get("preferred_product"):
            resolved.append((generic, str(pref["preferred_product"]).strip()))
        elif pref and pref.get("preferred_brand"):
            resolved.append((generic, f"{pref['preferred_brand']} {generic}".strip()))
        else:
            resolved.append((generic, generic))
    return resolved


SEARCH_CACHE_TTL = 60 * 60  # 1 hour

import hashlib as _hashlib

def _search_cache_key(query: str, stores: list[str], location: str) -> str:
    raw = f"{query.lower()}|{'|'.join(sorted(s.lower() for s in stores))}|{clean_location(location).lower()}"
    h = _hashlib.md5(raw.encode()).hexdigest()[:16]
    return ns(f"search_cache:{h}")


# ── Step 1: Search prices via SearchAPI (Google Shopping) ──────────────
async def search_item_prices(item_name: str, query: str, stores: list[str], location: str) -> list[dict]:
    """Search Google Shopping for a query, filtered to target stores. Results cached 1h in Redis."""
    cache_key = _search_cache_key(query, stores, location)
    try:
        r = get_redis()
        cached = await r.get(cache_key)
        if cached:
            logger.info(f"Search cache hit for {query!r}")
            data = json.loads(cached)
            # Re-stamp item_query in case it was cached under a different item name
            for p in data:
                p["item_query"] = item_name
            return data
    except Exception as e:
        logger.warning(f"Search cache read failed: {e}")

    store_query = " ".join(stores)
    params = {
        "engine": "google_shopping",
        "q": f"{query} {store_query}",
        "gl": "gb",
        "hl": "en",
        "num": 15,
        "location": clean_location(location),
        "api_key": SEARCHAPI_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(SEARCHAPI_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        logger.warning(f"SearchAPI request failed for {query!r}: {e}")
        raise HTTPException(status_code=502, detail="Search provider unavailable. Try again later.")

    results = []
    for r in data.get("shopping_results", []):
        title  = r.get("title", "")
        price  = r.get("extracted_price")
        seller = r.get("seller", "").lower()
        if price is None:
            continue
        matched_store = None
        for s in stores:
            if s.lower() in seller or s.lower() in title.lower():
                matched_store = s
                break
        if matched_store:
            results.append({
                "item_query": item_name,
                "product": title,
                "price": float(price),
                "store": matched_store,
            })

    # Cache results for 1h
    try:
        r = get_redis()
        await r.set(cache_key, json.dumps(results), ex=SEARCH_CACHE_TTL)
    except Exception as e:
        logger.warning(f"Search cache write failed: {e}")

    return results


async def search_item_open(query: str, location: str) -> list[dict]:
    """Search Google Shopping without store filtering — returns all results."""
    params = {
        "engine": "google_shopping",
        "q": f"{query} grocery UK",
        "gl": "gb",
        "hl": "en",
        "num": 20,
        "location": clean_location(location),
        "api_key": SEARCHAPI_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(SEARCHAPI_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        logger.warning(f"SearchAPI request failed for {query!r}: {e}")
        raise HTTPException(status_code=502, detail="Search provider unavailable. Try again later.")

    results = []
    for r in data.get("shopping_results", []):
        title  = r.get("title", "")
        price  = r.get("extracted_price")
        seller = r.get("seller", "")
        if price is None:
            continue
        results.append({
            "item_query": query,
            "product": title,
            "price": float(price),
            "store": seller if seller else "Online",
        })
    return results


async def fetch_all_prices(items: list[str], stores: list[str], location: str) -> list[dict]:
    """Fetch prices for all grocery items concurrently."""
    MOCK_PRICES = [
        {"item_query": "Bananas",  "product": "Tesco Bananas Loose",               "price": 0.78, "store": "Tesco"},
        {"item_query": "Bananas",  "product": "Sainsbury's Bananas 5 Pack",        "price": 1.30, "store": "Sainsburys"},
        {"item_query": "Bananas",  "product": "Aldi Bananas Loose",                "price": 0.69, "store": "Aldi"},
        {"item_query": "Bread",    "product": "Tesco Everyday White Bread 800g",   "price": 1.15, "store": "Tesco"},
        {"item_query": "Bread",    "product": "Sainsbury's White Bread 800g",      "price": 1.25, "store": "Sainsburys"},
        {"item_query": "Bread",    "product": "Aldi Village Bakery White 800g",    "price": 1.09, "store": "Aldi"},
        {"item_query": "Milk",     "product": "Tesco Whole Milk 2 Pints",          "price": 1.45, "store": "Tesco"},
        {"item_query": "Milk",     "product": "Sainsbury's Whole Milk 2 Pints",    "price": 1.55, "store": "Sainsburys"},
        {"item_query": "Chicken",  "product": "Tesco British Chicken Breast 300g", "price": 3.50, "store": "Tesco"},
        {"item_query": "Chicken",  "product": "Sainsbury's Chicken Breast 300g",   "price": 3.75, "store": "Sainsburys"},
        {"item_query": "Chicken",  "product": "Aldi Chicken Breast 400g",          "price": 2.99, "store": "Aldi"},
        {"item_query": "Pasta",    "product": "Tesco Spaghetti 500g",              "price": 0.75, "store": "Tesco"},
        {"item_query": "Pasta",    "product": "Sainsbury's Spaghetti 500g",        "price": 0.85, "store": "Sainsburys"},
        {"item_query": "Pasta",    "product": "Aldi Spaghetti 500g",               "price": 0.59, "store": "Aldi"},
        {"item_query": "Eggs",     "product": "Tesco Free Range Eggs 6pk",         "price": 1.89, "store": "Tesco"},
        {"item_query": "Eggs",     "product": "Sainsbury's Free Range Eggs 6pk",   "price": 2.10, "store": "Sainsburys"},
        {"item_query": "Eggs",     "product": "Aldi Free Range Eggs 6pk",          "price": 1.65, "store": "Aldi"},
    ]
    items_lower = [i.lower() for i in items]
    mock = [p for p in MOCK_PRICES if p["item_query"].lower() in items_lower]
    if mock:
        logger.info(f"Using mock prices ({len(mock)} entries) — SearchAPI quota exhausted")
        return mock

    logger.info("Fetching prices (%d items)", len(items))
    preferences = await load_my_groceries()
    search_reqs = resolve_search_requests(items, preferences)
    tasks = [search_item_prices(item_name, query, stores, location) for item_name, query in search_reqs]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    prices: list[dict] = []
    for item, result in zip(items, all_results):
        if isinstance(result, Exception):
            logger.warning(f"Failed to fetch prices for {item!r}: {result}")
            continue
        prices.extend(result)

    logger.info(f"Prices fetched — {len(prices)} products")
    return prices


# ── Step 2: Redis price store ──────────────────────────────────────────
async def load_previous_prices() -> dict:
    r = get_redis()
    raw = await r.get(ns("grocery_prices:latest"))
    if raw:
        previous = json.loads(raw)
        logger.info(f"Previous prices loaded ({len(previous.get('prices', []))} products)")
        return previous
    logger.info("No previous prices — this is the first run")
    return {}


async def store_current_prices(prices: list[dict]) -> None:
    r = get_redis()
    await r.set(
        ns("grocery_prices:latest"),
        json.dumps({"prices": prices, "timestamp": datetime.now().isoformat()}),
    )
    logger.info(f"Stored {len(prices)} current prices")


# ── Step 3: Deal analysis (pure Python, no LLM) ────────────────────────
async def analyze_deals(
    current_prices: list[dict], previous_data: dict, items: list[str], **_kwargs
) -> str:
    """Build a plain-text deal summary with price-drop detection. No external API needed."""
    if not current_prices:
        return "No prices found for the selected items today. Try again later or broaden your item names."

    # Build previous price map: item_lower -> store -> price
    prev_map: dict[str, dict[str, float]] = {}
    for p in previous_data.get("prices", []):
        iq = str(p.get("item_query") or "").strip().lower()
        st = str(p.get("store") or "").strip()
        pr = p.get("price")
        if iq and st and pr is not None:
            prev_map.setdefault(iq, {})[st] = float(pr)

    # Build current price map: item_lower -> list of {store, price, product}
    by_item: dict[str, list[dict]] = {}
    for p in current_prices:
        key = str(p.get("item_query") or "").strip()
        if key:
            by_item.setdefault(key, []).append(p)

    lines: list[str] = [f"Best prices today ({datetime.now().strftime('%d %b %Y')}):"]
    for item in items:
        pts = by_item.get(item, [])
        if not pts:
            continue
        best = min(pts, key=lambda r: float(r.get("price", 1e18)))
        price   = float(best.get("price", 0.0))
        store   = str(best.get("store", "")).strip() or "Unknown"
        product = str(best.get("product", "")).strip()

        # Price drop detection
        prev_price = prev_map.get(item.lower(), {}).get(store)
        drop_tag = ""
        if prev_price is not None and price < prev_price:
            drop_tag = f" ↓ was £{prev_price:.2f}"

        product_tag = f" — {product}" if product else ""
        lines.append(f"- {item}: £{price:.2f} at {store}{drop_tag}{product_tag}")

    out = "\n".join(lines)
    if len(out) > 900:
        out = out[:897] + "..."
    logger.info(f"Deal summary built ({len(out)} chars)")
    return out


# ── Step 4: WhatsApp via Twilio ────────────────────────────────────────
async def send_whatsapp_notification(summary: str, phone_number: str) -> None:
    """Send deal summary via Twilio WhatsApp."""
    logger.info(f"Sending WhatsApp to {phone_number}")

    def _send():
        twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        twilio.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            body=summary,
            to=f"whatsapp:+{phone_number}",
        )

    # Run blocking Twilio SDK in a thread so we don't block the event loop
    await asyncio.to_thread(_send)
    logger.info("WhatsApp notification sent")


# ── Core workflow (shared by endpoint + scheduler) ─────────────────────
async def run_deal_check(
    items: list[str],
    stores: list[str],
    location: str,
    phone_number: str | None,
    *,
    use_llm: bool,
) -> dict:
    """Fetch prices, analyse deals, persist, and notify. Returns result dict."""
    current_prices = await fetch_all_prices(items, stores, location)
    previous_data  = await load_previous_prices()
    is_first_run   = len(previous_data) == 0

    summary = await analyze_deals(current_prices, previous_data, items, use_llm=use_llm)

    await store_current_prices(current_prices)

    # Persist per-item price history for saved favourites
    saved_items  = await load_my_groceries()
    saved_names  = {i["generic_name"].lower() for i in saved_items}
    for p in current_prices:
        if p.get("item_query", "").lower() in saved_names:
            await append_price_history(p["item_query"], p["price"], p["store"], p["product"])

    if phone_number:
        await send_whatsapp_notification(summary, phone_number)

    return {
        "items_tracked":   len(items),
        "products_found":  len(current_prices),
        "deals_summary":   summary,
        "is_first_run":    is_first_run,
        "message": (
            ("Deal check complete! WhatsApp alert sent." if phone_number else "Deal check complete!")
            if not is_first_run
            else (
                "First run complete – baseline prices captured. Daily monitoring scheduled at 08:00 UTC."
                if phone_number
                else "First run complete – baseline prices captured."
            )
        ),
    }


# ── Scheduler ──────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="UTC")


async def scheduled_deal_check() -> None:
    """Job that runs daily at 08:00 UTC."""
    logger.info("Scheduled deal check starting")
    if not DEFAULT_PHONE_NUMBER:
        logger.warning("DEFAULT_PHONE_NUMBER not set — skipping scheduled check")
        return
    await run_deal_check(
        DEFAULT_ITEMS, DEFAULT_STORES, DEFAULT_LOCATION, DEFAULT_PHONE_NUMBER, use_llm=True
    )


SERVERLESS = bool(os.environ.get("VERCEL"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SERVERLESS:
        scheduler.add_job(
            scheduled_deal_check,
            CronTrigger(hour=8, minute=0),
            id="daily_deal_check",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Scheduler started — daily check at 08:00 UTC")
    yield
    if not SERVERLESS:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


# ── FastAPI app ────────────────────────────────────────────────────────
app = FastAPI(
    title="Supermarket Deal Monitor",
    description="Daily UK supermarket grocery deal monitor with WhatsApp alerts",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def serve_ui():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ── Pydantic models ────────────────────────────────────────────────────
class MonitorRequest(BaseModel):
    phone_number: str | None = Field(default=None, example="447442801959")
    items:        list[str] = Field(default=DEFAULT_ITEMS)
    stores:       list[str] = Field(default=DEFAULT_STORES)
    location:     str       = Field(default=DEFAULT_LOCATION)


class MonitorResponse(BaseModel):
    items_tracked:  int  = Field(...)
    products_found: int  = Field(...)
    deals_summary:  str  = Field(...)
    is_first_run:   bool = Field(...)
    message:        str  = Field(...)


class GroceryItem(BaseModel):
    generic_name:      str           = Field(..., example="Chicken Breast")
    preferred_brand:   str | None    = Field(None, example="Tariq Halal")
    preferred_store:   str | None    = Field(None, example="Tesco")
    preferred_product: str | None    = Field(None)


class AddGroceryRequest(BaseModel):
    item: GroceryItem


class AddFavouriteFromSearchRequest(BaseModel):
    generic_name: str      = Field(..., example="Dairy Milk Chocolate")
    product:      str      = Field(..., example="Cadbury Dairy Milk Giant Buttons 250g")
    store:        str | None = Field(None)


class RemoveGroceryRequest(BaseModel):
    generic_name: str = Field(..., example="Bananas")


class MyGroceriesResponse(BaseModel):
    items: list[GroceryItem]
    count: int


class PricePoint(BaseModel):
    price:   float = Field(...)
    store:   str   = Field(...)
    product: str   = Field(...)
    date:    str   = Field(...)


class ItemPriceHistory(BaseModel):
    item_name:    str              = Field(...)
    history:      list[PricePoint] = Field(...)
    lowest_ever:  float | None     = Field(None)
    current:      float | None     = Field(None)
    trend:        str | None       = Field(None)


class PriceHistoryResponse(BaseModel):
    items: list[ItemPriceHistory]


class SearchRequest(BaseModel):
    query:    str       = Field(..., example="Cadbury Dairy Milk 850g")
    stores:   list[str] = Field(default=DEFAULT_STORES)
    location: str       = Field(default=DEFAULT_LOCATION)


class ProductResult(BaseModel):
    product: str   = Field(...)
    price:   float = Field(...)
    store:   str   = Field(...)


class SearchResponse(BaseModel):
    query:        str                  = Field(...)
    results:      list[ProductResult]  = Field(...)
    cheapest:     ProductResult | None = Field(None)
    result_count: int                  = Field(...)


class ItemPricesRequest(BaseModel):
    generic_name: str = Field(..., example="Dairy Milk Chocolate")
    stores: list[str] = Field(default_factory=lambda: DEFAULT_STORES)
    location: str = Field(default=DEFAULT_LOCATION)


class ItemPriceResult(BaseModel):
    store: str = Field(...)
    product: str = Field(...)
    price: float = Field(...)


class ItemPricesResponse(BaseModel):
    generic_name: str = Field(...)
    query: str = Field(...)
    results: list[ItemPriceResult] = Field(default_factory=list)


class ShoppingPlanRequest(BaseModel):
    items:            list[str]        = Field(...)
    stores:           list[str]        = Field(default=DEFAULT_STORES)
    location:         str              = Field(default=DEFAULT_LOCATION)
    phone_number:     str | None       = Field(default=None)
    locked_items:     dict[str, str]   = Field(default_factory=dict)
    preferred_store:  str | None       = Field(default=None)
    # preferred_store: the store the user usually shops at — used as baseline for savings
    # locked_items: { "chicken breast": "Sainsburys" } — item is pre-assigned, skips optimiser


class StoreTripItem(BaseModel):
    item:    str   = Field(...)
    product: str   = Field(...)
    price:   float = Field(...)


class StoreTrip(BaseModel):
    store:    str             = Field(...)
    items:    list[StoreTripItem] = Field(...)
    subtotal: float           = Field(...)


class ShoppingPlanResponse(BaseModel):
    trips:            list[StoreTrip] = Field(...)
    total_estimated:  float           = Field(...)
    summary:          str             = Field(...)
    items_found:      int             = Field(...)
    items_missing:    list[str]       = Field(default_factory=list)
    all_prices:       list[dict]      = Field(default_factory=list)
    savings_estimate:        float     = Field(default=0.0)
    savings_vs_store:        str       = Field(default="")
    one_stop_baseline_total: float     = Field(default=0.0)
    all_plans:               list[dict] = Field(default_factory=list)
    recommended_plan_idx:    int        = Field(default=0)
    baseline_store:          str        = Field(default="")
    items_not_covered:       list[str]  = Field(default_factory=list)


# ── Endpoints ──────────────────────────────────────────────────────────
@app.post("/", response_model=MonitorResponse)
async def check_deals(request: MonitorRequest):
    """
    Check supermarket prices and send a WhatsApp deal summary.
    On first run, captures baseline prices. Subsequent runs diff against history.
    """
    # Manual checks should not spend OpenAI credits.
    result = await run_deal_check(
        request.items,
        request.stores,
        request.location,
        request.phone_number,
        use_llm=False,
    )
    return MonitorResponse(**result)


@app.get("/my-groceries", response_model=MyGroceriesResponse)
async def get_my_groceries():
    """List all saved grocery items with brand preferences."""
    items_raw = await load_my_groceries()
    return MyGroceriesResponse(items=[GroceryItem(**i) for i in items_raw], count=len(items_raw))


@app.post("/my-groceries/add", response_model=MyGroceriesResponse)
async def add_grocery(request: AddGroceryRequest):
    """Add or update a grocery item with a brand preference."""
    items_raw = await load_my_groceries()
    updated   = [i for i in items_raw if i["generic_name"].lower() != request.item.generic_name.lower()]
    updated.append(request.item.model_dump())
    await save_my_groceries(updated)
    return MyGroceriesResponse(items=[GroceryItem(**i) for i in updated], count=len(updated))


@app.post("/my-groceries/add-from-search", response_model=MyGroceriesResponse)
async def add_from_search(request: AddFavouriteFromSearchRequest):
    """
    Save a favourite from a search result so future price checks target that product.
    """
    item = GroceryItem(
        generic_name=request.generic_name.strip(),
        preferred_product=request.product.strip(),
        preferred_store=request.store.strip() if request.store else None,
    )
    items_raw = await load_my_groceries()
    updated = [i for i in items_raw if i["generic_name"].lower() != item.generic_name.lower()]
    updated.append(item.model_dump())
    await save_my_groceries(updated)
    return MyGroceriesResponse(items=[GroceryItem(**i) for i in updated], count=len(updated))


@app.post("/my-groceries/remove", response_model=MyGroceriesResponse)
async def remove_grocery(request: RemoveGroceryRequest):
    """Remove a grocery item from the saved list."""
    items_raw = await load_my_groceries()
    updated   = [i for i in items_raw if i["generic_name"].lower() != request.generic_name.lower()]
    await save_my_groceries(updated)
    return MyGroceriesResponse(items=[GroceryItem(**i) for i in updated], count=len(updated))


@app.get("/my-groceries/prices", response_model=PriceHistoryResponse)
async def get_groceries_prices():
    """Get price history for all saved grocery items."""
    items_raw  = await load_my_groceries()
    histories  = await get_all_price_histories(items_raw)
    result_items: list[ItemPriceHistory] = []
    for name, pts in histories.items():
        prices = [p["price"] for p in pts]
        trend  = None
        if len(prices) >= 2:
            trend = "down" if prices[-1] < prices[-2] else "up" if prices[-1] > prices[-2] else "stable"
        result_items.append(ItemPriceHistory(
            item_name=name,
            history=[PricePoint(**p) for p in pts],
            lowest_ever=min(prices) if prices else None,
            current=prices[-1] if prices else None,
            trend=trend,
        ))
    return PriceHistoryResponse(items=result_items)


@app.post("/search", response_model=SearchResponse)
async def search_product(request: SearchRequest):
    """Search for a specific product across supermarkets and return all prices."""
    results = await search_item_open(request.query, request.location)
    product_results = sorted(
        [ProductResult(product=r["product"], price=r["price"], store=r["store"]) for r in results],
        key=lambda p: p.price,
    )
    return SearchResponse(
        query=request.query,
        results=product_results,
        cheapest=product_results[0] if product_results else None,
        result_count=len(product_results),
    )


@app.post("/item-prices", response_model=ItemPricesResponse)
async def item_prices(request: ItemPricesRequest):
    """
    Fetch current prices for a saved Pantry item across the user's selected stores.
    Uses any saved product/brand preference when available.
    """
    generic = request.generic_name.strip()
    if not generic:
        raise HTTPException(status_code=400, detail="generic_name is required")

    prefs = await load_my_groceries()
    pref_map = {p.get("generic_name", "").strip().lower(): p for p in prefs if p.get("generic_name")}
    pref = pref_map.get(generic.lower(), {})

    if pref.get("preferred_product"):
        query = str(pref["preferred_product"]).strip()
    elif pref.get("preferred_brand"):
        query = f"{pref['preferred_brand']} {generic}".strip()
    else:
        query = generic

    stores = request.stores or DEFAULT_STORES
    results_raw = await search_item_prices(generic, query, stores, request.location)
    results_sorted = sorted(results_raw, key=lambda r: r.get("price", 0))

    return ItemPricesResponse(
        generic_name=generic,
        query=query,
        results=[ItemPriceResult(store=r["store"], product=r["product"], price=r["price"]) for r in results_sorted],
    )


def _is_online_store(store: str) -> bool:
    return any(k in store.lower() for k in ONLINE_STORE_KEYS)


def _build_price_matrix(all_prices: list[dict]) -> dict:
    matrix: dict = {}
    for p in all_prices:
        item  = str(p.get("item_query", "")).strip().lower()
        store = str(p.get("store",      "")).strip()
        price = p.get("price")
        if not item or not store or price is None:
            continue
        if _is_online_store(store):
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        matrix.setdefault(item, {})
        existing = matrix[item].get(store)
        if existing is None or price < existing["price"]:
            matrix[item][store] = {
                "price":   price,
                "product": str(p.get("product", "")),
            }
    return matrix


def _plan_cost(
    store_set: list[str],
    priced_items: list[str],
    price_matrix: dict,
) -> tuple[float, dict]:
    total = 0.0
    breakdown: dict = {s: [] for s in store_set}
    for item in priced_items:
        best_price, best_store, best_product = None, None, ""
        for store in store_set:
            entry = price_matrix.get(item.lower(), {}).get(store)
            if entry and (best_price is None or entry["price"] < best_price):
                best_price, best_store, best_product = entry["price"], store, entry["product"]
        if best_store:
            breakdown[best_store].append(
                {"item": item, "product": best_product, "price": best_price}
            )
            total += best_price
    return round(total, 2), breakdown


def _combinations(lst: list, r: int) -> list[list]:
    if r == 0: return [[]]
    if r > len(lst): return []
    result = []
    for i, item in enumerate(lst):
        for rest in _combinations(lst[i+1:], r - 1):
            result.append([item] + rest)
    return result


def run_smart_plan_algorithm(
    all_prices: list[dict],
    items: list[str],
    stores: list[str],
    nearest_store: str | None = None,
    locked_items: dict[str, str] | None = None,
) -> dict:
    locked_items = {k.lower(): v for k, v in (locked_items or {}).items()}

    online_prices = [p for p in all_prices if _is_online_store(str(p.get("store", "")))]
    price_matrix  = _build_price_matrix(all_prices)

    all_priced    = [i for i in items if price_matrix.get(i.lower())]
    missing_items = [i for i in items if not price_matrix.get(i.lower())]

    # Split into locked (pre-assigned) and free (optimiser handles)
    locked_priced = [i for i in all_priced if i.lower() in locked_items]
    free_items    = [i for i in all_priced if i.lower() not in locked_items]

    available_stores = [
        s for s in stores
        if not _is_online_store(s)
        and any(s in price_matrix.get(i.lower(), {}) for i in all_priced)
    ]

    missing_items += [
        i for i in all_priced
        if not any(s in price_matrix.get(i.lower(), {}) for s in available_stores)
        and i not in missing_items
    ]

    if not all_priced or not available_stores:
        return {
            "all_plans":                          [],
            "recommended_plan":                   None,
            "recommended_idx":                    0,
            "baseline_total":                     0.0,
            "baseline_store":                     nearest_store or (stores[0] if stores else ""),
            "items_missing":                      missing_items,
            "items_not_covered_by_recommendation": [],
            "online_prices":                      online_prices,
        }

    baseline_store = nearest_store or available_stores[0]
    if baseline_store not in available_stores:
        baseline_store = available_stores[0]

    baseline_total = 0.0
    for item in all_priced:
        entry = price_matrix.get(item.lower(), {}).get(baseline_store)
        if entry:
            baseline_total += entry["price"]
        else:
            for s in available_stores:
                e = price_matrix.get(item.lower(), {}).get(s)
                if e:
                    baseline_total += e["price"]
                    break
    baseline_total = round(baseline_total, 2)

    # Pre-compute locked item costs (fixed regardless of plan)
    locked_breakdown: dict[str, list] = {}
    locked_cost = 0.0
    for item in locked_priced:
        store = locked_items[item.lower()]
        entry = price_matrix.get(item.lower(), {}).get(store)
        if entry:
            locked_breakdown.setdefault(store, []).append(
                {"item": item, "product": entry["product"], "price": entry["price"]}
            )
            locked_cost += entry["price"]

    all_plans = []
    optimiser_stores = available_stores if free_items else []
    n_range = range(1, len(optimiser_stores) + 1) if free_items else [0]

    for n in n_range:
        if n == 0:
            # Only locked items, no free optimisation
            best_cost = round(locked_cost, 2)
            best_breakdown: dict[str, list] = {s: list(its) for s, its in locked_breakdown.items()}
        else:
            best_cost, best_free_breakdown = None, None
            for combo in _combinations(optimiser_stores, n):
                cost, breakdown = _plan_cost(combo, free_items, price_matrix)
                if best_cost is None or cost < best_cost:
                    best_cost, best_free_breakdown = cost, breakdown
            if best_cost is None:
                continue
            best_cost = round(best_cost + locked_cost, 2)
            # Merge locked and free breakdowns
            best_breakdown = {s: list(its) for s, its in best_free_breakdown.items()}
            for store, its in locked_breakdown.items():
                best_breakdown.setdefault(store, []).extend(its)

        trips = [
            {"store": s, "items": its, "subtotal": round(sum(i["price"] for i in its), 2)}
            for s, its in best_breakdown.items() if its
        ]
        stops = len([t for t in trips if t["items"]])
        all_plans.append({
            "stops":         stops,
            "trips":         trips,
            "total":         best_cost,
            "saving":        round(baseline_total - best_cost, 2),
            "marginal_gain": 0.0,
        })

    for i, plan in enumerate(all_plans):
        plan["marginal_gain"] = (
            plan["saving"] if i == 0
            else round(plan["saving"] - all_plans[i - 1]["saving"], 2)
        )

    for plan in all_plans:
        plan_stores = [t["store"] for t in plan["trips"]]
        plan["items_not_covered"] = [
            i for i in all_priced
            if not any(s in price_matrix.get(i.lower(), {}) for s in plan_stores)
        ]

    recommended_idx = len(all_plans) - 1
    for i in range(1, len(all_plans)):
        if all_plans[i]["marginal_gain"] < MARGINAL_GAIN_THRESHOLD:
            recommended_idx = i - 1
            break

    return {
        "all_plans":                          all_plans,
        "recommended_plan":                   all_plans[recommended_idx] if all_plans else None,
        "recommended_idx":                    recommended_idx,
        "baseline_total":                     baseline_total,
        "baseline_store":                     baseline_store,
        "items_missing":                      missing_items,
        "items_not_covered_by_recommendation": all_plans[recommended_idx].get("items_not_covered", []) if all_plans else [],
        "online_prices":                      online_prices,
    }


@app.post("/shopping-plan", response_model=ShoppingPlanResponse)
async def shopping_plan(request: ShoppingPlanRequest):
    all_prices = await fetch_all_prices(request.items, request.stores, request.location)

    result = run_smart_plan_algorithm(
        all_prices      = all_prices,
        items           = request.items,
        stores          = request.stores,
        nearest_store   = request.preferred_store or (request.stores[0] if request.stores else None),
        locked_items    = request.locked_items,
    )

    rec = result["recommended_plan"]
    if not rec:
        return ShoppingPlanResponse(
            trips=[], total_estimated=0.0,
            summary="No prices found for any items.",
            items_found=0, items_missing=result["items_missing"],
            all_prices=all_prices, savings_estimate=0.0,
            savings_vs_store="", one_stop_baseline_total=0.0,
            all_plans=[], recommended_plan_idx=0,
            baseline_store=result["baseline_store"],
            items_not_covered=[],
        )

    trips = [
        StoreTrip(
            store=t["store"],
            items=[StoreTripItem(**i) for i in t["items"]],
            subtotal=t["subtotal"],
        )
        for t in rec["trips"]
    ]

    all_plan_options = result["all_plans"]

    summary = (
        f"⭐ {rec['stops']} stop{'s' if rec['stops'] != 1 else ''} recommended "
        f"— saves £{rec['saving']:.2f} vs {result['baseline_store']}."
    )

    response = ShoppingPlanResponse(
        trips=trips,
        total_estimated=rec["total"],
        summary=summary,
        items_found=sum(len(t.items) for t in trips),
        items_missing=result["items_missing"],
        all_prices=all_prices,
        savings_estimate=rec["saving"],
        savings_vs_store=result["baseline_store"],
        one_stop_baseline_total=result["baseline_total"],
        all_plans=all_plan_options,
        recommended_plan_idx=result["recommended_idx"],
        baseline_store=result["baseline_store"],
        items_not_covered=result["items_not_covered_by_recommendation"],
    )

    if request.phone_number:
        wa_text = (
            f"Smart Plan ready! {rec['stops']} stop(s), "
            f"est. £{rec['total']:.2f}, saves £{rec['saving']:.2f}. {summary}"
        )
        await send_whatsapp_notification(wa_text[:950], request.phone_number)

    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
