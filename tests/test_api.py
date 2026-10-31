"""API tests: real FastAPI app against a temporary database.

No network and no Ollama are required: the LLM and the Ollama probe are
monkeypatched. Ingestion, embeddings and date filtering run for real.
"""

import time

import pytest
from fastapi.testclient import TestClient

from necropsy.api import server as server_module
from necropsy.api.server import app

REFUSAL = "The dead do not remember that."


@pytest.fixture()
def client():
    """A TestClient that does not run startup warm-ups (keeps tests fast)."""
    return TestClient(app)


def fake_ollama(reachable=True, installed=True, model="qwen3:8b"):
    return {
        "reachable": reachable,
        "model": model,
        "model_installed": installed,
        "models": [model] if installed else [],
        "error": None if reachable else "connection refused",
        "error_kind": None if reachable else "connection",
        "base_url": "http://127.0.0.1:11434",
    }


# --- /health ---------------------------------------------------------------

def test_health_reports_real_state(client, monkeypatch):
    monkeypatch.setattr(server_module, "check_ollama", lambda *a, **k: fake_ollama())
    monkeypatch.setattr(
        server_module, "get_embed_state",
        lambda: {"state": "ready", "model": "BAAI/bge-small-en-v1.5", "message": None},
    )
    monkeypatch.setattr(server_module, "get_warmup_state", lambda: {"state": "warm"})

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["ollama"]["reachable"] is True
    assert body["ollama"]["model_installed"] is True
    assert body["ollama"]["model"] == "qwen3:8b"
    assert body["embeddings"]["state"] == "ready"


def test_health_is_error_when_ollama_is_down(client, monkeypatch):
    monkeypatch.setattr(server_module, "check_ollama",
                        lambda *a, **k: fake_ollama(reachable=False, installed=False))
    monkeypatch.setattr(
        server_module, "get_embed_state",
        lambda: {"state": "ready", "model": "m", "message": None},
    )
    monkeypatch.setattr(server_module, "get_warmup_state", lambda: {"state": "error"})

    body = client.get("/health").json()
    assert body["status"] == "error"
    assert body["ollama"]["reachable"] is False


def test_health_is_warming_while_embeddings_load(client, monkeypatch):
    monkeypatch.setattr(server_module, "check_ollama", lambda *a, **k: fake_ollama())
    monkeypatch.setattr(
        server_module, "get_embed_state",
        lambda: {"state": "loading", "model": "m", "message": None},
    )
    monkeypatch.setattr(server_module, "get_warmup_state", lambda: {"state": "warming"})

    body = client.get("/health").json()
    assert body["status"] == "warming"


# --- /repos ----------------------------------------------------------------

def test_repos_is_empty_on_a_fresh_database(client, fresh_db):  # noqa: ARG001
    response = client.get("/repos")
    assert response.status_code == 200
    assert response.json() == {"repos": []}


def test_unknown_repo_is_404(client, fresh_db):  # noqa: ARG001
    assert client.get("/repos/999/range").status_code == 404
    assert client.get("/autopsy?repo_id=999&as_of=2024-01-01").status_code == 404


# --- /exhume ---------------------------------------------------------------

