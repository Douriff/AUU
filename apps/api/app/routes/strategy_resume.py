"""One-time admin seal for a strategy outage. Does not place orders or invent gap trades."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth.accounts import auth_enabled
from app.paper.strategy_runner import RESUME_CONFIRM, resume_forward
from app.routes.auth import session_user
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["strategy-resume"])


class ResumeBody(BaseModel):
    confirm: str = ""
    note: str = Field(default="", max_length=200)


@router.post("/strategy/resume")
def strategy_resume(body: ResumeBody, request: Request):
    if not auth_enabled():
        return err("AUTH_REQUIRED", "需要管理员登录后才能封存缺口", 403)
    actor = session_user(request)
    if actor is None or not actor.get("is_admin"):
        return err("FORBIDDEN", "只有管理员可以封存策略缺口", 403)
    if body.confirm != RESUME_CONFIRM:
        return err("CONFIRM", "确认短语不正确", 400)
    try:
        out = resume_forward(now_ms=int(time.time() * 1000), by=str(actor["id"]), note=body.note)
    except ValueError as exc:
        return err(str(exc), "请求无效", 400)
    if not out.get("ok"):
        return err(str(out.get("code") or "RESUME"), str(out.get("message") or "未能封存"), 409)
    return ok(out)
