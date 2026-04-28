# GrocerySmart — Temporary Changes Tracker
*Last updated: April 14 2026*

This document tracks every temporary change made during development sessions that must be reverted or properly implemented before the app goes to production / App Store.

---

## 🔴 Must revert before production

### 1. Mock price data in `fetch_all_prices()` — `main.py`
**Status:** Active (added April 14 2026)
**Why added:** SearchAPI free tier quota exhausted for the month
**What it does:** At the top of `fetch_all_prices()`, a hardcoded `MOCK_PRICES` list returns fake prices for Bananas, Bread, Milk, Chicken, Pasta, Eggs at Tesco / Sainsburys / Aldi. If any requested item matches, it returns mock data and skips the real SearchAPI call entirely.
**What to do:**
- [ ] Upgrade SearchAPI plan at searchapi.io (paid plans from ~$15/month), OR wait for monthly quota reset
- [ ] Delete the entire `MOCK_PRICES` block and the `if mock: return mock` lines from `fetch_all_prices()`
- [ ] Test with a real curl call to confirm live prices are returned

---

## 🟡 Temporary architecture decisions (need proper implementation)

### 2. `nearest_store` hardcoded to `request.stores[0]` — `main.py`
**Status:** Active (added April 14 2026)
**Why:** No user location/nearest store resolution is implemented yet
**What it does:** The `/shopping-plan` endpoint passes `request.stores[0]` as `nearest_store` to the algorithm. This means the baseline (§4.1) is calculated against whichever store the frontend sends first, not the user's actual nearest store.
**What to do:**
- [ ] Add a `/nearest-store` endpoint or resolve nearest store from `UserSettings.location` + `nearby_stores[]`
- [ ] Pass the resolved nearest store into `run_smart_plan_algorithm()`

### 3. OpenAI dependency still in `requirements.txt` and imports
**Status:** Dormant (not called by shopping-plan anymore, but still imported)
**Why:** `build_shopping_plan()` was deleted but `from openai import AsyncOpenAI` and the `analyze_deals()` function still use it
**What to do:**
- [ ] Decide if OpenAI is still needed for the deal summary feature
- [ ] If not, remove from imports and `requirements.txt` to reduce dependencies

---

## ✅ Permanent changes made this session (no revert needed)

### 4. Replaced GPT-based `build_shopping_plan()` with `run_smart_plan_algorithm()`
**Status:** Permanent — this is the correct implementation
**What changed:** Deleted the LLM prompt-based planner. Replaced with the full §4.1–4.10 algorithm: per-item baseline, N-stop optimiser, marginal gain calculation, knee-of-curve recommendation, online/in-store separation.

### 5. Deleted `_compute_savings_vs_one_stop()` 
**Status:** Permanent — savings now calculated inside `run_smart_plan_algorithm()`

### 6. Added `PlanOption` Pydantic model and `all_plans` / `recommended_plan_idx` / `baseline_store` fields to `ShoppingPlanResponse`
**Status:** Permanent — frontend will use these to render the full plan curve

---

## 📋 Still to build this session

- [ ] Confirm algorithm test passes with mock data
- [ ] Update `index.html` frontend to consume `all_plans[]` and render the full curve (Step 2)
- [ ] Export project structure for Claude Code App Store build path

---

*Paste this document at the start of any new session to restore context on what's temporary vs permanent.*
