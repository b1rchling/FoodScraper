/**
 * Willys.se -> Google Sheets  |  barcode (EAN) -> macronutrients, FOOD ITEMS ONLY
 * --------------------------------------------------------------------------
 * HOW TO USE
 *   1) Open your Google Sheet -> Extensions -> Apps Script. Paste this file. Save.
 *   2) Run startBuild() ONCE and click through the authorization prompts.
 *   3) It crawls Willys food categories into the "WillysDB" sheet (resumable, ~30-50 min),
 *      then automatically runs an Open Food Facts (OFF) pass that fills:
 *        - loose / bulk "_KG" produce  -> OFF by NAME (they have no real barcode)
 *        - packaged items lacking Willys nutrition -> OFF by EAN
 *   4) Scan -> macros via the VLOOKUP at the bottom of this file.
 *   5) Re-run rebuild() (or set a weekly trigger on it) to refresh prices/assortment.
 *
 * NOTE: barn & kiosk categories are intentionally excluded (mixed non-food).
 */

var BASE = 'https://www.willys.se';   // Hemkop also works: 'https://www.hemkop.se' (same Axfood API)
var STORE_ID = '2110';
var DB = 'WillysDB';
var Q  = 'WillysQueue';

// Food categories only.
var FOOD = ['kott-chark-och-fagel','frukt-och-gront','mejeri-ost-och-agg','skafferi',
  'brod-och-kakor','fryst','fisk-och-skaldjur','vegetariskt','glass-godis-och-snacks',
  'dryck','fardigmat'];

// Swedish nutrientTypeCode -> column. 'energi' handled separately (kcal vs kJ).
var MACRO = {'fett':'fat','varav mättat fett':'satfat','kolhydrat':'carb',
  'varav sockerarter':'sugar','fiber':'fibre','fibrer':'fibre','kostfiber':'fibre',
  'protein':'protein','salt':'salt'};

var HDR = ['ean','code','name','brand','price','basis','kcal','kj','fat','satfat',
  'carb','sugar','fibre','protein','salt','source'];

/* ---------- 1. KICK OFF ---------- */
function startBuild(){
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var db = sheet_(ss, DB);
  if (db.getLastRow()===0){ db.appendRow(HDR); }
  db.getRange('A:A').setNumberFormat('@');               // keep EANs as text
  var q = sheet_(ss, Q); q.clear();
  var codes = {};
  FOOD.forEach(function(cat){
    var d = get_('/c/'+cat+'?size=100&page=0');
    var pages = (d.pagination && d.pagination.numberOfPages) || 1;
    (d.results||[]).forEach(function(it){ codes[it.code]=1; });
    for (var p=1; p<pages; p++)
      (get_('/c/'+cat+'?size=100&page='+p).results||[]).forEach(function(it){ codes[it.code]=1; });
  });
  var rows = Object.keys(codes).map(function(c){ return [c]; });
  if (rows.length) q.getRange(1,1,rows.length,1).setValues(rows);
  PropertiesService.getScriptProperties().setProperty('cursor','0');
  delTrig_('crawlChunk_'); delTrig_('enrichOFF_');
  ScriptApp.newTrigger('crawlChunk_').timeBased().everyMinutes(5).create();
  Logger.log('Queued '+rows.length+' food products; crawling every 5 min.');
}

/* ---------- 2. WILLYS CRAWL (resumable, trigger target) ---------- */
function crawlChunk_(){
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var q = ss.getSheetByName(Q), db = ss.getSheetByName(DB);
  var props = PropertiesService.getScriptProperties();
  var cur = parseInt(props.getProperty('cursor')||'0',10), total = q.getLastRow();
  if (cur >= total){
    delTrig_('crawlChunk_');
    props.setProperty('offcursor','1');
    delTrig_('enrichOFF_');
    ScriptApp.newTrigger('enrichOFF_').timeBased().everyMinutes(5).create();
    Logger.log('Willys crawl complete ('+total+'); starting Open Food Facts pass.');
    return;
  }
  var slice = q.getRange(cur+1,1,Math.min(1000,total-cur),1).getValues();
  var t0 = Date.now(), out = [], i = 0;
  for (; i < slice.length; i++){
    if (Date.now()-t0 > 250000) break;                   // stay under the 6-min wall
    try { out.push(rec_(slice[i][0])); } catch(e){}
    Utilities.sleep(120);
  }
  if (out.length) db.getRange(db.getLastRow()+1,1,out.length,HDR.length).setValues(out);
  props.setProperty('cursor', String(cur+i));
  Logger.log('Crawl '+(cur+i)+'/'+total);
}

function rec_(code){
  var p = get_('/axfood/rest/p/'+code), m = macros_(p);
  return [(p.ean||'').trim(), p.code||code, p.name||'', p.manufacturer||'', p.priceValue||'',
    m?m.basis:'',
    m?v_(m.kcal):'', m?v_(m.kj):'', m?v_(m.fat):'', m?v_(m.satfat):'',
    m?v_(m.carb):'', m?v_(m.sugar):'', m?v_(m.fibre):'', m?v_(m.protein):'', m?v_(m.salt):'',
    m?'willys':''];
}

function macros_(p){
  var hs = p.nutrientHeaders||[];
  if (!hs.length || !(hs[0].nutrientDetails||[]).length) return null;   // no Willys nutrition
  var h = hs[0];                                          // [0]=as sold; [1]=cooked (ignored)
  var o = { basis:(h.nutrientBasisQuantity||'')+' '+(h.nutrientBasisQuantityMeasurementUnitCode||'') };
  (h.nutrientDetails||[]).forEach(function(d){
    var t=(d.nutrientTypeCode||'').trim().toLowerCase(), val=num_(d.quantityContained);
    if (t==='energi') o[d.measurementUnitCode==='kilokalori'?'kcal':'kj']=val;
    else if (MACRO[t]) o[MACRO[t]]=val;
  });
  return o;
}

