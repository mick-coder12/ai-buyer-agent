"""Small evaluation harness: runs every seeded situation through the agent
and checks the outcome against what's expected, per the brief — not a
sophisticated framework, just a readable scorecard.

Run with: python tests/evaluate.py   (from the backend/ directory)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, db

EXPECTATIONS = {
    "SIT-1001": {"decision": "modify", "status": "resolved_with_adjustment"},
    "SIT-1002": {"decision": "accept", "status": "resolved"},
    "SIT-2001": {"decision": "sourced_alternate", "status": "resolved"},
    "SIT-3001": {"decision": "recommend_purchase", "status": "pending_approval"},
    "SIT-4001": {"decision": "escalate", "status": "escalated"},
}


def main():
    db.reset_and_seed()
    rows = []
    for situation_id, expected in EXPECTATIONS.items():
        result = agent.run_situation(situation_id)
        validated = result["validation"] is None or result["validation"]["ok"]
        rows.append({
            "situation": situation_id,
            "decision": result["decision"],
            "decision_ok": result["decision"] == expected["decision"],
            "status": result["status"],
            "status_ok": result["status"] == expected["status"],
            "took_action": result["purchase_order_id"] is not None,
            "validated": validated,
            "escalated_to_human": result["needs_human_approval"],
        })

    cols = [("situation", 10), ("decision", 18), ("decision_ok", 7), ("status", 22),
            ("status_ok", 7), ("took_action", 12), ("validated", 10), ("escalated_to_human", 10)]
    header = " ".join(f"{name:<{w}}" for name, w in cols)
    print(header)
    print("-" * len(header))
    all_ok = True
    for r in rows:
        all_ok = all_ok and r["decision_ok"] and r["status_ok"]
        print(" ".join(f"{str(r[name]):<{w}}" for name, w in cols))

    print()
    print("All scenarios matched expected decision + status." if all_ok
          else "Some scenarios diverged from expectations — see table above.")


if __name__ == "__main__":
    main()
