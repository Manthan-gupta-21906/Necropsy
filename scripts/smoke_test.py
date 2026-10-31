"""Necropsy smoke test: real server, real ingestion, real Ollama if present.

Starts uvicorn with subprocess.Popen (argument list, free port, temp data dir),
creates a local fixture git repo with known commit dates, indexes it through
the API, then checks /repos, /repos/{id}/range, /autopsy at two dates and one
real séance. Terminates only the process it started.

Usage:  python scripts/smoke_test.py
Exit code 0 = every check passed. 2 = something failed (details printed).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_COMMITS = [
    ("2024-01-15T10:00:00Z", "Initial import of the parser skeleton",
     [("README.md", "# parser\n"), ("lexer.py", "def lex():\n    pass\n")], []),
    ("2024-02-10T10:00:00Z", "Add tokenizer for the parser",
     [("parser.py", "def parse():\n    pass\n")], []),
    ("2024-03-20T10:00:00Z", "Remove the abandoned lexer experiment",
     [("cleanup.py", "# cleanup\n")], ["lexer.py"]),
]


# --- helpers ---------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None, timeout: float = 200.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def make_fixture_repo() -> str:
    """Create a temp git repo with three commits on known dates."""
    tmpdir = tempfile.mkdtemp(prefix="necropsy_smoke_fixture_")
    run = lambda *args, **kw: subprocess.run(  # noqa: E731
        ["git", *args], cwd=tmpdir, capture_output=True, text=True, **kw
    )
    run("init", "-q")
    run("config", "user.name", "Smoke Tester")
    run("config", "user.email", "smoke@example.com")
    run("config", "commit.gpgsign", "false")

    for date, message, added, removed in FIXTURE_COMMITS:
        for name, content in added:
            Path(tmpdir, name).write_text(content, encoding="utf-8")
        if added:
            run("add", "--", *[name for name, _ in added])
        for name in removed:
            run("rm", "-q", "--", name)
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        res = subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=tmpdir, capture_output=True, text=True, env=env,
        )
        if res.returncode != 0:
            raise RuntimeError(f"fixture commit failed: {res.stderr}")
    return tmpdir


def wait_for(url: str, proc: subprocess.Popen, seconds: int = 90) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited before becoming ready")
        try:
            status, _ = http("GET", url, timeout=3)
            if status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise RuntimeError(f"server did not answer {url} within {seconds}s")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


# --- main ------------------------------------------------------------------

def main() -> int:
    results = []

    fixture = make_fixture_repo()
    tmpdir = tempfile.mkdtemp(prefix="necropsy_smoke_")
    port = free_port()
    db_path = Path(tmpdir) / "smoke.db"
    env = {
        **os.environ,
        "NECROPSY_DB_PATH": str(db_path),
        "PYTHONUNBUFFERED": "1",
    }
    log_path = Path(tmpdir) / "server.log"
    log = open(log_path, "w", encoding="utf-8")

    print(f"fixture repo : {fixture}")
    print(f"temp database: {db_path}")
    print(f"server log   : {log_path}")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "necropsy.api.server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    try:
        wait_for(base + "/health", proc)
        print(f"server ready on {base} (pid {proc.pid})\n")

        # --- health ------------------------------------------------------
        status, health = http("GET", base + "/health")
        print("GET /health ->", status)
        print(json.dumps(health, indent=2))
        results.append(check("health returns 200", status == 200))
        results.append(check(
            "health reports real embedding state",
            health.get("embeddings", {}).get("state") in ("ready", "loading"),
            str(health.get("embeddings", {}).get("state")),
        ))
        ollama_up = health.get("ollama", {}).get("reachable", False)
        model_ok = health.get("ollama", {}).get("model_installed", False)
        print(f"ollama reachable={ollama_up} model_installed={model_ok} "
              f"model={health.get('ollama', {}).get('model')}")

        # --- /repos before indexing --------------------------------------
        status, repos = http("GET", base + "/repos")
        print(f"\nGET /repos (before) -> {status} {json.dumps(repos)[:200]}")
        results.append(check("/repos returns 200 (was the 500)", status == 200))
        results.append(check("/repos has a repos list", isinstance(repos.get("repos"), list)))

        # --- exhume ------------------------------------------------------
        status, job = http("POST", base + "/exhume", {"source": fixture})
        print(f"\nPOST /exhume -> {status} {json.dumps(job)}")
        results.append(check("/exhume accepted the local repo", status == 200, str(status)))
        job_id = job.get("job_id")
        results.append(check("/exhume returned a job id", bool(job_id)))

        seen_stages = []
        job_final = {}
        deadline = time.time() + 300
        while job_id and time.time() < deadline:
            try:
                status, job_final = http("GET", f"{base}/exhume/{job_id}", timeout=60)
            except Exception as exc:  # noqa: BLE001 - keep polling on a slow stage
                print(f"  poll error ({type(exc).__name__}); retrying")
                time.sleep(1)
                continue
            stage = job_final.get("stage")
            if stage and (not seen_stages or seen_stages[-1] != stage):
                seen_stages.append(stage)
                print(f"  job {job_id}: stage={stage} progress={job_final.get('progress')}")
            if job_final.get("status") in ("ready", "failed"):
                break
            time.sleep(0.5)

        print(f"  final job: {json.dumps(job_final)}")
        results.append(check("indexing job reached ready", job_final.get("status") == "ready",
                             str(job_final.get("error"))))
        print(f"  stages observed: {seen_stages}")

        # --- /repos after indexing ---------------------------------------
        status, repos = http("GET", base + "/repos")
        print(f"\nGET /repos (after) -> {status}")
        print(json.dumps(repos, indent=2))
        repo_list = repos.get("repos") or []
        results.append(check("repo was indexed", len(repo_list) == 1, f"{len(repo_list)} repos"))
        if not repo_list:
            return 2
        repo = repo_list[0]
        repo_id = repo["id"]
        results.append(check("repo has first_commit_at", bool(repo.get("first_commit_at")),
                             str(repo.get("first_commit_at"))))
        results.append(check("repo has last_commit_at", bool(repo.get("last_commit_at")),
                             str(repo.get("last_commit_at"))))
        results.append(check("repo commit_count == 3", repo.get("commit_count") == 3,
                             str(repo.get("commit_count"))))

        # --- range -------------------------------------------------------
        status, rng = http("GET", f"{base}/repos/{repo_id}/range")
        print(f"\nGET /repos/{repo_id}/range -> {status} {json.dumps(rng)}")
        results.append(check("range returns both bounds",
                             bool(rng.get("first_commit_at")) and bool(rng.get("last_commit_at"))))

        # --- autopsy at two dates ---------------------------------------
        autopsies = {}
        for as_of in ("2024-01-31", "2024-03-31"):
            status, data = http("GET", f"{base}/autopsy?repo_id={repo_id}&as_of={as_of}")
            autopsies[as_of] = data
            print(f"\nGET /autopsy as_of={as_of} -> {status}")
            print(json.dumps(data, indent=2)[:1200])
            results.append(check(f"autopsy {as_of} returns 200", status == 200))

        early = autopsies["2024-01-31"]
        late = autopsies["2024-03-31"]
        results.append(check(
            "autopsy count changes with as_of",
            early.get("commit_count") == 1 and late.get("commit_count") == 3,
            f"2024-01-31={early.get('commit_count')}, 2024-03-31={late.get('commit_count')}",
        ))
        results.append(check(
            "autopsy never shows a commit after as_of",
            (early.get("last_commit_at") or "") <= "2024-01-31T23:59:59Z",
            str(early.get("last_commit_at")),
        ))
        results.append(check(
            "tombstone appears only after it died",
            early.get("tombstone_count") == 0 and late.get("tombstone_count") >= 1,
            f"early={early.get('tombstone_count')}, late={late.get('tombstone_count')}",
        ))

        # --- séance ------------------------------------------------------
        # Re-probe health: the first probe may have landed while Ollama was
        # busy loading the model.
        _, health_now = http("GET", base + "/health")
        ollama_up = health_now.get("ollama", {}).get("reachable", False) or \
            health_now.get("ollama", {}).get("warmup", {}).get("state") == "warm"
        model_ok = health_now.get("ollama", {}).get("model_installed", False) or ollama_up
        print(f"\nhealth before séance: status={health_now.get('status')} "
              f"ollama_reachable={health_now.get('ollama', {}).get('reachable')} "
              f"model_installed={health_now.get('ollama', {}).get('model_installed')} "
              f"warmup={health_now.get('ollama', {}).get('warmup', {}).get('state')} "
              f"embeddings={health_now.get('embeddings', {}).get('state')}")

        question = "What did the project add in February 2024?"
        print(f"\nPOST /seance -> question: {question!r} (as_of=2024-03-31)")
        started = time.time()
        status, seance = http(
            "POST", base + "/seance",
            {"repo_id": repo_id, "as_of": "2024-03-31", "question": question},
            timeout=300,
        )
        elapsed = time.time() - started
        print(f"  -> {status} in {elapsed:.1f}s")
        print(json.dumps(seance, indent=2)[:2500])

        if not (ollama_up and model_ok):
            print("\nOllama is not reachable or the model is missing: "
                  "séance skipped, this is reported, not hidden.")
        else:
            results.append(check("seance returns 200", status == 200, str(status)))
            if status == 200:
                results.append(check("seance answer is non-empty",
                                     bool(seance.get("answer"))))
                results.append(check("seance reports a real latency",
                                     isinstance(seance.get("latency_ms"), int)))
                claims = seance.get("claims") or []
                print(f"  verified claims: {len(claims)} of {seance.get('claims_total')} proposed")
                for c in claims:
                    print(f"    - {c['commit_hash'][:8]} {c['at']} {c['subject'][:60]}")
                if not claims:
                    results.append(check("empty claim set yields the refusal text",
                                         seance.get("answer") == "The dead do not remember that."))

        # --- refusal on an early date -----------------------------------
        status, seance_early = http(
            "POST", base + "/seance",
            {"repo_id": repo_id, "as_of": "2023-06-01", "question": "What is this project?"},
            timeout=300,
        )
        print(f"\nPOST /seance as_of=2023-06-01 (before any commit) -> {status}")
        print(json.dumps(seance_early, indent=2)[:800])
        results.append(check(
            "no evidence before the first commit yields the exact refusal",
            seance_early.get("answer") == "The dead do not remember that.",
            str(seance_early.get("answer")),
        ))

        # --- invalid source ---------------------------------------------
        status, bad = http("POST", base + "/exhume", {"source": "--upload-pack=evil"})
        print(f"\nPOST /exhume with an invalid source -> {status} {json.dumps(bad)}")
        results.append(check("invalid source rejected with 400", status == 400, str(status)))

        # --- health after a séance (latency recorded) --------------------
        status, health2 = http("GET", base + "/health")
        print(f"\nGET /health (after séance) -> last_answer_ms="
              f"{health2.get('last_answer_ms')}")

        print("\n" + "=" * 70)
        passed = sum(1 for r in results if r)
        print(f"RESULT: {passed}/{len(results)} checks passed")
        print("=" * 70)
        return 0 if passed == len(results) else 2

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
        print(f"\nserver terminated; log kept at {log_path}")


if __name__ == "__main__":
    raise SystemExit(main())
