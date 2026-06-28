#!/usr/bin/env python3
"""
produce_scraper.py - Build a NAME -> per-100g macros table for loose/weighted
produce (fruit & veg), from Livsmedelsverket's open food-composition database.

WHY THIS EXISTS (and why it is SEPARATE from the store scrapers)
  The store scrapers (willys/hemkop/coop) key on a scannable EAN. Loose produce
  has no usable barcode: the lösvikt code (article `..._KG`, EAN starting `2..`)
  encodes weight/price and can't be scanned off a plain item. So a barcode lookup
  can't serve "I grabbed a cucumber". But produce is GENERIC, so its nutrition has
  one authoritative Swedish source: Livsmedelsverkets livsmedelsdatabas.

  This builds a small curated NAME-keyed table the app looks up by what the user
  TYPES (e.g. "gurka"), not by a scanned code. It does NOT touch the scan pipeline.

HOW THE APP USES IT
  The user enters e.g. "gurka 200g". The app:
    1. matches the typed name against the `query` column,
    2. reads the per-100g macros,
    3. scales them by grams/100  (200 g -> x2.0).
  The CSV always stores per-100g values (basis = "100 g"); scaling is the caller's
  job. `--lookup "gurka 200g"` below does exactly this, both as a demo and a check.

SOURCE
  Livsmedelsverket open API (no key, no auth):
    list:      /livsmedel/api/v1/livsmedel
    nutrients: /livsmedel/api/v1/livsmedel/<nummer>/naringsvarden?sprak=1
  Nutrients are per "100 g ätlig del". euroFIR codes used:
    ENERC(kcal/kJ by unit), FAT, FASAT, CHO, SUGAR, FIBT, PROT, NACL.

OUTPUT (written next to this script)
  produce_nutrition.csv   -> query,name,number,basis,<macros>,source  (one row per typed alias)
  produce_nutrition.json  -> same rows as JSON (git-ignored)
  .produce_cache.jsonl    -> per-food nutrient cache; a re-run skips fetched foods.

USAGE
  python produce_scraper.py                  # fetch (resumable) + write csv/json
  python produce_scraper.py --fresh          # ignore cache; refetch every food
  python produce_scraper.py --build-only     # rebuild csv/json from cache (instant)
  python produce_scraper.py --lookup "gurka 200g"   # demo the app lookup + scaling

Pure standard library - no `pip install`. Works on Python 3.9+.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
API = "https://dataportal.livsmedelsverket.se/livsmedel/api/v1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) produce-scraper/1.0"

# euroFIR nutrient code -> our column. Energy (ENERC) is split by unit below.
EUROFIR = {
    "FAT": "fat", "FASAT": "satfat", "CHO": "carb", "SUGAR": "sugar",
    "FIBT": "fibre", "PROT": "protein", "NACL": "salt",
}

# Output columns. `query` (what the user types) is col A so a lookup keys off it;
# the rest mirror the store index schema so the data stays consistent across files.
COLUMNS = ["query", "name", "number", "basis",
           "kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein",
           "salt", "source"]

# --------------------------------------------------------------------------- #
# Curated produce -> Livsmedelsverket food number.
#   Each entry: (nummer, official SLV name, [typed aliases the app should match]).
# Numbers were resolved against the live DB (June 2026) to the RAW/FRESH staple,
# not a cooked/canned/dried/dish variant. A few aliases fold onto the nearest
# staple where SLV has no separate raw entry (noted): rödlök->Lök gul,
# zucchini->Squash, svamp->Champinjon, spenat->the only (frozen) spinach entry
# (~= fresh), rabarber->the only (cooked, unsweetened) rhubarb entry.
#
# Aliases are Swedish and lowercase. They include the bare singular plus vowel-
# changing plurals that simple prefix-matching can't reach (morot->morötter,
# gurka->gurkor). Plain appended forms (definite "gurkan"/"tomaten", plural
# "citroner") are caught by the longest-prefix fallback in do_lookup(), so they
# need not be listed. The app should match the same way (see README).
#
# Intentionally OMITTED (no clean raw entry in SLV; a wrong answer is worse than
# none): isbergssallad, färska gröna bönor/haricots verts, spritärtor, färsk
# sparris, salladslök/vårlök/schalottenlök, färsk rosmarin/timjan/salvia.
# Dried staples (dadlar, russin, torkad aprikos) are excluded on purpose: they
# are packaged with a real EAN and so belong to the store scrapers, not here.
# --------------------------------------------------------------------------- #
PRODUCE = [
    # --- vegetables / roots ---
    (339,  "Gurka",            ["gurka", "gurkor"]),
    (364,  "Tomat",            ["tomat", "tomater"]),
    (289,  "Morot",            ["morot", "morötter"]),
    (4457, "Potatis rå",       ["potatis"]),
    (3765, "Sötpotatis rå",    ["sötpotatis"]),
    (344,  "Lök gul",          ["lök", "gul lök", "rödlök"]),   # red onion ~= yellow
    (371,  "Vitlök",           ["vitlök"]),
    (354,  "Purjolök",         ["purjolök"]),
    (351,  "Paprika röd",      ["paprika", "paprika röd", "röd paprika", "paprikor"]),
    (350,  "Paprika grön",     ["paprika grön", "grön paprika"]),
    (381,  "Paprika gul",      ["paprika gul", "gul paprika"]),
    (380,  "Chilipeppar färsk", ["chili", "chilipeppar", "röd chili", "chilifrukt"]),
    (325,  "Broccoli",         ["broccoli"]),
    (322,  "Blomkål",          ["blomkål"]),
    (362,  "Squash",           ["squash", "zucchini"]),
    (372,  "Aubergine",        ["aubergine"]),
    (353,  "Pumpa",            ["pumpa"]),
    (361,  "Spenat",           ["spenat"]),                     # only SLV spinach entry
    (348,  "Mangold",          ["mangold"]),
    (370,  "Vitkål",           ["vitkål", "kål"]),
    (355,  "Rödkål",           ["rödkål"]),
    (358,  "Salladskål",       ["salladskål", "kinakål"]),
    (337,  "Grönkål",          ["grönkål"]),
    (327,  "Brysselkål",       ["brysselkål"]),
    (7192, "Sellerikål pak choi", ["pak choi", "bok choy", "sellerikål"]),
    (292,  "Rotselleri",       ["rotselleri", "selleri"]),
    (321,  "Stjälkselleri",    ["stjälkselleri", "blekselleri"]),
    (294,  "Rödbeta",          ["rödbeta", "rödbetor"]),
    (290,  "Palsternacka",     ["palsternacka"]),
    (288,  "Kålrot",           ["kålrot"]),
    (343,  "Kålrabbi",         ["kålrabbi"]),
    (297,  "Majrova",          ["majrova", "rova"]),
    (295,  "Rättika",          ["rättika"]),
    (293,  "Rädisa",           ["rädisa", "rädisor"]),
    (296,  "Rotpersilja",      ["rotpersilja"]),
    (291,  "Pepparrot",        ["pepparrot"]),
    (298,  "Svartrot",         ["svartrot"]),
    (287,  "Jordärtskocka",    ["jordärtskocka"]),
    (342,  "Kronärtskocka",    ["kronärtskocka"]),
    (336,  "Fänkål",           ["fänkål"]),
    (2269, "Ingefära färsk",   ["ingefära"]),
    (320,  "Avokado",          ["avokado"]),
    (345,  "Majskolv",         ["majs", "majskolv"]),
    (359,  "Sockerärtor",      ["sockerärtor", "sockerärt"]),   # NOT generic ärtor (garden peas differ)
    (2561, "Ruccolasallat",    ["ruccola"]),
    (335,  "Endivesallat",     ["endive", "frisé"]),
    # --- mushrooms ---
    (333,  "Champinjon",       ["champinjon", "champinjoner", "svamp"]),
    (5007, "Kantarell gul rå", ["kantarell", "kantareller"]),
    (7034, "Shiitakesvamp",    ["shiitake", "shiitakesvamp"]),
    (7035, "Ostronskivling",   ["ostronskivling"]),
    # --- fresh herbs ---
    (377,  "Dill färsk",       ["dill"]),
    (352,  "Persilja blad",    ["persilja"]),
    (378,  "Gräslök",          ["gräslök"]),
    (379,  "Basilika färsk",   ["basilika"]),
    (7193, "Koriander blad",   ["koriander"]),
    (7194, "Grönmynta blad",   ["mynta", "grönmynta"]),
    # --- fruit / berries ---
    (553,  "Banan",            ["banan", "bananer"]),
    (588,  "Äpple m. skal",    ["äpple", "äpplen"]),
    (583,  "Päron",            ["päron"]),
    (551,  "Apelsin",          ["apelsin", "apelsiner"]),
    (560,  "Småcitrus (clementin/mandarin)", ["clementin", "mandarin", "klementin"]),
    (559,  "Citron",           ["citron"]),
    (572,  "Lime",             ["lime"]),
    (521,  "Grapefrukt",       ["grapefrukt"]),
    (568,  "Kumquat",          ["kumquat"]),
    (587,  "Vindruvor",        ["vindruvor", "druvor", "vindruva"]),
    (526,  "Jordgubbar",       ["jordgubbar", "jordgubb"]),
    (555,  "Blåbär",           ["blåbär"]),
    (523,  "Hallon",           ["hallon"]),
    (554,  "Björnbär",         ["björnbär"]),
    (566,  "Krusbär",          ["krusbär"]),
    (585,  "Vinbär röda",      ["vinbär", "röda vinbär", "rödvinbär"]),
    (586,  "Vinbär svarta",    ["svarta vinbär", "svartvinbär", "svartavinbär"]),
    (584,  "Tranbär",          ["tranbär"]),
    (573,  "Lingon",           ["lingon"]),
    (525,  "Hjortron",         ["hjortron"]),
    (574,  "Mango",            ["mango"]),
    (550,  "Ananas",           ["ananas"]),
    (565,  "Kiwi grön",        ["kiwi"]),
    (576,  "Nektarin",         ["nektarin"]),
    (580,  "Persika nektarin", ["persika"]),
    (552,  "Aprikos",          ["aprikos", "aprikoser"]),
    (582,  "Plommon",          ["plommon"]),
    (571,  "Sötkörsbär",       ["körsbär", "sötkörsbär"]),
    (570,  "Surkörsbär",       ["surkörsbär"]),
    (561,  "Fikon",            ["fikon"]),
    (577,  "Papaya",           ["papaya"]),
    (579,  "Passionsfrukt",    ["passionsfrukt"]),
    (567,  "Physalis",         ["physalis"]),
    (548,  "Rabarber tillagad u. socker", ["rabarber"]),   # only SLV rhubarb entry
    (549,  "Vattenmelon",      ["vattenmelon"]),
    (547,  "Nätmelon",         ["melon", "nätmelon", "cantaloupe"]),
    (546,  "Honungsmelon",     ["honungsmelon"]),
    (520,  "Granatäpple",      ["granatäpple"]),
]

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".produce_cache.jsonl")


# --------------------------------------------------------------------------- #
# HTTP + parsing
# --------------------------------------------------------------------------- #
def http_json(url, timeout=25, retries=4):
    """GET + parse JSON, with linear backoff on transient errors."""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:                       # noqa: BLE001 - network is messy
            last = e
        time.sleep(0.8 * (attempt + 1))
    raise last if last else RuntimeError("request failed: " + url)


def parse_nutrients(rows):
    """Map a /naringsvarden payload -> {kcal, kj, fat, ...} per 100 g."""
    out = {}
    for r in rows:
        code = (r.get("euroFIRkod") or "").strip()
        val = r.get("varde")
        if code == "ENERC":
            out["kcal" if r.get("enhet") == "kcal" else "kj"] = val
        elif code in EUROFIR:
            out[EUROFIR[code]] = val
    return out


def fetch_food(nr, namn, timeout):
    """Fetch one food's per-100g macros -> record dict."""
    rows = http_json(f"{API}/livsmedel/{nr}/naringsvarden?sprak=1", timeout=timeout)
    m = parse_nutrients(rows)
    rec = {"number": nr, "name": namn, "basis": "100 g", "source": "slv"}
    for k in ("kcal", "kj", "fat", "satfat", "carb", "sugar", "fibre", "protein", "salt"):
        rec[k] = m.get(k, "")
    return rec


