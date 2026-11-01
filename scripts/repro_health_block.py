"""Measure whether GET /health blocks while POST /seance is generating.

Starts the real server (uvicorn) on a free port against a throwaway database,
indexes a tiny git repo, waits for the model warm-up, then starts one real
séance and times GET /health in a loop while it runs.

Run:
    python scripts/repro_health_block.py

Nothing here touches data/necropsy.db: NECROPSY_DB_PATH points at a temp dir.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]

FIXTURE_COMMITS = [
    ("2024-01-15T10:00:00Z", "Jan initial commit", "Add a lexer skeleton"),
    ("2024-02-10T10:00:00Z", "Feb feature add", "Parse the first grammar"),
    ("2024-03-20T10:00:00Z", "Mar cleanup pass", "Drop the unused lexer"),
    ("2024-04-05T10:00:00Z", "Apr docs", "Write the README"),
    ("2024-05-11T10:00:00Z", "May refactor", "Split the parser into modules"),
    ("2024-06-02T10:00:00Z", "Jun last gasp", "Nothing works, giving up"),
]


def free_port() -> int:
    """Ask the OS for an unused TCP port on the loopback interface."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_fixture_repo(path: Path) -> Path:
    """Create a real git repo with commits on known dates."""
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str, env: dict | None = None) -> None:
        subprocess.run(["git", *args], cwd=str(path), check=True,
                       capture_output=True, text=True, env=env)

    run("init", "-q")
    run("config", "user.name", "Test Author")
    run("config", "user.email", "test@example.com")
    run("config", "commit.gpgsign", "false")

    for i, (date, subject, body) in enumerate(FIXTURE_COMMITS, 1):
        (path / f"module_{i}.py").write_text(f"# {subject}\n", encoding="utf-8")
        run("add", "--", f"module_{i}.py")
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(
            ["git", "commit", "-q", "-m", subject, "-m", body],
            cwd=str(path), check=True, capture_output=True, text=True, env=env,
        )
    return path


