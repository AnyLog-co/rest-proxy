#!/usr/bin/env python3
"""
MCP Web Bridge  v3.0
Bridges HTTP requests from browser -> MCP protocol -> AnyLog network

ARCHITECTURE
============
ONE subprocess (mcp-proxy), ONE worker thread, ONE MCP connection.

HTTP endpoints NEVER call MCP directly.  Instead they post a Job to the
worker's queue and block on a threading.Event until the worker completes it.
The worker executes jobs one at a time with CALL_DELAY_S between them.

This guarantees the MCP server sees at most one in-flight request at a time,
no matter how many browser tabs or concurrent HTTP requests arrive.

CACHE
=====
Results are stored in a TTL cache keyed by (tool, canonical-params).
If a cached result is still fresh, HTTP returns it immediately without
touching the queue at all.  Metadata (tables list, UNS, status) is cached
for CACHE_TTL_S; sensor data for DATA_TTL_S.

ENDPOINTS
=========
  GET  /api/status
  GET  /api/tables?dbms=
  GET  /api/columns?dbms=&table=
  GET  /api/uns/discover
  GET  /api/uns/policies?namespace=&name=
  POST /api/query          body: {dbms, sql, [nodes], [hours]}
  POST /api/query/increment body: {dbms, table, timeColumn, startTime,
                                   endTime, timeUnit, intervalLength, [nodes]}
  POST /api/cache/clear
  GET  /api/worker/status  (debug)

Usage:
    pip install flask flask-cors
    python3 mcp_web_bridge.py
"""

import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Configuration  — edit these as needed
# ---------------------------------------------------------------------------

MCP_PROXY_PATH = "/Users/mdavidson58/Documents/AnyLog/Prove-IT/proxy/myenv/bin/mcp-proxy"
MCP_SERVER_URL = "http://50.116.13.109:32049/mcp/sse"

CALL_DELAY_S   = 1.5    # seconds between MCP calls (throttle)
CACHE_TTL_S    = 300    # metadata cache lifetime (status, tables, columns, UNS)
DATA_TTL_S     = 60     # sensor-data cache lifetime
JOB_TIMEOUT_S  = 45     # max seconds an HTTP request waits for the worker
DEFAULT_DBMS   = "manufacturing_historian"
DEFAULT_HOURS  = 4

# ---------------------------------------------------------------------------
# TTL Cache  — the ONLY shared state between HTTP threads and the worker
# ---------------------------------------------------------------------------

_cache: dict = {}
_cache_lock   = threading.Lock()


def cache_get(key: str):
    with _cache_lock:
        e = _cache.get(key)
        return e["v"] if e and time.time() < e["exp"] else None


def cache_set(key: str, value, ttl: float):
    with _cache_lock:
        _cache[key] = {"v": value, "exp": time.time() + ttl}


def cache_clear():
    with _cache_lock:
        _cache.clear()


def cache_snapshot() -> dict:
    now = time.time()
    with _cache_lock:
        return {k: v for k, v in _cache.items() if now < v["exp"]}

# ---------------------------------------------------------------------------
# Job queue  — HTTP threads post Jobs; worker executes them one at a time
# ---------------------------------------------------------------------------


class Job:
    def __init__(self, tool: str, params: dict, cache_key: str, cache_ttl: float):
        self.tool      = tool
        self.params    = params
        self.cache_key = cache_key
        self.cache_ttl = cache_ttl
        self.done      = threading.Event()
        self.error     = None


_queue: deque = deque()
_queue_lock   = threading.Lock()
_queue_ready  = threading.Event()


def _enqueue(job: Job):
    with _queue_lock:
        for pending in _queue:
            if pending.cache_key == job.cache_key:
                return pending   # already queued; return existing job to wait on
        _queue.append(job)
    _queue_ready.set()
    return job


def _dequeue():
    with _queue_lock:
        return _queue.popleft() if _queue else None

# ---------------------------------------------------------------------------
# MCP Client — one persistent subprocess, one reader thread
# ---------------------------------------------------------------------------


