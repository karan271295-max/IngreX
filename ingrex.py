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
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime

# Invite-only gate. Access needs an invite code (see ensure_invites): set
# INGREX_ADMIN_CODE (master admin) and optionally INGREX_INVITES on the host.
# With no invite codes configured, the site is open (local dev, tests).
# INGREX_SECRET keeps auth cookies valid across restarts — set it in production.
AUTH_SECRET = (os.environ.get("INGREX_SECRET") or "ingrex-pilot-secret").encode()
COOKIE = "ing_auth"
COOKIE_MAXAGE = 60 * 60 * 24 * 30  # 30 days

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ingrex.db")
LOGIN_VIDEO = os.path.join(HERE, "185365-875417518.mp4")  # served at /bg.mp4

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
    ensure_invites(con)
    return con


def ensure_invites(con):
    """Invite table + env-seeded codes. Runs every start so INGREX_ADMIN_CODE /
    INGREX_INVITES survive the free-tier's ephemeral DB (which resets per deploy)."""
    con.execute("""CREATE TABLE IF NOT EXISTS invite(
        code TEXT PRIMARY KEY, note TEXT, is_admin INTEGER DEFAULT 0,
        revoked INTEGER DEFAULT 0, created TEXT)""")
    today = date.today().isoformat()
    admin = os.environ.get("INGREX_ADMIN_CODE", "").strip()
    if admin:
        con.execute("INSERT OR IGNORE INTO invite(code,note,is_admin,created) VALUES(?,?,1,?)",
                    (admin, "Master admin", today))
        con.execute("UPDATE invite SET is_admin=1, revoked=0 WHERE code=?", (admin,))
    for pair in os.environ.get("INGREX_INVITES", "").split(","):
        code, _, note = pair.strip().partition(":")
        code = code.strip()
        if code:
            con.execute("INSERT OR IGNORE INTO invite(code,note,created) VALUES(?,?,?)",
                        (code, note.strip() or code, today))
    con.commit()


# ---------- rendering ----------

