"""Background lookup of holder-structure / token-safety factors at entry (record only).

``submit`` is called from the evidence ``on_signal`` hook. It only enqueues a
job (no I/O, no ledger work) and returns; a small worker pool computes

* tape factors from the provider's logs-feed trade ledger, cut at the signal
  time (dev %, top-10 %, holder count, creation-slot bundle, early snipers);
* RPC factors from the public Solana RPC (mint / freeze authority, and the
  token accounts of the mint: holder count, top-10 excl. the bonding curve,
  dev %), rate-limited, with a per-call timeout and a per-mint cache.

The evidence record picks the result up when the trade is written (exit +
30 s). Nothing is read back by the strategy; decisions, params and
liveEnabled are untouched. No paid API or key is used.

Env: ``AUU_ENTRY_FACTORS=off`` disables everything, ``AUU_ENTRY_FACTORS_RPC=off``
only the RPC part; ``AUU_ENTRY_FACTORS_RPS`` (default 2 req/s),
``AUU_ENTRY_FACTORS_TIMEOUT_S`` (default 4 s per call). The RPC URL is the
one the feed already uses (``LIVE_PAPER_RPC_URL`` / ``SOLANA_RPC_URL``); it
is never logged. Under unit tests the RPC is off unless a fake is injected.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Optional

from app.paper.holders import (
    decode_owner_amount_rows,
    holder_stats_from_accounts,
    parse_mint_account,
    tape_holder_factors,
    token_accounts_gpa_params,
)
from app.providers.pumpfun_curve_math import TOKEN_TOTAL_SUPPLY

log = logging.getLogger("auu.paper.entry_factors")

FACTORS_VERSION = 1
RpcCall = Callable[[str, list[Any]], Any]

MAX_RESULTS = 512
MAX_MINT_CACHE = 1024
HOLDERS_TTL_MS = 15_000
# A job that starts this long after its signal no longer describes the entry.
MAX_START_DELAY_MS = 20_000
MAX_INFLIGHT = 16

FACTOR_KEYS = (
    "dev_pct",
    "top10_pct",
    "holder_count",
    "bundle_pct",
    "sniper_pct",
    "insider_pct",
    "mint_authority_revoked",
    "freeze_authority_revoked",
)


def _flag(name: str, default: str = "on") -> bool:
    return (os.getenv(name) or default).strip().lower() not in {"0", "off", "false", "no"}


def factors_enabled() -> bool:
    return _flag("AUU_ENTRY_FACTORS")


def _env_float(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.getenv(name) or default))
    except ValueError:
        return default


class RpcRateLimited(RuntimeError):
    pass


def _http_rpc(timeout_s: float) -> RpcCall:
    """Thread-local httpx client against the feed's RPC URL (never logged)."""
    local = threading.local()

    def call(method: str, params: list[Any]) -> Any:
        import httpx

        from app.providers.pump_verify import default_rpc_url

        client = getattr(local, "client", None)
        if client is None:
            client = httpx.Client(timeout=timeout_s)
            local.client = client
        resp = client.post(default_rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if resp.status_code == 429:
            raise RpcRateLimited("http 429")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            code = data["error"].get("code")
            if code == 429:
                raise RpcRateLimited("rpc 429")
            raise RuntimeError(f"rpc error code={code}")
        return data.get("result") if isinstance(data, dict) else None

    return call


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(0.1, rps)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self, deadline: float) -> bool:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            if start > deadline:
                return False
            self._next = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        return True


def _reason_of(exc: BaseException) -> str:
    if isinstance(exc, RpcRateLimited):
        return "rpc_rate_limited"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "rpc_timeout"
    return f"rpc_error:{type(exc).__name__}"


def pending_result(signal_ts: int, reason: str) -> dict[str, Any]:
    out: dict[str, Any] = {"v": FACTORS_VERSION, "status": reason, "signal_ts": signal_ts}
    for k in FACTOR_KEYS:
        out[k] = None
    out["null_reasons"] = {k: reason for k in FACTOR_KEYS}
    return out


