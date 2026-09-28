"""Base58 and an unsigned legacy memo transaction. No signing helpers."""
from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
MEMO_PROGRAM_B58 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def b58decode(text: str) -> bytes:
    raw = (text or "").strip()
    if not raw or any(char not in _INDEX for char in raw):
        raise ValueError("BAD_PUBKEY")
    number = 0
    for char in raw:
        number = number * 58 + _INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = 0
    for char in raw:
        if char != "1":
            break
        pad += 1
    return b"\x00" * pad + body


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(_ALPHABET[rem])
    pad = 0
    for byte in raw:
        if byte != 0:
            break
        pad += 1
    if not chars:
        return "1" * (pad or 1)
    return ("1" * pad) + "".join(reversed(chars))


def _compact(n: int) -> bytes:
    if n < 0:
        raise ValueError("BAD_TX")
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([(n & 0x7F) | 0x80, (n >> 7) & 0x7F])
    raise ValueError("BAD_TX")


def memo_program() -> bytes:
    program = b58decode(MEMO_PROGRAM_B58)
    if len(program) != 32:
        raise ValueError("BAD_TX")
    return program


def build_memo_message(fee_payer: bytes, blockhash: bytes, memo: str) -> bytes:
    """Legacy message: one signer, memo program readonly, no extra accounts."""
    if len(fee_payer) != 32 or len(blockhash) != 32:
        raise ValueError("BAD_TX")
    data = memo.encode("utf-8")
    if not data or len(data) > 256:
        raise ValueError("BAD_TX")
    accounts = fee_payer + memo_program()
    instruction = bytes([1]) + _compact(0) + _compact(len(data)) + data
    return bytes([1, 0, 1]) + _compact(2) + accounts + blockhash + _compact(1) + instruction


def unsigned_transaction(message: bytes) -> bytes:
    """One empty signature slot plus the message. The wallet fills the signature."""
    return _compact(1) + (b"\x00" * 64) + message
