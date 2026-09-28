import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import agent, db, tools


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    yield


def test_recommendation_modified_by_demand_math_and_storage_then_self_corrects():
    result = agent.run_situation("SIT-1001")
    assert result["decision"] == "modify"
    assert result["status"] == "resolved_with_adjustment"
    assert result["validation"]["adjusted"] is True
    # storage capacity shifts between decision and validation (300 -> 260 available)
    assert result["validation"]["final_quantity"] == 260


def test_recommendation_accepted_when_already_well_calibrated():
    result = agent.run_situation("SIT-1002")
    assert result["decision"] == "accept"
    assert result["status"] == "resolved"
    assert result["quantity"] == 150


def test_supplier_shortfall_sources_alternate_automatically():
    result = agent.run_situation("SIT-2001")
    assert result["decision"] == "sourced_alternate"
    assert result["status"] == "resolved"
    assert result["quantity"] == 250
    assert result["supplier_id"] == "SUP-B2"


def test_demand_spike_flagged_for_human_approval_then_resolves_on_approve():
    result = agent.run_situation("SIT-3001")
    assert result["decision"] == "recommend_purchase"
    assert result["needs_human_approval"] is True
    assert result["status"] == "pending_approval"

    approval = next(a for a in tools.list_approvals() if a["situation_id"] == "SIT-3001")
    outcome = agent.apply_approval(approval["id"])
    assert outcome["status"] in ("resolved", "resolved_with_adjustment")


def test_purchasing_constraint_escalates_when_no_feasible_quantity():
    result = agent.run_situation("SIT-4001")
    assert result["decision"] == "escalate"
    assert result["status"] == "escalated"
    assert result["quantity"] == 0


def test_rejecting_an_escalation_marks_it_rejected():
    agent.run_situation("SIT-4001")
    approval = next(a for a in tools.list_approvals() if a["situation_id"] == "SIT-4001")
    outcome = agent.reject_approval(approval["id"], reason="Budget exception denied this cycle")
    assert outcome["status"] == "rejected"


def test_shortfall_escalates_when_no_alternate_supplier_available():
    from app import policy

    ctx = {
        "original_qty": 500, "confirmed_qty": 250, "avg_daily_demand": 65, "forecast_daily_demand": 65,
        "current_inventory": 10, "other_open_po_qty": 0,
        "suppliers": [
            {"id": "SUP-X", "sku": "SKU-X", "name": "Only Supplier", "lead_time_days": 8,
             "min_order_qty": 100, "unit_price": 3.0, "reliability_score": 0.9, "available_qty": 0},
        ],
        "original_supplier_id": "SUP-X", "original_unit_price": 3.0,
    }
    result = policy.evaluate_shortfall(ctx)
    assert result["decision"] == "escalate"
    assert result["needs_human_approval"] is True


def test_investigate_when_spike_is_a_single_day_blip_not_sustained():
    from app import policy

    ctx = {
        "recommended_qty": 500, "avg_daily_demand": 30, "forecast_daily_demand": 30,
        "trailing_daily_actuals": [30, 29, 31, 120, 29, 30, 31],  # one-off blip, last 3 days back to normal
        "current_inventory": 200, "open_po_qty": 0,
        "suppliers": [
            {"id": "SUP-Y", "sku": "SKU-Y", "name": "Supplier Y", "lead_time_days": 9,
             "min_order_qty": 200, "unit_price": 1.2, "reliability_score": 0.9, "available_qty": 2000},
        ],
        "budget_remaining": 15000, "storage_remaining": 1200, "storage_units_per_item": 0.2, "node": "DC-Mumbai-1",
    }
    result = policy.evaluate_purchase(ctx)
    assert result["decision"] == "investigate"
    assert result["needs_human_approval"] is True
