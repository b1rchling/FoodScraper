#!/usr/bin/env python3
"""
coop_scraper.py - Build an  EAN -> {article, name, macros}  index from Coop.se
(FOOD ITEMS ONLY), for lookup in Google Sheets.

WHY THIS IS DIFFERENT FROM willys/hemkop
  Willys & Hemköp run on Axfood's platform (GET /axfood/rest/p/<code>); Coop runs
  on SAP Hybris behind Azure API Management. Reverse-engineered from coop.se's own
  XHR traffic (June 2026):
    - Catalog/search is POSTed to .../personalization/search/entities/by-attribute
      with an Ocp-Apim-Subscription-Key the site ships in its page HTML.
    - The category LISTING already returns ean + name + brand + FULL nutrition
      (`nutrientLinks`), so - unlike Willys/Hemköp - there is NO per-product detail
      fetch: one paginated POST per category page gets everything.
    - Coop exposes NO separate in-store article number: a product's `id` IS its EAN
      and the `code` field is always null. We therefore set article = id (= ean) to
      keep the CSV schema identical to the Willys/Hemköp output.

OUTPUT (written next to this script)
  coop_index.csv        -> full table: ean, article, name, brand, basis, price, macros, source
  coop_ean_article.csv  -> lean lookup: just ean,article (article == ean for Coop)
  coop_index.json       -> same data as the full table, as JSON (git-ignored)
  .coop_cache.jsonl     -> resume cache; a re-run upserts and skips nothing expensive
                           (the crawl is cheap). Delete it (or --fresh) to start clean.

USAGE
  python coop_scraper.py                 # crawl + write csv/json
  python coop_scraper.py --build-only    # just rebuild csv/json from the cache (instant)
  python coop_scraper.py --off           # also fill gaps from Open Food Facts (slow)
  python coop_scraper.py --limit 200     # quick smoke test (first N products)
  python coop_scraper.py --fresh         # ignore the cache, re-crawl everything
  python coop_scraper.py --workers 4 --delay 0.1   # tune politeness (defaults shown)
  python coop_scraper.py --store 251300  # Hybris store id (default; macros/EAN are store-agnostic)

Pure standard library - no `pip install` needed. Works on Python 3.9+.
"""

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE = "https://external.api.coop.se"
# Azure APIM subscription key Coop ships publicly in its handla page HTML. If Coop
# rotates it, grab the fresh value from a coop.se page (search "articleServiceSubscriptionKey").
SUB_KEY = "3becf0ce306f41a1ae94077c16798187"
STORE = "251300"                        # default Hybris store; EAN/macros are store-agnostic
PAGE = 200                              # items per page (take=500+ returns an empty body)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) coop-scraper/1.0"

# Food top-level category codes (from .../categories/tree/<store>). Querying a top-level
# code returns every product in its subcategories too. Non-food parents are intentionally
# excluded: Kiosk & tidningar (3839), Barn (27107), Hushåll (29659), Skönhet & hygien
# (28395), Fritid (324532), Apotek/hälsa (30793), Hem & inredning (29662), Djurmat (32045).
FOOD_CATEGORIES = [
    ("16534", "Frukt & grönsaker"),
    ("11777", "Kött, fågel & chark"),
    ("14754", "Fisk & skaldjur"),
    ("6262", "Mejeri & ägg"),
    ("6327", "Ost"),
    ("39033900", "Vegetariskt"),
    ("25854", "Frys"),
    ("22410", "Dryck"),
    ("21330", "Skafferi"),
    ("18121", "Bröd & bageri"),
    ("24425", "Godis, glass & snacks"),
    ("5377683", "Färdigmat & mellanmål"),
    ("24420", "Kryddor & smaksättare"),
    ("48200", "Delikatesser"),
]

# Swedish nutrientLink description -> our column. 'Energi' is split kcal/kJ by unit.
MACRO = {
    "fett": "fat", "varav mättat fett": "satfat", "varav mattat fett": "satfat",
    "kolhydrat": "carb", "varav sockerarter": "sugar",
    "fiber": "fibre", "fibrer": "fibre", "kostfiber": "fibre",
    "protein": "protein", "salt": "salt",
}

# Full-index column order. NOTE: keep `ean` first (col A) so VLOOKUP keys off it.
COLUMNS = ["ean", "article", "name", "brand", "weight", "price", "basis",
           "kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein",
           "salt", "source"]

# Lean lookup file: just the barcode -> article number (no macros).
LEAN_COLUMNS = ["ean", "article"]
LEAN_NAME = "coop_ean_article.csv"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".coop_cache.jsonl")