def combine(
    *, signal_ts: int, tape: Mapping[str, Any], rpc: Mapping[str, Any], mint_info: Mapping[str, Any]
) -> dict[str, Any]:
    """Headline factors: RPC holdings when available (ground truth, read just
    after the signal), else the tape ledger (exact at the signal). Bundle and
    sniper shares are tape-only; authorities are RPC-only; insider needs a
    wallet funding graph and is always null."""
    reasons: dict[str, str] = {}
    out: dict[str, Any] = {"v": FACTORS_VERSION, "status": "ok", "signal_ts": signal_ts}
    sources: dict[str, str] = {}
    rpc_ok = bool(rpc.get("ok"))
    for key in ("dev_pct", "top10_pct", "holder_count"):
        if rpc_ok and rpc.get(key) is not None:
            out[key] = rpc[key]
            sources[key] = "rpc"
        elif tape.get(key) is not None:
            out[key] = tape[key]
            sources[key] = "tape"
        else:
            out[key] = None
            reasons[key] = (rpc.get("null_reasons") or {}).get(key) or rpc.get("reason") or (
                tape.get("null_reasons") or {}
            ).get(key) or "unavailable"
    for key in ("bundle_pct", "sniper_pct"):
        out[key] = tape.get(key)
        if out[key] is not None:
            sources[key] = "tape"
        else:
            reasons[key] = (tape.get("null_reasons") or {}).get(key) or "unavailable"
    for key in ("mint_authority_revoked", "freeze_authority_revoked"):
        if mint_info.get("ok"):
            out[key] = bool(mint_info[key])
            sources[key] = "rpc"
        else:
            out[key] = None
            reasons[key] = str(mint_info.get("reason") or "unavailable")
    out["insider_pct"] = None
    reasons["insider_pct"] = "not_computable_free: needs wallet funding graph"
    out["sources"] = sources
    out["null_reasons"] = reasons
    missing = [k for k in FACTOR_KEYS if k != "insider_pct" and out.get(k) is None]
    if missing:
        out["status"] = "partial"
    out["tape"] = dict(tape)
    out["rpc"] = dict(rpc)
    out["mint"] = dict(mint_info)
    return out