# --------------------------------------------------------------------------- #
# Cache (resume), keyed by SLV food number
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
                    done[rec["number"]] = rec
                except Exception:                    # noqa: BLE001 - skip bad lines
                    pass
    return done


def append_cache(rec):
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(by_number, base):
    """Explode the curated aliases into one row per typed `query`."""
    rows = []
    for nr, namn, aliases in PRODUCE:
        rec = by_number.get(nr)
        if not rec:
            continue                                 # not fetched yet (e.g. --limit)
        for q in aliases:
            row = {k: rec.get(k, "") for k in COLUMNS}
            row["query"] = q
            rows.append(row)
    rows.sort(key=lambda r: r["query"])

    csv_path, json_path = base + ".csv", base + ".json"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"  wrote {len(rows)} query rows ({len(by_number)} foods) -> {csv_path}",
          file=sys.stderr)
    return csv_path


# --------------------------------------------------------------------------- #
# Lookup demo: parse "gurka 200g", scale per-100g macros to the entered amount.
# --------------------------------------------------------------------------- #
def parse_query(text):
    """'gurka 200g' -> ('gurka', 200.0).  Default 100 g when no amount is given.
    Understands g/gram/hg/kg; everything before the amount is the name."""
    t = text.strip().lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|hg|gram|g)\b", t)
    grams = 100.0
    if m:
        v = float(m.group(1).replace(",", "."))
        grams = {"kg": v * 1000, "hg": v * 100, "g": v, "gram": v}[m.group(2)]
        t = (t[:m.start()] + t[m.end():]).strip()
    return t.strip(), grams


