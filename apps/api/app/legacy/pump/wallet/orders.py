"""Mainnet order quotes. The server builds an unsigned transaction and broadcasts
only after the user's signature matches that exact message. It never signs.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from app.legacy.pump.wallet.codec import b58decode, b58encode, fee_payer, split_transaction
from app.legacy.pump.wallet.service import (
    HARD_MAX_NOTIONAL_SOL,
    book_stats,
    commit_fill,
    env_mode,
    mainnet_rpc_url,
    order_block_reason,
    prepare_ttl,
    rpc,
)

SLIP_CAP_BPS = 150.0
_RPC_SEND = "sendTransaction"


def _prepared() -> dict[str, dict[str, Any]]:
    from app.legacy.pump.wallet import service

    return service._PREPARED


def _portal_url() -> str:
    raw = os.getenv("PUMPPORTAL_TRADE_LOCAL_URL", "https://pumpportal.fun/api/trade-local").strip()
    return raw or "https://pumpportal.fun/api/trade-local"


def _priority_fee() -> float:
    try:
        fee = float(os.getenv("AUU_WALLET_PRIORITY_FEE", "0.00001"))
    except ValueError:
        return 0.00001
    if fee < 0 or fee > 0.01:
        return 0.00001
    return fee


def fetch_trade_local(payload: dict[str, Any]) -> bytes:
    """POST the local trade API. Returns raw unsigned transaction bytes."""
    from urllib.request import Request, urlopen

    body = json.dumps(payload).encode("utf-8")
    req = Request(
        _portal_url(),
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "AUU-wallet/0.1"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=8) as resp:
            raw = resp.read()
    except Exception as exc:
        raise ValueError("WALLET_ROUTE") from exc
    if not raw or raw[:1] in (b"{", b"["):
        raise ValueError("WALLET_ROUTE")
    return raw


def _slippage_bps(value: Any) -> float:
    if value is None or value == "":
        return SLIP_CAP_BPS
    try:
        bps = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SLIPPAGE") from exc
    if bps <= 0 or bps > SLIP_CAP_BPS + 1e-9:
        raise ValueError("SLIPPAGE")
    return bps


def _mint_bytes(mint: str) -> None:
    try:
        raw = b58decode(mint)
    except ValueError as exc:
        raise ValueError("BAD_MINT") from exc
    if len(raw) != 32:
        raise ValueError("BAD_MINT")


def _price(value: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("BAD_PRICE") from exc
    if price <= 0:
        raise ValueError("BAD_PRICE")
    return price


def recommendation(user_id: Optional[str], mint: str, price_sol: Optional[float]) -> dict[str, Any]:
    from app.legacy.pump.strategies.pump_paper_v1 import (
        PositionState,
        PumpfunPaperSnapshot,
        TapeWindow,
        current_params,
        evaluate,
        target_notional_sol,
    )
    from app.legacy.pump.strategies import pump_paper_v1 as strategy

    params, equity = current_params()
    stats = book_stats(user_id)
    risk = stats["risk"]
    raw = target_notional_sol(equity, params)
    cap = min(float(risk["max_notional_sol"]), HARD_MAX_NOTIONAL_SOL)
    suggested = min(raw, cap)
    price = None
    if price_sol is not None:
        try:
            parsed = float(price_sol)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0:
            price = parsed
    position = None
    wallet_pos = None
    if mint and isinstance(stats["positions"].get(mint), dict):
        stored = stats["positions"][mint]
        qty = float(stored.get("qty") or 0.0)
        entry = float(stored.get("entry_price") or 0.0)
        if qty > 1e-12 and entry > 0:
            position = PositionState(
                mint=mint,
                symbol=mint[:8],
                qty=qty,
                entry_price=entry,
                entry_ts=int(time.time() * 1000),
                entry_notional=float(stored.get("cost_sol") or 0.0),
            )
            mark = price or entry
            wallet_pos = {
                "mint": mint,
                "qty": qty,
                "entry_price": entry,
                "mark": mark,
                "upnl_sol": (mark - entry) * qty,
            }
    signal = None
    if position is not None and price:
        now_ms = int(time.time() * 1000)
        snap = PumpfunPaperSnapshot(
            mint=mint,
            symbol=mint[:8],
            progress_bps=3000,
            virtual_sol_reserves="1",
            virtual_token_reserves="1",
            real_sol_reserves="1",
            real_token_reserves="1",
            token_total_supply="1",
            price_sol=price,
            updated_ts=now_ms,
        )
        signal = evaluate(
            snapshot=snap,
            tape=TapeWindow(),
            params=params,
            now_ms=now_ms,
            impact_entry_bps=0.0,
            position=position,
        )
    elif strategy._engine is not None and mint:
        signal = strategy._engine.last_signal(mint)
    if user_id is None:
        from app.auth.accounts import auth_enabled

        block = "AUTH_OFF" if not auth_enabled() else "AUTH_REQUIRED"
    else:
        block = order_block_reason(user_id)
    take = float(params.take_profit_pct)
    stop = float(params.stop_loss_pct)
    return {
        "strategy_id": "pump-paper-v1",
        "mint": mint or None,
        "side": signal.side if signal is not None else "flat",
        "reason": signal.reason if signal is not None else "levels",
        "entry_price": price,
        "take_profit": (price * (1.0 + take)) if price else None,
        "stop_loss": (price * (1.0 - stop)) if price else None,
        "take_profit_pct": take,
        "stop_loss_pct": stop,
        "suggested_sol": suggested,
        "strategy_sol": raw,
        "slippage_bps": SLIP_CAP_BPS,
        "order_allowed": block is None,
        "real_money": env_mode() == "mainnet",
        "liveEnabled": False,
        "custodial": False,
        "wallet_mode": env_mode(),
        "block": block,
        "day_loss_pct": stats["day_loss_pct"],
        "day_loss_tripped": stats["day_loss_tripped"],
        "open_positions": stats["open_positions"],
        "position": wallet_pos,
        "note": "信号只是建议。真钱单要你本人在钱包里签名，不会自动下单。",
    }


def _guard_buy(user_id: str, mint: str, amount: float) -> None:
    stats = book_stats(user_id)
    cap = float(stats["risk"]["max_notional_sol"])
    if not (0 < amount <= cap + 1e-9) or amount > HARD_MAX_NOTIONAL_SOL + 1e-9:
        raise ValueError("RISK_NOTIONAL")
    if stats["day_loss_tripped"]:
        raise ValueError("DAY_LOSS")
    if mint not in stats["open_mints"] and stats["open_positions"] >= int(stats["risk"]["max_open_positions"]):
        raise ValueError("RISK_POSITIONS")


def prepare_order(
    user_id: str,
    *,
    mint: str,
    side: str,
    notional_sol: Any = None,
    sell_pct: Any = None,
    price_sol: Any = None,
    slippage_bps: Any = None,
) -> dict[str, Any]:
    reason = order_block_reason(user_id)
    if reason:
        raise ValueError(reason)
    if side not in {"buy", "sell"}:
        raise ValueError("BAD_BODY")
    _mint_bytes(mint)
    price = _price(price_sol)
    bps = _slippage_bps(slippage_bps)
    from app.legacy.pump.wallet.service import _user_row

    pubkey = str(_user_row(user_id).get("pubkey") or "")
    stats = book_stats(user_id)
    if side == "buy":
        try:
            amount = float(notional_sol)
        except (TypeError, ValueError) as exc:
            raise ValueError("RISK_NOTIONAL") from exc
        _guard_buy(user_id, mint, amount)
        qty = amount / price
        portal_amount: Any = amount
        denominated = "true"
    else:
        stored = stats["positions"].get(mint)
        if not isinstance(stored, dict) or float(stored.get("qty") or 0.0) <= 1e-12:
            raise ValueError("NO_POSITION")
        try:
            pct = float(100 if sell_pct in (None, "") else sell_pct)
        except (TypeError, ValueError) as exc:
            raise ValueError("BAD_BODY") from exc
        if not (0 < pct <= 100):
            raise ValueError("BAD_BODY")
        qty = float(stored["qty"]) * (pct / 100.0)
        amount = qty * price
        portal_amount = f"{pct:g}%"
        denominated = "false"
    payload = {
        "publicKey": pubkey,
        "action": side,
        "mint": mint,
        "denominatedInSol": denominated,
        "amount": portal_amount,
        "slippage": bps / 100.0,
        "priorityFee": _priority_fee(),
        "pool": "auto",
    }
    raw = fetch_trade_local(payload)
    try:
        signatures, message = split_transaction(raw)
    except ValueError as exc:
        raise ValueError("WALLET_ROUTE") from exc
    if fee_payer(message) != b58decode(pubkey) or any(sig != b"\x00" * 64 for sig in signatures):
        raise ValueError("TAMPER")
    prepare_id = uuid.uuid4().hex
    _prepared()[prepare_id] = {
        "user_id": user_id,
        "pubkey": pubkey,
        "message": message,
        "exp": int(time.time()) + prepare_ttl(),
        "kind": "order",
        "mint": mint,
        "side": side,
        "amount_sol": float(amount),
        "price": price,
        "qty": float(qty),
        "slippage_bps": bps,
    }
    return {
        "prepare_id": prepare_id,
        "expires_at": _prepared()[prepare_id]["exp"],
        "network": "mainnet",
        "tx_base64": base64.b64encode(raw).decode("ascii"),
        "mint": mint,
        "side": side,
        "amount_sol": float(amount),
        "price_sol": price,
        "qty": float(qty),
        "slippage_bps": bps,
        "real_money": True,
        "liveEnabled": False,
        "custodial": False,
        "note": "未签名主网交易。请在你的钱包里确认。服务器不会代签。",
    }


def submit_order(user_id: str, prepare_id: str, signed_tx_b64: str) -> dict[str, Any]:
    prepared = _prepared().get(prepare_id or "")
    if prepared is None or prepared.get("user_id") != user_id or prepared.get("kind") != "order":
        raise ValueError("PREPARE")
    if int(prepared["exp"]) < int(time.time()):
        _prepared().pop(prepare_id, None)
        raise ValueError("PREPARE")
    try:
        raw = base64.b64decode(signed_tx_b64 or "", validate=True)
        signatures, message = split_transaction(raw)
    except Exception as exc:
        raise ValueError("BAD_TX") from exc
    if message != prepared["message"] or fee_payer(message) != b58decode(str(prepared["pubkey"])):
        raise ValueError("TAMPER")
    signature = signatures[0]
    if signature == b"\x00" * 64:
        raise ValueError("BAD_SIGNATURE")
    try:
        VerifyKey(b58decode(str(prepared["pubkey"]))).verify(message, signature)
    except BadSignatureError as exc:
        raise ValueError("BAD_SIGNATURE") from exc
    encoded = b58encode(signature)
    try:
        parsed = rpc(_RPC_SEND, [base64.b64encode(raw).decode("ascii"), {"encoding": "base64"}], mainnet_rpc_url())
    except Exception as exc:
        raise ValueError("WALLET_RPC") from exc
    result = parsed.get("result") if isinstance(parsed, dict) else None
    if result != encoded:
        raise ValueError("WALLET_RPC")
    entry = commit_fill(
        user_id,
        mint=str(prepared["mint"]),
        side=str(prepared["side"]),
        amount_sol=float(prepared["amount_sol"]),
        price=float(prepared["price"]),
        qty=float(prepared["qty"]),
        signature=encoded,
        status="submitted",
    )
    _prepared().pop(prepare_id, None)
    return {"liveEnabled": False, "real_money": True, "custodial": False, "item": _public(entry)}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    from app.legacy.pump.wallet.service import _public_entry

    return _public_entry(row)