/* ---------- 3. OPEN FOOD FACTS PASS (fills gaps; resumable, trigger target) ---------- */
function enrichOFF_(){
  var ss = SpreadsheetApp.getActiveSpreadsheet(), db = ss.getSheetByName(DB);
  var props = PropertiesService.getScriptProperties();
  var cur = parseInt(props.getProperty('offcursor')||'1',10), last = db.getLastRow();
  if (cur >= last){ delTrig_('enrichOFF_'); Logger.log('OFF pass complete.'); return; }
  var win = db.getRange(cur+1,1,Math.min(500,last-cur),HDR.length).getValues();
  var t0 = Date.now(), i = 0;
  for (; i < win.length; i++){
    if (Date.now()-t0 > 250000) break;
    var row = win[i];
    if (row[6]!=='' && row[6]!==null) continue;           // already has kcal
    var code = String(row[1]||''), ean = String(row[0]||''), m = null;
    if (code.slice(-3)==='_KG' || !ean || ean.charAt(0)==='2'){
      m = offByName_(row[2]); Utilities.sleep(6500);       // OFF search: respect ~10/min
    } else {
      m = offByEan_(ean); Utilities.sleep(700);            // OFF barcode: ~100/min
    }
    if (m){
      var rn = cur+1+i;
      db.getRange(rn,7,1,9).setValues([[v_(m.kcal),v_(m.kj),v_(m.fat),v_(m.satfat),
        v_(m.carb),v_(m.sugar),v_(m.fibre),v_(m.protein),v_(m.salt)]]);
      db.getRange(rn,16).setValue('off');
    }
  }
  props.setProperty('offcursor', String(cur+i));
  Logger.log('OFF pass at row '+(cur+i)+'/'+last);
}

function offByEan_(ean){
  try{
    var r = JSON.parse(UrlFetchApp.fetch(
      'https://world.openfoodfacts.org/api/v2/product/'+ean+'.json?fields=nutriments',
      offOpts_()).getContentText());
    if (r.status===1) return offNutr_(r.product && r.product.nutriments);
  }catch(e){}
  return null;
}
function offByName_(name){
  try{
    var url = 'https://world.openfoodfacts.org/cgi/search.pl?search_terms='+
      encodeURIComponent(clean_(name))+'&search_simple=1&action=process&json=1&page_size=1&fields=nutriments';
    var r = JSON.parse(UrlFetchApp.fetch(url, offOpts_()).getContentText());
    if (r.products && r.products.length) return offNutr_(r.products[0].nutriments);
  }catch(e){}
  return null;
}
function offNutr_(n){
  if (!n) return null;
  return { kcal:n['energy-kcal_100g'], kj:n['energy-kj_100g'], fat:n['fat_100g'],
    satfat:n['saturated-fat_100g'], carb:n['carbohydrates_100g'], sugar:n['sugars_100g'],
    fibre:n['fiber_100g'], protein:n['proteins_100g'], salt:n['salt_100g'] };
}
function offOpts_(){ return {headers:{'User-Agent':'willys-macros/1.0 (eliasbjoerk@gmail.com)'},muteHttpExceptions:true}; }
function clean_(name){
  return String(name||'')
    .replace(/klass\s*\d|import|ekologisk|krav|lösvikt|i lösvikt|\beko\b|ca\.?/gi,'')
    .replace(/\d+[.,]?\d*\s*(kg|gram|g|st|p|pack|liter|l|cl|ml)\b/gi,'')
    .replace(/\s+/g,' ').trim();
}

/* ---------- helpers ---------- */
function get_(path){
  var r = UrlFetchApp.fetch(BASE+path,
    {headers:{Accept:'application/json','User-Agent':'willys-macros/1.0'},muteHttpExceptions:true});
  if (r.getResponseCode()!==200) throw new Error('HTTP '+r.getResponseCode()+' '+path);
  return JSON.parse(r.getContentText());
}
function num_(x){ var n=parseFloat(String(x).replace(',','.').replace(/^[<≈~ ]+/,'')); return isNaN(n)?'':n; }
function v_(x){ return (x===undefined||x===null)?'':x; }
function sheet_(ss,n){ return ss.getSheetByName(n)||ss.insertSheet(n); }
function delTrig_(fn){ ScriptApp.getProjectTriggers().forEach(function(t){ if(t.getHandlerFunction()===fn) ScriptApp.deleteTrigger(t); }); }

/* ---------- refresh ---------- */
function rebuild(){
  var ss=SpreadsheetApp.getActiveSpreadsheet(); var db=ss.getSheetByName(DB);
  if (db) db.clearContents();
  startBuild();
}

/* ===========================================================================
 * SCAN FORMULA — on a "Scan" sheet put the scanned barcode in A2, then in B2:
 *
 * =IFERROR(VLOOKUP(IF(LEN(TO_TEXT(A2))=12,"0"&TO_TEXT(A2),TO_TEXT(A2)),
 *          WillysDB!$A:$P, {3,7,9,10,11,12,13,14,15}, FALSE), "not found")
 *
 * Spills: name, kcal, fat, satfat, carb, sugar, fibre, protein, salt  (per 100 g/ml)
 * =========================================================================== */