def wait_for(predicate, timeout_s: float, interval_s: float = 1.0, label: str = ""):
    """Poll a predicate until it is true or the timeout expires."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except requests.exceptions.RequestException as exc:
            last = exc
        time.sleep(interval_s)
    raise SystemExit(f"timed out waiting for {label or 'condition'}; last={last!r}")


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="necropsy_repro_"))
    db_path = tmp / "necropsy.db"
    log_path = tmp / "server.log"
    repo_path = make_fixture_repo(tmp / "fixture_repo")

    env = {
        **os.environ,
        "NECROPSY_DB_PATH": str(db_path),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }

    print(f"server: {base}")
    print(f"db:     {db_path}")
    print(f"repo:   {repo_path}")
    print("starting uvicorn ...", flush=True)

    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "necropsy.api.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        wait_for(
            lambda: requests.get(f"{base}/health", timeout=30).status_code == 200,
            timeout_s=float(os.environ.get("REPRO_START_SECONDS", "120")),
            label="server start",
        )
        print("server is up", flush=True)

        # Wait for the one-time model load so the séance timing is a warm run.
        print("waiting for the LLM warm-up (background thread) ...", flush=True)
        t_warm = time.time()
        wait_for(
            lambda: requests.get(f"{base}/health", timeout=30).json()
            .get("ollama", {}).get("warmup", {}).get("state") == "warm",
            timeout_s=600, interval_s=2.0, label="llm warm-up",
        )
        print(f"model warm after {time.time() - t_warm:.1f}s", flush=True)

        # Index the fixture repo through the real endpoint.
        job = requests.post(f"{base}/exhume", json={"source": str(repo_path)},
                            timeout=60).json()
        print(f"indexing job {job['job_id']} ...", flush=True)
        final = wait_for(
            lambda: (lambda j: j if j.get("status") in ("ready", "failed") else None)(
                requests.get(f"{base}/exhume/{job['job_id']}", timeout=60).json()
            ),
            timeout_s=600, interval_s=1.0, label="indexing",
        )
        if final.get("status") != "ready":
            raise SystemExit(f"indexing failed: {final.get('error')}")
        repo_id = final["repo_id"]
        last_at = str(final["last_commit_at"])[:10]
        print(f"indexed repo {repo_id}: {final['commit_count']} commits, "
              f"{final['embeddings']} embeddings", flush=True)

        seance_result: dict = {}

        def ask() -> None:
            started = time.time()
            try:
                resp = requests.post(
                    f"{base}/seance",
                    json={"repo_id": repo_id, "as_of": last_at,
                          "question": "What did this project try to become?"},
                    timeout=900,
                )
                seance_result["seconds"] = time.time() - started
                seance_result["status"] = resp.status_code
                seance_result["body"] = resp.json()
            except Exception as exc:  # noqa: BLE001 - report, never hide
                seance_result["seconds"] = time.time() - started
                seance_result["status"] = "EXCEPTION"
                seance_result["body"] = f"{type(exc).__name__}: {exc}"

        print("\n--- starting /seance, polling /health every 1 s ---", flush=True)
        thread = threading.Thread(target=ask, name="seance-caller", daemon=True)
        thread.start()
        time.sleep(0.3)

        max_poll = float(os.environ.get("REPRO_MAX_SECONDS", "900"))
        poll_started = time.time()
        print(f"{'#':>3} {'seconds':>8} {'http':>6} {'status':>10} "
              f"{'reachable':>10} {'busy':>6} warmup", flush=True)

        samples = []
        while thread.is_alive() and (time.time() - poll_started) < max_poll:
            t0 = time.time()
            try:
                resp = requests.get(f"{base}/health", timeout=20)
                elapsed = time.time() - t0
                body = resp.json()
                sample = {
                    "seconds": elapsed,
                    "http": resp.status_code,
                    "status": body.get("status"),
                    "reachable": body.get("ollama", {}).get("reachable"),
                    "busy": body.get("busy"),
                    "warmup": body.get("ollama", {}).get("warmup", {}).get("state"),
                }
            except Exception as exc:  # noqa: BLE001 - a blocked health is the bug
                sample = {
                    "seconds": time.time() - t0,
                    "http": "EXC",
                    "status": f"{type(exc).__name__}: {str(exc)[:60]}",
                    "reachable": None, "busy": None, "warmup": None,
                }
            samples.append(sample)
            # Print immediately so an interrupted run still shows the timings.
            print(f"{len(samples):>3} {sample['seconds']:>8.2f} {str(sample['http']):>6} "
                  f"{str(sample['status'])[:10]:>10} {str(sample['reachable']):>10} "
                  f"{str(sample['busy']):>6} {sample['warmup']}", flush=True)
            time.sleep(1.0)

        if thread.is_alive():
            print(f"\n(polling stopped after {max_poll:.0f}s; the séance was still running)")
        else:
            thread.join()

        times = [s["seconds"] for s in samples]
        if times:
            times_sorted = sorted(times)
            median = times_sorted[len(times_sorted) // 2]
            print(f"\n/health while generating: n={len(times)} "
                  f"min={min(times):.2f}s median={median:.2f}s max={max(times):.2f}s")
            slow = [t for t in times if t >= 1.0]
            print(f"samples >= 1 s: {len(slow)}/{len(times)}")

        if not seance_result:
            print("\nseance: still generating when polling stopped")
        else:
            print(f"\nseance: status={seance_result.get('status')} "
                  f"seconds={seance_result.get('seconds', 0):.1f}")
            body = seance_result.get("body")
            if isinstance(body, dict):
                print(f"  answer: {str(body.get('answer'))[:300]}")
                print(f"  verified={body.get('verified')} "
                      f"claims_total={body.get('claims_total')} "
                      f"latency_ms={body.get('latency_ms')} "
                      f"evidence={len(body.get('evidence') or [])} "
                      f"tokens={(body.get('usage') or {}).get('total_tokens')}")
                if body.get("detail"):
                    print(f"  detail: {json.dumps(body['detail'])[:400]}")
            else:
                print(f"  body: {body}")

        print("\n--- server log lines about evidence/tokens ---")
        log_file.flush()
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "evidence" in line.lower() or "token" in line.lower():
                print(line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        print(f"\nserver log kept at {log_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
