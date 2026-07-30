#!/usr/bin/env python3
"""Ingrex - B2B nutraceutical ingredient portal.

Single file, stdlib only. Run:  python3 ingrex.py   ->  http://localhost:8000
Self-check:                     python3 ingrex.py --test
"""
import html
import http.server
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import date

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingrex.db")

DOC_TYPES = ["COA", "MSDS", "Spec Sheet", "GMP", "FSSAI", "ISO 22000",
             "Halal", "Kosher", "Organic (NPOP/USDA)", "Allergen Statement",
             "Heavy Metals Report", "Stability Data"]
VENDOR_KINDS = ["Manufacturer", "Trader", "Importer"]

SCHEMA = """
CREATE TABLE ingredient (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT,
  cas TEXT, functions TEXT, description TEXT, unit TEXT DEFAULT 'kg');
CREATE TABLE vendor (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('Manufacturer','Trader','Importer')),
  city TEXT, country TEXT, gst TEXT,
  -- ponytail: docs as comma list, not a join table. Fixed 12-value vocabulary,
  -- LIKE filter is enough. Normalise if vendors start uploading real files.
  docs TEXT DEFAULT '');
CREATE TABLE offer (
  id INTEGER PRIMARY KEY,
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id),
  vendor_id INTEGER NOT NULL REFERENCES vendor(id),
  price_min REAL NOT NULL, price_max REAL NOT NULL, currency TEXT DEFAULT 'INR',
  unit TEXT DEFAULT 'kg', moq TEXT, lead_days INTEGER, updated TEXT,
  UNIQUE (ingredient_id, vendor_id));
CREATE TABLE rating (
  id INTEGER PRIMARY KEY, vendor_id INTEGER NOT NULL REFERENCES vendor(id),
  rater TEXT NOT NULL, rater_type TEXT, score INTEGER NOT NULL
    CHECK (score BETWEEN 1 AND 5), note TEXT, created TEXT);
CREATE TABLE price_point (
  ingredient_id INTEGER NOT NULL REFERENCES ingredient(id),
  month TEXT NOT NULL, price REAL NOT NULL,
  PRIMARY KEY (ingredient_id, month));
"""

SEED_INGREDIENTS = [
    # name, category, cas, functions, description
    ("Ashwagandha Extract 5% Withanolides", "Herbal Extract", "90147-33-6",
     "Adaptogen, Stress, Sleep", "Withania somnifera root extract, water-ethanol, HPLC assayed."),
    ("Curcumin 95% (Turmeric Extract)", "Herbal Extract", "458-37-7",
     "Anti-inflammatory, Joint", "Curcuma longa rhizome, 95% total curcuminoids by HPLC."),
    ("Vitamin D3 100,000 IU/g (Cholecalciferol)", "Vitamin", "67-97-0",
     "Bone, Immunity", "Oil-based or CWS beadlet, lanolin derived."),
    ("Omega-3 Fish Oil 18/12 TG", "Lipid", "None",
     "Cardio, Brain", "Anchovy/sardine, 18% EPA 12% DHA, IFOS-grade options."),
    ("Hydrolysed Bovine Collagen Peptides", "Protein", "9007-34-5",
     "Skin, Joint", "Type I & III, ~2000 Da, bovine hide, low-odour."),
    ("Melatonin USP", "Active", "73-31-4",
     "Sleep", "Synthetic, USP monograph, 99% assay."),
    ("Magnesium Bisglycinate", "Mineral", "14783-68-7",
     "Sleep, Muscle", "Fully reacted chelate, ~14% elemental Mg."),
    ("L-Theanine 98%", "Amino Acid", "3081-61-6",
     "Focus, Calm", "Fermentation derived, L-isomer 98% min."),
    ("Probiotic Blend 100B CFU/g", "Probiotic", "None",
     "Gut, Immunity", "L. acidophilus + B. lactis, DFM, shelf-stable."),
    ("Whey Protein Isolate 90%", "Protein", "None",
     "Sports, Recovery", "Cross-flow microfiltration, instantised, low lactose."),
]

