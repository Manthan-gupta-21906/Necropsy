"""Step 1 diagnosis: reproduce /repos failure, dump /health JSON, check Ollama.

Read-only: does not start the server's lifespan (no DB init) and does not
write to the database.
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from fastapi.testclient import TestClient

from necropsy.api.server import app
from necropsy.config import LLM_MODEL, EMBED_MODEL

print("=" * 70)
print("1) GET /repos  (full traceback)")
print("=" * 70)
client = TestClient(app)
try:
    r = client.get("/repos")
    print("HTTP", r.status_code)
    print(r.text[:500])
except Exception:
    traceback.print_exc()

print()
print("=" * 70)
print("2) GET /health (exact JSON)")
print("=" * 70)
try:
    r = client.get("/health")
    print("HTTP", r.status_code)
    print(json.dumps(r.json(), indent=2))
except Exception:
    traceback.print_exc()

print()
print("=" * 70)
print("3) Ollama direct: GET http://127.0.0.1:11434/api/tags")
print("=" * 70)
print("configured LLM_MODEL =", LLM_MODEL)
print("configured EMBED_MODEL =", EMBED_MODEL)
try:
    resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
    print("HTTP", resp.status_code)
    tags = resp.json()
    names = [m.get("name") for m in tags.get("models", [])]
    print("models returned:", json.dumps(names, indent=2))
    print("LLM_MODEL installed (exact tag match):", LLM_MODEL in names)
    print("any name containing LLM_MODEL:", any(LLM_MODEL in (n or "") for n in names))
except Exception:
    traceback.print_exc()

print()
print("=" * 70)
print("4) POST /autopsy and POST /seance smoke (signature check only)")
print("=" * 70)
try:
    r = client.post("/autopsy?repo_id=1&as_of=2024-02-01")
    print("POST /autopsy?repo_id=1&as_of=... -> HTTP", r.status_code)
    print(r.text[:300])
except Exception:
    traceback.print_exc()

try:
    r = client.post(
        "/seance",
        json={"repo_id": 1, "as_of": "2024-02-01", "question": "hello"},
    )
    print("POST /seance (json body) -> HTTP", r.status_code)
    print(r.text[:300])
except Exception:
    traceback.print_exc()
