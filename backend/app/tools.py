"""Everything the agent can look up or act on. Kept as plain functions over
sqlite rather than behind a framework so the tool surface the agent uses is
easy to read end to end: these are exactly the "tools" the agent calls."""
import datetime
import itertools
import json

from . import db

_po_counter = itertools.count(9001)


def get_product(sku):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    conn.close()
    d = dict(row)
    d["trailing_daily_actuals"] = json.loads(d["trailing_daily_actuals"])
    return d


def get_suppliers(sku):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM suppliers WHERE sku=? ORDER BY unit_price ASC", (sku,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_purchase_orders(sku, exclude_id=None):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM open_purchase_orders WHERE sku=?", (sku,)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    if exclude_id:
        result = [r for r in result if r["id"] != exclude_id]
    return result


def get_open_purchase_order(po_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM open_purchase_orders WHERE id=?", (po_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_situation(situation_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM situations WHERE id=?", (situation_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["trigger_payload"] = json.loads(d["trigger_payload"])
    return d


def list_situations():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM situations ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_situation_status(situation_id, status):
    conn = db.get_conn()
    conn.execute("UPDATE situations SET status=? WHERE id=?", (status, situation_id))
    conn.commit()
    conn.close()


def read_storage_remaining(situation_id):
    """Re-checking a shared capacity pool. The first read is what the decision
    is made against; a later read (at validation time) can show that capacity
    moved in the meantime — e.g. another workflow reserved part of it."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT storage_remaining, storage_drift, storage_reads FROM situations WHERE id=?", (situation_id,)
    ).fetchone()
    reads = row["storage_reads"] + 1
    conn.execute("UPDATE situations SET storage_reads=? WHERE id=?", (reads, situation_id))
    conn.commit()
    conn.close()
    if reads <= 1:
        return row["storage_remaining"]
    return row["storage_remaining"] - row["storage_drift"]


def create_purchase_order(situation_id, sku, supplier_id, quantity, unit_price, notes=""):
    po_id = f"PO-{next(_po_counter)}"
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO purchase_orders (id,situation_id,sku,supplier_id,quantity,unit_price,status,notes) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (po_id, situation_id, sku, supplier_id, quantity, unit_price, "created", notes),
    )
    conn.commit()
    conn.close()
    return po_id


def update_purchase_order(po_id, **fields):
    conn = db.get_conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE purchase_orders SET {sets} WHERE id=?", (*fields.values(), po_id))
    conn.commit()
    conn.close()


def get_purchase_order(po_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_purchase_orders():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM purchase_orders ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_approval(situation_id, action_type, proposal):
    approval_id = f"APR-{situation_id}"
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO approvals (id,situation_id,action_type,proposal,status) VALUES (?,?,?,?,'pending')",
        (approval_id, situation_id, action_type, json.dumps(proposal)),
    )
    conn.commit()
    conn.close()
    return approval_id


def get_approval(approval_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["proposal"] = json.loads(d["proposal"])
    return d


def list_approvals():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM approvals ORDER BY id").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["proposal"] = json.loads(d["proposal"])
        result.append(d)
    return result


def set_approval_status(approval_id, status):
    conn = db.get_conn()
    conn.execute("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))
    conn.commit()
    conn.close()


def log(situation_id, step, detail):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO audit_log (situation_id, ts, step, detail) VALUES (?,?,?,?)",
        (situation_id, datetime.datetime.utcnow().isoformat(), step, json.dumps(detail, default=str)),
    )
    conn.commit()
    conn.close()


def get_audit_log(situation_id):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM audit_log WHERE situation_id=? ORDER BY id", (situation_id,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        result.append(d)
    return result
