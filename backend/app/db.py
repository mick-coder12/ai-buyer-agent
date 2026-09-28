import json
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "purchasing_agent.db"))
SEED_PATH = BASE_DIR / "seed_data.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    storage_units_per_item REAL NOT NULL,
    current_inventory REAL NOT NULL,
    avg_daily_demand REAL NOT NULL,
    forecast_daily_demand REAL NOT NULL,
    trailing_daily_actuals TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    lead_time_days REAL NOT NULL,
    min_order_qty REAL NOT NULL,
    unit_price REAL NOT NULL,
    reliability_score REAL NOT NULL,
    available_qty REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS open_purchase_orders (
    id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    quantity REAL NOT NULL,
    expected_arrival_days REAL NOT NULL,
    status TEXT NOT NULL,
    supplier_id TEXT
);

CREATE TABLE IF NOT EXISTS situations (
    id TEXT PRIMARY KEY,
    scenario_type TEXT NOT NULL,
    sku TEXT NOT NULL,
    node TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    recommended_qty REAL NOT NULL,
    budget_remaining REAL NOT NULL,
    storage_remaining REAL NOT NULL,
    storage_drift REAL NOT NULL DEFAULT 0,
    storage_reads INTEGER NOT NULL DEFAULT 0,
    trigger_payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id TEXT PRIMARY KEY,
    situation_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    situation_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    proposal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    situation_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    step TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def reset_and_seed():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_conn()
    conn.executescript(SCHEMA)
    data = json.loads(SEED_PATH.read_text())

    for p in data["products"]:
        conn.execute(
            "INSERT INTO products (sku,name,category,storage_units_per_item,current_inventory,"
            "avg_daily_demand,forecast_daily_demand,trailing_daily_actuals) VALUES (?,?,?,?,?,?,?,?)",
            (p["sku"], p["name"], p["category"], p["storage_units_per_item"], p["current_inventory"],
             p["avg_daily_demand"], p["forecast_daily_demand"], json.dumps(p["trailing_daily_actuals"])),
        )

    for s in data["suppliers"]:
        conn.execute(
            "INSERT INTO suppliers (id,sku,name,lead_time_days,min_order_qty,unit_price,"
            "reliability_score,available_qty) VALUES (?,?,?,?,?,?,?,?)",
            (s["id"], s["sku"], s["name"], s["lead_time_days"], s["min_order_qty"],
             s["unit_price"], s["reliability_score"], s["available_qty"]),
        )

    for o in data["open_purchase_orders"]:
        conn.execute(
            "INSERT INTO open_purchase_orders (id,sku,quantity,expected_arrival_days,status,supplier_id) "
            "VALUES (?,?,?,?,?,?)",
            (o["id"], o["sku"], o["quantity"], o["expected_arrival_days"], o["status"], o.get("supplier_id")),
        )

    for s in data["situations"]:
        conn.execute(
            "INSERT INTO situations (id,scenario_type,sku,node,title,description,recommended_qty,"
            "budget_remaining,storage_remaining,storage_drift,trigger_payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s["scenario_type"], s["sku"], s["node"], s["title"], s["description"],
             s["recommended_qty"], s["budget_remaining"], s["storage_remaining"],
             s.get("storage_drift", 0), json.dumps(s.get("trigger_payload", {}))),
        )

    conn.commit()
    conn.close()


def ensure_db():
    if not os.path.exists(DB_PATH):
        reset_and_seed()
