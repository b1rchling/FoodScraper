# Willys → Sheets macro tracker — TODO

## Setup (on laptop)
- [ ] Open the Google Sheet that will hold the data.
- [ ] Extensions → Apps Script → paste `willys-macros.gs` → Save.
- [ ] Run `startBuild()` once; click through the authorization prompts.
- [ ] Watch progress: Apps Script → **Executions** (or the `WillysDB` sheet filling).
      Willys crawl ≈ 30–50 min, then the Open Food Facts pass starts automatically.
- [ ] Add a "Scan" sheet; barcode in `A2`, paste the SCAN FORMULA (bottom of the .gs) in `B2`.
- [ ] Smoke test: EAN `7310401034584` → Trocadero Zero (≈2 kcal/100 ml).

## Verify
- [ ] Spot-check 5–10 `WillysDB` rows against the live willys.se product pages.
- [ ] Confirm loose produce (e.g. "Banan Klass 1") got macros with `source="off"`.
- [ ] Review rows where `source=""` (no macros anywhere) → non-food/unmatched: delete or ignore.

## Make it seamless / maintain
- [ ] Add a **weekly** time-driven trigger on `rebuild()` (prices & assortment drift).
- [ ] Pick the phone/scanner input — it MUST emit the full 13-digit EAN-13.
- [ ] (Optional) Swap fuzzy OFF name-search for a **curated produce table** (banana, apple,
      potato, carrot, onion, tomato…) for more accurate loose-produce macros.

## Decisions already made
- [x] Food categories only; **barn & kiosk dropped** (mixed non-food).
- [x] "Has a nutrient table" = food (`ingredients` is NOT a valid filter).
- [x] Loose/bulk `_KG` produce filled via Open Food Facts **by name** (no real barcode).
- [x] No open Willys EAN→article endpoint → we build our own index (crawl).

## Watch out for
- [ ] UrlFetch daily quota (~20k consumer) — don't `rebuild()` twice in one day.
- [ ] OFF search is rate-limited (~10/min); the produce pass is the slow part (paced in code).
- [ ] `_KG` / `209…` items can't be scanned (no product barcode) — name-matched only.

## Bonus / future chains
- [ ] **Hemköp**: same script — just set `BASE = 'https://www.hemkop.se'`.
- [ ] **ICA**: different API (`handla.api.ica.se`, needs auth) → write a new adapter; reuse architecture.
- [ ] **Coop**: different platform → reverse-engineer endpoints via DevTools first, then a new adapter.