SEED_VENDORS = [
    # name, kind, city, country, gst, docs
    ("Nutriva Biotech Pvt Ltd", "Manufacturer", "Hyderabad", "India", "36AABCN1234F1Z5",
     "COA,MSDS,Spec Sheet,GMP,FSSAI,ISO 22000,Halal,Heavy Metals Report,Stability Data"),
    ("Vedic Botanicals LLP", "Manufacturer", "Indore", "India", "23AACFV5678K1Z2",
     "COA,MSDS,Spec Sheet,GMP,FSSAI,Organic (NPOP/USDA),Halal,Kosher"),
    ("Meridian Ingredients", "Trader", "Mumbai", "India", "27AAGCM9012L1Z8",
     "COA,MSDS,Spec Sheet,FSSAI"),
    ("Kalyan Global Imports", "Importer", "Chennai", "India", "33AABCK3456M1Z1",
     "COA,MSDS,Spec Sheet,FSSAI,Halal,Allergen Statement"),
    ("Aureus Lifesciences", "Manufacturer", "Ahmedabad", "India", "24AADCA7890N1Z4",
     "COA,MSDS,Spec Sheet,GMP,FSSAI,ISO 22000,Kosher,Heavy Metals Report"),
    ("SeaPure Marine Nutrition", "Manufacturer", "Kochi", "India", "32AABCS2345P1Z7",
     "COA,MSDS,Spec Sheet,GMP,FSSAI,Allergen Statement,Stability Data"),
    ("Orbit Nutra Trading Co", "Trader", "Delhi", "India", "07AAECO6789Q1Z3",
     "COA,Spec Sheet,FSSAI"),
]

# ingredient index, vendor index, price_min, price_max, moq, lead_days
SEED_OFFERS = [
    (0, 0, 1450, 1850, "25 kg", 14), (0, 1, 1300, 1700, "50 kg", 21),
    (0, 2, 1600, 2100, "5 kg", 7), (0, 6, 1550, 2000, "10 kg", 10),
    (1, 1, 2900, 3600, "25 kg", 21), (1, 0, 3100, 3800, "25 kg", 14),
    (1, 2, 3300, 4200, "5 kg", 7),
    (2, 4, 5200, 6400, "5 kg", 18), (2, 3, 4800, 6000, "10 kg", 30),
    (3, 5, 1150, 1500, "200 kg", 25), (3, 3, 1250, 1650, "50 kg", 35),
    (4, 4, 1050, 1400, "100 kg", 20), (4, 3, 980, 1350, "200 kg", 40),
    (4, 2, 1200, 1550, "25 kg", 10),
    (5, 4, 12500, 15500, "1 kg", 15), (5, 6, 13500, 17000, "500 g", 7),
    (6, 4, 890, 1150, "50 kg", 18), (6, 2, 950, 1250, "25 kg", 8),
    (7, 3, 8500, 10500, "5 kg", 30), (7, 0, 9200, 11500, "5 kg", 20),
    (8, 0, 6800, 8600, "5 kg", 21), (8, 5, 7200, 9000, "5 kg", 18),
    (9, 3, 720, 940, "500 kg", 35), (9, 2, 760, 1000, "100 kg", 12),
]

# vendor index, rater, rater_type, score, note
SEED_RATINGS = [
    (0, "Zenith Wellness Labs", "Manufacturer", 5, "COA matched our in-house HPLC every lot. Docs on time."),
    (0, "Prana Formulations", "Manufacturer", 4, "Good quality, lead time slipped a week in Q1."),
    (1, "Ayur Naturals", "Client", 5, "Best curcumin assay consistency we have seen."),
    (1, "Zenith Wellness Labs", "Manufacturer", 4, "Organic cert valid. Packaging could be better."),
    (2, "Corepeak Nutrition", "Manufacturer", 3, "Trader margins high, but stock is always ready."),
    (2, "Prana Formulations", "Manufacturer", 3, "Source mill changes between lots. Ask for lot-wise COA."),
    (3, "SunGrow Health", "Client", 4, "Import paperwork clean, customs clearance handled well."),
    (4, "Corepeak Nutrition", "Manufacturer", 5, "GMP audit passed. Strong technical support."),
    (4, "Ayur Naturals", "Client", 5, "Melatonin USP grade, zero rejections in 8 lots."),
    (5, "SunGrow Health", "Client", 4, "Oxidation values well within spec. IFOS available on request."),
    (6, "Corepeak Nutrition", "Manufacturer", 2, "Two short-shipments. Needs better order tracking."),
]

