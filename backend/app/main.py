from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, db, tools

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title="Purchasing Agent")


@app.on_event("startup")
def _startup():
    db.reset_and_seed()


class RejectBody(BaseModel):
    reason: str = ""


@app.get("/api/situations")
def api_list_situations():
    return tools.list_situations()


@app.get("/api/situations/{situation_id}")
def api_get_situation(situation_id: str):
    situation = tools.get_situation(situation_id)
    if not situation:
        raise HTTPException(404, "situation not found")
    situation["audit_log"] = tools.get_audit_log(situation_id)
    return situation


@app.post("/api/situations/{situation_id}/run")
def api_run_situation(situation_id: str):
    try:
        return agent.run_situation(situation_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/purchase-orders")
def api_list_pos():
    return tools.list_purchase_orders()


@app.get("/api/approvals")
def api_list_approvals():
    return tools.list_approvals()


@app.post("/api/approvals/{approval_id}/approve")
def api_approve(approval_id: str):
    try:
        return agent.apply_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/approvals/{approval_id}/reject")
def api_reject(approval_id: str, body: RejectBody = RejectBody()):
    try:
        return agent.reject_approval(approval_id, body.reason)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
