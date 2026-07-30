#!/usr/bin/env python3
"""Ingrex - B2B nutraceutical ingredient portal.

Single file, stdlib only. Run:  python3 ingrex.py   ->  http://localhost:8000
Self-check:                     python3 ingrex.py --test
"""
import hashlib
import hmac
import html
import http.server
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import date

# Shared pilot gate. Set INGREX_PW in the host env to require a password to
# enter the site. Unset/empty (local dev, tests) leaves the site open.
AUTH_PW = os.environ.get("INGREX_PW", "")
AUTH_SECRET = (os.environ.get("INGREX_SECRET") or AUTH_PW or "dev-insecure").encode()
COOKIE = "ing_auth"
COOKIE_MAXAGE = 60 * 60 * 24 * 30  # 30 days

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
:root{
  --ink:#0f1f1a;--body:#33443d;--mut:#6b7d75;--line:#e6ece8;--line2:#f0f4f1;
  --bg:#f4f7f5;--card:#fff;--acc:#0d7a56;--acc-d:#0a5d41;--acc-t:#e7f4ee;
  --up:#c1531a;--down:#0d7a56;--gold:#d99a1c;
  --shadow:0 1px 2px rgba(15,31,26,.04),0 4px 16px -8px rgba(15,31,26,.10);
  --radius:14px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:15px;line-height:1.55;color:var(--body);background:var(--bg);
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:var(--acc);text-decoration:none}a:hover{color:var(--acc-d)}
h1{font-size:27px;line-height:1.15;letter-spacing:-.02em;margin:0 0 6px;color:var(--ink);font-weight:700}
h2{font-size:12px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
  color:var(--mut);margin:34px 0 12px}
p{margin:0 0 12px}

/* header */
header{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.82);
  backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:8px;padding:0 24px;height:60px}
.brand{display:flex;align-items:baseline;gap:2px;font-size:21px;font-weight:800;
  letter-spacing:-.03em;color:var(--ink);margin-right:20px}
.brand span{color:var(--acc)}
.brand small{margin-left:10px;font-size:11px;font-weight:600;letter-spacing:.04em;
  color:var(--mut);align-self:center;text-transform:uppercase}
nav{display:flex;gap:4px}
nav a{padding:7px 13px;border-radius:8px;font-size:14px;font-weight:600;color:var(--body)}
nav a:hover{background:var(--acc-t);color:var(--acc-d)}
.spacer{flex:1}
.pill-live{font-size:11px;font-weight:700;color:var(--acc);background:var(--acc-t);
  padding:5px 11px;border-radius:20px;letter-spacing:.03em}

/* layout */
main{max-width:1120px;margin:0 auto;padding:30px 24px 60px}
.lead{color:var(--mut);font-size:15px;max-width:60ch;margin:-2px 0 22px}
.back{display:inline-block;font-size:13px;font-weight:600;color:var(--mut);margin-bottom:14px}
.back:hover{color:var(--acc)}

/* cards */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
a.tile,.tile{display:block;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);color:inherit;
  transition:box-shadow .16s ease,border-color .16s ease,transform .16s ease}
a.tile:hover{border-color:#cfe0d7;box-shadow:0 2px 4px rgba(15,31,26,.05),0 14px 30px -12px rgba(15,31,26,.18);
  transform:translateY(-2px)}
.tile .ttl{font-size:16px;font-weight:700;color:var(--ink);letter-spacing:-.01em;line-height:1.3}
.tile:hover .ttl{color:var(--acc-d)}
.price{font-size:19px;font-weight:700;color:var(--ink);letter-spacing:-.01em}
.price .unit{font-size:13px;font-weight:500;color:var(--mut)}

/* tables */
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);background:var(--card)}
table{width:100%;border-collapse:collapse;font-size:14px}
thead th{background:var(--line2);font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:var(--mut);text-align:left;padding:11px 16px;
  border-bottom:1px solid var(--line);white-space:nowrap}
tbody td{padding:14px 16px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--line2)}

/* tags + badges */
.tag{display:inline-block;font-size:11px;font-weight:600;padding:3px 9px;border-radius:7px;
  background:var(--bg);border:1px solid var(--line);color:var(--mut);margin:2px 3px 2px 0}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}