# ingredient index -> 12 monthly average prices (market trend)
SEED_TREND = {
    0: [1520, 1540, 1580, 1620, 1600, 1650, 1700, 1680, 1720, 1750, 1730, 1690],
    1: [3400, 3350, 3300, 3250, 3300, 3400, 3550, 3600, 3520, 3480, 3420, 3380],
    2: [5400, 5450, 5600, 5750, 5900, 5850, 5700, 5650, 5550, 5500, 5600, 5680],
    3: [1200, 1220, 1260, 1310, 1380, 1420, 1400, 1360, 1330, 1300, 1290, 1310],
    4: [1180, 1160, 1140, 1120, 1100, 1090, 1080, 1070, 1090, 1110, 1130, 1150],
    5: [14800, 14500, 14200, 13900, 13600, 13800, 14100, 14400, 14000, 13700, 13500, 13400],
    6: [980, 990, 1010, 1040, 1030, 1010, 995, 985, 1000, 1020, 1050, 1070],
    7: [9800, 9700, 9500, 9400, 9600, 9900, 10100, 10000, 9800, 9650, 9550, 9500],
    8: [7400, 7450, 7500, 7600, 7750, 7900, 7850, 7800, 7700, 7650, 7600, 7580],
    9: [820, 840, 870, 900, 880, 860, 845, 830, 850, 875, 895, 910],
}


def months(n=12):
    y, m = date.today().year, date.today().month
    out = []
    for i in range(n - 1, -1, -1):
        mm, yy = m - i, y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(f"{yy}-{mm:02d}")
    return out


def connect():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")   # concurrent reads during a write
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db(path=None):
    global DB
    if path:
        DB = path
    fresh = not os.path.exists(DB) or os.path.getsize(DB) == 0
    con = connect()
    if fresh:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO ingredient (name,category,cas,functions,description) VALUES (?,?,?,?,?)",
            SEED_INGREDIENTS)
        con.executemany(
            "INSERT INTO vendor (name,kind,city,country,gst,docs) VALUES (?,?,?,?,?,?)",
            SEED_VENDORS)
        today = date.today().isoformat()
        con.executemany(
            "INSERT INTO offer (ingredient_id,vendor_id,price_min,price_max,moq,lead_days,updated)"
            " VALUES (?,?,?,?,?,?,?)",
            [(i + 1, v + 1, lo, hi, moq, ld, today) for i, v, lo, hi, moq, ld in SEED_OFFERS])
        con.executemany(
            "INSERT INTO rating (vendor_id,rater,rater_type,score,note,created) VALUES (?,?,?,?,?,?)",
            [(v + 1, r, rt, s, n, today) for v, r, rt, s, n in SEED_RATINGS])
        mo = months()
        con.executemany(
            "INSERT INTO price_point (ingredient_id,month,price) VALUES (?,?,?)",
            [(i + 1, mo[k], p) for i, series in SEED_TREND.items()
             for k, p in enumerate(series)])
        con.commit()
    return con


# ---------- rendering ----------

CSS = """
:root{--ink:#12211c;--mut:#5d7168;--line:#dfe7e2;--bg:#f6f8f7;--card:#fff;
--acc:#0f7a5a;--warn:#b4541c}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
color:var(--ink);background:var(--bg)}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--ink);color:#fff;padding:14px 24px;display:flex;align-items:center;gap:18px}
header b{font-size:20px;letter-spacing:-.5px}header b span{color:#6fd1a8}
header a{color:#cfe0d8;font-size:14px}
main{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px;color:var(--mut);
text-transform:uppercase;letter-spacing:.6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut)}
.tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:20px;
background:var(--bg);border:1px solid var(--line);color:var(--mut);margin:1px 2px 1px 0}
.kind{font-weight:600;color:#fff;background:var(--acc);border:0}
.kind.Trader{background:#7a5cc4}.kind.Importer{background:#c47a1c}
.mut{color:var(--mut);font-size:13px}
form.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);
border-radius:7px;background:#fff;color:var(--ink)}
input[type=search]{flex:1;min-width:220px}
button{font:inherit;font-weight:600;padding:8px 16px;border:0;border-radius:7px;
background:var(--acc);color:#fff;cursor:pointer}
.up{color:var(--warn);font-weight:600}.down{color:var(--acc);font-weight:600}
.stars{color:#e0a30c;letter-spacing:1px}
.empty{color:var(--mut);padding:20px 0}
footer{max-width:1080px;margin:0 auto;padding:8px 24px 40px;color:var(--mut);font-size:12px}
"""

