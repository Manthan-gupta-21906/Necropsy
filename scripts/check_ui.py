"""Item 5/6 check: drive the real UI in headless Chrome against a real server.

Starts uvicorn (temp DB), indexes a local fixture repo, then drives headless Chrome
twice: at the default boundary and with ?as_of=2024-01-31.
Prints the rendered Machine Status, repo, slider and autopsy, and fails if the
page logs a console error or anything is still on "loading".
Usage: python scripts/check_ui.py
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
    ( "2024-01-15T10:00:00Z", "Initial import",
      [("README.md", "# parser\n"), ("lexer.py", "def lex():\n    pass\n")], []),
    ( "2024-02-10T10:00:00Z", "Add tokenizer",
      [("parser.py", "def parse(): pass\n")], []),
    ( "2024-03-20T10:00:00Z", "Remove lexer",
      [("cleanup.py", "# cleanup\n")], ["lexer.py"]),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method, url, body=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body_data = resp.read().decode()
        parsed = json.loads(body_data or "{}")
        return resp.status, parsed


def make_fixture() -> str:
    tmp = tempfile.mkdtemp(prefix="necropsy_ui_fixture_")
    run = lambda *a, **k: subprocess.run(["git", *a], cwd=tmp, capture_output=True,
                                         text=True, **k)
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
        res = subprocess.run(["git", "commit", "-q", "-m", "UI commit"], cwd=tmp,
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
    m = re.search(r'id="' + re.escape(element_id) + r'"[^>]>(.*?)</', dom, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def attr_of(dom: str, element_id: str, attr: str) -> str:
    m = re.search(r'id="' + re.escape(element_id) + r'"[^>]*\\b' + attr + r'="([^"]*)"', dom)
    return m.group(1) if m else ""


def count_class(dom: str, cls: str) -> int:
    return len(re.findall(r'class="[^\"]*\\b' + re.escape(cls) + r'\\b', dom))


def wait_for_stats(chrome: str, base: str, profile: str, max_wait: int = 60, poll: float = 0.5) -> bool:
    """Poll until stat cards appear or timeout.

    Returns True if stats appear, False if timeout/error.
    """
    elapsed = 0
    while elapsed < max_wait:
        dom, errors = dump_dom(chrome, base + "/#/app", profile)
        stats = count_class(dom, "stat-label")
        if stats >= 6:
            return True
        # Check for visible error
        error_text = text_of(dom, "autopsy-error")
        if error_text and error_text != "Could not load the autopsy.":
            return False
        time.sleep(poll)
        elapsed += poll
    return False


def main() -> int:
    chrome = find_chrome()
    print("browser:", chrome)

    # Read the actual model from env var, falling back to Ollama list
    env_llm = os.environ.get("NECROPSY_LLM_MODEL", "")
    if not env_llm:
        try:
            req = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5)
            data = json.loads(req.read())
            if data.get("models"):
                env_llm = data["models"][0]["name"]
        except Exception:
            env_llm = "qwen3:4b"
    print("Using LLM model:", env_llm)

    fixture = make_fixture()
    tmpdir = tempfile.mkdtemp(prefix="necropsy_ui_")
    port = free_port()
    db_path = Path(tmpdir) / "ui.db"
    log = open(Path(tmpdir) / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "necropsy.api.server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env={**os.environ, "NECROPSY_DB_PATH": str(db_path),
             "NECROPSY_LLM_MODEL": env_llm, "PYTHONUNBUFFERED": "1"},
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
            except Exception:
                pass
            time.sleep(0.5)
        print("server:", base, "(pid", proc.pid, ")")

        # Wait for embeddings to be ready so the UI shows a settled state.
        deadline = time.time() + 180
        while time.time() < deadline:
            status, h = http("GET", base + "/health")
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

        # Navigate Chrome to #/app so the repo selector auto-loads and
        # the autopsy section with stats becomes visible in the DOM.
        print("Navigating Chrome to #/app...")
        chrome = find_chrome()
        chrome_args = (
            "--headless=new --disable-gpu --no-first-run --no-default-browser-check "
            "--disable-extensions --user-data-dir=" + profile
        )
        subprocess.Popen(
            [chrome, chrome_args, base + "/#/app"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        # Wait for the app to fully load (poll for stat cards)
        if wait_for_stats(chrome, base, profile):
            print("Stats rendered in #/app")
        else:
            print("Timeout waiting for stats - continuing anyway")

        # --- default view (last commit) ----------------------------------
        dom, errors = dump_dom(chrome, base + "/", profile)
        Path(tmpdir, "dom-default.html").write_text(dom, encoding="utf-8")
        time.sleep(15)
        print("\n--- rendered Machine Status ---")
        for element_id in ("status-pill", "status-model", "status-ollama",
                           "status-embeddings", "status-latency"):
            print("  " + element_id + " = " + repr(text_of(dom, element_id)))
        print("--- rendered repository / timeline ---")
        print("  repo-name          = " + repr(text_of(dom, "repo-name")))
        print("  date-display       = " + repr(text_of(dom, "date-display")))
        print("  slider min/max     = " + attr_of(dom, "date-slider", "min")
              + " / " + attr_of(dom, "date-slider", "max")
              + " value = " + attr_of(dom, "date-slider", "value"))
        print("--- rendered autopsy ---")
        print("  stat cards         = " + str(count_class(dom, "stat-label")))
        print("  recent commits     = " + str(len(re.findall(r'class="commit-hash"', dom))))

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

        # The séance progress line and the Cancel button exist and start hidden.
        print("--- séance controls ---")
        ss = text_of(dom, "seance-status")
        ss_clean = ss.encode('cp1252', errors='replace').decode('cp1252')
        hidden = bool(re.search(r'id="seance-status"[^>\b]*\bhidden\b', dom))
        print("  seance-status      = " + ss_clean + "  hidden = " + str(hidden))
        results.append(("séance cancel button exists and starts hidden",
                        bool(re.search(r'id="btn-seance-cancel"[^>\b]*\bhidden\b', dom))))
        results.append(("séance progress element exists and starts hidden",
                        bool(re.search(r'id="seance-status"[^>\b]*\bhidden\b', dom))))

        # --- deep link to an earlier boundary ----------------------------
        dom2, errors2 = dump_dom(chrome, base + "/?as_of=2024-01-31", profile)
        Path(tmpdir, "dom-early.html").write_text(dom2, encoding="utf-8")
        print("\n--- ?as_of=2024-01-31 ---")
        print("  date-display       = " + repr(text_of(dom2, "date-display")))
        print("  stat cards         = " + str(count_class(dom2, "stat-label")))
        print("  recent commits     = " + str(len(re.findall(r'class="commit-hash"', dom2))))
        print("  console errors     = " + str(len(errors2)))
        results.append(("no console errors on deep link", not errors2))
        results.append(("deep link date applied",
                        "2024-01-31" in text_of(dom2, "date-display") or
                        "31 Jan 2024" in text_of(dom2, "date-display")))
        early_commits = len(re.findall(r'class="commit-hash"', dom2))
        results.append(("earlier boundary shows fewer commits",
                        early_commits < len(re.findall(r'class="commit-hash"', dom))))

        print("\n" + "=" * 66)
        for name, ok in results:
            print("[{'PASS' if ok else 'FAIL'}] " + name)
        passed = sum(1 for _, ok in results if ok)
        print("RESULT: " + str(passed) + "/" + str(len(results)) + " UI checks passed")
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
        print("\nartifacts kept in " + tmpdir)


if __name__ == "__main__":
    raise SystemExit(main())