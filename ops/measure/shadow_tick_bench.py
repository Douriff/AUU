#!/usr/bin/env python3
"""Benchmark of the auu_signal shadow tick: duration, bytes/fsyncs written, event-loop stall, /health latency.

Runs ONE code variant per invocation (``--api-dir`` = an ``apps/api`` checkout: origin/main for "before", the fix
branch for "after") against a synthetic, production-sized market store (19 coins x 730 daily bars + 3 funding points
per day; the tick reads the newest 500 bars / 500 funding rows per coin, like production). Nothing touches real data:
everything lives in a temp dir, no network, no exchange calls, no secrets.

What is measured, per scenario, through the REAL ``run_loop`` of that code variant:
  * the tick: wall seconds, process CPU seconds, rows/commits (after only), bytes+calls written to regular files and
    fsync/fdatasync count (LD_PRELOAD shim ``io_shim.so``, see io_shim.c), kernel ``write_bytes`` (/proc/self/io)
  * event-loop stall: a 5 ms ticker task in the same loop as uvicorn+run_loop; max overshoot while the tick runs
  * /api/v1/health latency: a SEPARATE PROCESS polls a uvicorn-served health route every 50 ms (like auu-guard's
    ``curl --max-time 5``); max latency, and number of probes slower than 5 s

Scenarios (the tick is the only thing that differs between variants):
  cold      first tick of a process, shadow DB empty
  steady    next tick, market data unchanged except the forming daily bar (what ~143 of 144 daily ticks look like)
  rollover  a new daily bar closes for all 19 coins (+ funding settlement): the one tick per day that has real work
  steady2   rollover done, nothing new
  restart   (after) new process state on an up-to-date DB / (before) identical to steady: every tick is a full one
Usage:
  python3 shadow_tick_bench.py --api-dir /path/to/apps/api --label before --out before.json
  python3 shadow_tick_bench.py --report before.json after.json   > table.md
Optional: AUU_SHIM_FSYNC_DELAY_US=1000 python3 shadow_tick_bench.py ...   emulates a 1 ms fsync (slow cloud disk).
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

DAY = 86_400_000
COINS = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()
HERE = Path(__file__).resolve().parent
SHIM_SRC = HERE / "io_shim.c"
SHIM = Path(tempfile.gettempdir()) / "auu_io_shim.so"


def build_shim() -> bool:
    """Compile the LD_PRELOAD counter next to the temp dir (needs gcc; without it the fsync/byte counters read n/a)."""
    if SHIM.exists() and SHIM.stat().st_mtime >= SHIM_SRC.stat().st_mtime:
        return True
    try:
        subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", str(SHIM), str(SHIM_SRC), "-ldl"], check=True,
                       capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False

PROBER = r'''
import http.client, json, sys, time
host, port, path, out = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
f = open(out, "w")
while True:
    t = time.time()
    try:
        c = http.client.HTTPConnection(host, port, timeout=30)
        c.request("GET", path); r = c.getresponse(); r.read(); c.close(); ok = r.status == 200
    except Exception as e:
        ok = False
    f.write(json.dumps([t, time.time() - t, ok]) + "\n"); f.flush()
    time.sleep(0.05)
'''


def lcg(seed):
    x = seed
    while True:
        x = (1103515245 * x + 12345) % (2**31)
        yield x / 2**31


def history(coin: str, n: int, t0: int):
    g = lcg(sum(ord(c) for c in coin) * 7919)
    px, drift, bars = 100.0 + len(coin), 0.0, []
    for j in range(n):
        if j % 23 == 0:
            drift = (next(g) - 0.5) * 0.05
        o = px
        px = max(1.0, px * (1 + drift + (next(g) - 0.5) * 0.04))
        bars.append([t0 + j * DAY, o, max(o, px) * 1.01, min(o, px) * 0.99, px, 1000 + 800 * next(g)])
    fund = [(t0 + j * DAY + k * 8 * 3_600_000, round((next(g) - 0.45) * 0.0004, 8)) for j in range(n) for k in range(3)]
    return bars, fund


def read_io():
    d = {}
    for line in open("/proc/self/io"):
        k, v = line.split(":")
        d[k] = int(v)
    return d


def shim_counts():
    try:
        lib = ctypes.CDLL(None)
        a = (ctypes.c_uint64 * 4)()
        lib.shim_counts(a)
        return {"fsyncs": a[0], "write_calls": a[1], "write_bytes_userland": a[2]}
    except (AttributeError, OSError):
        return {"fsyncs": None, "write_calls": None, "write_bytes_userland": None}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def main_bench(a):
    if not a.no_shim and "LD_PRELOAD" not in os.environ and build_shim():
        env = dict(os.environ, LD_PRELOAD=str(SHIM))
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    tmp = tempfile.mkdtemp(prefix=f"shadow_bench_{a.label}_", dir=a.workdir)
    os.environ["AUU_DATA_DIR"] = tmp
    os.environ.setdefault("AUU_SIGNAL_SHADOW", "on")
    sys.path.insert(0, str(Path(a.api_dir).resolve()))
    from app.marketdata.mainstream.store import MarketStore
    from app.paper import auu_signal_shadow as sh
    import app.marketdata.mainstream as ms
    import uvicorn
    from fastapi import FastAPI

    n = a.bars
    now0 = (int(time.time() * 1000) // DAY) * DAY + 5 * 60_000  # 00:05 UTC today: bar n-1 is the (just started) forming bar
    t0 = now0 - 5 * 60_000 - (n - 1) * DAY
    store = MarketStore(Path(tmp) / "mainstream.sqlite")
    hist = {c: history(c, n + 1, t0) for c in COINS[: a.coins]}
    clock = {"now": 0}

    def advance(now, noise=0.0, extra_funding=False):
        clock["now"] = now
        cur = (now - t0) // DAY
        for c, (bars, fund) in hist.items():
            rows = [list(b) for b in bars[: cur + 1]]
            if rows[-1][0] + DAY > now:
                rows[-1][4] = rows[-1][1] * (1 + 0.003 * (1 + noise))
                rows[-1][5] *= 0.3 * (1 + noise)
            store.upsert_candles("binance", c, "1d", rows)
            store.upsert_funding("binance", c, [f for f in fund if f[0] <= now])

    svc = SimpleNamespace(store=store, cfg=SimpleNamespace(strategy_symbols=list(hist)),
                          now_ms=lambda: clock["now"], exchange_for_read=lambda: "binance")
    ms.get_service = lambda: svc  # _tick_universe does ``from app.marketdata.mainstream import get_service`` at call time
    sh.shadow_enabled = lambda: True
    shared_state = sh.TickState() if hasattr(sh, "TickState") else None
    if shared_state is not None:
        sh.TickState = lambda: shared_state
    after = shared_state is not None

    records: list[dict] = []
    done = threading.Event()
    real_tick = sh._tick_universe

    def measured_tick(*args, **kw):
        io0, sc0, c0, w0, cpu0 = read_io(), shim_counts(), time.perf_counter(), time.time(), time.process_time()
        try:
            res = real_tick(*args, **kw)
        finally:
            io1, sc1 = read_io(), shim_counts()
            rec = {
                "t_start": w0, "t_end": time.time(), "wall_s": round(time.perf_counter() - c0, 3),
                "cpu_s": round(time.process_time() - cpu0, 3),
                "kernel_write_bytes": io1["write_bytes"] - io0["write_bytes"],
                "wchar": io1["wchar"] - io0["wchar"],
            }
            for k in sc1:
                rec[k] = None if sc1[k] is None else sc1[k] - sc0[k]
            if isinstance(res, dict):
                rec["tick_stats"] = {k: res[k] for k in ("recomputed", "unchanged", "cand_recomputed", "rows_written",
                                                         "rows_skipped", "commits", "skipped") if k in res}
            records.append(rec)
            done.set()
        return res

    sh._tick_universe = measured_tick

    app = FastAPI()

    @app.get("/api/v1/health")
    def health():  # a plain ``def`` route like the real one (runs in the threadpool); the event loop still has to
        return {"ok": True}  # accept the connection, dispatch and write the reply: a stalled loop breaks it

    port = free_port()
    prober_py = Path(tmp) / "prober.py"
    prober_py.write_text(PROBER)
    probe_out = Path(tmp) / "probe.jsonl"

    async def run():
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"))
        stask = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        prober = subprocess.Popen([sys.executable, str(prober_py), "127.0.0.1", str(port), "/api/v1/health", str(probe_out)], env=env)
        lag_samples: list[tuple[float, float]] = []

        async def lagmon():
            while True:
                t = time.perf_counter()
                await asyncio.sleep(0.005)
                lag_samples.append((time.time(), time.perf_counter() - t - 0.005))

        lm = asyncio.create_task(lagmon())
        await asyncio.sleep(1.0)
        out = []

        async def scenario(name, setup, *, probe_only=False):
            await asyncio.to_thread(setup)  # harness data prep must not stall the loop under measurement
            await asyncio.sleep(0.4)
            done.clear()
            n0 = len(records)
            if probe_only:
                await asyncio.to_thread(sh._tick_universe, state=shared_state, full=False)
            else:
                kw = {"interval_sec": 3600, "probe_sec": 3600} if after else {"interval_sec": 3600}
                lt = asyncio.create_task(sh.run_loop(**kw))
                while not done.is_set():
                    await asyncio.sleep(0.05)
                lt.cancel()
                try:
                    await lt
                except asyncio.CancelledError:
                    pass
            await asyncio.sleep(0.6)
            r = records[n0]
            lo, hi = r["t_start"] - 0.05, r["t_end"] + 0.4
            lags = [l for t, l in lag_samples if lo <= t <= hi]
            probes = [json.loads(x) for x in probe_out.read_text().splitlines() if x.strip()]
            pw = [p for p in probes if lo <= p[0] <= hi]
            r.update(
                scenario=name,
                loop_max_stall_ms=round(max(lags) * 1000, 1) if lags else None,
                health_probes=len(pw),
                health_max_ms=round(max((p[1] for p in pw), default=0) * 1000, 1),
                health_p99_ms=round((pct([p[1] for p in pw], 0.99) or 0) * 1000, 1),
                health_over_5s=sum(1 for p in pw if p[1] > 5 or not p[2]),
                health_failed=sum(1 for p in pw if not p[2]),
                db_bytes=sum(f.stat().st_size for f in Path(tmp).glob("auu_signal_shadow.sqlite*")),
            )
            out.append(r)
            print(f"[{a.label}] {name}: wall={r['wall_s']}s cpu={r['cpu_s']}s stall={r['loop_max_stall_ms']}ms "
                  f"health_max={r['health_max_ms']}ms fsync={r['fsyncs']} written={r['write_bytes_userland']} "
                  f"kernel_write_bytes={r['kernel_write_bytes']}", flush=True)

        step = [now0]

        def adv(noise=0.0):
            return lambda: advance(step[0], noise)

        await scenario("cold", adv())
        step[0] = now0 + 10 * 60_000
        await scenario("steady", adv(0.7))
        step[0] = now0 + DAY + 5 * 60_000
        await scenario("rollover", adv())
        step[0] = now0 + DAY + 15 * 60_000
        await scenario("steady2", adv(0.5))
        if after:
            step[0] = now0 + DAY + 25 * 60_000
            await scenario("probe_only_idle", adv(0.9), probe_only=True)
            def restart():
                shared_state.__init__()
                advance(now0 + DAY + 35 * 60_000, 0.2)
            await scenario("restart", restart)
        else:
            step[0] = now0 + DAY + 25 * 60_000
            await scenario("restart", adv(0.2))
        prober.terminate()
        lm.cancel()
        server.should_exit = True
        await stask
        return out

    res = asyncio.run(run())
    meta = {
        "label": a.label, "api_dir": str(a.api_dir), "coins": len(hist), "bars_in_store": n, "bars_read": 500,
        "python": sys.version.split()[0], "cpus": os.cpu_count(), "io_shim": "LD_PRELOAD" in os.environ,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    Path(a.out).write_text(json.dumps({"meta": meta, "scenarios": res}, indent=1))
    print("wrote", a.out)


def report(paths):
    docs = [json.loads(Path(p).read_text()) for p in paths]
    names = []
    for d in docs:
        for s in d["scenarios"]:
            if s["scenario"] not in names:
                names.append(s["scenario"])
    cols = [("wall_s", "tick s"), ("cpu_s", "CPU s"), ("write_bytes_userland", "bytes written"), ("fsyncs", "fsync"),
            ("kernel_write_bytes", "kernel write_bytes"), ("loop_max_stall_ms", "loop max stall ms"),
            ("health_max_ms", "/health max ms"), ("health_over_5s", "/health >5s or failed")]
    for d in docs:
        print(f"\n**{d['meta']['label']}** ({d['meta']['api_dir']})\n")
        print("| scenario | " + " | ".join(c[1] for c in cols) + " |")
        print("|---|" + "---|" * len(cols))
        for n in names:
            s = next((x for x in d["scenarios"] if x["scenario"] == n), None)
            if s:
                def f(k):
                    v = s.get(k)
                    return "n/a" if v is None else (f"{v/1e6:.1f} MB" if k.endswith("bytes") or k.endswith("userland") else str(v))
                print(f"| {n} | " + " | ".join(f(c[0]) for c in cols) + " |")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-dir")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default="bench.json")
    ap.add_argument("--coins", type=int, default=19)
    ap.add_argument("--bars", type=int, default=730)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--no-shim", action="store_true")
    ap.add_argument("--report", nargs="+")
    a = ap.parse_args()
    if a.report:
        report(a.report)
    else:
        main_bench(a)
