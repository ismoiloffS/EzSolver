"""
Cloudflare Turnstile Solver Service
------------------------------------
Listens on http://0.0.0.0:8191 (or PORT env var).

POST /solve
  Body (JSON): {"sitekey": "...", "siteurl": "https://example.com"}
  Response:    {"token": "...", "elapsed": 4.23}
               {"error": "..."} on failure


made by ismoiloff
"""


import os
import platform
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional
import json

from solver import solve


PORT = int(os.environ.get("PORT", 8191))
# How many Chrome instances to run in parallel.
# Rule of thumb: ~500 MB RAM per worker. 4 workers = ~2 GB.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 4))

# Semaphore caps concurrent Chrome instances; threads above the limit
# block here (queued) until a slot opens — no requests are dropped.
_worker_sem = threading.Semaphore(MAX_WORKERS)
_active_count = 0
_queued_count = 0
_count_lock = threading.Lock()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in its own thread so solves don't block each other."""
    daemon_threads = True


def _ensure_display() -> Optional[subprocess.Popen]:
    """Start a virtual Xvfb display so Chrome runs invisibly.

    Forces Chrome onto X11 via Xvfb even on Wayland desktops:
    - Unsets WAYLAND_DISPLAY so Chrome can't connect to the compositor
    - Sets DISPLAY to the virtual Xvfb server
    """
    if platform.system() != "Linux":
        return None
    # Kill any stale Xvfb on :99
    subprocess.run(["pkill", "-f", "Xvfb :99"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    xvfb = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Force X11 — prevent Chrome from connecting to Wayland/Hyprland
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
    print("[service] started Xvfb on :99 (Wayland disabled, Chrome renders offscreen)")
    return xvfb


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log noise
        print(f"[service] {self.address_string()} - {fmt % args}")

    def send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/solve":
            self.send_json(404, {"error": "not found — use POST /solve"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        sitekey = payload.get("sitekey", "").strip()
        siteurl = payload.get("siteurl", "").strip()
        timeout = int(payload.get("timeout", 45))

        if not sitekey or not siteurl:
            self.send_json(400, {"error": "sitekey and siteurl are required"})
            return

        global _active_count, _queued_count

        with _count_lock:
            _queued_count += 1
        print(f"[service] queued — sitekey={sitekey!r} url={siteurl!r} "
              f"(active={_active_count}/{MAX_WORKERS} queued={_queued_count})")

        # Block until a worker slot is free — other threads keep running
        _worker_sem.acquire()

        with _count_lock:
            _queued_count -= 1
            _active_count += 1

        t0 = time.time()
        try:
            print(f"[service] solving sitekey={sitekey!r} url={siteurl!r} "
                  f"(active={_active_count}/{MAX_WORKERS})")
            token = solve(sitekey, siteurl, timeout=timeout)
            elapsed = round(time.time() - t0, 2)
            print(f"[service] solved in {elapsed}s  token={token[:20]}...")
            self.send_json(200, {"token": token, "elapsed": elapsed})
        except Exception as exc:
            elapsed = round(time.time() - t0, 2)
            print(f"[service] error after {elapsed}s: {exc}")
            self.send_json(500, {"error": str(exc)})
        finally:
            with _count_lock:
                _active_count -= 1
            _worker_sem.release()

    def do_GET(self):
        if self.path == "/health":
            with _count_lock:
                self.send_json(200, {
                    "status": "ok",
                    "workers": MAX_WORKERS,
                    "active": _active_count,
                    "queued": _queued_count,
                })
        else:
            self.send_json(404, {"error": "use POST /solve"})


if __name__ == "__main__":
    xvfb_proc = _ensure_display()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[service] Turnstile solver service running on http://0.0.0.0:{PORT}")
    print(f"[service] worker pool: {MAX_WORKERS} concurrent Chrome instances "
          f"(set MAX_WORKERS env var to change)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[service] shutting down")
        server.server_close()
        if xvfb_proc:
            xvfb_proc.terminate()
