"""Local end-to-end measurement for the exchange WebSocket feed (docs/realtime-ws-plan.md, steps 1-2).

mock Binance combined stream (child process)  ->  API (uvicorn child process, AUU_RT_WS=on)  ->  N /api/v1/ws clients
(child processes). Nothing touches a real exchange or the network: everything listens on 127.0.0.1, every
background job of the API that could call out is switched off, the data dir is a temp dir.

Latency per frame = client receive time - exchange event time (``E`` stamped by the mock at send, same host clock,
1 ms resolution); split into exchange->server (``recvAt - E``) and server->client. CPU/RSS of the API process are
sampled from /proc once a second.

    cd apps/api && python ../../ops/measure/realtime_ws_e2e.py run --out /tmp/rtws_e2e
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

COINS = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()
TFS = ("1h", "4h", "1d")
H = 3_600_000
TF_MS = {"1h": H, "4h": 4 * H, "1d": 24 * H}


def now_ms() -> int:
    return int(time.time() * 1000)


# ---- mock exchange --------------------------------------------------------------------------------
async def mock_main(port_file: str, mult: float) -> None:
    from tests.rt_mock_exchange import MockExchange

    m = await MockExchange("binance").start()
    Path(port_file).write_text(str(m.port))
    px = {c: 100.0 * (i + 1) for i, c in enumerate(COINS)}
    # Binance cadence: miniTicker every 1 s, kline every 2 s -> per 2 s cycle 38 tickers + 57 klines (47.5 frames/s)
    cycle = [("t", c) for c in COINS] * 2 + [("k", c, tf) for c in COINS for tf in TFS]
    random.Random(7).shuffle(cycle)
    gap = 2.0 / len(cycle) / mult
    nxt = time.perf_counter()
    while True:
        for item in cycle:
            nxt += gap
            delay = nxt - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            if not m.conns:
                continue
            c = item[1]
            px[c] *= 1 + random.uniform(-2e-4, 2e-4)
            e = now_ms()
            if item[0] == "t":
                await m.broadcast(m.binance_ticker(c, round(px[c], 6), open_=100.0, event_ms=e))
            else:
                tf = item[2]
                t = e // TF_MS[tf] * TF_MS[tf]
                p = round(px[c], 6)
                await m.broadcast(m.binance_kline(c, tf, t, p, p * 1.001, p * 0.999, p, event_ms=e))


# ---- clients --------------------------------------------------------------------------------------
async def clients_main(url: str, n: int, duration: float, out: str) -> None:
    from websockets.asyncio.client import connect

    lat_total: list[int] = []
    lat_srv: list[int] = []
    frames = {"ticker": 0, "kline": 0}
    errors: list[str] = []
    t_end = time.time() + duration

    async def one(i: int) -> None:
        try:
            async with connect(url, open_timeout=10, max_queue=4096, compression=None) as ws:
                await ws.recv()  # hello
                await ws.send(json.dumps({"type": "subscribe", "channel": "ticker", "symbols": COINS}))
                for c in ("BTC", "ETH"):
                    await ws.send(json.dumps({"type": "subscribe", "channel": "kline", "symbol": c, "interval": "1h"}))
                warm = time.time() + 2.0
                while time.time() < t_end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), max(0.1, t_end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    recv = now_ms()
                    msg = json.loads(raw)
                    typ = msg.get("type")
                    if typ == "ping":
                        await ws.send('{"type":"pong"}')
                        continue
                    if typ not in frames or time.time() < warm:
                        continue  # snapshots right after subscribing are not live frames
                    p = msg["payload"]
                    ev = p.get("ts") if typ == "ticker" else p.get("eventTs")
                    if not ev:
                        continue
                    frames[typ] += 1
                    lat_total.append(recv - int(ev))
                    lat_srv.append(int(p["recvAt"]) - int(ev))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}"[:160])

    await asyncio.gather(*(one(i) for i in range(n)))
    Path(out).write_text(json.dumps({"n": n, "frames": frames, "latTotal": lat_total, "latSrv": lat_srv, "errors": errors}))


# ---- orchestrator ---------------------------------------------------------------------------------
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def proc_sample(pid: int) -> tuple[float, int]:
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    cpu = (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    rss = 0
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
    return cpu, rss


def pct(vals: list[int], q: float) -> float:
    if not vals:
        return float("nan")
    v = sorted(vals)
    return float(v[min(len(v) - 1, int(q * len(v)))])


def api_env(data_dir: str, rt: bool, mock_url: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUU_", "SMTP"))}
    env.update({
        "AUU_SKIP_DOTENV": "1", "AUU_DATA_DIR": data_dir, "AUU_AUTH": "off", "AUU_SHIELD": "off",
        "AUU_API_DOCS": "off", "AUU_MAINSTREAM_REFRESH": "off", "AUU_STRATEGY_RUNNER": "off", "AUU_SHADOW_S3": "off",
        "AUU_SHADOW_H2": "off", "AUU_SIGNAL_SHADOW": "off", "AUU_ALERTS": "off", "AUU_PRICE_ALERTS": "off",
        "AUU_RECON": "off", "AUU_UPTIME": "off", "AUU_NEWS": "off", "AUU_AIRDROPS_INGEST": "off", "AUU_TRC20_POLL": "off",
        "AUU_MAINSTREAM_SYMBOLS": "BTC,ETH,SOL", "PYTHONDONTWRITEBYTECODE": "1",
    })
    if rt:
        env.update({"AUU_RT_WS": "on", "AUU_RT_WS_EXCHANGES": "binance", "AUU_RT_BINANCE_WS_URLS": mock_url,
                    "AUU_RT_XCHECK_SEC": "0", "AUU_RT_WS_PER_CLIENT": "10000"})
    return env


def start_api(port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning", "--ws", "websockets"], cwd=str(API_DIR), env=env)


def wait_http(port: int, path: str, pred, timeout: float = 30.0):
    import urllib.request

    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"user-agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2) as r:
                last = json.loads(r.read())
                if pred(last):
                    return last
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for {path}: {last}")


def sample_phase(pid: int, seconds: float) -> dict:
    c0, _ = proc_sample(pid)
    t0 = time.time()
    rss: list[int] = []
    while time.time() - t0 < seconds:
        time.sleep(1.0)
        rss.append(proc_sample(pid)[1])
    c1, r1 = proc_sample(pid)
    return {"cpuPct": round((c1 - c0) / (time.time() - t0) * 100, 2), "rssMB": round(r1 / 2**20, 1),
            "rssMaxMB": round(max(rss or [r1]) / 2**20, 1)}


def run_clients(url: str, n: int, duration: float, procs: int, tmp: str, tag: str) -> tuple[list, dict]:
    per = [n // procs + (1 if i < n % procs else 0) for i in range(procs)]
    outs, ps = [], []
    for i, k in enumerate(per):
        if k == 0:
            continue
        out = f"{tmp}/{tag}_{i}.json"
        outs.append(out)
        ps.append(subprocess.Popen([sys.executable, __file__, "clients", "--url", url, "--n", str(k), "--duration",
                                    str(duration), "--out", out]))
    return ps, {"outs": outs}


def collect(outs: list[str]) -> dict:
    tot, srv, frames, errors, n = [], [], {"ticker": 0, "kline": 0}, [], 0
    for o in outs:
        d = json.loads(Path(o).read_text())
        n += d["n"]
        tot += d["latTotal"]
        srv += d["latSrv"]
        errors += d["errors"]
        for k in frames:
            frames[k] += d["frames"][k]
    return {"clients": n, "frames": frames, "errors": errors[:5], "errorCount": len(errors),
            "latencyMs": {"p50": pct(tot, .5), "p90": pct(tot, .9), "p99": pct(tot, .99), "max": max(tot) if tot else None,
                          "mean": round(statistics.fmean(tot), 2) if tot else None},
            "exchangeToServerMs": {"p50": pct(srv, .5), "p99": pct(srv, .99), "max": max(srv) if srv else None},
            "serverToClientMs": {"p50": pct([a - b for a, b in zip(tot, srv)], .5),
                                 "p99": pct([a - b for a, b in zip(tot, srv)], .99)}}


def orchestrate(out_dir: str, clients: int, duration: float, mult: float, procs: int) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="rtws_")
    res: dict = {"host": {"cpus": os.cpu_count(), "python": sys.version.split()[0]}, "mult": mult, "phases": {}}
    port_file = f"{tmp}/mock.port"
    mock = subprocess.Popen([sys.executable, __file__, "mock", "--port-file", port_file, "--mult", str(mult)])
    try:
        for _ in range(100):
            if Path(port_file).exists() and Path(port_file).read_text():
                break
            time.sleep(0.1)
        mock_url = f"ws://127.0.0.1:{Path(port_file).read_text().strip()}"
        # A: switch off (today's behaviour), idle
        port = free_port()
        api = start_api(port, api_env(f"{tmp}/data_off", False, mock_url))
        try:
            wait_http(port, "/api/v1/mainstream/realtime", lambda d: d["data"] == {"enabled": False})
            time.sleep(3)
            res["phases"]["off_idle"] = sample_phase(api.pid, 15)
        finally:
            api.send_signal(signal.SIGINT)
            api.wait(15)
        # B: switch on
        port = free_port()
        api = start_api(port, api_env(f"{tmp}/data_on", True, mock_url))
        try:
            wait_http(port, "/api/v1/mainstream/realtime", lambda d: d["data"].get("selected") == "binance")
            time.sleep(3)
            res["phases"]["on_0_clients"] = sample_phase(api.pid, 15)
            url = f"ws://127.0.0.1:{port}/api/v1/ws"
            for n in (1, clients):
                ps, meta = run_clients(url, n, duration, min(procs, n), tmp, f"c{n}")
                time.sleep(4)  # connect + warm-up
                ph = sample_phase(api.pid, max(5.0, duration - 8))
                for p in ps:
                    p.wait(duration + 60)
                ph.update(collect(meta["outs"]))
                res["phases"][f"on_{n}_clients"] = ph
            st = wait_http(port, "/api/v1/mainstream/realtime", lambda d: True)["data"]
            res["serverStatus"] = {k: st.get(k) for k in ("selected", "clients", "rejected", "slowClosed", "published")}
            res["serverStatus"]["feed"] = {k: st["feeds"][0].get(k) for k in ("messages", "parseErrors", "connects", "disconnects")}
        finally:
            api.send_signal(signal.SIGINT)
            api.wait(15)
    finally:
        mock.kill()
        mock.wait()
    Path(out_dir, f"result_x{mult:g}.json").write_text(json.dumps(res, indent=2))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("mock")
    a.add_argument("--port-file", required=True)
    a.add_argument("--mult", type=float, default=1.0)
    b = sub.add_parser("clients")
    b.add_argument("--url", required=True)
    b.add_argument("--n", type=int, required=True)
    b.add_argument("--duration", type=float, required=True)
    b.add_argument("--out", required=True)
    c = sub.add_parser("run")
    c.add_argument("--out", default="/tmp/rtws_e2e")
    c.add_argument("--clients", type=int, default=100)
    c.add_argument("--duration", type=float, default=60)
    c.add_argument("--mult", type=float, default=1.0, help="exchange message rate multiplier (1 = Binance cadence)")
    c.add_argument("--procs", type=int, default=4, help="client processes")
    args = ap.parse_args()
    if args.cmd == "mock":
        asyncio.run(mock_main(args.port_file, args.mult))
    elif args.cmd == "clients":
        asyncio.run(clients_main(args.url, args.n, args.duration, args.out))
    else:
        print(json.dumps(orchestrate(args.out, args.clients, args.duration, args.mult, args.procs), indent=2))


if __name__ == "__main__":
    main()
