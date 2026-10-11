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

import asyncio
import json
import os
import platform
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional

import nodriver as uc
from solver import _find_chrome, _get_profile_dir, _start_xvfb_if_needed, solve_on_page, start_browser

PORT = int(os.environ.get("PORT", 8191))

_active_count = 0
_queued_count = 0
_count_lock = threading.Lock()


class PersistentSolver:
    """Maintains a persistent Chrome instance to eliminate cold-start overhead and memory churn."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._browser: Optional[uc.Browser] = None
        self._page: Optional[uc.Tab] = None
        self._lock = threading.Lock()
        self._started = False

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self):
        if self._started:
            return
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._init_browser(), self._loop)
        fut.result(timeout=30)
        self._started = True

    async def _init_browser(self):
        self._browser = await start_browser()
        self._page = await self._browser.get("about:blank")

    def solve(self, sitekey: str, siteurl: str, timeout: int = 45) -> str:
        with self._lock:
            fut = asyncio.run_coroutine_threadsafe(
                self._solve_async(sitekey, siteurl, timeout), self._loop
            )
            return fut.result(timeout=timeout + 10)

    async def _solve_async(self, sitekey: str, siteurl: str, timeout: int) -> str:
        try:
            token = await solve_on_page(self._page, sitekey, siteurl, timeout)
            await self._page.get("about:blank")
            return token
        except Exception:
            # If the page or browser crashed, attempt recovery
            try:
                await self._page.get("about:blank")
            except Exception:
                try:
                    self._browser.stop()
                except Exception:
                    pass
                await self._init_browser()
            raise

    def stop(self):
        if self._browser:
            try:
                self._browser.stop()
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)


_solver = PersistentSolver()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in its own thread so requests don't block socket acceptance."""

    daemon_threads = True


def _ensure_display() -> Optional[subprocess.Popen]:
    """On Linux headless servers, start a virtual display so Chrome can run."""
    if platform.system() != "Linux":
        return None
    if os.environ.get("DISPLAY"):
        return None
    xvfb = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1024x768x16"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
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

        global _active_count, _queued_count

        with _count_lock:
            _queued_count += 1
        print(
            f"[service] queued — sitekey={sitekey!r} url={siteurl!r} "
            f"(active={_active_count} queued={_queued_count})"
        )

        with _count_lock:
            _queued_count -= 1
            _active_count += 1

        t0 = time.time()
        try:
            print(f"[service] solving sitekey={sitekey!r} url={siteurl!r} (active={_active_count})")
            token = _solver.solve(sitekey, siteurl, timeout=timeout)
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

    def do_GET(self):
        if self.path == "/health":
            with _count_lock:
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "active": _active_count,
                        "queued": _queued_count,
                    },
                )
        else:
            self.send_json(404, {"error": "use POST /solve"})


if __name__ == "__main__":
    xvfb_proc = _ensure_display()
    print("[service] initializing persistent Chrome browser...")
    _solver.start()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[service] Turnstile solver service running on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[service] shutting down")
        server.server_close()
        _solver.stop()
        if xvfb_proc:
            xvfb_proc.terminate()
