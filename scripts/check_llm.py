"""Item 3 check: real Ollama call with schema, think=false, keep_alive.

Prints the exact payload options, wall-clock latency and parsed JSON.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from necropsy import config
from necropsy.ghost.llm import check_ollama, chat, get_warmup_state, warmup_async

print("configured LLM_MODEL      :", config.LLM_MODEL)
print("configured LLM_TIMEOUT    :", config.LLM_TIMEOUT, "seconds")
print("configured LLM_KEEP_ALIVE :", config.LLM_KEEP_ALIVE)
print("thinking disabled         :", config.LLM_THINKING_DISABLED)
print("ollama status             :", json.dumps(check_ollama(config.LLM_MODEL), indent=2))

print("\nstarting warm-up thread and waiting for it...")
warmup_async(config.LLM_MODEL)
t0 = time.time()
while time.time() - t0 < config.LLM_TIMEOUT:
    state = get_warmup_state()
    if state["state"] in ("warm", "error"):
        print(f"warm-up finished in {time.time() - t0:.1f}s -> {state}")
        break
    time.sleep(1)

schema = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "ok": {"type": "boolean"}},
    "required": ["answer", "ok"],
}

print("\nsending schema-constrained chat request...")
start = time.time()
result = chat(
    model=config.LLM_MODEL,
    messages=[{"role": "user", "content": "Say hello in five words or fewer."}],
    schema=schema,
)
elapsed = time.time() - start

print(f"wall clock: {elapsed:.2f}s")
print(f"reported latency_ms: {result['latency_ms']}")
print("tokens:", json.dumps(result.get("usage"), indent=2))
print("parsed JSON:", json.dumps(result["parsed"], indent=2))
