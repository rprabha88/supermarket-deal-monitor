#!/usr/bin/env python3
"""
Supermarket Deal Monitor
Daily UK supermarket price tracker with WhatsApp alerts via Twilio.
"""

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
from fastapi import FastAPI
from openai import AsyncOpenAI
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


# ── Redis helpers ──────────────────────────────────────────────────────
def get_redis() -> aioredis.Redis:
    return aioredis.from_url(REDIS_URL, decode_responses=True)


def ns(key: str) -> str:
    """Namespace a Redis key."""
    return f"{REDIS_NAMESPACE}:{key}"


# ── My Groceries persistence ───────────────────────────────────────────
async def load_my_groceries() -> list[dict]:
    async with get_redis() as r:
        raw = await r.get(ns(MY_GROCERIES_KEY))
        return json.loads(raw) if raw else []


async def save_my_groceries(items: list[dict]) -> None:
    async with get_redis() as r:
        await r.set(ns(MY_GROCERIES_KEY), json.dumps(items))


async def append_price_history(item_name: str, price: float, store: str, product: str) -> None:
    """Append a price data-point to per-item history (max 30 entries)."""
    async with get_redis() as r:
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
    async with get_redis() as r:
        key = ns(f"price_history:{item_name.lower().replace(' ', '_')}")
        raw = await r.get(key)
        return json.loads(raw) if raw else []


async def get_all_price_histories(items: list[dict]) -> dict:
    result: dict = {}
    for item in items:
        name = item.get("generic_name", "")
        result[name] = await get_price_history(name)
    return result


def resolve_search_queries(items: list[str], preferences: list[dict]) -> list[str]:
    """Replace generic item names with preferred brand queries where set."""
    pref_map = {p["generic_name"].lower(): p for p in preferences}
    resolved: list[str] = []
    for item in items:
        pref = pref_map.get(item.strip().lower())
        if pref and pref.get("preferred_brand"):
            resolved.append(f"{pref['preferred_brand']} {item}")
        else:
            resolved.append(item)
    return resolved


# ── Step 1: Search prices via SearchAPI (Google Shopping) ──────────────
async def search_item_prices(item: str, stores: list[str], location: str) -> list[dict]:
    """Search Google Shopping for a grocery item, filtered to target stores."""
    store_query = " ".join(stores)
    params = {
        "engine": "google_shopping",
        "q": f"{item} {store_query}",
        "gl": "gb",
        "hl": "en",
        "num": 15,
        "location": location,
        "api_key": SEARCHAPI_KEY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(SEARCHAPI_URL, params=params)
        response.raise_for_status()
        data = response.json()

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
                "item_query": item,
                "product": title,
                "price": float(price),
                "store": matched_store,
            })
    return results


