#!/usr/bin/env python3
"""Ingrex - B2B nutraceutical ingredient portal.

Single file, stdlib only. Run:  python3 ingrex.py   ->  http://localhost:8000
Self-check:                     python3 ingrex.py --test
"""
import gzip
import hashlib
import hmac
import html
import http.server
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

# Invite-only gate. Access needs an invite code (see ensure_invites): set
# INGREX_ADMIN_CODE (master admin) and optionally INGREX_INVITES on the host.
# With no invite codes configured, the site is open (local dev, tests).
# INGREX_SECRET keeps auth cookies valid across restarts — set it in production.
AUTH_SECRET = (os.environ.get("INGREX_SECRET") or "ingrex-pilot-secret").encode()
COOKIE = "ing_auth"
COOKIE_MAXAGE = 60 * 60 * 24 * 30  # 30 days

# Google sign-in (optional). Set both in the host env to switch the button on;
# with either missing the login page simply doesn't offer Google.
# Authorised redirect URI to register with Google: https://<your-host>/auth/google/callback
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ON = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ingrex.db")

DOC_TYPES = ["COA", "MSDS", "Spec Sheet", "GMP", "FSSAI", "ISO 22000",
             "Halal", "Kosher", "Organic (NPOP/USDA)", "Allergen Statement",
             "Heavy Metals Report", "Stability Data"]
VENDOR_KINDS = ["Manufacturer", "Trader", "Importer"]
BUSINESS_ROLES = ["Contract Manufacturer", "Brand / Client", "Raw Material Manufacturer",
                  "Trader", "Importer", "Distributor"]
# Only buyers (those purchasing from suppliers) may review suppliers.
BUYER_ROLES = {"Contract Manufacturer", "Brand / Client", "Distributor"}
REQUEST_STATUS = ["Open", "In progress", "Sourcing vendor", "Fulfilled", "Closed"]

# Subscription. First month is free for every new account; after that a plan is
# needed. Billing itself is handled offline by the team — the portal only records
# which plan an account picked (no card data ever touches this app).
TRIAL_DAYS = 30
PLANS = [
    # key, name, ₹/month billed monthly, ₹/month billed yearly, blurb, features
    ("starter", "Starter", 99, 79, "For small brands checking prices before they buy.",
     ["Full ingredient catalogue", "Vendor price bands", "Watchlist on any ingredient",
      "3 sourcing requests / month", "Email support"]),
    ("growth", "Growth", 999, 799, "For contract manufacturers buying at scale.",
     ["Everything in Starter", "Unlimited sourcing requests", "12-month price history + trends",
      "CSV export of any search", "Supplier documents (COA, MSDS, GMP)",
      "Supplier reviews + ratings", "Priority support"]),
    ("enterprise", "Enterprise", 0, 0, "For multi-plant buyers and distributors.",
     ["Everything in Growth", "Dedicated sourcing manager", "Custom vendor onboarding",
      "Contract price benchmarking", "API access", "SLA-backed support"]),
]

# Inferred material make / origin, shown on the ingredient page.
MATERIAL_MAKE = {
    "Herbal Extract": "Plant-derived (botanical extract)",
    "Vitamin & Mineral": "Synthetic / mineral source",
    "Protein": "Plant, dairy or animal-derived",
    "Dairy": "Milk-derived",
    "Oil & Lipid": "Plant / marine-derived",
    "Amino Acid": "Fermentation / synthetic",
    "Probiotic & Enzyme": "Fermentation-derived",
    "Sweetener & Fibre": "Plant-derived / synthetic",
    "Flavour & Colour": "Nature-identical / synthetic",
    "Excipient": "Mineral / synthetic (pharma grade)",
    "Powder & Flour": "Plant-derived (milled)",
}

SCHEMA = """
CREATE TABLE ingredient (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT,
  cas TEXT, functions TEXT, description TEXT, unit TEXT DEFAULT 'kg');
CREATE TABLE vendor (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('Manufacturer','Trader','Importer')),
  city TEXT, country TEXT, gst TEXT, docs TEXT DEFAULT '',
  poc TEXT, phone TEXT, email TEXT, address TEXT, state TEXT, pincode TEXT,
  blacklisted INTEGER DEFAULT 0);
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

CATALOGUE_CSV = os.path.join(HERE, "suppliers.csv")

# keyword → category. First match wins; order matters (specific before generic).
CAT_RULES = [
    (("collagen", "whey", "wpc", "protein", "isolate", "casein", "peptide"), "Protein"),
    (("extract", "sitosterol", "carotene", "leutin", "lutein", "zeaxanthin", "astaxanthin"), "Herbal Extract"),
    (("vitamin", "d3", "premix", "mineral", "zinc", "calcium", "iron"), "Vitamin & Mineral"),
    (("flavour", "flavor", "vanilla", "chocolate", "colour", "color", "fcf"), "Flavour & Colour"),
    (("sugar", "jaggery", "maltodextrin", "inulin", "isomalt", "fos", "fiber", "fibre",
      "dextrose", "palatinose", "date", "stevia", "sweeten"), "Sweetener & Fibre"),
    (("milk", "smp", "lactose", "permeate", "dairy"), "Dairy"),
    (("oil", "mct", "triglyceride", "dha", "omega", "fat"), "Oil & Lipid"),
    (("glutamine", "methionine", "alanine", "citrul", "theanine", "arginine", "lysine",
      "taurine", "creatine"), "Amino Acid"),
    (("probiotic", "enzyme", "mos", "digest"), "Probiotic & Enzyme"),
    (("acid", "chloride", "citrate", "sulphate", "sulfate", "dioxide", "peg", "mcc",
      "guar", "silicon", "hpmc", "cap", "gum", "mdc"), "Excipient"),
    (("powder", "flour", "oats", "ragi", "jowar", "bajra"), "Powder & Flour"),
]


def infer_category(name):
    low = name.lower()
    for words, cat in CAT_RULES:
        if any(w in low for w in words):
            return cat
    return "Other Ingredient"


def infer_kind(vname):
    low = vname.lower()
    if any(w in low for w in ("import", "impex")):
        return "Importer"
    if any(w in low for w in ("global", "trading", "traders", "enterprise", "stockist",
                              "international", "additives", "chemicals zone")):
        return "Trader"
    return "Manufacturer"


def parse_rate(raw):
    s = re.sub(r"[^0-9.]", "", (raw or "").replace(",", ""))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def price_band(rate):
    """Show a range, not the exact quoted rate — ±12% around it, sensibly rounded."""
    lo, hi = rate * 0.88, rate * 1.14
    if rate < 10:
        return round(lo, 2), round(hi, 2)
    step = 1 if rate < 100 else 5 if rate < 1000 else 50
    return round(lo / step) * step, round(hi / step) * step


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


# ---------- storage: SQLite locally, Postgres (Supabase/Neon) when DATABASE_URL set ----------
# The app uses the tiny sqlite3-style API (execute/fetchone/fetchall/lastrowid/commit).
# When DATABASE_URL is present these wrappers translate the same calls to Postgres, so the
# data survives every deploy. No behaviour change locally — SQLite stays the default.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG = bool(DATABASE_URL)
_PG_ID_TABLES = ("vendor", "ingredient", "offer", "rating", "request", "req_note")


class _Row:
    """Dict- and tuple-indexable row, like sqlite3.Row."""
    __slots__ = ("_c", "_v")

    def __init__(self, cols, vals):
        self._c, self._v = cols, vals

    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else self._v[self._c.index(k)]

    def keys(self):
        return list(self._c)

    def __iter__(self):
        return iter(self._v)


def _pg_sql(sql):
    """Translate the SQLite-flavoured SQL the app writes into Postgres. Returns (sql, returns_id)."""
    s = sql.strip()
    ret = False
    low = s.lower()
    if low.startswith("create table"):
        return s.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY").replace("?", "%s"), False
    if low.startswith("insert or ignore into"):
        s = re.sub(r"^insert or ignore into", "INSERT INTO", s, flags=re.I) + " ON CONFLICT DO NOTHING"
    elif low.startswith("insert or replace into profile"):
        s = (re.sub(r"^insert or replace into", "INSERT INTO", s, flags=re.I) +
             " ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name,company=EXCLUDED.company,"
             "role=EXCLUDED.role,gst=EXCLUDED.gst,city=EXCLUDED.city,"
             "completed=EXCLUDED.completed,created=EXCLUDED.created,"
             "email=COALESCE(EXCLUDED.email,profile.email)")
    elif low.startswith("insert into"):
        m = re.match(r"insert into (\w+)", s, re.I)
        if m and m.group(1).lower() in _PG_ID_TABLES:
            s += " RETURNING id"
            ret = True
    return s.replace("?", "%s"), ret


class _PgCursor:
    def __init__(self, raw, sql, params):
        self._cur = raw.cursor()
        s, ret = _pg_sql(sql)
        self._cur.execute(s, tuple(params))
        self.lastrowid = None
        self._cols = [d[0] for d in self._cur.description] if self._cur.description else []
        if ret:
            row = self._cur.fetchone()
            self.lastrowid = row[0] if row else None
            self._cols = []

    def fetchone(self):
        r = self._cur.fetchone()
        return _Row(self._cols, r) if r else None

    def fetchall(self):
        return [_Row(self._cols, r) for r in self._cur.fetchall()]

    def __iter__(self):
        for r in self._cur.fetchall():
            yield _Row(self._cols, r)


class _PgConn:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return _PgCursor(self.raw, sql, params)

    def executemany(self, sql, seq):
        cur = self.raw.cursor()
        s, _ = _pg_sql(sql)
        for p in seq:
            cur.execute(s, tuple(p))
        cur.close()

    def executescript(self, script):
        cur = self.raw.cursor()
        cur.execute(script.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY"))
        cur.close()

    def commit(self):
        self.raw.commit()

    def close(self):
        """Return to the idle pool instead of dropping the connection. Opening a
        fresh psycopg2 connection to a hosted Postgres costs a TLS + auth round
        trip; doing that per page request is most of the app's latency."""
        try:
            self.raw.rollback()          # never hand over a half-open transaction
        except Exception:
            try:
                self.raw.close()
            except Exception:
                pass
            return
        with _PG_LOCK:
            if len(_PG_POOL) < PG_POOL_MAX:
                _PG_POOL.append(self)
                return
        self.raw.close()


PG_POOL_MAX = 8          # ponytail: fixed-size idle list, swap for pgbouncer if load grows
_PG_POOL = []
_PG_LOCK = threading.Lock()


def connect():
    if PG:
        with _PG_LOCK:
            while _PG_POOL:                      # reuse a live idle connection
                con = _PG_POOL.pop()
                if not con.raw.closed:
                    return con
        import psycopg2
        return _PgConn(psycopg2.connect(DATABASE_URL))
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
    if PG:
        con = connect()
        fresh = con.execute("SELECT to_regclass('public.ingredient')").fetchone()[0] is None
    else:
        fresh = not os.path.exists(DB) or os.path.getsize(DB) == 0   # check before connect creates it
        con = connect()
    if fresh:
        con.executescript(SCHEMA)
        seed_catalogue(con)
    ensure_invites(con)
    return con


def seed_catalogue(con, csv_path=None):
    """Load real supplier data from the committed CSV. Since the file lives in the
    repo, the catalogue re-seeds identically on every deploy — no disk needed."""
    import csv
    path = csv_path or CATALOGUE_CSV
    if not os.path.exists(path):
        return
    # tolerant read: source files carry a mangled ₹ byte, so replace bad bytes
    # then strip the resulting junk (U+FFFD / control chars) from names.
    clean = lambda s: re.sub(r"\s+", " ", re.sub(r"[�\x00-\x1f]+", " ", s or "")).strip()
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f))
    today = date.today().isoformat()
    vendors, ings = {}, {}
    for r in rows:
        item = clean(r.get("Item Name"))
        vname = clean(r.get("Vendor Name"))
        if not item or not vname:
            continue
        vkey = vname.lower()
        if vkey not in vendors:
            con.execute(
                "INSERT OR IGNORE INTO vendor(name,kind,city,country,gst,docs,"
                "poc,phone,email,address,state,pincode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (vname, infer_kind(vname), (r.get("Vendor State") or "").strip() or "India",
                 "India", (r.get("Vendor GST") or "").strip(), "",
                 (r.get("POC") or "").strip(), (r.get("POC Number") or "").strip(),
                 (r.get("POC Email") or "").strip(), (r.get("Vendor Address") or "").strip(),
                 (r.get("Vendor State") or "").strip(), (r.get("Vendor Pincode") or "").strip()))
            vendors[vkey] = con.execute(
                "SELECT id FROM vendor WHERE name=?", (vname,)).fetchone()["id"]
        ikey = item.lower()
        if ikey not in ings:
            cat = infer_category(item)
            con.execute(
                "INSERT INTO ingredient(name,category,cas,functions,description,unit) "
                "VALUES(?,?,?,?,?,?)",
                (item, cat, "—", cat, f"{item} — supplier-listed ingredient.", "kg"))
            ings[ikey] = con.execute(
                "SELECT id FROM ingredient WHERE name=?", (item,)).fetchone()["id"]
        rate = parse_rate(r.get("Rate Per Kg"))
        if rate:
            lo, hi = price_band(rate)
            con.execute(
                "INSERT OR IGNORE INTO offer(ingredient_id,vendor_id,price_min,price_max,"
                "unit,updated) VALUES(?,?,?,?,?,?)",
                (ings[ikey], vendors[vkey], lo, hi, "kg", today))
    # indicative 12-month price history per priced ingredient (modeled, deterministic)
    mo = months()
    for iid in ings.values():
        base = con.execute(
            "SELECT AVG((price_min+price_max)/2.0) a FROM offer WHERE ingredient_id=?",
            (iid,)).fetchone()["a"]
        if not base:
            continue
        rnd = random.Random(iid)
        v = base * rnd.uniform(0.9, 1.0)
        series = []
        for _ in range(12):
            v *= 1 + rnd.uniform(-0.03, 0.035)
            series.append(round(v))
        con.executemany("INSERT OR IGNORE INTO price_point(ingredient_id,month,price) VALUES(?,?,?)",
                        [(iid, mo[k], p) for k, p in enumerate(series)])
    con.commit()


def ensure_invites(con):
    """Invite table + env-seeded codes. Runs every start so INGREX_ADMIN_CODE /
    INGREX_INVITES survive the free-tier's ephemeral DB (which resets per deploy)."""
    con.execute("""CREATE TABLE IF NOT EXISTS invite(
        code TEXT PRIMARY KEY, note TEXT, is_admin INTEGER DEFAULT 0,
        revoked INTEGER DEFAULT 0, created TEXT, vendor_id INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS profile(
        code TEXT PRIMARY KEY, name TEXT, company TEXT, role TEXT,
        gst TEXT, city TEXT, completed INTEGER DEFAULT 0, created TEXT,
        phone TEXT, plan TEXT DEFAULT '', cycle TEXT DEFAULT '', email TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS request(
        id INTEGER PRIMARY KEY, code TEXT, requester TEXT, company TEXT,
        ingredient TEXT NOT NULL, details TEXT, status TEXT DEFAULT 'Open',
        reply TEXT, created TEXT, updated TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS req_note(
        id INTEGER PRIMARY KEY, request_id INTEGER, author TEXT, company TEXT,
        note TEXT, created TEXT)""")
    if not PG:   # SQLite: add columns to pre-existing local DBs (Postgres has them from CREATE)
        if "vendor_id" not in [r[1] for r in con.execute("PRAGMA table_info(invite)")]:
            con.execute("ALTER TABLE invite ADD COLUMN vendor_id INTEGER")
        if "blacklisted" not in [r[1] for r in con.execute("PRAGMA table_info(vendor)")]:
            con.execute("ALTER TABLE vendor ADD COLUMN blacklisted INTEGER DEFAULT 0")
        have = [r[1] for r in con.execute("PRAGMA table_info(profile)")]
        for col, decl in (("phone", "TEXT"), ("plan", "TEXT DEFAULT ''"),
                          ("cycle", "TEXT DEFAULT ''"), ("email", "TEXT")):
            if col not in have:
                con.execute(f"ALTER TABLE profile ADD COLUMN {col} {decl}")
    else:        # Postgres: table may predate these columns, CREATE IF NOT EXISTS won't add them
        for col, decl in (("phone", "TEXT"), ("plan", "TEXT DEFAULT ''"),
                          ("cycle", "TEXT DEFAULT ''"), ("email", "TEXT")):
            con.execute(f"ALTER TABLE profile ADD COLUMN IF NOT EXISTS {col} {decl}")
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
  --ink:#111512;--body:#3d4a44;--mut:#7d8a83;--line:#e2e8e4;--line2:#f0f3f1;
  /* the page sits a clear step below the cards — at #fbfbfa vs #fff they read flat */
  --bg:#f2f5f3;--card:#fff;--acc:#0d7a56;--acc-d:#0a5d41;--acc-t:#eaf3ee;
  --sb:#171c1a;--up:#bf5327;--down:#0d7a56;--gold:#c99a2e;
  --shadow:0 1px 2px rgba(17,21,18,.05),0 1px 1px rgba(17,21,18,.03);
  --radius:12px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",
  Roboto,Helvetica,Arial,sans-serif;
  font-size:13.5px;line-height:1.5;color:var(--body);background:var(--bg);letter-spacing:-.006em;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-variant-numeric:tabular-nums}
a{color:var(--acc);text-decoration:none}a:hover{color:var(--acc-d)}
h1{font-size:21px;line-height:1.16;letter-spacing:-.022em;margin:0 0 5px;color:var(--ink);font-weight:650}
h2{font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;
  color:var(--mut);margin:30px 0 11px}
p{margin:0 0 11px}

/* app shell — CoreUI-style dark sidebar */
.shell{display:flex;min-height:100vh}
.side{width:198px;flex:none;position:sticky;top:0;height:100vh;overflow:auto;
  background:var(--sb);color:rgba(255,255,255,.55);display:flex;flex-direction:column}
.sidebar-header{display:flex;align-items:center;padding:0 15px;height:52px;flex:none;
  border-bottom:1px solid rgba(255,255,255,.07)}