E = html.escape


def page(title, body):
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Ingrex</title><style>{CSS}</style>"
            f"<header><b>ingre<span>x</span></b>"
            f"<a href='/'>Ingredients</a><a href='/vendors'>Vendors</a></header>"
            f"<main>{body}</main>"
            f"<footer>Ingrex demo · prices and ratings are seed data, not live quotes.</footer>"
            f"</html>").encode()


def stars(avg):
    if avg is None:
        return "<span class=mut>no ratings</span>"
    full = int(round(avg))
    return f"<span class=stars>{'★' * full}{'☆' * (5 - full)}</span> {avg:.1f}"


def sparkline(points, w=180, h=40):
    """SVG trend line + percent change over the window."""
    vals = [p for _, p in points]
    if len(vals) < 2:
        return "<span class=mut>no trend data</span>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = w / (len(vals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}"
                   for i, v in enumerate(vals))
    pct = (vals[-1] - vals[0]) / vals[0] * 100
    cls, sign = ("up", "+") if pct >= 0 else ("down", "")
    return (f"<svg width={w} height={h} viewBox='0 0 {w} {h}' aria-label='12 month price trend'>"
            f"<polyline points='{pts}' fill=none stroke=currentColor stroke-width=1.8/></svg>"
            f" <span class={cls}>{sign}{pct:.1f}%</span> <span class=mut>12 mo</span>")


def doc_tags(docs):
    return "".join(f"<span class=tag>{E(d)}</span>" for d in docs.split(",") if d) \
        or "<span class=mut>none listed</span>"


# ---------- queries ----------

def search_ingredients(con, q="", kind="", doc="", maxp=None):
    sql = """
    SELECT i.*, COUNT(DISTINCT o.vendor_id) vendors,
           MIN(o.price_min) lo, MAX(o.price_max) hi
    FROM ingredient i
    LEFT JOIN offer o ON o.ingredient_id = i.id
    LEFT JOIN vendor v ON v.id = o.vendor_id
    WHERE 1=1"""
    args = []
    if q:
        sql += " AND (i.name LIKE ? OR i.category LIKE ? OR i.functions LIKE ? OR i.cas LIKE ?)"
        args += [f"%{q}%"] * 4
    if kind:
        sql += " AND v.kind = ?"
        args.append(kind)
    if doc:
        sql += " AND (',' || v.docs || ',') LIKE ?"
        args.append(f"%,{doc},%")
    if maxp is not None:
        sql += " AND o.price_min <= ?"
        args.append(maxp)
    sql += " GROUP BY i.id ORDER BY i.name"
    return con.execute(sql, args).fetchall()


def vendor_rating(con, vendor_id):
    r = con.execute("SELECT AVG(score) a, COUNT(*) n FROM rating WHERE vendor_id=?",
                    (vendor_id,)).fetchone()
    return r["a"], r["n"]


def offers_for_ingredient(con, ing_id):
    return con.execute("""
        SELECT o.*, v.id vid, v.name vname, v.kind, v.city, v.docs,
               (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) avg_score,
               (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n_score
        FROM offer o JOIN vendor v ON v.id=o.vendor_id
        WHERE o.ingredient_id=? ORDER BY o.price_min""", (ing_id,)).fetchall()


# ---------- views ----------

