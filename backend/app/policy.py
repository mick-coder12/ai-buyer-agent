"""Deterministic decision policy. This is the part of the agent that decides
what to do — kept rule-based and fully explainable (every decision returns
the factors behind it) rather than delegated to a model call, so the same
input always produces the same, auditable output. See README for why."""
from statistics import mean
from typing import Any, Dict, List

REVIEW_BUFFER_DAYS = 7
SAFETY_MARGIN_DAYS = 1
ANOMALY_HIGH_RATIO = 1.3
SUSTAINED_DAY_RATIO = 1.2
SUSTAINED_DAYS_REQUIRED = 3
AUTO_APPROVE_COST_THRESHOLD = 5000.0
ALT_SUPPLIER_MAX_PREMIUM = 0.10
ACCEPT_TOLERANCE_RATIO = 0.10


def _factor(label: str, detail: str) -> Dict[str, str]:
    return {"label": label, "detail": detail}


def choose_supplier(suppliers: List[Dict[str, Any]], days_of_cover: float):
    """Cheapest supplier that can still arrive before cover runs out; falls
    back to the fastest supplier when nobody cheap enough can make it in time."""
    available = [s for s in suppliers if s["available_qty"] > 0]
    if not available:
        return None
    cheapest = min(available, key=lambda s: s["unit_price"])
    if days_of_cover >= cheapest["lead_time_days"]:
        return cheapest
    return min(available, key=lambda s: s["lead_time_days"])


