"""GET/PUT/DELETE /api/v1/watch/traders — observe wallets (addresses only)."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.routes.envelope import err, ok
from app.traders import COPY_TRADE_ENABLED
from app.traders.compare import compare_report
from app.traders.distill import distill_watch
from app.traders.habits import habit_profile
from app.traders.helius import helius_enabled, reader_mode
from app.traders.snapshot import get_snapshot
from app.traders.store import delete_watch, get_watch, list_watches, upsert_watch

router = APIRouter(prefix="/api/v1/watch", tags=["watch"])


def _watch_payload() -> dict[str, Any]:
    return {
        "items": [i.model_dump() for i in list_watches()],
        "copy_trade_enabled": COPY_TRADE_ENABLED,
        "reader": reader_mode(),
        "helius_enabled": helius_enabled(),
        "liveDisabled": True,
    }


@router.get("/traders")
def get_traders():
    return ok(_watch_payload())


@router.put("/traders")
async def put_traders(request: Request):
    try:
        body = await request.json()
    except Exception:
        return err("BAD_REQUEST", "JSON body required", 400)
    if isinstance(body, list):
        body = {"items": body}
    if not isinstance(body, dict):
        return err("BAD_REQUEST", "object or list of watch items required", 400)
    try:
        upsert_watch(body)
    except ValidationError as e:
        return err("BAD_REQUEST", e.errors()[0].get("msg", str(e)) if e.errors() else str(e), 422)
    except ValueError as e:
        return err("BAD_REQUEST", str(e), 400)
    return ok(_watch_payload())


@router.delete("/traders/{watch_id}")
def delete_trader(watch_id: str):
    if not delete_watch(watch_id):
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    return ok(_watch_payload())


@router.get("/traders/{watch_id}")
def get_trader(watch_id: str):
    item = get_watch(watch_id)
    if item is None:
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    snap = get_snapshot(item.watch_id)
    return ok(
        {
            "item": item.model_dump(),
            "snapshot": snap.model_dump() if snap else None,
            "copy_trade_enabled": COPY_TRADE_ENABLED,
            "liveDisabled": True,
        }
    )


@router.get("/traders/{watch_id}/snapshot")
def get_trader_snapshot(watch_id: str):
    item = get_watch(watch_id)
    if item is None:
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    snap = get_snapshot(item.watch_id)
    if snap is None:
        return err("NOT_FOUND", f"snapshot unavailable: {watch_id}", 404)
    return ok(snap.model_dump())


@router.get("/traders/{watch_id}/habits")
def get_trader_habits(watch_id: str):
    item = get_watch(watch_id)
    if item is None:
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    profile = habit_profile(item.watch_id)
    if profile is None:
        return err("NOT_FOUND", f"habits unavailable: {watch_id}", 404)
    return ok(profile.model_dump())


@router.post("/traders/{watch_id}/distill")
def post_trader_distill(watch_id: str):
    item = get_watch(watch_id)
    if item is None:
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    result = distill_watch(item.watch_id)
    if result is None:
        return err("NOT_FOUND", f"distill unavailable: {watch_id}", 404)
    payload = result.model_dump()
    payload["copy_trade_enabled"] = COPY_TRADE_ENABLED
    payload["applied"] = False
    payload["note"] = "compute-only — POST apply-distill with confirm=true to write paper params"
    return ok(payload)


@router.get("/traders/{watch_id}/compare")
def get_trader_compare(
    watch_id: str,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    item = get_watch(watch_id)
    if item is None:
        return err("NOT_FOUND", f"watch not found: {watch_id}", 404)
    report = compare_report(item.watch_id, from_ts=from_ts, to_ts=to_ts)
    if report is None:
        return err("NOT_FOUND", f"compare unavailable: {watch_id}", 404)
    return ok(report.model_dump())