def view_home(con, params):
    q = params.get("q", [""])[0].strip()
    kind = params.get("kind", [""])[0]
    doc = params.get("doc", [""])[0]
    raw = params.get("maxp", [""])[0].strip()
    maxp = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else None
    if kind not in VENDOR_KINDS:
        kind = ""
    if doc not in DOC_TYPES:
        doc = ""

    rows = search_ingredients(con, q, kind, doc, maxp)
    opts = lambda vals, sel, label: (
        f"<option value=''>{label}</option>" +
        "".join(f"<option{' selected' if v == sel else ''}>{E(v)}</option>" for v in vals))

    cards = []
    for r in rows:
        trend = con.execute(
            "SELECT month,price FROM price_point WHERE ingredient_id=? ORDER BY month",
            (r["id"],)).fetchall()
        price = (f"₹{r['lo']:,.0f} – ₹{r['hi']:,.0f} /{E(r['unit'])}"
                 if r["lo"] else "<span class=mut>no offers</span>")
        cards.append(f"""<div class=card>
          <a href='/ingredient/{r['id']}'><b>{E(r['name'])}</b></a>
          <div class=mut>{E(r['category'])} · CAS {E(r['cas'])}</div>
          <div style='margin:8px 0'>{price}</div>
          <div class=mut>{r['vendors']} vendor(s)</div>
          <div>{"".join(f"<span class=tag>{E(f.strip())}</span>" for f in r['functions'].split(','))}</div>
          <div style='margin-top:8px'>{sparkline([(m['month'], m['price']) for m in trend])}</div>
        </div>""")

    return page("Ingredients", f"""
      <h1>Ingredient directory</h1>
      <p class=mut>Search ingredients, compare vendor price bands, check documents and ratings.</p>
      <div class=card><form class=filters method=get action='/'>
        <input type=search name=q placeholder='Ashwagandha, curcumin, CAS, function…' value='{E(q)}'>
        <select name=kind>{opts(VENDOR_KINDS, kind, 'Any vendor type')}</select>
        <select name=doc>{opts(DOC_TYPES, doc, 'Any document')}</select>
        <input name=maxp inputmode=decimal placeholder='Max ₹/unit' value='{E(raw)}' style='width:130px'>
        <button>Search</button>
      </form></div>
      <h2>{len(rows)} ingredient(s)</h2>
      <div class=grid>{"".join(cards) or "<p class=empty>Nothing matched those filters.</p>"}</div>""")


def view_ingredient(con, ing_id):
    ing = con.execute("SELECT * FROM ingredient WHERE id=?", (ing_id,)).fetchone()
    if not ing:
        return None
    offers = offers_for_ingredient(con, ing_id)
    trend = con.execute(
        "SELECT month,price FROM price_point WHERE ingredient_id=? ORDER BY month",
        (ing_id,)).fetchall()

    rows = "".join(f"""<tr>
        <td><a href='/vendor/{o['vid']}'>{E(o['vname'])}</a>
            <div class=mut>{E(o['city'])}</div></td>
        <td><span class='tag kind {E(o['kind'])}'>{E(o['kind'])}</span></td>
        <td>₹{o['price_min']:,.0f} – ₹{o['price_max']:,.0f}<div class=mut>/{E(o['unit'])}</div></td>
        <td>{E(o['moq'] or '-')}</td>
        <td>{o['lead_days'] or '-'} d</td>
        <td>{stars(o['avg_score'])}<div class=mut>{o['n_score']} review(s)</div></td>
        <td>{doc_tags(o['docs'])}</td></tr>""" for o in offers)

    return page(ing["name"], f"""
      <p class=mut><a href='/'>← Ingredients</a></p>
      <h1>{E(ing['name'])}</h1>
      <p class=mut>{E(ing['category'])} · CAS {E(ing['cas'])} · {E(ing['functions'])}</p>
      <div class=card>{E(ing['description'])}</div>
      <h2>Market trend</h2>
      <div class=card>{sparkline([(m['month'], m['price']) for m in trend], 520, 90)}
        <div class=mut>Monthly average landed price, ₹/{E(ing['unit'])}.
        {(str(trend[0]['month']) + ' → ' + str(trend[-1]['month'])) if trend else ''}</div></div>
      <h2>{len(offers)} vendor(s)</h2>
      <div class=card><table>
        <tr><th>Vendor</th><th>Type</th><th>Price range</th><th>MOQ</th>
            <th>Lead</th><th>Rating</th><th>Documents</th></tr>
        {rows or "<tr><td colspan=7 class=empty>No vendors listed yet.</td></tr>"}
      </table></div>""")


def view_vendors(con):
    rows = con.execute("""
        SELECT v.*, (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) a,
               (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n,
               (SELECT COUNT(*) FROM offer WHERE vendor_id=v.id) items
        FROM vendor v ORDER BY a DESC NULLS LAST, v.name""").fetchall()
    cards = "".join(f"""<div class=card>
        <a href='/vendor/{v['id']}'><b>{E(v['name'])}</b></a>
        <div style='margin:6px 0'><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
          <span class=mut>{E(v['city'])}, {E(v['country'])}</span></div>
        <div>{stars(v['a'])} <span class=mut>({v['n']})</span></div>
        <div class=mut>{v['items']} ingredient(s) listed</div>
        <div style='margin-top:6px'>{doc_tags(v['docs'])}</div></div>""" for v in rows)
    return page("Vendors", f"<h1>Vendors</h1><div class=grid>{cards}</div>")