def evaluate_purchase(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Core recommendation-review policy (Scenario 1). Also reused for
    Scenario 3, after the demand signal is re-based on actuals, and Scenario 4,
    where constraints leave no feasible quantity."""
    factors = []
    recommended_qty = ctx.get("recommended_qty") or 0
    baseline_rate = max(ctx["avg_daily_demand"], ctx["forecast_daily_demand"])
    trailing = ctx.get("trailing_daily_actuals") or []
    trailing_avg = mean(trailing) if trailing else baseline_rate
    last3 = trailing[-3:] if len(trailing) >= 3 else trailing
    sustained_spike = (
        bool(last3)
        and len(last3) >= SUSTAINED_DAYS_REQUIRED
        and all(v >= baseline_rate * SUSTAINED_DAY_RATIO for v in last3)
    )
    spike_signal = trailing_avg >= baseline_rate * ANOMALY_HIGH_RATIO

    if spike_signal and not sustained_spike:
        factors.append(_factor(
            "Demand signal",
            f"Trailing actual demand (~{trailing_avg:.0f}/day) is running above the forecast baseline "
            f"({baseline_rate:.0f}/day), but the elevated level hasn't held for {SUSTAINED_DAYS_REQUIRED} "
            f"straight days yet. Recommend confirming the trend holds before committing to a larger buy.",
        ))
        return {
            "decision": "investigate", "quantity": 0, "supplier_id": None, "unit_price": None,
            "factors": factors, "needs_human_approval": True, "confidence": "low",
        }

    demand_rate = round(mean(last3), 1) if sustained_spike else baseline_rate
    if sustained_spike:
        factors.append(_factor(
            "Demand signal",
            f"Actual sales have run at ~{demand_rate:.0f}/day for the last {len(last3)} days, "
            f"{demand_rate / baseline_rate:.1f}x the forecast baseline of {baseline_rate:.0f}/day. "
            f"Treating this as a real shift rather than noise and re-basing the calculation on it.",
        ))
    else:
        factors.append(_factor(
            "Demand signal",
            f"Demand looks stable: trailing actuals average {trailing_avg:.0f}/day against a forecast of "
            f"{baseline_rate:.0f}/day.",
        ))

    current_inventory = ctx["current_inventory"]
    open_po_qty = ctx.get("open_po_qty", 0)
    current_position = current_inventory + open_po_qty
    days_of_cover = current_position / demand_rate if demand_rate else float("inf")

    factors.append(_factor(
        "Inventory position",
        f"On-hand {current_inventory:.0f} + {open_po_qty:.0f} incoming = {current_position:.0f} units, "
        f"~{days_of_cover:.1f} days of cover at the current demand rate.",
    ))

    supplier = choose_supplier(ctx["suppliers"], days_of_cover)
    if supplier is None:
        factors.append(_factor("Supplier availability", "No supplier currently has stock available for this SKU."))
        return {
            "decision": "escalate", "quantity": 0, "supplier_id": None, "unit_price": None,
            "factors": factors, "needs_human_approval": True, "confidence": "low",
        }

    urgent = days_of_cover < supplier["lead_time_days"]
    factors.append(_factor(
        "Supplier selection",
        f"Chose {supplier['name']} (lead time {supplier['lead_time_days']:.0f}d, "
        f"${supplier['unit_price']:.2f}/unit, MOQ {supplier['min_order_qty']:.0f}) "
        + ("because cheaper options would arrive too late to avoid a stockout."
           if urgent else "as the lowest-cost option that can still arrive in time."),
    ))

    target_cover_days = supplier["lead_time_days"] + REVIEW_BUFFER_DAYS
    needed_qty = max(0.0, round(demand_rate * target_cover_days - current_position))

    max_affordable = ctx["budget_remaining"] / supplier["unit_price"] if supplier["unit_price"] else float("inf")
    max_storable = (
        ctx["storage_remaining"] / ctx["storage_units_per_item"] if ctx["storage_units_per_item"] else float("inf")
    )

    factors.append(_factor(
        "Coverage requirement",
        f"To hold {target_cover_days:.0f} days of cover (lead time + {REVIEW_BUFFER_DAYS}d review buffer) at "
        f"{demand_rate:.0f}/day, {needed_qty:.0f} more units are needed beyond the current position.",
    ))
    factors.append(_factor(
        "Budget",
        f"${ctx['budget_remaining']:.0f} remaining allows up to {max_affordable:.0f} units at this supplier's price.",
    ))
    factors.append(_factor(
        "Storage",
        f"{ctx['storage_remaining']:.0f} units of capacity remain at {ctx.get('node', 'the node')}, allowing up "
        f"to {max_storable:.0f} more units of this SKU.",
    ))

    if needed_qty <= 0:
        factors.append(_factor(
            "Recommendation check", "Current position already covers the target window; no purchase is needed."
        ))
        return {
            "decision": "reject", "quantity": 0, "supplier_id": None, "unit_price": None,
            "factors": factors, "needs_human_approval": False, "confidence": "high",
        }

    constrained_qty = min(needed_qty, max_affordable, max_storable)
    binding = min([("need", needed_qty), ("budget", max_affordable), ("storage", max_storable)],
                  key=lambda pair: pair[1])[0]

    if constrained_qty < supplier["min_order_qty"]:
        factors.append(_factor(
            "Feasibility",
            f"Even the most permissive quantity available under current constraints ({constrained_qty:.0f}) falls "
            f"below this supplier's {supplier['min_order_qty']:.0f}-unit minimum order — no compliant quantity can "
            f"be placed automatically.",
        ))
        return {
            "decision": "escalate", "quantity": 0, "supplier_id": supplier["id"], "unit_price": supplier["unit_price"],
            "factors": factors, "needs_human_approval": True, "confidence": "low",
        }

    final_qty = max(constrained_qty, supplier["min_order_qty"])

    if (
        recommended_qty
        and binding == "need"
        and abs(recommended_qty - needed_qty) <= max(ACCEPT_TOLERANCE_RATIO * recommended_qty, 1)
    ):
        decision = "accept"
        final_qty = recommended_qty
        factors.append(_factor(
            "Verdict",
            "The original recommendation is close to the calculated need and no constraint binds tighter — "
            "accepting as-is.",
        ))
    else:
        decision = "modify" if recommended_qty else "recommend_purchase"
        prefix = f"Recommendation of {recommended_qty:.0f} adjusted" if recommended_qty else "No standing recommendation existed"
        if binding != "need":
            factors.append(_factor("Verdict", f"{prefix} — {binding} is the binding constraint, setting the order to {final_qty:.0f} units."))
        else:
            factors.append(_factor("Verdict", f"{prefix} to match the calculated need of {final_qty:.0f} units."))

    cost = final_qty * supplier["unit_price"]
    needs_human_approval = sustained_spike or cost > AUTO_APPROVE_COST_THRESHOLD
    confidence = "high" if (binding == "need" and not sustained_spike) else "medium"

    return {
        "decision": decision,
        "quantity": final_qty,
        "supplier_id": supplier["id"],
        "unit_price": supplier["unit_price"],
        "factors": factors,
        "needs_human_approval": needs_human_approval,
        "confidence": confidence,
    }


def evaluate_shortfall(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Scenario 2: a supplier has just confirmed less than what was ordered."""
    factors = []
    original_qty = ctx["original_qty"]
    confirmed_qty = ctx["confirmed_qty"]
    gap = original_qty - confirmed_qty
    demand_rate = max(ctx["avg_daily_demand"], ctx["forecast_daily_demand"])
    position_after = ctx["current_inventory"] + confirmed_qty + ctx.get("other_open_po_qty", 0)
    days_cover = position_after / demand_rate if demand_rate else float("inf")

    candidates = [s for s in ctx["suppliers"] if s["available_qty"] > 0]
    fastest_lead = min((s["lead_time_days"] for s in candidates), default=float("inf"))

    factors.append(_factor(
        "Shortfall", f"Supplier confirmed {confirmed_qty:.0f} of {original_qty:.0f} units ordered — a gap of {gap:.0f} units."
    ))
    factors.append(_factor(
        "Position after confirmation",
        f"On-hand + confirmed + other incoming = {position_after:.0f} units, ~{days_cover:.1f} days of cover. "
        f"A fresh order placed today would take at least {fastest_lead:.0f} days to arrive.",
    ))

    if days_cover >= fastest_lead + SAFETY_MARGIN_DAYS:
        factors.append(_factor(
            "Verdict",
            "Existing inventory plus the confirmed quantity covers demand until a normal reorder could land — "
            "no further sourcing action needed.",
        ))
        return {
            "decision": "sufficient_as_is", "quantity": confirmed_qty, "supplier_id": ctx.get("original_supplier_id"),
            "unit_price": None, "factors": factors, "needs_human_approval": False, "confidence": "high",
        }

    original_price = ctx.get("original_unit_price")
    alternates = [
        s for s in candidates if s["id"] != ctx.get("original_supplier_id") and s["available_qty"] >= gap
    ]
    if not alternates:
        factors.append(_factor(
            "Alternate sourcing",
            "No alternate supplier currently holds enough available stock to cover the gap before the projected stockout.",
        ))
        return {
            "decision": "escalate", "quantity": gap, "supplier_id": None, "unit_price": None,
            "factors": factors, "needs_human_approval": True, "confidence": "low",
        }

    alt = min(alternates, key=lambda s: s["unit_price"])
    premium = (alt["unit_price"] - original_price) / original_price if original_price else 0
    factors.append(_factor(
        "Alternate sourcing",
        f"{alt['name']} can supply the {gap:.0f}-unit gap (lead time {alt['lead_time_days']:.0f}d) at "
        f"${alt['unit_price']:.2f}/unit, a {premium * 100:.1f}% premium over the original supplier.",
    ))

    gap_cost = gap * alt["unit_price"]
    needs_human_approval = premium > ALT_SUPPLIER_MAX_PREMIUM or gap_cost > AUTO_APPROVE_COST_THRESHOLD
    factors.append(_factor(
        "Verdict",
        "Premium or order value exceeds the auto-approval limit — proposing this for buyer sign-off rather than "
        "placing it automatically." if needs_human_approval else
        "Premium and order value are within auto-approval limits — placing the supplemental order automatically.",
    ))

    return {
        "decision": "sourced_alternate", "quantity": gap, "supplier_id": alt["id"], "unit_price": alt["unit_price"],
        "factors": factors, "needs_human_approval": needs_human_approval, "confidence": "medium",
    }
