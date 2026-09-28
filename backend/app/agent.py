"""Orchestrator: perceive (tools) -> reason (policy) -> act (tools) ->
validate (tools) -> escalate on anything uncertain. One entry point per
situation; the shortfall scenario gets its own gather/decide step because it
starts from a different trigger, but shares the same act/validate/approve path."""
from . import policy, tools


def run_situation(situation_id):
    situation = tools.get_situation(situation_id)
    if situation is None:
        raise ValueError(f"unknown situation {situation_id}")
    tools.log(situation_id, "perceive:start", {"scenario_type": situation["scenario_type"]})

    if situation["scenario_type"] == "supplier_shortfall":
        return _run_shortfall(situation)
    return _run_purchase_review(situation)


def _gather_purchase_context(situation):
    product = tools.get_product(situation["sku"])
    suppliers = tools.get_suppliers(situation["sku"])
    open_pos = tools.get_open_purchase_orders(situation["sku"])
    open_po_qty = sum(o["quantity"] for o in open_pos if o["status"] in ("in_transit", "pending_confirmation"))
    storage_remaining = tools.read_storage_remaining(situation["id"])

    ctx = {
        "recommended_qty": situation["recommended_qty"],
        "avg_daily_demand": product["avg_daily_demand"],
        "forecast_daily_demand": product["forecast_daily_demand"],
        "trailing_daily_actuals": product["trailing_daily_actuals"],
        "current_inventory": product["current_inventory"],
        "open_po_qty": open_po_qty,
        "suppliers": suppliers,
        "budget_remaining": situation["budget_remaining"],
        "storage_remaining": storage_remaining,
        "storage_units_per_item": product["storage_units_per_item"],
        "node": situation["node"],
    }
    tools.log(situation["id"], "perceive:gathered", {
        "product": product, "suppliers": suppliers, "open_purchase_orders": open_pos,
        "storage_remaining_read": storage_remaining,
    })
    return ctx


def _run_purchase_review(situation):
    ctx = _gather_purchase_context(situation)
    rec = policy.evaluate_purchase(ctx)
    tools.log(situation["id"], "reason:decision", rec)

    if rec["decision"] in ("accept", "modify", "recommend_purchase") and not rec["needs_human_approval"]:
        po_id = tools.create_purchase_order(
            situation["id"], situation["sku"], rec["supplier_id"], rec["quantity"], rec["unit_price"],
            notes=f"Auto-created ({rec['decision']})",
        )
        tools.log(situation["id"], "act:create_po", {"po_id": po_id, "quantity": rec["quantity"]})
        validation = _validate_purchase_order(situation, po_id, rec["quantity"])
        status = "resolved_with_adjustment" if validation["adjusted"] else "resolved"
        tools.set_situation_status(situation["id"], status)
        return _trace(situation["id"], rec, po_id, validation, status)

    if rec["decision"] == "reject":
        tools.set_situation_status(situation["id"], "resolved")
        return _trace(situation["id"], rec, None, None, "resolved")

    action_type = "execute_proposed_po" if rec["quantity"] else "review_blocked_recommendation"
    tools.create_approval(situation["id"], action_type, rec)
    status = "escalated" if rec["decision"] == "escalate" else "pending_approval"
    tools.set_situation_status(situation["id"], status)
    return _trace(situation["id"], rec, None, None, status)


def _run_shortfall(situation):
    product = tools.get_product(situation["sku"])
    payload = situation["trigger_payload"]
    original_po = tools.get_open_purchase_order(payload["original_po_id"])
    suppliers = tools.get_suppliers(situation["sku"])
    other_open = tools.get_open_purchase_orders(situation["sku"], exclude_id=payload["original_po_id"])
    other_qty = sum(o["quantity"] for o in other_open if o["status"] == "in_transit")
    original_supplier = next((s for s in suppliers if s["id"] == payload["original_supplier_id"]), None)

    ctx = {
        "original_qty": original_po["quantity"],
        "confirmed_qty": payload["confirmed_qty"],
        "avg_daily_demand": product["avg_daily_demand"],
        "forecast_daily_demand": product["forecast_daily_demand"],
        "current_inventory": product["current_inventory"],
        "other_open_po_qty": other_qty,
        "suppliers": suppliers,
        "original_supplier_id": payload["original_supplier_id"],
        "original_unit_price": original_supplier["unit_price"] if original_supplier else None,
    }
    tools.log(situation["id"], "perceive:gathered", {"product": product, "suppliers": suppliers, "original_po": original_po})

    rec = policy.evaluate_shortfall(ctx)
    tools.log(situation["id"], "reason:decision", rec)

    if rec["decision"] == "sufficient_as_is":
        tools.set_situation_status(situation["id"], "resolved")
        return _trace(situation["id"], rec, None, None, "resolved")

    if rec["decision"] == "sourced_alternate" and not rec["needs_human_approval"]:
        po_id = tools.create_purchase_order(
            situation["id"], situation["sku"], rec["supplier_id"], rec["quantity"], rec["unit_price"],
            notes="Supplemental order covering supplier shortfall",
        )
        tools.log(situation["id"], "act:create_supplemental_po", {"po_id": po_id, "quantity": rec["quantity"]})
        validation = _validate_purchase_order(situation, po_id, rec["quantity"])
        status = "resolved_with_adjustment" if validation["adjusted"] else "resolved"
        tools.set_situation_status(situation["id"], status)
        return _trace(situation["id"], rec, po_id, validation, status)

    action_type = "execute_supplemental_po" if rec["quantity"] and rec["supplier_id"] else "review_blocked_recommendation"
    tools.create_approval(situation["id"], action_type, rec)
    status = "escalated" if rec["decision"] == "escalate" else "pending_approval"
    tools.set_situation_status(situation["id"], status)
    return _trace(situation["id"], rec, None, None, status)


