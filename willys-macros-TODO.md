# Willys → Sheets macro tracker — TODO

_Tracks the current Python setup. The Apps Script checklist this file used to contain is
superseded — `willys-macros.gs` / its HANDOFF are kept only as historical reference._

## Setup
- [x] Write `willys_scraper.py` (stdlib-only Python crawler).
- [x] Run the full crawl: `python willys_scraper.py` → 7,648/7,649 food products cached.
- [x] Harden against Willys rate-limiting: retry on HTTP 403/429 with backoff instead of
      dropping the product.
- [x] Generate `willys_index.csv` (full: ean/article/name/brand/basis/price/macros/source) and
      `willys_ean_article.csv` (lean: ean,article only).
- [x] Make the repo public and host both CSVs via `raw.githubusercontent.com` (+ jsDelivr as
      a fallback mirror).
- [x] Google Sheet: `DB` tab pulls a CSV via `IMPORTDATA(...)`; `Scan` tab does a `VLOOKUP`
      on the scanned EAN. See [README.md](README.md) for the exact formulas.
- [x] Smoke test: EAN `7310401034584` → article `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

## Verify  (done June 2026 — empirical pass over all three chains)
- [x] Spot-check rows against the live pages. Willys live = 3/3 exact (Trocadero, olive oil,
      1664 Blanc); Coop live (APIM) = 8/8 exact (category "Ost") and the subscription key is
      still valid; smoke test `7310401034584` → `101278894_ST`, kcal 2.0. No duplicate EANs,
      no negative values in any chain.
- [x] Review rows where `source=""` (no macros): ~795 Willys / 771 Hemköp / 884 Coop. These are
      **legit food that carries no per-100g table** — fresh produce ("Klass 1"), coffee
      (beans/ground/capsules), tea, water — NOT non-food garbage. **Decision: keep, don't prune**
      (they still return a valid EAN→article; blank macros is correct). `--off` has only been run
      for Hemköp (28 `off` rows); Willys/Coop produce stays `source=""` until `--off` is run.

### Data-quality fixes applied (build-only, no re-crawl)
- [x] **Energy kcal↔kJ swap.** ~21 Willys / 18 Hemköp / 19 Coop rows had the energy units swapped
      *at the source* (verified live: Willys returns `kilokalori=1340, kilojoule=320` for
      Chistorra). Added `normalize_macros()` to all three scrapers: swap when `kj < kcal` and
      `kcal/kj` is in the ~3–5.5 band (so unrelated single-value glitches are left alone).
      Impossible-kcal rows dropped from 9/8/10 → 0.
- [x] **Hemköp schema alignment.** The scraper code already emitted `name`+`price` (cache had
      both); only the on-disk CSV was stale with the old `altText`/no-price layout. Rebuilt →
      `hemkop_index.csv` now matches Willys/Coop (all 7,864 rows have name + price).
- [x] **Outlier clamp.** `normalize_macros()` blanks impossible per-100g values (gram-macro >100 g,
      kcal >950) — supplier typos like gum fat=164, tahini salt=350, Coop choc kcal=1942. Now 0.
- [ ] Residual: ~7–16 ambiguous single-value energy glitches per chain (e.g. `kj=1.982` next to a
      plausible `kcal=474`) were intentionally left — no confident correct value to pick.

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
- [x] Full index carries `name` (plain product name, e.g. "Bacon") + `price` (shelf price as
      display text, e.g. "24 kr" / "19,83 kr"). Earlier the index dropped name/price in favour
      of `altText` (name + size); that was reversed — `altText` is gone, `name` + `price` are in.
      A lean `ean,article`-only file also exists for the simplest use case.

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
- [x] **Hemköp**: `hemkop_scraper.py` exists, same architecture, outputs `hemkop_index.csv` /
      `hemkop_ean_article.csv` / `.hemkop_cache.jsonl`. Needs the same "Verify" pass as Willys.
- [x] **ICA**: investigated and **not feasible** (June 2026). ICA serves nutrition but **never
      exposes the EAN/GTIN** — products are keyed only by `retailerProductId` (internal article)
      + a GUID. With no barcode in ICA's data there's no key to VLOOKUP a scanned EAN against, so
      a like-for-like `ean,article` index can't be built. Verified across the search-listing SSR
      state, the `bop` product-detail cache, and the `webproductpagews/v6/products` bulk endpoint
      (ICA also sits behind AWS WAF, so a plain-urllib crawl would be challenged).
- [x] **Coop**: `coop_scraper.py` exists. Coop runs on **SAP Hybris behind Azure APIM**, not
      Axfood — reverse-engineered from coop.se's XHR (June 2026):
  - Catalog search is a **POST** to
    `https://external.api.coop.se/personalization/search/entities/by-attribute?api-version=v1&store=251300&groups=CUSTOMER_PRIVATE&device=desktop&direct=false`
    with header `Ocp-Apim-Subscription-Key: 3becf0ce306f41a1ae94077c16798187` (the key Coop
    ships in its page HTML; if it 401s, re-grab `articleServiceSubscriptionKey` from a coop.se page).
  - Body: `{"attribute":{"name":"categoryIds","value":"<catCode>"},"resultsOptions":{"skip":S,"take":200,...},"customData":{...}}`.
    Paginate via `skip`/`take` (`take` ≥ ~500 returns an empty body → cap at 200).
  - **The listing already returns `ean` + `name` + `manufacturerName` + `salesPriceData.b2cPrice`
    + full nutrition (`nutrientLinks`)** → no per-product detail fetch needed (the big difference
    vs Willys/Hemköp; full crawl ≈ 28 s). Category codes come from
    `.../ecommerce/coop/users/anonymous/categories/tree/<store>`.
  - Coop has **no separate article number**: a product's `id` == its `ean` (`code` is always null),
    so `article` = `id`. (In-store/weight items with EANs starting `2097…` get a store-scoped
    `251300_<ean>` id — not home-scannable anyway.)
  - Result: 9,512 food products, 8,618 (91%) with macros. Outputs `coop_index.csv` /
    `coop_ean_article.csv` / `coop_index.json` / `.coop_cache.jsonl`. Needs the same "Verify" pass.