SEARCH_URL = (f"{BASE}/personalization/search/entities/by-attribute"
              f"?api-version=v1&store={{store}}&groups=CUSTOMER_PRIVATE&device=desktop&direct=false")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class RateLimited(Exception):
    """Raised after exhausting retries against a 403/429 throttle."""


def http_post_json(url, payload, timeout=30, retries=5):
    """POST JSON and parse the JSON response.

    400/404 are real answers -> raise immediately. 403/429 mean Coop is throttling
    us -> wait (honour Retry-After) and RETRY so a page is never silently dropped.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": SUB_KEY,
        "Origin": "https://www.coop.se", "Referer": "https://www.coop.se/",
    }
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                if not raw:                               # server's "too big / busy" reply
                    raise ValueError("empty response body")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise
            last = e
            if e.code in (403, 429):
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if (ra and str(ra).isdigit()) else min(45, 4 * (2 ** attempt))
                time.sleep(wait + random.uniform(0, 1.0))  # de-sync the worker threads
                continue
        except Exception as e:                            # noqa: BLE001 - network is messy
            last = e
        time.sleep(0.8 * (attempt + 1))                   # linear backoff for transient errors
    if isinstance(last, urllib.error.HTTPError) and last.code in (403, 429):
        raise RateLimited(f"throttled after {retries} tries: {url}")
    raise last if last else RuntimeError("request failed: " + url)


def search_page(store, category, skip, take, timeout):
    """One catalog page for a category. Returns (count, items)."""
    payload = {
        "attribute": {"name": "categoryIds", "value": category},
        "resultsOptions": {"skip": skip, "take": take, "sortBy": [], "facets": []},
        "customData": {"getEntitiesByAttributeABTest": False, "consent": True},
    }
    res = (http_post_json(SEARCH_URL.format(store=store), payload, timeout=timeout) or {}).get("results") or {}
    return res.get("count") or 0, (res.get("items") or [])


def num(x):
    """Parse a Coop/OFF numeric value ('1,5', '<0.5', '≈2', ['47']) -> float or ''."""
    if isinstance(x, (list, tuple)):
        x = x[0] if x else None
    if x is None:
        return ""
    s = str(x).replace(",", ".").lstrip("<≈~ ").strip()
    try:
        return float(s)
    except ValueError:
        return ""


def price_str(v):
    """Format a numeric price as a Swedish display string: 24 -> '24 kr', 37.81 -> '37,81 kr'."""
    if v in ("", None):
        return ""
    try:
        f = float(str(v).replace(",", "."))
    except ValueError:
        return str(v)
    return (f"{int(f)} kr" if f == int(f)
            else f"{f:.2f}".replace(".", ",") + " kr")


# --------------------------------------------------------------------------- #
# Parse a catalog item into our record shape
# --------------------------------------------------------------------------- #
def parse_macros(p):
    """Pull macros out of a product's `nutrientLinks`; basis from `nutrientInformation`."""
    links = p.get("nutrientLinks") or []
    if not links:
        return None
    header = ((p.get("nutrientInformation") or [{}])[0] or {}).get("header") or {}
    unit = (header.get("nutrientBasisQuantityUnit") or {}).get("value") or ""
    qty = header.get("nutrientBasisQuantity")
    if qty is None:
        qty = (p.get("nutrientBasis") or {}).get("quantity")
    basis = f"{qty if qty is not None else ''} {unit}".strip()
    out = {"basis": basis}
    for link in links:
        desc = (link.get("description") or "").strip().lower()
        val = num(link.get("amount"))
        if desc == "energi":
            out["kcal" if link.get("unit") == "Kilokalori" else "kj"] = val
        elif desc in MACRO:
            out[MACRO[desc]] = val
    # A nutrientLinks block with only micronutrients (no kcal/macros) isn't useful.
    return out if any(k in out for k in ("kcal", "fat", "protein", "carb")) else None


def parse_item(p):
    """Turn one catalog item into a record dict (or None if it carries no id)."""
    ean = (str(p.get("ean") or p.get("id") or "")).strip()
    if not ean:
        return None
    m = parse_macros(p)
    name = (p.get("name") or "").strip()
    price = (p.get("salesPriceData") or {}).get("b2cPrice")   # consumer shelf price
    rec = {
        "ean": ean,
        "article": str(p.get("id") or ean).strip(),   # Coop has no separate article -> id (== ean)
        "name": name,
        "brand": p.get("manufacturerName") or "",
        "weight": p.get("packageSizeInformation") or "",
        "basis": m.get("basis", "") if m else "",
        "price": price if price is not None else "",
        "source": "coop" if m else "",
    }
    for k in ("kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein", "salt"):
        rec[k] = m.get(k, "") if m else ""
    return rec


