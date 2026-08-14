"""Restart-only Docker proxy -- the ONLY component allowed to hold the host's
Docker socket.

WHY (2026-08-15): Phase 1 shipped auto-heal restarts by mounting
/var/run/docker.sock read-write into the dashboard container. That socket is
effectively root over every container on the host (including the paper
trading engine) -- an acceptable trade on a machine only its owner touches,
but a different one once the container holding it is dashboard.carsonng.com,
public with no auth gate. This proxy is the narrow replacement: it alone
mounts the socket, and it accepts exactly one action -- POST /restart for a
container name on an explicit allow-list. A compromised dashboard can
therefore only restart the three auto-heal agents; it cannot create, delete,
exec into, or otherwise touch anything else on the daemon.

Only reachable on the compose-internal network (no host port is published);
the dashboard calls it at http://restart-proxy:8096 (see noc.py
_RESTART_PROXY_URL).

Run: python restart_proxy.py   (env: RESTART_PROXY_PORT, ALLOWED_CONTAINERS)
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

PORT = int(os.environ.get("RESTART_PROXY_PORT", "8096"))
ALLOWED = {c.strip() for c in os.environ.get(
    "ALLOWED_CONTAINERS", "quant-dashboard-docker,event-radar,study-app"
).split(",") if c.strip()}

_SOCKET = "/var/run/docker.sock"


def _docker_restart(name: str) -> tuple[int, bool]:
    """POST /containers/{name}/restart via the Engine API over the socket."""
    transport = httpx.HTTPTransport(uds=_SOCKET)
    with httpx.Client(transport=transport, timeout=60) as client:
        resp = client.post(f"http://localhost/containers/{name}/restart")
    return resp.status_code, resp.status_code < 300


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/restart":
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
            status, ok = _docker_restart(name)
            self._reply(200, {"ok": ok, "container": name, "status": status})
        except Exception as e:                      # noqa: BLE001
            self._reply(502, {"ok": False, "container": name, "error": str(e)})

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