## Next phase: full-stack app (Expo + Supabase)
_Pulled in from a planning doc (`TODO.md`) sketched outside this repo; adjusted below to match
what actually exists here rather than a generic from-scratch plan._

**Goal:** replace the Google Sheets/CSV+VLOOKUP flow with a proper app — scan an EAN-13 with
`expo-camera` (Android) or type it in (Web fallback), hit Supabase, show name + macros.

- **Frontend:** Expo (React Native) targeting Android + Web, styled with NativeWind (Tailwind).
- **Backend:** Supabase (Postgres) replacing the public-CSV + `IMPORTDATA`/`VLOOKUP` hack.
- **Ingestion:** the existing `willys_scraper.py` / `hemkop_scraper.py` stay as-is for crawling;
  only the *output* step changes (CSV → `supabase-py` bulk upsert).

### Adjustments vs. the original plan
- [ ] **Schema mismatch to resolve:** the original plan assumes one `products` table keyed by
      `ean` with a single `article` column. We have **two chains** (Willys, Hemköp — soon
      maybe ICA/Coop) that can assign **different article numbers to the same EAN**. A single
      `ean` PK can't hold two articles. Decide: (a) `products` keyed by `ean` for the
      name/macros (chain-agnostic, since nutrition for the same barcode is the same product),
      plus a separate `chain_articles` table keyed by `(chain, ean) → article`, or (b) one row
      per `(chain, ean)` with article + macros duplicated per chain. (a) avoids duplicating
      macros; recommended.
- [ ] Column names should mirror the existing CSV header exactly (17 cols, identical across all
      three chains): `ean, article, name, brand, weight, price, basis, kcal, kj, fat, satfat,
      carb, sugar, fibre, protein, salt, source` — note `name` is the plain product name (e.g.
      "Trocadero Zero Sugar Läsk Pet 1,5l"), `weight` is the package size display string
      ("2,2 kg" / "500 ml"), `price` is a display string ("24 kr" / "15,04 kr"), and `source`
      is `willys` / `off` / `hemkop` / `coop` / empty, not a free-text provenance string.
- [ ] Add a `chain` column (or the `chain_articles` table above) so the same scraper output
      shape (per chain) can upsert without clobbering the other chain's rows.

### Tasks
1. [ ] Initialize the Expo project (NativeWind + `@supabase/supabase-js` client setup) — net
       new, nothing here yet.
2. [ ] Create the Supabase `products` table (+ `chain_articles` if going with option (a) above).
3. [ ] Scaffold UI: camera view (Android, `expo-camera` `onBarcodeScanned`, EAN-13), text input
       fallback (Web), placeholder result card.
4. [ ] Write the Supabase query: fetch product by EAN (join `chain_articles` if split out).
5. [ ] Add a `--supabase` upsert mode to `willys_scraper.py` and `hemkop_scraper.py` (via
       `supabase-py`) as an alternative to `--build-only`'s CSV write — keep CSV output too,
       since the Sheets flow is still live and shouldn't break.
