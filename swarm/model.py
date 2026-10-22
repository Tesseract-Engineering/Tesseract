"""
model.py - the one place the swarm calls a model.

Every LLM step goes through Model.extract(system, text, schema): a system
prompt, the user text, and a JSON Schema the answer must match. Moving from
the direct API to Bedrock is a config change (docs/SWARM.md, "Architecture"):

  SWARM_MODEL_BACKEND  anthropic (local dev, the default) | bedrock | off
  SWARM_MODEL          claude-opus-5-5; Bedrock adds its anthropic. prefix
  SWARM_MODEL_EFFORT   low | medium | high | xhigh | max (default medium)
  AWS_REGION           Bedrock only

  ClaudeApi   the Anthropic API, through the egress proxy in local dev
  Bedrock     the customer's Bedrock endpoint, over a private link
  Replay      recorded answers, keyed by the request; CI never calls a model

A failure the model can't fix by being asked again (a refusal, an answer cut
off at max_tokens) comes back as a Completion with no data. A failure that may
pass (rate limits, timeouts, 5xx after the SDK's own retries) raises
ModelUnavailable, and the caller stops asking for this run. Anything else, a
bad key or an unknown model, raises as it is: the run fails and is retried.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .records import Tokens

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000
# Server-side refusal fallback: a declined request is rerun on the model
# Anthropic recommends for that refusal category. Direct API only; Bedrock
# has no server-side fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelUnavailable(RuntimeError):
    """The model endpoint is down or rate limited; ask again next run."""


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any] | None          # the parsed answer, or None when there is none
    model: str                           # the model that answered, after any fallback
    stop_reason: str
    tokens: Tokens = field(default_factory=Tokens)


class Model(Protocol):
    name: str

    def extract(self, system: str, text: str, schema: dict[str, Any]) -> Completion: ...


def _request(model: str, system: str, text: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    return dict(
        model=model, max_tokens=MAX_TOKENS,
        # The system prompt is the same for every item of one kind; cache it.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": text}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )


def _completion(response: Any) -> Completion:
    usage = response.usage
    tokens = Tokens(input=(usage.input_tokens or 0) + (usage.cache_creation_input_tokens or 0)
                    + (usage.cache_read_input_tokens or 0), output=usage.output_tokens or 0)
    data = None
    if response.stop_reason == "end_turn":
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return Completion(data=data, model=response.model, stop_reason=response.stop_reason or "", tokens=tokens)


def _call(create, **kwargs) -> Completion:
    import anthropic

    try:
        return _completion(create(**kwargs))
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
        raise ModelUnavailable(type(e).__name__) from e


class ClaudeApi:
    """The Anthropic API. Credentials resolve the SDK's usual way
    (ANTHROPIC_API_KEY in .env for local dev); HTTPS_PROXY is honored."""

    def __init__(self, model: str = DEFAULT_MODEL, *, effort: str = DEFAULT_EFFORT, client: Any = None):
        import anthropic

        self.name = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def extract(self, system: str, text: str, schema: dict[str, Any]) -> Completion:
        return _call(self.client.beta.messages.create, betas=[FALLBACK_BETA], fallbacks="default",
                     **_request(self.name, system, text, schema, self.effort))


class Bedrock:
    """Claude on the customer's Bedrock, through the Messages-API (Mantle)
    endpoint. AWS credentials come from the job's role."""

    def __init__(self, model: str = DEFAULT_MODEL, *, effort: str = DEFAULT_EFFORT,
                 region: str | None = None, client: Any = None):
        import anthropic

        self.name = model if model.startswith("anthropic.") else f"anthropic.{model}"
        self.effort = effort
        self.client = client or anthropic.AnthropicBedrockMantle(aws_region=region or os.environ.get("AWS_REGION"))

    def extract(self, system: str, text: str, schema: dict[str, Any]) -> Completion:
        return _call(self.client.messages.create, **_request(self.name, system, text, schema, self.effort))


def request_key(system: str, text: str, schema: dict[str, Any]) -> str:
    blob = json.dumps([system, text, schema], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class Replay:
    """Answers recorded on disk, one JSON file per request:

        {dir}/{request_key}.json   {"model", "stop_reason", "tokens", "data"}

    With `record`, a request with no recording goes to that model and its
    answer is saved; without it, a missing recording is an error, so a prompt
    change shows up in CI as missing recordings rather than silent calls."""

    def __init__(self, directory: str | Path, *, record: Model | None = None):
        self.dir = Path(directory)
        self.record = record
        self.name = record.name if record else "replay"

    def extract(self, system: str, text: str, schema: dict[str, Any]) -> Completion:
        path = self.dir / f"{request_key(system, text, schema)}.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            return Completion(data=saved["data"], model=saved["model"], stop_reason=saved["stop_reason"],
                              tokens=Tokens(**saved["tokens"]))
        if self.record is None:
            raise FileNotFoundError(f"no recorded answer at {path}; record one with a real model")
        done = self.record.extract(system, text, schema)
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": done.model, "stop_reason": done.stop_reason,
                                    "tokens": done.tokens.model_dump(), "data": done.data},
                                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return done


def from_env() -> Model | None:
    """The model the environment asks for, or None when LLM steps are off."""
    backend = os.environ.get("SWARM_MODEL_BACKEND", "anthropic")
    model = os.environ.get("SWARM_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("SWARM_MODEL_EFFORT", DEFAULT_EFFORT)
    if backend == "off":
        return None
    if backend == "anthropic":
        return ClaudeApi(model, effort=effort)
    if backend == "bedrock":
        return Bedrock(model, effort=effort)
    raise SystemExit(f"SWARM_MODEL_BACKEND={backend!r}: expected anthropic, bedrock or off")