class EntryFactorService:
    def __init__(
        self,
        *,
        rpc_call: Optional[RpcCall] = None,
        rpc_enabled: Optional[bool] = None,
        workers: int = 2,
        rps: Optional[float] = None,
        timeout_s: Optional[float] = None,
        clock: Optional[Callable[[], int]] = None,
    ) -> None:
        from app.data_paths import tests_active

        self.timeout_s = timeout_s if timeout_s is not None else _env_float("AUU_ENTRY_FACTORS_TIMEOUT_S", 4.0, 0.5)
        if rpc_enabled is None:
            rpc_enabled = _flag("AUU_ENTRY_FACTORS_RPC") and (rpc_call is not None or not tests_active())
        self.rpc_enabled = bool(rpc_enabled)
        self._rpc = rpc_call if rpc_call is not None else (_http_rpc(self.timeout_s) if self.rpc_enabled else None)
        self._limiter = _RateLimiter(rps if rps is not None else _env_float("AUU_ENTRY_FACTORS_RPS", 2.0, 0.1))
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="auu-entry-factors")
        self._lock = threading.Lock()
        self._results: "OrderedDict[tuple[str, int], dict[str, Any]]" = OrderedDict()
        self._mint_cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._holders_cache: "OrderedDict[str, tuple[int, dict[str, Any]]]" = OrderedDict()
        self._inflight = 0
        self.counters: dict[str, Any] = {
            "submitted": 0,
            "dropped_busy": 0,
            "done": 0,
            "rpc_calls": 0,
            "rpc_errors": 0,
            "cache_hits": 0,
            "submit_ns_max": 0,
            "submit_ns_total": 0,
            "lookup_ms_max": 0,
        }

    # ------------------------------------------------------------------ API
    def submit(self, provider: Any, *, symbol: str, mint: str, signal_ts: int) -> None:
        """Enqueue a lookup; never blocks, never raises into the caller."""
        t0 = time.perf_counter_ns()
        key = (mint, int(signal_ts))
        try:
            with self._lock:
                if key in self._results:
                    return
                if self._inflight >= MAX_INFLIGHT:
                    self.counters["dropped_busy"] += 1
                    self._store_locked(key, pending_result(int(signal_ts), "dropped_busy"))
                    return
                self._inflight += 1
                self.counters["submitted"] += 1
                self._store_locked(key, pending_result(int(signal_ts), "pending"))
            self._pool.submit(self._run, provider, symbol, mint, int(signal_ts))
        except Exception:
            log.exception("entry factor submit failed")
        finally:
            dt = time.perf_counter_ns() - t0
            self.counters["submit_ns_total"] += dt
            self.counters["submit_ns_max"] = max(self.counters["submit_ns_max"], dt)

    def result(self, mint: str, signal_ts: int) -> dict[str, Any]:
        with self._lock:
            got = self._results.get((mint, int(signal_ts)))
        return dict(got) if got is not None else pending_result(int(signal_ts), "not_submitted")

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with self._lock:
                if self._inflight == 0:
                    return True
            time.sleep(0.01)
        return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"rpc_enabled": self.rpc_enabled, "inflight": self._inflight, **self.counters}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- internals
    def _store_locked(self, key: tuple[str, int], value: dict[str, Any]) -> None:
        self._results[key] = value
        self._results.move_to_end(key)
        while len(self._results) > MAX_RESULTS:
            self._results.popitem(last=False)

    def _run(self, provider: Any, symbol: str, mint: str, signal_ts: int) -> None:
        started = self._clock()
        try:
            out = self._compute(provider, symbol, mint, signal_ts, started)
        except Exception as exc:  # never escapes the worker
            log.exception("entry factor lookup failed for %s", symbol)
            out = pending_result(signal_ts, f"error:{type(exc).__name__}")
        done = self._clock()
        out["lookup"] = {"start_after_signal_ms": started - signal_ts, "done_after_signal_ms": done - signal_ts}
        with self._lock:
            self._store_locked((mint, signal_ts), out)
            self._inflight -= 1
            self.counters["done"] += 1
            self.counters["lookup_ms_max"] = max(self.counters["lookup_ms_max"], done - started)

    def _compute(self, provider: Any, symbol: str, mint: str, signal_ts: int, started: int) -> dict[str, Any]:
        curve = {}
        getter = getattr(provider, "curve_evidence", None)
        if callable(getter):
            curve = dict(getter(symbol) or {})
        creator = str(curve.get("creator") or "") or None
        snap_fn = getattr(provider, "holder_ledger_snapshot", None)
        snap = snap_fn(mint, upto_ts=signal_ts) if callable(snap_fn) else None
        supply = int(curve.get("token_total_supply") or TOKEN_TOTAL_SUPPLY)
        tape = tape_holder_factors(
            snap,
            creator=creator,
            created_slot=curve.get("created_slot"),
            from_create=curve.get("source") == "logs",
            supply=supply,
        )
        mint_info, rpc = self._rpc_factors(mint, creator, started - signal_ts)
        return combine(signal_ts=signal_ts, tape=tape, rpc=rpc, mint_info=mint_info)

    def _call(self, method: str, params: list[Any], deadline: float) -> Any:
        if not self._limiter.wait(deadline):
            raise RpcRateLimited("local rate budget exhausted")
        self.counters["rpc_calls"] += 1
        try:
            return self._rpc(method, params)  # type: ignore[misc]
        except Exception:
            self.counters["rpc_errors"] += 1
            raise

    def _rpc_factors(self, mint: str, creator: Optional[str], start_delay_ms: int) -> tuple[dict, dict]:
        if not self.rpc_enabled or self._rpc is None:
            r = {"ok": False, "reason": "rpc_disabled"}
            return dict(r), dict(r)
        if start_delay_ms > MAX_START_DELAY_MS:
            r = {"ok": False, "reason": "lookup_started_too_late"}
            return dict(r), dict(r)
        deadline = time.monotonic() + 3 * self.timeout_s
        with self._lock:
            mint_info = self._mint_cache.get(mint)
        if mint_info is None:
            try:
                res = self._call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}], deadline)
                mint_info = parse_mint_account((res or {}).get("value") if isinstance(res, Mapping) else None)
                mint_info["slot"] = ((res or {}).get("context") or {}).get("slot") if isinstance(res, Mapping) else None
            except Exception as exc:
                mint_info = {"ok": False, "reason": _reason_of(exc)}
            if mint_info.get("ok"):
                with self._lock:
                    self._mint_cache[mint] = mint_info
                    while len(self._mint_cache) > MAX_MINT_CACHE:
                        self._mint_cache.popitem(last=False)
        else:
            self.counters["cache_hits"] += 1
        if not mint_info.get("ok"):
            return mint_info, {"ok": False, "reason": mint_info.get("reason") or "mint_info_unavailable"}
        now = self._clock()
        with self._lock:
            cached = self._holders_cache.get(mint)
        if cached is not None and now - cached[0] <= HOLDERS_TTL_MS:
            self.counters["cache_hits"] += 1
            return mint_info, dict(cached[1], cache_age_ms=now - cached[0])
        try:
            from app.providers.pump_verify import bonding_curve_address

            curve_owner = bonding_curve_address(mint)
            t0 = self._clock()
            res = self._call("getProgramAccounts", token_accounts_gpa_params(mint, mint_info["token_program"]), deadline)
            if isinstance(res, Mapping):
                accounts, slot = res.get("value") or [], (res.get("context") or {}).get("slot")
            else:
                accounts, slot = res or [], None
            stats = holder_stats_from_accounts(
                decode_owner_amount_rows(accounts),
                supply=mint_info.get("supply"),
                curve_owner=curve_owner,
                creator=creator,
            )
            stats.update({"ok": True, "slot": slot, "fetch_ms": self._clock() - t0})
            with self._lock:
                self._holders_cache[mint] = (now, stats)
                while len(self._holders_cache) > MAX_MINT_CACHE:
                    self._holders_cache.popitem(last=False)
            return mint_info, stats
        except Exception as exc:
            return mint_info, {"ok": False, "reason": _reason_of(exc)}


_service: Optional[EntryFactorService] = None
_service_lock = threading.Lock()


def get_entry_factor_service() -> Optional[EntryFactorService]:
    global _service
    if not factors_enabled():
        return None
    with _service_lock:
        if _service is None:
            _service = EntryFactorService()
        return _service


def set_entry_factor_service(svc: Optional[EntryFactorService]) -> None:
    """Tests: install a service (e.g. with a fake RPC) or clear it."""
    global _service
    with _service_lock:
        old, _service = _service, svc
    if old is not None and old is not svc:
        old.shutdown()
