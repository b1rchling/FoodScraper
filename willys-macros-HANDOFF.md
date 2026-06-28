# Willys → Google Sheets macro tracker — HANDOFF

_Status: superseded by the Python scraper. This file is kept as historical context for
**why** the architecture looks the way it does and **how** the original Apps Script
(`willys-macros.gs`) worked. For the current setup, see [README.md](README.md)._

## Goal
Scan a grocery barcode (EAN) → get the **article number** and **macronutrients per 100 g/ml**
in Google Sheets, covering **all of Willys's food items** (Open Food Facts alone has gaps).
Food items only.

## The core problem we solved
Willys's website has **no open "EAN → article number" endpoint**. Verified live (June 2026)
across ~20 endpoint variants — search, autocomplete, REST product/collection paths:
- Search ignores the barcode (`q=<ean>` → `results: null`; result objects don't even carry an
  `ean` field).
- Product detail by EAN (`/axfood/rest/p/<ean>`) → HTTP 400 "No product found" — needs the
  internal **code**, not the EAN.
- `/axfood/rest/products/ean/<ean>` exists but always returns empty `items: []` for anonymous
  callers — even with cookies + CSRF token. Gated behind a logged-in, in-store self-scan session.
- Category browse listings carry `code` but **never** `ean`.

**Therefore:** you must build your own `EAN → article → macros` index by crawling Willys's
catalog (each product-detail response exposes its `ean` + nutrition), cache it, and look it up
at scan time with `VLOOKUP`.

## Willys API reference (Axfood Storefront REST; JSON; no auth for reads) — verified June 2026
| Purpose | Request | Notes |
|---|---|---|
| Search (NAME/text only — ignores EAN) | `GET /search/clean?q=&size=&page=` | `size` capped at 100 server-side |
| Browse category (the catalog) | `GET /c/<path>?size=100&page=N` | send `Accept: application/json`; pages in `pagination.numberOfPages` |
| Category tree | `GET /leftMenu/categorytree?storeId=2110&deviceType=OTHER` | 577 nodes |
| **Product detail (EAN + macros)** | `GET /axfood/rest/p/<code>` | needs internal **code**, NOT the EAN |

Macros live at `product.nutrientHeaders[0].nutrientDetails[]`. Gotchas:
- `nutrientHeaders` may have **2 entries**: `[0]` = as sold, `[1]` = cooked → use `[0]`.
- Energy appears **twice** (`energi` in `kilokalori` and in `kilojoule`) → split by unit.
  **Gotcha (verified June 2026):** some suppliers *mislabel* the units at the source, so the
  kJ value arrives tagged `kilokalori` and vice-versa (e.g. Chistorra `7392055251319` returns
  `kilokalori=1340, kilojoule=320`). All three scrapers now run `normalize_macros()` at write
  time: when `kj < kcal` and `kcal/kj` is in the ~3–5.5 band it swaps them back (kJ is always
  ≈4.184× kcal for real food). The same pass blanks impossible per-100g values (gram-macro
  >100 g, kcal >950) from supplier typos.
- Values are strings with a dot/comma decimal; basis is usually **per 100 g/ml**.
- Swedish typeCodes: `fett`, `varav mättat fett`, `kolhydrat`, `varav sockerarter`,
  `fiber`, `protein`, `salt`.
- Product name is taken from `product.name`; the shelf price as display text is `product.price`
  ("99,90 kr"), with `product.priceValue` the numeric form. (`product.image.altText` carries
  name + size, but the output now uses the cleaner `product.name`.)
- Package size (the `weight` column) comes from `product.displayVolume` ("2,2kg"); Coop's
  equivalent field is `packageSizeInformation`. `weight_str()` inserts a space between number and
  unit and lowercases it → "2,2 kg", "500 ml", "410 g".

## Key decisions
1. **Food only** via two filters: (a) only food top-level categories, (b) **barn & kiosk
   excluded** (mixed: diapers / tobacco / magazines).
2. **"Has a nutrient table" = food.** `ingredients` is NOT a valid filter — soap, shampoo,
   diapers and snus all carry an ingredients string.
3. **Loose / bulk produce → Livsmedelsverket (name lookup).** Weighed goods use `_KG` codes with
   in-store EANs starting `2…` (e.g. banana `2090165200009`) — NOT real barcodes, not scannable at
   home. `produce_scraper.py` builds a name→nutrition table (`produce_nutrition.csv`, `source=slv`)
   from Livsmedelsverket for these. (Originally a fuzzy Open Food Facts name-search; SLV replaced it.)
