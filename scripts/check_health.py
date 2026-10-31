"""Item 2/3 check: real /health states from a real uvicorn process.

Starts uvicorn with subprocess.Popen (argument list, free port, temp DB),
polls /health until embeddings and the LLM warm-up settle, prints every
observed change, then terminates only its own process.

Usage:  python scripts/check_health.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    port = free_port()
    tmpdir = tempfile.mkdtemp(prefix="necropsy_health_")
    env = {
        **os.environ,
        "NECROPSY_DB_PATH": str(Path(tmpdir) / "test.db"),
        "PYTHONUNBUFFERED": "1",
    }
    log = open(Path(tmpdir) / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "necropsy.api.server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for the server socket.
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                get(base + "/health", timeout=3)
                break
            except Exception:
                if proc.poll() is not None:
                    print("server exited early; log:")
                    print(Path(tmpdir, "server.log").read_text(encoding="utf-8"))
                    return 1
                time.sleep(0.5)

        print(f"server on {base} (pid {proc.pid}), temp db {env['NECROPSY_DB_PATH']}")
        print(f"{'t':>4}  {'status':<8} {'embeddings':<12} {'ollama':<8} "
              f"{'model_installed':<16} warmup")
        print("-" * 78)

        start = time.time()
        last = None
        final = None
        while time.time() - start < 300:
            try:
                h = get(base + "/health")
            except Exception as exc:
                print("health error:", type(exc).__name__, exc)
                time.sleep(1)
                continue
            row = (
                h.get("status"),
                (h.get("embeddings") or {}).get("state"),
                "up" if (h.get("ollama") or {}).get("reachable") else "down",
                "yes" if (h.get("ollama") or {}).get("model_installed") else "no",
                ((h.get("ollama") or {}).get("warmup") or {}).get("state"),
            )
            if row != last:
                print(f"{time.time() - start:4.0f}  {row[0]:<8} {str(row[1]):<12} "
                      f"{row[2]:<8} {row[3]:<16} {row[4]}")
                last = row
            final = h
            if row[0] == "ok":
                break
            time.sleep(1)

        print("\nFINAL /health JSON")
        print(json.dumps(final, indent=2))
        return 0 if final and final.get("status") == "ok" else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
        print(f"\nserver terminated (exit {proc.returncode}); log: {Path(tmpdir, 'server.log')}")


if __name__ == "__main__":
    raise SystemExit(main())