class MCPClient:

    def __init__(self):
        self.process       = None
        self.reader_thread = None
        self.initialized   = False
        self._req_id       = 0
        self._id_lock      = threading.Lock()
        self._pending      = {}
        self._pending_lock = threading.Lock()

    def start(self) -> bool:
        print("[MCP] Starting proxy subprocess...", file=sys.stderr)
        try:
            self.process = subprocess.Popen(
                [MCP_PROXY_PATH, MCP_SERVER_URL],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self.reader_thread = threading.Thread(
                target=self._read_loop, daemon=True, name="mcp-reader"
            )
            self.reader_thread.start()
            if not self._handshake(timeout=20):
                print("[MCP] Handshake timed out", file=sys.stderr)
                return False
            self.initialized = True
            print("[MCP] Connected and ready", file=sys.stderr)
            return True
        except Exception as exc:
            print(f"[MCP] Start error: {exc}", file=sys.stderr)
            return False

    def stop(self):
        self.initialized = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                pass
            self.process = None

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _next_id(self) -> int:
        with self._id_lock:
            self._req_id += 1
            return self._req_id

    def _slot(self, rid: int) -> dict:
        s = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[rid] = s
        return s

    def _send(self, obj: dict):
        try:
            self.process.stdin.write((json.dumps(obj) + "\n").encode())
            self.process.stdin.flush()
        except Exception as exc:
            print(f"[MCP] Write error: {exc}", file=sys.stderr)

    def _read_loop(self):
        try:
            for raw in iter(self.process.stdout.readline, b""):
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = resp.get("id")
                if rid is not None:
                    with self._pending_lock:
                        s = self._pending.pop(rid, None)
                    if s:
                        s["result"] = resp
                        s["event"].set()
        except Exception as exc:
            print(f"[MCP] Reader error: {exc}", file=sys.stderr)

    def _handshake(self, timeout: float) -> bool:
        rid  = self._next_id()
        slot = self._slot(rid)
        self._send({
            "jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-bridge", "version": "3.0"},
            },
        })
        if not slot["event"].wait(timeout=timeout):
            return False
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def call(self, tool: str, params: dict, timeout: float = 30) -> dict:
        """Execute one MCP tool call. MUST only be called from the worker thread."""
        if not self.initialized or not self.is_alive():
            return {"error": "MCP not connected"}

        if ":" in tool:
            tool = tool.split(":", 1)[1]

        rid  = self._next_id()
        slot = self._slot(rid)
        self._send({
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": tool, "arguments": params},
        })
        print(f"[MCP] -> {tool}  {str(params)[:120]}", file=sys.stderr)

        if not slot["event"].wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            print(f"[MCP] TIMEOUT {tool}", file=sys.stderr)
            return {"error": f"timeout: {tool}"}

        resp   = slot["result"]
        result = resp.get("result") or {}
        is_err = result.get("isError", False)
        texts  = [
            b.get("text", "") for b in result.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "\n".join(t for t in texts if t).strip()

        print(f"[MCP] {'ERR' if is_err else 'OK '} {tool}  {text[:80] or '(empty)'}", file=sys.stderr)

        if is_err:
            return {"error": text or "isError=true"}
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return {}

# ---------------------------------------------------------------------------
# Worker  — the ONLY thread that ever calls MCPClient.call()
# ---------------------------------------------------------------------------

_client: MCPClient = None
_worker_thread: threading.Thread = None
_worker_stop = threading.Event()


def _ensure_client() -> bool:
    global _client
    if _client and _client.initialized and _client.is_alive():
        return True
    if _client:
        _client.stop()
    _client = MCPClient()
    return _client.start()


def _worker_body():
    print("[WORKER] Started", file=sys.stderr)
    while not _worker_stop.is_set():
        job = _dequeue()
        if job is None:
            _queue_ready.wait(timeout=5)
            _queue_ready.clear()
            continue

        # Result may have arrived while job sat in queue (duplicate requests)
        if cache_get(job.cache_key) is not None:
            job.done.set()
            continue

        if not _ensure_client():
            job.error = "MCP connection failed"
            job.done.set()
            time.sleep(3)
            continue

        try:
            result = _client.call(job.tool, job.params)
            if isinstance(result, dict) and "error" in result:
                job.error = result["error"]
                # Still cache transport errors briefly so we don't hammer MCP
                cache_set(job.cache_key, result, ttl=10)
            else:
                cache_set(job.cache_key, result, job.cache_ttl)
        except Exception as exc:
            job.error = str(exc)

        job.done.set()
        time.sleep(CALL_DELAY_S)

    print("[WORKER] Stopped", file=sys.stderr)


def start_worker():
    global _worker_thread
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_worker_body, daemon=True, name="mcp-worker")
    _worker_thread.start()

# ---------------------------------------------------------------------------
# HTTP helper — the only way endpoints talk to MCP
# ---------------------------------------------------------------------------


def run_job(tool: str, params: dict, cache_key: str, cache_ttl: float):
    """Return cached result or block until worker completes the MCP call."""
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    job = _enqueue(Job(tool, params, cache_key, cache_ttl))
    if not job.done.wait(timeout=JOB_TIMEOUT_S):
        return {"error": f"Worker timeout ({JOB_TIMEOUT_S}s) for {cache_key}"}

    result = cache_get(cache_key)
    if result is not None:
        return result
    return {"error": job.error or "No result after worker completed"}