def view_vendor(con, vid, msg=""):
    v = con.execute("SELECT * FROM vendor WHERE id=?", (vid,)).fetchone()
    if not v:
        return None
    avg, n = vendor_rating(con, vid)
    offers = con.execute("""
        SELECT o.*, i.id iid, i.name iname, i.category FROM offer o
        JOIN ingredient i ON i.id=o.ingredient_id WHERE o.vendor_id=? ORDER BY i.name""",
                         (vid,)).fetchall()
    reviews = con.execute(
        "SELECT * FROM rating WHERE vendor_id=? ORDER BY id DESC", (vid,)).fetchall()

    items = "".join(f"""<tr><td><a href='/ingredient/{o['iid']}'>{E(o['iname'])}</a>
        <div class=mut>{E(o['category'])}</div></td>
        <td>₹{o['price_min']:,.0f} – ₹{o['price_max']:,.0f} /{E(o['unit'])}</td>
        <td>{E(o['moq'] or '-')}</td><td>{o['lead_days'] or '-'} d</td>
        <td class=mut>{E(o['updated'] or '')}</td></tr>""" for o in offers)

    revs = "".join(f"""<div class=card><b>{E(r['rater'])}</b>
        <span class=tag>{E(r['rater_type'] or 'Client')}</span> {stars(r['score'])}
        <div class=mut style='margin-top:4px'>{E(r['note'] or '')} · {E(r['created'] or '')}</div>
        </div>""" for r in reviews) or "<p class=empty>No reviews yet.</p>"

    return page(v["name"], f"""
      <p class=mut><a href='/vendors'>← Vendors</a></p>
      <h1>{E(v['name'])}</h1>
      <p><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
         <span class=mut>{E(v['city'])}, {E(v['country'])} · GSTIN {E(v['gst'] or '-')}</span></p>
      <div class=card>{stars(avg)} from {n} client/manufacturer review(s)</div>
      <h2>Documents on file</h2><div class=card>{doc_tags(v['docs'])}</div>
      <h2>{len(offers)} ingredient(s)</h2>
      <div class=card><table>
        <tr><th>Ingredient</th><th>Price range</th><th>MOQ</th><th>Lead</th><th>Updated</th></tr>
        {items or "<tr><td colspan=5 class=empty>Nothing listed.</td></tr>"}</table></div>
      <h2>Rate this vendor</h2>
      <div class=card>
        {f"<p class=down>{E(msg)}</p>" if msg else ""}
        <form class=filters method=post action='/rate'>
          <input type=hidden name=vendor_id value='{vid}'>
          <input name=rater placeholder='Your company' required maxlength=120>
          <select name=rater_type><option>Client</option><option>Manufacturer</option></select>
          <select name=score>{"".join(f"<option value={s}>{s} ★</option>" for s in (5, 4, 3, 2, 1))}</select>
          <input name=note placeholder='Quality, docs, lead time…' maxlength=500 style='flex:1'>
          <button>Submit</button>
        </form></div>
      <h2>Reviews</h2>{revs}""")


def post_rate(con, body):
    f = urllib.parse.parse_qs(body)
    try:
        vid = int(f.get("vendor_id", ["0"])[0])
        score = int(f.get("score", ["0"])[0])
    except ValueError:
        return None, "Bad rating input."
    rater = f.get("rater", [""])[0].strip()[:120]
    rtype = f.get("rater_type", ["Client"])[0]
    note = f.get("note", [""])[0].strip()[:500]
    if not (1 <= score <= 5) or not rater or rtype not in ("Client", "Manufacturer"):
        return vid or None, "Need a company name and a score of 1-5."
    if not con.execute("SELECT 1 FROM vendor WHERE id=?", (vid,)).fetchone():
        return None, "Unknown vendor."
    con.execute("INSERT INTO rating (vendor_id,rater,rater_type,score,note,created)"
                " VALUES (?,?,?,?,?,?)",
                (vid, rater, rtype, score, note, date.today().isoformat()))
    con.commit()
    return vid, "Thanks - rating recorded."