4. **`source` column** records where each row's macros came from: `willys` / `hemkop` / `coop`,
   `off` (Open Food Facts EAN backfill), or empty. The separate produce table uses `slv`.

### Discriminator evidence (food vs non-food)
```
mjölk / barnmat / trocadero        nutrients=YES   -> food
diskmedel / schampo / pampers /    nutrients=NO    -> non-food (but all have "ingredients")
snus / kattmat
loose produce (banan/potatis/lök)  nutrients=NO, ean starts 2, code _KG -> SLV name table
```

## Architecture / scale
- ~7,650 unique food products across 11 categories. No bulk endpoint → one detail call per
  product.
- Full crawl in the current Python script: ~10–20 min at 4 workers with a small delay,
  resumable via a local JSONL cache, with automatic retry/backoff on HTTP 403/429.

## Two implementations exist in this repo
1. **`willys_scraper.py`** (current) — Python, standard library only, runs locally or in CI.
   Outputs `willys_index.csv` (full: ean/article/name/brand/weight/price/basis/macros/source) and
   `willys_ean_article.csv` (lean: just ean,article). Hosted on this public GitHub repo and
   pulled into Sheets with `IMPORTDATA`. See [README.md](README.md) for the exact setup.
2. **`willys-macros.gs`** (legacy) — same idea, but the crawl runs inside Google Apps Script
   with time-based triggers, writing into a `WillysDB` sheet directly. Superseded because:
   - Apps Script's 6-minute execution limit forces chunking via triggers (`crawlChunk_`,
     `enrichOFF_`), which is fragile and slow to debug.
   - UrlFetch has a 20k/day quota on consumer accounts — fine for one crawl/day, awkward for
     iterating.
   - Python lets you run the exact same crawl logic locally, commit the *output* (not the
     scraping logic) to the repo, and have Sheets just read a static file.

## Risks / open questions (apply to both implementations)
- **Scan quality is the real gatekeeper.** A short/garbled barcode capture (e.g. 12 digits
  instead of 13) won't be in the index. Make sure the scanner outputs the full EAN-13.
- **OFF name-match was fuzzy** for `_KG` produce (it took OFF's top hit). Replaced by a curated
  **Livsmedelsverket** table (`produce_scraper.py` → `produce_nutrition.csv`): authoritative
  Swedish per-100g data for ~60 raw staples, matched by typed name and scaled by grams.
- OFF search API is rate-limited (~10/min for name search, ~100/min for EAN lookup) — paced
  in code; the OFF pass is opt-in (`--off`) precisely because it's slow and low-yield.

## BONUS — does this work for ICA / Coop / Hemköp?
- **Hemköp — YES, almost free.** Hemköp is Axfood too and serves the **identical API**
  (`/search/clean` and `/axfood/rest/...` both return 200 JSON, same fields). Just set
  `BASE = 'https://www.hemkop.se'` in `willys_scraper.py` (and the matching store id). Same
  for other Axfood banners (mat.se, Tempo/Handlarn/Matöppet are likely the same platform).
- **ICA — NO (not feasible).** Investigated live June 2026 via `handlaprivatkund.ica.se/stores/{storeId}`.
  ICA serves nutrition (an HTML table in the `bop` product-detail react-query cache) but **never
  exposes the EAN/GTIN** — every product representation (search-listing SSR state, `bop` detail
  cache, and the `webproductpagews/v6/products` bulk endpoint) keys on `retailerProductId`
  (internal article) + a GUID, with no barcode field anywhere. The crawl→index design transfers,
  but without an EAN there's no key to VLOOKUP a scanned barcode against, so a like-for-like
  `ean,article` index can't be built. (ICA also sits behind AWS WAF, so a plain-urllib crawl
  would be challenged.)
- **Coop (Sweden) — YES, done (separate adapter).** Different platform — **SAP Hybris behind
  Azure API Management**, not Axfood (Axfood paths 404 on coop.se). Reverse-engineered from
  coop.se's own XHR (June 2026) and built as `coop_scraper.py`. The twist: unlike Willys/Hemköp,
  Coop's **category listing already carries `ean` + name + brand + full nutrition**
  (`nutrientLinks`), so there's **no per-product detail call** — a paginated
  `POST .../personalization/search/entities/by-attribute` (subscription key shipped in the page
  HTML) returns everything; full crawl ≈28 s. `id == ean` (no separate article number). Exact
  endpoint / headers / body are in [willys-macros-TODO.md](willys-macros-TODO.md).

**Reusable rule of thumb:** only the "API adapter" (base URL, category browse, product detail,
field names) is site-specific. The crawl→index→VLOOKUP→OFF-fallback design is the same everywhere.
