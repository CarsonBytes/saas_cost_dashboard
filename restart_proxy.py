"""Restart-only Docker proxy -- the ONLY component allowed to hold the host's
Docker socket.

WHY (2026-08-15): Phase 1 shipped auto-heal restarts by mounting
/var/run/docker.sock read-write into the dashboard container. That socket is
effectively root over every container on the host (including the paper
trading engine) -- an acceptable trade on a machine only its owner touches,
but a different one once the container holding it is dashboard.carsonng.com,
public with no auth gate. This proxy is the narrow replacement: it alone
mounts the socket, and it accepts exactly a few actions -- POST /restart,
POST /pause, POST /unpause, GET /stats -- each for a container name on an
explicit allow-list (GET /stats covers all of them at once, read-only). A
compromised dashboard can therefore only restart, pause/unpause, or read
memory stats for the allow-listed agents; it cannot create, delete, exec
into, or otherwise touch anything else on the daemon.

Only reachable on the compose-internal network (no host port is published);
the dashboard calls it at http://restart-proxy:8096 (see noc.py
_RESTART_PROXY_URL).

Run: python restart_proxy.py   (env: RESTART_PROXY_PORT, ALLOWED_CONTAINERS)
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

PORT = int(os.environ.get("RESTART_PROXY_PORT", "8096"))
ALLOWED = {c.strip() for c in os.environ.get(
    "ALLOWED_CONTAINERS",
    "quant-dashboard-docker,event-radar,study-app,quant-dashboard-live-docker,"
    "event-radar-demo,study-demo,linked-content-engine,linked-content-engine-scheduler"
).split(",") if c.strip()}

_SOCKET = "/var/run/docker.sock"

# action -> Docker Engine API endpoint suffix on /containers/{name}
_ACTIONS = {
    "restart": "/restart",
    "pause": "/pause",
    "unpause": "/unpause",
}


def _docker_action(name: str, action: str) -> tuple[int, bool]:
    """POST /containers/{name}/{action} via the Engine API over the socket."""
    transport = httpx.HTTPTransport(uds=_SOCKET)
    with httpx.Client(transport=transport, timeout=60) as client:
        resp = client.post(f"http://localhost/containers/{name}{_ACTIONS[action]}")
    return resp.status_code, resp.status_code < 300


def _container_stats(name: str) -> dict | None:
    """ADDED 2026-09-04 (memory-control spec item #4): GET /containers/{name}
    /stats?stream=false -- a one-shot cgroup snapshot, read-only, same
    allow-list boundary as restart/pause. `usage` includes page cache, which
    inflates the number vs. what a leak actually looks like -- subtract
    `stats.cache` (cgroup v1) / `stats.inactive_file` (cgroup v2), matching
    what `docker stats` itself shows."""
    try:
        transport = httpx.HTTPTransport(uds=_SOCKET)
        with httpx.Client(transport=transport, timeout=10) as client:
            resp = client.get(f"http://localhost/containers/{name}/stats",
                              params={"stream": "false"})
        if resp.status_code >= 300:
            return None
        mem = resp.json().get("memory_stats", {})
        usage = mem.get("usage")
        if usage is None:
            return None
        cache = mem.get("stats", {}).get("cache", mem.get("stats", {}).get("inactive_file", 0))
        return {"usage_bytes": max(usage - cache, 0), "limit_bytes": mem.get("limit")}
    except Exception:                                 # noqa: BLE001
        return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/stats":
            self._reply(404, {"ok": False, "error": "unknown path"})
            return
        # One call per allow-listed container -- the Engine API has no bulk
        # stats endpoint. Local Unix-socket calls, not network, so a small
        # pool keeps this well under noc.py's per-cycle budget.
        with ThreadPoolExecutor(max_workers=max(len(ALLOWED), 1)) as pool:
            names = sorted(ALLOWED)
            results = pool.map(_container_stats, names)
            containers = {name: s for name, s in zip(names, results) if s is not None}
        self._reply(200, {"ok": True, "containers": containers})

    def do_POST(self) -> None:
        action = self.path.lstrip("/")
        if action not in _ACTIONS:
            self._reply(404, {"ok": False, "error": "unknown action"})
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            body = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            body = {}
        name = str(body.get("container", ""))
        if name not in ALLOWED:
            self._reply(403, {"ok": False, "error": "container not in allow-list"})
            return
        try:
            status, ok = _docker_action(name, action)
            self._reply(200, {"ok": ok, "action": action, "container": name, "status": status})
        except Exception as e:                      # noqa: BLE001
            self._reply(502, {"ok": False, "action": action, "container": name, "error": str(e)})

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args) -> None:     # quieter access log
        pass


def main() -> None:
    print(f"restart-proxy listening on :{PORT}, allow-list={sorted(ALLOWED)}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
