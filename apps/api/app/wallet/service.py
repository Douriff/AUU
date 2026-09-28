"""Pubkey binding, per-user risk, and a devnet memo the user must sign.

The process stores public keys and transaction signatures. It does not hold
signing keys and it does not broadcast.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from app.wallet.codec import b58decode, b58encode, build_memo_message, unsigned_transaction

HARD_MAX_NOTIONAL_SOL = 1.0
HARD_MAX_DAY_LOSS_PCT = 0.045
HARD_MAX_OPEN_POSITIONS = 10
RISK_VERSION = "wallet-risk-v1"
RISK_TEXT = (
    "非托管钱包风险提示 v1。"
    "平台只保存公钥，不保存私钥，不托管资金，不代替签名，不自动下单。"
    "每一笔链上交易都要你在自己的钱包里确认，交易不可撤销，网络费从你的钱包支付。"
    "当前只开放 devnet 测试，不是真钱。主网默认关闭。"
    "平台硬上限是单笔 1 SOL、日亏 4.5%、同时持仓 10。你只能把限额调得更严。"
)

_LOCK = threading.RLock()
_STATE: Optional[dict[str, Any]] = None
_CHALLENGES: dict[str, dict[str, Any]] = {}
_PREPARED: dict[str, dict[str, Any]] = {}


def reset_wallet() -> None:
    global _STATE
    with _LOCK:
        _STATE = None
        _CHALLENGES.clear()
        _PREPARED.clear()


def env_mode() -> str:
    raw = os.getenv("AUU_WALLET_MODE", "off").strip().lower()
    if raw in {"off", "devnet", "mainnet"}:
        return raw
    return "off"


def devnet_rpc_url() -> str:
    raw = os.getenv("SOLANA_RPC_URL_DEVNET", "https://api.devnet.solana.com").strip()
    return raw or "https://api.devnet.solana.com"


def _store_path() -> Path:
    raw = (os.getenv("AUU_WALLET_STORE") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "wallets.json"


def _challenge_ttl() -> int:
    try:
        return int(os.getenv("AUU_WALLET_CHALLENGE_TTL", "300"))
    except ValueError:
        return 300


def _blank() -> dict[str, Any]:
    return {"global_halt": False, "global_halt_ts": 0, "users": {}, "ledger": []}


def _load() -> dict[str, Any]:
    global _STATE
    with _LOCK:
        if _STATE is not None:
            return _STATE
        path = _store_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = _blank()
        if not isinstance(raw, dict):
            raw = _blank()
        raw.setdefault("global_halt", False)
        raw.setdefault("global_halt_ts", 0)
        raw.setdefault("users", {})
        raw.setdefault("ledger", [])
        _STATE = raw
        return _STATE


def _save() -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_load())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _default_risk() -> dict[str, Any]:
    return {
        "max_notional_sol": HARD_MAX_NOTIONAL_SOL,
        "max_day_loss_pct": HARD_MAX_DAY_LOSS_PCT,
        "max_open_positions": HARD_MAX_OPEN_POSITIONS,
    }


def _user_row(user_id: str) -> dict[str, Any]:
    users = _load()["users"]
    row = users.get(user_id)
    if not isinstance(row, dict):
        row = {
            "pubkey": "",
            "bound_ts": 0,
            "wallet_enabled": False,
            "consent": None,
            "risk": _default_risk(),
        }
        users[user_id] = row
    row.setdefault("risk", _default_risk())
    return row


def normalize_risk(notional: float, day_loss: float, positions: int) -> dict[str, Any]:
    try:
        sol = float(notional)
        day = float(day_loss)
        slots = int(positions)
    except (TypeError, ValueError) as exc:
        raise ValueError("BAD_RISK") from exc
    if not (0 < sol <= HARD_MAX_NOTIONAL_SOL + 1e-9):
        raise ValueError("RISK_NOTIONAL")
    if not (0 < day <= HARD_MAX_DAY_LOSS_PCT + 1e-9):
        raise ValueError("RISK_DAY_LOSS")
    if not (1 <= slots <= HARD_MAX_OPEN_POSITIONS):
        raise ValueError("RISK_POSITIONS")
    return {
        "max_notional_sol": sol,
        "max_day_loss_pct": day,
        "max_open_positions": slots,
    }


def check_notional(user_id: str, amount_sol: float) -> None:
    row = _user_row(user_id)
    cap = float(row["risk"]["max_notional_sol"])
    if float(amount_sol) > cap + 1e-9:
        raise ValueError("RISK_NOTIONAL")


def _pubkey_bytes(text: str) -> bytes:
    try:
        raw = b58decode(text)
    except ValueError as exc:
        raise ValueError("BAD_PUBKEY") from exc
    if len(raw) != 32:
        raise ValueError("BAD_PUBKEY")
    return raw


def issue_challenge(user_id: str, name: str, pubkey: str) -> dict[str, Any]:
    _pubkey_bytes(pubkey)
    nonce = uuid.uuid4().hex
    exp = int(time.time()) + _challenge_ttl()
    message = "\n".join(
        [
            "AUU 非托管钱包绑定",
            f"user: {name}",
            f"pubkey: {pubkey}",
            f"nonce: {nonce}",
            f"exp: {exp}",
            "network: devnet",
            "此签名只绑定公钥，不授权转账或下单。",
        ]
    )
    _CHALLENGES[nonce] = {
        "user_id": user_id,
        "pubkey": pubkey,
        "exp": exp,
        "message": message,
    }
    return {
        "nonce": nonce,
        "exp": exp,
        "message": message,
        "network": "devnet",
        "liveEnabled": False,
    }


def bind_pubkey(user_id: str, pubkey: str, nonce: str, signature_b64: str) -> dict[str, Any]:
    challenge = _CHALLENGES.get(nonce or "")
    if challenge is None or int(challenge["exp"]) < int(time.time()):
        _CHALLENGES.pop(nonce, None)
        raise ValueError("CHALLENGE")
    if challenge["user_id"] != user_id or challenge["pubkey"] != pubkey:
        raise ValueError("CHALLENGE")
    try:
        signature = base64.b64decode(signature_b64 or "", validate=True)
    except Exception as exc:
        raise ValueError("BAD_SIGNATURE") from exc
    if len(signature) != 64:
        raise ValueError("BAD_SIGNATURE")
    pubkey_raw = _pubkey_bytes(pubkey)
    try:
        VerifyKey(pubkey_raw).verify(challenge["message"].encode("utf-8"), signature)
    except BadSignatureError as exc:
        raise ValueError("BAD_SIGNATURE") from exc
    state = _load()
    for other_id, other in state["users"].items():
        if other_id != user_id and str(other.get("pubkey") or "") == pubkey:
            raise ValueError("PUBKEY_TAKEN")
    row = _user_row(user_id)
    row["pubkey"] = pubkey
    row["bound_ts"] = int(time.time() * 1000)
    row["wallet_enabled"] = False
    with _LOCK:
        _save()
    _CHALLENGES.pop(nonce, None)
    return status_for(user_id)


def unbind(user_id: str) -> dict[str, Any]:
    row = _user_row(user_id)
    row["pubkey"] = ""
    row["bound_ts"] = 0
    row["wallet_enabled"] = False
    with _LOCK:
        _save()
    return status_for(user_id)


def update_risk(user_id: str, notional: float, day_loss: float, positions: int) -> dict[str, Any]:
    row = _user_row(user_id)
    row["risk"] = normalize_risk(notional, day_loss, positions)
    with _LOCK:
        _save()
    return status_for(user_id)


def set_wallet_mode(user_id: str, enabled: bool, accept_risk: bool) -> dict[str, Any]:
    row = _user_row(user_id)
    if enabled:
        if _load().get("global_halt"):
            raise ValueError("WALLET_HALT")
        if env_mode() == "off":
            raise ValueError("WALLET_OFF")
        if not row.get("pubkey"):
            raise ValueError("NOT_BOUND")
        if not accept_risk:
            raise ValueError("RISK_CONSENT")
        row["consent"] = {"version": RISK_VERSION, "ts": int(time.time() * 1000)}
        row["wallet_enabled"] = True
    else:
        row["wallet_enabled"] = False
    with _LOCK:
        _save()
    return status_for(user_id)


def set_global_halt(halt: bool) -> dict[str, Any]:
    state = _load()
    state["global_halt"] = bool(halt)
    state["global_halt_ts"] = int(time.time() * 1000) if halt else 0
    if halt:
        for row in state["users"].values():
            if isinstance(row, dict):
                row["wallet_enabled"] = False
    with _LOCK:
        _save()
    return {"global_halt": bool(state["global_halt"]), "liveEnabled": False, "mode": "paper"}


def _tx_block_reason(user_id: str) -> Optional[str]:
    if _load().get("global_halt"):
        return "WALLET_HALT"
    mode = env_mode()
    if mode == "off":
        return "WALLET_OFF"
    if mode != "devnet":
        return "WALLET_DEVNET_ONLY"
    row = _user_row(user_id)
    if not row.get("pubkey"):
        return "NOT_BOUND"
    if not row.get("wallet_enabled") or not isinstance(row.get("consent"), dict):
        return "WALLET_MODE_OFF"
    return None


def status_for(user_id: Optional[str]) -> dict[str, Any]:
    state = _load()
    mode = env_mode()
    body: dict[str, Any] = {
        "liveEnabled": False,
        "liveDisabled": True,
        "mode": "paper",
        "wallet_mode": mode,
        "network": "devnet" if mode == "devnet" else None,
        "global_halt": bool(state.get("global_halt")),
        "tx_allowed": False,
        "bound": False,
        "pubkey": None,
        "wallet_enabled": False,
        "consent": None,
        "risk": _default_risk(),
        "hard_limits": _default_risk(),
        "risk_version": RISK_VERSION,
        "risk_text": RISK_TEXT,
        "custodial": False,
    }
    if not user_id:
        return body
    row = _user_row(user_id)
    pubkey = str(row.get("pubkey") or "")
    body["bound"] = bool(pubkey)
    body["pubkey"] = pubkey or None
    body["wallet_enabled"] = bool(row.get("wallet_enabled"))
    body["consent"] = row.get("consent")
    body["risk"] = dict(row.get("risk") or _default_risk())
    body["tx_allowed"] = _tx_block_reason(user_id) is None
    return body


def rpc(method: str, params: list[Any]) -> Any:
    import json as _json
    from urllib.request import Request, urlopen

    payload = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = Request(
        devnet_rpc_url(),
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "AUU-wallet/0.1"},
        method="POST",
    )
    with urlopen(req, timeout=4) as resp:
        parsed = _json.loads(resp.read().decode("utf-8"))
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError("rpc")
    return parsed


def prepare_memo(user_id: str) -> dict[str, Any]:
    reason = _tx_block_reason(user_id)
    if reason:
        raise ValueError(reason)
    check_notional(user_id, 0.0)
    row = _user_row(user_id)
    pubkey = str(row["pubkey"])
    try:
        block = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        blockhash = str(block["result"]["value"]["blockhash"])
        blockhash_raw = b58decode(blockhash)
    except Exception as exc:
        raise ValueError("WALLET_RPC") from exc
    if len(blockhash_raw) != 32:
        raise ValueError("WALLET_RPC")
    memo = "AUU devnet wallet check"
    message = build_memo_message(_pubkey_bytes(pubkey), blockhash_raw, memo)
    raw_tx = unsigned_transaction(message)
    prepare_id = uuid.uuid4().hex
    _PREPARED[prepare_id] = {
        "user_id": user_id,
        "pubkey": pubkey,
        "message": message,
        "exp": int(time.time()) + 90,
        "amount_sol": 0.0,
        "kind": "memo",
    }
    return {
        "prepare_id": prepare_id,
        "network": "devnet",
        "kind": "memo",
        "amount_sol": 0.0,
        "tx_base64": base64.b64encode(raw_tx).decode("ascii"),
        "liveEnabled": False,
        "custodial": False,
        "note": "未签名交易。请用你的钱包签名并发送，平台不会代签。",
    }


def _signature_status(signature: str) -> str:
    try:
        parsed = rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        value = (parsed.get("result") or {}).get("value") or [None]
        row = value[0] if value else None
    except Exception:
        return "submitted"
    if not isinstance(row, dict):
        return "submitted"
    if row.get("err"):
        return "failed"
    status = str(row.get("confirmationStatus") or "submitted")
    if status not in {"processed", "confirmed", "finalized"}:
        return "submitted"
    return status


def record_signed(user_id: str, prepare_id: str, signature_b58: str) -> dict[str, Any]:
    prepared = _PREPARED.get(prepare_id or "")
    if prepared is None or int(prepared["exp"]) < int(time.time()) or prepared["user_id"] != user_id:
        raise ValueError("PREPARE")
    try:
        signature = b58decode(signature_b58 or "")
    except ValueError as exc:
        raise ValueError("BAD_SIGNATURE") from exc
    if len(signature) != 64:
        raise ValueError("BAD_SIGNATURE")
    try:
        VerifyKey(_pubkey_bytes(prepared["pubkey"])).verify(prepared["message"], signature)
    except BadSignatureError as exc:
        raise ValueError("BAD_SIGNATURE") from exc
    status = _signature_status(signature_b58)
    entry = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "pubkey": prepared["pubkey"],
        "network": "devnet",
        "signature": signature_b58,
        "status": status,
        "amount_sol": 0.0,
        "kind": "memo",
        "ts": int(time.time() * 1000),
    }
    _load()["ledger"].append(entry)
    with _LOCK:
        _save()
    _PREPARED.pop(prepare_id, None)
    return {"liveEnabled": False, "item": _public_entry(entry)}


def ledger_for(user_id: str, limit: int = 50) -> dict[str, Any]:
    rows = [row for row in _load()["ledger"] if row.get("user_id") == user_id]
    rows = rows[-max(1, min(int(limit), 100)) :]
    rows.reverse()
    changed = False
    for row in rows:
        if row.get("status") in {"confirmed", "finalized", "failed"}:
            continue
        nxt = _signature_status(str(row.get("signature") or ""))
        if nxt != row.get("status"):
            row["status"] = nxt
            changed = True
    if changed:
        with _LOCK:
            _save()
    return {
        "liveEnabled": False,
        "network": "devnet",
        "items": [_public_entry(row) for row in rows],
    }


def _public_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "pubkey": row.get("pubkey"),
        "network": row.get("network"),
        "signature": row.get("signature"),
        "status": row.get("status"),
        "amount_sol": float(row.get("amount_sol") or 0.0),
        "kind": row.get("kind") or "memo",
        "ts": int(row.get("ts") or 0),
    }
