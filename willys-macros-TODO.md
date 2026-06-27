# Willys → Sheets macro tracker — TODO

_Tracks the current Python setup. The Apps Script checklist this file used to contain is
superseded — `willys-macros.gs` / its HANDOFF are kept only as historical reference._

## Setup
- [x] Write `willys_scraper.py` (stdlib-only Python crawler).
- [x] Run the full crawl: `python willys_scraper.py` → 7,648/7,649 food products cached.
- [x] Harden against Willys rate-limiting: retry on HTTP 403/429 with backoff instead of
      dropping the product.
- [x] Generate `willys_index.csv` (full: ean/article/altText/brand/basis/macros/source) and
      `willys_ean_article.csv` (lean: ean,article only).
- [x] Make the repo public and host both CSVs via `raw.githubusercontent.com` (+ jsDelivr as
      a fallback mirror).
- [x] Google Sheet: `DB` tab pulls a CSV via `IMPORTDATA(...)`; `Scan` tab does a `VLOOKUP`
      on the scanned EAN. See [README.md](README.md) for the exact formulas.
- [x] Smoke test: EAN `7310401034584` → article `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

## Verify
- [ ] Spot-check 5–10 rows in `willys_index.csv` against the live willys.se product pages.
- [ ] Confirm loose produce (e.g. "Banan Klass 1") got macros with `source="off"` (only if
      `--off` has been run).
- [ ] Review rows where `source=""` (no macros anywhere) → non-food/unmatched: ignore or prune.

## Make it seamless / maintain
- [ ] Re-run `python willys_scraper.py` periodically (prices & assortment drift) and push the
      refreshed CSVs — `IMPORTDATA` in the sheet picks them up automatically (~hourly via raw;
      jsDelivr mirror can lag hours after a push).
- [ ] Consider a scheduled job (cron / GitHub Actions) to run the refresh + commit + push
      automatically instead of doing it by hand.
- [ ] Pick the phone/scanner input — it MUST emit the full 13-digit EAN-13.
- [ ] (Optional) Run `--off` to fill macro gaps from Open Food Facts; swap its fuzzy `_KG`
      name-search for a **curated produce table** (banana, apple, potato, carrot, onion,
      tomato…) if accuracy on loose produce matters.

## Decisions already made
- [x] Food categories only; **barn & kiosk dropped** (mixed non-food).
- [x] "Has a nutrient table" = food (`ingredients` is NOT a valid filter).
- [x] Loose/bulk `_KG` produce, if filled at all, comes via Open Food Facts **by name**
      (no real barcode) — via the opt-in `--off` pass.
- [x] No open Willys EAN→article endpoint (verified ~20 endpoint variants) → we build our
      own index by crawling product details.
- [x] Scraping moved from Google Apps Script to a local Python script: easier to debug, no
      6-minute execution limit, no UrlFetch quota; the sheet just reads a static hosted CSV.
- [x] Drop `name`/`price` columns from the full index (`altText` already has the name with
      size; price isn't needed for lookup) and add a lean `ean,article`-only file for the
      simplest use case.

## Watch out for
- [ ] Willys throttles aggressive crawling (HTTP 403) — the script retries with backoff, but
      keep workers/delay conservative (`--workers 4`, small `--delay`) rather than cranking
      concurrency up.
- [ ] OFF search is rate-limited (~10/min for name search, ~100/min for EAN); the `--off`
      pass is the slow part, paced in code, and skipped by default.
- [ ] `_KG` / EANs starting `2…` items can't be scanned from a home package (no real
      barcode) — name-matched only, and only if `--off` is used.
- [ ] jsDelivr's `@main` CDN mirror caches for hours — don't expect it to reflect a push
      immediately; `raw.githubusercontent.com` is the fresher source.

## Bonus / future chains
- [ ] **Hemköp**: same script — just set `BASE = 'https://www.hemkop.se'` in `willys_scraper.py`.
- [ ] **ICA**: different API (`handla.api.ica.se`, needs auth) → write a new adapter; reuse architecture.
- [ ] **Coop**: different platform → reverse-engineer endpoints via DevTools first, then a new adapter.