# --------------------------------------------------------------------------- #
# Phase 1/2 - crawl every food category, paginated
# --------------------------------------------------------------------------- #
def crawl(store, timeout, delay, workers, limit):
    """Return {ean: record} for all food products across the food categories."""
    records = {}
    page_jobs = []        # (category, name, skip) for pages beyond the first

    # Page 0 of each category: gives the count (so we know how many more pages) + items.
    for cat, name in FOOD_CATEGORIES:
        if delay:
            time.sleep(delay)
        try:
            count, items = search_page(store, cat, 0, PAGE, timeout)
        except Exception as e:                            # noqa: BLE001
            print(f"  ! {name} ({cat}) page 0: {e}", file=sys.stderr)
            continue
        for p in items:
            rec = parse_item(p)
            if rec:
                records[rec["ean"]] = rec
        for skip in range(PAGE, count, PAGE):
            page_jobs.append((cat, name, skip))
        print(f"  {name:24} count={count:5}  (running unique {len(records)})", file=sys.stderr)
        if limit and len(records) >= limit:
            return dict(list(records.items())[:limit])

    # Remaining pages in parallel (each is independent once we know its skip).
    def fetch(job):
        cat, name, skip = job
        if delay:
            time.sleep(delay)
        return name, skip, search_page(store, cat, skip, PAGE, timeout)[1]

    print(f"Phase 2: fetching {len(page_jobs)} extra pages with {workers} workers ...",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, j): j for j in page_jobs}
        for fut in as_completed(futs):
            cat, name, skip = futs[fut]
            try:
                _, _, items = fut.result()
            except Exception as e:                        # noqa: BLE001
                print(f"  ! {name} skip={skip}: {e}", file=sys.stderr)
                continue
            for p in items:
                rec = parse_item(p)
                if rec:
                    records[rec["ean"]] = rec

    if limit:
        return dict(list(records.items())[:limit])
    return records


# --------------------------------------------------------------------------- #
# Open Food Facts gap-fill (by EAN) for items Coop lacks macros
# --------------------------------------------------------------------------- #
OFF_UA = "coop-scraper/1.0 (eliasbjoerk@gmail.com)"


