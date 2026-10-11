"""FoodRescue AI - inventory, waste prediction, redistribution marketplace. Run: uvicorn backend.main:app --reload"""
import csv, io, math, random, sqlite3, datetime as dt
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB = Path(__file__).parent / "foodrescue.db"
app = FastAPI(title="FoodRescue AI")
today = lambda: dt.date.today()

# Food taxonomy: category -> (perishability 1-5, storage)
TAXONOMY = {"dairy": (5, "chilled"), "meat": (5, "frozen/chilled"), "bakery": (4, "ambient"),
            "produce": (4, "chilled"), "prepared": (5, "chilled"), "beverages": (2, "ambient"),
            "grains": (1, "dry"), "canned": (1, "dry")}

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS orgs(id INTEGER PRIMARY KEY, name TEXT, role TEXT, lat REAL, lon REAL, needs TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, biz_id INT, name TEXT, barcode TEXT, category TEXT,
        qty REAL, unit TEXT DEFAULT 'kg', expiry TEXT, capacity REAL DEFAULT 100);
    CREATE TABLE IF NOT EXISTS sales(item_id INT, day TEXT, units REAL);
    CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY, biz_id INT, item_id INT, name TEXT, category TEXT,
        qty REAL, expiry TEXT, location TEXT, window TEXT, status TEXT DEFAULT 'open', ngo_id INT);
    CREATE TABLE IF NOT EXISTS pickups(id INTEGER PRIMARY KEY, listing_id INT, ngo_id INT, slot TEXT, status TEXT DEFAULT 'scheduled');
    """); c.commit(); c.close()
init()

# ---------- Models ----------
class Item(BaseModel):
    biz_id: int; name: str; category: str; qty: float; expiry: str
    barcode: str = ""; unit: str = "kg"; capacity: float = 100
class Scan(BaseModel):
    biz_id: int; barcode: str; delta: float
class Listing(BaseModel):
    item_id: int; qty: float; location: str; window: str
class Pickup(BaseModel):
    listing_id: int; ngo_id: int; slot: str

# ---------- Forecasting ----------
def forecast(item_id, horizon=14):
    """Daily demand forecast. Uses Prophet if installed, else weekday-seasonal average + linear trend."""
    c = db(); rows = c.execute("SELECT day, units FROM sales WHERE item_id=? ORDER BY day", (item_id,)).fetchall(); c.close()
    if len(rows) < 7: return [0.0] * horizon
    try:
        import pandas as pd; from prophet import Prophet
        m = Prophet(daily_seasonality=False); m.fit(pd.DataFrame({"ds": [r["day"] for r in rows], "y": [r["units"] for r in rows]}))
        f = m.predict(m.make_future_dataframe(horizon))
        return [max(0, v) for v in f["yhat"].tail(horizon)]
    except Exception:
        pass
    ys = [r["units"] for r in rows]; n = len(ys)
    by_dow = {}
    for r in rows: by_dow.setdefault(dt.date.fromisoformat(r["day"]).weekday(), []).append(r["units"])
    base = sum(ys) / n
    recent = sum(ys[-7:]) / min(7, n)
    trend = (recent - base) / max(1, n / 2)
    out = []
    for i in range(1, horizon + 1):
        d = today() + dt.timedelta(days=i)
        s = by_dow.get(d.weekday(), [base]); seas = (sum(s) / len(s)) / base if base else 1
        out.append(max(0.0, (recent + trend * i) * seas))
    return out

def risk(item):
    """0-100 waste risk from days-to-expiry, stock level and forecast demand."""
    days = (dt.date.fromisoformat(item["expiry"]) - today()).days
    if days < 0 or item["qty"] <= 0: return (100.0 if item["qty"] > 0 else 0.0), days
    fc = forecast(item["id"], 14); expected = sum(fc[:max(1, days)])
    overstock = max(0.0, item["qty"] - expected) / item["qty"]
    urgency = 1 - min(days, 14) / 14
    perish = TAXONOMY.get(item["category"], (3,))[0] / 5
    return round(100 * (0.55 * overstock + 0.3 * urgency + 0.15 * perish), 1), days

def level(s): return "high" if s >= 60 else "medium" if s >= 35 else "low"

# ---------- Inventory ----------
@app.post("/api/items")
def add_item(i: Item):
    if i.category not in TAXONOMY: raise HTTPException(400, f"category must be one of {list(TAXONOMY)}")
    c = db(); cur = c.execute("INSERT INTO items(biz_id,name,barcode,category,qty,unit,expiry,capacity) VALUES(?,?,?,?,?,?,?,?)",
        (i.biz_id, i.name, i.barcode, i.category, i.qty, i.unit, i.expiry, i.capacity)); c.commit(); return {"id": cur.lastrowid}

@app.post("/api/items/upload/{biz_id}")
async def upload(biz_id: int, file: UploadFile = File(...)):
    """CSV columns: name,category,qty,expiry[,barcode,unit,capacity]. Also the target for POS REST integrations."""
    rows = list(csv.DictReader(io.StringIO((await file.read()).decode())))
    c = db()
    for r in rows:
        c.execute("INSERT INTO items(biz_id,name,barcode,category,qty,unit,expiry,capacity) VALUES(?,?,?,?,?,?,?,?)",
            (biz_id, r["name"], r.get("barcode", ""), r["category"], float(r["qty"]), r.get("unit") or "kg", r["expiry"], float(r.get("capacity") or 100)))
    c.commit(); return {"imported": len(rows)}

@app.post("/api/scan")
def scan(s: Scan):
    c = db(); it = c.execute("SELECT id,qty FROM items WHERE biz_id=? AND barcode=?", (s.biz_id, s.barcode)).fetchone()
    if not it: raise HTTPException(404, "barcode not found")
    c.execute("UPDATE items SET qty=? WHERE id=?", (max(0, it["qty"] + s.delta), it["id"]))
    if s.delta < 0: c.execute("INSERT INTO sales VALUES(?,?,?)", (it["id"], str(today()), -s.delta))
    c.commit(); return {"id": it["id"], "qty": max(0, it["qty"] + s.delta)}

@app.get("/api/items/{biz_id}")
def items(biz_id: int):
    c = db(); out = []
    for r in c.execute("SELECT * FROM items WHERE biz_id=? AND qty>0", (biz_id,)):
        d = dict(r); d["risk"], d["days_to_expiry"] = risk(d); d["level"] = level(d["risk"])
        d["storage"] = TAXONOMY.get(d["category"], (0, "?"))[1]; out.append(d)
    return sorted(out, key=lambda x: -x["risk"])

@app.get("/api/alerts/{biz_id}")
def alerts(biz_id: int, days: int = 3):
    return [i for i in items(biz_id) if i["days_to_expiry"] <= days]

@app.get("/api/reorder/{biz_id}")
def reorder(biz_id: int):
    out = []
    for i in items(biz_id):
        fc = forecast(i["id"], 7); need = sum(fc); sug = max(0, math.ceil(min(need, i["capacity"]) - i["qty"]))
        out.append({"item": i["name"], "stock": i["qty"], "forecast_7d": round(need, 1), "suggested_order": sug,
                    "note": "overstocked - skip reorder" if i["level"] == "high" else ""})
    return out

# ---------- Marketplace ----------
@app.post("/api/listings")
def list_surplus(l: Listing):
    c = db(); it = c.execute("SELECT * FROM items WHERE id=?", (l.item_id,)).fetchone()
    if not it or l.qty > it["qty"]: raise HTTPException(400, "invalid item or quantity")
    c.execute("UPDATE items SET qty=qty-? WHERE id=?", (l.qty, l.item_id))
    cur = c.execute("INSERT INTO listings(biz_id,item_id,name,category,qty,expiry,location,window) VALUES(?,?,?,?,?,?,?,?)",
        (it["biz_id"], it["id"], it["name"], it["category"], l.qty, it["expiry"], l.location, l.window)); c.commit()
    return {"id": cur.lastrowid}

@app.get("/api/listings")
def listings(status: str = "open"):
    return [dict(r) for r in db().execute("SELECT * FROM listings WHERE status=? ORDER BY expiry", (status,))]

def hav(a, b, c, d):
    p = math.pi / 180; x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))

@app.get("/api/match/{listing_id}")
def match(listing_id: int):
    """Rank NGOs: 40% food-type need, 35% proximity, 25% quantity fit."""
    c = db(); l = c.execute("SELECT l.*, o.lat, o.lon FROM listings l JOIN orgs o ON o.id=l.biz_id WHERE l.id=?", (listing_id,)).fetchone()
    if not l: raise HTTPException(404)
    res = []
    for n in c.execute("SELECT * FROM orgs WHERE role='ngo'"):
        needs = [x for x in n["needs"].split(",") if x]; km = hav(l["lat"], l["lon"], n["lat"], n["lon"])
        s = 0.4 * (1 if l["category"] in needs else 0.2) + 0.35 * max(0, 1 - km / 25) + 0.25 * min(1, 50 / max(l["qty"], 1))
        res.append({"ngo_id": n["id"], "ngo": n["name"], "distance_km": round(km, 1), "score": round(s * 100, 1)})
    return sorted(res, key=lambda x: -x["score"])

@app.post("/api/pickups")
def schedule(p: Pickup):
    c = db(); l = c.execute("SELECT status FROM listings WHERE id=?", (p.listing_id,)).fetchone()
    if not l or l["status"] != "open": raise HTTPException(400, "listing unavailable")
    c.execute("UPDATE listings SET status='claimed', ngo_id=? WHERE id=?", (p.ngo_id, p.listing_id))
    cur = c.execute("INSERT INTO pickups(listing_id,ngo_id,slot) VALUES(?,?,?)", (p.listing_id, p.ngo_id, p.slot)); c.commit()
    return {"pickup_id": cur.lastrowid, "notify": ["donor", "recipient"], "slot": p.slot}

@app.post("/api/pickups/{pid}/complete")
def complete(pid: int):
    c = db(); r = c.execute("SELECT listing_id FROM pickups WHERE id=?", (pid,)).fetchone()
    c.execute("UPDATE pickups SET status='completed' WHERE id=?", (pid,))
    c.execute("UPDATE listings SET status='collected' WHERE id=?", (r["listing_id"],)); c.commit(); return {"ok": True}

@app.get("/api/pickups/{ngo_id}")
def ngo_pickups(ngo_id: int):
    return [dict(r) for r in db().execute("SELECT p.*, l.name, l.qty, l.location FROM pickups p JOIN listings l ON l.id=p.listing_id WHERE p.ngo_id=?", (ngo_id,))]

@app.get("/api/impact")
def impact():
    kg = db().execute("SELECT COALESCE(SUM(qty),0) k FROM listings WHERE status IN ('claimed','collected')").fetchone()["k"]
    return {"kg_diverted": kg, "meals": round(kg / 0.45), "co2_kg_saved": round(kg * 2.5, 1)}

@app.get("/api/orgs")
def orgs(): return [dict(r) for r in db().execute("SELECT * FROM orgs")]

@app.post("/api/seed")
def seed():
    """Demo data: 1 business, 2 NGOs, 6 products with 60 days of sales history."""
    c = db()
    for t in ("orgs", "items", "sales", "listings", "pickups"): c.execute(f"DELETE FROM {t}")
    c.executemany("INSERT INTO orgs VALUES(?,?,?,?,?,?)", [
        (1, "Fresh Mart Supermarket", "business", 13.6288, 79.4192, ""),
        (2, "Annapurna Community Kitchen", "ngo", 13.6500, 79.4300, "prepared,bakery,produce"),
        (3, "City Food Bank", "ngo", 13.7000, 79.3800, "grains,canned,dairy,beverages")])
    random.seed(1)
    demo = [("Milk", "dairy", 80, 2, 12), ("Bread", "bakery", 60, 1, 25), ("Tomatoes", "produce", 90, 3, 15),
            ("Chicken", "meat", 40, 2, 10), ("Rice", "grains", 200, 120, 8), ("Cooked Meals", "prepared", 30, 1, 20)]
    for n, (name, cat, qty, d, avg) in enumerate(demo, 1):
        c.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)", (n, 1, name, f"890{n}", cat, qty, "kg", str(today() + dt.timedelta(days=d)), 150))
        for k in range(60, 0, -1):
            day = today() - dt.timedelta(days=k)
            c.execute("INSERT INTO sales VALUES(?,?,?)", (n, str(day), max(0, random.gauss(avg * (1.3 if day.weekday() >= 5 else 1), 2))))
    c.commit(); return {"seeded": True}

app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="ui")
