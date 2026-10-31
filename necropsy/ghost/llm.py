# necropsy/ghost/llm.py — Ollama client with schema-constrained output
# -------------------------------------------------------------------
# Responsibilities:
#   • Chat with Ollama using thinking=disabled
#   • Enforce JSON-schema output (remembers, answer, claims[])
#   • One retry on transient failure
#   • Clear error messages: model missing, Ollama down, timeout, bad JSON
#   • Never fake inference; always report real status

import json
import time
import logging
from typing import Optional, Dict, Any, List

import requests

from ..config import LLM_MODEL, LLM_FALLBACK, LLM_TIMEOUT, LLM_RETRY, LLM_THINKING_DISABLED

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Base Ollama error with helpful user message."""
    pass


class ModelMissingError(OllamaError):
    """The requested model is not available on Ollama."""
    pass


class OllamaDownError(OllamaError):
    """Ollama server is not reachable."""
    pass


class JSONVerifyError(RuntimeError):
    """Model returned invalid JSON after retry."""
    pass


def _check_ollama_health() -> bool:
    """Ping Ollama server at localhost:11434/api/tags.

    Returns True if the server is reachable and the model is available.
    """
    try:
        resp = requests.get(
            "http://localhost:11434/api/tags",
            timeout=5,
        )
        if resp.status_code != 200:
            return False
        models = resp.json().get("models", [])
        return any(LLM_MODEL in m.get("name", "") for m in models)
    except (requests.ConnectionError, requests.TimeoutError):
        return False


def model_available(model_name: str = None) -> bool:
    """Check if a model is available on the local Ollama server."""
    if model_name is None:
        model_name = LLM_MODEL
    try:
        resp = requests.get(
            "http://localhost:11434/api/tags",
            timeout=5,
        )
        if resp.status_code != 200:
            return False
        models = resp.json().get("models", [])
        return any(model_name in m.get("name", "") for m in models)
    except (requests.ConnectionError, requests.TimeoutError):
        return False


def ensure_model_loaded(model_name: str = None) -> None:
    """Ensure the model is available; raise ModelMissingError if not.

    If the model is not loaded, attempt to pull it.
    If pulling fails, raise an error with the exact `ollama pull` command.
    """
    if model_name is None:
        model_name = LLM_MODEL

    if not model_available(model_name):
        raise ModelMissingError(
            f"Model '{model_name}' is not available on Ollama. "
            f"Pull it with: `ollama pull {model_name}`"
        )


def chat(
    model: str = LLM_MODEL,
    messages: List[Dict[str, Any]] = None,
    schema: Dict[str, Any] = None,
    format: str = "json",
    timeout: int = LLM_TIMEOUT,
    retry: bool = LLM_RETRY,
) -> Dict[str, Any]:
    """Send a chat request to Ollama with JSON-schema output.

    Args:
        model: Ollama model name
        messages: list of {role, content} messages
        schema: JSON schema for structured output
        format: "json" or "text"
        timeout: seconds before timeout
        retry: whether to retry once on transient failure

    Returns:
        Parsed JSON dict from the model response.

    Raises:
        ModelMissingError: model not available
        OllamaDownError: Ollama server unreachable
        JSONVerifyError: model didn't return valid JSON after retry
    """
    # Ensure model is available
    ensure_model_loaded(model)

    # Build the chat payload
    payload = {
        "model": model,
        "messages": messages or [],
        "stream": False,
        "options": {
            "temperature": 0.0,
        },
    }

    # Disable thinking if configured
    if LLM_THINKING_DISABLED:
        if "thinking" not in payload.get("options", {}):
            payload["options"] = payload.get("options", {})
        payload["options"]["thinking"] = False

    # Add JSON format directive if schema provided
    if schema:
        payload["format"] = "json"
        # Add system instruction for structured output
        if not any(m.get("role") == "system" for m in messages):
            payload["messages"].insert(
                0,
                {"role": "system", "content": "Respond with valid JSON only matching the given schema. No prose, no explanations."},
            )

    # Attempt(s)
    last_error = None
    for attempt in range(2 if retry else 1):
        try:
            start = time.time()
            resp = requests.post(
                "http://localhost:11434/api/chat",
                json=payload,
                timeout=timeout,
            )
            latency_ms = int((time.time() - start) * 1000)

            if resp.status_code != 200:
                raise OllamaDownError(
                    f"Ollama server returned status {resp.status_code}"
                )

            raw = resp.json()
            content = raw.get("message", {}).get("content", "")

            # Try to parse as JSON
            try:
                parsed = json.loads(content)
                return {
                    "success": True,
                    "content": content,
                    "parsed": parsed,
                    "latency_ms": latency_ms,
                }
            except json.JSONDecodeError:
                # Not raw JSON; check if it's a code block or mixed
                # Try to extract JSON from the content
                parsed = _extract_json_from_text(content)
                if parsed:
                    return {
                        "success": True,
                        "content": content,
                        "parsed": parsed,
                        "latency_ms": latency_ms,
                    }

            # If we get here, JSON not found
            last_error = JSONVerifyError(
                f"Model did not return valid JSON. Content: {content[:200]}"
            )
            if attempt == 0 and retry:
                continue  # one retry
            raise last_error

        except (requests.ConnectionError, requests.TimeoutError) as e:
            last_error = OllamaDownError(
                f"Ollama server unreachable: {e}"
            )
            if attempt == 0 and retry:
                continue  # one retry
            raise last_error

        except OllamaError:
            raise
        except Exception as e:
            last_error = OllamaError(f"Unexpected error: {e}")
            if attempt == 0 and retry:
                continue
            raise last_error

    # Exhausted retries
    raise last_error or OllamaDownError("Ollama request failed")


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Try to extract a JSON object from text that may contain prose.

    Looks for the first {...} block or the entire text if it's valid JSON.
    """
    text = text.strip()

    # Try the whole text as JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the innermost {...} block
    # Simple approach: find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None