CSS = """
:root{
  --ink:#0f1f1a;--body:#33443d;--mut:#6b7d75;--line:#e6ece8;--line2:#f0f4f1;
  --bg:#f4f7f5;--card:#fff;--acc:#0d7a56;--acc-d:#0a5d41;--acc-t:#e7f4ee;
  --sb:#20293a;--up:#c1531a;--down:#0d7a56;--gold:#d99a1c;
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

/* app shell — CoreUI-style dark sidebar */
.shell{display:flex;min-height:100vh}
.side{width:224px;flex:none;position:sticky;top:0;height:100vh;overflow:auto;
  background:var(--sb);color:rgba(255,255,255,.6);display:flex;flex-direction:column}
.sidebar-header{display:flex;align-items:center;padding:0 16px;height:60px;flex:none;
  border-bottom:1px solid rgba(255,255,255,.08)}
.side .brand{display:flex;align-items:center;gap:9px;color:#fff}
.side .brand .mk{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;
  font-weight:800;font-size:16px;color:#fff;background:linear-gradient(135deg,#12b884,#0a5d41)}
.side .brand .nm{font-size:19px;font-weight:800;letter-spacing:-.03em;line-height:1}
.side .brand .nm span{color:#4fe0a6}
.side .brand small{display:block;font-size:9px;font-weight:600;color:rgba(255,255,255,.4);
  letter-spacing:.02em;margin-top:2px}
/* CoreUI sidebar-nav */
.sidebar-nav{list-style:none;margin:0;padding:8px 0;display:flex;flex-direction:column;
  flex:1;min-height:0}
.nav-title{padding:16px 16px 8px;font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;color:rgba(255,255,255,.38)}
.nav-item{position:relative}
.nav-link{display:flex;align-items:center;gap:12px;padding:11px 16px;font-size:14px;
  font-weight:500;color:rgba(255,255,255,.62);text-decoration:none;
  transition:background .14s,color .14s}
.nav-link:hover{color:#fff;background:rgba(255,255,255,.05)}
.nav-link.active{color:#fff;background:rgba(255,255,255,.09);box-shadow:inset 2px 0 0 var(--acc)}
.nav-icon{width:20px;height:20px;flex:none;stroke:rgba(255,255,255,.5);stroke-width:1.9;
  fill:none;stroke-linecap:round;stroke-linejoin:round}
.nav-link:hover .nav-icon{stroke:#fff}
.nav-link.active .nav-icon{stroke:#4fe0a6}
.nav-item.disabled .nav-link{color:rgba(255,255,255,.32);cursor:default}
.nav-item.disabled .nav-icon{stroke:rgba(255,255,255,.28)}
.nav-badge{margin-left:auto;font-size:9px;font-weight:700;letter-spacing:.05em;
  padding:2px 7px;border-radius:20px}
.nav-badge.new{background:var(--acc);color:#fff}
.nav-badge.soon{background:rgba(255,255,255,.1);color:rgba(255,255,255,.5)}
.mt-auto{margin-top:auto}
.side .me{display:flex;align-items:center;gap:10px;padding:14px 16px;
  border-top:1px solid rgba(255,255,255,.08)}
.av{width:34px;height:34px;border-radius:50%;flex:none;display:grid;place-items:center;
  font-weight:700;font-size:12px;color:#fff;background:linear-gradient(135deg,#3a4a54,#1c2632)}
.me .who{min-width:0;flex:1}
.me .nm{color:#fff;font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.me .rl{color:rgba(255,255,255,.45);font-size:11px}
.logout{flex:none;width:32px;height:32px;border-radius:8px;display:grid;place-items:center;
  color:rgba(255,255,255,.5);background:rgba(255,255,255,.05)}
.logout:hover{color:#fff;background:rgba(255,255,255,.12)}
.logout svg{width:17px;height:17px;stroke:currentColor;stroke-width:2;fill:none;
  stroke-linecap:round;stroke-linejoin:round}

.content{flex:1;min-width:0;display:flex;flex-direction:column}
.top{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;
  padding:12px 26px;background:rgba(244,247,245,.86);backdrop-filter:saturate(180%) blur(10px);
  border-bottom:1px solid var(--line)}
.top form{flex:1;max-width:660px;position:relative}
.top form svg{position:absolute;left:15px;top:50%;transform:translateY(-50%);
  width:17px;height:17px;stroke:#9fb0a8;stroke-width:2;fill:none}
.top input{width:100%;padding:11px 14px 11px 42px;border-radius:11px;font-size:14px;
  border:1px solid var(--line);background:#fff}
.live{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;
  color:var(--acc-d);background:var(--acc-t);border:1px solid #cfe8dc;
  padding:8px 13px;border-radius:20px;white-space:nowrap}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--acc);
  box-shadow:0 0 0 0 rgba(13,122,86,.5);animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(13,122,86,.5)}
  70%{box-shadow:0 0 0 7px rgba(13,122,86,0)}100%{box-shadow:0 0 0 0 rgba(13,122,86,0)}}
/* notifications dropdown (native details) */
.notif{position:relative}
.notif summary{list-style:none;cursor:pointer;width:40px;height:40px;border-radius:11px;
  display:grid;place-items:center;background:#fff;border:1px solid var(--line);color:var(--mut);
  position:relative}
.notif summary::-webkit-details-marker{display:none}
.notif summary:hover{border-color:#cfe0d7;color:var(--acc-d)}
.notif[open] summary{border-color:var(--acc);color:var(--acc-d)}
.notif .dot{position:absolute;top:-6px;right:-6px;min-width:18px;height:18px;padding:0 5px;
  border-radius:20px;background:var(--acc);color:#fff;font-size:10px;font-weight:700;
  display:grid;place-items:center}
.notif .menu{position:absolute;top:48px;right:0;width:320px;max-height:70vh;overflow:auto;
  background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 20px 50px -16px rgba(15,31,26,.3);
  z-index:30;padding:6px}
.mhead{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:var(--mut);padding:10px 12px 6px}
.nitem{display:flex;gap:11px;align-items:flex-start;padding:10px 12px;border-radius:10px;color:inherit}
.nitem:hover{background:var(--line2)}
.nmark{flex:none;width:24px;height:24px;border-radius:7px;display:grid;place-items:center;
  font-size:12px;font-weight:700}
.nmark.up{background:#fbe9df;color:var(--up)}.nmark.down{background:var(--acc-t);color:var(--acc-d)}
.nmark.star{background:#fbf1d8;color:var(--gold)}
.ntext{min-width:0}.ntext b{color:var(--ink);font-size:13px;display:block;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nsub{color:var(--mut);font-size:12px;display:block;line-height:1.35}
.nempty{padding:20px;text-align:center;color:var(--mut);font-size:13px}
.mfoot{display:block;text-align:center;font-size:12px;font-weight:600;padding:10px;
  border-top:1px solid var(--line);margin-top:4px}

/* watchlist star on cards */
.iwrap{position:relative}
.star{position:absolute;top:10px;right:10px;z-index:2;width:30px;height:30px;border-radius:50%;
  display:grid;place-items:center;font-size:15px;text-decoration:none;color:#b9c6bf;
  background:rgba(255,255,255,.92);border:1px solid var(--line);box-shadow:var(--shadow)}
.star:hover{color:var(--gold);border-color:#f0d9a0}
.star.on{color:var(--gold);border-color:#f0d9a0;background:#fffdf5}

/* market movers panel */
.mover{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}
.mover:last-child{border-bottom:0}
.mover .nm{font-size:14px;font-weight:700;color:var(--ink)}
.mover .pc{margin-left:auto;font-weight:700;font-size:14px;display:flex;align-items:center;gap:5px}
.mover .pr{color:var(--mut);font-size:12px}
.wrap{padding:26px 28px 60px;max-width:1200px;width:100%}
.lead{color:var(--mut);font-size:15px;max-width:60ch;margin:-2px 0 22px}
.back{display:inline-block;font-size:13px;font-weight:600;color:var(--mut);margin-bottom:14px}
.back:hover{color:var(--acc)}
.titlerow{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.wbtn{flex:none;font-size:13px;font-weight:700;padding:10px 16px;border-radius:10px;
  border:1px solid var(--line);background:#fff;color:var(--body);white-space:nowrap;margin-top:4px}
.wbtn:hover{border-color:#f0d9a0;color:var(--gold)}
.wbtn.on{background:#fffdf5;border-color:#f0d9a0;color:var(--gold)}

/* dashboard */
.hi h1{font-size:26px;margin:0 0 4px}
.hi .sub{color:var(--mut);margin-bottom:22px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:22px}
.stat{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px 20px;
  box-shadow:var(--shadow)}
.stat .l{color:var(--mut);font-size:13px;font-weight:600}
.stat .v{font-size:28px;font-weight:800;color:var(--ink);letter-spacing:-.02em;margin:8px 0 6px}
.stat .d{font-size:12px;color:var(--mut)}.stat .d b{color:var(--acc);font-weight:700}
.panel{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px 22px 6px;
  box-shadow:var(--shadow);margin-bottom:18px}
.panel.pad{padding:22px}
.ph{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.ph h3{font-size:17px;font-weight:700;color:var(--ink);margin:0}
.ph a{font-size:13px;font-weight:600}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 18px}
.pill{padding:8px 15px;border-radius:20px;border:1px solid var(--line);background:#fff;
  font-size:13px;font-weight:600;color:var(--body);cursor:pointer}
.pill:hover{border-color:#cfe0d7;color:var(--acc-d)}
.pill.on{background:var(--acc);color:#fff;border-color:transparent}
.icards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
a.icard{display:block;border:1px solid var(--line);border-radius:16px;padding:12px 12px 14px;
  color:inherit;background:#fff;transition:box-shadow .16s,border-color .16s,transform .16s}
a.icard:hover{border-color:#cfe0d7;transform:translateY(-2px);
  box-shadow:0 14px 30px -14px rgba(15,31,26,.28)}
.icard{padding:16px}
.icard .irate{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;color:var(--ink)}
.icard .irate .st{color:var(--gold);letter-spacing:.5px}
.icard .irate .new{color:var(--mut);font-weight:600}
.icard .icat{font-size:12px;font-weight:600;color:var(--mut);margin-top:8px}
.icard .inm{font-size:16px;font-weight:700;color:var(--ink);line-height:1.32;margin:4px 0 2px;
  min-height:2.4em}
.iwrap .star{top:14px;right:14px}
.icard .iprice{font-size:16px;font-weight:800;color:var(--ink);margin-top:6px}
.icard .iprice .unit{font-size:12px;font-weight:500;color:var(--mut)}
.icard .isup{color:var(--mut);font-size:12.5px;font-weight:600;margin-top:3px}
.icard .foot{display:flex;align-items:center;justify-content:space-between;margin-top:11px;
  padding-top:10px;border-top:1px solid var(--line2)}
.icard .ibadge{font-size:11px;font-weight:800;padding:4px 9px;border-radius:20px}
.icard .ibadge.down{background:var(--acc-t);color:var(--acc-d)}
.icard .ibadge.up{background:#fbe9df;color:var(--up)}
.icard .ibadge.flat{background:var(--bg);color:var(--mut)}
.icard .iupd{font-size:11px;color:var(--mut);font-weight:600}
.xbtn{font-size:12px;font-weight:700;padding:6px 12px;border-radius:8px;background:#fff;
  color:var(--up);border:1px solid #e6c3ad;cursor:pointer}
.xbtn:hover{background:#fbe9df}
code.inv{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--line2);
  padding:2px 7px;border-radius:6px;color:var(--ink)}
.duo{display:grid;grid-template-columns:1.5fr 1fr;gap:18px;align-items:start}
.chartbox{margin-top:14px}
.chartbox svg{width:100%;height:auto;display:block}
.axl{fill:var(--mut);font-size:11px;font-family:system-ui,sans-serif}
.sup{display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid var(--line)}
.sup:last-child{border-bottom:0}
.sup .av{background:linear-gradient(135deg,#0d7a56,#12b884)}
.sup .nm{font-size:14px;font-weight:700;color:var(--ink)}
.sup .lc{color:var(--mut);font-size:12px}
.sup .rt{margin-left:auto;text-align:right}
.sup .rt .s{font-weight:700;color:var(--ink);font-size:14px}
.sup .rt .n{color:var(--mut);font-size:11px}
.sup .btn{margin-left:12px;font-size:12px;font-weight:700;color:var(--acc-d);
  border:1px solid var(--line);padding:7px 13px;border-radius:9px}

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

footer{padding:20px 28px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}

@media(max-width:960px){
  .stats{grid-template-columns:repeat(2,1fr)}.duo{grid-template-columns:1fr}
  .side{width:200px}
}
@media(max-width:720px){
  .shell{flex-direction:column}
  .side{position:static;width:100%;height:auto;flex-direction:column}
  .sidebar-nav{flex-direction:row;flex-wrap:wrap;padding:6px}
  .nav-title,.me,.side .brand small{display:none}
  .nav-link{padding:8px 12px}.nav-badge{display:none}
  .top{padding:12px 16px}.wrap{padding:20px 16px 48px}
  .stats{grid-template-columns:1fr 1fr}.hi h1{font-size:22px}
}"""

