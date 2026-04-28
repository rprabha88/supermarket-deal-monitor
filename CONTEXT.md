# GrocerySmart — Full Project Brief
*Last updated: April 2026 · Paste this at the start of any new session to restore full context.*

---

## 1. What we're building

A UK grocery price comparison and smart shopping planner. The core value proposition: help everyday UK shoppers spend less on groceries with minimal effort, by combining price comparison, smart multi-store planning, online vs in-store routing, and purchase tracking in one app.

**Target market:** UK consumers, starting with London. Primary persona: someone who already shops at a default store (e.g. Sainsbury's) but doesn't have time to manually compare prices across stores.

**Current build status:** Functional prototype built in Claude Code. UI has been redesigned and a full algorithm logic document has been produced. The next step is restructuring the Claude Code project around the new architecture and algorithm.

---

## 2. App structure — 4 tabs

### Tab 1 — My List
The core entry point. An interactive checklist (not a textarea). Users add items individually or paste a multi-line list (smart paste detects multiple items and shows a confirmation preview before adding). Each item has:
- A checkbox (tick off as bought in-store)
- A quantity stepper (defaults to 1)
- A per-item store preference (optional — tap chevron to set)
- An urgency tag: **today** (in-store only) or **flexible** (can be delivered)
- Retailer toggle buttons for online-eligible items (Amazon, Ocado)

Items ticked move to an "In Basket" section at the bottom. Store selector chips at the bottom pre-populate from the user's saved nearby stores. Primary CTA: "Get Smart Plan →"

### Tab 2 — Smart Plan
The primary output screen. Shows the optimised shopping plan grouped by store, with:
- A savings hero card (£X saved vs baseline, N stops)
- Mode toggle: Cheapest / Fewest Stops
- All plan options ranked (full curve, not just two extremes) — user picks any
- Recommended plan highlighted (⭐)
- Items grouped by store, each tappable to tick off in-store
- Per-unit prices shown on each item
- When all items at a store are ticked → inline prompt to record spend
- Online order section below in-store plan (if user moved items online)
- "Send to WhatsApp" button

### Tab 3 — Price Tracker
Two views toggled by a button:
- **Search view:** text input, searches across in-store + online (Amazon, Ocado). Results sorted cheapest first with per-unit prices. Filter pills: All / In-store / Online. Loyalty card prices shown where applicable. "+ Track" pins an item.
- **Pinned view:** list of tracked items with sparkline chart. Tap to expand full price history chart. Shows current price, lowest ever, and per-unit comparison.

### Tab 4 — Me
Accordion sections:
- **Spending History** — total saved hero + session list (estimated vs confirmed)
- **My Item Preferences** — item → store mappings, add/remove
- **Receipt Scanner** — camera upload for pantry management (OCR)
- **Loyalty Cards** — toggle switches (Clubcard, Nectar, More Card, myWaitrose)
- **Location & Stores** — text input + nearby store checklist ranked by distance
- **Delivery Subscriptions** — Amazon Prime toggle, Ocado Smart Pass toggle
- **WhatsApp Alerts** — phone number, daily deal alert time

---

## 3. User journey

**Stage 1 — Meal planning** *(future)*
User picks meals, app extracts ingredient list.

**Stage 2 — Pantry check** *(future — OCR receipt scanning)*
App cross-references what's already in stock. Learns purchase frequency (e.g. buys milk every 3 days) and auto-suggests restocking. Receipt scanning is the passive input mechanism — no manual pantry management.

**Stage 3 — Shopping list** *(MVP focus)*
User builds or pastes their list. Sets quantities. Tags items as today/flexible. Optionally sets store/brand preferences per item.

**Stage 4 — Smart Plan** *(MVP focus)*
App generates optimised plan. User can review all options (cheapest, fewest stops, mixed online/in-store). Adjusts and accepts.

**Stage 5 — Shop & tick off**
In-store checklist grouped by store. Items tick off as bought. When a store is complete, app prompts to record spend.

**Stage 6 — Price tracking** *(passive)*
Every session feeds price history. Alerts when staples go on sale.

---

## 4. Smart Plan algorithm — full logic

### 4.1 Baseline (per item)

The baseline represents what the user would have spent *without the app* — i.e. if they had just gone to their usual store and bought everything there.

**The baseline store is the user's self-declared usual store**, set during onboarding ("Which supermarket do you usually shop at?") and editable in the Me tab. This is the honest answer to "what would you have spent otherwise?"

| Situation | Baseline price used |
|---|---|
| Item stocked at user's usual store | That store's price |
| Item not stocked at usual store | Cheapest price at any other available store (fallback) |
| Item only available online | Online price — no in-store baseline exists |

**Key rule:** The baseline is never shown to the user as "vs [store name]". The savings figure is presented simply as money saved — the comparison store is an internal calculation only.

**Key rule:** Ocado/Amazon prices are never used as the baseline if an in-store option exists — even if they're cheaper. The in-store price is what the user would have paid by default.

**Onboarding dependency:** The usual store must be set before savings can be meaningful. Onboarding asks for this on first launch. If skipped, the app falls back to the first store in the user's selected list.

### 4.2 Item locks (availability vs preference)

Two types of locks — treated differently:

**Availability locks** — item is only stocked at one store (e.g. SO Organic Chicken at Sainsbury's, specialist brands). The algorithm assigns these before the optimiser runs. They are a fixed cost in every plan.

**Preference locks** — user has explicitly chosen to always buy an item from a specific store (e.g. "Chicken from Sainsbury's only"). Also assigned before the optimiser, but the user can override them.

Both types are removed from the optimisation pool. All other items are optimised freely.

### 4.3 Store-level preference vs item-level preference

**Item-level:** "I only buy X from store Y." Item is locked to that store regardless of price.

**Store-level:** "I prefer to shop at Sainsbury's generally." The app calculates the cost of shopping entirely at the preferred store and compares it to the recommended plan:

| Cost difference | App behaviour |
|---|---|
| Preferred store costs less than £X more | Suggest preferred store with small premium noted |
| Preferred store costs significantly more | Show smart plan as primary, preferred store as alternative with gap labelled |

The threshold (£X) defaults to **£2.50** and is user-configurable: "Suggest multi-stop only if I'd save more than £[N]."

### 4.4 The optimisation algorithm

**Step 1 — Price matrix**
For every item × every store, find the effective price (loyalty price if user holds that card). Locked items are excluded.

**Step 2 — Generate all N-stop plans**
For N = 1, 2, 3... up to the number of available stores:
- Try all combinations of N stores
- For each combination, assign each free item to the cheapest available store in that set
- Add fixed costs of locked items to every plan
- Keep the best (lowest cost) combination for each N

**Step 3 — Calculate marginal gains**
For each step from N to N+1, calculate how much extra is saved by adding that stop.

**Step 4 — Find the recommended plan**
The recommended plan is where the marginal gain of adding one more stop drops below **£1.00**. This is the "knee of the curve" — beyond this point, extra stops aren't worth the effort.

**Step 5 — Present the full curve**
Show all viable plans ranked, each with total cost, saving vs baseline, and number of stops. The recommended plan is highlighted. User can tap any plan to adopt it.

### 4.5 Stop counting
The stop count shown to the user always reflects the **total number of physical stores they need to visit** — not the number of stores the optimiser iterated over. Availability-locked stores count as stops too.

### 4.6 Quantities

Every item carries a quantity (default 1). All price calculations multiply by quantity before any comparison is made.

**For bulk-only online products (e.g. Amazon multipacks):**
```
packs_needed  = ceil(quantity / pack_size)
online_total  = packs_needed × pack_price
instore_equiv = quantity × instore_unit_price
overshoot     = (packs_needed × pack_size) − quantity
saving        = instore_equiv − online_total
```

**Case A — saving ≥ 0 (bulk is cheaper or equal in absolute terms):**
Recommend online. Show saving and overshoot ("7 units spare, ~3 weeks at your rate").

**Case B — saving < 0 (bulk costs more upfront):**
This is a **pre-buy decision**, not a saving. Surface separately: "Costs £5.74 more today, but your next 9 units are pre-bought at £1.41 vs £3.75." Never count as a Type 2 saving.

**Multi-buy deals (in-store):**
- If user's quantity already qualifies: apply automatically
- If quantity is below threshold: flag it — "Buy 1 more to trigger the deal, saves £0.40"
- Never suggest buying extra of a perishable for a deal

### 4.7 Three saving types — tracked separately

**Type 1 — Store switching (in-store)**
```
Saving = Σ (baseline_price − in_store_plan_price) × qty
         for all in-store items
```

**Type 2 — Online switching**
```
Gross saving = Σ (baseline_price × qty − online_price)
               for all online-committed items
Net saving   = gross saving − delivery_fee per retailer
```
Only counted as a saving if net saving > 0. Pre-buy decisions are excluded entirely.

**Type 3 — Loyalty cards**
```
Saving = Σ (standard_price − loyalty_price) × qty
         where user holds the relevant card
```
Applied to both baseline and plan. Shown explicitly per item.

### 4.8 Mixed basket (online + in-store combined)

When the user commits some items to online orders and the rest to in-store:

```
Total saving = baseline_total
             − in_store_plan_total      (online items excluded)
             − online_order_totals      (pack prices × packs needed)
             − delivery_fees            (one per online retailer)
```

Online-committed items are removed from the in-store optimiser before it runs. Availability-locked items can never be moved online.

**Delivery fee logic:**
- Amazon Prime: £0
- Ocado Smart Pass: £0
- "I already have an order running": £0 marginal cost (declared in settings)
- Default: £3.99 per retailer

The delivery fee is deducted once per online retailer, not per item. The optimal online basket is the set of items where gross online saving > delivery fee.

### 4.9 Unit price normalisation

All price comparisons are made on a **normalised per-unit basis**, never on total pack price.

| Category | Comparison unit |
|---|---|
| Drinks, liquids | per 100ml |
| Food sold by weight | per 100g |
| Countable household items | per item / sheet |
| Teabags, pods, sachets | per bag / pod |
| Online multipacks | per single-serve equivalent |

**Non-perishables:** per-unit price is the definitive comparison. Larger pack = better value if per-unit price is lower.

**Perishables:** per-unit price is only valid if the user will consume all of it before expiry. Cross-reference purchase frequency (from receipt history) against pack size and shelf life. Flag if likely to waste.

**Budget vs value tension:** Where the cheapest total price and best per-unit price point to different products, show both explicitly — never hide one.

### 4.10 Online stores — rules

- Online stores are **never** part of the in-store optimisation
- Online stores are **never** used as the baseline if an in-store option exists
- After the in-store plan is finalised, a second pass checks online prices for flexible items
- Online suggestions are shown as a separate layer — user explicitly accepts/rejects
- Accepted online items are removed from the in-store plan and added to an online order section
- Bulk online deals show per-unit price AND upfront commitment clearly
- Pre-buy decisions (bulk costs more upfront) are shown separately, never as savings

### 4.11 Plan verification and savings accuracy

**The problem:** The app can't know if the user actually followed the plan.

**MVP approach — estimated vs confirmed:**
- Plan savings are recorded as **estimated** immediately (motivating, shows potential)
- Savings become **confirmed** when the user scans a receipt from the relevant store
- Spending history shows both states distinctly

**Receipt scanning (the upgrade path):**
- User photographs receipts after shopping
- OCR extracts: store, items, prices, date
- App reconciles against the plan
- Feeds into purchase history, pantry restock logic, and consumption rate learning

**What this means for savings figures:**
- Total confirmed savings = receipts scanned and reconciled
- Total estimated savings = plans generated but not yet verified
- Both shown in the Me tab with clear labelling

### 4.12 Item identification in-store

**Minimum required per item:** brand, variant, weight/size, price. This is what the user needs to find it on the shelf.

**Substitution fallback:** pre-calculate second-best option per item. "If this isn't available, get [X] at £Y instead."

**No preference items:** the app recommends the cheapest product in the category. User can override with a brand preference.

**Future:** barcode scanning to confirm product match in-store.

---

## 5. Data model

```
ShoppingItem:
  id, name, done, urgency ("today" | "flexible"),
  quantity: number,
  pref: { store } | null,
  unit_type: "volume" | "weight" | "count" | "serve",
  unit_size: number, unit_label: string,
  perishable: boolean, shelf_life_days: number

Product:
  id, name, brand, weight, category,
  pack_size: number,
  perishable: boolean, shelf_life_days: number,
  multibuy_qty: number, multibuy_price: number

Pricing:
  product_id, store_id, date,
  standard_price, loyalty_price, per_unit_price,
  online_pack_size, online_pack_total

StoreSession:
  id, date, stores[], items, total, saved,
  status: "estimated" | "confirmed",
  receipt_id: string | null

TrackedItem:
  id, name, store, price, low, trend, delta,
  history: PricePoint[],
  loyalty_price: number | null

UserPurchaseHistory:
  item_id, avg_days_between_purchase,
  avg_quantity_per_purchase

UserSettings:
  location, nearby_stores[],
  preferred_store: string | null,
  multistop_threshold: number (default £2.50),
  loyalty_cards: string[],
  amazon_prime: boolean,
  ocado_smart_pass: boolean,
  default_delivery_fee: number,
  whatsapp_number: string,
  alert_time: string
```

---

## 6. Open UX questions (to resolve before building)

These decisions have been raised but not yet finalised:

**Plan verification:**
- [ ] MVP shows estimated savings only — receipt scanning is a future feature. Agreed in principle, not yet implemented.
- [ ] What does the "record spend" flow look like after completing a store? Simple confirm button, or itemised review?

**Item selection in-store:**
- [ ] What level of detail does the app show per item — brand + weight, or category only for no-preference items?
- [ ] How does the user handle an out-of-stock item while shopping? Does the app suggest a substitution in the plan?
- [ ] Should items be grouped by aisle/category within a store in the in-store checklist?

**Online routing:**
- [ ] When a user accepts an online suggestion, does it immediately update the Smart Plan screen, or does it stay as a suggestion until they explicitly confirm?
- [ ] How does the app handle Ocado minimum order requirements for free delivery?

**Receipt scanning:**
- [ ] Confirmed as a future feature for pantry management, but also needed for savings verification. Build together or separately?

---

## 7. Current Claude Code build

The app was initially built in Claude Code with the following structure (now being refactored):

**Old tabs:** Shopping List, Pantry, Price Watch, Settings
**Issues with old build:**
- Shopping list used a textarea instead of an interactive checklist
- Pantry and Price Watch were confusing and overlapping in purpose
- Smart Plan had no dedicated output screen
- No quantities, no urgency tags, no online routing
- Settings was a flat list, not structured

**New architecture** (to be implemented):
- 4 tabs: My List, Smart Plan, Price Tracker, Me
- Full algorithm as documented in Section 4
- Interactive checklist with quantity steppers
- Smart paste for pasting multi-line lists
- Per-item store preferences
- Online/in-store routing with delivery cost logic
- Mixed basket savings calculation
- Receipt scanner placeholder (coming soon)

**Reference artifacts from this session:**
- `algorithm_worked_example` — interactive React prototype showing the full algorithm (Tabs: Items & Quantities, In-Store Plans, Online + Delivery, Final Result)
- `smart_plan_logic` — full algorithm decision log
- This document (`project_brief`) — full project context

---

## 8. Handoff brief for Claude Code

When starting a Claude Code session, use this prompt:

```
I'm building a UK grocery price comparison and shopping 
planner app. Before making any changes, read all files and 
give me a map of the current structure (components, routing, 
state management, data layer).

Here is the full product and algorithm context:
[paste this document]

Today I want to work on: [specific task]

Rules for this session:
- Make one change at a time and confirm it works before continuing
- Do not batch multiple structural changes together
- Follow the data model in Section 5 exactly
- All price comparisons must use per-unit normalisation (Section 4.9)
- Online stores must never be part of the in-store optimisation (Section 4.10)
```

**Suggested implementation order:**
1. Create new 4-tab routing structure with empty screens
2. Migrate My List — interactive checklist + smart paste + quantity steppers
3. Build Smart Plan screen — plan generation, store grouping, spend recording
4. Rebuild Price Tracker — search + pinned views + online results
5. Build Me tab — accordion sections, spending history, receipt scanner placeholder
6. Wire shared state — preferences, loyalty cards, delivery settings, spend history
7. Implement the Smart Plan algorithm (Section 4)
8. Delete old Pantry and Price Watch tabs