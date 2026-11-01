# necropsy/ghost/llm.py — Ollama client with schema-constrained output
# -------------------------------------------------------------------
# Responsibilities:
#   • Report the REAL Ollama state (reachable, model, model_installed)
#   • Chat with Ollama using thinking disabled and a JSON schema
#   • One retry on transient failure
#   • Clear error messages: model missing, Ollama down, timeout, bad JSON
#   • Never fake inference; always report real status
#
# Cold-loading qwen3:8b on a laptop CPU can take ~85 s, so the generation
# timeout comes from config (180 s by default) and `keep_alive` keeps the
# model resident between requests.

import json
import logging
import threading
import time
from typing import Optional, Dict, Any, List

import requests

from ..config import (
    LLM_MODEL,
    LLM_TIMEOUT,
    LLM_RETRY,
    LLM_THINKING_DISABLED,
    LLM_KEEP_ALIVE,
    LLM_NUM_PREDICT,
    LLM_NUM_CTX,
    OLLAMA_BASE_URL,
)

logger = logging.getLogger(__name__)

# Short timeout for status probes so /health never blocks the UI.
PROBE_TIMEOUT = 2.0


class OllamaError(RuntimeError):
    """Base Ollama error with a helpful user message."""

    def __init__(self, message: str, code: str = "ollama_error"):
        super().__init__(message)
        self.code = code


class ModelMissingError(OllamaError):
    """The requested model is not available on Ollama."""

    def __init__(self, message: str):
        super().__init__(message, code="model_missing")


class OllamaDownError(OllamaError):
    """Ollama server is not reachable."""

    def __init__(self, message: str):
        super().__init__(message, code="ollama_down")


class OllamaTimeoutError(OllamaError):
    """The model did not answer within the configured timeout."""

    def __init__(self, message: str):
        super().__init__(message, code="timeout")


class JSONVerifyError(OllamaError):
    """Model returned invalid JSON after the retry."""

    def __init__(self, message: str):
        super().__init__(message, code="invalid_json")


# --- Warm-up state (read by /health) ---------------------------------------

_WARMUP_LOCK = threading.Lock()
_WARMUP = {
    "state": "not_started",   # not_started | warming | warm | error
    "message": None,
}
_WARMUP_THREAD: Optional[threading.Thread] = None


def get_warmup_state() -> dict:
    """Return the real LLM warm-up state."""
    with _WARMUP_LOCK:
        return dict(_WARMUP)


def _set_warmup(state: str, message: Optional[str] = None) -> None:
    with _WARMUP_LOCK:
        _WARMUP["state"] = state
        _WARMUP["message"] = message


# --- Status probes ---------------------------------------------------------

def check_ollama(model_name: Optional[str] = None) -> Dict[str, Any]:
    """Probe Ollama with a short timeout and return the real state.

    Returns:
        {reachable: bool, model: str, model_installed: bool, error: str|None,
         models: [names], base_url: str}
    """
    model_name = model_name or LLM_MODEL
    result: Dict[str, Any] = {
        "reachable": False,
        "model": model_name,
        "model_installed": False,
        "models": [],
        "error": None,
        "error_kind": None,
        "base_url": OLLAMA_BASE_URL,
    }
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=PROBE_TIMEOUT)
    except requests.exceptions.Timeout:
        result["error"] = (
            f"Ollama at {OLLAMA_BASE_URL} did not answer within "
            f"{PROBE_TIMEOUT:.0f}s (it may be busy loading a model)"
        )
        result["error_kind"] = "timeout"
        return result
    except requests.exceptions.RequestException as exc:
        result["error"] = (
            f"Ollama not reachable at {OLLAMA_BASE_URL}: {type(exc).__name__}"
        )
        result["error_kind"] = "connection"
        return result

    if resp.status_code != 200:
        result["error"] = f"Ollama /api/tags returned HTTP {resp.status_code}"
        result["error_kind"] = "http"
        return result

    try:
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except ValueError:
        result["error"] = "Ollama /api/tags returned invalid JSON"
        result["error_kind"] = "invalid_json"
        return result

    result["reachable"] = True
    result["models"] = names
    # Exact tag match first, then a tolerant match ("qwen3" -> "qwen3:8b").
    result["model_installed"] = any(
        n == model_name or n.split(":")[0] == model_name for n in names
    )
    return result


def model_available(model_name: Optional[str] = None) -> bool:
    """True when Ollama is reachable and the model tag is installed."""
    return bool(check_ollama(model_name)["model_installed"])


def ensure_model_loaded(model_name: Optional[str] = None) -> None:
    """Raise a friendly error if Ollama is down or the model is missing."""
    model_name = model_name or LLM_MODEL
    status = check_ollama(model_name)
    if not status["reachable"]:
        raise OllamaDownError(
            f"Ollama is not reachable at {OLLAMA_BASE_URL}. "
            "Start it with `ollama serve` and try again."
        )
    if not status["model_installed"]:
        raise ModelMissingError(
            f"Model '{model_name}' is not installed on Ollama. "
            f"Install it with: ollama pull {model_name}"
        )