def _validate_purchase_order(situation, po_id, committed_qty):
    """Feedback loop: re-check the constraint the decision relied on with a
    fresh read. If the world moved between decision and execution, correct
    the order instead of leaving in place something nobody actually authorized."""
    product = tools.get_product(situation["sku"])
    fresh_storage = tools.read_storage_remaining(situation["id"])
    max_storable = fresh_storage / product["storage_units_per_item"] if product["storage_units_per_item"] else float("inf")

    if committed_qty <= max_storable:
        tools.update_purchase_order(po_id, status="validated")
        result = {
            "ok": True, "adjusted": False, "final_quantity": committed_qty,
            "note": f"Storage re-check passed: {max_storable:.0f} units of room available for {committed_qty:.0f} committed.",
        }
        tools.log(situation["id"], "validate:ok", result)
        return result

    po = tools.get_purchase_order(po_id)
    supplier_moq = next((s["min_order_qty"] for s in tools.get_suppliers(situation["sku"]) if s["id"] == po["supplier_id"]), None)

    if supplier_moq is not None and max_storable >= supplier_moq:
        adjusted_qty = max_storable
        tools.update_purchase_order(
            po_id, quantity=adjusted_qty, status="validated",
            notes=(po["notes"] or "") + f" | Auto-adjusted from {committed_qty:.0f} to {adjusted_qty:.0f} after storage re-check.",
        )
        result = {
            "ok": True, "adjusted": True, "final_quantity": adjusted_qty,
            "note": f"Storage capacity had shifted to {max_storable:.0f} units of room by the time the order was "
                    f"confirmed. Automatically trimmed {po_id} from {committed_qty:.0f} to {adjusted_qty:.0f} units "
                    f"to stay within capacity.",
        }
        tools.log(situation["id"], "validate:auto_corrected", result)
        return result

    tools.update_purchase_order(po_id, status="exception")
    result = {
        "ok": False, "adjusted": False, "final_quantity": 0,
        "note": "Storage capacity dropped below this supplier's minimum order quantity between decision and "
                "execution. The order can't be safely trimmed — escalating for manual review.",
    }
    tools.log(situation["id"], "validate:failed", result)
    tools.create_approval(situation["id"], "review_validation_exception", {"po_id": po_id, **result})
    tools.set_situation_status(situation["id"], "escalated")
    return result


def _trace(situation_id, rec, po_id, validation, status):
    return {
        "situation_id": situation_id,
        "decision": rec["decision"],
        "quantity": rec["quantity"],
        "supplier_id": rec["supplier_id"],
        "confidence": rec["confidence"],
        "needs_human_approval": rec["needs_human_approval"],
        "factors": rec["factors"],
        "purchase_order_id": po_id,
        "validation": validation,
        "status": status,
        "audit_log": tools.get_audit_log(situation_id),
    }


def apply_approval(approval_id):
    approval = tools.get_approval(approval_id)
    if approval is None:
        raise ValueError("unknown approval")
    situation_id = approval["situation_id"]
    situation = tools.get_situation(situation_id)
    rec = approval["proposal"]

    if not rec.get("quantity") or not rec.get("supplier_id"):
        tools.set_approval_status(approval_id, "acknowledged")
        tools.set_situation_status(situation_id, "resolved")
        tools.log(situation_id, "approve:acknowledged_no_action", {"approval_id": approval_id})
        return {
            "situation_id": situation_id, "status": "resolved",
            "note": "Acknowledged — no compliant automatic action was available; handled manually outside the system.",
        }

    po_id = tools.create_purchase_order(
        situation_id, situation["sku"], rec["supplier_id"], rec["quantity"], rec.get("unit_price") or 0,
        notes=f"Created after buyer approval ({rec['decision']})",
    )
    tools.log(situation_id, "act:create_po_after_approval", {"po_id": po_id, "quantity": rec["quantity"]})
    validation = _validate_purchase_order(situation, po_id, rec["quantity"])
    status = "resolved_with_adjustment" if validation["adjusted"] else "resolved"
    tools.set_situation_status(situation_id, status)
    tools.set_approval_status(approval_id, "approved")
    return {"situation_id": situation_id, "status": status, "purchase_order_id": po_id, "validation": validation}


def reject_approval(approval_id, reason=""):
    approval = tools.get_approval(approval_id)
    if approval is None:
        raise ValueError("unknown approval")
    tools.set_approval_status(approval_id, "rejected")
    tools.set_situation_status(approval["situation_id"], "rejected")
    tools.log(approval["situation_id"], "reject", {"approval_id": approval_id, "reason": reason})
    return {"situation_id": approval["situation_id"], "status": "rejected"}
