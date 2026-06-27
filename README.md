# Willys → Google Sheets: scan an EAN, get the article number (+ macros)

Scrape Willys's food catalog with **Python**, then your dad's Google Sheet looks up a
scanned barcode (EAN) and returns the **article number**, product name, and macros.

- **Scraper:** [`willys_scraper.py`](willys_scraper.py) — pure standard library, no `pip install`.
- **Data (hosted for the sheet):**
  [`willys_index.csv`](willys_index.csv) — full table (article + name + macros), and
  [`willys_ean_article.csv`](willys_ean_article.csv) — lean `ean,article` only (no macros).
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
python willys_scraper.py --build-only   # just rebuild the output files from the cache (instant)
python willys_scraper.py --off          # also fill gaps from Open Food Facts (slow, opt-in)
python willys_scraper.py --limit 50     # quick smoke test (first 50 products)
python willys_scraper.py --fresh        # ignore the resume cache, re-crawl everything
```

It writes, next to the script:

- **`willys_index.csv`** ← full lookup table for Google Sheets
- **`willys_ean_article.csv`** ← lean `ean,article` lookup (no macros)
- `willys_index.json` ← same data as the full table, as JSON (git-ignored)
- `.willys_cache.jsonl` ← resume cache. The crawl is **resumable** — if Willys rate-limits you
  (HTTP 403), just re-run and it fetches only what's missing. Delete it (or `--fresh`) to start over.

The crawl is polite by default (4 workers, small delay) and **retries on 403/429** rather than
dropping products. Full catalog ≈ 7,600 products, ~10–20 min.

**`willys_index.csv` columns** (`A`→`O`):

| A | B | C | D | E | F | G | H | I | J | K | L | M | N | O |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ean | article | altText | brand | basis | kcal | kj | fat | satfat | carb | sugar | fibre | protein | salt | source |

`article` is the Willys article number (e.g. `101278894_ST`). `altText` is the product name
including size/volume (e.g. `Trocadero Zero Sugar Läsk Pet 1,5l Trocadero`). Macros are per
100 g/ml. `source` = `willys`, `off` (Open Food Facts), or empty.

**`willys_ean_article.csv`** is just two columns — `ean,article` — for the simplest scan use case.

---

## 2. Get it into your dad's Google Sheet

> **Heads-up:** Google Sheets has **no native JSON import** — its only URL-pull functions are
> `IMPORTDATA` (CSV/TSV), `IMPORTXML`, `IMPORTHTML`, `IMPORTFEED`. So we use the **CSV**, not JSON.

### Auto-refresh from the hosted CSV (current setup)

This repo is public, so the CSV is served straight from GitHub. In a tab named **`DB`**, cell **`A1`**
— pick the file you want:

```
=IMPORTDATA("https://raw.githubusercontent.com/b1rchling/FoodScraper/main/willys_index.csv")
```
…or the lean version (just `ean,article`):
```
=IMPORTDATA("https://raw.githubusercontent.com/b1rchling/FoodScraper/main/willys_ean_article.csv")
```

It spills the whole table into `DB` and re-pulls automatically (~hourly). If Sheets ever balks
at the raw URL, swap the host for the jsDelivr CDN mirror
(`https://cdn.jsdelivr.net/gh/b1rchling/FoodScraper@main/<file>.csv`).

_(Prefer not to host? You can also **File → Import → Upload** the CSV into the `DB` tab.)_

### The scan formula

On a **`Scan`** tab: scanned barcode goes in **`A2`**. Then in **`B2`**:

**Full index** (`willys_index.csv`) — returns article + name + macros:
```
=IFERROR(VLOOKUP(TO_TEXT(A2),{ARRAYFORMULA(TO_TEXT(DB!$A:$A)),DB!$B:$O},{2,3,6,8,9,10,11,12,13,14},FALSE),"not found")
```
Spills: **article · name · kcal · fat · satfat · carb · sugar · fibre · protein · salt** (label `B1:K1`).

**Lean lookup** (`willys_ean_article.csv`) — returns just the article number:
```
=IFERROR(VLOOKUP(TO_TEXT(A2),{ARRAYFORMULA(TO_TEXT(DB!$A:$A)),DB!$B:$B},2,FALSE),"not found")
```

> The `TO_TEXT(...)` on both sides makes the match work no matter whether Sheets imports the
> EAN column as text or as a number. (The data has no leading-zero EANs, so nothing is lost.)

**Smoke test:** EAN `7310401034584` → article `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

---

## 3. Refresh later (prices & assortment drift)

```bash
python willys_scraper.py                       # re-crawl (resumable)
git add willys_index.csv willys_ean_article.csv && git commit -m "Refresh data" && git push
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