E = html.escape


# nav: label, href, active-key, svg-path (24x24), optional 'soon'
NAV = [
    ("Dashboard", "/", "dashboard", "M3 12h7V3H3zM14 21h7v-9h-7zM14 3v6h7V3zM3 21h7v-6H3z"),
    ("Search Ingredients", "/search", "search", "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3"),
    ("Suppliers", "/vendors", "suppliers", "M3 21V8l9-5 9 5v13M9 21v-6h6v6"),
    ("Market Insights", None, "insights", "M4 19V5m0 14h16M8 15l3-4 3 2 4-6"),
    ("My Reviews", None, "reviews", "M12 3l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 18l-5.8 3 1.1-6.5L2.6 9.8l6.5-.9z"),
    ("Documents", None, "docs", "M14 3H6v18h12V7zM14 3v4h4"),
    ("Watchlist", "/watchlist", "watch", "M20.8 6a5.5 5.5 0 0 0-9-1.7L12 5l.2-.7A5.5 5.5 0 1 0 4 12l8 8 8-8a5.5 5.5 0 0 0 .8-6z"),
]


def initials(name):
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    return ((words[0][:2] if len(words) == 1 else words[0][0] + words[1][0]).upper()
            if words else "IX")


def icon(path):
    return (f"<svg class=nav-icon viewBox='0 0 24 24' aria-hidden=true>"
            f"<path d='{path}'/></svg>")


def sidebar(active):
    nav_items = list(NAV)
    if is_admin():
        nav_items.append(("Admin", "/admin", "admin",
                          "M12 2l7 4v6c0 5-3.5 8-7 10-3.5-2-7-5-7-10V6z"))
    lis = "<li class=nav-title>Platform</li>"
    for label, href, key, path in nav_items:
        badge = ("<span class='nav-badge new'>NEW</span>" if key == "watch" else "")
        if href:
            act = " active" if key == active else ""
            lis += (f"<li class=nav-item><a class='nav-link{act}' href='{href}'>"
                    f"{icon(path)}{label}{badge}</a></li>")
        else:
            lis += (f"<li class='nav-item disabled'><a class=nav-link>"
                    f"{icon(path)}{label}<span class='nav-badge soon'>SOON</span></a></li>")
    lis += ("<li class='nav-item mt-auto'><a class=nav-link href='/'>"
            "<svg class=nav-icon viewBox='0 0 24 24' aria-hidden=true>"
            "<path d='M12 3v12m0 0 4-4m-4 4-4-4M5 21h14'/></svg>Explore catalogue</a></li>")
    nav = f"<ul class=sidebar-nav>{lis}</ul>"
    ident = current()
    name = ident["note"] if ident else "Guest"
    role = "Master admin" if is_admin() else ("Invited user" if ident else "Preview")
    return (f"<aside class=side>"
            f"<div class=sidebar-header><a class=brand href='/'><span class=mk>i</span>"
            f"<span class=nm>ingre<span>x</span>"
            f"<small>Nutraceutical sourcing</small></span></a></div>"
            f"{nav}"
            f"<div class=me><span class=av>{E(initials(name))}</span>"
            f"<span class=who><span class=nm>{E(name)}</span><br>"
            f"<span class=rl>{role}</span></span>"
            f"<a class=logout href='/logout' title='Log out' aria-label='Log out'>"
            f"<svg viewBox='0 0 24 24'><path d='M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3'/></svg>"
            f"</a></div></aside>")


def topbar(q=""):
    con = connect()
    try:
        items = notifications(con)
        s = con.execute("SELECT (SELECT COALESCE(MAX(id),0) FROM rating) r,"
                        " (SELECT COALESCE(MAX(month),'') FROM price_point) m").fetchone()
    finally:
        con.close()
    sig = f"{s['r']}-{s['m']}"   # changes when a new review or price month lands
    feed = "".join(
        f"<a class=nitem href='{it['href']}'>"
        f"<span class='nmark {it['cls']}'>{it['mark']}</span>"
        f"<span class=ntext><b>{E(it['title'])}</b><span class=nsub>{E(it['sub'])}</span></span></a>"
        for it in items) or "<div class=nempty>No activity yet.</div>"
    bell = (
        f"<details class=notif data-sig='{sig}'><summary title=Notifications>"
        f"<svg width=18 height=18 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2>"
        f"<path d='M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10 21h4'/></svg>"
        f"{f'<span class=dot>{len(items)}</span>' if items else ''}</summary>"
        f"<div class=menu><div class=mhead>Activity</div>{feed}"
        f"<a class=mfoot href='/search'>View all ingredients →</a></div></details>")
    return (f"<div class=top>"
            f"<form method=get action='/search'>"
            f"<svg viewBox='0 0 24 24'><circle cx=11 cy=11 r=7/><path d='M21 21l-4.3-4.3'/></svg>"
            f"<input name=q value='{E(q)}' placeholder='Search ingredients, suppliers, CAS no., etc.'></form>"
            f"<span class=grow></span>"
            f"<span class=live title='Users active in the last 5 minutes'>"
            f"<span class=pulse></span>{online_count()} online</span>"
            f"{bell}<span class=av>{E(initials(current()['note'] if current() else 'Guest'))}</span></div>")


# Badge = unread. Hidden once this device has "seen" the current activity
# signature; opening the bell marks it seen (per-device cookie, 1 year).
NOTIF_JS = """<script>
(function(){var n=document.querySelector('.notif');if(!n)return;
var sig=n.dataset.sig,m=document.cookie.match(/(?:^|; )seen=([^;]*)/),
seen=m?decodeURIComponent(m[1]):'',dot=n.querySelector('.dot');
if(seen===sig&&dot)dot.remove();
n.addEventListener('toggle',function(e){if(e.target.open){
document.cookie='seen='+encodeURIComponent(sig)+';path=/;max-age=31536000;samesite=lax';
var d=n.querySelector('.dot');if(d)d.remove();}});})();
</script>"""

def page(title, body, active="dashboard", q=""):
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Ingrex</title><style>{CSS}</style>"
            f"<div class=shell>{sidebar(active)}"
            f"<div class=content>{topbar(q)}<main class=wrap>{body}</main>"
            f"<footer>Ingrex · B2B nutraceutical ingredient portal. "
            f"Pilot preview — prices and ratings are sample data, not live quotes.</footer>"
            f"</div></div>{NOTIF_JS}</html>").encode()


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


