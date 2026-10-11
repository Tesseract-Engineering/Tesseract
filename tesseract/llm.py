"""
llm.py - one place that knows how to call a language model.

Same discipline as store.py: exactly one module knows which vendor is behind
the call, so swapping providers is an edit here rather than everywhere.

Provider is chosen from whichever key is present (LLM_PROVIDER pins one):

    ANTHROPIC_API_KEY   -> Anthropic Claude   (anthropic), model ANTHROPIC_MODEL
    GEMINI_API_KEY      -> Google Gemini      (google-genai), model discovered
    neither             -> disabled, and every caller degrades to keyword search

Gemini's model name is discovered rather than hardcoded, since a retired name
fails at runtime with an unhelpful 404. Set GEMINI_MODEL to pin one.

Nothing here can touch the graph. A model receives text and returns text; it
never gets a database handle, a tool, or a write path.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from functools import lru_cache

from . import config  # noqa: F401  loads .env before any key is read

TRANSIENT_RETRIES = int(os.environ.get("LLM_RETRIES", "2"))
BACKOFF_SECONDS = float(os.environ.get("LLM_BACKOFF", "0.8"))

# The SDK warns about automatic function calling on every call. We pass no
# tools, so it is pure noise in the terminal.
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "400"))

def provider() -> str | None:
    """LLM_PROVIDER=gemini|anthropic pins one. Otherwise Anthropic wins when its
    key is set (paid, no free-tier throttling), then Gemini."""
    pinned = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if pinned == "gemini" and os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if pinned == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


# --------------------------------------------------------------------------- #
# rate limit: a hard ceiling on model calls, so a runaway client or a loop
# cannot run up the bill. Over the limit, generate() returns None, which
# every caller already treats as "model off": answers fall back to graph-only
# search instead of failing.
#   LLM_MAX_PER_MINUTE   calls in any rolling 60 seconds   (default 30)
#   LLM_MAX_PER_DAY      calls since the API process started, reset daily (default 1500)
# Set either to 0 to disable that limit.
# --------------------------------------------------------------------------- #
LLM_MAX_PER_MINUTE = int(os.environ.get("LLM_MAX_PER_MINUTE", "30"))
LLM_MAX_PER_DAY = int(os.environ.get("LLM_MAX_PER_DAY", "1500"))
_rate_lock = threading.Lock()
_recent: deque = deque()
_day = {"date": "", "count": 0, "refused": 0}


def _allow() -> bool:
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    with _rate_lock:
        if _day["date"] != today:
            _day.update(date=today, count=0, refused=0)
        while _recent and now - _recent[0] > 60:
            _recent.popleft()
        if (LLM_MAX_PER_MINUTE and len(_recent) >= LLM_MAX_PER_MINUTE) or \
           (LLM_MAX_PER_DAY and _day["count"] >= LLM_MAX_PER_DAY):
            _day["refused"] += 1
            return False
        _recent.append(now)
        _day["count"] += 1
        return True


def usage() -> dict:
    """Current rate-limit state, for /health."""
    now = time.time()
    with _rate_lock:
        last_minute = sum(1 for t in _recent if now - t <= 60)
        return {"provider": provider(), "calls_last_minute": last_minute,
                "max_per_minute": LLM_MAX_PER_MINUTE, "calls_today": _day["count"],
                "max_per_day": LLM_MAX_PER_DAY, "refused_today": _day["refused"]}


def available() -> bool:
    return provider() is not None


@lru_cache(maxsize=1)
def _gemini():
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# Listing a model does NOT mean you can call it. A key with no billing sees 41
# models and can use a handful: the newest lines return 429 with "limit: 0",
# which is a quota refusal dressed as a rate limit. So availability is settled
# by trying, not by reading the catalogue.

# Not text generation, whatever their names suggest.
NOT_TEXT = ("image", "tts", "transcribe", "lyria", "robotics", "computer-use",
            "nano-banana", "embedding", "veo", "imagen")

# Established models first: they are the ones with free-tier quota. Newer is
# not better if it returns 429 on every call.
PREFERRED = ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite",
             "gemini-flash-lite-latest", "gemini-2.5-pro", "gemini-pro-latest")

_working: str | None = None      # first model that actually answered


@lru_cache(maxsize=1)
def candidates() -> list[str]:
    """Ordered models worth trying, best-odds first."""
    pinned = os.environ.get("GEMINI_MODEL")
    if pinned:
        return [pinned]
    try:
        names = [
            m.name.split("/")[-1] for m in _gemini().models.list()
            if "generateContent" in (getattr(m, "supported_actions", None) or [])
        ]
    except Exception:                          # noqa: BLE001 - offline or bad key
        return list(PREFERRED)

    usable = [n for n in names if not any(bad in n for bad in NOT_TEXT)]
    ordered = [n for n in PREFERRED if n in usable]
    # Then any other stable flash model, then whatever is left.
    ordered += sorted(n for n in usable if n not in ordered
                      and "flash" in n and "preview" not in n)
    ordered += sorted(n for n in usable if n not in ordered and "preview" not in n)
    return ordered or list(PREFERRED)


def model_name() -> str:
    if provider() == "anthropic":
        return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    return _working or (candidates() or ["gemini-2.5-flash"])[0]


def _call(model: str, system: str, user: str, max_tokens: int,
          thinking_off: bool, temperature: float = 0.2) -> tuple[str | None, Exception | None]:
    """One attempt. Returns (text, None) or (None, exception).

    thinking_off matters more than it looks: Gemini 2.5+ spends output tokens on
    internal reasoning BEFORE writing anything, so a small max_output_tokens can
    be consumed entirely by thinking and return an empty string rather than an
    error. These are one-sentence summaries of rows already retrieved - there is
    nothing to reason about. But some models reject thinking_budget=0 with a
    400, so the caller tries it both ways.
    """
    from google.genai import types

    settings: dict = {"system_instruction": system, "max_output_tokens": max_tokens,
                      "temperature": temperature}
    if thinking_off:
        try:
            settings["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except (AttributeError, TypeError):
            pass
    try:
        response = _gemini().models.generate_content(
            model=model, contents=user, config=types.GenerateContentConfig(**settings))
    except Exception as exc:                      # noqa: BLE001
        return None, exc

    text = (response.text or "").strip()
    if text:
        return text, None

    reason = "unknown"
    try:
        reason = str(response.candidates[0].finish_reason)
    except Exception:                             # noqa: BLE001
        pass
    if "MAX_TOKENS" in reason and thinking_off:
        return None, RuntimeError(f"400 INVALID_ARGUMENT budget exhausted ({reason})")
    return f"(empty response from {model}, finish_reason={reason})", None


def generate(system: str, user: str, max_tokens: int = MAX_TOKENS,
             temperature: float = 0.2) -> str | None:
    """Return generated text, an error string, or None when disabled.

    Errors come back as text rather than raising: a failed generation must
    degrade to 'no sentence on top of the table', never take down the answer.

    `temperature` is 0 for anything whose output is EXECUTED - a query - so the
    same question produces the same query and a failure can be reproduced.
    Prose over rows can keep a little variation; a query cannot.
    """
    global _working
    which = provider()
    if which is None:
        return None
    if not _allow():
        return None     # rate limited: callers fall back to graph-only answers

    try:
        if which == "gemini":
            # Prefer the model that answered last time, but keep the rest as
            # fallbacks. Trying ONLY the sticky one meant a per-minute quota on
            # that single model took the whole feature down, even though four
            # other models were sitting there usable.
            ladder = candidates()
            tries = ([_working] + [c for c in ladder if c != _working]) if _working else ladder
            last_error = None

            for candidate in tries:
                # Thinking off first (see _call), then on - some models reject
                # thinking_budget=0 outright with 400 INVALID_ARGUMENT.
                for thinking_off in (True, False):
                    for attempt in range(TRANSIENT_RETRIES + 1):
                        text, exc = _call(candidate, system, user, max_tokens, thinking_off,
                                          temperature)
                        if exc is None:
                            _working = candidate
                            return text
                        message = str(exc)
                        last_error = exc

                        # Busy: the same model will likely work in a moment.
                        if any(t in message for t in ("503", "500", "UNAVAILABLE")):
                            if attempt < TRANSIENT_RETRIES:
                                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                                continue
                            break
                        # Config this model will not accept - retry it with the
                        # other thinking setting before writing the model off.
                        if "INVALID_ARGUMENT" in message or "400" in message:
                            break
                        # Out of quota, or not really there. Next model.
                        if any(t in message for t in ("429", "404", "RESOURCE_EXHAUSTED")):
                            if candidate == _working:
                                _working = None
                            thinking_off = None       # skip the second config pass
                            break
                        break
                    if thinking_off is None:
                        break
            # Free-tier quotas are per-minute as well as per-day, so say how
            # long to wait rather than making it look permanently broken.
            detail = str(last_error or "")
            wait = ""
            match = re.search(r"retry in ([\d.]+)s", detail) or re.search(
                r"'retryDelay': '(\d+)s'", detail)
            if match:
                wait = f" Retry in about {float(match.group(1)):.0f}s."
            if "RESOURCE_EXHAUSTED" in detail or "429" in detail:
                return (f"(all {len(tries)} models are out of free-tier quota.{wait} "
                        f"The rows below are unaffected.)")
            return (f"(no usable model - tried {len(tries)}, last error "
                    f"{type(last_error).__name__}: {detail[:120]})")

        from anthropic import Anthropic
        # No temperature: the Claude 5 models reject the parameter outright
        # ("`temperature` is deprecated for this model"), and the prompts here
        # ask for JSON or one sentence, where the default is fine.
        response = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]).messages.create(
            model=model_name(), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        # Claude 5 may return thinking blocks before the text; keep text only.
        return "".join(getattr(b, "text", "") for b in response.content
                       if getattr(b, "type", "") == "text").strip()

    except Exception as exc:                   # noqa: BLE001 - surface, never crash
        return f"(generation failed: {type(exc).__name__}: {exc})"


if __name__ == "__main__":
    import sys
    which = provider()
    print(f"provider:  {which or 'none - set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env'}")
    if not which:
        sys.exit(1)
    result = generate("Reply with exactly: ok", "ping", max_tokens=200)
    print(f"model:     {model_name()}")
    print(f"test:      {result}")
