"""Provider-configuration tests (LLM_PROVIDER registry: groq/openrouter/openai).

All tests are offline: constructing the OpenAI SDK client performs no
network I/O; only request methods would, and none are invoked.
"""

from __future__ import annotations

import os

import pytest

from rf_catalogue.nlp.llm_client import (
    PROVIDER_CONFIG,
    OpenAICompatibleLLMClient,
)

ALL_KEY_VARS = (
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
)


@pytest.fixture(autouse=True)
def scrub_llm_env(monkeypatch):
    """Remove every provider variable before AND after each test.

    load_dotenv writes into the real os.environ (outside monkeypatch's
    tracking), so a post-test scrub is required to prevent leakage into
    other test modules.
    """
    for var in ALL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in ALL_KEY_VARS:
        os.environ.pop(var, None)


def write_env(tmp_path, content: str):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


class TestProviderSelection:
    def test_groq_provider_initializes_with_groq_key(self, monkeypatch, tmp_path):
        """A. LLM_PROVIDER=groq + GROQ_API_KEY -> client initializes."""
        env_file = write_env(tmp_path, "GROQ_API_KEY=groq-key-123\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.provider == "groq"
        assert client.model == PROVIDER_CONFIG["groq"]["default_model"]
        assert client._client.base_url.host == "api.groq.com"
        assert client._client.api_key == "groq-key-123"

    def test_groq_model_default_is_gpt_oss_120b(self, monkeypatch, tmp_path):
        env_file = write_env(tmp_path, "GROQ_API_KEY=k\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.model == "openai/gpt-oss-120b"

    def test_groq_missing_key_fails_gracefully(self, monkeypatch, tmp_path):
        """B. LLM_PROVIDER=groq + no GROQ_API_KEY -> clear config error."""
        env_file = write_env(tmp_path, "LLM_PROVIDER=groq\n")
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            OpenAICompatibleLLMClient.from_env(env_file)

    def test_openrouter_provider_still_supported(self, monkeypatch, tmp_path):
        """C. LLM_PROVIDER=openrouter + OPENROUTER_API_KEY -> initializes."""
        env_file = write_env(
            tmp_path, "LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=or-key-1\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.provider == "openrouter"
        assert client._client.base_url.host == "openrouter.ai"
        assert client._client.api_key == "or-key-1"

    def test_groq_never_uses_openrouter_credentials(self, monkeypatch, tmp_path):
        """D. provider=groq + OPENROUTER_API_KEY but no GROQ_API_KEY
        must NOT silently use the OpenRouter key."""
        env_file = write_env(
            tmp_path,
            "LLM_PROVIDER=groq\nOPENROUTER_API_KEY=openrouter-key-xyz\n")
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            OpenAICompatibleLLMClient.from_env(env_file)

    def test_environment_variable_overrides_dotenv(self, monkeypatch, tmp_path):
        """E. Real environment variables take precedence over .env values."""
        env_file = write_env(
            tmp_path,
            "LLM_PROVIDER=groq\nGROQ_API_KEY=dotenv-groq-key\n"
            "LLM_MODEL=dotenv/model\n")
        monkeypatch.setenv("GROQ_API_KEY", "env-groq-key")
        monkeypatch.setenv("LLM_MODEL", "env/model")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client._client.api_key == "env-groq-key"
        assert client.model == "env/model"

    def test_model_read_from_llm_model(self, monkeypatch, tmp_path):
        """F. LLM_MODEL overrides the provider default."""
        env_file = write_env(
            tmp_path,
            "LLM_PROVIDER=groq\nGROQ_API_KEY=k\nLLM_MODEL=openai/gpt-oss-120b\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.model == "openai/gpt-oss-120b"

    def test_unknown_provider_fails_clearly(self, monkeypatch, tmp_path):
        """G. Unknown LLM_PROVIDER -> clear error naming known providers."""
        env_file = write_env(
            tmp_path, "LLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=x\n")
        with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER") as exc:
            OpenAICompatibleLLMClient.from_env(env_file)
        assert "groq" in str(exc.value) and "openrouter" in str(exc.value)

    def test_openrouter_key_does_not_leak_into_groq_client(self, monkeypatch, tmp_path):
        """Security: the Groq client must carry the Groq key only."""
        env_file = write_env(
            tmp_path,
            "LLM_PROVIDER=groq\nGROQ_API_KEY=groq-real-key\n"
            "OPENROUTER_API_KEY=or-should-be-ignored\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client._client.api_key == "groq-real-key"