# ---------------------------------------------------------------------------
# SQL helpers  (AnyLog does not support SQL aggregate functions)
# ---------------------------------------------------------------------------


def _rows_to_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("rows", "data", "results"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def compute_stats(rows: list) -> dict:
    vals = []
    for row in rows:
        v = row.get("value") if isinstance(row, dict) else None
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    if not vals:
        return {"mean": None, "minv": None, "maxv": None, "stddev": None, "n": 0}
    n = len(vals)
    return {
        "mean":   round(sum(vals) / n, 6),
        "minv":   round(min(vals), 6),
        "maxv":   round(max(vals), 6),
        "stddev": round(statistics.pstdev(vals) if n > 1 else 0.0, 6),
        "n":      n,
    }


def bucket_rows(rows: list, ts_col: str, bucket_secs: int) -> list:
    buckets: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        v  = row.get("value")
        ts = row.get(ts_col) or row.get("timestamp")
        if v is None or ts is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        try:
            s  = str(ts).replace("Z", "+00:00").replace(" ", "T")
            s  = re.sub(r"(\.\d{6})\d+", r"\1", s)
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            epoch = dt.timestamp()
            bk    = int(epoch // bucket_secs) * bucket_secs
            if bk not in buckets:
                bdt = datetime.fromtimestamp(bk, tz=timezone.utc)
                buckets[bk] = {"ts": bdt.strftime("%Y-%m-%dT%H:%M:%SZ"), "vals": []}
            buckets[bk]["vals"].append(v)
        except Exception:
            pass
    out = []
    for bk in sorted(buckets):
        vs = buckets[bk]["vals"]
        n  = len(vs)
        out.append({
            "timestamp": buckets[bk]["ts"],
            "value":     round(sum(vs) / n, 6),
            "min":       round(min(vs), 6),
            "max":       round(max(vs), 6),
            "stddev":    round(statistics.pstdev(vs) if n > 1 else 0.0, 6),
            "count":     n,
        })
    return out


def _bucket_secs(unit: str, length: int) -> int:
    return {"minute": 60, "hour": 3600, "day": 86400,
            "week": 604800, "month": 2592000}.get(unit.lower(), 60) * length


def _rewrite_agg_sql(sql: str, hours: float):
    """Rewrite aggregate SQL to plain SELECT. Returns new sql or None."""
    if not re.search(r'\b(avg|min|max|count|stddev)\s*\(', sql, re.IGNORECASE):
        return None
    tbl   = re.search(r'\bFROM\s+(\S+)', sql, re.IGNORECASE)
    where = re.search(r'\bWHERE\b(.+)$', sql, re.IGNORECASE | re.DOTALL)
    tbl   = tbl.group(1) if tbl else "unknown"
    where = f" WHERE {where.group(1).strip()}" if where else \
            f" WHERE timestamp >= NOW() - {hours} hours"
    return f"SELECT value, timestamp FROM {tbl}{where}"

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)


@app.route("/api/status")
def api_status():
    return jsonify(run_job("anylog-proveit:checkStatus", {}, "status", CACHE_TTL_S))


@app.route("/api/tables")
def api_tables():
    dbms   = request.args.get("dbms", DEFAULT_DBMS)
    key    = f"tables:{dbms}"
    result = run_job("anylog-proveit:listTables", {"dbms": dbms}, key, CACHE_TTL_S)
    if isinstance(result, list):
        return jsonify([{"name": t["name"]} if isinstance(t, dict) else {"name": str(t)}
                        for t in result])
    return jsonify(result)


@app.route("/api/columns")
def api_columns():
    dbms  = request.args.get("dbms", DEFAULT_DBMS)
    table = request.args.get("table", "")
    if not table:
        return jsonify({"error": "Missing ?table="}), 400
    key = f"columns:{dbms}:{table}"
    return jsonify(run_job("anylog-proveit:listColumns",
                           {"dbms": dbms, "table": table}, key, CACHE_TTL_S))


@app.route("/api/uns/discover")
def api_uns_discover():
    result = run_job("anylog-proveit:listPolicyTypes", {}, "uns:policyTypes", CACHE_TTL_S)
    types  = result if isinstance(result, list) else []
    return jsonify({"policyTypes": types, "unsPolicies": []})


@app.route("/api/uns/policies")
def api_uns_policies():
    namespace = request.args.get("namespace", "")
    name      = request.args.get("name", "")
    if not namespace and not name:
        return jsonify({"error": "Provide ?namespace= or ?name="}), 400
    parts = []
    if namespace:
        parts.append(f'namespace = "{namespace}"')
    if name:
        parts.append(f'name = "{name}"')
    where  = " and ".join(parts)
    key    = f"uns:policies:{where}"
    return jsonify(run_job(
        "anylog-proveit:listPolicies",
        {"policyType": "uns", "whereCond": where},
        key, CACHE_TTL_S,
    ))


@app.route("/api/query", methods=["POST"])
def api_query():
    body  = request.json or {}
    dbms  = body.get("dbms", DEFAULT_DBMS)
    sql   = body.get("sql", "").strip()
    nodes = body.get("nodes")
    hours = float(body.get("hours", DEFAULT_HOURS))

    if not sql:
        return jsonify({"error": "Missing sql"}), 400

    rewritten = _rewrite_agg_sql(sql, hours)
    is_stats  = rewritten is not None
    exec_sql  = rewritten if is_stats else sql

    params    = {"dbms": dbms, "sql": exec_sql}
    if nodes:
        params["nodes"] = nodes

    cache_key = f"query:{dbms}:{exec_sql}"
    if body.get("refresh"):
        with _cache_lock:
            _cache.pop(cache_key, None)

    raw  = run_job("anylog-proveit:executeQuery", params, cache_key, DATA_TTL_S)
    rows = _rows_to_list(raw)
    return jsonify(compute_stats(rows) if is_stats else (rows or raw))


@app.route("/api/query/increment", methods=["POST"])
def api_query_increment():
    body = request.json or {}
    for f in ("dbms", "table", "timeColumn", "startTime", "endTime", "timeUnit", "intervalLength"):
        if f not in body:
            return jsonify({"error": f"Missing field: {f}"}), 400

    dbms   = body["dbms"]
    table  = body["table"]
    ts_col = body["timeColumn"]
    start  = body["startTime"]
    end    = body["endTime"]
    nodes  = body.get("nodes")

    sql    = (f"SELECT value, {ts_col} FROM {table} "
              f"WHERE {ts_col} >= '{start}' AND {ts_col} < '{end}'")
    params = {"dbms": dbms, "sql": sql}
    if nodes:
        params["nodes"] = nodes

    cache_key = f"incr:{dbms}:{table}:{start}:{end}:{body['timeUnit']}:{body['intervalLength']}"
    raw   = run_job("anylog-proveit:executeQuery", params, cache_key, DATA_TTL_S)
    rows  = _rows_to_list(raw)
    bsecs = _bucket_secs(body["timeUnit"], int(body["intervalLength"]))
    return jsonify(bucket_rows(rows, ts_col, bsecs))


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    cache_clear()
    return jsonify({"cleared": True})


@app.route("/api/worker/status")
def api_worker_status():
    snap = cache_snapshot()
    with _queue_lock:
        q = [j.cache_key for j in _queue]
    return jsonify({
        "worker_alive":    _worker_thread.is_alive() if _worker_thread else False,
        "mcp_initialized": _client.initialized if _client else False,
        "mcp_alive":       _client.is_alive() if _client else False,
        "cached_entries":  len(snap),
        "cached_keys":     sorted(snap.keys()),
        "queue_depth":     len(q),
        "queued_keys":     q,
        "call_delay_s":    CALL_DELAY_S,
        "job_timeout_s":   JOB_TIMEOUT_S,
    })


@app.route("/")
def index():
    dashboard = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "enterprise_c_spc_mcp_dashboard.html",
    )
    if os.path.exists(dashboard):
        with open(dashboard) as f:
            return f.read()
    return (
        "<h1>MCP Web Bridge v3.0</h1>"
        "<p>Endpoints: /api/status /api/tables /api/columns "
        "/api/uns/discover /api/uns/policies "
        "/api/query(POST) /api/query/increment(POST) "
        "/api/cache/clear(POST) /api/worker/status</p>"
    )


if __name__ == "__main__":
    print("=" * 60, file=sys.stderr)
    print("MCP Web Bridge  v3.0  (single worker, all MCP calls serialised)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Proxy  : {MCP_PROXY_PATH}", file=sys.stderr)
    print(f"Server : {MCP_SERVER_URL}", file=sys.stderr)
    print(f"Delay  : {CALL_DELAY_S}s between MCP calls", file=sys.stderr)
    print(f"Timeout: {JOB_TIMEOUT_S}s per HTTP request wait", file=sys.stderr)
    print(file=sys.stderr)
    start_worker()
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
