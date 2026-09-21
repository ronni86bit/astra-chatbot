"""Provider-agnostic LLM adapter for structured query parsing.

The application depends only on the `LLMClient` protocol:

    client.complete_structured(system, user, schema) -> dict

Provider specifics live exclusively behind adapters:

- OpenAICompatibleLLMClient: any OpenAI-compatible endpoint
  (OpenRouter, OpenAI, Z.ai GLM, Groq, DeepSeek, vLLM, Ollama, ...).
  Uses native structured output (`response_format: json_schema`) and falls
  back to `json_object` mode for endpoints without schema support.
- FakeLLMClient: deterministic test double (no network).

Rules enforced here:
- The model returns structured data ONLY (JSON schema constrained).
- No prose parsing, no regex extraction of model output anywhere.
- Invalid output surfaces as StructuredOutputError -> fails validation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dotenv import load_dotenv

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"

#: Known providers for LLM_PROVIDER. Each provider uses ITS OWN key variable;
#: a selected provider never silently falls back to another provider's key.
PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_var": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
    },
    "openrouter": {
        "base_url": DEFAULT_OPENROUTER_BASE_URL,
        "key_var": "OPENROUTER_API_KEY",
        "default_model": DEFAULT_OPENROUTER_MODEL,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
}

#: Project root (src/rf_catalogue/nlp/llm_client.py -> parents[3]).
#: The application expects its .env file here.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Candidate .env locations, in priority order.
ENV_FILE_CANDIDATES = (PROJECT_ROOT / ".env", Path.cwd() / ".env")


def load_env(env_file: str | Path | None = None) -> Path | None:
    """Load the project .env file into os.environ (once, before config read).

    - Real environment variables ALWAYS take precedence: existing values are
      never overridden (python-dotenv `override=False`).
    - Values are never printed or logged by this function.
    - Returns the path of the .env file that was loaded, or None.

    An explicit `env_file` (used by tests) bypasses auto-detection.
    """
    if env_file is not None:
        candidates = [Path(env_file)]
    else:
        candidates = list(ENV_FILE_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None


class StructuredOutputError(RuntimeError):
    """The provider failed to return schema-valid structured output."""


@runtime_checkable
class LLMClient(Protocol):
    """The only LLM surface the rest of the application may depend on."""

    def complete_structured(self, system: str, user: str, schema: dict) -> dict:
        """Return a JSON-decodable dict constrained by `schema`."""
        ...  # pragma: no cover


class OpenAICompatibleLLMClient:
    """Adapter for OpenAI-compatible chat-completions endpoints.

    Works with OpenRouter (default), OpenAI, Z.ai, Groq, DeepSeek, Ollama.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        base_url: str = DEFAULT_OPENROUTER_BASE_URL,
        temperature: float = 0.0,
        timeout: float = 60.0,
        max_tokens: int = 700,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "The 'openai' package is required for LLM parsing. "
                "Install it with: pip install openai"
            ) from exc
        self._client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout
        )
        self.model = model
        self.temperature = temperature
        #: Provider label (groq / openrouter / openai / custom); set by
        #: from_env, or "custom" when constructed directly.
        self.provider: str = "custom"
        #: Structured drafts are small JSON; an explicit completion cap keeps
        #: cost bounded and lets near-empty credit balances still serve calls.
        self.max_tokens = max_tokens
        #: Observability (diagnostics only): which structured-output mode the
        #: last call used and the provider-side error, if any. Never contains
        #: credentials.
        self.last_call_mode: str | None = None
        self.last_provider_error: str | None = None

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "OpenAICompatibleLLMClient":
        """Build a client from environment configuration.

        The project .env file is loaded first (project root, then CWD);
        real environment variables keep precedence over .env values.

        Provider selection via ``LLM_PROVIDER`` (groq | openrouter | openai):

            LLM_PROVIDER=groq      -> requires GROQ_API_KEY
                                      base https://api.groq.com/openai/v1
            LLM_PROVIDER=openrouter-> requires OPENROUTER_API_KEY
                                      base https://openrouter.ai/api/v1
            LLM_PROVIDER=openai    -> requires OPENAI_API_KEY
                                      base https://api.openai.com/v1

        When LLM_PROVIDER is unset, the legacy auto behaviour applies:
            LLM_API_KEY (+ optional LLM_BASE_URL)  -> generic endpoint
            OPENROUTER_API_KEY                     -> OpenRouter
            OPENAI_API_KEY                         -> OpenAI

        ``LLM_MODEL`` always overrides the provider default model;
        ``LLM_BASE_URL`` always overrides the provider base URL.
        A selected provider never silently uses another provider's key.
        """
        load_env(env_file)

        provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
        if provider:
            if provider not in PROVIDER_CONFIG:
                known = ", ".join(sorted(PROVIDER_CONFIG))
                raise RuntimeError(
                    f"Unknown LLM_PROVIDER {provider!r}. Known providers: {known}."
                )
            cfg = PROVIDER_CONFIG[provider]
            key_var = cfg["key_var"]
            api_key = os.environ.get(key_var)
            if not api_key:
                raise RuntimeError(
                    f"LLM_PROVIDER={provider} requires {key_var}. Set it as an "
                    f"environment variable or in {PROJECT_ROOT / '.env'} "
                    f"(see .env.example)."
                )
            base_url = os.environ.get("LLM_BASE_URL") or cfg["base_url"]
            model = os.environ.get("LLM_MODEL") or cfg["default_model"]
            client = cls(api_key=api_key, model=model, base_url=base_url)
            client.provider = provider
            return client

        # Legacy / auto selection (LLM_PROVIDER unset).
        api_key = os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
        provider = "custom"
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY")
            provider = "openrouter"
        if not api_key:
            api_key = os.environ.get("GROQ_API_KEY")
            if api_key:
                provider = "groq"
                if "LLM_BASE_URL" not in os.environ:
                    base_url = PROVIDER_CONFIG["groq"]["base_url"]
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                provider = "openai"
                if "LLM_BASE_URL" not in os.environ:
                    base_url = "https://api.openai.com/v1"
        if not api_key:
            raise RuntimeError(
                "No LLM API key configured. Set LLM_PROVIDER (groq, openrouter, "
                "openai) with the matching key variable (GROQ_API_KEY, "
                "OPENROUTER_API_KEY, OPENAI_API_KEY, or LLM_API_KEY) as an "
                f"environment variable or in {PROJECT_ROOT / '.env'} "
                f"(see .env.example)."
            )
        model = os.environ.get("LLM_MODEL") or \
            PROVIDER_CONFIG.get(provider, {}).get("default_model",
                                                  DEFAULT_OPENROUTER_MODEL)
        client = cls(api_key=api_key, model=model, base_url=base_url)
        client.provider = provider
        return client

    def complete_structured(self, system: str, user: str, schema: dict) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        self.last_provider_error = None
        # 1) Native structured output (strict schema) when supported.
        try:
            self.last_call_mode = "json_schema"
            response = self._create_with_retry(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "query_intent_draft", "schema": schema,
                                    "strict": True},
                },
            )
            content = response.choices[0].message.content
            return self._decode(content)
        except StructuredOutputError:
            raise
        except Exception as exc:
            self.last_provider_error = f"{type(exc).__name__}: {exc}"[:500]
            if self._is_permanent_provider_error(exc):
                raise
            # 2) Fallback: json_object mode with the schema in the prompt.
            #    The API contract still guarantees a JSON object — this is
            #    structured output, not prose parsing. (Non-silent: the mode
            #    is recorded in last_call_mode for observability.)
            self.last_call_mode = "json_object_fallback"
            response = self._create_with_retry(
                model=self.model,
                messages=[
                    {"role": "system",
                     "content": system + "\nRespond with a single JSON object "
                                "that validates against this JSON schema:\n"
                                + json.dumps(schema)},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            return self._decode(content)

    def _create_with_retry(self, **kwargs):
        """Single retry for transient generation/provider hiccups.

        Retries only non-auth, non-schema-rejection errors (e.g. Groq's
        transient ``json_validate_failed`` on reasoning models, 429, 5xx).
        Authentication and credit errors are never retried.
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            text = f"{exc}"
            if self._is_permanent_provider_error(exc):
                raise
            self.last_provider_error = f"retry after: {type(exc).__name__}: {exc}"[:500]
            return self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _is_permanent_provider_error(exc: Exception) -> bool:
        text = f"{exc}".lower()
        return ("401" in text or "unauthorized" in text
                or "402" in text or "credit" in text
                or "invalid json schema" in text
                or "is not permitted" in text)

    @staticmethod
    def _decode(content: str | None) -> dict:
        if not content:
            raise StructuredOutputError("LLM returned empty content")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(
                f"LLM returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise StructuredOutputError("LLM output is not a JSON object")
        return data


class FakeLLMClient:
    """Deterministic scripted client for offline tests.

    Either `responses` (list of dicts, consumed in order) or `handler`
    (callable user_text -> dict) may be supplied. Records calls for
    assertions.
    """

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses) if responses is not None else None
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.handler is not None:
            return self.handler(user)
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError("FakeLLMClient has no scripted response left")


def default_client(env_file: str | Path | None = None) -> LLMClient:
    """Build the default client from the environment."""
    return OpenAICompatibleLLMClient.from_env(env_file=env_file)