async def search_item_open(query: str, location: str) -> list[dict]:
    """Search Google Shopping without store filtering — returns all results."""
    params = {
        "engine": "google_shopping",
        "q": f"{query} grocery UK",
        "gl": "gb",
        "hl": "en",
        "num": 20,
        "location": location,
        "api_key": SEARCHAPI_KEY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(SEARCHAPI_URL, params=params)
        response.raise_for_status()
        data = response.json()

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
    logger.info("Fetching prices", item_count=len(items) if hasattr(len, '__call__') else len(items))
    tasks = [search_item_prices(item, stores, location) for item in items]
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
    async with get_redis() as r:
        raw = await r.get(ns("grocery_prices:latest"))
        if raw:
            previous = json.loads(raw)
            logger.info(f"Previous prices loaded ({len(previous.get('prices', []))} products)")
            return previous
        logger.info("No previous prices — this is the first run")
        return {}


async def store_current_prices(prices: list[dict]) -> None:
    async with get_redis() as r:
        await r.set(
            ns("grocery_prices:latest"),
            json.dumps({"prices": prices, "timestamp": datetime.now().isoformat()}),
        )
    logger.info(f"Stored {len(prices)} current prices")


# ── Step 3: LLM deal analysis ──────────────────────────────────────────
async def analyze_deals(
    current_prices: list[dict], previous_data: dict, items: list[str]
) -> str:
    logger.info("Analysing deals with OpenAI")

    previous_prices = previous_data.get("prices", [])
    prev_timestamp  = previous_data.get("timestamp", "N/A")

    prompt = f"""You are a savvy UK grocery shopping assistant. Analyse today's supermarket prices and find the best deals for the shopper.

Today's prices ({datetime.now().strftime('%d %B %Y')}):
{json.dumps(current_prices, indent=2)}

Previous prices (from {prev_timestamp}):
{json.dumps(previous_prices, indent=2) if previous_prices else 'No previous data – this is the first check.'}

For each grocery item the user tracks ({', '.join(items)}):
1. Find the cheapest option across Tesco, Sainsburys, and Iceland.
2. If previous prices exist, highlight any price DROPS (sales/deals).
3. Flag any notably good value.

Respond with a concise, mobile-friendly plain-text summary (no markdown, no special chars). Use emoji sparingly. Keep it under 900 characters total so it fits in a WhatsApp message. Start with a greeting line, then list the best deals/prices."""

    client   = AsyncOpenAI()
    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
    )

    summary = response.choices[0].message.content.strip()
    summary = summary.replace("**", "").replace("*", "").replace("_", "").replace("`", "")
    summary = " ".join(summary.split())
    if len(summary) > 950:
        summary = summary[:947] + "..."

    logger.info(f"Deal analysis complete ({len(summary)} chars)")
    return summary


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
    phone_number: str,
) -> dict:
    """Fetch prices, analyse deals, persist, and notify. Returns result dict."""
    current_prices = await fetch_all_prices(items, stores, location)
    previous_data  = await load_previous_prices()
    is_first_run   = len(previous_data) == 0

    summary = await analyze_deals(current_prices, previous_data, items)

    await store_current_prices(current_prices)

    # Persist per-item price history for saved favourites
    saved_items  = await load_my_groceries()
    saved_names  = {i["generic_name"].lower() for i in saved_items}
    for p in current_prices:
        if p.get("item_query", "").lower() in saved_names:
            await append_price_history(p["item_query"], p["price"], p["store"], p["product"])

    await send_whatsapp_notification(summary, phone_number)

    return {
        "items_tracked":   len(items),
        "products_found":  len(current_prices),
        "deals_summary":   summary,
        "is_first_run":    is_first_run,
        "message": (
            "Deal check complete! WhatsApp alert sent."
            if not is_first_run
            else "First run complete – baseline prices captured. Daily monitoring scheduled at 08:00 UTC."
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
    await run_deal_check(DEFAULT_ITEMS, DEFAULT_STORES, DEFAULT_LOCATION, DEFAULT_PHONE_NUMBER)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(
        scheduled_deal_check,
        CronTrigger(hour=8, minute=0),
        id="daily_deal_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — daily check at 08:00 UTC")
    yield
    scheduler.shutdown()
    logger.info("Scheduler stopped")


# ── FastAPI app ────────────────────────────────────────────────────────
app = FastAPI(
    title="Supermarket Deal Monitor",
    description="Daily UK supermarket grocery deal monitor with WhatsApp alerts",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Pydantic models ────────────────────────────────────────────────────
class MonitorRequest(BaseModel):
    phone_number: str = Field(default="447442801959", example="447442801959")
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


class ShoppingPlanRequest(BaseModel):
    items:        list[str]   = Field(...)
    stores:       list[str]   = Field(default=DEFAULT_STORES)
    location:     str         = Field(default=DEFAULT_LOCATION)
    phone_number: str | None  = Field(default=None)


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


# ── Endpoints ──────────────────────────────────────────────────────────
@app.post("/", response_model=MonitorResponse)
async def check_deals(request: MonitorRequest):
    """
    Check supermarket prices and send a WhatsApp deal summary.
    On first run, captures baseline prices. Subsequent runs diff against history.
    """
    result = await run_deal_check(
        request.items, request.stores, request.location, request.phone_number
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


@app.post("/shopping-plan", response_model=ShoppingPlanResponse)
async def shopping_plan(request: ShoppingPlanRequest):
    """
    Submit a shopping list and get an optimised store-by-store plan.
    Balances cheapest prices with fewest store trips.
    """
    preferences    = await load_my_groceries()
    search_queries = resolve_search_queries(request.items, preferences)
    all_prices     = await fetch_all_prices(search_queries, request.stores, request.location)
    plan           = await build_shopping_plan(all_prices, request.items, request.stores)

    trips = [
        StoreTrip(
            store=t["store"],
            items=[StoreTripItem(**i) for i in t["items"]],
            subtotal=t["subtotal"],
        )
        for t in plan.get("trips", [])
    ]

    result = ShoppingPlanResponse(
        trips=trips,
        total_estimated=plan.get("total_estimated", 0),
        summary=plan.get("summary", ""),
        items_found=sum(len(t.items) for t in trips),
        items_missing=plan.get("items_missing", []),
    )

    if request.phone_number:
        wa_text = f"Shopping plan ready! {len(trips)} store(s), est. total £{result.total_estimated:.2f}. {result.summary}"
        await send_whatsapp_notification(wa_text[:950], request.phone_number)

    return result


async def build_shopping_plan(all_prices: list[dict], items: list[str], stores: list[str]) -> dict:
    """Use OpenAI to build an optimised store-by-store shopping plan."""
    prompt = f"""You are a smart UK grocery shopping planner. Given the price data below, create an optimised shopping plan that BALANCES cheapest prices with fewest store trips.

Rules:
- If one store is only slightly more expensive (under 20p per item), prefer buying there to avoid an extra trip.
- Group items by store to minimise trips.
- Every item on the list MUST appear in exactly one store trip.
- If an item has no price data, include it in "items_missing".

Shopping list: {json.dumps(items)}
Available stores: {json.dumps(stores)}

Price data:
{json.dumps(all_prices, indent=2)}

Respond ONLY with valid JSON (no markdown fences):
{{
  "trips": [
    {{
      "store": "Tesco",
      "items": [
        {{"item": "Chicken Breast", "product": "Tesco British Chicken Breast", "price": 4.65}}
      ],
      "subtotal": 4.65
    }}
  ],
  "total_estimated": 12.50,
  "items_missing": [],
  "summary": "One-sentence shopping advice."
}}"""

    client   = AsyncOpenAI()
    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200,
        response_format={"type": "json_object"},
    )

    return json.loads(response.choices[0].message.content.strip())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