MON_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def greeting():
    h = datetime.now().hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"


def icard(r, wl=frozenset(), back="/", moves=None):
    price = (f"₹{r['lo']:,.0f}–{r['hi']:,.0f}<span class=unit> /{E(r['unit'])}</span>"
             if r["lo"] else "<span class=mut>No offers</span>")
    rating = (f"<span class=st>★</span> {r['rating']:.1f}" if r["rating"]
              else "<span class=new>New</span>")
    today = date.today().isoformat()
    updated = ("Updated today" if r["updated"] == today
               else f"Updated {E(r['updated'])}" if r["updated"] else "—")
    pct = (moves or {}).get(r["id"])
    if pct is None:
        badge = "<span class='ibadge flat'>Compare</span>"
    elif pct <= 0:
        badge = f"<span class='ibadge down'>▼ Best price</span>"
    else:
        badge = f"<span class='ibadge up'>▲ {pct:.1f}%</span>"
    on = r["id"] in wl
    star = (f"<a class='star{' on' if on else ''}' "
            f"href='/watch?id={r['id']}&back={urllib.parse.quote(back)}' "
            f"title='{'Remove from' if on else 'Add to'} watchlist' "
            f"aria-label='toggle watchlist'>{'★' if on else '☆'}</a>")
    return (f"<div class=iwrap>{star}"
            f"<a class=icard href='/ingredient/{r['id']}'>"
            f"<div class=irate>{rating}</div>"
            f"<div class=icat>{E(r['category'])}</div>"
            f"<div class=inm>{E(r['name'])}</div>"
            f"<div class=iprice>{price}</div>"
            f"<div class=isup>{r['vendors']} Supplier{'' if r['vendors'] == 1 else 's'}</div>"
            f"<div class=foot>{badge}<span class=iupd>{updated}</span></div></a></div>")


def moves_map(con):
    """{ingredient_id: month-over-month % change} for the whole catalogue."""
    return {m["id"]: m["pct"] for m in market_movers(con, limit=999)}


def market_movers(con, limit=6):
    """Month-over-month market price change per ingredient, biggest first."""
    rows = con.execute("""
        SELECT p.ingredient_id id, i.name, i.unit, p.month, p.price
        FROM price_point p JOIN ingredient i ON i.id=p.ingredient_id
        ORDER BY p.ingredient_id, p.month""").fetchall()
    last = {}
    for r in rows:
        last.setdefault(r["id"], []).append(r)
    moves = []
    for pts in last.values():
        if len(pts) < 2:
            continue
        prev, cur = pts[-2]["price"], pts[-1]["price"]
        if not prev:
            continue
        moves.append({"id": pts[-1]["id"], "name": pts[-1]["name"], "unit": pts[-1]["unit"],
                      "price": cur, "pct": (cur - prev) / prev * 100})
    moves.sort(key=lambda m: abs(m["pct"]), reverse=True)
    return moves[:limit]


def notifications(con):
    """Activity feed for the bell: biggest price moves + newest reviews."""
    items = []
    for m in market_movers(con, 3):
        up = m["pct"] >= 0
        items.append({
            "cls": "up" if up else "down",
            "mark": "▲" if up else "▼",
            "title": f"{m['name']}",
            "sub": f"Market price {'up' if up else 'down'} {abs(m['pct']):.1f}% this month",
            "href": f"/ingredient/{m['id']}"})
    for r in con.execute("""
            SELECT r.score, r.rater, r.note, v.id vid, v.name vname
            FROM rating r JOIN vendor v ON v.id=r.vendor_id
            ORDER BY r.id DESC LIMIT 4""").fetchall():
        items.append({
            "cls": "star", "mark": "★",
            "title": f"{r['score']}★ · {r['vname']}",
            "sub": f"{r['rater']}: {(r['note'] or '')[:44]}",
            "href": f"/vendor/{r['vid']}"})
    return items


