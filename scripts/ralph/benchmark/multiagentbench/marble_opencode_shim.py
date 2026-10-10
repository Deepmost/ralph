"""MARBLE × opencode-go shim.

MARBLE delegates every LLM call to litellm. opencode-go exposes an
OpenAI-compatible endpoint at https://opencode.ai/zen/go/v1 but requires an
``x-opencode-session`` header for routing. This shim:

  1. reads the opencode-go key from pi's auth.json (never hardcoded / printed),
  2. points litellm at the endpoint via OPENAI_API_BASE / OPENAI_API_KEY,
  3. wraps litellm.completion / litellm.acompletion to inject the session header.

Import this BEFORE marble runs (see run_native.sh). Set MARBLE_OPENCODE_SHIM=0
to disable, OPENCODE_SESSION to override the session id.
"""
from __future__ import annotations

import functools
import json
import os

DEFAULT_BASE = "https://opencode.ai/zen/go/v1"
AUTH_PATH = os.path.expanduser("~/.pi/agent/auth.json")


def _read_key() -> str | None:
    try:
        with open(AUTH_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get("opencode-go") if isinstance(data, dict) else None
    if isinstance(entry, dict):
        return entry.get("key")
    if isinstance(entry, str):
        return entry
    return None


def _apply() -> None:
    if os.environ.get("MARBLE_OPENCODE_SHIM") == "0":
        return

    base = os.environ.get("OPENBENCH_OPENCODE_BASE") or DEFAULT_BASE
    os.environ.setdefault("OPENAI_API_BASE", base)
    os.environ.setdefault("OPENAI_BASE_URL", base)
    if not os.environ.get("OPENAI_API_KEY"):
        key = _read_key()
        if key:
            os.environ["OPENAI_API_KEY"] = key

    try:
        import litellm
    except Exception:  # litellm not installed yet / irrelevant interpreter
        return

    session_id = os.environ.get("OPENCODE_SESSION", "marble")

    def _wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            headers = dict(kwargs.get("extra_headers") or {})
            headers.setdefault("x-opencode-session", session_id)
            kwargs["extra_headers"] = headers
            return fn(*args, **kwargs)

        inner._marble_wrapped = True  # type: ignore[attr-defined]
        return inner

    completion = getattr(litellm, "completion", None)
    if completion is not None and not getattr(completion, "_marble_wrapped", False):
        litellm.completion = _wrap(completion)

    acompletion = getattr(litellm, "acompletion", None)
    if acompletion is not None and not getattr(acompletion, "_marble_wrapped", False):
        litellm.acompletion = _wrap(acompletion)


_apply()
