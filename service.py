"""
Cloudflare Turnstile Solver Service
------------------------------------
Listens on http://0.0.0.0:8191 (or PORT env var).

POST /solve
  Body (JSON): {"sitekey": "...", "siteurl": "https://example.com"}
  Response:    {"token": "...", "elapsed": 4.23}
               {"error": "..."} on failure
"""

import os
import platform
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional
import json

from solver import solve


PORT = int(os.environ.get("PORT", 8191))


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in its own thread so solves don't block each other."""
    daemon_threads = True


def _ensure_display() -> Optional[subprocess.Popen]:
    """On Linux headless servers, start a virtual display so Chrome can run."""
    if platform.system() != "Linux":
        return None
    if os.environ.get("DISPLAY"):
        return None
    xvfb = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)  # give Xvfb a moment to start
    print("[service] started Xvfb on :99")
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

        print(f"[service] solving sitekey={sitekey!r} url={siteurl!r}")
        t0 = time.time()
        try:
            token = solve(sitekey, siteurl, timeout=timeout)
            elapsed = round(time.time() - t0, 2)
            print(f"[service] solved in {elapsed}s  token={token[:20]}...")
            self.send_json(200, {"token": token, "elapsed": elapsed})
        except Exception as exc:
            elapsed = round(time.time() - t0, 2)
            print(f"[service] error after {elapsed}s: {exc}")
            self.send_json(500, {"error": str(exc)})

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "use POST /solve"})


if __name__ == "__main__":
    xvfb_proc = _ensure_display()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[service] Turnstile solver service running on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[service] shutting down")
        server.server_close()
        if xvfb_proc:
            xvfb_proc.terminate()
