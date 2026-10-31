"""Item 5/6 check: drive the real UI in headless Chrome against a real server.

Starts uvicorn (temp DB), indexes a local fixture repo, then loads the page in
headless Chrome twice: at the default boundary and with ?as_of=2024-01-31.
Prints the rendered Machine Status, repo, slider and autopsy, and fails if the
page logs a console error or anything is still on "loading".

Usage:  python scripts/check_ui.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

FIXTURE = [
    ("2024-01-15T10:00:00Z", "Initial import of the parser skeleton",
     [("README.md", "# parser\n"), ("lexer.py", "def lex():\n    pass\n")], []),
    ("2024-02-10T10:00:00Z", "Add tokenizer for the parser",
     [("parser.py", "def parse():\n    pass\n")], []),
    ("2024-03-20T10:00:00Z", "Remove the abandoned lexer experiment",
     [("cleanup.py", "# cleanup\n")], ["lexer.py"]),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, body=None, timeout=200.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def make_fixture() -> str:
    tmp = tempfile.mkdtemp(prefix="necropsy_ui_fixture_")
    run = lambda *a, **k: subprocess.run(["git", *a], cwd=tmp, capture_output=True,
                                        text=True, **k)  # noqa: E731
    run("init", "-q")
    run("config", "user.name", "UI Tester")
    run("config", "user.email", "ui@example.com")
    run("config", "commit.gpgsign", "false")
    for date, msg, added, removed in FIXTURE:
        for name, content in added:
            Path(tmp, name).write_text(content, encoding="utf-8")
        if added:
            run("add", "--", *[n for n, _ in added])
        for name in removed:
            run("rm", "-q", "--", name)
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        res = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=tmp,
                             capture_output=True, text=True, env=env)
        if res.returncode != 0:
            raise RuntimeError(res.stderr)
    return tmp


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("No Chrome or Edge executable found")


def dump_dom(chrome: str, url: str, profile: str, wait_ms: int = 12000):
    """Load a URL in headless Chrome, return (dom, console_errors)."""
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--user-data-dir=" + profile,
        "--enable-logging=stderr",
        "--virtual-time-budget=" + str(wait_ms),
        "--dump-dom",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                          encoding="utf-8", errors="replace")
    errors = []
    for line in (proc.stderr or "").splitlines():
        if "CONSOLE" not in line:
            continue
        if "Uncaught" in line or "Error:" in line or "error" in line.lower():
            errors.append(line.strip())
    return proc.stdout or "", errors


def text_of(dom: str, element_id: str) -> str:
    m = re.search(r'id="' + re.escape(element_id) + r'"[^>]*>(.*?)</', dom, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def attr_of(dom: str, element_id: str, attr: str) -> str:
    m = re.search(r'id="' + re.escape(element_id) + r'"[^>]*\b' + attr + r'="([^"]*)"', dom)
    return m.group(1) if m else ""


def count_class(dom: str, cls: str) -> int:
    return len(re.findall(r'class="[^"]*\b' + re.escape(cls) + r'\b', dom))


def main() -> int:
    chrome = find_chrome()
    print("browser:", chrome)

    fixture = make_fixture()
    tmpdir = tempfile.mkdtemp(prefix="necropsy_ui_")
    port = free_port()
    db_path = Path(tmpdir) / "ui.db"
    log = open(Path(tmpdir) / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "necropsy.api.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env={**os.environ, "NECROPSY_DB_PATH": str(db_path), "PYTHONUNBUFFERED": "1"},
        stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    results = []
    profile = str(Path(tmpdir) / "chrome-profile")

    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if http("GET", base + "/health", timeout=3)[0] == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        print(f"server: {base} (pid {proc.pid}), fixture: {fixture}")

        # Wait for embeddings to be ready so the UI shows a settled state.
        deadline = time.time() + 180
        while time.time() < deadline:
            _, h = http("GET", base + "/health")
            if h.get("embeddings", {}).get("state") == "ready":
                break
            time.sleep(1)

        status, job = http("POST", base + "/exhume", {"source": fixture})
        print("POST /exhume ->", status, job)
        job_id = job["job_id"]
        deadline = time.time() + 300
        while time.time() < deadline:
            _, job = http("GET", f"{base}/exhume/{job_id}")
            if job.get("status") in ("ready", "failed"):
                break
            time.sleep(0.5)
        print("indexing:", job.get("status"), job.get("stage"),
              "commits=", job.get("commit_count"), "embeddings=", job.get("embeddings"))
        results.append(("indexing ready", job.get("status") == "ready"))

        # --- default view (last commit) ----------------------------------
        dom, errors = dump_dom(chrome, base + "/", profile)
        Path(tmpdir, "dom-default.html").write_text(dom, encoding="utf-8")
        print("\n--- rendered Machine Status ---")
        for element_id in ("status-pill", "status-model", "status-ollama",
                           "status-embeddings", "status-latency"):
            print(f"  {element_id:18} = {text_of(dom, element_id)!r}")
        print("--- rendered repository / timeline ---")
        print("  repo-name          =", repr(text_of(dom, "repo-name")))
        print("  date-display       =", repr(text_of(dom, "date-display")))
        print("  slider min/max     =", attr_of(dom, "date-slider", "min"),
              "/", attr_of(dom, "date-slider", "max"),
              "value =", attr_of(dom, "date-slider", "value"))
        print("--- rendered autopsy ---")
        print("  stat cards         =", count_class(dom, "stat-label"))
        print("  recent commits     =", len(re.findall(r'class="commit-hash"', dom)))
        print("  chart bars         =", len(re.findall(r"<rect", dom)))

        results.append(("no console errors", not errors))
        for line in errors[:10]:
            print("  CONSOLE:", line)

        results.append(("status pill is ok", text_of(dom, "status-pill") == "ok"))
        results.append(("model name is real", "qwen3" in text_of(dom, "status-model")))
        results.append(("embeddings ready", "ready" in text_of(dom, "status-embeddings")))
        results.append(("no element stuck on loading",
                        not re.search(r"loading|checking", dom.split("<main")[0])))
        results.append(("repo name rendered", bool(text_of(dom, "repo-name"))))
        results.append(("date display rendered", "2024" in text_of(dom, "date-display")))
        results.append(("slider bounds differ",
                        attr_of(dom, "date-slider", "min") != attr_of(dom, "date-slider", "max")))
        results.append(("six or more stat cards", count_class(dom, "stat-label") >= 6))
        results.append(("three recent commits at last date",
                        len(re.findall(r'class="commit-hash"', dom)) >= 3))

        # --- deep link to an earlier boundary ----------------------------
        dom2, errors2 = dump_dom(chrome, base + "/?as_of=2024-01-31", profile)
        Path(tmpdir, "dom-early.html").write_text(dom2, encoding="utf-8")
        print("\n--- ?as_of=2024-01-31 ---")
        print("  date-display       =", repr(text_of(dom2, "date-display")))
        print("  stat cards         =", count_class(dom2, "stat-label"))
        print("  recent commits     =", len(re.findall(r'class="commit-hash"', dom2)))
        print("  console errors     =", len(errors2))
        results.append(("no console errors on deep link", not errors2))
        results.append(("deep link date applied",
                        "2024-01-31" in text_of(dom2, "date-display") or
                        "31 Jan 2024" in text_of(dom2, "date-display")))
        early_commits = len(re.findall(r'class="commit-hash"', dom2))
        results.append(("earlier boundary shows fewer commits",
                        early_commits < len(re.findall(r'class="commit-hash"', dom))))

        print("\n" + "=" * 66)
        for name, ok in results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        passed = sum(1 for _, ok in results if ok)
        print(f"RESULT: {passed}/{len(results)} UI checks passed")
        print("=" * 66)
        return 0 if passed == len(results) else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(profile, ignore_errors=True)
        print(f"\nartifacts kept in {tmpdir}")


if __name__ == "__main__":
    raise SystemExit(main())