def off_by_ean(ean, timeout):
    url = f"https://world.openfoodfacts.org/api/v2/product/{ean}.json?fields=nutriments"
    req = urllib.request.Request(url, headers={"User-Agent": OFF_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                     # noqa: BLE001
        return None
    if d.get("status") != 1:
        return None
    n = (d.get("product") or {}).get("nutriments") or {}
    if not n:
        return None
    return {
        "kcal": num(n.get("energy-kcal_100g")), "kj": num(n.get("energy-kj_100g")),
        "fat": num(n.get("fat_100g")), "satfat": num(n.get("saturated-fat_100g")),
        "carb": num(n.get("carbohydrates_100g")), "sugar": num(n.get("sugars_100g")),
        "fibre": num(n.get("fiber_100g")), "protein": num(n.get("proteins_100g")),
        "salt": num(n.get("salt_100g")),
    }


def off_pass(records, timeout):
    """Fill macros from OFF for rows that have a real (scannable) EAN but no kcal."""
    targets = [r for r in records
               if (r.get("kcal") in ("", None))
               and r.get("ean") and not r["ean"].startswith("2")]    # 2.. = in-store/loose
    print(f"\nOpen Food Facts pass: {len(targets)} items missing macros with a real EAN",
          file=sys.stderr)
    filled = 0
    for i, r in enumerate(targets, 1):
        m = off_by_ean(r["ean"], timeout)
        if m and (m.get("kcal") not in ("", None)):
            r.update(m)
            r["source"] = "off"
            r["basis"] = r.get("basis") or "100 g"
            filled += 1
        time.sleep(0.7)                                   # OFF barcode API ~100/min
        if i % 50 == 0:
            print(f"  OFF {i}/{len(targets)} (filled {filled})", file=sys.stderr)
    print(f"OFF pass done: filled {filled}/{len(targets)}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Cache (resume) helpers - keyed by ean
# --------------------------------------------------------------------------- #
def load_cache():
    done = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done[rec["ean"]] = rec
                except Exception:                         # noqa: BLE001 - skip bad lines
                    pass
    return done


def rewrite_cache(done):
    with open(CACHE, "w", encoding="utf-8") as f:
        for r in done.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def normalize_macros(row):
    """Repair source-side energy/macro defects before writing (in-place).

    Some suppliers mislabel the energy units, so the kJ value lands in `kcal` and the
    kcal value in `kj` (e.g. Chistorra: kcal=1340, kj=320). For any real food kJ is
    ~4.184x kcal, so when kj < kcal AND kcal/kj falls in the ~3-5.5 band it's a unit
    swap -> swap them back (the band avoids touching unrelated single-value glitches).
    Then blank impossible per-100g values (supplier typos): a gram-macro can't exceed
    100 g per 100 g and kcal can't exceed ~900 (pure fat).
    """
    def f(v):
        try:
            return float(str(v).replace(",", ".")) if v not in ("", None) else None
        except ValueError:
            return None
    kcal, kj = f(row.get("kcal")), f(row.get("kj"))
    if kcal is not None and kj is not None and 0 < kj < kcal and 3.0 <= kcal / kj <= 5.5:
        row["kcal"], row["kj"] = row["kj"], row["kcal"]
        kcal = f(row.get("kcal"))
    if kcal is not None and kcal > 950:
        row["kcal"] = ""
    for c in ("fat", "satfat", "carb", "sugar", "fibre", "protein", "salt"):
        v = f(row.get(c))
        if v is not None and v > 100:
            row[c] = ""
    return row


def weight_str(v):
    """Normalize a package-size string: insert one space between the number and its unit
    so '2,2kg' -> '2,2 kg', '450g' -> '450 g'. Blank stays blank; already-spaced values
    are left unchanged."""
    if v in ("", None):
        return ""
    s, out = str(v).strip(), []
    for i, ch in enumerate(s):
        if ch.isalpha() and i > 0 and s[i - 1].isdigit():
            out.append(" ")
        out.append(ch)
    return "".join(out)


def write_outputs(records, base):
    records = sorted(records, key=lambda r: (r.get("name") or "").lower())
    csv_path, json_path = base + ".csv", base + ".json"

    # Project to the output columns and format the price for display ('37,81 kr').
    rows = []
    for r in records:
        row = {k: r.get(k, "") for k in COLUMNS}
        row["price"] = price_str(r.get("price"))
        row["weight"] = weight_str(r.get("weight") or r.get("volume"))
        normalize_macros(row)
        rows.append(row)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    lean_path = os.path.join(os.path.dirname(base) or ".", LEAN_NAME)
    lean_rows = [r for r in records if r.get("ean")]
    with open(lean_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEAN_COLUMNS)
        for r in lean_rows:
            w.writerow([r["ean"], r.get("article", "")])

    with_macros = sum(1 for r in records if r.get("kcal") not in ("", None))
    print(f"  wrote {len(records)} rows ({with_macros} with macros) -> {csv_path}", file=sys.stderr)
    print(f"  wrote {len(lean_rows)} rows -> {lean_path}", file=sys.stderr)
    return csv_path, json_path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Crawl Coop food catalog -> EAN/article/macros index.")
    ap.add_argument("--workers", type=int, default=4, help="parallel page fetches (default 4)")
    ap.add_argument("--delay", type=float, default=0.1, help="seconds between requests per worker")
    ap.add_argument("--timeout", type=int, default=30, help="per-request timeout seconds")
    ap.add_argument("--limit", type=int, default=0, help="only keep first N products (smoke test)")
    ap.add_argument("--store", default=STORE, help=f"Hybris store id (default {STORE})")
    ap.add_argument("--off", action="store_true", help="also fill gaps from Open Food Facts (slow)")
    ap.add_argument("--fresh", action="store_true", help="ignore cache; re-crawl everything")
    ap.add_argument("--build-only", action="store_true", help="skip crawl; rebuild csv/json from cache")
    ap.add_argument("--out", default=os.path.join(HERE, "coop_index"), help="output basename")
    args = ap.parse_args()

    if args.build_only:
        done = load_cache()
        print(f"Build-only: {len(done)} products in cache.", file=sys.stderr)
        write_outputs(list(done.values()), args.out)
        return

    if args.fresh and os.path.exists(CACHE):
        os.remove(CACHE)

    t0 = time.time()
    print(f"Phase 1: crawling {len(FOOD_CATEGORIES)} food categories (store {args.store}) ...",
          file=sys.stderr)
    fresh = crawl(args.store, args.timeout, args.delay, args.workers, args.limit)

    # Upsert into the cache so --build-only / --off persist across runs.
    done = load_cache()
    done.update(fresh)
    rewrite_cache(done)

    records = list((fresh or done).values())
    print(f"\nCrawl done: {len(records)} unique products. Writing outputs ...", file=sys.stderr)
    write_outputs(records, args.out)

    if args.off:
        off_pass(records, args.timeout)
        for r in records:
            done[r["ean"]] = r
        rewrite_cache(done)                               # persist OFF fills for next time
        print("Re-writing outputs with OFF macros ...", file=sys.stderr)
        write_outputs(records, args.out)

    print(f"Done in {time.time()-t0:.0f}s.", file=sys.stderr)


if __name__ == "__main__":
    main()