def warmup_async(model_name: Optional[str] = None) -> None:
    """Send one tiny request in a background thread to load the model.

    qwen3:8b takes a long time to cold-load; doing it at startup means the
    first real séance does not pay that cost. Idempotent.
    """
    global _WARMUP_THREAD

    with _WARMUP_LOCK:
        if _WARMUP_THREAD is not None and _WARMUP_THREAD.is_alive():
            return

    model_name = model_name or LLM_MODEL

    def _run() -> None:
        status = check_ollama(model_name)
        if not status["reachable"]:
            _set_warmup("error", "Ollama is not reachable. Run `ollama serve`.")
            return
        if not status["model_installed"]:
            _set_warmup(
                "error",
                f"Model '{model_name}' is not installed. Run: ollama pull {model_name}",
            )
            return

        _set_warmup("warming", f"Loading {model_name} into memory")
        try:
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "stream": False,
                    "think": False,
                    "keep_alive": LLM_KEEP_ALIVE,
                    # Must match chat() exactly: Ollama reloads the model when
                    # num_ctx changes, and a reload costs ~70 s on this CPU.
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 4,
                        "num_ctx": LLM_NUM_CTX,
                    },
                },
                timeout=LLM_TIMEOUT,
            )
            if resp.status_code != 200:
                _set_warmup("error", f"Ollama warm-up returned HTTP {resp.status_code}")
                return
            _set_warmup("warm", f"{model_name} loaded")
        except requests.exceptions.Timeout:
            _set_warmup(
                "error",
                f"Warm-up timed out after {LLM_TIMEOUT}s. "
                "The model may still load on the first request.",
            )
        except requests.exceptions.RequestException as exc:
            _set_warmup("error", f"Warm-up failed: {type(exc).__name__}: {exc}")

    _WARMUP_THREAD = threading.Thread(target=_run, name="llm-warmup", daemon=True)
    _WARMUP_THREAD.start()


# --- Chat -----------------------------------------------------------------

def chat(
    model: str = LLM_MODEL,
    messages: Optional[List[Dict[str, Any]]] = None,
    schema: Optional[Dict[str, Any]] = None,
    timeout: int = LLM_TIMEOUT,
    retry: bool = LLM_RETRY,
) -> Dict[str, Any]:
    """Send a chat request to Ollama and return parsed JSON.

    Raises:
        OllamaDownError, ModelMissingError, OllamaTimeoutError, JSONVerifyError
    """
    ensure_model_loaded(model)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": list(messages or []),
        "stream": False,
        # qwen3 supports disabling its thinking trace; required for JSON.
        "think": not LLM_THINKING_DISABLED,
        "keep_alive": LLM_KEEP_ALIVE,
        "options": {
            "temperature": 0.0,
            # Bound the answer: the séance reply is a small JSON object.
            "num_predict": LLM_NUM_PREDICT,
            # Keep the KV cache small so CPU prefill stays quick.
            "num_ctx": LLM_NUM_CTX,
        },
    }
    if schema:
        # Ollama accepts a JSON schema here and constrains generation to it.
        payload["format"] = schema

    attempts = 2 if retry else 1
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        start = time.time()
        try:
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            last_error = OllamaTimeoutError(
                f"The model did not answer within {timeout}s. "
                f"Cold-loading {model} can take a minute or two; try again."
            )
            logger.warning("Ollama timeout on attempt %s", attempt + 1)
            continue
        except requests.exceptions.RequestException as exc:
            last_error = OllamaDownError(
                f"Ollama is not reachable at {OLLAMA_BASE_URL}: {type(exc).__name__}. "
                "Start it with `ollama serve`."
            )
            logger.warning("Ollama request failed on attempt %s: %s", attempt + 1, exc)
            continue

        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code == 404:
            last_error = ModelMissingError(
                f"Model '{model}' is not installed on Ollama. "
                f"Install it with: ollama pull {model}"
            )
            break
        if resp.status_code != 200:
            last_error = OllamaDownError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
            continue

        try:
            body = resp.json()
        except ValueError:
            last_error = JSONVerifyError("Ollama returned a non-JSON response body.")
            continue

        content = (body.get("message") or {}).get("content", "")
        usage = {
            "prompt_tokens": body.get("prompt_eval_count"),
            "completion_tokens": body.get("eval_count"),
            "total_tokens": (
                (body.get("prompt_eval_count") or 0) + (body.get("eval_count") or 0)
            ),
            "total_duration_ms": (
                int(body["total_duration"] / 1_000_000)
                if body.get("total_duration") else None
            ),
        }
        logger.info(
            "Ollama %s: prompt_tokens=%s completion_tokens=%s total=%.1fs "
            "num_predict=%s num_ctx=%s",
            model, usage["prompt_tokens"], usage["completion_tokens"],
            latency_ms / 1000.0, LLM_NUM_PREDICT, LLM_NUM_CTX,
        )
        parsed = _extract_json_from_text(content)
        if isinstance(parsed, dict):
            return {
                "success": True,
                "content": content,
                "parsed": parsed,
                "latency_ms": latency_ms,
                "model": model,
                "usage": usage,
            }

        last_error = JSONVerifyError(
            "The model did not return valid JSON. "
            f"First 200 chars: {content[:200]!r}"
        )
        logger.warning("Invalid JSON from model on attempt %s", attempt + 1)

    raise last_error or OllamaError("Ollama request failed for an unknown reason.")


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from text that may include prose."""
    if not text:
        return None
    text = text.strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
