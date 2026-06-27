#!/usr/bin/env python3
"""
willys_scraper.py - Build an  EAN -> {article number, name, macros}  index from
Willys.se (FOOD ITEMS ONLY), for lookup in Google Sheets.

WHY THIS EXISTS
  Willys has NO public "EAN -> article number" endpoint. Verified June 2026:
    - /search/clean ignores the barcode (returns null; results carry no `ean`)
    - /axfood/rest/p/<EAN> -> HTTP 400 "No product found" (needs the internal code)
    - /axfood/rest/products/ean/<EAN> exists but returns {} for anonymous callers
      (gated behind a logged-in in-store session) - even with cookies + CSRF.
    - category browse listings carry `code` but never the `ean`.
  The EAN is exposed ONLY inside each product's detail response. So we crawl every
  food product's detail (which gives ean + nutrition), cache it, and write a table
  your dad's Google Sheet looks up with VLOOKUP on the scanned barcode.

OUTPUT (written next to this script)
  willys_index.csv        -> full table: ean, article, name, brand, basis, price, macros (for Sheets IMPORTDATA)
  willys_ean_article.csv  -> lean lookup: just ean,article (no macros)
  willys_index.json       -> same data as the full table, as JSON (git-ignored)
  .willys_cache.jsonl     -> resume cache; a re-run skips products already fetched.
                             Delete it (or pass --fresh) to force a full re-crawl.

USAGE
  python willys_scraper.py                # crawl (resumable) + write csv/json
  python willys_scraper.py --build-only   # just rebuild csv/json from the cache (instant)
  python willys_scraper.py --off          # also fill gaps from Open Food Facts (slow)
  python willys_scraper.py --limit 50     # quick smoke test (first 50 products)
  python willys_scraper.py --fresh        # ignore the cache, re-crawl everything
  python willys_scraper.py --workers 4 --delay 0.1   # tune politeness (defaults shown)

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
BASE = "https://www.willys.se"          # Hemkop works too (same Axfood API): https://www.hemkop.se
STORE_ID = "2110"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) willys-scraper/1.0"

# Food categories only (barn & kiosk intentionally excluded - mixed non-food).
FOOD = [
    "kott-chark-och-fagel", "frukt-och-gront", "mejeri-ost-och-agg", "skafferi",
    "brod-och-kakor", "fryst", "fisk-och-skaldjur", "vegetariskt",
    "glass-godis-och-snacks", "dryck", "fardigmat",
]

# Swedish nutrientTypeCode -> our column. 'energi' is handled separately (kcal vs kJ).
MACRO = {
    "fett": "fat", "varav mattat fett": "satfat", "varav mättat fett": "satfat",
    "kolhydrat": "carb", "varav sockerarter": "sugar",
    "fiber": "fibre", "fibrer": "fibre", "kostfiber": "fibre",
    "protein": "protein", "salt": "salt",
}

# Full-index column order. NOTE: keep `ean` first (col A) so VLOOKUP keys off it.
COLUMNS = ["ean", "article", "name", "brand", "basis", "price",
           "kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein",
           "salt", "source"]

# Lean lookup file: just the barcode -> article number (no macros).
LEAN_COLUMNS = ["ean", "article"]
LEAN_NAME = "willys_ean_article.csv"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".willys_cache.jsonl")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class RateLimited(Exception):
    """Raised after exhausting retries against a 403/429 throttle."""


def http_json(url, accept="application/json", timeout=25, retries=5):
    """GET a URL and parse JSON.

    400/404 are real "missing product" answers -> raise immediately.
    403/429 mean Willys is throttling us -> wait (honour Retry-After) and RETRY,
    so a rate-limited product is never silently dropped.
    """
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise
            last = e
            if e.code in (403, 429):
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if (ra and str(ra).isdigit()) else min(45, 4 * (2 ** attempt))
                time.sleep(wait + random.uniform(0, 1.0))    # de-sync the worker threads
                continue
        except Exception as e:                                # noqa: BLE001 - network is messy
            last = e
        time.sleep(0.8 * (attempt + 1))                       # linear backoff for transient errors
    if isinstance(last, urllib.error.HTTPError) and last.code in (403, 429):
        raise RateLimited(f"throttled after {retries} tries: {url}")
    raise last if last else RuntimeError("request failed: " + url)


def num(x):
    """Parse a Willys/OFF numeric string ('1,5', '<0.5', '≈2') -> float or ''."""
    if x is None:
        return ""
    s = str(x).replace(",", ".").lstrip("<≈~ ").strip()
    try:
        return float(s)
    except ValueError:
        return ""


def price_str(v):
    """Format a numeric price as a Swedish display string: 24 -> '24 kr', 99.9 -> '99,90 kr'."""
    if v in ("", None):
        return ""
    try:
        f = float(str(v).replace(",", "."))
    except ValueError:
        return str(v)                                # already a string (e.g. '99,90 kr')
    return (f"{int(f)} kr" if f == int(f)
            else f"{f:.2f}".replace(".", ",") + " kr")


# --------------------------------------------------------------------------- #
# Phase 1 - collect all food product codes from category browse pages
# --------------------------------------------------------------------------- #
def collect_codes(timeout):
    codes = {}                                       # code -> 1 (dedupe across categories)
    for cat in FOOD:
        first = http_json(f"{BASE}/c/{cat}?size=100&page=0", timeout=timeout)
        pages = ((first.get("pagination") or {}).get("numberOfPages")) or 1
        for it in (first.get("results") or []):
            if it.get("code"):
                codes[it["code"]] = 1
        for p in range(1, pages):
            try:
                d = http_json(f"{BASE}/c/{cat}?size=100&page={p}", timeout=timeout)
            except Exception as e:                    # noqa: BLE001
                print(f"  ! {cat} page {p}: {e}", file=sys.stderr)
                continue
            for it in (d.get("results") or []):
                if it.get("code"):
                    codes[it["code"]] = 1
        print(f"  collected {cat:28} (running total {len(codes)})", file=sys.stderr)
    return list(codes)


# --------------------------------------------------------------------------- #
# Phase 2 - fetch each product's detail (ean + macros live here)
# --------------------------------------------------------------------------- #
def parse_macros(p):
    headers = p.get("nutrientHeaders") or []
    if not headers or not (headers[0].get("nutrientDetails") or []):
        return None                                  # no Willys nutrition table
    h = headers[0]                                   # [0]=as sold; [1]=cooked (ignored)
    basis = f"{h.get('nutrientBasisQuantity','')} {h.get('nutrientBasisQuantityMeasurementUnitCode','')}".strip()
    out = {"basis": basis}
    for d in (h.get("nutrientDetails") or []):
        code = (d.get("nutrientTypeCode") or "").strip().lower()
        val = num(d.get("quantityContained"))
        if code == "energi":
            out["kcal" if d.get("measurementUnitCode") == "kilokalori" else "kj"] = val
        elif code in MACRO:
            out[MACRO[code]] = val
    return out


def fetch_detail(code, timeout, delay):
    """Return a record dict for one product code (raises on hard 400/404)."""
    if delay:
        time.sleep(delay)                            # gentle spacing between requests
    p = http_json(f"{BASE}/axfood/rest/p/{code}", timeout=timeout)
    m = parse_macros(p)
    rec = {
        "ean": (p.get("ean") or "").strip(),
        "article": p.get("code") or code,
        "name": (p.get("name") or "").strip(),
        "brand": p.get("manufacturer") or "",
        "volume": p.get("displayVolume") or "",
        "price": p.get("priceValue") if p.get("priceValue") is not None else "",
        "basis": m.get("basis", "") if m else "",
        "source": "willys" if m else "",
    }
    for k in ("kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein", "salt"):
        rec[k] = m.get(k, "") if m else ""
    return rec


# --------------------------------------------------------------------------- #
# Phase 3 - Open Food Facts gap-fill (by EAN only) for items Willys lacks macros
# --------------------------------------------------------------------------- #
OFF_UA = "willys-scraper/1.0 (eliasbjoerk@gmail.com)"   # OFF asks for a contact UA


def off_by_ean(ean, timeout):
    url = f"https://world.openfoodfacts.org/api/v2/product/{ean}.json?fields=nutriments"
    req = urllib.request.Request(url, headers={"User-Agent": OFF_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                # noqa: BLE001
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
               and r.get("ean") and not r["ean"].startswith("2")]   # 2.. = in-store/loose
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
        time.sleep(0.7)                              # OFF barcode API ~100/min
        if i % 50 == 0:
            print(f"  OFF {i}/{len(targets)} (filled {filled})", file=sys.stderr)
    print(f"OFF pass done: filled {filled}/{len(targets)}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Cache (resume) helpers
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
                    done[rec["article"]] = rec
                except Exception:                    # noqa: BLE001 - skip bad lines
                    pass
    return done


def append_cache(rec):
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rewrite_cache(done):
    with open(CACHE, "w", encoding="utf-8") as f:
        for r in done.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(records, base):
    records = sorted(records, key=lambda r: (r.get("name") or "").lower())
    csv_path, json_path = base + ".csv", base + ".json"

    # Project to the output columns and format the price for display ('99,90 kr').
    rows = []
    for r in records:
        row = {k: r.get(k, "") for k in COLUMNS}
        row["price"] = price_str(r.get("price"))
        rows.append(row)

    # Full index (ean, article, name, brand, basis, price, macros, source).
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    # Lean lookup (ean, article) - no macros, for the simplest scan use case.
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
    ap = argparse.ArgumentParser(description="Crawl Willys food catalog -> EAN/article/macros index.")
    ap.add_argument("--workers", type=int, default=4, help="parallel detail fetches (default 4)")
    ap.add_argument("--delay", type=float, default=0.1, help="seconds between requests per worker")
    ap.add_argument("--timeout", type=int, default=25, help="per-request timeout seconds")
    ap.add_argument("--limit", type=int, default=0, help="only fetch first N products (smoke test)")
    ap.add_argument("--off", action="store_true", help="also fill gaps from Open Food Facts (slow)")
    ap.add_argument("--fresh", action="store_true", help="ignore cache; re-crawl everything")
    ap.add_argument("--build-only", action="store_true", help="skip crawl; rebuild csv/json from cache")
    ap.add_argument("--out", default=os.path.join(HERE, "willys_index"), help="output basename")
    args = ap.parse_args()

    # Rebuild outputs from the cache without touching the network.
    if args.build_only:
        done = load_cache()
        print(f"Build-only: {len(done)} products in cache.", file=sys.stderr)
        write_outputs(list(done.values()), args.out)
        return

    if args.fresh and os.path.exists(CACHE):
        os.remove(CACHE)

    t0 = time.time()
    print("Phase 1: collecting food product codes ...", file=sys.stderr)
    codes = collect_codes(args.timeout)
    if args.limit:
        codes = codes[:args.limit]
    print(f"  {len(codes)} unique product codes.", file=sys.stderr)

    done = load_cache()
    todo = [c for c in codes if c not in done]
    print(f"Phase 2: {len(done)} cached, fetching {len(todo)} details "
          f"with {args.workers} workers ...", file=sys.stderr)

    fetched = dropped = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_detail, c, args.timeout, args.delay): c for c in todo}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:                    # noqa: BLE001 - log & continue
                dropped += 1
                print(f"  ! {code}: {e}", file=sys.stderr)
                continue
            done[rec["article"]] = rec
            append_cache(rec)
            fetched += 1
            if fetched % 100 == 0:
                print(f"  fetched {fetched}/{len(todo)}", file=sys.stderr)

    # Always write a usable file right after the crawl (before the slow OFF pass).
    records = [done[c] for c in codes if c in done]
    print(f"\nCrawl done: {len(records)} products cached "
          f"({fetched} new, {dropped} unresolved). Writing outputs ...", file=sys.stderr)
    write_outputs(records, args.out)

    if args.off:
        off_pass(records, args.timeout)
        rewrite_cache(done)                           # persist OFF fills for next time
        print("Re-writing outputs with OFF macros ...", file=sys.stderr)
        write_outputs(records, args.out)

    print(f"Done in {time.time()-t0:.0f}s.", file=sys.stderr)
    if dropped:
        print(f"  {dropped} products were unresolved (rate-limit/404). "
              f"Re-run to pick them up - the crawl is resumable.", file=sys.stderr)


if __name__ == "__main__":
    main()
