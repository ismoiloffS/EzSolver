"""
Cloudflare Turnstile Solver — Client
--------------------------------------
Sends a solve request to the running service.py and prints the token.

Usage:
    python clientsend.py <sitekey> <siteurl> [timeout_seconds]

Example:
    python clientsend.py 0x4AAAAAAAA... https://example.com 30
"""

import sys
import json
import urllib.request
import urllib.error

SERVICE_URL = "http://127.0.0.1:8191/solve"


def request_token(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    payload = json.dumps({
        "sitekey": sitekey,
        "siteurl": siteurl,
        "timeout": timeout,
    }).encode()

    req = urllib.request.Request(
        SERVICE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout + 20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        raise RuntimeError(data.get("error", str(e)))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach service at {SERVICE_URL}: {e.reason}")

    if "error" in data:
        raise RuntimeError(data["error"])

    return data["token"], data.get("elapsed", 0)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    sitekey = sys.argv[1]
    siteurl = sys.argv[2]
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 45

    try:
        token, elapsed = request_token(sitekey, siteurl, timeout)
        print(f"Token ({elapsed}s): {token}")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