.kind{font-weight:700;color:#fff;background:var(--acc);border:0;letter-spacing:.01em}
.kind.Trader{background:#6a58c4}.kind.Importer{background:#c47f1c}
.func{background:var(--acc-t);border-color:transparent;color:var(--acc-d)}
.mut{color:var(--mut);font-size:13px}
.metaline{color:var(--mut);font-size:13px;margin:3px 0}

/* forms */
form.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input,select,textarea{font:inherit;font-size:14px;padding:10px 12px;border:1px solid var(--line);
  border-radius:9px;background:#fff;color:var(--ink);transition:border-color .12s,box-shadow .12s}
input::placeholder{color:#9fb0a8}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--acc);
  box-shadow:0 0 0 3px var(--acc-t)}
input[type=search]{flex:1;min-width:240px}
select{cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%236b7d75' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 12px center;padding-right:32px;appearance:none}
button{font:inherit;font-size:14px;font-weight:600;padding:10px 20px;border:0;border-radius:9px;
  background:var(--acc);color:#fff;cursor:pointer;transition:background .12s,transform .06s}
button:hover{background:var(--acc-d)}button:active{transform:translateY(1px)}

/* trend + rating */
.up{color:var(--up);font-weight:700}.down{color:var(--down);font-weight:700}
.trend svg{color:var(--acc);vertical-align:middle}
.stars{color:var(--gold);letter-spacing:1px}
.score{font-weight:700;color:var(--ink)}
.review{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;
  background:var(--card);box-shadow:var(--shadow)}
.review b{color:var(--ink)}
.empty{color:var(--mut);padding:26px 0;text-align:center}
.count{color:var(--mut);font-weight:600;font-size:13px}

footer{max-width:1120px;margin:0 auto;padding:24px;color:var(--mut);font-size:12px;
  border-top:1px solid var(--line);margin-top:20px}

@media(max-width:560px){
  main{padding:22px 16px 48px}header{padding:0 16px}
  h1{font-size:23px}.brand small{display:none}
}
"""

E = html.escape


def page(title, body):
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Ingrex</title><style>{CSS}</style>"
            f"<header><a class=brand href='/'>ingre<span>x</span>"
            f"<small>Nutraceutical sourcing</small></a>"
            f"<nav><a href='/'>Ingredients</a><a href='/vendors'>Vendors</a></nav>"
            f"<span class=spacer></span><span class=pill-live>Pilot</span></header>"
            f"<main>{body}</main>"
            f"<footer>Ingrex · B2B nutraceutical ingredient portal. "
            f"Pilot preview — prices and ratings are sample data, not live quotes.</footer>"
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
    return (f"<span class=trend style='display:inline-flex;align-items:center;gap:8px'>"
            f"<svg width={w} height={h} viewBox='0 0 {w} {h}' preserveAspectRatio=none "
            f"aria-label='12 month price trend'>"
            f"<polyline points='{pts}' fill=none stroke=currentColor "
            f"stroke-width=1.8 stroke-linejoin=round stroke-linecap=round/></svg>"
            f"<span class={cls}>{sign}{pct:.1f}%</span> <span class=mut>12&nbsp;mo</span></span>")


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
        price = (f"<span class=price>₹{r['lo']:,.0f} – ₹{r['hi']:,.0f}"
                 f"<span class=unit> /{E(r['unit'])}</span></span>"
                 if r["lo"] else "<span class=mut>No offers yet</span>")
        funcs = "".join(f"<span class='tag func'>{E(f.strip())}</span>"
                        for f in r['functions'].split(','))
        cards.append(f"""<a class=tile href='/ingredient/{r['id']}'>
          <div class=ttl>{E(r['name'])}</div>
          <div class=metaline>{E(r['category'])} · CAS {E(r['cas'])}</div>
          <div style='margin:12px 0 4px'>{price}</div>
          <div class=count>{r['vendors']} vendor(s)</div>
          <div class=chips style='margin:10px 0'>{funcs}</div>
          <div>{sparkline([(m['month'], m['price']) for m in trend])}</div>
        </a>""")

    return page("Ingredients", f"""
      <h1>Ingredient directory</h1>
      <p class=lead>Search the nutraceutical ingredient catalogue — compare vendor price
         bands, available documents, supplier type and market trend in one view.</p>
      <div class=card><form class=filters method=get action='/'>
        <input type=search name=q placeholder='Search ingredient, CAS, function…' value='{E(q)}'>
        <select name=kind>{opts(VENDOR_KINDS, kind, 'Any vendor type')}</select>
        <select name=doc>{opts(DOC_TYPES, doc, 'Any document')}</select>
        <input name=maxp inputmode=decimal placeholder='Max ₹/unit' value='{E(raw)}' style='width:140px'>
        <button>Search</button>
      </form></div>
      <h2>{len(rows)} ingredient{'' if len(rows) == 1 else 's'}</h2>
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
        <td><a href='/vendor/{o['vid']}'><b>{E(o['vname'])}</b></a>
            <div class=metaline>{E(o['city'])}</div></td>
        <td><span class='tag kind {E(o['kind'])}'>{E(o['kind'])}</span></td>
        <td><span class=price style='font-size:15px'>₹{o['price_min']:,.0f} – ₹{o['price_max']:,.0f}</span>
            <div class=metaline>per {E(o['unit'])}</div></td>
        <td>{E(o['moq'] or '—')}</td>
        <td>{str(o['lead_days']) + ' d' if o['lead_days'] else '—'}</td>
        <td>{stars(o['avg_score'])}<div class=metaline>{o['n_score']} review(s)</div></td>
        <td><div class=chips>{doc_tags(o['docs'])}</div></td></tr>""" for o in offers)

    return page(ing["name"], f"""
      <a class=back href='/'>← Ingredients</a>
      <h1>{E(ing['name'])}</h1>
      <p class=metaline>{E(ing['category'])} · CAS {E(ing['cas'])}</p>
      <div class=chips style='margin:10px 0 18px'>
        {"".join(f"<span class='tag func'>{E(f.strip())}</span>" for f in ing['functions'].split(','))}</div>
      <div class=card style='color:var(--body)'>{E(ing['description'])}</div>
      <h2>Market trend</h2>
      <div class=card>{sparkline([(m['month'], m['price']) for m in trend], 560, 90)}
        <div class=metaline style='margin-top:10px'>Monthly average landed price, ₹/{E(ing['unit'])}
        {('· ' + str(trend[0]['month']) + ' → ' + str(trend[-1]['month'])) if trend else ''}</div></div>
      <h2>{len(offers)} vendor{'' if len(offers) == 1 else 's'}</h2>
      <div class=tablewrap><table>
        <thead><tr><th>Vendor</th><th>Type</th><th>Price range</th><th>MOQ</th>
            <th>Lead</th><th>Rating</th><th>Documents</th></tr></thead>
        <tbody>{rows or "<tr><td colspan=7 class=empty>No vendors listed yet.</td></tr>"}</tbody>
      </table></div>""")


def view_vendors(con):
    rows = con.execute("""
        SELECT v.*, (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) a,
               (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n,
               (SELECT COUNT(*) FROM offer WHERE vendor_id=v.id) items
        FROM vendor v ORDER BY a DESC NULLS LAST, v.name""").fetchall()
    cards = "".join(f"""<a class=tile href='/vendor/{v['id']}'>
        <div class=ttl>{E(v['name'])}</div>
        <div style='margin:10px 0 8px'><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
          <span class=metaline>{E(v['city'])}, {E(v['country'])}</span></div>
        <div style='margin-bottom:6px'>{stars(v['a'])} <span class=count>({v['n']})</span></div>
        <div class=count>{v['items']} ingredient(s) listed</div>
        <div class=chips style='margin-top:10px'>{doc_tags(v['docs'])}</div></a>""" for v in rows)
    return page("Vendors", f"""
      <h1>Vendors</h1>
      <p class=lead>Manufacturers, traders and importers on the platform — ranked by
         client and manufacturer ratings.</p>
      <div class=grid>{cards}</div>""")


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

    items = "".join(f"""<tr><td><a href='/ingredient/{o['iid']}'><b>{E(o['iname'])}</b></a>
        <div class=metaline>{E(o['category'])}</div></td>
        <td><span class=price style='font-size:15px'>₹{o['price_min']:,.0f} – ₹{o['price_max']:,.0f}</span>
            <div class=metaline>per {E(o['unit'])}</div></td>
        <td>{E(o['moq'] or '—')}</td><td>{str(o['lead_days']) + ' d' if o['lead_days'] else '—'}</td>
        <td class=metaline>{E(o['updated'] or '')}</td></tr>""" for o in offers)

    revs = "".join(f"""<div class=review>
        <b>{E(r['rater'])}</b> <span class=tag>{E(r['rater_type'] or 'Client')}</span>
        {stars(r['score'])}
        <div class=metaline style='margin-top:6px'>{E(r['note'] or '')}</div>
        <div class=count style='margin-top:4px'>{E(r['created'] or '')}</div>
        </div>""" for r in reviews) or "<p class=empty>No reviews yet.</p>"

    return page(v["name"], f"""
      <a class=back href='/vendors'>← Vendors</a>
      <h1>{E(v['name'])}</h1>
      <p style='margin-bottom:16px'><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
         <span class=metaline>{E(v['city'])}, {E(v['country'])} · GSTIN {E(v['gst'] or '—')}</span></p>
      <div class=card style='display:flex;align-items:center;gap:14px'>
        <span style='font-size:26px'>{stars(avg)}</span>
        <span class=count>from {n} client / manufacturer review(s)</span></div>
      <h2>Documents on file</h2>
      <div class=card><div class=chips>{doc_tags(v['docs'])}</div></div>
      <h2>{len(offers)} ingredient{'' if len(offers) == 1 else 's'} listed</h2>
      <div class=tablewrap><table>
        <thead><tr><th>Ingredient</th><th>Price range</th><th>MOQ</th><th>Lead</th><th>Updated</th></tr></thead>
        <tbody>{items or "<tr><td colspan=5 class=empty>Nothing listed.</td></tr>"}</tbody>
      </table></div>
      <h2>Rate this vendor</h2>
      <div class=card>
        {f"<p class=down style='margin-top:0'>{E(msg)}</p>" if msg else ""}
        <form class=filters method=post action='/rate'>
          <input type=hidden name=vendor_id value='{vid}'>
          <input name=rater placeholder='Your company' required maxlength=120>
          <select name=rater_type><option>Client</option><option>Manufacturer</option></select>
          <select name=score>{"".join(f"<option value={s}>{s} ★</option>" for s in (5, 4, 3, 2, 1))}</select>
          <input name=note placeholder='Quality, docs, lead time…' maxlength=500 style='flex:1'>
          <button>Submit rating</button>
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


# ---------- auth gate ----------

def auth_token():
    return hmac.new(AUTH_SECRET, b"pilot-v1", hashlib.sha256).hexdigest()


def is_authed(headers):
    if not AUTH_PW:                      # gate disabled
        return True
    want = auth_token()
    for part in headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and hmac.compare_digest(v, want):
            return True
    return False


def login_page(err=""):
    return page("Sign in", f"""
      <h1>ingre<span style='color:var(--acc)'>x</span> · pilot access</h1>
      <p class=mut>This preview is invite-only. Enter the access password your
         Ingrex contact shared. You'll stay signed in on this device.</p>
      <div class=card style='max-width:380px'>
        {f"<p class=up>{E(err)}</p>" if err else ""}
        <form class=filters method=post action='/login'>
          <input type=password name=pw placeholder='Access password' required
                 autofocus style='flex:1' autocomplete=current-password>
          <button>Enter</button>
        </form></div>""")


# ---------- server ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ingrex/0.1"

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, target, cookie=None):
        self.send_response(303)
        self.send_header("Location", target)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def do_HEAD(self):        # health checks / port scans (Render probes with HEAD)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        if url.path == "/login":
            return self._send(login_page())
        if not is_authed(self.headers):
            return self._redirect("/login")
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
        path = urllib.parse.urlparse(self.path).path
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        body = self.rfile.read(n).decode("utf-8", "replace")

        if path == "/login":
            pw = urllib.parse.parse_qs(body).get("pw", [""])[0]
            if AUTH_PW and hmac.compare_digest(pw, AUTH_PW):
                cookie = (f"{COOKIE}={auth_token()}; Max-Age={COOKIE_MAXAGE}; "
                          "Path=/; HttpOnly; SameSite=Lax; Secure")
                return self._redirect("/", cookie)
            time.sleep(1)   # ponytail: crude brute-force damper; use a long passphrase
            return self._send(login_page("Wrong password."), 401)

        if not is_authed(self.headers):
            return self._redirect("/login")
        if path != "/rate":
            return self._send(page("Not found", "<h1>404</h1>"), 404)
        con = connect()
        try:
            vid, msg = post_rate(con, body)
        finally:
            con.close()
        self._redirect(f"/vendor/{vid}?msg={urllib.parse.quote(msg)}" if vid else "/vendors")

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

    # auth gate: token unforgeable, cookie round-trips, gate off when no pw
    global AUTH_PW, AUTH_SECRET
    assert is_authed({}) is True, "gate must be open when no password set"
    AUTH_PW, AUTH_SECRET = "s3cret", b"s3cret"
    try:
        good = auth_token()
        assert is_authed({"Cookie": f"{COOKIE}={good}"}) is True
        assert is_authed({}) is False
        assert is_authed({"Cookie": f"{COOKIE}=deadbeef"}) is False
        assert is_authed({"Cookie": "other=1"}) is False
        # a cookie signed with a different secret must not validate
        AUTH_SECRET = b"different"
        assert is_authed({"Cookie": f"{COOKIE}={good}"}) is False
    finally:
        AUTH_PW, AUTH_SECRET = "", b"dev"

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
        host = os.environ.get("HOST", "0.0.0.0")  # bind all interfaces so hosts can reach it
        print(f"ingrex on http://{host}:{port}  (db: {DB})")
        http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
