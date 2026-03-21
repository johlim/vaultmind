# =============================================================
# vaultmind — ai_backend.py
# =============================================================
# Shared AI caller used by all scripts.
# All Ollama communication goes through this file.
# If you want to add a new model provider, do it here.
# =============================================================

import os
import sys
import requests

# add the script directory to the path so config.py is always found
sys.path.insert(0, os.path.dirname(__file__))
from config import OLLAMA_MODEL, OLLAMA_API_URL, TEMPERATURE, TIMEOUT, KEEP_ALIVE, NUM_CTX


def get_backend() -> str:
    """
    Read the --api flag from command line arguments.
    Currently only Ollama is supported, so this always returns 'ollama'.
    Kept for future extensibility (e.g. --api gemini, --api openai).
    """
    return "ollama"


def call_ai(prompt: str, backend: str = "ollama", timeout: int = None) -> str:
    """
    Send a prompt to the configured AI backend and return the response text.

    Args:
        prompt:  The full prompt string to send to the model.
        backend: Which backend to use. Currently only 'ollama' is supported.
        timeout: Override the default timeout from config.py (in seconds).

    Returns:
        The model's response as a stripped string.

    Raises:
        requests.exceptions.HTTPError: If the Ollama API returns an error.
        ValueError: If the response format is unexpected.
    """
    return _call_ollama(prompt, timeout or TIMEOUT)


def _call_ollama(prompt: str, timeout: int) -> str:
    """
    Internal function that makes the actual HTTP request to the Ollama API.

    Uses settings from config.py:
        - OLLAMA_MODEL:   which model to run
        - OLLAMA_API_URL: where Ollama is listening
        - TEMPERATURE:    controls randomness (low = factual)
        - KEEP_ALIVE:     how long to keep the model loaded
        - NUM_CTX:        context window size in tokens

    Args:
        prompt:  The prompt to send.
        timeout: Max seconds to wait for a response.

    Returns:
        The model's response text, stripped of leading/trailing whitespace.
    """
    response = requests.post(
        OLLAMA_API_URL,
        json={
            "model":      OLLAMA_MODEL,
            "prompt":     prompt,
            "stream":     False,       # get the full response at once, not streamed
            "keep_alive": KEEP_ALIVE,  # keep model in memory between calls
            "options": {
                "temperature":    TEMPERATURE,  # lower = more factual
                "top_p":          0.9,          # nucleus sampling threshold
                "repeat_penalty": 1.1,          # penalize repeated phrases
                "num_ctx":        NUM_CTX,      # context window in tokens
            }
        },
        timeout=timeout,
    )

    # raise an exception for HTTP errors (4xx, 5xx)
    response.raise_for_status()

    data = response.json()

    # sanity check — Ollama always returns a 'response' field for /api/generate
    if "response" not in data:
        raise ValueError(f"Unexpected Ollama response format: {data}")

    return data["response"].strip()


def backend_label(backend: str = "ollama") -> str:
    """
    Return a human-readable label for the current backend.
    Used in terminal output so users know which model is running.
    """
    return f"Ollama ({OLLAMA_MODEL})"