def price_chart(points, w=560, h=210):
    """Line chart with y gridlines + month labels for the market-trend panel."""
    vals = [p for _, p in points]
    if len(vals) < 2:
        return "<p class=empty>No price history.</p>"
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.18 or hi * 0.1 or 1
    lo, hi = lo - pad, hi + pad
    rng = hi - lo or 1
    pl, pr, pt, pb = 52, 12, 14, 26
    iw, ih, n = w - pl - pr, h - pt - pb, len(vals)
    xs = [pl + iw * i / (n - 1) for i in range(n)]
    ys = [pt + ih * (1 - (v - lo) / rng) for v in vals]
    grid = ""
    for g in range(4):
        gy = pt + ih * g / 3
        grid += (f"<line x1={pl} y1={gy:.1f} x2={w - pr} y2={gy:.1f} stroke='var(--line)'/>"
                 f"<text x={pl - 8} y={gy + 4:.1f} text-anchor=end class=axl>"
                 f"{hi - rng * g / 3:,.0f}</text>")
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{pl},{pt + ih} {line} {w - pr},{pt + ih}"
    dots = "".join(f"<circle cx={x:.1f} cy={y:.1f} r=3 fill='var(--acc)'/>"
                   for x, y in zip(xs, ys))
    step = max(1, n // 6)
    xl = "".join(
        f"<text x={xs[i]:.1f} y={h - 8} text-anchor=middle class=axl>"
        f"{MON_ABBR[int(m.split('-')[1])]}</text>"
        for i, (m, _) in enumerate(points) if i % step == 0 or i == n - 1)
    return (f"<svg viewBox='0 0 {w} {h}' role=img aria-label='price trend'>"
            f"<defs><linearGradient id=g x1=0 y1=0 x2=0 y2=1>"
            f"<stop offset=0 stop-color='var(--acc)' stop-opacity=.18/>"
            f"<stop offset=1 stop-color='var(--acc)' stop-opacity=0/></linearGradient></defs>"
            f"{grid}<polygon points='{area}' fill='url(#g)'/>"
            f"<polyline points='{line}' fill=none stroke='var(--acc)' stroke-width=2 "
            f"stroke-linejoin=round stroke-linecap=round/>{dots}{xl}</svg>")


# ---------- queries ----------

def search_ingredients(con, q="", kind="", doc="", maxp=None):
    sql = """
    SELECT i.*, COUNT(DISTINCT o.vendor_id) vendors,
           MIN(o.price_min) lo, MAX(o.price_max) hi, MAX(o.updated) updated,
           (SELECT AVG(score) FROM rating r JOIN offer o2 ON o2.vendor_id = r.vendor_id
            WHERE o2.ingredient_id = i.id) rating
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

def category_pills(con, active=""):
    cats = [r["category"] for r in con.execute(
        "SELECT category, COUNT(*) c FROM ingredient GROUP BY category ORDER BY c DESC")]
    pills = f"<a class='pill{"" if active else " on"}' href='/search'>Popular</a>"
    for c in cats:
        on = " on" if c == active else ""
        pills += f"<a class='pill{on}' href='/search?q={urllib.parse.quote(c)}'>{E(c)}</a>"
    return f"<div class=pills>{pills}</div>"


def stat_cards(con):
    ing = con.execute("SELECT COUNT(*) n FROM ingredient").fetchone()["n"]
    ven = con.execute(
        "SELECT COUNT(*) n, SUM(kind='Manufacturer') m FROM vendor").fetchone()
    rat = con.execute("SELECT AVG(score) a, COUNT(*) n FROM rating").fetchone()
    pts = con.execute("SELECT COUNT(*) n FROM price_point").fetchone()["n"]
    avg = f"{rat['a']:.1f} ★" if rat["a"] else "—"
    return f"""<div class=stats>
      <div class=stat><div class=l>Ingredients tracked</div><div class=v>{ing}</div>
        <div class=d>across the catalogue</div></div>
      <div class=stat><div class=l>Suppliers</div><div class=v>{ven['n']}</div>
        <div class=d><b>{ven['m']}</b> manufacturers · rest traders/importers</div></div>
      <div class=stat><div class=l>Avg supplier rating</div><div class=v>{avg}</div>
        <div class=d>from <b>{rat['n']}</b> reviews</div></div>
      <div class=stat><div class=l>Market data points</div><div class=v>{pts}</div>
        <div class=d>12-month price history</div></div></div>"""


def top_suppliers(con, limit=3):
    rows = con.execute("""
        SELECT v.id, v.name, v.city, v.country, v.kind,
               (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) a,
               (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n
        FROM vendor v WHERE (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) > 0
        ORDER BY a DESC LIMIT ?""", (limit,)).fetchall()
    out = ""
    for v in rows:
        ini = initials(v["name"])
        out += (f"<div class=sup><span class=av>{ini}</span>"
                f"<span><span class=nm>{E(v['name'])}</span><br>"
                f"<span class=lc>{E(v['city'])}, {E(v['country'])}</span></span>"
                f"<span class=rt><span class=s>★ {v['a']:.1f}</span><br>"
                f"<span class=n>{v['n']} review(s)</span></span>"
                f"<a class=btn href='/vendor/{v['id']}'>View</a></div>")
    return out or "<p class=empty>No rated suppliers yet.</p>"


def view_dashboard(con, wl=frozenset()):
    rows = search_ingredients(con)
    mv = moves_map(con)
    feat = max(rows, key=lambda r: r["vendors"] or 0)
    trend = con.execute(
        "SELECT month,price FROM price_point WHERE ingredient_id=? ORDER BY month",
        (feat["id"],)).fetchall()
    movers = ""
    for m in market_movers(con):
        up = m["pct"] >= 0
        movers += (f"<a class=mover href='/ingredient/{m['id']}'>"
                   f"<span><span class=nm>{E(m['name'])}</span>"
                   f"<div class=pr>₹{m['price']:,.0f}/{E(m['unit'])} · this month</div></span>"
                   f"<span class='pc {'up' if up else 'down'}'>{'▲' if up else '▼'} "
                   f"{abs(m['pct']):.1f}%</span></a>")
    ident = current()
    who = ident["note"].split()[0] if ident and ident["note"] else "there"
    body = f"""
      <div class=hi><h1>{greeting()}, {E(who)} 👋</h1>
        <div class=sub>Here's what's happening with your sourcing today.</div></div>
      {stat_cards(con)}
      <div class='panel pad'>
        <div class=ph><h3>Find ingredients. Compare. Source smart.</h3>
          <a href='/search'>View all →</a></div>
        {category_pills(con)}
        <div class=icards>{"".join(icard(r, wl, "/", mv) for r in rows[:10])}</div>
      </div>
      <div class=duo>
        <div class='panel pad'>
          <div class=ph><h3>Price trend</h3>
            <a href='/ingredient/{feat['id']}'>Details →</a></div>
          <div class=metaline>{E(feat['name'])} · monthly avg ₹/{E(feat['unit'])}</div>
          <div class=chartbox>{price_chart([(m['month'], m['price']) for m in trend])}</div>
        </div>
        <div class='panel pad'>
          <div class=ph><h3>Market movers</h3><span class=count>month over month</span></div>
          <div class=metaline style='margin-bottom:6px'>Biggest market price changes — ▲ up, ▼ down.</div>
          {movers or "<p class=empty>No price history yet.</p>"}
        </div>
      </div>
      <div class='panel pad'>
        <div class=ph><h3>Top rated suppliers</h3><a href='/vendors'>View all →</a></div>
        {top_suppliers(con, 5)}
      </div>"""
    return page("Dashboard", body, active="dashboard")


def view_search(con, params, wl=frozenset()):
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
    mv = moves_map(con)
    qs = urllib.parse.urlencode({k: v for k, v in
                                 [("q", q), ("kind", kind), ("doc", doc), ("maxp", raw)] if v})
    back = "/search" + (f"?{qs}" if qs else "")
    cats = {r["category"] for r in con.execute("SELECT DISTINCT category FROM ingredient")}
    opts = lambda vals, sel, label: (
        f"<option value=''>{label}</option>" +
        "".join(f"<option{' selected' if v == sel else ''}>{E(v)}</option>" for v in vals))

    body = f"""
      <div class=hi><h1>Search ingredients</h1>
        <div class=sub>Compare vendor price bands, documents, supplier type and market trend.</div></div>
      {category_pills(con, q if q in cats else "")}
      <div class='panel pad'><form class=filters method=get action='/search'>
        <input type=search name=q placeholder='Ingredient, CAS, function…' value='{E(q)}'>
        <select name=kind>{opts(VENDOR_KINDS, kind, 'Any vendor type')}</select>
        <select name=doc>{opts(DOC_TYPES, doc, 'Any document')}</select>
        <input name=maxp inputmode=decimal placeholder='Max ₹/unit' value='{E(raw)}' style='width:150px'>
        <button>Search</button>
      </form></div>
      <h2>{len(rows)} ingredient{'' if len(rows) == 1 else 's'}</h2>
      <div class=icards>{"".join(icard(r, wl, back, mv) for r in rows) or "<p class=empty>Nothing matched those filters.</p>"}</div>"""
    return page("Search", body, active="search", q=q)


def view_watchlist(con, wl=frozenset()):
    rows = [r for r in search_ingredients(con) if r["id"] in wl] if wl else []
    mv = moves_map(con)
    grid = "".join(icard(r, wl, "/watchlist", mv) for r in rows)
    inner = (f"<div class=icards>{grid}</div>" if grid else
             "<div class='panel pad'><p class=empty>Nothing here yet — tap the ☆ on any "
             "ingredient to track its price and vendors on this device.</p></div>")
    body = f"""
      <div class=hi><h1>Watchlist</h1>
        <div class=sub>Ingredients you're tracking on this device.</div></div>
      {inner}"""
    return page("Watchlist", body, active="watch")


def _ago(s):
    return "just now" if s < 15 else f"{s}s ago" if s < 60 else f"{s // 60}m ago"


def view_admin(con):
    who = online_list()
    online_rows = "".join(
        f"""<tr><td><b>{E(u['label'])}</b></td>
        <td class=metaline>{E(u['code'] or '—')}</td>
        <td class=metaline>{E(u['ip'])}</td>
        <td>{'<span class=tag>admin</span>' if u['admin'] else ''}</td>
        <td class=metaline>{_ago(u['ago'])}</td></tr>""" for u in who) \
        or "<tr><td colspan=5 class=empty>No one online right now.</td></tr>"

    invites = con.execute(
        "SELECT * FROM invite ORDER BY is_admin DESC, revoked, created DESC").fetchall()
    inv_rows = ""
    for iv in invites:
        status = ("<span class='tag kind Trader'>admin</span>" if iv["is_admin"]
                  else "<span class=tag style='color:#b4541c;border-color:#e6c3ad'>revoked</span>"
                  if iv["revoked"] else "<span class='tag func'>active</span>")
        action = ("" if iv["is_admin"] or iv["revoked"] else
                  f"<form method=post action='/admin/revoke' style='margin:0'>"
                  f"<input type=hidden name=code value='{E(iv['code'])}'>"
                  f"<button class=xbtn>Revoke</button></form>")
        link = "" if iv["revoked"] else f"<code class=inv>/login?code={E(iv['code'])}</code>"
        inv_rows += (f"<tr><td><code class=inv>{E(iv['code'])}</code>{link and '<br>' + link}</td>"
                     f"<td>{E(iv['note'] or '')}</td><td>{status}</td>"
                     f"<td class=metaline>{E(iv['created'] or '')}</td><td>{action}</td></tr>")

    body = f"""
      <div class=hi><h1>Admin</h1>
        <div class=sub>Manage invites and see who's using the pilot right now.</div></div>
      <div class='panel pad'>
        <div class=ph><h3>Online now</h3><span class=count>{len(who)} active · last 5 min</span></div>
        <div class=tablewrap style='margin-top:14px'><table>
          <thead><tr><th>User</th><th>Invite code</th><th>IP</th><th></th><th>Last active</th></tr></thead>
          <tbody>{online_rows}</tbody></table></div>
      </div>
      <div class='panel pad'>
        <div class=ph><h3>Invite codes</h3></div>
        <form method=post action='/admin/invite' class=filters style='margin:14px 0 4px'>
          <input name=note placeholder='Company / person this invite is for' maxlength=80 style='flex:1'>
          <button>Generate invite</button>
        </form>
        <div class=metaline style='margin-bottom:12px'>Share the generated
          <code class=inv>/login?code=…</code> link (prepend your domain) or just the code.</div>
        <div class=tablewrap><table>
          <thead><tr><th>Code / link</th><th>For</th><th>Status</th><th>Created</th><th></th></tr></thead>
          <tbody>{inv_rows}</tbody></table></div>
      </div>"""
    return page("Admin", body, active="admin")


def view_ingredient(con, ing_id, wl=frozenset()):
    ing = con.execute("SELECT * FROM ingredient WHERE id=?", (ing_id,)).fetchone()
    if not ing:
        return None
    watching = ing_id in wl
    wbtn = (f"<a class='wbtn{' on' if watching else ''}' "
            f"href='/watch?id={ing_id}&back=/ingredient/{ing_id}'>"
            f"{'★ Watching' if watching else '☆ Add to watchlist'}</a>")
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
        <td class=metaline>{E(o['updated'] or '—')}</td>
        <td><div class=chips>{doc_tags(o['docs'])}</div></td></tr>""" for o in offers)
    last_upd = max((o["updated"] for o in offers if o["updated"]), default=None)

    return page(ing["name"], f"""
      <a class=back href='/'>← Ingredients</a>
      <div class=titlerow><h1>{E(ing['name'])}</h1>{wbtn}</div>
      <p class=metaline>{E(ing['category'])} · CAS {E(ing['cas'])}</p>
      <div class=chips style='margin:10px 0 18px'>
        {"".join(f"<span class='tag func'>{E(f.strip())}</span>" for f in ing['functions'].split(','))}</div>
      <div class=card style='color:var(--body)'>{E(ing['description'])}</div>
      <h2>Market trend</h2>
      <div class=card>{sparkline([(m['month'], m['price']) for m in trend], 560, 90)}
        <div class=metaline style='margin-top:10px'>Monthly average landed price, ₹/{E(ing['unit'])}
        {('· ' + str(trend[0]['month']) + ' → ' + str(trend[-1]['month'])) if trend else ''}</div></div>
      <div class=ph style='margin:34px 0 12px'>
        <h2 style='margin:0'>{len(offers)} vendor{'' if len(offers) == 1 else 's'}</h2>
        {f"<span class=count>Prices last updated {E(last_upd)}</span>" if last_upd else ""}</div>
      <div class=tablewrap><table>
        <thead><tr><th>Vendor</th><th>Type</th><th>Price range</th><th>MOQ</th>
            <th>Lead</th><th>Rating</th><th>Updated</th><th>Documents</th></tr></thead>
        <tbody>{rows or "<tr><td colspan=8 class=empty>No vendors listed yet.</td></tr>"}</tbody>
      </table></div>""", active="search")


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
      <div class=grid>{cards}</div>""", active="suppliers")


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
      <h2>Reviews</h2>{revs}""", active="suppliers")


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


