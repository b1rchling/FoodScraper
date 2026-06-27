# Willys → Google Sheets: scan an EAN, get the article number (+ macros)

Scrape Willys's food catalog with **Python**, then your dad's Google Sheet looks up a
scanned barcode (EAN) and returns the **article number**, product name, and macros.

- **Scraper:** [`willys_scraper.py`](willys_scraper.py) — pure standard library, no `pip install`.
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
python willys_scraper.py                # full crawl + Open Food Facts gap-fill
python willys_scraper.py --limit 50     # quick smoke test (first 50 products)
python willys_scraper.py --no-off       # skip the Open Food Facts fallback pass
python willys_scraper.py --fresh        # ignore the resume cache, re-crawl everything
```

It writes, next to the script:

- **`willys_index.csv`** ← the lookup table for Google Sheets
- `willys_index.json` ← same data as JSON (optional)
- `.willys_cache.jsonl` ← resume cache; the crawl is **resumable** — re-run to finish an
  interrupted crawl. Delete it (or use `--fresh`) to start over.

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

### Option A — Import the file (works today, zero setup) ✅ start here

1. In the sheet: **File → Import → Upload → `willys_index.csv`**.
2. Import location: **Replace current sheet**, into a tab named **`DB`**. Separator: comma.
3. Build the scan as in step 3 below.
4. To refresh later: re-run the scraper and re-import (Replace) into `DB`.

### Option B — Auto-refresh from a URL (hands-off)

Host `willys_index.csv` at a **public** URL, then in cell `DB!A1`:

```
=IMPORTDATA("https://your-public-url/willys_index.csv")
```

It spills the whole table into `DB` and re-pulls automatically (~hourly). Hosting choices:

- **Google Drive** (keeps everything private elsewhere): upload the CSV → **Share → Publish to
  web → CSV** → use that link. Re-upload to refresh.
- **Public GitHub gist / repo** (this repo is currently *private*, so its raw URLs won't work
  for `IMPORTDATA`): put just the CSV in a **public** gist and use its raw URL, or
  `https://cdn.jsdelivr.net/gh/<user>/<repo>@main/willys_index.csv`.

### 3. The scan formula

On a **`Scan`** tab: scanned barcode goes in **`A2`**. Then in **`B2`**:

```
=IFERROR(
  VLOOKUP(IF(LEN(TO_TEXT(A2))=12,"0"&TO_TEXT(A2),TO_TEXT(A2)),
          DB!$A:$Q, {2,4,8,10,11,12,13,14,15,16}, FALSE),
  "not found")
```

Spills across the row: **article · altText · kcal · fat · satfat · carb · sugar · fibre · protein · salt**.

Put labels in `B1:K1` to match. For **just the article number**, use `2` instead of the `{…}` array.
(The `LEN=12` bit re-adds a leading zero when the sheet/scanner drops it from an EAN-13.)

**Smoke test:** EAN `7310401034584` → article `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

---

## Notes & limits

- **Loose/weighed produce** (article `…_KG`, EANs starting `2…`) often has Willys macros but a
  store-internal barcode you can't scan at home — fine for the table, just not scannable.
- **Open Food Facts pass** only fills items that have a *real* EAN and no Willys macros
  (`--no-off` to skip). It's the slow tail (OFF rate-limits ~100/min).
- **Politeness:** default 5–6 workers. Don't hammer; the full crawl is ~7.6k requests.
- **Other chains:** Hemköp is the same Axfood API — set `BASE = "https://www.hemkop.se"`.
  ICA/Coop need a different adapter (see the legacy HANDOFF doc).
