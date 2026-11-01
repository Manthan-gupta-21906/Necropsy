"""Regression tests for the "health blocks while the ghost is answering" bug.

The model is mocked with a 5 s sleep, so these tests need no Ollama and no
network. They prove three things:

  1. GET /health answers in well under a second while POST /seance is
     generating, and reports busy=true / busy_reason="answering".
  2. A probe that times out during a generation does NOT flip
     ollama_reachable to false: the last known good value is kept.
  3. Outside a generation a failed probe is still reported honestly.
"""

import threading
import time

from fastapi.testclient import TestClient

from necropsy.api import server as server_module
from necropsy.api.server import app

SLOW_SECONDS = 5.0


def fake_ollama(reachable=True, installed=True, error_kind=None):
    return {
        "reachable": reachable,
        "model": "qwen3:8b",
        "model_installed": installed,
        "models": ["qwen3:8b"] if installed else [],
        "error": None if reachable else "timed out",
        "error_kind": None if reachable else (error_kind or "connection"),
        "base_url": "http://127.0.0.1:11434",
    }


def slow_chat(parsed, seconds=SLOW_SECONDS):
    """A stand-in for the real model that takes a measurable time."""
    calls = []

    def fake_chat(model=None, messages=None, schema=None, timeout=None, retry=None):
        calls.append(time.monotonic())
        time.sleep(seconds)
        return {
            "success": True,
            "content": "{}",
            "parsed": parsed,
            "latency_ms": int(seconds * 1000),
            "model": model or "mock",
            "usage": {"prompt_tokens": 400, "completion_tokens": 60, "total_tokens": 460},
        }

    fake_chat.calls = calls
    return fake_chat


def patch_health(monkeypatch, reachable=True, error_kind=None):
    monkeypatch.setattr(
        server_module, "check_ollama",
        lambda *a, **k: fake_ollama(reachable=reachable, error_kind=error_kind),
    )
    monkeypatch.setattr(
        server_module, "get_embed_state",
        lambda: {"state": "ready", "model": "BAAI/bge-small-en-v1.5", "message": None},
    )
    monkeypatch.setattr(server_module, "get_warmup_state", lambda: {"state": "warm"})


def start_seance(client, indexed_repo, parsed):
    """Ask the ghost in a thread; return (thread, result holder)."""
    result = {}

    def ask():
        result["response"] = client.post("/seance", json={
            "repo_id": indexed_repo["repo_id"],
            "as_of": "2024-03-31",
            "question": "What did it add in February?",
        })

    thread = threading.Thread(target=ask, name="test-seance")
    thread.start()
    return thread, result


def feb_parsed(indexed_repo):
    feb = indexed_repo["by_subject"]["Feb feature add"]
    return {
        "remembers": "a feature",
        "answer": "It added a feature in February.",
        "claims": [{
            "claim": "feature added",
            "commit_hash": feb["hash"],
            "support": "Feb feature add",
        }],
    }


def test_health_answers_in_under_a_second_while_seance_generates(
    indexed_repo, monkeypatch
):
    patch_health(monkeypatch)
    monkeypatch.setattr(server_module, "chat", slow_chat(feb_parsed(indexed_repo)))
    server_module.refresh_health_once()

    client = TestClient(app)
    thread, result = start_seance(client, indexed_repo, feb_parsed(indexed_repo))

    # Wait until the request is definitely inside the (slow) generation.
    time.sleep(0.5)

    timings = []
    for _ in range(3):
        started = time.perf_counter()
        response = client.get("/health")
        timings.append(time.perf_counter() - started)
        assert response.status_code == 200
        body = response.json()
        assert body["busy"] is True, body
        assert body["busy_reason"] == "answering", body
        assert body["status"] == "busy", body
        assert body["ollama"]["reachable"] is True, body
        time.sleep(0.2)

    thread.join(timeout=60)
    assert not thread.is_alive(), "the séance request never finished"
    assert result["response"].status_code == 200, result["response"].text

    print(f"/health timings during generation: "
          f"{[round(t * 1000) for t in timings]} ms")
    assert max(timings) < 1.0, f"/health took {max(timings):.3f}s while busy"

    # Busy clears when the answer is done.
    after = client.get("/health").json()
    assert after["busy"] is False
    assert after["busy_reason"] is None


def test_timed_out_probe_while_busy_keeps_last_known_good(indexed_repo, monkeypatch):
    # Start from a known-good snapshot.
    patch_health(monkeypatch, reachable=True)
    monkeypatch.setattr(server_module, "chat", slow_chat(feb_parsed(indexed_repo), 2.0))
    server_module.refresh_health_once()
    assert server_module._health_snapshot()["ollama"]["reachable"] is True

    client = TestClient(app)
    thread, result = start_seance(client, indexed_repo, feb_parsed(indexed_repo))
    time.sleep(0.4)
    assert server_module._busy_state()["busy"] is True

    # Now the probe times out, exactly as Ollama's /api/tags does while a CPU
    # generation is hogging the model.
    patch_health(monkeypatch, reachable=False, error_kind="timeout")
    server_module.refresh_health_once()

    body = client.get("/health").json()
    assert body["busy"] is True
    assert body["ollama"]["reachable"] is True, "a busy probe must not fake an outage"
    assert body["status"] == "busy"
    assert not body["ollama"]["error"]

    thread.join(timeout=60)
    assert result["response"].status_code == 200

    # Once the generation ends, a failing probe is reported honestly again.
    server_module.refresh_health_once()
    after = client.get("/health").json()
    assert after["busy"] is False
    assert after["ollama"]["reachable"] is False
    assert after["status"] == "error"


def test_health_stays_fast_with_no_ollama_at_all(monkeypatch):
    """Even a dead Ollama cannot slow /health down: it is all cached."""
    def slow_probe(*args, **kwargs):  # noqa: ARG001
        time.sleep(2.0)
        return fake_ollama(reachable=False)

    monkeypatch.setattr(server_module, "check_ollama", slow_probe)
    client = TestClient(app)

    started = time.perf_counter()
    response = client.get("/health")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.5, f"/health took {elapsed:.3f}s with a slow probe"