# ---------- presence ----------
# ponytail: in-memory, per-process. Resets on restart and is NOT shared across
# instances — fine for a single-box pilot. For multi-instance, move to Redis.
ONLINE = {}
ONLINE_LOCK = threading.Lock()
ONLINE_WINDOW = 300  # seconds since last request to still count as "online"


def touch_online(key, label="Guest", code="", ip="", admin=False):
    now = time.time()
    with ONLINE_LOCK:
        ONLINE[key] = {"t": now, "label": label, "code": code, "ip": ip, "admin": admin}
        for k, v in list(ONLINE.items()):
            if now - v["t"] > ONLINE_WINDOW:
                del ONLINE[k]


def online_count():
    now = time.time()
    with ONLINE_LOCK:
        return sum(1 for v in ONLINE.values() if now - v["t"] <= ONLINE_WINDOW)


def online_list():
    now = time.time()
    with ONLINE_LOCK:
        rows = [dict(v, ago=int(now - v["t"])) for v in ONLINE.values()
                if now - v["t"] <= ONLINE_WINDOW]
    return sorted(rows, key=lambda r: r["ago"])


# ---------- watchlist (per-device cookie) ----------
WATCH_COOKIE = "watch"


def watched_ids(headers):
    for part in headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == WATCH_COOKIE:
            return {int(x) for x in v.split(".") if x.isdigit()}
    return set()


def safe_back(path):
    """Only allow same-site relative redirects (no open redirect)."""
    return path if path.startswith("/") and not path.startswith("//") else "/"


# ---------- invite-only auth ----------
# Access is granted by an invite code (admin-issued). The auth cookie carries
# "code.signature"; the code is the real secret, the DB lookup enforces revocation.
CTX = threading.local()          # per-request identity, read by sidebar()/topbar()


