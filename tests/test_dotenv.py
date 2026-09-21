"""Tests for .env loading (python-dotenv) and LLM configuration resolution.

Guarantees verified here:
- .env values are loaded into configuration
- real environment variables take precedence over .env values
- a missing OPENROUTER_API_KEY degrades gracefully (no crash beyond the
  documented RuntimeError)
- no secret values are ever printed or logged by the loading code paths

No live API call is made: client CONSTRUCTION is offline; only request
methods would hit the network and none are invoked.
"""

from __future__ import annotations

import logging

import pytest

from rf_catalogue.nlp.llm_client import (
    DEFAULT_OPENROUTER_MODEL,
    OpenAICompatibleLLMClient,
    load_env,
)

_KEY_VARS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
             "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER")


@pytest.fixture
def clean_llm_env(monkeypatch):
    """Remove all LLM key/config variables so tests are hermetic."""
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _write_env(tmp_path, content: str):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    return env_file


class TestDotenvLoading:
    def test_env_values_are_loaded(self, clean_llm_env, tmp_path):
        env_file = _write_env(
            tmp_path,
            "OPENROUTER_API_KEY=test-key-from-dotenv\n"
            "LLM_MODEL=test/model-from-dotenv\n",
        )
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.model == "test/model-from-dotenv"
        # the key reached the client without being printed anywhere
        assert client._client.api_key == "test-key-from-dotenv"

    def test_real_environment_variable_takes_precedence(
        self, clean_llm_env, tmp_path
    ):
        env_file = _write_env(
            tmp_path,
            "OPENROUTER_API_KEY=dotenv-key-loses\n"
            "LLM_MODEL=dotenv/model-loses\n",
        )
        clean_llm_env.setenv("OPENROUTER_API_KEY", "env-var-key-wins")
        clean_llm_env.setenv("LLM_MODEL", "env/model-wins")

        client = OpenAICompatibleLLMClient.from_env(env_file)

        assert client.model == "env/model-wins"
        assert client._client.api_key == "env-var-key-wins"

    def test_missing_key_handled_gracefully(self, clean_llm_env, tmp_path):
        env_file = _write_env(tmp_path, "LLM_MODEL=some/model\n")  # no key
        with pytest.raises(RuntimeError, match="No LLM API key configured"):
            OpenAICompatibleLLMClient.from_env(env_file)

    def test_missing_env_file_handled_gracefully(self, clean_llm_env, tmp_path):
        nowhere = tmp_path / "does_not_exist.env"
        with pytest.raises(RuntimeError, match="No LLM API key configured"):
            OpenAICompatibleLLMClient.from_env(nowhere)

    def test_load_env_returns_path_or_none(self, clean_llm_env, tmp_path):
        env_file = _write_env(tmp_path, "LLM_MODEL=x/y\n")
        assert load_env(env_file) == env_file
        assert load_env(tmp_path / "absent.env") is None


class TestNoSecretLeakage:
    def test_error_message_contains_no_secret(self, clean_llm_env, tmp_path, caplog):
        # No key anywhere: the graceful RuntimeError must name the variables
        # and the .env location but embed no values whatsoever.
        env_file = _write_env(tmp_path, "LLM_MODEL=some/model\n")
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError) as excinfo:
                OpenAICompatibleLLMClient.from_env(env_file)
        message = str(excinfo.value)
        assert "No LLM API key configured" in message
        assert "OPENROUTER_API_KEY" in message
        assert "some/model" not in message
        assert "=" not in message.split(".env")[-1]  # no key=value pairs after path
        assert caplog.text == ""

    def test_nothing_logged_during_load(self, clean_llm_env, tmp_path, caplog):
        env_file = _write_env(tmp_path, "OPENROUTER_API_KEY=quiet-key-456\n")
        with caplog.at_level(logging.DEBUG):
            loaded = load_env(env_file)
        assert loaded == env_file
        assert "quiet-key-456" not in caplog.text
        assert caplog.text == ""

    def test_client_repr_leaks_no_key(self, clean_llm_env, tmp_path):
        env_file = _write_env(tmp_path, "OPENROUTER_API_KEY=repr-secret-789\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert "repr-secret-789" not in repr(client)
        assert "repr-secret-789" not in str(client)

    def test_default_model_when_unconfigured(self, clean_llm_env, tmp_path):
        env_file = _write_env(tmp_path, "OPENROUTER_API_KEY=some-key\n")
        client = OpenAICompatibleLLMClient.from_env(env_file)
        assert client.model == DEFAULT_OPENROUTER_MODEL