def do_lookup(text, base):
    name, grams = parse_query(text)
    with open(base + ".csv", encoding="utf-8") as f:
        table = {r["query"]: r for r in csv.DictReader(f)}
    hit = table.get(name)
    if not hit:
        # Swedish definite/plural forms ("gurkan", "tomaten", "citroner"):
        # the typed word starts with a stored query -> take the LONGEST (most
        # specific) such query, so "grönkålssallad" beats the bare "kål".
        pref = [q for q in table if name.startswith(q)]
        if pref:
            hit = table[max(pref, key=len)]
        else:
            # user typed a prefix/abbreviation of a stored name -> shortest match.
            cand = [q for q in table if q.startswith(name)]
            hit = table[min(cand, key=len)] if cand else None
    if not hit:
        print(f"no match for '{name}'. Try one of: "
              f"{', '.join(sorted(table)[:12])} ...", file=sys.stderr)
        return
    factor = grams / 100.0
    print(f"{name} {grams:g} g  ->  {hit['name']} (SLV #{hit['number']}, x{factor:g})")
    for k, label in [("kcal", "kcal"), ("fat", "fett"), ("satfat", "mättat"),
                     ("carb", "kolhydrat"), ("sugar", "socker"), ("fibre", "fiber"),
                     ("protein", "protein"), ("salt", "salt")]:
        v = hit.get(k, "")
        if v not in ("", None):
            unit = "" if k == "kcal" else " g"
            print(f"   {label:10s} {float(v) * factor:7.1f}{unit}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Build a name->macros produce table from Livsmedelsverket.")
    ap.add_argument("--fresh", action="store_true", help="ignore cache; refetch every food")
    ap.add_argument("--build-only", action="store_true", help="rebuild csv/json from cache")
    ap.add_argument("--lookup", metavar="TEXT", help='demo a lookup, e.g. "gurka 200g"')
    ap.add_argument("--delay", type=float, default=0.15, help="seconds between requests")
    ap.add_argument("--timeout", type=int, default=25, help="per-request timeout seconds")
    ap.add_argument("--out", default=os.path.join(HERE, "produce_nutrition"),
                    help="output basename")
    args = ap.parse_args()

    if args.lookup:
        do_lookup(args.lookup, args.out)
        return

    if args.build_only:
        done = load_cache()
        print(f"Build-only: {len(done)} foods in cache.", file=sys.stderr)
        write_outputs(done, args.out)
        return

    if args.fresh and os.path.exists(CACHE):
        os.remove(CACHE)

    done = load_cache()
    todo = [(nr, namn) for nr, namn, _ in PRODUCE if nr not in done]
    print(f"{len(done)} cached, fetching {len(todo)} foods from Livsmedelsverket ...",
          file=sys.stderr)
    for i, (nr, namn) in enumerate(todo, 1):
        try:
            rec = fetch_food(nr, namn, args.timeout)
        except Exception as e:                       # noqa: BLE001 - log & continue
            print(f"  ! #{nr} {namn}: {e}", file=sys.stderr)
            continue
        done[nr] = rec
        append_cache(rec)
        if i % 20 == 0:
            print(f"  fetched {i}/{len(todo)}", file=sys.stderr)
        time.sleep(args.delay)

    write_outputs(done, args.out)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