def _cookie(headers, name):
    for part in headers.get("Cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def sign_code(code):
    return hmac.new(AUTH_SECRET, code.encode(), hashlib.sha256).hexdigest()


def auth_cookie(code):
    return (f"{COOKIE}={code}.{sign_code(code)}; Max-Age={COOKIE_MAXAGE}; "
            "Path=/; HttpOnly; SameSite=Lax; Secure")


def gate_active(con):
    return con.execute("SELECT 1 FROM invite WHERE revoked=0 LIMIT 1").fetchone() is not None


def identity(con, headers):
    """Return the invite row for a validly signed, non-revoked cookie, else None."""
    raw = _cookie(headers, COOKIE)
    code, _, sig = raw.rpartition(".")
    if not code or not hmac.compare_digest(sig, sign_code(code)):
        return None
    return con.execute("SELECT * FROM invite WHERE code=? AND revoked=0", (code,)).fetchone()


def current():
    return getattr(CTX, "ident", None)


def is_admin():
    ident = current()
    return bool(ident and ident["is_admin"])


LOGIN_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  min-height:100vh;display:grid;place-items:center;padding:24px;overflow:hidden;
  color:#f2fbf7;background:#04140f;position:relative}
/* full-bleed background video + contrast tint */
.vid{position:fixed;inset:0;width:100%;height:100%;object-fit:cover;z-index:-2}
.tint{position:fixed;inset:0;z-index:-1;
  background:radial-gradient(130% 130% at 25% 15%,rgba(4,20,15,.35),rgba(3,14,10,.72) 70%,rgba(2,10,7,.9)),
    linear-gradient(180deg,rgba(4,20,15,.2),rgba(4,20,15,.55))}

/* premium glass card — ~80% transparent fill */
.glass{position:relative;width:100%;max-width:410px;padding:46px 40px 40px;
  border-radius:26px;overflow:hidden;
  background:rgba(255,255,255,0);
  backdrop-filter:blur(30px) saturate(150%);-webkit-backdrop-filter:blur(30px) saturate(150%);
  border:1px solid rgba(255,255,255,.28);
  box-shadow:0 1px 0 rgba(255,255,255,.5) inset,0 -1px 0 rgba(255,255,255,.08) inset,
    0 40px 100px -24px rgba(0,0,0,.7),0 10px 30px -14px rgba(0,0,0,.55)}
/* fine light ring on the very edge for that premium bevel */
.glass::after{content:"";position:absolute;inset:0;border-radius:26px;pointer-events:none;
  padding:1px;background:linear-gradient(150deg,rgba(255,255,255,.55),transparent 40%,transparent 70%,rgba(255,255,255,.18));
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude}

.brand{font-size:36px;font-weight:800;letter-spacing:-.035em;color:#fff;
  text-shadow:0 2px 20px rgba(0,0,0,.3)}
.brand span{color:#5fe6ad}
.tag{display:inline-block;margin-top:12px;font-size:10.5px;font-weight:700;letter-spacing:.16em;
  text-transform:uppercase;color:#d6f7e8;background:rgba(255,255,255,.12);
  border:1px solid rgba(255,255,255,.22);padding:6px 13px;border-radius:20px}
.lead{margin:22px 0 26px;font-size:14px;line-height:1.6;color:rgba(255,255,255,.82)}
form{display:flex;flex-direction:column;gap:14px}
.field{position:relative}
.field svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);opacity:.7}
input{width:100%;padding:16px 16px 16px 46px;font-size:15px;color:#fff;
  background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.24);
  border-radius:14px;outline:0;transition:border-color .18s,box-shadow .18s,background .18s}
input::placeholder{color:rgba(255,255,255,.6)}
input:focus{border-color:rgba(95,230,173,.8);background:rgba(255,255,255,.16);
  box-shadow:0 0 0 4px rgba(95,230,173,.2)}
