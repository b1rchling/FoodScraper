# Willys → Google Sheets macro tracker — HANDOFF

_Self-contained context so you can continue on your laptop. Companion files in this folder:
`willys-macros.gs` (the script) and `willys-macros-TODO.md` (checklist)._

## Goal
Scan a grocery barcode (EAN) → get **macronutrients per 100 g/ml** in Google Sheets,
covering **all of Willys's food items** (Open Food Facts alone has gaps). Food items only.

## The core problem we solved
Willys's website has **no open "EAN → article number" endpoint**. We probed search,
autocomplete, and ~13 REST path variants — all ignore the barcode or 404. The article
number (e.g. `101278894_ST`) is **independent** of the EAN (`7310401034584`); the only
place Willys resolves one to the other is its logged-in in-store self-scan flow.

**Therefore:** you must build your own `EAN → article → macros` index by crawling Willys's
catalog (each product-detail response exposes its `ean` + nutrition), cache it in a sheet,
and look it up at scan time with `VLOOKUP`. That is exactly what `willys-macros.gs` does.

## Willys API reference (Axfood Storefront REST; JSON; no auth for reads) — verified June 2026
| Purpose | Request | Notes |
|---|---|---|
| Search (NAME/text only — ignores EAN) | `GET /search/clean?q=&size=&page=` | `size` capped at 100 server-side |
| Browse category (the catalog) | `GET /c/<path>?size=100&page=N` | send `Accept: application/json`; pages in `pagination.numberOfPages` |
| Category tree | `GET /leftMenu/categorytree?storeId=2110&deviceType=OTHER` | 577 nodes |
| **Product detail (EAN + macros)** | `GET /axfood/rest/p/<code>` | needs internal **code**, NOT the EAN |

Macros live at `product.nutrientHeaders[0].nutrientDetails[]`. Gotchas baked into the script:
- `nutrientHeaders` may have **2 entries**: `[0]` = as sold, `[1]` = cooked → we use `[0]`.
- Energy appears **twice** (`energi` in `kilokalori` and in `kilojoule`) → split by unit.
- Values are strings with a dot decimal; basis is usually **per 100 g/ml**.
- Swedish typeCodes: `fett`, `varav mättat fett`, `kolhydrat`, `varav sockerarter`,
  `fiber`, `protein`, `salt`.

## Key decisions
1. **Food only** via two filters: (a) only food top-level categories, (b) **barn & kiosk
   excluded** (mixed: diapers / tobacco / magazines).
2. **"Has a nutrient table" = food.** `ingredients` is NOT a valid filter — soap, shampoo,
   diapers and snus all carry an ingredients string. (See data below.)
3. **Loose / bulk produce → Open Food Facts.** Weighed goods use `_KG` codes with in-store
   EANs starting `209…` (e.g. banana `2090165200009`) — these are NOT real barcodes and
   aren't in OFF, and you can't scan them at home anyway. So the script fills them via
   **OFF name search**; packaged items lacking Willys nutrition are filled via **OFF by EAN**.
4. **`source` column** records where each row's macros came from: `willys`, `off`, or empty
   (no macros found anywhere → likely a non-food straggler or an unmatched item; prune or ignore).

### Discriminator evidence (food vs non-food)
```
mjölk / barnmat / trocadero        nutrients=YES   -> food
diskmedel / schampo / pampers /    nutrients=NO    -> non-food (but all have "ingredients")
snus / kattmat
loose produce (banan/potatis/lök)  nutrients=NO, ean starts 209, code _KG -> OFF by name
```

## Architecture / scale
- ~9–10k food products. No bulk endpoint → one detail call per product.
- Crawl is **resumable** (5-min trigger + cursor), ~30–50 min, then an automatic OFF gap-fill pass.
- UrlFetch ≈ 11–12k calls total — under the 20k/day consumer quota (don't rebuild twice/day).

## Setup (laptop)
1. Google Sheet → Extensions → Apps Script → paste `willys-macros.gs` → Save.
2. Run `startBuild()` once; approve the auth prompts.
3. Watch **Executions** (or `WillysDB` filling). Crawl, then OFF pass, run on their own.
4. "Scan" sheet: scanned barcode in `A2`, paste the SCAN FORMULA (bottom of the .gs) in `B2`.
5. Sanity check: EAN `7310401034584` → Trocadero Zero (2 kcal/100 ml, article `101278894_ST`).
6. Freshness: add a weekly time-driven trigger on `rebuild()`.

## Risks / open questions
- **Scan quality is the real gatekeeper.** Your earlier `731040100374` was a bad 12-digit
  capture — absent from Willys AND OFF. Make sure your scanner outputs the full 13-digit EAN-13.
- **OFF name-match is fuzzy** for `_KG` produce (it takes OFF's top hit). Audit `source="off"`
  rows on `_KG` items; consider a small **curated produce table** for better accuracy.
- OFF search API is rate-limited (~10/min) — the script paces it (6.5 s between name searches),
  so the OFF pass for produce is the slow part (still only a few trigger runs).

## BONUS — does this work for ICA / Coop / Hemköp?
- **Hemköp — YES, almost free.** Hemköp is Axfood too and serves the **identical API**
  (`/search/clean` and `/axfood/rest/...` both return 200 JSON, same fields). Just set
  `BASE = 'https://www.hemkop.se'` (and the matching store id). Same for other Axfood banners
  (mat.se, Tempo/Handlarn/Matöppet are likely the same platform).
- **ICA — partially.** ICA has its own reverse-engineered JSON API (`handla.api.ica.se`, and
  `handlaprivatkund.ica.se/stores/{storeId}`), but a **different structure** and it generally
  needs an auth ticket / store selection. The *architecture* here (crawl → detail → index by
  EAN → VLOOKUP + OFF fallback) transfers; the **API adapter** (URLs + field mapping) must be
  rewritten. Refs: github.com/svendahlstrand/ica-api, github.com/HampusAndersson01/ICA-Products-API
- **Coop (Sweden) — NO, not as-is.** Different platform entirely (Axfood paths 404 on coop.se).
  You'd reverse-engineer Coop's own endpoints first (Chrome DevTools → Network → Fetch/XHR while
  browsing/searching), then write a new adapter. Same architecture still applies.

**Reusable rule of thumb:** only the "API adapter" (base URL, category browse, product detail,
field names) is site-specific. The crawl→index→VLOOKUP→OFF-fallback design is the same everywhere.