# ---------- server ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ingrex/0.1"

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        con = connect()
        try:
            if url.path == "/":
                out = view_home(con, params)
            elif url.path == "/vendors":
                out = view_vendors(con)
            elif m := re.fullmatch(r"/ingredient/(\d+)", url.path):
                out = view_ingredient(con, int(m[1]))
            elif m := re.fullmatch(r"/vendor/(\d+)", url.path):
                out = view_vendor(con, int(m[1]), params.get("msg", [""])[0][:80])
            else:
                out = None
            self._send(out or page("Not found", "<h1>404</h1><p><a href='/'>Home</a></p>"),
                       200 if out else 404)
        finally:
            con.close()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/rate":
            return self._send(page("Not found", "<h1>404</h1>"), 404)
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        body = self.rfile.read(n).decode("utf-8", "replace")
        con = connect()
        try:
            vid, msg = post_rate(con, body)
        finally:
            con.close()
        target = f"/vendor/{vid}?msg={urllib.parse.quote(msg)}" if vid else "/vendors"
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))


def demo():
    """Self-check: run against a throwaway DB."""
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    con = init_db(tmp)

    assert len(search_ingredients(con)) == len(SEED_INGREDIENTS)
    # text search hits name, category, function and CAS
    assert len(search_ingredients(con, "ashwagandha")) == 1
    assert len(search_ingredients(con, "458-37-7")) == 1
    assert len(search_ingredients(con, "Sleep")) >= 3
    assert search_ingredients(con, "zzzz") == []
    # vendor-type filter: only ingredients offered by an Importer
    imp = {r["name"] for r in search_ingredients(con, kind="Importer")}
    assert imp and all(any(o["kind"] == "Importer" for o in offers_for_ingredient(
        con, con.execute("SELECT id FROM ingredient WHERE name=?", (n,)).fetchone()["id"]))
        for n in imp)
    # doc filter must not match on substring of another doc name
    assert all("GMP" in r["docs"] for r in con.execute(
        "SELECT docs FROM vendor WHERE (','||docs||',') LIKE '%,GMP,%'"))
    assert not [r for r in con.execute(
        "SELECT 1 FROM vendor WHERE (','||docs||',') LIKE '%,MP,%'")]
    # price cap filter
    cheap = search_ingredients(con, maxp=1000)
    assert cheap and all(r["lo"] <= 1000 for r in cheap)

    # ratings aggregate + validation
    before = vendor_rating(con, 1)
    assert before[1] == 2 and abs(before[0] - 4.5) < 1e-9
    assert post_rate(con, "vendor_id=1&score=9&rater=X")[1].startswith("Need")
    assert post_rate(con, "vendor_id=1&score=5&rater=")[1].startswith("Need")
    assert post_rate(con, "vendor_id=999&score=5&rater=X")[0] is None
    assert vendor_rating(con, 1) == before, "invalid ratings must not be stored"
    vid, msg = post_rate(con, "vendor_id=1&score=1&rater=Test+Co&rater_type=Client&note=hi")
    assert vid == 1 and vendor_rating(con, 1)[1] == 3

    # rendering: escapes user input, no crash on any page
    con.execute("INSERT INTO rating (vendor_id,rater,score) VALUES (1,?,3)",
                ("<script>x</script>",))
    con.commit()
    assert b"<script>x" not in view_vendor(con, 1)
    assert b"&lt;script&gt;" in view_vendor(con, 1)
    for i in range(1, len(SEED_INGREDIENTS) + 1):
        assert view_ingredient(con, i)
    for v in range(1, len(SEED_VENDORS) + 1):
        assert view_vendor(con, v)
    assert view_ingredient(con, 9999) is None and view_vendor(con, 9999) is None
    assert view_home(con, {"q": ["x"], "maxp": ["abc"], "kind": ["../etc"]})
    assert view_vendors(con)

    # sparkline: direction and degenerate input
    assert "class=up" in sparkline([("a", 10), ("b", 20)])
    assert "class=down" in sparkline([("a", 20), ("b", 10)])
    assert "no trend" in sparkline([("a", 10)])
    assert "no trend" in sparkline([])
    assert "0.0%" in sparkline([("a", 10), ("b", 10)])

    con.close()
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        demo()
    else:
        init_db()
        port = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PORT", 8000))
        host = os.environ.get("HOST", "127.0.0.1")  # set HOST=0.0.0.0 when hosting
        print(f"ingrex on http://{host}:{port}  (db: {DB})")
        http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
