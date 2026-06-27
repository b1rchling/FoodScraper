# Willys → Google Sheets: skanna en EAN, få artikelnummer (+ näringsvärden)

Skrapa Willys matvarusortiment med **Python**, så att ett Google Sheet kan slå upp en skannad
streckkod (EAN) och visa **artikelnummer**, produktnamn och näringsvärden.

- **Skrapare:** [`willys_scraper.py`](willys_scraper.py) — enbart Pythons standardbibliotek, ingen `pip install`.
- **Data (hostad för kalkylarket):**
  [`willys_index.csv`](willys_index.csv) — fullständig tabell (artikelnummer + namn + näringsvärden), och
  [`willys_ean_article.csv`](willys_ean_article.csv) — endast `ean,article` (utan näringsvärden).
- **Äldre version (Apps Script):** [`willys-macros.gs`](willys-macros.gs) med tillhörande HANDOFF/TODO.
  Samma idé, men skrapningen körs inuti Google Sheets. Ersatt av Python-skriptet.

---

## Varför vi måste skrapa (det finns ingen genväg från EAN → artikelnummer)

Verifierat mot Willys live-API (juni 2026):

| Försök | Resultat |
|---|---|
| Sök på **EAN** (`/search/clean?q=<ean>`) | `results: null` — sökningen ignorerar streckkoder; resultaten har inte ens ett `ean`-fält |
| Produktdetalj via **EAN** (`/axfood/rest/p/<ean>`) | HTTP 400 "No product found" — kräver den interna **koden**, inte EAN |
| `/axfood/rest/products/ean/<ean>` | HTTP 200 men alltid tom `items: []`, även med cookies+CSRF — kräver en inloggad butikssession |
| **Bläddra** i kategori | innehåller `code` men **aldrig** `ean` |

EAN finns **endast** inuti varje produkts detaljsvar. Därför skrapar vi varje matvaras detaljsida
(`code → ean + näring`), cachar det och slår upp via EAN i kalkylarket.

---

## 1. Kör skraparen

```bash
python willys_scraper.py                # skrapa (kan återupptas) + skriv csv/json
python willys_scraper.py --build-only   # bygg bara om utdatafilerna från cachen (direkt)
python willys_scraper.py --off          # fyll även luckor från Open Food Facts (långsamt, valfritt)
python willys_scraper.py --limit 50     # snabbt test (första 50 produkterna)
python willys_scraper.py --fresh        # ignorera cachen, skrapa om allt
```

Den skriver, bredvid skriptet:

- **`willys_index.csv`** ← fullständig uppslagstabell för Google Sheets
- **`willys_ean_article.csv`** ← enkel `ean,article`-tabell (utan näringsvärden)
- `willys_index.json` ← samma data som fullständiga tabellen, som JSON (ignoreras av git)
- `.willys_cache.jsonl` ← cache för återupptagning. Skrapningen **kan återupptas** — om Willys
  hastighetsbegränsar dig (HTTP 403), kör bara igen så hämtas endast det som saknas. Radera den
  (eller `--fresh`) för att börja om.

Skrapningen är skonsam som standard (4 trådar, kort fördröjning) och **gör nya försök vid 403/429**
istället för att tappa produkter. Hela sortimentet ≈ 7 600 produkter, ~10–20 min.

**Kolumner i `willys_index.csv`** (`A`→`P`, rubrikerna är på engelska eftersom de är själva fil-rubrikerna):

| A | B | C | D | E | F | G | H | I | J | K | L | M | N | O | P |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ean | article | name | brand | basis | price | kcal | kj | fat | satfat | carb | sugar | fibre | protein | salt | source |

`article` är Willys artikelnummer (t.ex. `101278894_ST`). `name` är produktnamnet (t.ex.
`Trocadero Zero Sugar Läsk Pet 1,5l`). `price` är hyllpriset som visningstext (t.ex. `24 kr`
eller `15,04 kr`). Näringsvärden är per 100 g/ml. `source` = `willys`, `off` (Open Food Facts) eller tomt.

**`willys_ean_article.csv`** har bara två kolumner — `ean,article` — för enklaste uppslag.

---

