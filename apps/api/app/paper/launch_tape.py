"""Record-only launch tapes: every logs-discovered pump mint from its ``Create``.

For each mint registered from a pump ``Create`` event this keeps a compact
tape of its real prints (the ones the logs feed already delivers) from the
Create until ``AUU_LAUNCH_TAPE_MAX_S`` (default 180 s), graduation, or the
provider dropping the mint (watch-list eviction), then appends one JSONL
line to ``data/evidence/launch_tapes.jsonl`` (rotated:
``AUU_LAUNCH_TAPE_MAX_MB`` default 32 x ``AUU_LAUNCH_TAPE_KEEP`` default 8).

It exists so launch-time strategies (liquidity-speed entry, creator-buy
entry, launch-block vetoes, creator-quality filters) can be simulated offline
by :mod:`app.paper.launch_study` with print-level fills. It never trades,
never feeds a decision and never calls the network. ``AUU_LAUNCH_TAPE=off``
disables it; under unit tests it is off unless set to ``on``.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.paper.launch_tape")

LAUNCH_TAPE_VERSION = 1
FIELDS = ("dt_ms", "side", "sol", "vs", "vt", "slot_off", "who")
MAX_ROWS = 3_000
MAX_OPEN = 400
_VT_OFFSET = 1_073_000_000_000_000 - 793_100_000_000_000


def _env_int(name: str, default: int, lo: int) -> int:
    try:
        return max(lo, int(os.getenv(name) or default))
    except ValueError:
        return default


def launch_tape_enabled() -> bool:
    from app.data_paths import tests_active

    raw = (os.getenv("AUU_LAUNCH_TAPE") or "").strip().lower()
    if raw:
        return raw not in {"0", "off", "false", "no"}
    return not tests_active()


def launch_tape_path() -> Path:
    raw = (os.getenv("LAUNCH_TAPE_STORE") or "").strip()
    return Path(raw).expanduser() if raw else data_dir() / "evidence" / "launch_tapes.jsonl"


class _Open:
    __slots__ = ("mint", "creator", "t0", "slot", "name", "symbol", "rows", "capped", "creator_first_buy_sol")

    def __init__(self, mint: str, creator: str, t0: int) -> None:
        self.mint = mint
        self.creator = creator
        self.t0 = t0
        self.slot: Optional[int] = None
        self.name: Optional[str] = None
        self.symbol: Optional[str] = None
        self.rows: list[list[Any]] = []
        self.capped = False


class LaunchTapeRecorder:
    def __init__(self, writer: Any = None, *, max_age_ms: Optional[int] = None, clock=None, background: bool = True) -> None:
        from app.paper.evidence import EvidenceWriter

        self._writer = writer or EvidenceWriter(
            launch_tape_path(),
            max_bytes=lambda: _env_int("AUU_LAUNCH_TAPE_MAX_MB", 32, 1) * 1024 * 1024,
            keep=lambda: _env_int("AUU_LAUNCH_TAPE_KEEP", 8, 0),
        )
        self.max_age_ms = max_age_ms if max_age_ms is not None else _env_int("AUU_LAUNCH_TAPE_MAX_S", 180, 10) * 1000
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._open: "OrderedDict[str, _Open]" = OrderedDict()
        self._lock = threading.Lock()
        self.counters = {"opened": 0, "written": 0, "dropped_queue": 0, "dropped_open_cap": 0, "errors": 0}
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=5_000)
        self._background = background
        if background:
            threading.Thread(target=self._drain, name="auu-launch-tape", daemon=True).start()

    # ------------------------------------------------------------ hooks
    def on_register(self, mint: str, *, creator: str, source: str, registered_ts: int) -> None:
        if source != "logs" or not mint:
            return
        done = []
        with self._lock:
            if mint in self._open:
                return
            self._open[mint] = _Open(mint, creator or "", int(registered_ts))
            self.counters["opened"] += 1
            while len(self._open) > MAX_OPEN:
                _m, ep = self._open.popitem(last=False)
                done.append((ep, "open_cap"))
                self.counters["dropped_open_cap"] += 1
        for ep, why in done:
            self._emit(ep, why)

    def note_meta(self, mint: str, *, slot: Any = None, name: Any = None, symbol: Any = None) -> None:
        with self._lock:
            ep = self._open.get(mint)
            if ep is None:
                return
            if slot is not None and ep.slot is None:
                try:
                    ep.slot = int(slot)
                except (TypeError, ValueError):
                    pass
            if name is not None:
                ep.name = str(name)[:64]
            if symbol is not None:
                ep.symbol = str(symbol)[:32]

    def on_print(self, mint: str, *, ts: int, side: str, sol: float, vs: int, vt: int, slot: Any, trader: Any) -> None:
        ended = None
        with self._lock:
            ep = self._open.get(mint)
            if ep is None:
                return
            if len(ep.rows) >= MAX_ROWS:
                ep.capped = True
            else:
                try:
                    off = int(slot) - ep.slot if slot is not None and ep.slot is not None else None
                except (TypeError, ValueError):
                    off = None
                ep.rows.append(
                    [int(ts) - ep.t0, 1 if side == "buy" else -1, round(float(sol or 0.0), 6), int(vs), int(vt), off, (str(trader or "")[:8] or None)]
                )
            if int(vt) - _VT_OFFSET <= 0:
                ended = self._open.pop(mint)
        if ended is not None:
            self._emit(ended, "graduated")
        self.expire()

    def on_drop(self, mint: str, reason: str = "evicted") -> None:
        with self._lock:
            ep = self._open.pop(mint, None)
        if ep is not None:
            self._emit(ep, reason)

    def expire(self, now_ms: Optional[int] = None) -> int:
        now = int(now_ms if now_ms is not None else self._clock())
        done = []
        with self._lock:
            while self._open:
                mint, ep = next(iter(self._open.items()))
                if now - ep.t0 < self.max_age_ms:
                    break
                self._open.popitem(last=False)
                done.append(ep)
        for ep in done:
            self._emit(ep, "max_age")
        return len(done)

    def flush(self) -> None:
        """Tests / shutdown: write everything still queued (synchronously)."""
        while True:
            try:
                rec = self._q.get_nowait()
            except queue.Empty:
                return
            self._write(rec)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"open": len(self._open), "queued": self._q.qsize(), **self.counters}

    # -------------------------------------------------------- internals
    def _emit(self, ep: _Open, reason: str) -> None:
        rec = {
            "kind": "launch_tape",
            "v": LAUNCH_TAPE_VERSION,
            "mint": ep.mint,
            "creator": ep.creator or None,
            "creator8": (ep.creator or "")[:8] or None,
            "name": ep.name,
            "symbol": ep.symbol,
            "t0": ep.t0,
            "created_slot": ep.slot,
            "end_reason": reason,
            "capped": ep.capped,
            "fields": list(FIELDS),
            "rows": ep.rows,
        }
        if not self._background:
            self._write(rec)
            return
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            self.counters["dropped_queue"] += 1

    def _write(self, rec: dict) -> None:
        try:
            self._writer.write(rec)
            self.counters["written"] += 1
        except Exception:
            self.counters["errors"] += 1
            log.exception("launch tape write failed")

    def _drain(self) -> None:
        while True:
            rec = self._q.get()
            self._write(rec)


_rec: Optional[LaunchTapeRecorder] = None
_rec_lock = threading.Lock()


def get_launch_recorder() -> Optional[LaunchTapeRecorder]:
    global _rec
    if not launch_tape_enabled():
        return None
    with _rec_lock:
        if _rec is None:
            _rec = LaunchTapeRecorder()
        return _rec


def set_launch_recorder(rec: Optional[LaunchTapeRecorder]) -> None:
    global _rec
    with _rec_lock:
        _rec = rec


def launch_hook(_method: str, /, *args: Any, **kwargs: Any) -> None:
    """Call ``LaunchTapeRecorder.<_method>``; errors are logged and swallowed."""
    try:
        rec = get_launch_recorder()
        if rec is not None:
            getattr(rec, _method)(*args, **kwargs)
    except Exception:
        log.debug("launch tape hook %s failed", _method, exc_info=True)