.side .brand{display:flex;align-items:center;gap:9px;color:#fff}
.side .brand .mk{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;
  font-weight:700;font-size:14px;color:#fff;background:linear-gradient(135deg,#12b884,#0a5d41)}
.side .brand .nm{font-size:16px;font-weight:700;letter-spacing:-.03em;line-height:1}
.side .brand .nm span{color:#4fe0a6}
.side .brand small{display:block;font-size:9px;font-weight:600;color:rgba(255,255,255,.4);
  letter-spacing:.02em;margin-top:2px}
.sidebar-nav{list-style:none;margin:0;padding:6px 8px;display:flex;flex-direction:column;
  flex:1;min-height:0}
.nav-title{padding:14px 8px 6px;font-size:10px;font-weight:650;text-transform:uppercase;
  letter-spacing:.07em;color:rgba(255,255,255,.32)}
.nav-item{position:relative}
.nav-link{display:flex;align-items:center;gap:10px;padding:7px 9px;font-size:13px;border-radius:7px;
  font-weight:500;color:rgba(255,255,255,.6);text-decoration:none;transition:background .14s,color .14s}
.nav-link:hover{color:#fff;background:rgba(255,255,255,.06)}
.nav-link.active{color:#fff;background:rgba(255,255,255,.1)}
.nav-icon{width:17px;height:17px;flex:none;stroke:rgba(255,255,255,.48);stroke-width:1.8;
  fill:none;stroke-linecap:round;stroke-linejoin:round}
.nav-link:hover .nav-icon{stroke:#fff}
.nav-link.active .nav-icon{stroke:#4fe0a6}
.nav-item.disabled .nav-link{color:rgba(255,255,255,.32);cursor:default}
.nav-item.disabled .nav-icon{stroke:rgba(255,255,255,.28)}
.nav-badge{margin-left:auto;font-size:8.5px;font-weight:700;letter-spacing:.04em;
  padding:1px 6px;border-radius:20px}
.nav-badge.new{background:var(--acc);color:#fff}
.nav-badge.soon{background:rgba(255,255,255,.09);color:rgba(255,255,255,.45)}
.mt-auto{margin-top:auto}
.side .me{display:flex;align-items:center;gap:9px;padding:11px 13px;
  border-top:1px solid rgba(255,255,255,.07)}
.av{width:30px;height:30px;border-radius:50%;flex:none;display:grid;place-items:center;
  font-weight:700;font-size:11px;color:#fff;background:linear-gradient(135deg,#3a4a54,#1c2632)}
.me .who{min-width:0;flex:1;text-decoration:none}
.me .who:hover .nm{text-decoration:underline}
a.av{text-decoration:none}
.me .nm{color:#fff;font-size:12.5px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.me .rl{color:rgba(255,255,255,.42);font-size:10.5px}
.logout{flex:none;width:29px;height:29px;border-radius:8px;display:grid;place-items:center;
  color:#fff;background:#d9463b}
.logout:hover{background:#c23a30}
.logout svg{width:17px;height:17px;stroke:currentColor;stroke-width:2.2;fill:none;
  stroke-linecap:round;stroke-linejoin:round}

.content{flex:1;min-width:0;display:flex;flex-direction:column}
.top{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:12px;height:52px;
  padding:0 22px;background:rgba(251,251,250,.8);backdrop-filter:saturate(180%) blur(14px);
  border-bottom:1px solid var(--line)}
.top form{flex:1;max-width:520px;position:relative}
.top .grow{flex:1}
.top form svg{position:absolute;left:13px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;stroke:var(--mut);stroke-width:2;fill:none}
.top input{width:100%;padding:8px 13px 8px 36px;border-radius:9px;font-size:13px;
  border:1px solid var(--line);background:#fff}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  color:var(--acc-d);background:var(--acc-t);border:1px solid #d7e8df;
  padding:5px 11px;border-radius:20px;white-space:nowrap}
.sgbox{position:absolute;top:calc(100% + 6px);left:0;right:0;background:#fff;
  border:1px solid var(--line);border-radius:11px;overflow:hidden;z-index:40;padding:4px;
  box-shadow:0 16px 40px -16px rgba(17,21,18,.3),0 2px 6px -3px rgba(17,21,18,.12);
  opacity:0;transform:translateY(-8px) scale(.985);transform-origin:top;pointer-events:none;
  transition:opacity .17s cubic-bezier(.2,.7,.2,1),transform .17s cubic-bezier(.2,.7,.2,1)}
.sgbox.on{opacity:1;transform:translateY(0) scale(1);pointer-events:auto}
.sgi{display:flex;align-items:center;gap:10px;padding:8px 11px;color:var(--ink);
  font-size:13px;border-radius:8px;text-decoration:none;transition:background .1s}
.sgi:hover,.sgi.on{background:var(--acc-t)}
.sgt{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  color:var(--acc-d);background:var(--acc-t);padding:2px 6px;border-radius:5px;flex:none}
.sgi.on .sgt,.sgi:hover .sgt{background:#fff}
.sgl{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.sgs{margin-left:auto;color:var(--mut);font-size:11.5px;flex:none;white-space:nowrap}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--acc);
  box-shadow:0 0 0 0 rgba(13,122,86,.5);animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(13,122,86,.5)}
  70%{box-shadow:0 0 0 7px rgba(13,122,86,0)}100%{box-shadow:0 0 0 0 rgba(13,122,86,0)}}
/* notifications dropdown (native details) */
.notif{position:relative}
.notif summary{list-style:none;cursor:pointer;width:34px;height:34px;border-radius:9px;
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
/* account menu on the avatar — same <details> pattern as the bell */
.acctmenu{position:relative}
.acctmenu summary{list-style:none;cursor:pointer}
.acctmenu summary::-webkit-details-marker{display:none}
.acctmenu[open] .av{box-shadow:0 0 0 3px var(--acc-t)}
.acctmenu .menu{position:absolute;top:48px;right:0;width:262px;background:#fff;
  border:1px solid var(--line);border-radius:14px;z-index:30;padding:6px;
  box-shadow:0 20px 50px -16px rgba(15,31,26,.3)}
.awho{padding:11px 12px 12px;border-bottom:1px solid var(--line);margin-bottom:5px}
.awho .anm{font-size:13.5px;font-weight:750;color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.awho .aem{font-size:12px;color:var(--mut);margin-top:1px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.aplan{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:10px;
  padding:7px 10px;border-radius:9px;background:var(--acc-t);border:1px solid #d7e8df}
.aplan .pl{font-size:11.5px;font-weight:750;color:var(--acc-d)}
.aplan .up{font-size:11px;font-weight:700;color:var(--acc-d);text-decoration:underline}
.aplan.warn{background:#fbe9df;border-color:#e6c3ad}
.aplan.warn .pl,.aplan.warn .up{color:#b4541c}
.aitem{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;
  font-size:13px;font-weight:600;color:var(--ink)}
.aitem:hover{background:var(--line2)}
.aitem svg{width:15px;height:15px;stroke:var(--mut);stroke-width:1.9;fill:none;
  stroke-linecap:round;stroke-linejoin:round;flex:none}
.aitem.danger{color:#c0392b;border-top:1px solid var(--line);margin-top:5px;border-radius:0 0 9px 9px}
.aitem.danger svg{stroke:#c0392b}

/* watchlist star on cards */
.iwrap{position:relative}
.star{position:absolute;top:10px;right:10px;z-index:2;width:25px;height:25px;border-radius:50%;
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
.wrap{padding:24px 26px 56px;max-width:1120px;width:100%}
.lead{color:var(--mut);font-size:13.5px;max-width:62ch;margin:-1px 0 20px}
.back{display:inline-block;font-size:12.5px;font-weight:600;color:var(--mut);margin-bottom:12px}
.back:hover{color:var(--acc)}
.titlerow{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
.titlerow h1{font-size:21px}
.wbtn{flex:none;font-size:12px;font-weight:650;padding:7px 12px;border-radius:8px;
  border:1px solid var(--line);background:#fff;color:var(--body);white-space:nowrap}
.wbtn:hover{border-color:#f0d9a0;color:var(--gold)}
.wbtn.on{background:#fffdf5;border-color:#f0d9a0;color:var(--gold)}

/* dashboard */
.hi h1{font-size:22px;margin:0 0 3px}
.hi .sub{color:var(--mut);margin-bottom:20px;font-size:13.5px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px;
  box-shadow:var(--shadow)}
.stat .l{color:var(--mut);font-size:12px;font-weight:600}
.stat .v{font-size:23px;font-weight:700;color:var(--ink);letter-spacing:-.025em;margin:6px 0 5px}
.stat .d{font-size:11.5px;color:var(--mut)}.stat .d b{color:var(--acc);font-weight:650}
/* actionable strip — one dense row, every cell is a link somewhere useful */
.actionbar{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;margin-bottom:16px;
  background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.act{display:flex;flex-direction:column;gap:2px;padding:11px 14px;background:var(--card);
  color:inherit;transition:background .14s}
.act:hover{background:var(--acc-t);color:inherit}
.act .al{font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  color:var(--mut)}
.act .av2{font-size:14.5px;font-weight:700;color:var(--ink);letter-spacing:-.015em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.act .ad{font-size:11.5px;color:var(--mut);font-weight:600}
.act .ad.down{color:var(--down)}.act .ad.up{color:var(--up)}
@media(max-width:900px){.actionbar{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.actionbar{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 18px 6px;
  box-shadow:var(--shadow);margin-bottom:14px}
.panel.pad{padding:18px}
.ph{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.ph h3{font-size:15px;font-weight:650;color:var(--ink);margin:0;letter-spacing:-.01em}
.ph a{font-size:12.5px;font-weight:600}
.pills{display:flex;gap:7px;flex-wrap:wrap;margin:14px 0 16px}
.pill{padding:6px 13px;border-radius:8px;border:1px solid var(--line);background:#fff;
  font-size:12.5px;font-weight:600;color:var(--body);cursor:pointer}
.pill:hover{border-color:#cfe0d7;color:var(--acc-d)}
.pill.on{background:var(--acc);color:#fff;border-color:transparent}
.icards{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:10px}
a.icard{display:block;border:1px solid var(--line);border-radius:12px;padding:10px 11px 11px;
  color:inherit;background:#fff;transition:box-shadow .16s,border-color .16s,transform .16s}
a.icard:hover{border-color:#cfe0d7;transform:translateY(-2px);
  box-shadow:0 14px 30px -14px rgba(15,31,26,.28)}
.icard{padding:11px 13px 12px;border-left:3px solid var(--cc,var(--line));
  background:linear-gradient(180deg,color-mix(in srgb,var(--cc) 4%,#fff),#fff 55%)}
.icard:hover{border-color:color-mix(in srgb,var(--cc) 40%,var(--line));
  border-left-color:var(--cc)}
.icard .irate{display:flex;align-items:center;gap:8px;font-size:11.5px;font-weight:700;color:var(--ink)}
.icard .irate .st{color:var(--gold);letter-spacing:.5px}
.icard .irate .new{color:var(--mut);font-weight:600}
.icard .icat{font-size:9.5px;font-weight:700;letter-spacing:.04em;margin-top:5px;
  color:var(--cc);text-transform:uppercase}
.icard .inm{font-size:13.5px;font-weight:700;color:var(--ink);line-height:1.28;margin:2px 0 1px;
  min-height:2.55em}
.icard .priceband{display:inline-block;font-size:13.5px;font-weight:800;color:var(--ink);
  margin-top:6px;padding:4px 9px;border-radius:8px;
  background:color-mix(in srgb,var(--cc) 12%,#fff);
  border:1px solid color-mix(in srgb,var(--cc) 24%,#fff)}
.icard .priceband .unit{font-size:12px;font-weight:500;color:var(--mut)}
.iwrap .star{top:9px;right:9px}
.itable td{vertical-align:middle}.itable tbody tr:hover{background:var(--acc-t)}
.itable .starcell{width:34px;text-align:center;padding-right:0}
/* supplier hits above the ingredient table on /search */
.vhits{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:10px;
  margin-bottom:14px}
.vhit{display:flex;align-items:center;gap:10px;padding:11px 13px;background:var(--card);
  border:1px solid var(--line);border-radius:11px;color:inherit;box-shadow:var(--shadow);
  transition:border-color .16s,box-shadow .16s}
.vhit:hover{border-color:#cfe0d7;color:inherit;box-shadow:0 4px 14px -6px rgba(15,31,26,.2)}
.vhit b{display:block;font-size:13.5px;color:var(--ink);line-height:1.25}
.vhit .metaline{font-size:11.5px}
.vhit .go{margin-left:auto;font-size:11.5px;font-weight:700;color:var(--acc-d);white-space:nowrap}
.dym{margin-bottom:14px;padding:11px 14px;border-radius:11px;font-size:13px;font-weight:600;
  background:var(--acc-t);border:1px solid #d7e8df;color:var(--acc-d)}
.dym a{font-weight:800;text-decoration:underline}
.itable tr.crow td.starcell{border-left:3px solid var(--cc,var(--line))}
.itable tr.crow:hover{background:color-mix(in srgb,var(--cc) 7%,#fff)}
.star2{font-size:16px;color:#c2ccc6;text-decoration:none}
.star2:hover{color:var(--gold)}.star2.on{color:var(--gold)}
.icard .iprice{font-size:16px;font-weight:800;color:var(--ink);margin-top:6px}
.icard .iprice .unit{font-size:12px;font-weight:500;color:var(--mut)}
.icard .isup{color:var(--mut);font-size:11.5px;font-weight:600;margin-top:2px}
.icard .foot{display:flex;align-items:center;justify-content:space-between;margin-top:8px;
  padding-top:10px;border-top:1px solid var(--line2)}
.icard .ibadge{font-size:10px;font-weight:800;padding:3px 8px;border-radius:20px}
.icard .ibadge.down{background:var(--acc-t);color:var(--acc-d)}
.icard .ibadge.up{background:#fbe9df;color:var(--up)}
.icard .ibadge.flat{background:var(--bg);color:var(--mut)}
.icard .iupd{font-size:10px;color:var(--mut);font-weight:600}
.xbtn{font-size:12px;font-weight:700;padding:6px 12px;border-radius:8px;background:#fff;
  color:var(--up);border:1px solid #e6c3ad;cursor:pointer}
.xbtn:hover{background:#fbe9df}
code.inv{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--line2);
  padding:2px 7px;border-radius:6px;color:var(--ink)}
.envbox{width:100%;font-family:ui-monospace,Menlo,monospace;font-size:12px;padding:9px 11px;
  border:1px solid var(--line);border-radius:8px;background:var(--line2);color:var(--ink);
  resize:vertical;margin-top:4px}
.blbanner{background:#fbe9e6;border:1px solid #e6b3ab;color:#a83a2c;font-size:13px;font-weight:600;
  padding:9px 13px;border-radius:10px;margin-bottom:12px}
.addsup summary{cursor:pointer;font-size:14px;font-weight:650;color:var(--acc-d);list-style:none}
.addsup summary::-webkit-details-marker{display:none}
.addsup[open] summary{margin-bottom:2px}
.vform{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.vform label{display:flex;flex-direction:column;gap:5px;font-size:11px;font-weight:650;color:var(--mut)}
.vform label.full{grid-column:1/-1}
.vform input,.vform select{font-size:13px;font-weight:400;color:var(--ink)}
@media(max-width:680px){.vform{grid-template-columns:1fr 1fr}}
/* ingredient detail two-column: suppliers left, trend + facts right */
.igrid{display:grid;grid-template-columns:1fr 300px;gap:20px;align-items:start}
.iside{position:sticky;top:66px}
.iside .tchart svg{width:100%}
.facts{display:flex;flex-direction:column;gap:0}
.fact{display:flex;align-items:center;justify-content:space-between;padding:8px 0;
  border-bottom:1px solid var(--line2);font-size:12.5px}
.fact:last-child{border-bottom:0}
.fact .fl{color:var(--mut);font-weight:600}
.fact .fv{color:var(--ink);font-weight:700}
.fact .funit{color:var(--mut);font-weight:500;font-size:11px}
@media(max-width:820px){.igrid{grid-template-columns:1fr}.iside{position:static;order:-1}}

/* marketplace-style supplier cards (ingredient detail) */
.vlist{display:flex;flex-direction:column;gap:10px}
.vcard{display:flex;gap:13px;align-items:center;background:#fff;border:1px solid var(--line);
  border-radius:12px;padding:12px 15px;box-shadow:var(--shadow);
  border-left:3px solid var(--cc,var(--acc));transition:box-shadow .16s,transform .16s}
.vcard:hover{box-shadow:0 10px 22px -14px rgba(15,31,26,.25);transform:translateY(-1px)}
.vmono{flex:none;width:42px;height:42px;border-radius:10px;display:grid;place-items:center;
  font-size:14px;font-weight:800;color:#fff;letter-spacing:.02em;
  background:linear-gradient(135deg,var(--cc),color-mix(in srgb,var(--cc) 60%,#0b0b0b))}
.vbody{flex:1;min-width:0}
.vtop{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.vname{font-size:15px;font-weight:700;letter-spacing:-.01em;color:var(--ink)}
.vname:hover{color:var(--acc-d)}
.vbadge-best{font-size:10px;font-weight:800;color:#1b47c4;background:#e7edff;
  padding:2px 8px;border-radius:6px;letter-spacing:.01em}
.vbadge-verified{display:inline-flex;align-items:center;gap:3px;font-size:11px;font-weight:700;
  color:#1f8a54;background:#e7f5ee;padding:2px 8px;border-radius:20px}
.vmeta{display:flex;align-items:center;gap:7px;margin:3px 0 7px;font-size:12px;color:var(--mut)}
.vr{font-weight:700;color:var(--ink)}.vr .st{color:var(--gold)}.vrn{color:var(--mut);font-weight:600}
.vr-new{color:var(--mut);font-weight:600;background:var(--bg);padding:1px 7px;border-radius:20px;font-size:11px}
.vdot{color:var(--line)}
.vchips{display:flex;flex-wrap:wrap;gap:6px}
.vchip{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:var(--body);
  background:var(--bg);border:1px solid var(--line);padding:3px 9px;border-radius:7px}
.vchip.vkind{color:#fff;background:var(--acc);border:0}
.vchip.vkind.Trader{background:#6a58c4}.vchip.vkind.Importer{background:#c47f1c}
.vright{flex:none;text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:1px;min-width:120px}
.vprice{font-size:18px;font-weight:800;letter-spacing:-.02em;color:var(--ink);line-height:1}
.vprice .vunit{font-size:12px;font-weight:600;color:var(--mut)}
.vpricesub{font-size:10px;color:var(--mut);margin-bottom:7px}
.vbook{display:inline-block;background:var(--acc);color:#fff;font-weight:700;font-size:13px;
  padding:7px 16px;border-radius:9px;box-shadow:0 5px 14px -8px rgba(13,122,86,.6)}
.vbook:hover{background:var(--acc-d);color:#fff}
@media(max-width:620px){
  .vcard{flex-wrap:wrap}.vmono{width:38px;height:38px;font-size:13px}
  .vright{min-width:0;width:100%;flex-direction:row;justify-content:space-between;align-items:center;
    margin-top:6px;padding-top:10px;border-top:1px solid var(--line)}
  .vpricesub{display:none}
}
.duo{display:grid;grid-template-columns:1.5fr 1fr;gap:18px;align-items:start}
.trendfind{display:flex;align-items:center;gap:8px;position:relative}
.trendfind svg{position:absolute;left:12px;width:15px;height:15px;stroke:var(--mut);
  stroke-width:2;fill:none;pointer-events:none}
.trendsel{flex:1;min-width:0;font:inherit;font-size:14px;font-weight:600;padding:9px 12px 9px 34px;
  border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink)}
.trendsel:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-t)}
.trendgo{flex:none;padding:9px 15px;font-size:13px;font-weight:700;border:0;border-radius:9px;
  background:var(--acc);color:#fff;cursor:pointer}
.trendgo:hover{background:var(--acc-d)}
.chartbox{margin-top:14px}
.chartbox svg{width:100%;height:auto;display:block}
.axl{fill:var(--mut);font-size:11px;font-family:system-ui,sans-serif}
.axl-x{fill:var(--body);font-size:11.5px;font-weight:600;font-family:system-ui,sans-serif}
.tchart{position:relative}
.tchart svg{width:100%;height:auto;display:block}
.thit{cursor:crosshair}.tcursor{pointer-events:none}
.ttip{position:absolute;transform:translate(-50%,-140%);background:var(--ink);color:#fff;
  font-size:12px;font-weight:600;padding:5px 9px;border-radius:7px;white-space:nowrap;
  pointer-events:none;opacity:0;transition:opacity .1s;z-index:5;
  box-shadow:0 6px 16px -8px rgba(0,0,0,.4)}
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
  padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
a.tile,.tile{display:block;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius);padding:15px;box-shadow:var(--shadow);color:inherit;
  transition:box-shadow .16s ease,border-color .16s ease,transform .16s ease}
a.tile:hover{border-color:#dfe4e1;box-shadow:0 1px 2px rgba(17,21,18,.04),0 10px 24px -14px rgba(17,21,18,.16);
  transform:translateY(-1px)}
.tile .ttl{font-size:14.5px;font-weight:650;color:var(--ink);letter-spacing:-.01em;line-height:1.3}
.tile:hover .ttl{color:var(--acc-d)}
.price{font-size:17px;font-weight:700;color:var(--ink);letter-spacing:-.015em}
.price .unit{font-size:12px;font-weight:500;color:var(--mut)}

/* tables */
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);background:var(--card)}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{background:var(--line2);font-size:10.5px;font-weight:650;text-transform:uppercase;
  letter-spacing:.06em;color:var(--mut);text-align:left;padding:9px 14px;
  border-bottom:1px solid var(--line);white-space:nowrap}
tbody td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--line2)}

/* tags + badges */
.tag{display:inline-block;font-size:10.5px;font-weight:600;padding:2px 8px;border-radius:6px;
  background:var(--bg);border:1px solid var(--line);color:var(--mut);margin:2px 3px 2px 0}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}
.kind{font-weight:650;color:#fff;background:var(--acc);border:0;letter-spacing:.01em}
.kind.Trader{background:#6a58c4}.kind.Importer{background:#c47f1c}
.func{background:var(--acc-t);border-color:transparent;color:var(--acc-d)}
.mut{color:var(--mut);font-size:12.5px}
.metaline{color:var(--mut);font-size:12.5px;margin:3px 0}

/* forms */
form.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select,textarea{font:inherit;font-size:13px;padding:8px 11px;border:1px solid var(--line);
  border-radius:8px;background:#fff;color:var(--ink);transition:border-color .12s,box-shadow .12s}
input::placeholder{color:#a8b3ad}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--acc);
  box-shadow:0 0 0 3px var(--acc-t)}
input[type=search]{flex:1;min-width:220px}
select{cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%238a968f' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center;padding-right:30px;appearance:none}
button{font:inherit;font-size:13px;font-weight:650;padding:8px 16px;border:0;border-radius:8px;
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
.sbadge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;letter-spacing:.02em}
.st-open{background:#fbeede;color:#a86a12}
.st-prog{background:#e7edff;color:#1b47c4}
.st-done{background:var(--acc-t);color:var(--acc-d)}
.st-closed{background:var(--line2);color:var(--mut)}
.rreply{margin-top:8px;padding:9px 12px;background:var(--acc-t);border-radius:9px;
  font-size:12.5px;color:var(--ink)}.rreply b{color:var(--acc-d)}
.leads{margin-top:8px;display:flex;flex-direction:column;gap:6px}
.lead{font-size:12.5px;color:var(--body);background:var(--line2);border-radius:8px;padding:7px 10px}
.lead b{color:var(--ink)}
/* community ticker */
.ticker{display:flex;align-items:center;gap:12px;background:var(--sb);color:#fff;
  border-radius:12px;padding:0 14px;height:40px;overflow:hidden;margin-bottom:16px}
.ticker-label{flex:none;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  color:#4fe0a6;background:rgba(79,224,166,.14);padding:4px 9px;border-radius:20px}
.ticker-track{flex:1;overflow:hidden;position:relative;height:100%}
.ticker-run{display:flex;gap:34px;align-items:center;height:100%;width:max-content;
  animation:tick 18s linear infinite}
.ticker:hover .ticker-run{animation-play-state:paused}
.ticker-run a{color:rgba(255,255,255,.85);font-size:13px;white-space:nowrap;flex:none}
.ticker-run a b{color:#fff}.ticker-run a span{color:rgba(255,255,255,.5)}
.ticker-cta{flex:none;font-size:12px;font-weight:700;color:#4fe0a6}
@keyframes tick{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media(prefers-reduced-motion:reduce){.ticker-run{animation:none}}
.empty{color:var(--mut);padding:26px 0;text-align:center}
.count{color:var(--mut);font-weight:600;font-size:13px}
.cline{display:flex;gap:14px;padding:8px 0;border-bottom:1px solid var(--line2);font-size:14px}
.cline:last-child{border-bottom:0}
.cline .cl{flex:none;width:110px;color:var(--mut);font-weight:600;font-size:13px}
.cline .cv{color:var(--ink)}

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
}

/* trial strip + plans + account */
.trial{display:flex;align-items:center;gap:11px;margin-bottom:16px;padding:9px 10px 9px 12px;
  border-radius:10px;font-size:12.5px;font-weight:600;color:var(--body);
  background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow)}
.trial .bar{flex:none;width:3px;align-self:stretch;border-radius:3px;background:var(--acc);
  margin:-1px 2px -1px 0}
.trial.warn .bar{background:var(--up)}
.trial b{font-weight:750;color:var(--ink)}
.trial .pin{font-size:9.5px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;
  padding:3px 8px;border-radius:5px;background:var(--acc-t);color:var(--acc-d);white-space:nowrap}
.trial.warn .pin{background:#fbe9df;color:#b4541c}
.trial a{margin-left:auto;font-weight:700;white-space:nowrap;padding:5px 11px;border-radius:7px;
  border:1px solid var(--line);color:var(--acc-d);transition:background .14s}
.trial a:hover{background:var(--acc-t)}
.trial.warn a{border-color:#e6c3ad;color:#b4541c}
.trial.warn a:hover{background:#fbe9df}
@media(max-width:620px){.trial{flex-wrap:wrap}.trial a{margin-left:0;width:100%;text-align:center}}
.plans{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:18px}
@media(max-width:900px){.plans{grid-template-columns:1fr}}
.plan{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius);padding:22px;box-shadow:var(--shadow);position:relative}
.plan.best{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-t),var(--shadow)}
.plan .tagbest{position:absolute;top:-11px;left:22px;font-size:10px;font-weight:800;
  text-transform:uppercase;letter-spacing:.06em;color:#fff;background:var(--acc);
  padding:4px 10px;border-radius:20px}
.plan h3{font-size:17px;margin-bottom:4px}
.plan .blurb{color:var(--mut);font-size:12.5px;min-height:34px}
.plan .amt{font-size:30px;font-weight:800;letter-spacing:-.03em;color:var(--ink);margin:12px 0 2px}
.plan .amt small{font-size:13px;font-weight:600;color:var(--mut);letter-spacing:0}
.plan .save{font-size:11.5px;font-weight:700;color:var(--acc-d);min-height:17px}
.plan ul{list-style:none;margin:16px 0 20px;display:flex;flex-direction:column;gap:9px}
.plan li{font-size:13px;display:flex;gap:8px;align-items:flex-start;color:var(--ink)}
.plan li::before{content:'✓';color:var(--acc);font-weight:800;flex:none}
.plan form{margin-top:auto}.plan button{width:100%}
.plan .ghost{width:100%;display:block;text-align:center;padding:11px;border-radius:10px;
  border:1px solid var(--line);font-weight:700;font-size:13px;color:var(--ink);background:#fff}
.plan .ghost:hover{background:var(--bg)}
.plan .onplan{width:100%;text-align:center;padding:11px;border-radius:10px;font-weight:700;
  font-size:13px;background:var(--acc-t);color:var(--acc-d);border:1px solid #d7e8df}
/* billing-cycle switch: pure CSS, radios drive both the pill and the prices */
.cyc{display:inline-flex;background:var(--line2);border:1px solid var(--line);
  border-radius:20px;padding:3px;gap:2px}
.cyc label{font-size:12.5px;font-weight:700;color:var(--mut);padding:6px 15px;
  border-radius:20px;cursor:pointer;user-select:none}
#cyc-m,#cyc-y{position:absolute;opacity:0;pointer-events:none}
#cyc-m:checked~.cyc label[for=cyc-m],#cyc-y:checked~.cyc label[for=cyc-y]{
  background:var(--card);color:var(--ink);box-shadow:var(--shadow)}
#cyc-m:checked~.plans .yr,#cyc-y:checked~.plans .mo{display:none}
.acct{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;align-items:start}
@media(max-width:900px){.acct{grid-template-columns:1fr}}
.acct .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:620px){.acct .grid2{grid-template-columns:1fr}}
.fl{display:block;font-size:11px;font-weight:700;color:var(--mut);margin-bottom:5px}
.acct input,.acct select{width:100%}
.kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
  border-bottom:1px solid var(--line);font-size:13px}
.kv:last-child{border-bottom:0}.kv .k{color:var(--mut);font-weight:600}
.kv .v{font-weight:700;color:var(--ink);text-align:right}
.ok{background:var(--acc-t);border:1px solid #d7e8df;color:var(--acc-d);font-size:13px;
  font-weight:700;padding:10px 13px;border-radius:10px;margin-bottom:14px}"""

CSS_HASH = hashlib.md5(CSS.encode()).hexdigest()[:8]  # cache-bust /app.css on edit

E = html.escape


# nav: label, href, active-key, svg-path (24x24), optional 'soon'
NAV = [
    ("Dashboard", "/", "dashboard", "M3 12h7V3H3zM14 21h7v-9h-7zM14 3v6h7V3zM3 21h7v-6H3z"),
    ("Search Ingredients", "/search", "search", "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3"),
    ("Suppliers", "/vendors", "suppliers", "M3 21V8l9-5 9 5v13M9 21v-6h6v6"),
    ("Market Insights", "/insights", "insights", "M4 19V5m0 14h16M8 15l3-4 3 2 4-6"),
    ("My Reviews", "/reviews", "reviews", "M12 3l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 18l-5.8 3 1.1-6.5L2.6 9.8l6.5-.9z"),
    ("Sourcing Requests", "/requests", "requests", "M9 12h6M9 16h6M9 8h6M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z"),
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


def sidebar(con, active):
    if is_supplier():
        # suppliers get a focused nav: manage their own listing + market view
        nav_items = [
            ("My listing", f"/vendor/{supplier_vid()}", "suppliers",
             "M3 21V8l9-5 9 5v13M9 21v-6h6v6"),
            ("Market Insights", "/insights", "insights", "M4 19V5m0 14h16M8 15l3-4 3 2 4-6"),
            ("Sourcing Requests", "/requests", "requests",
             "M9 12h6M9 16h6M9 8h6M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z"),
        ]
        active_map = {"suppliers": "suppliers", "insights": "insights", "requests": "requests"}
        active = active_map.get(active, "suppliers" if active in ("search", "dashboard") else active)
    else:
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
    # was a second link to the dashboard; downloading the catalogue is what the
    # icon already promised, and it's the thing buyers actually ask for
    if is_master(con):        # catalogue export is admin-only for now
        lis += ("<li class='nav-item mt-auto'><a class=nav-link href='/export.csv' "
                "title='Download the full catalogue as a spreadsheet'>"
                "<svg class=nav-icon viewBox='0 0 24 24' aria-hidden=true>"
                "<path d='M12 3v12m0 0 4-4m-4 4-4-4M5 21h14'/></svg>Export catalogue</a></li>")
    nav = f"<ul class=sidebar-nav>{lis}</ul>"
    ident = current()
    name = ident["note"] if ident else "Guest"
    role = "Master admin" if is_admin() else ("Invited user" if ident else "Preview")
    return (f"<aside class=side>"
            f"<div class=sidebar-header><a class=brand href='/'><span class=mk>i</span>"
            f"<span class=nm>ingre<span>x</span>"
            f"<small>Nutraceutical sourcing</small></span></a></div>"
            f"{nav}"
            f"<div class=me><a class=av href='/account' title='Account settings'>"
            f"{E(initials(name))}</a>"
            f"<a class=who href='/account' title='Account settings'>"
            f"<span class=nm>{E(name)}</span><br>"
            f"<span class=rl>{role}</span></a>"
            f"<a class=logout href='/logout' title='Log out' aria-label='Log out'>"
            f"<svg viewBox='0 0 24 24'><path d='M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3'/></svg>"
            f"</a></div></aside>")


def topbar(con, q=""):
    items = notifications(con)
    s = con.execute("SELECT (SELECT COALESCE(MAX(id),0) FROM rating) r,"
                    " (SELECT COALESCE(MAX(month),'') FROM price_point) m").fetchone()
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
            f"<form method=get action='/search' autocomplete=off>"
            f"<svg viewBox='0 0 24 24'><circle cx=11 cy=11 r=7/><path d='M21 21l-4.3-4.3'/></svg>"
            f"<input id=topsearch name=q value='{E(q)}' autocomplete=off spellcheck=false "
            f"placeholder='Search ingredients, suppliers, CAS no.…   (press / )'>"
            f"<div id=sgbox class=sgbox></div></form>"
            f"<span class=grow></span>"
            f"<span class=live title='Users active in the last 5 minutes'>"
            f"<span class=pulse></span>{online_count()} online</span>"
            f"{bell}{account_menu(con)}</div>")


def account_menu(con):
    """Avatar dropdown: who you are, what you're paying, and where to change it."""
    ident = current()
    name = ident["note"] if ident else "Guest"
    p = account(con)
    email = (p["email"] or "") if p and "email" in p.keys() else ""
    plan, cycle, left = subscription(con)
    if plan:
        pill, warn = f"{plan_name(plan)} · {cycle or 'monthly'}", ""
        cta = "Manage"
    elif left is None:
        pill, warn, cta = "Admin access", "", ""
    elif left > 0:
        pill = f"Free trial · {left} day{'' if left == 1 else 's'} left"
        warn = " warn" if left <= 7 else ""
        cta = "Upgrade"
    else:
        pill, warn, cta = "Trial ended", " warn", "Choose plan"
    ic = lambda d: f"<svg viewBox='0 0 24 24'><path d='{d}'/></svg>"
    items = [
        ("Account settings", "/account", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"
         "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0"
         "1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 1 1 0-4"
         "1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4a2 2 0 1 1 4 0"
         "1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 1 1 0 4"),
        ("Billing &amp; plans", "/plans", "M2 9h20M2 7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v10"
         "a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2zM6 15h4"),
        ("Account information", "/account#details",
         "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-5M12 8h.01"),
        ("Security", "/account#security",
         "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4"),
    ]
    links = "".join(f"<a class=aitem href='{href}'>{ic(d)}{label}</a>"
                    for label, href, d in items)
    return (f"<details class=acctmenu><summary title='Account'>"
            f"<span class=av>{E(initials(name))}</span></summary>"
            f"<div class=menu>"
            f"<div class=awho><div class=anm>{E(name)}</div>"
            f"{f'<div class=aem>{E(email)}</div>' if email else ''}"
            f"<div class='aplan{warn}'><span class=pl>{E(pill)}</span>"
            f"{f'<a class=up href=/plans>{cta}</a>' if cta else ''}</div></div>"
            f"{links}"
            f"<a class='aitem danger' href='/logout'>"
            f"{ic('M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3')}"
            f"Log out</a></div></details>")


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

# Interactive price chart: hover any month to read its price.
TREND_JS = """<script>
document.querySelectorAll('.tchart').forEach(function(ch){
var svg=ch.querySelector('svg'),tip=ch.querySelector('.ttip'),
guide=ch.querySelector('.tguide'),cur=ch.querySelector('.tcursor'),vb=svg.viewBox.baseVal;
function show(r){var x=+r.dataset.x,y=+r.dataset.y;
guide.setAttribute('x1',x);guide.setAttribute('x2',x);guide.setAttribute('opacity','.45');
cur.setAttribute('cx',x);cur.setAttribute('cy',y);cur.setAttribute('opacity','1');
var b=svg.getBoundingClientRect(),sx=b.width/vb.width,sy=b.height/vb.height;
tip.textContent=r.dataset.m+' · '+r.dataset.v;
tip.style.left=(x*sx)+'px';tip.style.top=(y*sy)+'px';tip.style.opacity='1';}
ch.querySelectorAll('.thit').forEach(function(r){
r.addEventListener('pointerenter',function(){show(r);});});
ch.addEventListener('pointerleave',function(){tip.style.opacity='0';
guide.setAttribute('opacity','0');cur.setAttribute('opacity','0');});});
</script>"""

# Top-bar live search suggestions with fluid dropdown + keyboard nav.
SEARCH_JS = """<script>
(function(){var inp=document.getElementById('topsearch'),box=document.getElementById('sgbox');
if(!inp||!box)return;var items=[],sel=-1,t;
function esc(s){return (s||'').replace(/[&<>\\"]/g,function(c){
return {'&':'&amp;','<':'&lt;','>':'&gt;','\\"':'&quot;'}[c];});}
function hide(){box.classList.remove('on');sel=-1;}
function render(){box.innerHTML=items.map(function(it,i){
return '<a class=\\"sgi'+(i===sel?' on':'')+'\\" href=\\"'+it.h+'\\">'+
'<span class=sgt>'+it.t+'</span><span class=sgl>'+esc(it.l)+'</span>'+
'<span class=sgs>'+esc(it.s)+'</span></a>';}).join('');
box.classList.toggle('on',items.length>0);}
inp.addEventListener('input',function(){clearTimeout(t);var v=inp.value.trim();
if(!v){items=[];hide();return;}
t=setTimeout(function(){fetch('/suggest?q='+encodeURIComponent(v))
.then(function(r){return r.json();}).then(function(d){items=d;sel=-1;render();})
.catch(function(){});},110);});
inp.addEventListener('keydown',function(e){if(!box.classList.contains('on'))return;
if(e.key==='ArrowDown'){e.preventDefault();sel=Math.min(sel+1,items.length-1);render();}
else if(e.key==='ArrowUp'){e.preventDefault();sel=Math.max(sel-1,0);render();}
else if(e.key==='Enter'&&sel>=0){e.preventDefault();location=items[sel].h;}
else if(e.key==='Escape'){hide();}});
document.addEventListener('click',function(e){if(!inp.parentNode.contains(e.target))hide();});
document.addEventListener('keydown',function(e){
if(e.key!=='/'||e.metaKey||e.ctrlKey)return;var a=document.activeElement;
if(a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))return;e.preventDefault();inp.focus();inp.select();});})();
</script>"""

def trial_strip(con):
    """Free-trial / plan banner. Nothing to show for admin, suppliers or paid accounts."""
    plan, cycle, left = subscription(con)
    if plan:
        return ""
    if left is None:                     # admin, supplier or open dev mode
        return ""
    if left > 0:
        warn = " warn" if left <= 7 else ""
        return (f"<div class='trial{warn}'><i class=bar></i><span class=pin>Free trial</span>"
                f"<span><b>{left} day{'' if left == 1 else 's'} left</b> — full access, "
                f"no card needed.</span>"
                f"<a href='/plans'>See plans</a></div>")
    return ("<div class='trial warn'><i class=bar></i><span class=pin>Trial ended</span>"
            "<span>Pick a plan to keep sourcing without limits.</span>"
            "<a href='/plans'>Choose a plan</a></div>")


def page(con, title, body, active="dashboard", q=""):
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Ingrex</title>"
            f"<link rel=stylesheet href='/app.css?v={CSS_HASH}'>"
            f"<div class=shell>{sidebar(con, active)}"
            f"<div class=content>{topbar(con, q)}"
            f"<main class=wrap>{trial_strip(con)}{body}</main>"
            f"<footer>Ingrex · B2B nutraceutical ingredient portal. "
            f"Pilot preview — prices and ratings are sample data, not live quotes.</footer>"
            f"</div></div>{NOTIF_JS}{TREND_JS}{SEARCH_JS}</html>").encode()


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

CAT_TINT = {
    "Protein": "#2f7cc4", "Herbal Extract": "#3f8a1c", "Vitamin & Mineral": "#c47f1c",
    "Flavour & Colour": "#b5486e", "Sweetener & Fibre": "#0e8a8a", "Dairy": "#3a76c4",
    "Oil & Lipid": "#c04a6e", "Amino Acid": "#6a58c4", "Probiotic & Enzyme": "#0d7a56",
    "Excipient": "#5d7168", "Powder & Flour": "#9a7418", "Other Ingredient": "#4b6a5e",
}


def cat_color(cat):
    return CAT_TINT.get(cat, "#4b6a5e")


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
            f"<a class=icard href='/ingredient/{r['id']}' style='--cc:{cat_color(r['category'])}'>"
            f"<div class=irate>{rating}</div>"
            f"<div class=icat>{E(r['category'])}</div>"
            f"<div class=inm>{E(r['name'])}</div>"
            f"<div class=priceband>{price}</div>"
            f"<div class=isup>{r['vendors']} Supplier{'' if r['vendors'] == 1 else 's'}</div>"
            f"<div class=foot>{badge}<span class=iupd>{updated}</span></div></a></div>")


def moves_map(con):
    """{ingredient_id: month-over-month % change} for the whole catalogue."""
    return {m["id"]: m["pct"] for m in market_movers(con, limit=999)}


_MOVES = (0.0, None)     # (computed_at, moves) — see market_movers


def market_movers(con, limit=6):
    """Month-over-month market price change per ingredient, biggest first.

    Every page hits this 2-3 times (KPI cards, movers panel, the bell feed) and it
    scans the whole price history each time — one network round trip per call once
    the DB is remote. Prices move monthly, so a short cache costs nothing real.
    ponytail: 60s process-local TTL; swap for invalidate-on-write if prices ever go live."""
    global _MOVES
    at, cached = _MOVES
    if cached is not None and time.time() - at < 60:
        return cached[:limit]
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
    _MOVES = (time.time(), moves)
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


def _smooth_path(xs, ys, top, bot):
    """Catmull-Rom through every point, emitted as cubic beziers.

    Control points are clamped to the plot box so a sharp spike can't bow the
    curve outside the chart area."""
    if len(xs) < 3:
        return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    clamp = lambda v: max(top, min(bot, v))
    d = [f"M {xs[0]:.1f},{ys[0]:.1f}"]
    n = len(xs)
    for i in range(n - 1):
        x0, y0 = (xs[i - 1], ys[i - 1]) if i else (xs[0], ys[0])
        x1, y1 = xs[i], ys[i]
        x2, y2 = xs[i + 1], ys[i + 1]
        x3, y3 = (xs[i + 2], ys[i + 2]) if i + 2 < n else (xs[-1], ys[-1])
        c1x, c1y = x1 + (x2 - x0) / 6.0, clamp(y1 + (y2 - y0) / 6.0)
        c2x, c2y = x2 - (x3 - x1) / 6.0, clamp(y2 - (y3 - y1) / 6.0)
        d.append(f"C {c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {x2:.1f},{y2:.1f}")
    return " ".join(d)


def price_chart(points, w=620, h=220):
    """Smoothed price line with a marker on every month. Hover reads any point."""
    vals = [p for _, p in points]
    if len(vals) < 2:
        return "<p class=empty>No price history.</p>"
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.18 or hi * 0.1 or 1
    lo, hi = lo - pad, hi + pad
    rng = hi - lo or 1
    pl, pr, pt, pb = 56, 16, 18, 30
    iw, ih, n = w - pl - pr, h - pt - pb, len(vals)
    xs = [pl + iw * i / (n - 1) for i in range(n)]
    ys = [pt + ih * (1 - (v - lo) / rng) for v in vals]
    # a narrow band (say ₹43-45) renders "₹44, ₹44" at zero decimals — pick enough
    # precision that the four gridline labels are always distinct
    dec = 0 if rng / 3 >= 2 else (1 if rng / 3 >= 0.2 else 2)
    grid = ""
    for g in range(4):
        gy = pt + ih * g / 3
        grid += (f"<line x1={pl} y1={gy:.1f} x2={w - pr} y2={gy:.1f} stroke='var(--line)'/>"
                 f"<text x={pl - 10} y={gy + 4:.1f} text-anchor=end class=axl>"
                 f"₹{hi - rng * g / 3:,.{dec}f}</text>")
    curve = _smooth_path(xs, ys, pt, pt + ih)
    area = f"{curve} L {xs[-1]:.1f},{pt + ih:.1f} L {xs[0]:.1f},{pt + ih:.1f} Z"
    # a marker on every month, emphasised where the price actually moved
    dots, hits = "", ""
    for i, ((m, v), x, y) in enumerate(zip(points, xs, ys)):
        yr, mm = m.split("-")
        label = f"{MON_ABBR[int(mm)]} {yr}"
        moved = i > 0 and abs(v - vals[i - 1]) > 1e-9
        r, op = (3.6, 1) if (moved or i in (0, n - 1)) else (2.4, .8)
        dots += (f"<circle class=tdot cx={x:.1f} cy={y:.1f} r={r} fill='#fff' "
                 f"stroke='var(--acc)' stroke-width=2 opacity={op}/>")
        hits += (f"<rect class=thit x={x - iw / (n - 1) / 2:.1f} y={pt} "
                 f"width={iw / (n - 1):.1f} height={ih} fill=transparent "
                 f"data-x='{x:.1f}' data-y='{y:.1f}' data-m='{E(label)}' data-v='₹{v:,.0f}'/>")
    step = max(1, n // 5)
    xl = f"<line x1={pl} y1={pt + ih} x2={w - pr} y2={pt + ih} stroke='var(--line)'/>"
    for i, (m, _) in enumerate(points):
        if not (i % step == 0 or i == n - 1):
            continue
        yr, mm = m.split("-")
        lab = MON_ABBR[int(mm)] + (f" '{yr[2:]}" if i in (0, n - 1) else "")
        xl += f"<text x={xs[i]:.1f} y={h - 8} text-anchor=middle class=axl-x>{lab}</text>"
    gid = "pcg" + re.sub(r"\W", "", f"{points[0][0]}{n}{int(vals[0])}")
    return (f"<div class=tchart>"
            f"<svg viewBox='0 0 {w} {h}' role=img aria-label='price trend'>"
            f"<defs><linearGradient id='{gid}' x1=0 y1=0 x2=0 y2=1>"
            f"<stop offset='0' stop-color='var(--acc)' stop-opacity='0.20'/>"
            f"<stop offset='1' stop-color='var(--acc)' stop-opacity='0'/>"
            f"</linearGradient></defs>"
            f"{grid}<path d='{area}' fill='url(#{gid})' stroke='none'/>"
            f"<path d='{curve}' fill='none' stroke='var(--acc)' stroke-width='2.2' "
            f"stroke-linejoin='round' stroke-linecap='round'/>"
            f"<line class=tguide x1=0 y1={pt} x2=0 y2={pt + ih} stroke='var(--acc)' "
            f"stroke-dasharray='3 3' opacity=0/><circle class=tcursor r=4.5 fill='var(--acc)' "
            f"stroke='#fff' stroke-width=2 opacity=0/>"
            f"{dots}{xl}{hits}</svg><div class=ttip></div></div>")


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
        # every word must appear somewhere, so word order and extra words still match;
        # supplier name is searchable too — "see pold" should find what they sell
        for tok in [t for t in q.split() if t]:
            sql += (" AND (i.name LIKE ? OR i.category LIKE ? OR i.functions LIKE ?"
                    " OR i.cas LIKE ? OR v.name LIKE ?)")
            args += [f"%{tok}%"] * 5
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


def suggest(con, q):
    """Top-bar autocomplete over ingredients + suppliers.

    Matches every word separately (so word order doesn't matter), then falls back
    to fuzzy matching — real buyers type "Sea Pold" for "See Pold Chemicals" and
    a plain LIKE returns nothing at all for that."""
    q = (q or "").strip()
    if not q:
        return []
    toks = q.split()
    where = " AND ".join(["name LIKE ?"] * len(toks))
    args = [f"%{t}%" for t in toks]
    out = []
    for r in con.execute(f"SELECT id,name,category FROM ingredient WHERE {where} "
                         "ORDER BY (name LIKE ?) DESC, name LIMIT 6", args + [f"{q}%"]):
        out.append({"t": "Ingredient", "l": r["name"], "s": r["category"],
                    "h": f"/ingredient/{r['id']}"})
    for r in con.execute(f"SELECT id,name,state FROM vendor WHERE {where} "
                         "ORDER BY (name LIKE ?) DESC, name LIMIT 4", args + [f"{q}%"]):
        out.append({"t": "Supplier", "l": r["name"], "s": r["state"] or "",
                    "h": f"/vendor/{r['id']}"})
    if out or len(q) < 3:
        return out
    return _fuzzy_suggest(con, q)


def _fuzzy_suggest(con, q):
    """Nothing matched literally — find the nearest catalogue and supplier names."""
    import difflib
    rows = ([("Ingredient", r["id"], r["name"], r["category"], "/ingredient/")
             for r in con.execute("SELECT id,name,category FROM ingredient")] +
            [("Supplier", r["id"], r["name"], r["state"] or "", "/vendor/")
             for r in con.execute("SELECT id,name,state FROM vendor")])
    ql = q.lower()
    scored = []
    for kind, rid, name, sub, href in rows:
        nl = name.lower()
        # best of: whole-string similarity, or the closest single word in the name
        score = difflib.SequenceMatcher(None, ql, nl).ratio()
        for word in nl.split():
            score = max(score, difflib.SequenceMatcher(None, ql, word).ratio())
            for qt in ql.split():
                score = max(score, difflib.SequenceMatcher(None, qt, word).ratio() * 0.9)
        if score >= 0.62:
            scored.append((score, kind, rid, name, sub, href))
    scored.sort(reverse=True)
    return [{"t": k, "l": n, "s": s, "h": f"{h}{i}"}
            for _, k, i, n, s, h in scored[:6]]


def vendor_rating(con, vendor_id):
    r = con.execute("SELECT AVG(score) a, COUNT(*) n FROM rating WHERE vendor_id=?",
                    (vendor_id,)).fetchone()
    return r["a"], r["n"]


def offers_for_ingredient(con, ing_id):
    return con.execute("""
        SELECT o.*, v.id vid, v.name vname, v.kind, v.city, v.state, v.docs,
               v.gst, v.email, v.poc,
               (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) avg_score,
               (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n_score
        FROM offer o JOIN vendor v ON v.id=o.vendor_id
        WHERE o.ingredient_id=? AND COALESCE(v.blacklisted,0)=0
        ORDER BY o.price_min""", (ing_id,)).fetchall()


# ---------- views ----------

def category_pills(con, active=""):
    cats = [r["category"] for r in con.execute(
        "SELECT category, COUNT(*) c FROM ingredient GROUP BY category ORDER BY c DESC")]
    pills = f"<a class='pill{"" if active else " on"}' href='/search'>Popular</a>"
    for c in cats:
        on = " on" if c == active else ""
        pills += f"<a class='pill{on}' href='/search?q={urllib.parse.quote(c)}'>{E(c)}</a>"
    return f"<div class=pills>{pills}</div>"


def action_bar(con, rows, mv, wl=frozenset()):
    """What a buyer can act on right now. Replaces the old vanity KPI row —
    catalogue totals told nobody anything they could do something about."""
    by_id = {r["id"]: r for r in rows}
    # cheapest win: the watched item that fell hardest, else the market's biggest faller
    fallers = sorted(((i, p) for i, p in mv.items() if p < 0), key=lambda t: t[1])
    watched_moves = [(i, p) for i, p in fallers if i in wl]
    pick = (watched_moves or fallers or [None])[0]
    open_reqs = con.execute(
        "SELECT COUNT(*) n FROM request WHERE status!='Closed'").fetchone()["n"]
    easing = sum(1 for p in mv.values() if p < 0)

    items = []
    if pick and pick[0] in by_id:
        r = by_id[pick[0]]
        items.append((f"/ingredient/{r['id']}", "Best move today",
                      f"{E(r['name'])[:34]}", f"▼ {abs(pick[1]):.1f}%", "down"))
    items.append(("/search", "Prices easing", f"{easing} ingredient{'' if easing == 1 else 's'}",
                  "this month", ""))
    items.append(("/watchlist", "Your watchlist",
                  f"{len(wl)} tracked" if wl else "Nothing tracked yet",
                  "Manage" if wl else "Add one", ""))
    items.append(("/requests", "Sourcing requests",
                  f"{open_reqs} open", "Community board", ""))
    return "<div class=actionbar>" + "".join(
        f"<a class=act href='{href}'><span class=al>{lab}</span>"
        f"<span class=av2>{val}</span>"
        f"<span class='ad {cls}'>{sub}</span></a>"
        for href, lab, val, sub, cls in items) + "</div>"


def top_suppliers(con, limit=3):
    # ranked by catalogue breadth (real data has no reviews yet), rating shown if any
    rows = con.execute("""
        SELECT v.id, v.name, v.city, v.country, v.kind,
               (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) a,
               (SELECT COUNT(DISTINCT ingredient_id) FROM offer WHERE vendor_id=v.id) items
        FROM vendor v ORDER BY items DESC, v.name LIMIT ?""", (limit,)).fetchall()
    out = ""
    for v in rows:
        rate = f"★ {v['a']:.1f}" if v["a"] else "New"
        out += (f"<div class=sup><span class=av>{initials(v['name'])}</span>"
                f"<span><span class=nm>{E(v['name'])}</span><br>"
                f"<span class=lc>{E(v['city'])} · {E(v['kind'])}</span></span>"
                f"<span class=rt><span class=s>{rate}</span><br>"
                f"<span class=n>{v['items']} item(s)</span></span>"
                f"<a class=btn href='/vendor/{v['id']}'>View</a></div>")
    return out or "<p class=empty>No suppliers yet.</p>"


def view_dashboard(con, wl=frozenset(), trend_sel=""):
    rows = search_ingredients(con)
    mv = moves_map(con)
    # accepts an id (from the datalist) or a typed name, exact then partial
    trend_sel = (trend_sel or "").strip()
    tid = int(trend_sel) if trend_sel.isdigit() else None
    feat = next((r for r in rows if r["id"] == tid), None)
    if feat is None and trend_sel:
        low = trend_sel.lower()
        feat = (next((r for r in rows if r["name"].lower() == low), None)
                or next((r for r in rows if low in r["name"].lower()), None))
    if feat is None:
        feat = max(rows, key=lambda r: r["vendors"] or 0)
    trend = con.execute(
        "SELECT month,price FROM price_point WHERE ingredient_id=? ORDER BY month",
        (feat["id"],)).fetchall()
    # native datalist: type to filter, no JS, works on mobile keyboards
    trend_opts = "".join(f"<option value='{E(r['name'])}'></option>" for r in rows)
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
      {ticker(con)}
      <div class=hi><h1>{greeting()}, {E(who)} 👋</h1>
        <div class=sub>Here's what's happening with your sourcing today.</div></div>
      {action_bar(con, rows, mv, wl)}
      <div class='panel pad'>
        <div class=ph><h3>Find ingredients. Compare. Source smart.</h3>
          <a href='/search'>View all →</a></div>
        {category_pills(con)}
        <div class=icards>{"".join(icard(r, wl, "/", mv) for r in rows[:8])}</div>
      </div>
      <div class=duo>
        <div class='panel pad'>
          <div class=ph><h3>Price trend</h3>
            <a href='/ingredient/{feat['id']}'>Details →</a></div>
          <form method=get action='/' style='margin:12px 0 4px'>
            <div class=trendfind>
              <svg viewBox='0 0 24 24' aria-hidden=true><circle cx=11 cy=11 r=7/>
                <path d='M21 21l-4.3-4.3'/></svg>
              <input name=trend list=trendlist class=trendsel autocomplete=off
                value='{E(feat['name'])}' placeholder='Search an ingredient…'
                aria-label='Search an ingredient to chart'>
              <datalist id=trendlist>{trend_opts}</datalist>
              <button class=trendgo>Show</button>
            </div>
          </form>
          <div class=metaline>Monthly average landed price, ₹/{E(feat['unit'])}</div>
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
    return page(con, "Dashboard", body, active="dashboard")


def view_search(con, params, wl=frozenset(), msg=""):
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
    # suppliers whose name matches — a query like "see pold" is looking for the
    # company, not an ingredient, so link straight to them
    vmatch = []
    if q:
        vwhere = " AND ".join(["name LIKE ?"] * len(q.split()))
        vmatch = con.execute(
            f"SELECT id,name,kind,state FROM vendor WHERE {vwhere} "
            "AND COALESCE(blacklisted,0)=0 ORDER BY name LIMIT 4",
            [f"%{t}%" for t in q.split()]).fetchall()
    vstrip = ("<div class=vhits>" + "".join(
        f"<a class=vhit href='/vendor/{v['id']}'><span class=av>{E(initials(v['name']))}</span>"
        f"<span><b>{E(v['name'])}</b><span class=metaline>{E(v['kind'])}"
        f"{' · ' + E(v['state']) if v['state'] else ''}</span></span>"
        f"<span class=go>View →</span></a>" for v in vmatch) + "</div>") if vmatch else ""
    # nothing matched literally: offer the nearest real names instead of a dead end
    didyoumean = ""
    if q and not rows and not vmatch:
        near = _fuzzy_suggest(con, q)[:4]
        if near:
            didyoumean = ("<div class=dym>Did you mean " + " · ".join(
                f"<a href='{n['h']}'>{E(n['l'])}</a>" for n in near) + "?</div>")
    export_link = (f"<a href='/export.csv{'?' + qs if qs else ''}' "
                   f"style='margin-left:auto;font-weight:700;font-size:12.5px' "
                   f"title='Download these results as a spreadsheet'>↓ Export CSV</a>"
                   if rows and is_master(con) else "")
    cats = {r["category"] for r in con.execute("SELECT DISTINCT category FROM ingredient")}
    opts = lambda vals, sel, label: (
        f"<option value=''>{label}</option>" +
        "".join(f"<option{' selected' if v == sel else ''}>{E(v)}</option>" for v in vals))

    body = f"""
      <div class=hi><h1>Search ingredients</h1>
        <div class=sub>Compare vendor price bands, documents, supplier type and market trend.</div></div>
      {category_pills(con, q if q in cats else "")}
      {add_ingredient_form(con, q if not rows else "", msg)}
      <div class='panel pad'><form class=filters method=get action='/search'>
        <input type=search name=q placeholder='Ingredient, CAS, function…' value='{E(q)}'>
        <select name=kind>{opts(VENDOR_KINDS, kind, 'Any vendor type')}</select>
        <select name=doc>{opts(DOC_TYPES, doc, 'Any document')}</select>
        <input name=maxp inputmode=decimal placeholder='Max ₹/unit' value='{E(raw)}' style='width:150px'>
        <button>Search</button>
      </form></div>
      <div style='display:flex;align-items:baseline;gap:12px;flex-wrap:wrap'>
        <h2 style='margin-bottom:0'>{len(rows)} ingredient{'' if len(rows) == 1 else 's'}</h2>
        {export_link}
      </div>
      {vstrip}{didyoumean}
      {(f'''<div class=tablewrap><table class=itable>
        <thead><tr><th></th><th>Ingredient</th><th>Price range</th><th>Suppliers</th>
          <th>12-mo</th><th>Updated</th></tr></thead>
        <tbody>{"".join(irow(r, wl, back, mv) for r in rows)}</tbody></table></div>''') if rows else
        f"<div class='panel pad' style='text-align:center'>"
        f"<p style='margin:0 0 10px'>No match for that search.</p>"
        f"<p class=metaline style='margin:0 0 14px'>Add it to the catalogue yourself using the "
        f"form above, or raise a sourcing request and our purchase team will find a supplier.</p>"
        f"<a class=vbook href='/requests?ing={urllib.parse.quote(q)}'>Request this ingredient</a></div>"}"""
    return page(con, "Search", body, active="search", q=q)


def add_ingredient_form(con, prefill="", msg="", open_it=False):
    """Anyone signed in can add a missing ingredient — the catalogue grows from
    the people actually sourcing. No prices here: those come from suppliers."""
    cats = [r["category"] for r in con.execute(
        "SELECT DISTINCT category FROM ingredient ORDER BY category")]
    opts = "".join(f"<option>{E(c)}</option>" for c in cats)
    units = "".join(f"<option{' selected' if u == 'kg' else ''}>{u}</option>"
                    for u in ("kg", "g", "litre", "piece"))
    return f"""
      <details class='panel pad addsup' style='margin-bottom:16px'{' open' if (open_it or prefill or msg) else ''}>
        <summary>+ Add an ingredient to the catalogue</summary>
        {f"<div class=ok style='margin-top:12px'>{E(msg)}</div>" if msg else ""}
        <div class=metaline style='margin-top:10px'>Missing something you buy? Add it and it's
          searchable for everyone. Suppliers and prices get attached later.</div>
        <form method=post action='/ingredient/new' style='margin-top:12px'>
          <div class=vform>
            <label class=full>Ingredient name
              <input name=name required maxlength=140 value='{E(prefill)}'
                placeholder='e.g. Organic Ashwagandha Root Extract 10%'></label>
            <label>Category
              <select name=category><option value=''>Auto-detect</option>{opts}</select></label>
            <label>CAS number <input name=cas maxlength=40 placeholder='optional'></label>
            <label>Unit <select name=unit>{units}</select></label>
            <label class=full>Function / use
              <input name=functions maxlength=140
                placeholder='e.g. Adaptogen, stress support'></label>
            <label class=full>Description
              <textarea name=description maxlength=600 rows=2
                placeholder='Grade, assay, typical application…'
                style='font:inherit;padding:9px 11px;border:1px solid var(--line);
                       border-radius:9px'></textarea></label>
          </div>
          <button style='margin-top:12px'>Add ingredient</button>
        </form>
      </details>"""


def export_csv(con, params):
    """Search results as a CSV a buyer can drop straight into a costing sheet."""
    import csv
    import io
    rows = search_ingredients(con, params.get("q", [""])[0].strip(),
                              params.get("kind", [""])[0] if params.get("kind", [""])[0] in VENDOR_KINDS else "",
                              params.get("doc", [""])[0] if params.get("doc", [""])[0] in DOC_TYPES else "",
                              None)
    mv = moves_map(con)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Ingredient", "Category", "CAS", "Suppliers", "Price low",
                "Price high", "Unit", "12-mo change %", "Last updated"])
    for r in rows:
        pct = mv.get(r["id"])
        w.writerow([r["name"], r["category"], r["cas"], r["vendors"],
                    f"{r['lo']:.0f}" if r["lo"] else "", f"{r['hi']:.0f}" if r["hi"] else "",
                    r["unit"], f"{pct:.1f}" if pct is not None else "", r["updated"] or ""])
    return buf.getvalue().encode("utf-8-sig")   # BOM so Excel reads ₹ names correctly


def irow(r, wl=frozenset(), back="/search", mv=None):
    """One ingredient as a table row (search list view)."""
    on = r["id"] in wl
    star = (f"<a class='star2{' on' if on else ''}' onclick='event.stopPropagation()' "
            f"href='/watch?id={r['id']}&back={urllib.parse.quote(back)}' "
            f"title='{'Remove from' if on else 'Add to'} watchlist'>{'★' if on else '☆'}</a>")
    price = (f"₹{r['lo']:,.0f}–{r['hi']:,.0f}<span class=metaline> /{E(r['unit'])}</span>"
             if r["lo"] else "<span class=metaline>—</span>")
    pct = (mv or {}).get(r["id"])
    trend = ("<span class=metaline>—</span>" if pct is None else
             f"<span class={'up' if pct >= 0 else 'down'}>{'▲' if pct >= 0 else '▼'} {abs(pct):.1f}%</span>")
    upd = f"{E(r['updated'])}" if r["updated"] else "—"
    cc = cat_color(r["category"])
    return (f"<tr onclick=\"location='/ingredient/{r['id']}'\" style='cursor:pointer;--cc:{cc}'"
            f" class=crow>"
            f"<td class=starcell>{star}</td>"
            f"<td><a href='/ingredient/{r['id']}'><b>{E(r['name'])}</b></a>"
            f"<div class=metaline style='color:var(--cc);font-weight:700'>{E(r['category'])}</div></td>"
            f"<td><span class=price style='font-size:14px'>{price}</span></td>"
            f"<td>{r['vendors']}</td><td>{trend}</td><td class=metaline>{upd}</td></tr>")


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
    return page(con, "Watchlist", body, active="watch")


def _ago(s):
    return "just now" if s < 15 else f"{s}s ago" if s < 60 else f"{s // 60}m ago"


def view_admin(con):
    who = online_list()
    online_rows = "".join(
        f"""<tr><td><b>{E(u['label'])}</b></td>
        <td class=metaline>{E(u['code'] or '—')}</td>
        <td class=metaline>{E(u['ip'])}</td>
        <td>{'<span class=tag>admin</span>' if u['admin'] else ''}</td>
        <td class=metaline>{_ago(u['ago'])}</td>
        <td>{'' if u['admin'] or not u['code'] else
             f"<form method=post action='/admin/kick' style='margin:0'>"
             f"<input type=hidden name=code value='{E(u['code'])}'>"
             f"<button class=xbtn>Remove</button></form>"}</td></tr>"""
        for u in who) \
        or "<tr><td colspan=6 class=empty>No one online right now.</td></tr>"

    invites = con.execute(
        "SELECT * FROM invite ORDER BY is_admin DESC, revoked, created DESC").fetchall()
    admin_code = next((iv["code"] for iv in invites if iv["is_admin"]), "")
    env_invites = ",".join(f"{iv['code']}:{(iv['note'] or 'Invitee').replace(',', ' ')}"
                           for iv in invites if not iv["is_admin"] and not iv["revoked"])
    reqs = con.execute("SELECT * FROM request ORDER BY (status='Open') DESC, id DESC").fetchall()
    open_reqs = sum(1 for r in reqs if r["status"] == "Open")
    req_rows = ""
    for r in reqs:
        opts = "".join(f"<option{' selected' if r['status'] == s else ''}>{s}</option>"
                       for s in REQUEST_STATUS)
        req_rows += f"""<div class=review>
          <div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>
            <b>{E(r['ingredient'])}</b>{status_badge(r['status'])}</div>
          <div class=metaline style='margin-top:4px'>{E(r['requester'] or '')}
            {f"· {E(r['company'])}" if r['company'] else ''} · raised {E(r['created'] or '')}</div>
          {f"<div class=metaline style='margin-top:6px;color:var(--ink)'>{E(r['details'])}</div>" if r['details'] else ""}
          <form method=post action='/admin/request' class=filters style='margin-top:10px'>
            <input type=hidden name=id value='{r['id']}'>
            <select name=status>{opts}</select>
            <input name=reply value='{E(r['reply'] or '')}'
              placeholder='Update for the requester…' maxlength=400 style='flex:1;min-width:200px'>
            <button>Update</button></form></div>"""
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

    # Loud, because silently losing every edit on deploy is the worst failure here
    storage = ("<div class='trial'><i class=bar></i><span class=pin>Storage</span>"
               "<span>Durable — edits are saved to Postgres and survive deploys.</span></div>"
               if PG else
               "<div class='trial warn'><i class=bar></i><span class=pin>Not saved</span>"
               "<span><b>DATABASE_URL is not set.</b> This instance uses a temporary file, so "
               "every supplier edit, review and request is wiped on the next deploy and the "
               "catalogue reloads from suppliers.csv. Set DATABASE_URL to a Postgres "
               "connection string to keep changes.</span></div>")
    body = f"""
      <div class=hi><h1>Admin</h1>
        <div class=sub>Manage sourcing requests, invites and see who's using the pilot right now.</div></div>
      {storage}
      <div class='panel pad'>
        <div class=ph><h3>Sourcing requests</h3>
          <span class=count>{open_reqs} open · {len(reqs)} total</span></div>
        <div class=metaline style='margin:8px 0 14px'>Buyer requests for ingredients not yet on Ingrex.
          Set a status and reply — the requester sees it on their Requests page.</div>
        {req_rows or "<p class=empty>No sourcing requests yet.</p>"}
      </div>
      <div class='panel pad'>
        <div class=ph><h3>Online now</h3><span class=count>{len(who)} active · last 5 min</span></div>
        <div class=tablewrap style='margin-top:14px'><table>
          <thead><tr><th>User</th><th>Invite code</th><th>IP</th><th></th>
            <th>Last active</th><th></th></tr></thead>
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
      </div>
      <div class='panel pad'>
        <div class=ph><h3>Keep users across deploys</h3></div>
        <div class=metaline style='margin:8px 0 14px'>Free hosting resets the database on every
          deploy, so codes created here are temporary. Paste these into your host's
          <b>Environment</b> and they'll be re-created on every restart — permanently.</div>
        <label style='font-size:11px;font-weight:700;color:var(--mut)'>INGREX_ADMIN_CODE</label>
        <textarea readonly onclick=this.select() class=envbox rows=1>{E(admin_code)}</textarea>
        <label style='font-size:11px;font-weight:700;color:var(--mut);margin-top:10px;display:block'>INGREX_INVITES</label>
        <textarea readonly onclick=this.select() class=envbox rows=2>{E(env_invites)}</textarea>
        <div class=metaline style='margin-top:8px'>Also set <code class=inv>INGREX_SECRET</code>
          to any long random string so logins survive restarts.</div>
      </div>"""
    return page(con, "Admin", body, active="admin")


def view_myreviews(con):
    ident = current()
    prof = (con.execute("SELECT name FROM profile WHERE code=?", (ident["code"],)).fetchone()
            if ident else None)
    name = prof["name"] if prof and prof["name"] else (ident["note"] if ident else "Anonymous")
    rows = con.execute("""
        SELECT r.*, v.id vid, v.name vname FROM rating r JOIN vendor v ON v.id=r.vendor_id
        WHERE r.rater=? ORDER BY r.id DESC""", (name,)).fetchall()
    cards = "".join(
        f"""<div class=review>
          <a href='/vendor/{r['vid']}'><b>{E(r['vname'])}</b></a> {stars(r['score'])}
          <div class=metaline style='margin-top:6px'>{E(r['note'] or '')}</div>
          <div class=count style='margin-top:4px'>{E(r['created'] or '')}</div></div>"""
        for r in rows)
    inner = cards or ("<div class='panel pad'><p class=empty>You haven't reviewed any suppliers yet. "
                      "Open a supplier and rate them from their page.</p></div>")
    body = f"""
      <div class=hi><h1>My reviews</h1>
        <div class=sub>Ratings you've posted, as {E(name)}.</div></div>{inner}"""
    return page(con, "My reviews", body, active="reviews")


def status_badge(s):
    cls = {"Open": "st-open", "In progress": "st-prog", "Sourcing vendor": "st-prog",
           "Fulfilled": "st-done", "Closed": "st-closed"}.get(s, "st-open")
    return f"<span class='sbadge {cls}'>{E(s)}</span>"


def requester_identity(con):
    ident = current()
    prof = (con.execute("SELECT name,company FROM profile WHERE code=?",
                        (ident["code"],)).fetchone() if ident else None)
    name = prof["name"] if prof and prof["name"] else (ident["note"] if ident else "You")
    company = prof["company"] if prof and prof["company"] else ""
    code = ident["code"] if ident else ""
    return code, name, company


def ticker(con):
    """Scrolling community ticker of open sourcing requests, shown app-wide."""
    reqs = con.execute("SELECT id,ingredient,company FROM request "
                       "WHERE status!='Closed' ORDER BY id DESC LIMIT 20").fetchall()
    if not reqs:
        return ""
    items = "".join(
        f"<a href='/requests#r{r['id']}'>◎ <b>{E(r['ingredient'])}</b>"
        f"<span> — {E(r['company'] or 'a buyer')} is sourcing</span></a>" for r in reqs)
    return (f"<div class=ticker><span class=ticker-label>Community sourcing</span>"
            f"<div class=ticker-track><div class=ticker-run>{items}{items}</div></div>"
            f"<a class=ticker-cta href='/requests'>Contribute →</a></div>")


def req_card(con, r, code, name):
    leads = con.execute("SELECT * FROM req_note WHERE request_id=? ORDER BY id", (r["id"],)).fetchall()
    def lead_line(l):
        co = f" · {E(l['company'])}" if l["company"] else ""
        return (f"<div class=lead><b>{E(l['author'])}</b>{co}: {E(l['note'])}"
                f"<span class=count> · {E(l['created'] or '')}</span></div>")
    lead_html = "".join(lead_line(l) for l in leads)
    owned = r["code"] == code or r["requester"] == name
    mine = " <span class='tag func'>your request</span>" if owned else ""
    reply = f"<div class=rreply><b>Purchase team:</b> {E(r['reply'])}</div>" if r["reply"] else ""
    remove = (f"<form method=post action='/request_close' style='margin-left:auto'>"
              f"<input type=hidden name=id value='{r['id']}'>"
              f"<button class=xbtn title='Remove from board'>Remove</button></form>"
              if owned or is_admin() or not gate_active(con) else "")
    return f"""<div class=review id=r{r['id']}>
      <div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>
        <b>{E(r['ingredient'])}</b>{status_badge(r['status'])}{mine}{remove}</div>
      <div class=metaline style='margin-top:4px'>{E(r['requester'] or 'A buyer')}
        {f"· {E(r['company'])}" if r['company'] else ''} · raised {E(r['created'] or '')}</div>
      {f"<div class=metaline style='margin-top:6px;color:var(--ink)'>{E(r['details'])}</div>" if r['details'] else ""}
      {reply}
      <div class=leads>{lead_html or "<div class=metaline>No community leads yet — know a supplier? Add one.</div>"}</div>
      <form method=post action='/request_note' class=filters style='margin-top:8px'>
        <input type=hidden name=id value='{r['id']}'>
        <input name=note required maxlength=400 placeholder='Know a supplier for this? Add a lead…' style='flex:1'>
        <button>Add lead</button></form></div>"""


def view_requests(con, prefill="", msg=""):
    code, name, company = requester_identity(con)
    active = con.execute("SELECT * FROM request WHERE status!='Closed' "
                         "ORDER BY (status='Open') DESC, id DESC").fetchall()
    board = "".join(req_card(con, r, code, name) for r in active)
    body = f"""
      <div class=hi><h1>Sourcing requests</h1>
        <div class=sub>Can't find an ingredient? Raise a request — the purchase team sources a
          supplier, and the whole community can chip in with leads.</div></div>
      <div class='panel pad' style='margin-bottom:16px'>
        {f"<p class=down style='margin-top:0'>{E(msg)}</p>" if msg else ""}
        <form method=post action='/requests'>
          <div class=f style='margin-bottom:10px'>
            <label style='font-size:11px;font-weight:650;color:var(--mut)'>Ingredient you need</label>
            <input name=ingredient required maxlength=140 value='{E(prefill)}'
              placeholder='e.g. Organic Ashwagandha Root Extract 10% Withanolides' style='width:100%'></div>
          <div class=f style='margin-bottom:12px'>
            <label style='font-size:11px;font-weight:650;color:var(--mut)'>Specs, quantity, target price (optional)</label>
            <textarea name=details maxlength=800 rows=3
              placeholder='Grade / assay, monthly quantity, target ₹/kg, certifications needed…'
              style='width:100%'></textarea></div>
          <button>Submit request</button>
        </form>
        <div class=metaline style='margin-top:10px'>Raising as
          <b style='color:var(--ink)'>{E(name)}{f' · {E(company)}' if company else ''}</b>.</div>
      </div>
      <h2>Community board ({len(active)} open)</h2>
      {board or "<div class='panel pad'><p class=empty>No open requests. Raise one above.</p></div>"}"""
    return page(con, "Sourcing requests", body, active="requests")


def view_plans(con, msg=""):
    plan, cycle, left = subscription(con)
    max_save = max(round((1 - yr / mo) * 100) for _, _, mo, yr, *_ in PLANS if mo)
    cards = ""
    for i, (key, nm, mo, yr, blurb, feats) in enumerate(PLANS):
        best = " best" if key == "growth" else ""
        if mo:
            save = round((1 - yr / mo) * 100)
            price = (f"<div class='amt mo'>₹{mo:,}<small> /month</small></div>"
                     f"<div class='amt yr'>₹{yr:,}<small> /month</small></div>"
                     f"<div class='save mo'>&nbsp;</div>"
                     f"<div class='save yr'>Save {save}% — billed ₹{yr * 12:,} yearly</div>")
        else:
            price = ("<div class=amt>Custom<small> /month</small></div>"
                     "<div class=save>Priced to your volume</div>")
        feat_html = "".join(f"<li>{E(f)}</li>" for f in feats)
        if plan == key:
            action = "<div class=onplan>Your current plan</div>"
        elif mo:
            # one form per cycle — the hidden one is display:none, so only the
            # visible cycle's button can be clicked (and only its value posts)
            action = "".join(
                f"<form method=post action='/plans' class={c}>"
                f"<input type=hidden name=plan value='{key}'>"
                f"<input type=hidden name=cycle value='{cy}'>"
                f"<button>Choose {E(nm)}</button></form>"
                for c, cy in (("mo", "monthly"), ("yr", "yearly")))
        else:
            action = "<a class=ghost href='/requests'>Talk to sales</a>"
        cards += (f"<div class='plan{best}'>"
                  f"{'<span class=tagbest>Most popular</span>' if best else ''}"
                  f"<h3>{E(nm)}</h3><div class=blurb>{E(blurb)}</div>"
                  f"{price}<ul>{feat_html}</ul>{action}</div>")

    if msg:
        head = ""                       # the confirmation already says which plan is live
    elif plan:
        head = (f"<div class=ok>You're on <b>{E(plan_name(plan))}</b>, billed "
                f"{E(cycle or 'monthly')}. Changing plan? Pick one below — "
                f"our team will confirm before anything is charged.</div>")
    elif left:
        head = (f"<div class=ok>You have <b>{left} day{'' if left == 1 else 's'}</b> left on your "
                f"free month. Pick a plan any time — it starts when the trial ends.</div>")
    else:
        head = ""
    body = f"""
      <div class=hi><h1>Plans &amp; billing</h1>
        <div class=sub>Every account starts with a free month. No card up front, cancel any time.</div></div>
      {f"<div class=ok>{E(msg)}</div>" if msg else ""}{head}
      <input type=radio name=cyc id=cyc-m checked><input type=radio name=cyc id=cyc-y>
      <div class=cyc><label for=cyc-m>Monthly</label>
        <label for=cyc-y>Yearly · save up to {max_save}%</label></div>
      <div class=plans>{cards}</div>
      <div class='panel pad' style='margin-top:18px'>
        <div class=ph><h3>How billing works</h3></div>
        <div class=metaline style='margin-top:8px'>Your first month is free from the day you join —
          full access to the catalogue, price bands and sourcing requests. Picking a plan here tells
          our team which one you want; we confirm by email and raise an invoice (NEFT / UPI / card
          on the invoice). No payment details are ever entered into this portal.</div>
      </div>"""
    return page(con, "Plans", body, active="account")


def view_account(con, msg=""):
    ident = current()
    p = account(con)
    plan, cycle, left = subscription(con)
    v = lambda k: E((p[k] or "") if p and k in p.keys() else "")
    roles = "".join(f"<option{' selected' if p and p['role'] == r else ''}>{E(r)}</option>"
                    for r in BUSINESS_ROLES)
    if plan:
        status = f"{E(plan_name(plan))} · billed {E(cycle or 'monthly')}"
    elif left:
        status = f"Free trial · {left} day{'' if left == 1 else 's'} left"
    elif left == 0:
        status = "Trial ended"
    else:
        status = "Admin access"
    signed_google = bool(p and "email" in p.keys() and p["email"])
    reviews = requests_n = 0
    if ident:
        reviews = con.execute("SELECT COUNT(*) n FROM rating WHERE rater=?",
                              (ident["note"] or "",)).fetchone()["n"]
        requests_n = con.execute("SELECT COUNT(*) n FROM request WHERE code=?",
                                 (ident["code"],)).fetchone()["n"]
    form = (f"""
      <div class='panel pad' id=details>
        <div class=ph><h3>Account information</h3></div>
        <form method=post action='/account' style='margin-top:14px'>
          <div class=grid2>
            <div><label class=fl>Full name</label>
              <input name=name required maxlength=80 value='{v('name')}'></div>
            <div><label class=fl>Company / organisation</label>
              <input name=company required maxlength=120 value='{v('company')}'></div>
            <div><label class=fl>Business type</label>
              <select name=role required>{roles}</select></div>
            <div><label class=fl>GSTIN</label>
              <input name=gst maxlength=15 value='{v('gst')}'
                style='text-transform:uppercase'></div>
            <div><label class=fl>City</label>
              <input name=city maxlength=60 value='{v('city')}'></div>
            <div><label class=fl>Phone</label>
              <input name=phone maxlength=20 value='{v('phone')}'
                placeholder='Optional — for quote callbacks'></div>
          </div>
          <button style='margin-top:14px'>Save changes</button>
        </form>
      </div>""" if p else
      "<div class='panel pad'><p class=empty>Admin and supplier logins don't carry a buyer "
      "profile. Sign in with an invited buyer account to edit these details.</p></div>")
    body = f"""
      <div class=hi><h1>Account settings</h1>
        <div class=sub>Your details, plan and activity on Ingrex.</div></div>
      {f"<div class=ok>{E(msg)}</div>" if msg else ""}
      <div class=acct>
        {form}
        <div style='display:flex;flex-direction:column;gap:16px'>
          <div class='panel pad'>
            <div class=ph><h3>Plan</h3><a href='/plans'>Change →</a></div>
            <div style='margin-top:10px'>
              <div class=kv><span class=k>Status</span><span class=v>{status}</span></div>
              <div class=kv><span class=k>Signed in as</span>
                <span class=v>{E(ident['note'] if ident else 'Guest')}</span></div>
              {f"<div class=kv><span class=k>Email</span><span class=v>{v('email')}</span></div>" if signed_google else ""}
              <div class=kv><span class=k>Invite code</span>
                <span class=v><code class=inv>{E(ident['code'] if ident else '—')}</code></span></div>
              <div class=kv><span class=k>Member since</span>
                <span class=v>{v('created') or '—'}</span></div>
            </div>
          </div>
          <div class='panel pad'>
            <div class=ph><h3>Your activity</h3></div>
            <div style='margin-top:10px'>
              <div class=kv><span class=k>Supplier reviews written</span>
                <span class=v>{reviews}</span></div>
              <div class=kv><span class=k>Sourcing requests raised</span>
                <span class=v>{requests_n}</span></div>
              <div class=kv><span class=k>Watchlist</span>
                <span class=v><a href='/watchlist'>View →</a></span></div>
            </div>
          </div>
          <div class='panel pad' id=security>
            <div class=ph><h3>Security</h3></div>
            <div style='margin-top:10px'>
              <div class=kv><span class=k>Sign-in method</span>
                <span class=v>{'Google' if signed_google else 'Invite code'}</span></div>
              <div class=kv><span class=k>Session</span>
                <span class=v>This device · 30 days</span></div>
              <div class=kv><span class=k>Access</span>
                <span class=v>{'Admin' if is_admin() else 'Buyer'}</span></div>
            </div>
            <div class=metaline style='margin:12px 0'>Your invite code is the key to this
              account — treat it like a password and don't share it. Lost it or think someone
              else has it? Ask your Ingrex contact to revoke and reissue.</div>
            <a class=ghost href='/logout'
              style='display:inline-block;padding:9px 16px;border:1px solid var(--line);
                     border-radius:9px;font-weight:700;font-size:13px'>Log out</a>
          </div>
        </div>
      </div>"""
    return page(con, "Account", body, active="account")


def view_insights(con):
    movers = market_movers(con, 200)
    rises = [m for m in sorted(movers, key=lambda m: m["pct"], reverse=True) if m["pct"] > 0][:6]
    falls = [m for m in sorted(movers, key=lambda m: m["pct"]) if m["pct"] < 0][:6]
    cats = con.execute("""
        SELECT i.category, COUNT(DISTINCT i.id) n FROM ingredient i
        GROUP BY i.category ORDER BY n DESC""").fetchall()
    kinds = {r["kind"]: r["n"] for r in
             con.execute("SELECT kind, COUNT(*) n FROM vendor GROUP BY kind")}
    avg_mv = sum(m["pct"] for m in movers) / len(movers) if movers else 0
    # per-category average % movement (no exact prices shown anywhere on insights)
    mv = moves_map(con)
    cat_of = {r["id"]: r["category"] for r in
              con.execute("SELECT id, category FROM ingredient")}
    by_cat = {}
    for i, pct in mv.items():
        by_cat.setdefault(cat_of.get(i), []).append(pct)
    cat_pct = {c["category"]: (sum(ps) / len(ps) if (ps := by_cat.get(c["category"])) else None)
               for c in cats}

    def mlist(items, up):
        if not items:
            return "<p class=empty>No movement.</p>"
        return "".join(
            f"<a class=mover href='/ingredient/{m['id']}'>"
            f"<span><span class=nm>{E(m['name'])}</span>"
            f"<div class=pr>{E(m['unit'])} · market</div></span>"
            f"<span class='pc {'up' if up else 'down'}'>{'▲' if up else '▼'} "
            f"{abs(m['pct']):.1f}%</span></a>" for m in items)

    def cat_row(c):
        p = cat_pct.get(c["category"])
        chg = ("—" if p is None else
               f"<span class={'up' if p >= 0 else 'down'}>{'+' if p >= 0 else ''}{p:.1f}%</span>")
        return (f"<tr><td><b>{E(c['category'])}</b></td><td>{c['n']}</td><td>{chg}</td></tr>")
    cat_rows = "".join(cat_row(c) for c in cats)

    body = f"""
      <div class=hi><h1>Market insights</h1>
        <div class=sub>Where nutraceutical ingredient prices are heading across the catalogue.</div></div>
      <div class=stats>
        <div class=stat><div class=l>Ingredients</div><div class=v>{sum(c['n'] for c in cats)}</div>
          <div class=d>across {len(cats)} categories</div></div>
        <div class=stat><div class=l>Suppliers</div><div class=v>{sum(kinds.values())}</div>
          <div class=d><b>{kinds.get('Manufacturer', 0)}</b> mfrs · {kinds.get('Trader', 0)} traders · {kinds.get('Importer', 0)} importers</div></div>
        <div class=stat><div class=l>Avg 12-mo move</div>
          <div class=v><span class={'up' if avg_mv >= 0 else 'down'}>{'+' if avg_mv >= 0 else ''}{avg_mv:.1f}%</span></div>
          <div class=d>across priced items</div></div>
        <div class=stat><div class=l>Categories</div><div class=v>{len(cats)}</div>
          <div class=d>ingredient groups</div></div>
      </div>
      <div class=duo>
        <div class='panel pad'>
          <div class=ph><h3>Prices rising</h3><span class=count>watch these</span></div>
          <div class=metaline style='margin-bottom:6px'>Biggest 12-month increases.</div>
          {mlist(rises, True)}</div>
        <div class='panel pad'>
          <div class=ph><h3>Prices easing</h3><span class=count>buy opportunities</span></div>
          <div class=metaline style='margin-bottom:6px'>Biggest 12-month decreases.</div>
          {mlist(falls, False)}</div>
      </div>
      <div class='panel pad'>
        <div class=ph><h3>Category overview</h3><span class=count>avg 12-mo change</span></div>
        <div class=tablewrap style='margin-top:12px'><table>
          <thead><tr><th>Category</th><th>Ingredients</th><th>12-mo change</th></tr></thead>
          <tbody>{cat_rows}</tbody></table></div>
      </div>"""
    return page(con, "Market insights", body, active="insights")


def view_ingredient(con, ing_id, wl=frozenset(), msg=""):
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
    cheapest = min((o["price_min"] for o in offers), default=None)
    dearest = max((o["price_max"] for o in offers), default=None)
    cards = "".join(vendor_card(o, ing, best=(o["price_min"] == cheapest)) for o in offers)
    last_upd = max((o["updated"] for o in offers if o["updated"]), default=None)
    pct = ((trend[-1]["price"] - trend[0]["price"]) / trend[0]["price"] * 100
           if trend and trend[0]["price"] else None)
    pct_html = (f"<span class={'up' if pct >= 0 else 'down'}>{'+' if pct >= 0 else ''}{pct:.1f}%</span>"
                if pct is not None else "—")
    make = MATERIAL_MAKE.get(ing["category"], "Supplier-specified")
    facts = f"""<div class=facts>
      <div class=fact><span class=fl>Make / origin</span>
        <span class=fv style='font-weight:600;font-size:11.5px;text-align:right'>{E(make)}</span></div>
      <div class=fact><span class=fl>Category</span>
        <span class=fv style='font-weight:600;font-size:11.5px'>{E(ing['category'])}</span></div>
      <div class=fact><span class=fl>Price range</span>
        <span class=fv>{'₹%s–%s' % (f"{cheapest:,.0f}", f"{dearest:,.0f}") if cheapest else '—'}
        <span class=funit>/{E(ing['unit'])}</span></span></div>
      <div class=fact><span class=fl>Suppliers</span><span class=fv>{len(offers)}</span></div>
      <div class=fact><span class=fl>12-mo change</span><span class=fv>{pct_html}</span></div>
      <div class=fact><span class=fl>Updated</span><span class=fv>{E(last_upd or '—')}</span></div>
    </div>"""

    return page(con, ing["name"], f"""
      <a class=back href='/'>← Ingredients</a>
      {f"<div class=ok>{E(msg)}</div>" if msg else ""}
      <div class=titlerow><h1>{E(ing['name'])}</h1>{wbtn}</div>
      <p class=metaline>{E(ing['category'])} · CAS {E(ing['cas'])}</p>
      <div class=chips style='margin:9px 0 16px'>
        {"".join(f"<span class='tag func'>{E(f.strip())}</span>" for f in ing['functions'].split(','))}</div>
      <div class=igrid>
        <div class=imain>
          <div class=ph style='margin-bottom:12px'>
            <h2 style='margin:0'>{len(offers)} supplier{'' if len(offers) == 1 else 's'}</h2>
            {f"<span class=count>Prices updated {E(last_upd)}</span>" if last_upd else ""}</div>
          <div class=vlist>{cards or "<p class=empty>No suppliers listed yet.</p>"}</div>
        </div>
        <aside class=iside>
          <div class=card style='margin:0 0 12px'>
            <div class=ph><h3 style='font-size:13px'>Market trend</h3>{pct_html}</div>
            <div style='margin-top:8px'>{price_chart([(m['month'], m['price']) for m in trend], 300, 150)}</div>
            <div class=metaline style='margin-top:8px;font-size:11.5px'>Hover for a month's price ·
              indicative ₹/{E(ing['unit'])}</div></div>
          <div class=card style='margin:0 0 12px'>{facts}</div>
          <div class=card style='margin:0;color:var(--body);font-size:12.5px'>{E(ing['description'])}</div>
        </aside>
      </div>""", active="search")


def vendor_card(o, ing, best=False):
    """Marketplace-style supplier card with clear visual hierarchy."""
    cc = cat_color(ing["category"])
    ini = initials(o["vname"])
    rating = (f"<span class=vr><span class=st>★</span> {o['avg_score']:.1f} "
              f"<span class=vrn>({o['n_score']})</span></span>" if o["avg_score"]
              else "<span class=vr vr-new>New supplier</span>")
    verified = ("<span class=vbadge-verified>◈ Verified</span>"
                if (o["gst"] or "").strip() else "")
    bestbadge = "<span class=vbadge-best>Best Price</span>" if best else ""
    chips = (f"<span class=vchip>◷ {E(o['moq'] or 'MOQ on request')}</span>"
             f"<span class=vchip>⚑ Lead {str(o['lead_days']) + ' days' if o['lead_days'] else 'on request'}</span>"
             f"<span class='vchip vkind {E(o['kind'])}'>{E(o['kind'])}</span>")
    loc = f"{E(o['city'])}" + (f" · {E(o['state'])}" if o["state"] and o["state"] != o["city"] else "")
    email = (o["email"] or "").split(",")[0].strip()
    cta = (f"<a class=vbook href='mailto:{E(email)}?subject={urllib.parse.quote('Enquiry: ' + ing['name'])}'>Enquire</a>"
           if email else f"<a class=vbook href='/vendor/{o['vid']}'>View</a>")
    return (f"<div class=vcard style='--cc:{cc}'>"
            f"<div class=vmono>{ini}</div>"
            f"<div class=vbody>"
            f"<div class=vtop>{bestbadge}"
            f"<a class=vname href='/vendor/{o['vid']}'>{E(o['vname'])}</a>{verified}</div>"
            f"<div class=vmeta>{rating}<span class=vdot>·</span><span class=vloc>{loc}</span></div>"
            f"<div class=vchips>{chips}</div></div>"
            f"<div class=vright>"
            f"<div class=vprice>₹{o['price_min']:,.0f}–{o['price_max']:,.0f}<span class=vunit> /{E(o['unit'])}</span></div>"
            f"<div class=vpricesub>indicative range</div>"
            f"{cta}</div></div>")


def view_vendors(con, q="", bl=False):
    q = (q or "").strip()
    admin = is_admin() or not gate_active(con)
    where, args = [], []
    if q:
        where.append("(v.name LIKE ? OR v.state LIKE ? OR v.poc LIKE ? OR v.gst LIKE ?)")
        args += [f"%{q}%"] * 4
    if admin and bl:
        where.append("COALESCE(v.blacklisted,0)=1")     # admin: blacklisted-only view
    else:
        where.append("COALESCE(v.blacklisted,0)=0")     # default: hide blacklisted for everyone
    sql = ("SELECT v.*, (SELECT AVG(score) FROM rating WHERE vendor_id=v.id) a,"
           " (SELECT COUNT(*) FROM rating WHERE vendor_id=v.id) n,"
           " (SELECT COUNT(*) FROM offer WHERE vendor_id=v.id) items FROM vendor v"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY a DESC NULLS LAST, v.name")
    rows = con.execute(sql, args).fetchall()
    # "sea pold" should still find "See Pold Chemicals"
    near = ""
    if q and not rows:
        hits = [h for h in _fuzzy_suggest(con, q) if h["t"] == "Supplier"][:4]
        if hits:
            near = ("<div class=dym>Did you mean " + " · ".join(
                f"<a href='{h['h']}'>{E(h['l'])}</a>" for h in hits) + "?</div>")
    n_bl = con.execute("SELECT COUNT(*) c FROM vendor WHERE COALESCE(blacklisted,0)=1").fetchone()["c"]
    cards = "".join(f"""<a class=tile href='/vendor/{v['id']}'>
        <div class=ttl>{E(v['name'])}
          {"<span class=tag style='color:#b4541c;border-color:#e6c3ad'>blacklisted</span>" if (admin and "blacklisted" in v.keys() and v["blacklisted"]) else ""}</div>
        <div style='margin:9px 0 7px'><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
          <span class=metaline>{E(v['city'])}, {E(v['country'])}</span></div>
        <div style='margin-bottom:6px'>{stars(v['a'])} <span class=count>({v['n']})</span></div>
        <div class=count>{v['items']} ingredient(s) listed</div></a>""" for v in rows)
    add_form = (f"""
      <details class='panel pad addsup' style='margin-bottom:16px'>
        <summary>+ Add a supplier</summary>
        <form method=post action='/admin/vendor' style='margin-top:14px'>
          <div class=vform>
            <label>Company name<input name=name required maxlength=140 placeholder='e.g. Acme Nutra Pvt Ltd'></label>
            <label>Type<select name=kind>{"".join(f"<option>{k}</option>" for k in VENDOR_KINDS)}</select></label>
            <label>Contact person<input name=poc maxlength=80></label>
            <label>Phone<input name=phone maxlength=40></label>
            <label>Email<input name=email maxlength=140></label>
            <label>GSTIN<input name=gst maxlength=15 style='text-transform:uppercase'></label>
            <label>State<input name=state maxlength=60></label>
            <label>Pincode<input name=pincode maxlength=10></label>
            <label class=full>Address<input name=address maxlength=200></label>
          </div>
          <button style='margin-top:14px'>Add supplier</button></form>
      </details>""" if (is_admin() or not gate_active(con)) else "")
    return page(con, "Vendors", f"""
      <div class=hi><h1>Suppliers</h1>
        <div class=sub>Manufacturers, traders and importers on the platform.</div></div>
      {add_form}
      <div class='panel pad' style='margin-bottom:16px'><form class=filters method=get action='/vendors'>
        <input type=search name=q value='{E(q)}'
          placeholder='Search suppliers by name, state, contact or GST…'>
        <button>Search</button></form></div>
      <div class=ph style='margin-bottom:12px'>
        <h2 style='margin:0'>{len(rows)} supplier{'' if len(rows) == 1 else 's'}
          {'· blacklisted' if bl else ''}{f' · “{E(q)}”' if q else ''}</h2>
        {(f"<a class=count href='/vendors{'' if bl else '?bl=1'}'>"
          f"{'← All suppliers' if bl else f'View blacklisted ({n_bl})'}</a>") if admin else ""}</div>
      {near}
      <div class=grid>{cards or "<p class=empty>No suppliers matched.</p>"}</div>""",
                active="suppliers")


def view_vendor(con, vid, msg=""):
    v = con.execute("SELECT * FROM vendor WHERE id=?", (vid,)).fetchone()
    if not v:
        return None
    avg, n = vendor_rating(con, vid)
    offers = con.execute("""
        SELECT o.*, i.id iid, i.name iname, i.category,
          (SELECT COUNT(DISTINCT vendor_id) FROM offer o2
             WHERE o2.ingredient_id=o.ingredient_id AND o2.vendor_id!=o.vendor_id) others,
          (SELECT MIN(price_min) FROM offer o3 WHERE o3.ingredient_id=o.ingredient_id) mlo,
          (SELECT MAX(price_max) FROM offer o4 WHERE o4.ingredient_id=o.ingredient_id) mhi
        FROM offer o JOIN ingredient i ON i.id=o.ingredient_id
        WHERE o.vendor_id=? ORDER BY i.name""", (vid,)).fetchall()
    reviews = con.execute(
        "SELECT * FROM rating WHERE vendor_id=? ORDER BY id DESC", (vid,)).fetchall()
    owner = can_edit_vendor(con, vid)           # admin, or the supplier who owns this listing
    admin_only = is_admin() or not gate_active(con)
    own_supplier = supplier_vid() == vid

    def item_row(o):
        others = (f"<a href='/ingredient/{o['iid']}'>{o['others']} other supplier"
                  f"{'' if o['others'] == 1 else 's'}</a>" if o["others"] else "<span class=metaline>only here</span>")
        market = f"₹{o['mlo']:,.0f}–{o['mhi']:,.0f}" if o["mlo"] else "—"
        rem = (f"<td><form method=post action='/admin/offer/del' style='margin:0'>"
               f"<input type=hidden name=id value='{o['id']}'>"
               f"<input type=hidden name=vendor_id value='{vid}'>"
               f"<button class=xbtn>Remove</button></form></td>" if owner else "")
        return (f"<tr><td><a href='/ingredient/{o['iid']}'><b>{E(o['iname'])}</b></a>"
                f"<div class=metaline>{E(o['category'])}</div></td>"
                f"<td><span class=price style='font-size:14px'>₹{o['price_min']:,.0f}–{o['price_max']:,.0f}</span>"
                f"<div class=metaline>/{E(o['unit'])}</div></td>"
                f"<td>{others}</td><td class=metaline>{market}</td>{rem}</tr>")
    items = "".join(item_row(o) for o in offers)

    revs = "".join(f"""<div class=review>
        <b>{E(r['rater'])}</b> <span class=tag>{E(r['rater_type'] or 'Client')}</span>
        {stars(r['score'])}
        <div class=metaline style='margin-top:6px'>{E(r['note'] or '')}</div>
        <div class=count style='margin-top:4px'>{E(r['created'] or '')}</div>
        </div>""" for r in reviews) or "<p class=empty>No reviews yet.</p>"

    def contact(label, val, href=None):
        if not val:
            return ""
        inner = f"<a href='{href}'>{E(val)}</a>" if href else E(val)
        return f"<div class=cline><span class=cl>{label}</span><span class=cv>{inner}</span></div>"

    contacts = "".join([
        contact("Contact", v["poc"] if "poc" in v.keys() else ""),
        contact("Phone", v["phone"] if "phone" in v.keys() else "",
                f"tel:{v['phone']}" if ("phone" in v.keys() and v["phone"]) else None),
        contact("Email", (v["email"] if "email" in v.keys() else "" or "").split(",")[0],
                f"mailto:{(v['email'] if 'email' in v.keys() else '' or '').split(',')[0]}"
                if ("email" in v.keys() and v["email"]) else None),
        contact("GSTIN", v["gst"]),
        contact("Address", v["address"] if "address" in v.keys() else ""),
        contact("State", (f"{v['state']}" + (f" · {v['pincode']}" if ('pincode' in v.keys() and v['pincode']) else ""))
                if ("state" in v.keys() and v["state"]) else ""),
    ]) or "<div class=metaline>No contact details on file.</div>"

    _id = current()
    _pf = (con.execute("SELECT name,company FROM profile WHERE code=?", (_id["code"],)).fetchone()
           if _id else None)
    rater_as = (_pf["name"] if _pf and _pf["name"] else (_id["note"] if _id else "You")) + \
               (f" · {_pf['company']}" if _pf and _pf["company"] else "")

    vk = lambda k: (v[k] if k in v.keys() and v[k] else "")
    edit_form = (f"""
      <details class='panel pad addsup' style='margin:0 0 8px'>
        <summary>Edit supplier information</summary>
        <form method=post action='/admin/vendor/edit' style='margin-top:14px'>
          <input type=hidden name=id value='{vid}'>
          <div class=vform>
            <label>Company name<input name=name required maxlength=140 value='{E(v['name'])}'></label>
            <label>Type<select name=kind>{"".join(f"<option{' selected' if v['kind'] == k else ''}>{k}</option>" for k in VENDOR_KINDS)}</select></label>
            <label>Contact person<input name=poc maxlength=80 value='{E(vk('poc'))}'></label>
            <label>Phone<input name=phone maxlength=40 value='{E(vk('phone'))}'></label>
            <label>Email<input name=email maxlength=140 value='{E(vk('email'))}'></label>
            <label>GSTIN<input name=gst maxlength=15 value='{E(vk('gst'))}'></label>
            <label>State<input name=state maxlength=60 value='{E(vk('state'))}'></label>
            <label>Pincode<input name=pincode maxlength=10 value='{E(vk('pincode'))}'></label>
            <label class=full>Address<input name=address maxlength=200 value='{E(vk('address'))}'></label>
          </div>
          <button style='margin-top:14px'>Save changes</button></form>
      </details>""" if owner else "")
    add_ing = (f"""
      <details class='panel pad addsup' style='margin:0 0 8px'{' open' if own_supplier and not offers else ''}>
        <summary>+ Add an ingredient {'you offer' if own_supplier else 'this supplier offers'}</summary>
        <form method=post action='/admin/offer' class=filters style='margin-top:14px'>
          <input type=hidden name=vendor_id value='{vid}'>
          <input name=ingredient required maxlength=140 placeholder='Ingredient name' style='flex:1'>
          <input name=rate required inputmode=decimal placeholder='Rate ₹/kg' style='width:130px'>
          <button>Add ingredient</button></form>
        <div class=metaline style='margin-top:8px'>Buyers see a price band, never your exact rate.</div>
      </details>""" if owner else "")
    sup_login = ""
    if admin_only:
        siv = con.execute("SELECT code FROM invite WHERE vendor_id=? AND revoked=0 LIMIT 1",
                          (vid,)).fetchone()
        if siv:
            sup_login = (f"<div class='panel pad' style='margin:0 0 8px'>"
                         f"<div class=ph><h3>Supplier login</h3></div>"
                         f"<div class=metaline style='margin:8px 0'>This supplier can manage their own "
                         f"listing at:</div><code class=inv>/login?code={E(siv['code'])}</code></div>")
        else:
            sup_login = (f"<form method=post action='/admin/supplier_invite' class='panel pad' style='margin:0 0 8px'>"
                         f"<input type=hidden name=vendor_id value='{vid}'>"
                         f"<div class=ph><h3>Supplier login</h3></div>"
                         f"<div class=metaline style='margin:8px 0 12px'>Let this supplier log in and "
                         f"manage their own catalogue &amp; see competitors.</div>"
                         f"<button>Create supplier login</button></form>")
    hdr = ("Your listing" if own_supplier else E(v["name"]))
    sub = ("<div class=sub>Manage your catalogue and see how you compare to other suppliers.</div>"
           if own_supplier else "")
    bl = v["blacklisted"] if "blacklisted" in v.keys() else 0
    bl_banner = ("<div class=blbanner>⛔ Blacklisted — hidden from buyers across the app.</div>"
                 if bl else "")
    bl_btn = (f"<form method=post action='/admin/vendor/blacklist' style='margin:0 0 8px'>"
              f"<input type=hidden name=id value='{vid}'>"
              f"<input type=hidden name=on value='{0 if bl else 1}'>"
              f"<button class='{'wbtn' if bl else 'xbtn'}'>"
              f"{'Remove from blacklist' if bl else '⛔ Blacklist this supplier'}</button></form>"
              if admin_only else "")

    return page(con, v["name"], f"""
      {"" if own_supplier else "<a class=back href='/vendors'>← Suppliers</a>"}
      <div class=hi><h1>{hdr}</h1>{sub}</div>
      {bl_banner}
      <p style='margin-bottom:16px'><span class='tag kind {E(v['kind'])}'>{E(v['kind'])}</span>
         <span class=metaline>{E(v['city'])}, {E(v['country'])}</span></p>
      {bl_btn}
      {sup_login}
      {edit_form}
      <div class=card style='display:flex;align-items:center;gap:14px'>
        <span style='font-size:26px'>{stars(avg)}</span>
        <span class=count>from {n} client / manufacturer review(s)</span></div>
      <h2>Contact & registration</h2>
      <div class=card>{contacts}</div>
      <h2>{len(offers)} ingredient{'' if len(offers) == 1 else 's'} offered</h2>
      {add_ing}
      {(f'''<div class=tablewrap><table>
        <thead><tr><th>Ingredient</th><th>{'Your price' if own_supplier else 'This supplier'}</th><th>Other suppliers</th><th>Market range</th>{'<th></th>' if owner else ''}</tr></thead>
        <tbody>{items}</tbody></table></div>''') if offers else
        f"<div class='panel pad'><p class=empty>{'You haven’t' if own_supplier else 'This supplier hasn’t'} listed any ingredients yet."
        f"{' Add your first above.' if owner else ''}</p></div>"}
      <h2>Rate this vendor</h2>
      <div class=card>
        {f"<p class=down style='margin-top:0'>{E(msg)}</p>" if msg else ""}
        {(
          f"<div class=metaline style='margin-top:0;margin-bottom:12px'>"
          f"Posting as <b style='color:var(--ink)'>{E(rater_as)}</b> — verified from your account.</div>"
          f"<form class=filters method=post action='/rate'>"
          f"<input type=hidden name=vendor_id value='{vid}'>"
          f"<select name=score aria-label='Star rating'>"
          + "".join(f"<option value={s}>{'★' * s}{'☆' * (5 - s)}  ({s})</option>" for s in (5, 4, 3, 2, 1))
          + "</select>"
          f"<input name=note placeholder='Remarks — quality, docs, lead time…' maxlength=500 style='flex:1'>"
          f"<button>Submit rating</button></form>"
         ) if can_review(con) else
         "<p class=metaline style='margin:0'>Reviews are for buyers — contract manufacturers, "
         "brands and distributors purchasing from suppliers. Your account isn't set up to review.</p>"}
      </div>
      <h2>Reviews</h2>{revs}""", active="suppliers")


def post_rate(con, body):
    f = urllib.parse.parse_qs(body)
    try:
        vid = int(f.get("vendor_id", ["0"])[0])
        score = int(f.get("score", ["0"])[0])
    except ValueError:
        return None, "Bad rating input."
    note = f.get("note", [""])[0].strip()[:500]
    if not can_review(con):
        return vid or None, "Only buyers (contract manufacturers, brands, distributors) can review."
    if not (1 <= score <= 5):
        return vid or None, "Pick a star rating (1-5)."
    if not con.execute("SELECT 1 FROM vendor WHERE id=?", (vid,)).fetchone():
        return None, "Unknown vendor."
    # rater identity is taken from the signed-in account — never user input
    ident = current()
    prof = (con.execute("SELECT name,company FROM profile WHERE code=?", (ident["code"],)).fetchone()
            if ident else None)
    rater = (prof["name"] if prof and prof["name"] else (ident["note"] if ident else "Anonymous"))[:120]
    rtype = (prof["company"] if prof and prof["company"] else "Ingrex user")[:120]
    con.execute("INSERT INTO rating (vendor_id,rater,rater_type,score,note,created)"
                " VALUES (?,?,?,?,?,?)",
                (vid, rater, rtype, score, note, date.today().isoformat()))
    con.commit()
    return vid, "Thanks — rating recorded."


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


# Profile mirrored into a signed per-device cookie so onboarding survives the
# free-tier's DB resets — the user never fills the form twice on the same device.
PROF_COOKIE = "prof"


def _psig(payload):
    return hmac.new(b"prof-" + AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]


def prof_cookie(d):
    payload = urllib.parse.urlencode({k: d.get(k, "") for k in
                                      ("name", "company", "role", "gst", "city")})
    val = urllib.parse.quote(payload) + "." + _psig(payload)
    return f"{PROF_COOKIE}={val}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax; Secure"


def read_prof(headers):
    enc, _, sig = _cookie(headers, PROF_COOKIE).rpartition(".")
    if not enc:
        return None
    payload = urllib.parse.unquote(enc)
    if hmac.compare_digest(sig, _psig(payload)):
        d = dict(urllib.parse.parse_qsl(payload))
        if d.get("name") and d.get("company"):
            return d
    return None


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


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")


def signup_account(con, name, email, company, verified=False):
    """Find-or-create a buyer account for an email. Returns its invite code.

    The email is a label; the credential is the signed cookie carrying the code
    below. Self-serve email signup is unverified — someone can type an address
    they don't own, but they gain only an empty trial account, not access to the
    real owner's. A Google sign-in (verified=True) is trusted to own the address.
    """
    email = email.strip().lower()[:140]
    row = con.execute("SELECT code FROM profile WHERE email=?", (email,)).fetchone()
    if row:
        live = con.execute("SELECT 1 FROM invite WHERE code=? AND revoked=0",
                           (row["code"],)).fetchone()
        if live:
            return row["code"]
    code = secrets.token_urlsafe(9)
    today = date.today().isoformat()
    con.execute("INSERT INTO invite(code,note,created) VALUES(?,?,?)", (code, name[:80], today))
    # completed=0 sends them through /welcome to finish business type, GST and city
    con.execute("INSERT INTO profile(code,name,company,role,gst,city,completed,created,email) "
                "VALUES(?,?,?,'','','',0,?,?)", (code, name[:80], company[:120], today, email))
    con.commit()
    account_changed()
    return code


def oauth_state():
    """Signed, self-expiring CSRF token for the Google round trip."""
    stamp = str(int(time.time()))
    return f"{stamp}.{hmac.new(AUTH_SECRET, stamp.encode(), hashlib.sha256).hexdigest()[:32]}"


def oauth_state_ok(state):
    stamp, _, sig = (state or "").partition(".")
    if not stamp.isdigit():
        return False
    want = hmac.new(AUTH_SECRET, stamp.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want) and (time.time() - int(stamp)) < 600


def google_identity(code, redirect_uri):
    """Swap an auth code for the signed-in user's profile. Returns (email, name) or None.

    The id_token comes straight back from Google's token endpoint over TLS, so its
    payload is trusted without a local signature check — that is the documented
    server-side code-flow shortcut, and stdlib has no RSA verify anyway.
    """
    import base64
    body = urllib.parse.urlencode({
        "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            tok = json.loads(r.read())
        payload = tok["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None
    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified"):
        return None
    return email, (claims.get("name") or email.split("@")[0])[:80]


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


def supplier_vid():
    """Vendor id this account manages, if it's a supplier login (else None)."""
    ident = current()
    return ident["vendor_id"] if ident and "vendor_id" in ident.keys() and ident["vendor_id"] else None


def is_supplier():
    return supplier_vid() is not None


def is_master(con):
    """Master admin — or open dev mode, where no invite gate is configured."""
    return is_admin() or not gate_active(con)


def can_edit_vendor(con, vid):
    return is_admin() or not gate_active(con) or supplier_vid() == vid


def profile_done(con, code):
    r = con.execute("SELECT completed FROM profile WHERE code=?", (code,)).fetchone()
    return bool(r and r["completed"])


def account(con):
    """Profile row for the signed-in account, or None (admin / supplier / open dev).

    Memoised per request: the trial banner and the avatar menu both need it on
    every single page, and each miss is a round trip to a remote database."""
    ident = current()
    if not ident:
        return None
    hit = getattr(CTX, "acct", None)
    if hit is not None and hit[0] == ident["code"]:
        return hit[1]
    row = con.execute("SELECT * FROM profile WHERE code=?", (ident["code"],)).fetchone()
    CTX.acct = (ident["code"], row)
    return row


def account_changed():
    """Drop the per-request profile memo after any write to it."""
    CTX.acct = None


def _days_since(iso):
    try:
        return (date.today() - date.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return 0


def subscription(con):
    """(plan_key, cycle, trial_days_left). plan_key '' means still on the free trial."""
    p = account(con)
    if not p:
        return ("", "", None)
    plan = (p["plan"] or "") if "plan" in p.keys() else ""
    cycle = (p["cycle"] or "") if "cycle" in p.keys() else ""
    left = max(0, TRIAL_DAYS - _days_since(p["created"]))
    return (plan, cycle, left)


def plan_name(key):
    return next((n for k, n, *_ in PLANS if k == key), "")


def current_role(con):
    ident = current()
    if not ident:
        return None
    r = con.execute("SELECT role FROM profile WHERE code=?", (ident["code"],)).fetchone()
    return r["role"] if r else None


def can_review(con):
    """Reviews are for buyers (contract manufacturers, brands, distributors). Admin always."""
    if not gate_active(con) or is_admin():   # open dev mode, or admin
        return True
    return current_role(con) in BUYER_ROLES


LOGIN_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,
  Helvetica,Arial,sans-serif;color:#0f1f1a;background:#04140f;
  letter-spacing:-.006em;-webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;font-variant-numeric:tabular-nums}
a{color:#0d7a56;text-decoration:none}a:hover{text-decoration:underline}
/* full-bleed moving mesh; the CSS gradient is the no-WebGL fallback */
.gradcanvas{position:fixed;inset:0;width:100%;height:100%;display:block;z-index:0;
  background:radial-gradient(90% 80% at 22% 18%,#2ce39f 0%,#0d7a56 34%,#052b1f 68%,#04140f 100%)}
.grain{position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.14;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.55'/%3E%3C/svg%3E")}
/* one card, centred on the mesh */
.stage{position:relative;z-index:2;min-height:100vh;min-height:100dvh;
  display:grid;place-items:center;padding:32px 20px}
.card{width:100%;max-width:412px;background:#fff;border-radius:22px;padding:38px 38px 32px;
  border:1px solid rgba(255,255,255,.6);
  box-shadow:0 40px 90px -30px rgba(2,12,8,.55),0 12px 30px -14px rgba(2,12,8,.35),
    0 1px 0 rgba(255,255,255,.7) inset}
.card .mark{display:flex;align-items:center;gap:9px;margin-bottom:22px}
.card .mark .mk{width:29px;height:29px;border-radius:8px;flex:none;display:grid;
  place-items:center;font-weight:800;font-size:14px;color:#fff;
  background:linear-gradient(140deg,#12b884,#0a5d41)}
.card .mark .wm{font-size:19px;font-weight:800;letter-spacing:-.035em;color:#0f1f1a}
.card .mark .wm span{color:#0d7a56}
.card h1{font-size:23px;letter-spacing:-.028em;margin-bottom:6px;font-weight:700}
.card .lead{font-size:13.5px;line-height:1.6;color:#6b7d75;margin-bottom:24px}
.gbtn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
  padding:12px;font-size:14px;font-weight:600;color:#1f2b26;background:#fff;
  border:1px solid #dfe7e2;border-radius:11px;cursor:pointer;text-decoration:none;
  transition:background .16s,box-shadow .16s,border-color .16s}
.gbtn:hover{background:#f7faf8;text-decoration:none;border-color:#cfdcd5;
  box-shadow:0 3px 12px -5px rgba(15,31,26,.25)}
.or{display:flex;align-items:center;gap:12px;margin:19px 0;
  font-size:10.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#a3b0a9}
.or::before,.or::after{content:"";flex:1;height:1px;background:#e6ece8}
form{display:flex;flex-direction:column;gap:13px}
label{font-size:11.5px;font-weight:700;color:#42544c;margin-bottom:6px;display:block}
input{width:100%;padding:12px 14px;font-size:14.5px;color:#0f1f1a;background:#fbfcfb;
  border:1px solid #dfe7e2;border-radius:11px;outline:0;
  transition:border-color .16s,box-shadow .16s,background .16s}
input::placeholder{color:#a3b0a9}
input:focus{border-color:#0d7a56;background:#fff;box-shadow:0 0 0 3px #e7f4ee}
button.go{padding:13px;font-size:14.5px;font-weight:700;color:#fff;cursor:pointer;border:0;
  border-radius:11px;background:linear-gradient(180deg,#0f8a61,#0a5d41);
  box-shadow:0 8px 20px -10px rgba(13,122,86,.8),0 1px 0 rgba(255,255,255,.14) inset;
  transition:filter .16s,transform .08s,box-shadow .16s}
button.go:hover{filter:brightness(1.06)}
button.go:active{transform:translateY(1px)}
.err{margin-bottom:16px;padding:11px 13px;font-size:13px;font-weight:650;color:#b4541c;
  background:#fbe9df;border:1px solid #e6c3ad;border-radius:11px}
.note{margin-top:20px;font-size:12.5px;color:#6b7d75;text-align:center}
.fine{margin-top:22px;font-size:11.5px;line-height:1.55;color:#a3b0a9;text-align:center}
.trialpin{display:inline-flex;align-items:center;gap:7px;margin-bottom:16px;padding:6px 12px;
  font-size:10.5px;font-weight:750;letter-spacing:.05em;text-transform:uppercase;
  color:#0a5d41;background:#eaf3ee;border:1px solid #d7e8df;border-radius:20px}
/* trust line sits on the mesh, under the card */
.below{margin-top:20px;display:flex;justify-content:center;gap:8px;flex-wrap:wrap}
.below span{font-size:11.5px;font-weight:600;color:rgba(255,255,255,.82);
  padding:5px 11px;border-radius:20px;backdrop-filter:blur(10px);
  background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18)}
:focus-visible{outline:2px solid #0d7a56;outline-offset:2px}
@media(max-width:520px){
  .card{padding:30px 24px 26px;border-radius:18px}
  .below{display:none}
}
"""


SHADER_JS = """<script>
(function(){
var c=document.getElementById('grad');if(!c)return;
var gl=c.getContext('webgl',{antialias:false,alpha:false,powerPreference:'low-power'})
     ||c.getContext('experimental-webgl');
if(!gl)return;                       // CSS gradient underneath stays visible
var VS='attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
var FS=[
'precision highp float;',
'uniform vec2 u_res;uniform float u_t;',
'vec2 hash(vec2 p){p=vec2(dot(p,vec2(127.1,311.7)),dot(p,vec2(269.5,183.3)));',
'return -1.+2.*fract(sin(p)*43758.5453123);}',
'float noise(vec2 p){const float K1=0.366025404,K2=0.211324865;',
'vec2 i=floor(p+(p.x+p.y)*K1);vec2 a=p-i+(i.x+i.y)*K2;',
'float m=step(a.y,a.x);vec2 o=vec2(m,1.-m);vec2 b=a-o+K2;vec2 c2=a-1.+2.*K2;',
'vec3 h=max(0.5-vec3(dot(a,a),dot(b,b),dot(c2,c2)),0.0);',
'vec3 n=h*h*h*h*vec3(dot(a,hash(i)),dot(b,hash(i+o)),dot(c2,hash(i+1.)));',
'return dot(n,vec3(70.));}',
// a mesh gradient, not fbm terrain: four orbiting colour wells, softly blended.
// stacked domain-warped fbm looks like marble veining at this scale, not a gradient.
'float well(vec2 uv,vec2 c,float r){return exp(-dot(uv-c,uv-c)/(r*r));}',
'void main(){',
' vec2 uv=gl_FragCoord.xy/u_res.xy;',
' float ar=u_res.x/u_res.y;',
' vec2 p=vec2(uv.x*ar,uv.y);',
' float t=u_t*0.12;',
// one gentle warp for organic edges — amplitude stays low so masses stay smooth
' p+=0.055*vec2(noise(p*1.5+vec2(0.0,t*0.6)),noise(p*1.5+vec2(4.7,-t*0.5)));',
' vec3 deep=vec3(0.008,0.055,0.043);',
' vec3 pine=vec3(0.031,0.286,0.204);',
' vec3 emer=vec3(0.055,0.502,0.353);',
' vec3 mint=vec3(0.376,0.937,0.718);',
// spread across the full viewport width, so a wide screen isn't one flat corner
' vec2 c1=vec2(0.22*ar+0.18*ar*sin(t*0.70),0.78+0.12*cos(t*0.53));',
' vec2 c2=vec2(0.84*ar+0.16*ar*cos(t*0.44),0.24+0.15*sin(t*0.61));',
' vec2 c3=vec2(0.55*ar+0.22*ar*sin(t*0.35+2.1),0.58+0.18*cos(t*0.40+1.2));',
' vec2 c4=vec2(0.12*ar+0.14*ar*cos(t*0.58+0.7),0.18+0.11*sin(t*0.47+2.6));',
' vec2 c5=vec2(0.95*ar+0.15*ar*sin(t*0.39+4.0),0.86+0.10*cos(t*0.50+0.4));',
' float rr=0.34+0.16*clamp(ar-1.0,0.0,1.6);',
' float w1=well(p,c1,rr),w2=well(p,c2,rr*1.18);',
' float w3=well(p,c3,rr*0.92),w4=well(p,c4,rr),w5=well(p,c5,rr*0.86);',
' float s=w1+w2+w3+w4+w5+0.16;',
' vec3 col=(mint*w1+pine*w2+emer*w3+mint*0.45*w4+emer*0.8*w5+deep*0.16)/s;',
// lift the brightest well so there is a light source, not a flat wash
' col+=mint*smoothstep(0.45,1.0,w1/(s*0.7))*0.28;',
' col=mix(col,deep,smoothstep(0.42,1.30,length((uv-vec2(0.34,0.70))*vec2(0.9,0.85))));',
' col+=(fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233)))*43758.5453)-0.5)*0.014;',
' gl_FragColor=vec4(col,1.0);}'
].join('\\n');
function sh(t,src){var s=gl.createShader(t);gl.shaderSource(s,src);gl.compileShader(s);
return gl.getShaderParameter(s,gl.COMPILE_STATUS)?s:null;}
var vs=sh(gl.VERTEX_SHADER,VS),fs=sh(gl.FRAGMENT_SHADER,FS);
if(!vs||!fs)return;
var pr=gl.createProgram();gl.attachShader(pr,vs);gl.attachShader(pr,fs);gl.linkProgram(pr);
if(!gl.getProgramParameter(pr,gl.LINK_STATUS))return;
gl.useProgram(pr);
var buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
var loc=gl.getAttribLocation(pr,'p');gl.enableVertexAttribArray(loc);
gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
var uR=gl.getUniformLocation(pr,'u_res'),uT=gl.getUniformLocation(pr,'u_t');
function size(){
  var d=Math.min(window.devicePixelRatio||1,1.5);   // half-res on retina: this is a backdrop
  var w=Math.max(1,Math.round(c.clientWidth*d)),h=Math.max(1,Math.round(c.clientHeight*d));
  if(c.width!==w||c.height!==h){c.width=w;c.height=h;gl.viewport(0,0,w,h);}
  gl.uniform2f(uR,c.width,c.height);
}
var still=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
var t0=Date.now(),raf=0;
function frame(){
  size();
  gl.uniform1f(uT,still?18.0:(Date.now()-t0)/1000);
  gl.drawArrays(gl.TRIANGLES,0,3);
  if(!still&&!document.hidden)raf=requestAnimationFrame(frame);else raf=0;
}
frame();
// stop burning GPU on a backgrounded tab
document.addEventListener('visibilitychange',function(){
  if(!document.hidden&&!still&&!raf)raf=requestAnimationFrame(frame);});
window.addEventListener('resize',function(){if(still||!raf)frame();});
})();
</script>"""

GOOGLE_G = ("<svg width=17 height=17 viewBox='0 0 48 48' aria-hidden=true>"
            "<path fill='#4285F4' d='M45.1 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h11.8c-.5 2.7-2 5-4.4 6.6v5.5h7.1c4.1-3.8 6.6-9.4 6.6-16.1z'/>"
            "<path fill='#34A853' d='M24 46c5.9 0 10.9-2 14.5-5.4l-7.1-5.5c-2 1.3-4.5 2.1-7.4 2.1-5.7 0-10.5-3.8-12.2-9H4.5v5.7C8.1 41.1 15.4 46 24 46z'/>"
            "<path fill='#FBBC05' d='M11.8 28.2c-.4-1.3-.7-2.7-.7-4.2s.3-2.9.7-4.2v-5.7H4.5C3 17 2 20.4 2 24s1 7 2.5 9.9l7.3-5.7z'/>"
            "<path fill='#EA4335' d='M24 10.8c3.2 0 6.1 1.1 8.4 3.3l6.3-6.3C34.9 4.2 29.9 2 24 2 15.4 2 8.1 6.9 4.5 14.1l7.3 5.7c1.7-5.2 6.5-9 12.2-9z'/></svg>")


def login_page(err="", prefill="", mode="signin", d=None):
    """Split screen: the film on the left, sign-in / sign-up on white at the right."""
    d = d or {}
    v = lambda k: E(d.get(k, ""))
    google = (f"<a class=gbtn href='/auth/google'>{GOOGLE_G}"
              f"Continue with Google</a><div class=or>or</div>") if GOOGLE_ON else ""
    if mode == "signup":
        head = ("<span class=trialpin>✦ First month free</span>"
                "<h1>Create your account</h1>"
                "<p class=lead>Full access to the catalogue, vendor price bands and sourcing "
                "requests for 30 days. No card needed.</p>")
        form = (f"<form method=post action='/signup'>"
                f"<div><label>Full name</label>"
                f"<input name=name value='{v('name')}' required maxlength=80 "
                f"placeholder='e.g. Karan Sharma' autofocus></div>"
                f"<div><label>Work email</label>"
                f"<input name=email type=email value='{v('email')}' required maxlength=140 "
                f"placeholder='you@company.com' autocomplete=email></div>"
                f"<div><label>Company</label>"
                f"<input name=company value='{v('company')}' required maxlength=120 "
                f"placeholder='e.g. Nutraform Labs'></div>"
                f"<button class=go>Start free month</button></form>"
                f"<div class=note>Already have access? <a href='/login'>Sign in</a></div>")
    else:
        head = ("<h1>Sign in</h1>"
                "<p class=lead>Use your Google account, or the invite code from your "
                "Ingrex contact.</p>")
        form = (f"<form method=post action='/login'>"
                f"<div><label>Invite code</label>"
                f"<input name=code value='{E(prefill)}' required maxlength=64 "
                f"placeholder='Invite code' autocomplete=off spellcheck=false></div>"
                f"<button class=go>Enter portal</button></form>"
                f"<div class=note>New to Ingrex? <a href='/signup'>Start a free month</a></div>")
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{'Create account' if mode == 'signup' else 'Sign in'} · Ingrex</title>"
            f"<style>{LOGIN_CSS}</style>"
            f"<canvas id=grad class=gradcanvas aria-hidden=true></canvas>"
            f"<div class=grain aria-hidden=true></div>"
            f"<div class=stage><div>"
            f"<div class=card>"
            f"<div class=mark><span class=mk>i</span>"
            f"<span class=wm>ingre<span>x</span></span></div>"
            f"{head}"
            f"{f'<div class=err>{E(err)}</div>' if err else ''}"
            f"{google}{form}"
            f"<div class=fine>By continuing you agree to Ingrex's terms and privacy policy. "
            f"Prices shown are indicative market bands, not live quotes.</div>"
            f"</div>"
            f"<div class=below><span>67 ingredients</span>"
            f"<span>48 verified suppliers</span><span>12-month price history</span></div>"
            f"</div></div>{SHADER_JS}</html>").encode()


WELCOME_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;
  display:grid;place-items:center;padding:24px;color:#22332c;
  background:radial-gradient(120% 120% at 20% 0%,#e7f4ee,#f4f7f5 60%)}
.wz{width:100%;max-width:460px;background:#fff;border:1px solid #e6ece8;border-radius:20px;
  box-shadow:0 30px 70px -30px rgba(15,31,26,.35);padding:30px 30px 26px}
.wz .brand{font-size:24px;font-weight:800;letter-spacing:-.03em;color:#0f1f1a}
.wz .brand span{color:#0d7a56}
.wz .sub{color:#6b7d75;font-size:14px;margin:4px 0 20px}
.bar{height:6px;background:#e6ece8;border-radius:20px;overflow:hidden;margin-bottom:6px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#12b884,#0d7a56);
  width:33%;transition:width .3s ease}
.stepno{font-size:12px;font-weight:700;color:#6b7d75;margin-bottom:18px}
.step{display:none;flex-direction:column;gap:14px}
.step.active{display:flex}
.step h3{font-size:17px;color:#0f1f1a}
label{font-size:12px;font-weight:700;color:#42544c;display:block;margin-bottom:5px}
.f input,.f select{width:100%;padding:12px 13px;font-size:14px;border:1px solid #dfe7e2;
  border-radius:10px;background:#fff;color:#0f1f1a;outline:0}
.f input:focus,.f select:focus{border-color:#0d7a56;box-shadow:0 0 0 3px #e7f4ee}
.err{background:#fbe9df;border:1px solid #e6c3ad;color:#b4541c;font-size:13px;font-weight:600;
  padding:10px 12px;border-radius:10px;margin-bottom:14px}
.row{display:flex;gap:20px;margin-top:20px}
button{flex:1;padding:13px;font-size:14px;font-weight:700;border:0;border-radius:11px;cursor:pointer}
.next{background:#0d7a56;color:#fff}.next:hover{background:#0a5d41}
.back{background:#fff;border:1px solid #dfe7e2;color:#42544c}.back:hover{background:#f4f7f5}
.back[hidden]{display:none}
"""


def view_welcome(con, code, err="", d=None):
    d = d or {}
    roles = "".join(f"<option{' selected' if d.get('role') == r else ''}>{E(r)}</option>"
                    for r in BUSINESS_ROLES)
    v = lambda k: E(d.get(k, ""))
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Welcome · Ingrex</title><style>{WELCOME_CSS}</style>"
            f"<form class=wz method=post action='/welcome'>"
            f"<div class=brand>ingre<span>x</span></div>"
            f"<div class=sub>Let's set up your account — takes under a minute.</div>"
            f"<div class=bar><i id=fill></i></div>"
            f"<div class=stepno id=stepno>Step 1 of 3</div>"
            f"{f'<div class=err>{E(err)}</div>' if err else ''}"
            f"<div class='step active'><h3>About you</h3>"
            f"<div class=f><label>Full name</label>"
            f"<input name=name value='{v('name')}' required maxlength=80 placeholder='e.g. Karan Sharma'></div>"
            f"<div class=f><label>Company / organisation</label>"
            f"<input name=company value='{v('company')}' required maxlength=120 placeholder='e.g. Sapiens Labs'></div></div>"
            f"<div class=step><h3>Your business</h3>"
            f"<div class=f><label>Business type</label>"
            f"<select name=role required><option value=''>Select…</option>{roles}</select></div>"
            f"<div class=f><label>GSTIN</label>"
            f"<input name=gst value='{v('gst')}' required maxlength=15 minlength=15 "
            f"placeholder='15-character GST number' style='text-transform:uppercase'></div></div>"
            f"<div class=step><h3>Location</h3>"
            f"<div class=f><label>City</label>"
            f"<input name=city value='{v('city')}' required maxlength=60 placeholder='e.g. Hyderabad'></div>"
            f"<div class=f><label>Country</label>"
            f"<input name=country value='{v('country') or 'India'}' maxlength=60></div></div>"
            f"<div class=row>"
            f"<button type=button class=back id=back hidden>Back</button>"
            f"<button type=button class=next id=next>Continue</button></div>"
            f"</form>{WELCOME_JS}</html>").encode()


WELCOME_JS = """<script>
(function(){var steps=[].slice.call(document.querySelectorAll('.step')),i=0,n=steps.length;
var fill=document.getElementById('fill'),lbl=document.getElementById('stepno'),
back=document.getElementById('back'),next=document.getElementById('next'),
form=document.querySelector('.wz');
function render(){steps.forEach(function(s,k){s.classList.toggle('active',k===i);});
fill.style.width=((i+1)/n*100)+'%';lbl.textContent='Step '+(i+1)+' of '+n;
back.hidden=i===0;next.textContent=i===n-1?'Finish':'Continue';}
function valid(){var ok=true;steps[i].querySelectorAll('input,select').forEach(function(el){
if(!el.checkValidity()){el.reportValidity();ok=false;}});return ok;}
next.addEventListener('click',function(){if(!valid())return;if(i<n-1){i++;render();}else{form.submit();}});
back.addEventListener('click',function(){if(i>0){i--;render();}});
render();})();
</script>"""


# ---------- server ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ingrex/0.1"
    # HTTP/1.1 keeps the TCP+TLS connection open across requests. On HTTP/1.0 the
    # browser re-handshakes for every page, stylesheet and /suggest call, which
    # costs a round trip each behind Render's TLS terminator. Every response below
    # must send a Content-Length (or the client waits for EOF that never comes).
    protocol_version = "HTTP/1.1"
    # ...and an idle keep-alive socket otherwise pins a thread forever. A browser
    # opens ~6 connections per tab, so without this they pile up on a small dyno.
    timeout = 15

    def _send(self, body, code=200, ctype="text/html; charset=utf-8", cache="", extra=()):
        # pages are 25-50KB of repetitive markup; gzip cuts that ~7x on the wire
        gz = len(body) > 900 and "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            body = gzip.compress(body, 1)   # level 1: ~same ratio on HTML, far less CPU
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        if cache:
            self.send_header("Cache-Control", cache)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, target, cookie=None):
        self.send_response(303)
        self.send_header("Location", target)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")   # keep-alive needs an explicit length
        self.end_headers()

    def do_HEAD(self):        # health checks / port scans (Render probes with HEAD)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _oauth_redirect(self):
        """Callback URL for this deployment — must match what's registered with Google."""
        proto = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() or "http"
        host = self.headers.get("Host", "localhost")
        return f"{proto}://{host}/auth/google/callback"

    def _client_ip(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        if url.path == "/app.css":
            return self._send(CSS.encode(), ctype="text/css; charset=utf-8",
                              cache="public, max-age=31536000, immutable")
        con = connect()
        try:
            gated = gate_active(con)
            ident = identity(con, self.headers) if gated else None
            CTX.ident = ident
            CTX.acct = None
            if url.path == "/login":
                return self._send(login_page(prefill=params.get("code", [""])[0][:64]))
            if url.path == "/signup":
                return self._send(login_page(mode="signup"))
            if url.path == "/auth/google":
                if not GOOGLE_ON:
                    return self._redirect("/login")
                q = urllib.parse.urlencode({
                    "client_id": GOOGLE_CLIENT_ID, "redirect_uri": self._oauth_redirect(),
                    "response_type": "code", "scope": "openid email profile",
                    "state": oauth_state(), "prompt": "select_account"})
                return self._redirect("https://accounts.google.com/o/oauth2/v2/auth?" + q)
            if url.path == "/auth/google/callback":
                if not (GOOGLE_ON and oauth_state_ok(params.get("state", [""])[0])):
                    return self._send(login_page("Sign-in link expired — try again."), 400)
                got = google_identity(params.get("code", [""])[0], self._oauth_redirect())
                if not got:
                    return self._send(login_page("Google sign-in failed. Try again."), 401)
                email, name = got
                code = signup_account(con, name, email, "", verified=True)
                return self._redirect("/", auth_cookie(code))
            if url.path == "/logout":
                return self._redirect("/login", f"{COOKIE}=; Max-Age=0; Path=/; "
                                      "HttpOnly; SameSite=Lax; Secure")
            if gated and not ident:
                code = params.get("code", [""])[0][:64]
                return self._redirect("/login" + (f"?code={urllib.parse.quote(code)}" if code else ""))
            touch_online(f"{ident['code'] if ident else 'anon'}|{self._client_ip()}",
                         ident["note"] if ident else "Guest", ident["code"] if ident else "",
                         self._client_ip(), is_admin())
            if url.path == "/export.csv":
                if not is_master(con):                  # master admin only for now
                    return self._redirect("/search")
                return self._send(export_csv(con, params), ctype="text/csv; charset=utf-8",
                                  extra=[("Content-Disposition",
                                          "attachment; filename=ingrex-ingredients.csv")])
            if url.path == "/suggest":
                body = json.dumps(suggest(con, params.get("q", [""])[0][:60])).encode()
                return self._send(body, ctype="application/json")
            # onboarding: invited (non-admin, non-supplier) buyers complete a profile first.
            # If a signed profile cookie survives a DB reset, restore it silently.
            if ident and not is_admin() and not is_supplier() and not profile_done(con, ident["code"]):
                saved = read_prof(self.headers)
                if saved:
                    old = con.execute("SELECT email, created FROM profile WHERE code=?",
                                      (ident["code"],)).fetchone()
                    con.execute(
                        "INSERT OR REPLACE INTO profile(code,name,company,role,gst,city,"
                        "completed,created,email) VALUES(?,?,?,?,?,?,1,?,?)",
                        (ident["code"], saved["name"], saved.get("company", ""),
                         saved.get("role", ""), saved.get("gst", ""), saved.get("city", ""),
                         (old["created"] if old else None) or date.today().isoformat(),
                         old["email"] if old else None))
                    con.execute("UPDATE invite SET note=? WHERE code=?",
                                (saved["name"], ident["code"]))
                    con.commit()
                    account_changed()
                elif url.path != "/welcome":
                    return self._redirect("/welcome")
                else:
                    return self._send(view_welcome(con, ident["code"]))
            if url.path == "/welcome":
                return self._redirect("/")   # already done, or admin/dev
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
                out = view_dashboard(con, wl, params.get("trend", [""])[0])
            elif url.path == "/search":
                out = view_search(con, params, wl, params.get("msg", [""])[0][:120])
            elif url.path == "/watchlist":
                out = view_watchlist(con, wl)
            elif url.path == "/vendors":
                out = view_vendors(con, params.get("q", [""])[0],
                                   params.get("bl", [""])[0] == "1")
            elif url.path == "/insights":
                out = view_insights(con)
            elif url.path == "/reviews":
                out = view_myreviews(con)
            elif url.path == "/requests":
                out = view_requests(con, params.get("ing", [""])[0][:140],
                                    params.get("msg", [""])[0][:80])
            elif url.path == "/account":
                out = view_account(con, params.get("msg", [""])[0][:120])
            elif url.path == "/plans":
                out = view_plans(con, params.get("msg", [""])[0][:120])
            elif url.path == "/admin":
                out = view_admin(con) if (is_admin() or not gated) else None
            elif m := re.fullmatch(r"/ingredient/(\d+)", url.path):
                out = view_ingredient(con, int(m[1]), wl, params.get("msg", [""])[0][:120])
            elif m := re.fullmatch(r"/vendor/(\d+)", url.path):
                out = view_vendor(con, int(m[1]), params.get("msg", [""])[0][:80])
            else:
                out = None
            self._send(out or page(con, "Not found", "<h1>404</h1><p><a href='/'>Home</a></p>"),
                       200 if out else 404)
        finally:
            con.close()
            CTX.ident = None
            CTX.acct = None

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = min(int(self.headers.get("Content-Length") or 0), 8192)
        body = self.rfile.read(n).decode("utf-8", "replace")
        con = connect()
        try:
            gated = gate_active(con)
            ident = identity(con, self.headers) if gated else None
            CTX.ident = ident
            CTX.acct = None
            if path == "/login":
                code = urllib.parse.parse_qs(body).get("code", [""])[0].strip()[:64]
                row = con.execute("SELECT * FROM invite WHERE code=? AND revoked=0",
                                  (code,)).fetchone()
                if row:
                    dest = ("/admin" if row["is_admin"] else
                            f"/vendor/{row['vendor_id']}" if ("vendor_id" in row.keys() and row["vendor_id"])
                            else "/")
                    return self._redirect(dest, auth_cookie(code))
                time.sleep(1)   # ponytail: crude brute-force damper on code guessing
                return self._send(login_page("Invalid or revoked invite code.", code), 401)
            if path == "/signup":
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                d = {"name": g("name")[:80], "email": g("email")[:140],
                     "company": g("company")[:120]}
                if not (d["name"] and d["company"] and EMAIL_RE.match(d["email"])):
                    return self._send(login_page("Enter your name, company and a valid "
                                                 "work email.", mode="signup", d=d), 400)
                code = signup_account(con, d["name"], d["email"], d["company"])
                return self._redirect("/welcome", auth_cookie(code))
            if gated and not ident:
                return self._redirect("/login")
            if path == "/welcome":
                if not ident:
                    return self._redirect("/")
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                d = {"name": g("name")[:80], "company": g("company")[:120], "role": g("role"),
                     "gst": g("gst").upper()[:15], "city": g("city")[:60],
                     "country": g("country")[:60] or "India"}
                if not (d["name"] and d["company"] and d["role"] in BUSINESS_ROLES
                        and len(d["gst"]) == 15 and d["city"]):
                    return self._send(view_welcome(
                        con, ident["code"], "Please complete every field (GSTIN is 15 characters).", d), 400)
                # keep the signup email / plan: a bare REPLACE would blank the row's
                # other columns for anyone who arrived via Google or self-serve signup
                old = con.execute("SELECT email, created FROM profile WHERE code=?",
                                  (ident["code"],)).fetchone()
                con.execute("INSERT OR REPLACE INTO profile"
                            "(code,name,company,role,gst,city,completed,created,email) "
                            "VALUES(?,?,?,?,?,?,1,?,?)",
                            (ident["code"], d["name"], d["company"], d["role"], d["gst"],
                             d["city"], (old["created"] if old else None) or date.today().isoformat(),
                             old["email"] if old else None))
                con.execute("UPDATE invite SET note=? WHERE code=?", (d["name"], ident["code"]))
                con.commit()
                account_changed()
                return self._redirect("/", prof_cookie(d))   # survives DB resets
            if path == "/requests":
                f = urllib.parse.parse_qs(body)
                ingredient = f.get("ingredient", [""])[0].strip()[:140]
                details = f.get("details", [""])[0].strip()[:800]
                if not ingredient:
                    return self._redirect("/requests")
                code, name, company = requester_identity(con)
                con.execute("INSERT INTO request(code,requester,company,ingredient,details,"
                            "status,created,updated) VALUES(?,?,?,?,?,'Open',?,?)",
                            (code, name, company, ingredient, details,
                             date.today().isoformat(), date.today().isoformat()))
                con.commit()
                return self._redirect(
                    "/requests?msg=" + urllib.parse.quote("Request submitted — the purchase team will update you here."))
            if path == "/request_note":
                f = urllib.parse.parse_qs(body)
                rid = f.get("id", ["0"])[0]
                note = f.get("note", [""])[0].strip()[:400]
                _, name, company = requester_identity(con)
                if rid.isdigit() and note and con.execute(
                        "SELECT 1 FROM request WHERE id=?", (rid,)).fetchone():
                    con.execute("INSERT INTO req_note(request_id,author,company,note,created) "
                                "VALUES(?,?,?,?,?)",
                                (int(rid), name, company, note, date.today().isoformat()))
                    con.commit()
                return self._redirect(f"/requests#r{rid}")
            if path == "/account":
                ident = current()
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                name, company = g("name")[:80], g("company")[:120]
                if ident and name and company:
                    role = g("role") if g("role") in BUSINESS_ROLES else ""
                    d = {"name": name, "company": company, "role": role,
                         "gst": g("gst").upper()[:15], "city": g("city")[:60]}
                    con.execute("UPDATE profile SET name=?,company=?,role=?,gst=?,city=?,phone=? "
                                "WHERE code=?",
                                (name, company, role, d["gst"], d["city"],
                                 g("phone")[:20], ident["code"]))
                    con.execute("UPDATE invite SET note=? WHERE code=?", (name, ident["code"]))
                    con.commit()
                    account_changed()
                    return self._redirect("/account?msg=" + urllib.parse.quote("Details saved."),
                                          prof_cookie(d))   # survives DB resets
                return self._redirect("/account")
            if path == "/plans":
                ident = current()
                f = urllib.parse.parse_qs(body)
                plan = f.get("plan", [""])[0]
                cycle = f.get("cycle", [""])[0]
                if ident and plan in [p[0] for p in PLANS] and cycle in ("monthly", "yearly"):
                    con.execute("UPDATE profile SET plan=?, cycle=? WHERE code=?",
                                (plan, cycle, ident["code"]))
                    con.commit()
                    account_changed()
                    return self._redirect("/plans?msg=" + urllib.parse.quote(
                        f"{plan_name(plan)} selected, billed {cycle}. Our team will confirm by "
                        f"email before anything is charged — nothing is billed today."))
                return self._redirect("/plans")
            if path == "/ingredient/new":
                # open to any signed-in user; suppliers/prices are attached separately
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                name = re.sub(r"\s+", " ", g("name"))[:140]
                if not name:
                    return self._redirect("/search")
                dupe = con.execute("SELECT id FROM ingredient WHERE LOWER(name)=LOWER(?)",
                                   (name,)).fetchone()
                if dupe:                      # already catalogued — just show it
                    return self._redirect(f"/ingredient/{dupe['id']}?msg=" +
                                          urllib.parse.quote("That ingredient is already listed."))
                cat = g("category")
                if not cat or cat not in [r["category"] for r in
                                          con.execute("SELECT DISTINCT category FROM ingredient")]:
                    cat = infer_category(name)
                unit = g("unit") if g("unit") in ("kg", "g", "litre", "piece") else "kg"
                iid = con.execute(
                    "INSERT INTO ingredient(name,category,cas,functions,description,unit) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, cat, g("cas")[:40] or "—", g("functions")[:140] or cat,
                     g("description")[:600] or f"{name} — added by the Ingrex community.",
                     unit)).lastrowid
                con.commit()
                return self._redirect(f"/ingredient/{iid}?msg=" + urllib.parse.quote(
                    "Added to the catalogue. Know a supplier? Raise a sourcing request."))
            if path == "/request_close":
                f = urllib.parse.parse_qs(body)
                rid = f.get("id", ["0"])[0]
                code, name, _ = requester_identity(con)
                row = con.execute("SELECT code,requester FROM request WHERE id=?", (rid,)).fetchone() \
                    if rid.isdigit() else None
                if row and (row["code"] == code or row["requester"] == name
                            or is_admin() or not gated):
                    con.execute("UPDATE request SET status='Closed', updated=? WHERE id=?",
                                (date.today().isoformat(), rid))
                    con.commit()
                return self._redirect("/requests")
            admin = is_admin() or not gated
            if path == "/admin/request" and admin:
                f = urllib.parse.parse_qs(body)
                rid = f.get("id", ["0"])[0]
                status = f.get("status", [""])[0]
                reply = f.get("reply", [""])[0].strip()[:400]
                if status in REQUEST_STATUS:
                    con.execute("UPDATE request SET status=?, reply=?, updated=? WHERE id=?",
                                (status, reply, date.today().isoformat(), rid))
                    con.commit()
                return self._redirect("/admin")
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
            if path == "/admin/vendor" and admin:
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                name = g("name")[:140]
                if not name:
                    return self._redirect("/admin")
                kind = g("kind") if g("kind") in VENDOR_KINDS else "Manufacturer"
                row = con.execute("SELECT id FROM vendor WHERE name=?", (name,)).fetchone()
                if row:
                    vid = row["id"]
                else:
                    cur = con.execute(
                        "INSERT INTO vendor(name,kind,city,country,gst,docs,poc,phone,"
                        "email,address,state,pincode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, kind, g("state") or "India", "India", g("gst").upper()[:15], "",
                         g("poc")[:80], g("phone")[:40], g("email")[:140], g("address")[:200],
                         g("state")[:60], g("pincode")[:10]))
                    con.commit()
                    vid = cur.lastrowid
                return self._redirect(f"/vendor/{vid}")
            if path == "/admin/vendor/edit":     # admin OR the supplier who owns it
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                vid = int(g("id")) if g("id").isdigit() else 0
                name = g("name")[:140]
                if vid and name and can_edit_vendor(con, vid):
                    kind = g("kind") if g("kind") in VENDOR_KINDS else "Manufacturer"
                    con.execute(
                        "UPDATE vendor SET name=?,kind=?,poc=?,phone=?,email=?,gst=?,"
                        "state=?,city=?,pincode=?,address=? WHERE id=?",
                        (name, kind, g("poc")[:80], g("phone")[:40], g("email")[:140],
                         g("gst").upper()[:15], g("state")[:60], g("state")[:60] or "India",
                         g("pincode")[:10], g("address")[:200], vid))
                    con.commit()
                return self._redirect(f"/vendor/{vid}" if vid else "/vendors")
            if path == "/admin/offer":           # admin OR the owning supplier
                f = urllib.parse.parse_qs(body)
                g = lambda k: f.get(k, [""])[0].strip()
                vid = int(g("vendor_id")) if g("vendor_id").isdigit() else 0
                ingredient = g("ingredient")[:140]
                rate = parse_rate(g("rate"))
                if vid and ingredient and rate and can_edit_vendor(con, vid):
                    row = con.execute("SELECT id FROM ingredient WHERE name=?", (ingredient,)).fetchone()
                    if row:
                        iid = row["id"]
                    else:
                        cat = infer_category(ingredient)
                        iid = con.execute(
                            "INSERT INTO ingredient(name,category,cas,functions,description,unit) "
                            "VALUES(?,?,?,?,?,?)",
                            (ingredient, cat, "—", cat,
                             f"{ingredient} — supplier-listed ingredient.", "kg")).lastrowid
                    lo, hi = price_band(rate)
                    con.execute("INSERT OR IGNORE INTO offer(ingredient_id,vendor_id,price_min,"
                                "price_max,unit,updated) VALUES(?,?,?,?,?,?)",
                                (iid, vid, lo, hi, "kg", date.today().isoformat()))
                    con.commit()
                return self._redirect(f"/vendor/{vid}" if vid else "/vendors")
            if path == "/admin/offer/del":
                f = urllib.parse.parse_qs(body)
                vid = int(f.get("vendor_id", ["0"])[0]) if f.get("vendor_id", ["0"])[0].isdigit() else 0
                oid = f.get("id", ["0"])[0]
                if vid and oid.isdigit() and can_edit_vendor(con, vid):
                    con.execute("DELETE FROM offer WHERE id=? AND vendor_id=?", (int(oid), vid))
                    con.commit()
                return self._redirect(f"/vendor/{vid}" if vid else "/vendors")
            if path == "/admin/vendor/blacklist" and admin:
                f = urllib.parse.parse_qs(body)
                vid = int(f.get("id", ["0"])[0]) if f.get("id", ["0"])[0].isdigit() else 0
                on = 1 if f.get("on", ["1"])[0] == "1" else 0
                if vid:
                    con.execute("UPDATE vendor SET blacklisted=? WHERE id=?", (on, vid))
                    con.commit()
                return self._redirect(f"/vendor/{vid}" if vid else "/vendors")
            if path == "/admin/supplier_invite" and admin:
                vid = int(urllib.parse.parse_qs(body).get("vendor_id", ["0"])[0] or 0)
                v = con.execute("SELECT name FROM vendor WHERE id=?", (vid,)).fetchone()
                if v and not con.execute(
                        "SELECT 1 FROM invite WHERE vendor_id=? AND revoked=0", (vid,)).fetchone():
                    con.execute("INSERT INTO invite(code,note,vendor_id,created) VALUES(?,?,?,?)",
                                (secrets.token_urlsafe(6), v["name"], vid, date.today().isoformat()))
                    con.commit()
                return self._redirect(f"/vendor/{vid}")
            if path == "/admin/kick" and admin:
                # remove an active user: revoke their code (logs them out) + drop presence
                code = urllib.parse.parse_qs(body).get("code", [""])[0]
                if code:
                    con.execute("UPDATE invite SET revoked=1 WHERE code=? AND is_admin=0", (code,))
                    con.commit()
                    with ONLINE_LOCK:
                        for k in [k for k, v in ONLINE.items() if v["code"] == code]:
                            del ONLINE[k]
                return self._redirect("/admin")
            self._send(page(con, "Not found", "<h1>404</h1>"), 404)
        finally:
            con.close()
            CTX.ident = None
            CTX.acct = None

    def handle_one_request(self):
        """Time each request so slow pages can be diagnosed from the host's logs:
        big server ms => the database; small server ms but a slow page => network,
        TLS or a cold start on the host."""
        t0 = time.time()
        super().handle_one_request()
        self._ms = (time.time() - t0) * 1000

    def log_message(self, fmt, *a):
        ms = getattr(self, "_ms", None)
        tail = f" {ms:.0f}ms" if ms else ""
        sys.stderr.write("%s %s%s\n" % (self.address_string(), fmt % a, tail))


def demo():
    """Self-check: run against a throwaway DB."""
    import tempfile
    global _MOVES
    _MOVES = (0.0, None)          # don't inherit a cache from another DB
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    con = init_db(tmp)

    # CSV import: real catalogue seeded, prices stored as ranges (not exact quotes)
    n_ing = con.execute("SELECT COUNT(*) c FROM ingredient").fetchone()["c"]
    n_ven = con.execute("SELECT COUNT(*) c FROM vendor").fetchone()["c"]
    assert n_ing > 50 and n_ven > 30, "catalogue seeded from suppliers.csv"
    assert len(search_ingredients(con)) == n_ing
    assert len(search_ingredients(con, "whey")) >= 1
    assert search_ingredients(con, "zzzznotreal") == []
    # searching a supplier finds what they sell — this returned nothing before
    vend = con.execute("SELECT name FROM vendor ORDER BY "
                       "(SELECT COUNT(*) FROM offer WHERE vendor_id=vendor.id) DESC "
                       "LIMIT 1").fetchone()["name"]
    assert search_ingredients(con, vend), "supplier name matches their ingredients"
    assert search_ingredients(con, vend.split()[0]), "partial supplier name works too"
    # word order must not matter
    two = [v["name"] for v in con.execute("SELECT name FROM vendor")
           if len(v["name"].split()) > 1][0].split()
    assert search_ingredients(con, f"{two[1]} {two[0]}") == search_ingredients(
        con, f"{two[0]} {two[1]}"), "token search is order-independent"
    # a near-miss on a real supplier still finds it ("Sea Pold" -> "See Pold Chemicals")
    real = con.execute("SELECT name FROM vendor WHERE name LIKE '%Pold%'").fetchone()
    if real:
        hits = suggest(con, "Sea Pold")
        assert any(h["l"] == real["name"] for h in hits), "fuzzy rescues the typo"
        assert b"Did you mean" in view_search(con, {"q": ["Sea Pold"]}), "search page suggests it"
    assert suggest(con, "zzzznotreal") == [], "fuzzy does not invent matches"
    if real:   # the suppliers page rescues the same typo
        assert b"Did you mean" in view_vendors(con, "Sea Pold"), "vendors page suggests too"
    assert b"class=tile" in view_vendors(con, "pold"), "literal supplier search still works"

    # chart: smooth curve, a marker per month, and it survives a flat series
    ch = price_chart([("2025-%02d" % m, 2400 + m * 11) for m in range(1, 13)])
    assert " C " in ch and ch.count("class=tdot") == 12, "bezier curve + one dot per point"
    assert "url(#pcg" in ch, "area gradient id is a valid reference"
    assert "No price history" in price_chart([("2025-01", 10)]), "single point degrades"
    # a narrow price band must not print the same axis label twice
    narrow = price_chart([("2025-%02d" % m, 43 + (m % 3) * 0.7) for m in range(1, 13)])
    labels = re.findall(r"class=axl>([^<]+)<", narrow)
    assert len(labels) == len(set(labels)), f"duplicate axis labels: {labels}"
    flat = price_chart([("2025-%02d" % m, 500) for m in range(1, 6)])
    assert "tdot" in flat, "a flat series still renders"
    # trend picker takes a typed name, not just an id
    named = view_dashboard(con, frozenset(), search_ingredients(con)[0]["name"])
    assert search_ingredients(con)[0]["name"].encode() in named, "chart follows the typed name"
    assert b"<datalist id=trendlist" in named, "searchable, not a dropdown"
    # autocomplete: near-matches across ingredients + suppliers, typed prefix ranked first
    sg = suggest(con, "prot")
    assert sg and all({"t", "l", "h"} <= set(s) for s in sg)
    assert suggest(con, "") == []
    o = con.execute("SELECT price_min, price_max FROM offer LIMIT 1").fetchone()
    assert o["price_min"] < o["price_max"], "offer price is a range, not exact"
    assert parse_rate("₹8,500.00 ") == 8500.0 and parse_rate("") is None
    assert price_band(100)[0] < 100 < price_band(100)[1]
    assert infer_category("Whey Protein Isolate 90%") == "Protein"
    cheap = search_ingredients(con, maxp=100)
    assert cheap and all(r["lo"] <= 100 for r in cheap)

    # ratings: none seeded; identity taken from account, not user input
    assert vendor_rating(con, 1) == (None, 0)
    assert post_rate(con, "vendor_id=1&score=9")[1].startswith("Pick"), "score out of range"
    assert post_rate(con, "vendor_id=999&score=5")[0] is None, "unknown vendor"
    vid, msg = post_rate(con, "vendor_id=1&score=4&note=good+lots")
    assert vid == 1 and vendor_rating(con, 1) == (4.0, 1)
    # rater was NOT taken from the request body (no impersonation)
    assert con.execute("SELECT rater FROM rating WHERE vendor_id=1").fetchone()["rater"] == "Anonymous"

    # rendering: escapes user input, no crash on real pages
    con.execute("INSERT INTO rating (vendor_id,rater,score) VALUES (1,?,3)",
                ("<script>x</script>",))
    con.commit()
    assert b"<script>x" not in view_vendor(con, 1)
    assert b"&lt;script&gt;" in view_vendor(con, 1)
    for i in (1, n_ing // 2, n_ing):
        assert view_ingredient(con, i)
    for v in (1, n_ven // 2, n_ven):
        assert view_vendor(con, v)
    assert view_ingredient(con, 99999) is None and view_vendor(con, 99999) is None
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
    assert b"Good " in view_dashboard(con) and b"actionbar" in view_dashboard(con)
    # the strip points at things a buyer can act on, not catalogue vanity totals
    dash = view_dashboard(con)
    for target in (b"/watchlist", b"/requests", b"/search"):
        assert target in dash, f"action bar links to {target}"
    assert b"Ingredients to source" not in dash, "vanity KPI row is gone"
    assert view_vendors(con)

    # new pages render; ingredient shows material make
    assert view_insights(con) and b"Market insights" in view_insights(con)
    assert view_myreviews(con)
    assert b"Make / origin" in view_ingredient(con, 1)

    # sourcing requests: raise + it appears for the requester and admin
    con.execute("INSERT INTO request(code,requester,company,ingredient,details,status,created,updated)"
                " VALUES('','Anonymous','','Rare Mushroom Extract','2% beta-glucan','Open','d','d')")
    con.commit()
    assert b"Rare Mushroom Extract" in view_requests(con)
    assert b"Rare Mushroom Extract" in view_admin(con)
    con.execute("UPDATE request SET status='Fulfilled', reply='Found a vendor' "
                "WHERE ingredient='Rare Mushroom Extract'")
    con.commit()
    assert b"Fulfilled" in view_requests(con) and b"Found a vendor" in view_requests(con)
    # community ticker + leads
    con.execute("INSERT INTO request(ingredient,status,created,updated) VALUES('Open Item','Open','d','d')")
    con.commit()
    assert "Open Item" in ticker(con), "ticker shows open requests"
    rid = con.execute("SELECT id FROM request WHERE ingredient='Open Item'").fetchone()["id"]
    con.execute("INSERT INTO req_note(request_id,author,company,note,created) "
                "VALUES(?,?,?,?,?)", (rid, "Zed", "Zco", "Try Vendor X", "d"))
    con.commit()
    assert b"Try Vendor X" in view_requests(con), "community lead shows on board"
    con.execute("DELETE FROM request")
    con.execute("DELETE FROM req_note")
    con.commit()

    # blacklist: hidden from buyer ingredient cards, shown to admin with a badge
    con.execute("UPDATE vendor SET blacklisted=1 WHERE id=1")
    con.commit()
    assert all(o["vid"] != 1 for o in offers_for_ingredient(con, 1)), "blacklisted vendor hidden"
    assert b"Blacklisted" in view_vendor(con, 1), "vendor page shows blacklist banner"
    con.execute("UPDATE vendor SET blacklisted=0 WHERE id=1")
    con.commit()

    # supplier self-serve: vendor-linked invite => supplier identity + edit rights
    con.execute("INSERT INTO invite(code,note,vendor_id,created) VALUES('SUP1','V1',1,'d')")
    con.execute("INSERT INTO invite(code,note,is_admin,created) VALUES('GATE','x',0,'d')")
    con.commit()  # ensure gate active
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=SUP1.{sign_code('SUP1')}"})
    assert is_supplier() and supplier_vid() == 1
    assert can_edit_vendor(con, 1) is True and can_edit_vendor(con, 2) is False
    assert b"Your listing" in view_vendor(con, 1), "supplier sees own-listing framing"
    assert b"/admin/offer/del" in view_vendor(con, 1), "supplier can remove ingredients"
    CTX.ident = None
    CTX.acct = None
    con.execute("DELETE FROM invite WHERE code IN ('SUP1','GATE')")
    con.commit()

    # trial + plans: 30 free days from the profile's join date, then a plan is needed
    con.execute("INSERT INTO invite(code,note,created) VALUES('BUY1','Riya','d')")
    con.execute("INSERT OR REPLACE INTO profile(code,name,company,role,gst,city,completed,created)"
                " VALUES('BUY1','Riya','Acme','Brand / Client','G','Pune',1,?)",
                (date.today().isoformat(),))
    con.commit()
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=BUY1.{sign_code('BUY1')}"})
    assert subscription(con) == ("", "", TRIAL_DAYS), "fresh account gets a full free month"
    assert b"Free trial" in view_account(con), "account page shows trial status"
    assert b"day" in trial_strip(con).encode(), "trial banner counts days left"
    # an old join date exhausts the trial and flips the banner to the upgrade prompt
    old = date.fromordinal(date.today().toordinal() - (TRIAL_DAYS + 5)).isoformat()
    con.execute("UPDATE profile SET created=? WHERE code='BUY1'", (old,))
    con.commit()
    account_changed()
    assert subscription(con)[2] == 0, "trial expires after TRIAL_DAYS"
    assert b"Trial ended" in trial_strip(con).encode()
    # picking a plan clears the banner and shows on both plan + account pages
    con.execute("UPDATE profile SET plan='growth', cycle='yearly' WHERE code='BUY1'")
    con.commit()
    account_changed()
    assert subscription(con)[:2] == ("growth", "yearly")
    assert trial_strip(con) == "", "paid account sees no trial banner"
    assert b"Your current plan" in view_plans(con) and b"Growth" in view_account(con)
    assert plan_name("growth") == "Growth" and plan_name("nope") == ""
    # yearly is cheaper per month than monthly on every priced tier
    assert all(yr < mo for _, _, mo, yr, *_ in PLANS if mo), "yearly must undercut monthly"
    CTX.ident = None
    CTX.acct = None
    con.execute("DELETE FROM invite WHERE code='BUY1'")
    con.execute("DELETE FROM profile WHERE code='BUY1'")
    con.commit()
    assert subscription(con) == ("", "", None), "no profile (admin/dev) => no trial clock"

    # self-serve signup: creates a live account, is idempotent per email, and
    # lands the user in onboarding rather than straight into the portal
    code1 = signup_account(con, "Arjun", "Arjun@Nutraform.COM ", "Nutraform")
    assert identity(con, {"Cookie": f"{COOKIE}={code1}.{sign_code(code1)}"}), "signup logs in"
    prof = con.execute("SELECT * FROM profile WHERE code=?", (code1,)).fetchone()
    assert prof["email"] == "arjun@nutraform.com", "email normalised"
    assert not prof["completed"], "new signup still has to finish onboarding"
    assert signup_account(con, "Arjun", "arjun@nutraform.com", "X") == code1, "same email reuses"
    # a revoked account doesn't resurrect on the old code
    con.execute("UPDATE invite SET revoked=1 WHERE code=?", (code1,))
    con.commit()
    assert signup_account(con, "Arjun", "arjun@nutraform.com", "X") != code1, "revoked => new code"
    assert EMAIL_RE.match("a@b.co") and not EMAIL_RE.match("nope@nodot")
    # finishing onboarding must not blank the email the signup stored
    code2 = signup_account(con, "Dev", "dev@nutraform.com", "Nutraform", verified=True)
    con.execute("INSERT OR REPLACE INTO profile(code,name,company,role,gst,city,"
                "completed,created,email) VALUES(?,?,?,?,?,?,1,?,?)",
                (code2, "Dev", "Nutraform", "Trader", "G" * 15, "Pune",
                 date.today().isoformat(),
                 con.execute("SELECT email FROM profile WHERE code=?", (code2,)).fetchone()["email"]))
    con.commit()
    assert con.execute("SELECT email FROM profile WHERE code=?",
                       (code2,)).fetchone()["email"] == "dev@nutraform.com", "email survives onboarding"
    # the per-request profile memo serves one query, then clears on write
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}={code2}.{sign_code(code2)}"})
    assert account(con) is account(con), "second read comes from the memo"
    account_changed()
    assert getattr(CTX, "acct", None) is None, "a profile write drops the memo"
    CTX.ident = None
    CTX.acct = None
    # Google CSRF token: signed, and not forgeable by editing the timestamp
    st = oauth_state()
    assert oauth_state_ok(st) and not oauth_state_ok(st.replace(".", "x.", 1))
    assert not oauth_state_ok("9999999999.deadbeef") and not oauth_state_ok("")
    # login screen offers signup, and Google only when credentials are configured
    assert b"/signup" in login_page() and b"Start free month" in login_page(mode="signup")
    assert (b"auth/google" in login_page()) == GOOGLE_ON
    con.execute("DELETE FROM invite WHERE code IN (SELECT code FROM profile WHERE email LIKE '%nutraform%')")
    con.execute("DELETE FROM profile WHERE email LIKE '%nutraform%'")
    con.commit()

    # CSV export mirrors the search filters and is spreadsheet-readable
    csv_all = export_csv(con, {})
    assert csv_all.startswith(b"\xef\xbb\xbf"), "BOM so Excel opens it as UTF-8"
    assert csv_all.count(b"\r\n") == len(search_ingredients(con)) + 1, "header + one row each"
    assert len(export_csv(con, {"q": ["whey"]})) < len(csv_all), "query narrows the export"
    # export is master-admin only: the link is hidden from an ordinary buyer
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=BUYX.{sign_code('BUYX')}"})
    con.execute("INSERT INTO invite(code,note,created) VALUES('BUYX','Buyer','d')")
    con.execute("INSERT INTO invite(code,note,is_admin,created) VALUES('GATEX','x',0,'d')")
    con.commit()
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=BUYX.{sign_code('BUYX')}"})
    assert not is_master(con), "plain buyer is not master admin"
    assert b"/export.csv" not in view_search(con, {}), "buyer sees no export link"
    con.execute("INSERT INTO invite(code,note,is_admin,created) VALUES('ADMX','Boss',1,'d')")
    con.commit()
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=ADMX.{sign_code('ADMX')}"})
    assert is_master(con) and b"/export.csv" in view_search(con, {}), "admin keeps it"
    CTX.ident = None
    CTX.acct = None
    con.execute("DELETE FROM invite WHERE code IN ('BUYX','GATEX','ADMX')")
    con.commit()

    # anyone can add a missing ingredient; duplicates fold into the existing one
    before = len(search_ingredients(con))
    assert b"/ingredient/new" in view_search(con, {}), "add form is on the search page"
    con.execute("INSERT INTO ingredient(name,category,cas,functions,description,unit) "
                "VALUES('Community Test Extract','Herbal Extract','—','test','t','kg')")
    con.commit()
    assert len(search_ingredients(con)) == before + 1
    assert con.execute("SELECT id FROM ingredient WHERE LOWER(name)=LOWER(?)",
                       ("community test EXTRACT",)).fetchone(), "dupe check is case-insensitive"
    assert infer_category("Ashwagandha Root Extract") == "Herbal Extract", "category auto-detect"
    con.execute("DELETE FROM ingredient WHERE name='Community Test Extract'")
    con.commit()

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
    fid = search_ingredients(con)[0]["id"]   # first card shown on the dashboard
    assert b"class='star on'" in view_dashboard(con, {fid}), "watched card shows filled star"
    assert b"class='star on'" not in view_dashboard(con, set())
    assert b"Watching" in view_ingredient(con, fid, {fid})
    assert view_watchlist(con, {fid}) and view_watchlist(con, set())

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
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": admin_c})
    assert is_admin() and b"Online now" in view_admin(con) and b"Admin" in view_dashboard(con)
    assert b"Boss" in view_dashboard(con), "greeting uses the signed-in user's name"
    assert b"/logout" in view_dashboard(con), "logout button present"
    CTX.ident = None
    CTX.acct = None

    # onboarding: profile gate + wizard render, then completion
    assert not profile_done(con, "CODE1")
    assert b"Step 1 of 3" in view_welcome(con, "CODE1")
    con.execute("INSERT OR REPLACE INTO profile"
                "(code,name,company,role,gst,city,completed,created) "
                "VALUES('CODE1','Riya','Acme','Trader','123456789012345','Pune',1,'d')")
    con.commit()
    assert profile_done(con, "CODE1")
    # profile persists in a signed cookie (survives DB resets, no impersonation)
    pc = prof_cookie({"name": "Riya", "company": "Acme", "role": "Trader",
                      "gst": "123456789012345", "city": "Pune"}).split(";")[0]
    assert read_prof({"Cookie": pc})["company"] == "Acme"
    assert read_prof({"Cookie": "prof=tampered.0000"}) is None

    # reviews are buyer-only (sellers can't review) when the gate is active
    con.execute("INSERT INTO invite(code,note,created) VALUES('BUY1','X',?)", (date.today().isoformat(),))
    con.execute("INSERT OR REPLACE INTO profile(code,name,company,role,gst,city,completed,created)"
                " VALUES('BUY1','X','Y','Trader','g','c',1,'d')")
    con.commit()
    CTX.acct = None
    CTX.ident = identity(con, {"Cookie": f"{COOKIE}=BUY1.{sign_code('BUY1')}"})
    assert gate_active(con) and can_review(con) is False, "seller cannot review"
    con.execute("UPDATE profile SET role='Brand / Client' WHERE code='BUY1'")
    con.commit()
    assert can_review(con) is True, "buyer can review"
    assert post_rate(con, "vendor_id=1&score=5&note=ok")[0] == 1, "buyer post accepted"
    CTX.ident = None
    CTX.acct = None
    con.execute("DELETE FROM invite WHERE code='BUY1'")
    con.execute("DELETE FROM profile WHERE code='BUY1'")
    con.commit()

    # kick: remove-active-user logic revokes code + drops presence
    ONLINE.clear()
    touch_online("CODE1|9.9.9.9", "Riya", "CODE1", "9.9.9.9")
    assert online_count() == 1
    with ONLINE_LOCK:
        for k in [k for k, v in ONLINE.items() if v["code"] == "CODE1"]:
            del ONLINE[k]
    assert online_count() == 0
    ONLINE.clear()
    con.execute("DELETE FROM invite")
    con.commit()

    # price-trend selector: dashboard renders a picker and honours the choice
    pick = search_ingredients(con)[2]
    assert b"class=trendsel" in view_dashboard(con)
    # an id still works (datalist submits the name, old links submit the id)
    assert E(pick["name"]).encode() in view_dashboard(con, set(), str(pick["id"]))

    # card: price band, colored accent, supplier count, updated — no image
    dash = view_dashboard(con)
    assert b"priceband" in dash and b"Supplier" in dash and b"Updated" in dash
    assert b"--cc:" in dash, "category-tinted accent"
    assert b"iimg" not in dash, "illustration removed"
    assert "rating" in search_ingredients(con)[0].keys()

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