button{margin-top:2px;padding:16px;font-size:15px;font-weight:700;color:#04140f;cursor:pointer;
  border:0;border-radius:14px;letter-spacing:.01em;
  background:linear-gradient(135deg,#7af0c0,#12b884);
  box-shadow:0 10px 30px -8px rgba(18,184,132,.65),0 1px 0 rgba(255,255,255,.5) inset;
  transition:transform .08s,filter .18s,box-shadow .18s}
button:hover{filter:brightness(1.05);box-shadow:0 14px 38px -8px rgba(18,184,132,.75),0 1px 0 rgba(255,255,255,.5) inset}
button:active{transform:translateY(1px)}
.err{margin-bottom:16px;padding:12px 14px;font-size:13px;font-weight:600;color:#ffd7c2;
  background:rgba(214,90,40,.24);border:1px solid rgba(214,90,40,.45);border-radius:12px}
.foot{margin-top:24px;font-size:12px;color:rgba(255,255,255,.6);text-align:center}
@media(max-width:440px){.glass{padding:38px 26px}.brand{font-size:31px}}
"""


def login_page(err="", prefill=""):
    key = ("<svg width=18 height=18 viewBox='0 0 24 24' fill=none stroke='#bff3dd' "
           "stroke-width=2 stroke-linecap=round><circle cx=8 cy=15 r=4/>"
           "<path d='M10.8 12.2 21 2M17 6l2 2M14 9l2 2'/></svg>")
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Sign in · Ingrex</title><style>{LOGIN_CSS}</style>"
            f"<video class=vid autoplay muted loop playsinline preload=auto>"
            f"<source src='/bg.mp4' type='video/mp4'></video>"
            f"<div class=tint></div>"
            f"<div class=glass>"
            f"<div class=brand>ingre<span>x</span></div>"
            f"<div class=tag>Nutraceutical sourcing · Invite only</div>"
            f"<p class=lead>Enter the invite code from your Ingrex contact. "
            f"Don't have one? Ask your account manager for an invite.</p>"
            f"{f'<div class=err>{E(err)}</div>' if err else ''}"
            f"<form method=post action='/login'>"
            f"<div class=field>{key}"
            f"<input name=code value='{E(prefill)}' placeholder='Invite code' required "
            f"autofocus autocomplete=off spellcheck=false></div>"
            f"<button>Enter portal</button></form>"
            f"<div class=foot>Ingrex · B2B ingredient intelligence</div>"
            f"</div></html>").encode()


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

    def _serve_video(self):
        try:
            size = os.path.getsize(LOGIN_VIDEO)
            f = open(LOGIN_VIDEO, "rb")
        except OSError:
            return self._send(b"video not found", 404)
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        if rng.startswith("bytes="):          # Safari requires 206 range replies
            s, _, e = rng[6:].partition("-")
            start = int(s) if s.isdigit() else 0
            end = int(e) if e.isdigit() else size - 1
            end = min(end, size - 1)
            start = min(start, end)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)
        f.close()

    def _client_ip(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        if url.path == "/bg.mp4":
            return self._serve_video()
        con = connect()
        try:
            gated = gate_active(con)
            ident = identity(con, self.headers) if gated else None
            CTX.ident = ident
            if url.path == "/login":
                return self._send(login_page(prefill=params.get("code", [""])[0][:64]))
            if url.path == "/logout":
                return self._redirect("/login", f"{COOKIE}=; Max-Age=0; Path=/; "
                                      "HttpOnly; SameSite=Lax; Secure")
            if gated and not ident:
                code = params.get("code", [""])[0][:64]
                return self._redirect("/login" + (f"?code={urllib.parse.quote(code)}" if code else ""))
            touch_online(f"{ident['code'] if ident else 'anon'}|{self._client_ip()}",
                         ident["note"] if ident else "Guest", ident["code"] if ident else "",
                         self._client_ip(), is_admin())
            wl = watched_ids(self.headers)
            if url.path == "/watch":
                iid = int(params.get("id", ["0"])[0]) if params.get("id", ["0"])[0].isdigit() else 0
                if iid:
                    wl.symmetric_difference_update({iid})   # toggle membership
                val = ".".join(str(i) for i in sorted(wl))
                cookie = (f"{WATCH_COOKIE}={val}; Max-Age=15552000; "
                          "Path=/; HttpOnly; SameSite=Lax; Secure")
                return self._redirect(safe_back(params.get("back", ["/"])[0]), cookie)
            if url.path == "/":
                out = view_dashboard(con, wl)
            elif url.path == "/search":
                out = view_search(con, params, wl)
            elif url.path == "/watchlist":
                out = view_watchlist(con, wl)
            elif url.path == "/vendors":
                out = view_vendors(con)
            elif url.path == "/admin":
                out = view_admin(con) if (is_admin() or not gated) else None
            elif m := re.fullmatch(r"/ingredient/(\d+)", url.path):
                out = view_ingredient(con, int(m[1]), wl)
            elif m := re.fullmatch(r"/vendor/(\d+)", url.path):
                out = view_vendor(con, int(m[1]), params.get("msg", [""])[0][:80])
            else:
                out = None
            self._send(out or page("Not found", "<h1>404</h1><p><a href='/'>Home</a></p>"),
                       200 if out else 404)
        finally:
            con.close()
            CTX.ident = None

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        body = self.rfile.read(n).decode("utf-8", "replace")
        con = connect()
        try:
            gated = gate_active(con)
            ident = identity(con, self.headers) if gated else None
            CTX.ident = ident
            if path == "/login":
                code = urllib.parse.parse_qs(body).get("code", [""])[0].strip()[:64]
                row = con.execute("SELECT * FROM invite WHERE code=? AND revoked=0",
                                  (code,)).fetchone()
                if row:
                    return self._redirect("/admin" if row["is_admin"] else "/", auth_cookie(code))
                time.sleep(1)   # ponytail: crude brute-force damper on code guessing
                return self._send(login_page("Invalid or revoked invite code.", code), 401)
            if gated and not ident:
                return self._redirect("/login")
            admin = is_admin() or not gated
            if path == "/rate":
                vid, msg = post_rate(con, body)
                return self._redirect(
                    f"/vendor/{vid}?msg={urllib.parse.quote(msg)}" if vid else "/vendors")
            if path == "/admin/invite" and admin:
                f = urllib.parse.parse_qs(body)
                note = f.get("note", [""])[0].strip()[:80] or "Invitee"
                con.execute("INSERT INTO invite(code,note,created) VALUES(?,?,?)",
                            (secrets.token_urlsafe(6), note, date.today().isoformat()))
                con.commit()
                return self._redirect("/admin")
            if path == "/admin/revoke" and admin:
                code = urllib.parse.parse_qs(body).get("code", [""])[0]
                con.execute("UPDATE invite SET revoked=1 WHERE code=? AND is_admin=0", (code,))
                con.commit()
                return self._redirect("/admin")
            self._send(page("Not found", "<h1>404</h1>"), 404)
        finally:
            con.close()
            CTX.ident = None

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
    # presence: distinct clients counted, labels carried, stale ones drop out
    ONLINE.clear()
    touch_online("a|1.1.1.1", "Acme", "a", "1.1.1.1")
    touch_online("b|2.2.2.2", "Beta", "b", "2.2.2.2", admin=True)
    touch_online("a|1.1.1.1", "Acme", "a", "1.1.1.1")
    assert online_count() == 2, "distinct clients"
    assert {u["label"] for u in online_list()} == {"Acme", "Beta"}
    assert any(u["admin"] for u in online_list())
    ONLINE["c|3.3.3.3"] = {"t": time.time() - ONLINE_WINDOW - 1, "label": "x",
                           "code": "c", "ip": "3.3.3.3", "admin": False}
    assert online_count() == 2, "stale client excluded"
    ONLINE.clear()

    assert view_dashboard(con)
    assert view_search(con, {"q": ["x"], "maxp": ["abc"], "kind": ["../etc"]})
    assert b"Good " in view_dashboard(con) and b"stat" in view_dashboard(con)
    assert view_vendors(con)

    # market movers + notifications feed derive from real price history
    mv = market_movers(con)
    assert mv and all("pct" in m for m in mv)
    assert mv == sorted(mv, key=lambda m: abs(m["pct"]), reverse=True), "sorted by move size"
    assert notifications(con), "activity feed non-empty"

    # watchlist: cookie parse, toggle rendering, no open redirect
    assert watched_ids({"Cookie": "watch=1.3.5"}) == {1, 3, 5}
    assert watched_ids({"Cookie": "other=x"}) == set() and watched_ids({}) == set()
    assert safe_back("/search?q=x") == "/search?q=x"
    assert safe_back("//evil.com") == "/" and safe_back("http://x") == "/"
    assert b"class='star on'" in view_dashboard(con, {1}), "watched card shows filled star"
    assert b"class='star on'" not in view_dashboard(con, set())
    assert b"Watching" in view_ingredient(con, 1, {1})
    assert view_watchlist(con, {1, 2}) and view_watchlist(con, set())

    # invite auth: gate off with no invites; valid signed cookie admits, forgery rejected
    assert gate_active(con) is False, "no invites => open gate"
    con.execute("INSERT INTO invite(code,note,is_admin,created) VALUES('CODE1','Acme',0,'d')")
    con.execute("INSERT INTO invite(code,note,is_admin,created) VALUES('ADMINX','Boss',1,'d')")
    con.commit()
    assert gate_active(con) is True
    good = f"{COOKIE}=CODE1.{sign_code('CODE1')}"
    assert identity(con, {"Cookie": good})["note"] == "Acme"
    assert identity(con, {}) is None
    assert identity(con, {"Cookie": f"{COOKIE}=CODE1.deadbeef"}) is None, "bad signature"
    assert identity(con, {"Cookie": f"{COOKIE}=NOPE.{sign_code('NOPE')}"}) is None, "unknown code"
    admin_c = f"{COOKIE}=ADMINX.{sign_code('ADMINX')}"
    assert identity(con, {"Cookie": admin_c})["is_admin"] == 1
    # revocation locks out
    con.execute("UPDATE invite SET revoked=1 WHERE code='CODE1'")
    con.commit()
    assert identity(con, {"Cookie": good}) is None, "revoked code denied"
    # admin view + presence-aware rendering via CTX
    CTX.ident = identity(con, {"Cookie": admin_c})
    assert is_admin() and b"Online now" in view_admin(con) and b"Admin" in view_dashboard(con)
    assert b"Boss" in view_dashboard(con), "greeting uses the signed-in user's name"
    assert b"/logout" in view_dashboard(con), "logout button present"
    CTX.ident = None
    con.execute("DELETE FROM invite")
    con.commit()

    # card shows rating, price-move badge, supplier count, updated label — no image
    dash = view_dashboard(con)
    assert b"ibadge" in dash and b"Supplier" in dash and b"Updated" in dash
    assert b"iimg" not in dash, "illustration removed"
    assert search_ingredients(con)[0]["rating"] is not None

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