def test_exhume_indexes_in_the_background(client, fresh_db, fixture_repo):  # noqa: ARG001
    response = client.post("/exhume", json={"source": str(fixture_repo)})
    assert response.status_code == 200
    job = response.json()
    assert job["job_id"]
    assert job["stage"] == "queued"

    deadline = time.time() + 180
    final = {}
    while time.time() < deadline:
        final = client.get(f"/exhume/{job['job_id']}").json()
        if final["status"] in ("ready", "failed"):
            break
        time.sleep(0.5)

    assert final["status"] == "ready", final.get("error")
    assert final["commit_count"] == 3
    assert final["embeddings"] == 3
    assert final["first_commit_at"] == "2024-01-15T10:00:00Z"
    assert final["last_commit_at"] == "2024-03-20T10:00:00Z"

    repos = client.get("/repos").json()["repos"]
    assert len(repos) == 1
    assert repos[0]["commit_count"] == 3
    assert repos[0]["status"] == "ready"

    # Re-indexing the same source updates the row instead of duplicating it.
    again = client.post("/exhume", json={"source": str(fixture_repo)}).json()
    deadline = time.time() + 180
    while time.time() < deadline:
        state = client.get(f"/exhume/{again['job_id']}").json()
        if state["status"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert state["status"] == "ready", state.get("error")
    assert len(client.get("/repos").json()["repos"]) == 1


def test_exhume_rejects_invalid_source(client, fresh_db):  # noqa: ARG001
    assert client.post("/exhume", json={"source": ""}).status_code == 400
    assert client.post("/exhume", json={"source": "--upload-pack=evil"}).status_code == 400
    assert client.post("/exhume", json={"source": "ftp://example.com/x"}).status_code == 400


def test_exhume_unknown_job_is_404(client, fresh_db):  # noqa: ARG001
    assert client.get("/exhume/does-not-exist").status_code == 404


# --- /autopsy --------------------------------------------------------------

def test_repo_range_has_real_bounds(client, indexed_repo):
    body = client.get(f"/repos/{indexed_repo['repo_id']}/range").json()
    assert body["first_commit_at"] == "2024-01-15T10:00:00Z"
    assert body["last_commit_at"] == "2024-03-20T10:00:00Z"
    assert body["commit_count"] == 3


@pytest.mark.parametrize("as_of,expected", [
    ("2024-01-01", 0),
    ("2024-01-31", 1),
    ("2024-02-15", 2),
    ("2024-03-31", 3),
])
def test_autopsy_respects_as_of(client, indexed_repo, as_of, expected):
    body = client.get(
        f"/autopsy?repo_id={indexed_repo['repo_id']}&as_of={as_of}"
    ).json()
    assert body["commit_count"] == expected
    cutoff = f"{as_of}T23:59:59Z"
    for commit in body["recent_commits"]:
        assert commit["at"] <= cutoff
    assert (body["last_commit_at"] or "") <= cutoff


def test_autopsy_tombstone_appears_only_after_death(client, indexed_repo):
    early = client.get(
        f"/autopsy?repo_id={indexed_repo['repo_id']}&as_of=2024-02-01"
    ).json()
    late = client.get(
        f"/autopsy?repo_id={indexed_repo['repo_id']}&as_of=2024-03-31"
    ).json()
    assert early["tombstone_count"] == 0
    assert late["tombstone_count"] == 1
    assert late["tombstones"][0]["path"] == "lexer.py"


# --- /seance ---------------------------------------------------------------

def _mock_chat(parsed):
    calls = []

    def fake_chat(model=None, messages=None, schema=None, timeout=None, retry=None):
        calls.append({"model": model, "messages": messages, "schema": schema})
        return {
            "success": True,
            "content": "{}",
            "parsed": parsed,
            "latency_ms": 1234,
            "model": model or "mock",
        }

    fake_chat.calls = calls
    return fake_chat


def test_seance_keeps_a_verified_claim(client, indexed_repo, monkeypatch):
    feb = indexed_repo["by_subject"]["Feb feature add"]
    parsed = {
        "remembers": "a feature",
        "answer": "It added a feature in February.",
        "claims": [{
            "claim": "feature added",
            "commit_hash": feb["hash"],
            "support": "Feb feature add",
        }],
    }
    mock = _mock_chat(parsed)
    monkeypatch.setattr(server_module, "chat", mock)

    response = client.post("/seance", json={
        "repo_id": indexed_repo["repo_id"],
        "as_of": "2024-03-31",
        "question": "What did it add in February?",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] == 1
    assert body["claims"][0]["commit_hash"] == feb["hash"]
    assert body["claims"][0]["subject"] == "Feb feature add"
    assert body["answer"] == "It added a feature in February."
    assert body["latency_ms"] >= 0
    assert mock.calls, "the mocked LLM must actually be called"
    assert mock.calls[0]["schema"] is not None


def test_seance_refuses_when_no_claim_survives(client, indexed_repo, monkeypatch):
    parsed = {
        "remembers": "",
        "answer": "Something unverifiable.",
        "claims": [{
            "claim": "invented history",
            "commit_hash": "0" * 40,
            "support": "made up",
        }],
    }
    monkeypatch.setattr(server_module, "chat", _mock_chat(parsed))

    body = client.post("/seance", json={
        "repo_id": indexed_repo["repo_id"],
        "as_of": "2024-03-31",
        "question": "Tell me something false.",
    }).json()

    assert body["answer"] == REFUSAL
    assert body["claims"] == []


def test_seance_refuses_without_any_evidence(client, indexed_repo, monkeypatch):
    mock = _mock_chat({"answer": "x", "remembers": "", "claims": []})
    monkeypatch.setattr(server_module, "chat", mock)

    body = client.post("/seance", json={
        "repo_id": indexed_repo["repo_id"],
        "as_of": "2023-01-01",
        "question": "What is this project?",
    }).json()

    assert body["answer"] == REFUSAL
    assert body["claims"] == []
    assert body["evidence"] == []
    assert mock.calls == [], "the model must not be called with no evidence"


def test_seance_friendly_error_when_ollama_is_down(client, indexed_repo, monkeypatch):
    from necropsy.ghost.llm import OllamaDownError

    def boom(**kwargs):
        raise OllamaDownError("Ollama is not reachable. Start it with `ollama serve`.")

    monkeypatch.setattr(server_module, "chat", boom)

    response = client.post("/seance", json={
        "repo_id": indexed_repo["repo_id"],
        "as_of": "2024-03-31",
        "question": "What happened?",
    })
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "ollama_down"
    assert "ollama serve" in detail["message"]
    assert "Traceback" not in response.text


def test_seance_rejects_bad_bodies(client, indexed_repo):
    assert client.post("/seance", json={"question": "x"}).status_code == 400
    assert client.post("/seance", json={
        "repo_id": "not-an-int", "as_of": "2024-01-01", "question": "x"
    }).status_code == 400
    assert client.post("/seance", json={
        "repo_id": indexed_repo["repo_id"], "as_of": "2024-01-01", "question": ""
    }).status_code == 400


# --- date leak through the API --------------------------------------------

def test_api_never_displays_a_commit_after_as_of(client, indexed_repo):
    """The strongest end-to-end check: no post-as_of hash anywhere in /autopsy."""
    mar_hash = indexed_repo["by_subject"]["Mar cleanup pass"]["hash"]
    feb_hash = indexed_repo["by_subject"]["Feb feature add"]["hash"]

    body = client.get(
        f"/autopsy?repo_id={indexed_repo['repo_id']}&as_of=2024-02-01"
    ).json()
    serialized = str(body)

    assert mar_hash not in serialized
    assert feb_hash not in serialized
    assert "2024-03-20" not in serialized