## 2. Få in det i Google Sheets

> **Obs:** Google Sheets kan **inte** läsa JSON direkt — de enda funktionerna som hämtar från en URL
> är `IMPORTDATA` (CSV/TSV), `IMPORTXML`, `IMPORTHTML`, `IMPORTFEED`. Därför använder vi **CSV**, inte JSON.

### Automatisk uppdatering från den hostade CSV-filen

Repot är publikt, så CSV-filen serveras direkt från GitHub. I en flik som heter **`DB`**, cell **`A1`**
— välj den fil du vill ha:

```
=IMPORTDATA("https://raw.githubusercontent.com/b1rchling/FoodScraper/main/willys_index.csv")
```
…eller den enkla versionen (bara `ean,article`):
```
=IMPORTDATA("https://raw.githubusercontent.com/b1rchling/FoodScraper/main/willys_ean_article.csv")
```

Hela tabellen fylls i i `DB` och hämtas om automatiskt (~varje timme). Om Sheets någon gång krånglar
med raw-URL:en, byt värd till jsDelivr-spegeln (`https://cdn.jsdelivr.net/gh/b1rchling/FoodScraper@main/<fil>.csv`).
Notera att `raw.githubusercontent.com` uppdateras inom minuter, medan jsDelivr kan ligga efter några
timmar efter en uppdatering.

_(Vill du inte hosta? Du kan också **Arkiv → Importera → Ladda upp** CSV-filen till `DB`-fliken.)_

### Skannformeln

På en flik (t.ex. **`Scan`**): den skannade streckkoden hamnar i **`A2`**. Skriv sedan i **`B2`**:

**Fullständig tabell** (`willys_index.csv`) — ger artikelnummer + namn + näringsvärden:
```
=IFERROR(VLOOKUP(TO_TEXT(A2),{ARRAYFORMULA(TO_TEXT(DB!$A:$A)),DB!$B:$O},{2,3,6,8,9,10,11,12,13,14},FALSE),"hittas ej")
```
Sprids över raden: **artikelnummer · namn · kcal · fett · mättat fett · kolhydrater · socker · fiber · protein · salt** (etiketter i `B1:K1`).

**Enkelt uppslag** (`willys_ean_article.csv`) — ger bara artikelnumret:
```
=IFERROR(VLOOKUP(TO_TEXT(A2),{ARRAYFORMULA(TO_TEXT(DB!$A:$A)),DB!$B:$B},2,FALSE),"hittas ej")
```

> `TO_TEXT(...)` på båda sidor gör att matchningen fungerar oavsett om Sheets importerar EAN-kolumnen
> som text eller tal. (Datan har inga EAN med inledande nolla, så inget går förlorat.)
> Fliken **måste** heta `DB` för att `DB!`-referenserna ska fungera.

**Snabbtest:** EAN `7310401034584` → artikelnummer `101278894_ST`, Trocadero Zero (~2 kcal/100 ml).

---

## 3. Uppdatera senare (priser och sortiment ändras)

```bash
python willys_scraper.py                       # skrapa om (kan återupptas)
git add willys_index.csv willys_ean_article.csv && git commit -m "Uppdatera data" && git push
```

Kalkylarkets `IMPORTDATA` hämtar den nya CSV-filen automatiskt inom ~en timme.

---

## Att tänka på & begränsningar

- **Lösvikt** (artikelnummer `…_KG`, EAN som börjar på `2…`, ~300 varor) har ofta näringsvärden i
  Willys men en butiksintern streckkod som inte går att skanna från en vanlig förpackning — finns
  med i tabellen, men går inte alltid att skanna.
- **Open Food Facts-passet** (`--off`) fyller bara varor som har en riktig EAN och saknar
  Willys-näring. Det är långsamt (OFF begränsar ~100/min) och ger lite, så det är valfritt.
- **Var skonsam:** standard 4 trådar + fördröjning. Hela skrapningen är ~7 600 anrop; överbelasta inte.
- **Andra kedjor:** Hemköp använder samma Axfood-API — sätt `BASE = "https://www.hemkop.se"`.
  ICA/Coop kräver en annan adapter (se den äldre HANDOFF-filen).
