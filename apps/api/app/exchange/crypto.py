"""Envelope encryption for exchange credentials.

Master key: AUU_EXCHANGE_MASTER_KEY (urlsafe base64 of 32 random bytes) from the server env
file (/etc/auu/auu.env). Never in repo, never logged. Each credential gets a random 256-bit
data key (DEK); the secret JSON is sealed with AES-256-GCM under the DEK, and the DEK is
wrapped with AES-256-GCM under the master key. Only ciphertext is stored.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV_MASTER = "AUU_EXCHANGE_MASTER_KEY"
KEY_VERSION = 1


class CryptoUnavailable(RuntimeError):
    """No usable master key configured: binding is refused (fail closed)."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


def _master() -> bytes:
    raw = (os.getenv(ENV_MASTER) or "").strip()
    if not raw:
        raise CryptoUnavailable("master key missing")
    try:
        key = _b64d(raw)
    except Exception as exc:  # noqa: BLE001
        raise CryptoUnavailable("master key invalid") from exc
    if len(key) != 32:
        raise CryptoUnavailable("master key invalid")
    return key


def available() -> bool:
    try:
        _master()
        return True
    except CryptoUnavailable:
        return False


def seal(payload: dict[str, Any], aad: str) -> dict[str, Any]:
    master = _master()
    dek = AESGCM.generate_key(bit_length=256)
    n1, n2 = os.urandom(12), os.urandom(12)
    ct = AESGCM(dek).encrypt(n1, json.dumps(payload).encode(), aad.encode())
    wrapped = AESGCM(master).encrypt(n2, dek, aad.encode())
    return {"v": KEY_VERSION, "n": _b64e(n1), "ct": _b64e(ct), "wn": _b64e(n2), "wk": _b64e(wrapped)}


def open_(blob: dict[str, Any], aad: str) -> dict[str, Any]:
    master = _master()
    dek = AESGCM(master).decrypt(_b64d(blob["wn"]), _b64d(blob["wk"]), aad.encode())
    pt = AESGCM(dek).decrypt(_b64d(blob["n"]), _b64d(blob["ct"]), aad.encode())
    return json.loads(pt.decode())


def mask(key: str) -> str:
    k = key or ""
    return (k[:4] + "…" + k[-4:]) if len(k) > 10 else "…" + k[-2:]
