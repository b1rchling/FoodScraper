# Willys → Google Sheets: scan an EAN, get the article number (+ macros)

Scrape Willys's food catalog with **Python**, then your dad's Google Sheet looks up a
scanned barcode (EAN) and returns the **article number**, product name, and macros.

- **Scraper:** [`willys_scraper.py`](willys_scraper.py) — pure standard library, no `pip install`.
- **Data:** [`willys_index.csv`](willys_index.csv) — ~7,648 food products, hosted for the sheet.
- **Legacy (Apps Script version):** [`willys-macros.gs`](willys-macros.gs) + its HANDOFF/TODO.
  Same idea, but the crawl runs inside Google Sheets. Superseded by the Python script.

---

## Why we have to scrape (there is no EAN → article shortcut)

Verified against the live Willys API (June 2026):

| Attempt | Result |
|---|---|
| Search by **EAN** (`/search/clean?q=<ean>`) | `results: null` — search ignores barcodes; results don't even carry an `ean` field |
| Product detail by **EAN** (`/axfood/rest/p/<ean>`) | HTTP 400 "No product found" — needs the internal **code**, not the EAN |
| `/axfood/rest/products/ean/<ean>` | HTTP 200 but always empty `items: []`, even with cookies+CSRF — gated behind a logged-in store session |
| Category **browse** listing | carries `code` but **never** the `ean` |

The EAN is exposed **only** inside each product's detail response. So we crawl every food
product's detail (`code → ean + nutrition`), cache it, and look it up by EAN in the sheet.

---

## 1. Run the scraper

```bash
python willys_scraper.py                # crawl (resumable) + write csv/json
python willys_scraper.py --build-only   # just rebuild csv/json from the cache (instant)
python willys_scraper.py --off          # also fill gaps from Open Food Facts (slow, opt-in)
python willys_scraper.py --limit 50     # quick smoke test (first 50 products)
python willys_scraper.py --fresh        # ignore the resume cache, re-crawl everything
```

It writes, next to the script:

- **`willys_index.csv`** ← the lookup table for Google Sheets
- `willys_index.json` ← same data as JSON (git-ignored)
- `.willys_cache.jsonl` ← resume cache. The crawl is **resumable** — if Willys rate-limits
  you (HTTP 403), just re-run and it fetches only what's missing. Delete it (or `--fresh`)
  to start over.

The crawl is polite by default (4 workers, small delay) and **retries on 403/429** rather
than dropping products. Full catalog ≈ 7,600 products, ~10–20 min.

**Columns** (`A`→`Q`):

| A | B | C | D | E | F | G | H | I | J | K | L | M | N | O | P | Q |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ean | article | name | altText | brand | price | basis | kcal | kj | fat | satfat | carb | sugar | fibre | protein | salt | source |

`article` is the Willys article number (e.g. `101278894_ST`). `altText` is the richer name
that includes size/volume (e.g. `Trocadero Zero Sugar Läsk Pet 1,5l Trocadero`). Macros are
per 100 g/ml. `source` = `willys`, `off` (filled from Open Food Facts), or empty (no macros found).

---

## 2. Get it into your dad's Google Sheet

> **Heads-up:** Google Sheets has **no native JSON import** — its only URL-pull functions are
> `IMPORTDATA` (CSV/TSV), `IMPORTXML`, `IMPORTHTML`, `IMPORTFEED`. So we use the **CSV**, not JSON.

### Auto-refresh from the hosted CSV (current setup)

This repo is public, so the CSV is served straight from GitHub. In a tab named **`DB`**, cell **`A1`**:

```
=IMPORTDATA("https://raw.githubusercontent.com/b1rchling/FoodScraper/main/willys_index.csv")
```

It spills the whole table into `DB!A:Q` and re-pulls automatically (~hourly). If Sheets ever
balks at the raw URL, use the jsDelivr CDN mirror instead:

```
=IMPORTDATA("https://cdn.jsdelivr.net/gh/b1rchling/FoodScraper@main/willys_index.csv")
```

_(Prefer not to host? You can also **File → Import → Upload `willys_index.csv`** into the `DB`
tab and re-import to refresh — no URL needed.)_

### The scan formula

On a **`Scan`** tab: scanned barcode goes in **`A2`**. Then in **`B2`**:

```
=IFERROR(VLOOKUP(TO_TEXT(A2),{ARRAYFORMULA(TO_TEXT(DB!$A:$A)),DB!$B:$Q},{2,4,8,10,11,12,13,14,15,16},FALSE),"not found")
```

Spills across the row: **article · name(altText) · kcal · fat · satfat · carb · sugar · fibre · protein · salt**.
Put labels in `B1:K1` to match. For **just the article number**, use `2` instead of the `{…}` array.

> The `TO_TEXT(...)` on both sides makes the match work no matter whether Sheets imports the
> EAN column as text or as a number. (The data has no leading-zero EANs, so nothing is lost.)

**Smoke test:** EAN `7310401034584` → article `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

---

## 3. Refresh later (prices & assortment drift)

```bash
python willys_scraper.py                       # re-crawl (resumable)
git add willys_index.csv && git commit -m "Refresh willys_index" && git push
```

The sheet's `IMPORTDATA` picks up the new CSV automatically within ~an hour.

---

## Notes & limits

- **Loose/weighed produce** (article `…_KG`, EANs starting `2…`, ~300 items) often has Willys
  macros but a store-internal barcode you can't scan off a home package — fine for the table,
  just not always scannable.
- **Open Food Facts pass** (`--off`) only fills items that have a *real* EAN and no Willys
  macros. It's slow (OFF rate-limits ~100/min) and low-yield, so it's opt-in.
- **Politeness:** default 4 workers + delay. The full crawl is ~7,600 requests; don't hammer.
- **Other chains:** Hemköp is the same Axfood API — set `BASE = "https://www.hemkop.se"`.
  ICA/Coop need a different adapter (see the legacy HANDOFF doc).
