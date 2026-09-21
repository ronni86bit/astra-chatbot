"""Automated acceptance-runner regression tests (offline, no network).

Covers mission section 15:
- standalone cases start with clean context
- conversation cases preserve context
- cases cannot contaminate each other
- expected row IDs are structurally checked (not response-text matching)
- provider errors are PROVIDER_ERROR, never model failures
- reports contain no secrets
- offline mode works without any API key
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rf_catalogue import acceptance
from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import FakeLLMClient
from rf_catalogue.service import RfCatalogueService

PROVIDER_KEY_VARS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                     "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL")


@pytest.fixture(scope="module")
def engine():
    return CatalogueEngine()


@pytest.fixture
def no_provider_keys(monkeypatch):
    for var in PROVIDER_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def offline_service(engine):
    """Offline service: mocked LLM, real deterministic engine."""
    return RfCatalogueService(engine=engine,
                              llm_client=FakeLLMClient(responses=[]))


# ---------------------------------------------------------------------------
# The full 30-case offline acceptance suite
# ---------------------------------------------------------------------------


def test_offline_manual_acceptance_all_pass(engine, offline_service,
                                            no_provider_keys):
    """THE acceptance gate: all 30 cases pass in OFFLINE/MOCKED mode.

    Proves: context handling (clean vs conversation), QueryIntent
    validation, deterministic lookup (row IDs), and rendering assertions.
    """
    report = acceptance.run_manual_suite(
        engine, offline_service, offline=True,
        cases_path=acceptance.DEFAULT_CASES_PATH)

    assert report["total_cases"] == 30, "the plan defines 30 cases"
    failed = [c for c in report["cases"]
              if c["outcome"] != acceptance.OUTCOME_PASSED]
    assert not failed, (
        "failing cases: "
        + "; ".join(f"{c['id']}: {c['reason']}" for c in failed)
    )
    assert report["counts"]["passed"] == 30
    assert report["counts"]["provider_error"] == 0
    assert report["counts"]["failed"] == 0
    # all 15 row-cited cases resolved their exact expected rows
    row_cases = [c for c in report["cases"] if c["expected_row_id"] is not None]
    assert len(row_cases) == 15
    assert all(c["actual_row_id"] == c["expected_row_id"] for c in row_cases)


def test_standalone_cases_get_clean_context(engine, offline_service,
                                            no_provider_keys):
    """Run the contaminated conversation first; the standalone threshold
    case that follows must still resolve to row 2850 with scope All nodes
    (no ADL8124 contamination)."""
    cases_path = acceptance.DEFAULT_CASES_PATH
    report = acceptance.run_manual_suite(engine, offline_service, offline=True,
                                         cases_path=cases_path)
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["M07"]["actual_row_id"] == 3227      # part summary (ADL8124)
    assert by_id["M09"]["actual_row_id"] == 2853      # standalone threshold
    assert by_id["M09"]["resolved_intent"][2] == "All nodes"


def test_conversation_case_preserves_context(engine, offline_service,
                                             no_provider_keys):
    report = acceptance.run_manual_suite(engine, offline_service, offline=True,
                                         cases_path=acceptance.DEFAULT_CASES_PATH)
    by_id = {c["id"]: c for c in report["cases"]}
    m23 = by_id["M23"]
    assert m23["outcome"] == acceptance.OUTCOME_PASSED
    msgs = m23["messages"]
    # message 1: Worst -> row 10; message 2 (elliptical) inherits -> row 9
    assert msgs[0].actual_row_id == 10
    assert msgs[1].actual_row_id == 9
    # the second message's intent inherited scope Final node
    assert m23["resolved_intent"][2] == "Final node"


def test_reset_case_proves_no_stale_resolution(engine, offline_service,
                                               no_provider_keys):
    report = acceptance.run_manual_suite(engine, offline_service, offline=True,
                                         cases_path=acceptance.DEFAULT_CASES_PATH)
    m25 = {c["id"]: c for c in report["cases"]}["M25"]
    assert m25["outcome"] == acceptance.OUTCOME_PASSED
    last = m25["messages"][-1]
    assert last.actual_status == "NEEDS_CLARIFICATION"


# ---------------------------------------------------------------------------
# Structural runner guarantees
# ---------------------------------------------------------------------------


def test_row_id_mutation_causes_failure(engine, offline_service,
                                        no_provider_keys, tmp_path):
    """Expected row IDs are structurally enforced: a mocked intent that
    resolves a DIFFERENT row than expected FAILS, even though the rendered
    answer would look plausible."""
    cases = json.loads(Path(acceptance.DEFAULT_CASES_PATH)
                       .read_text(encoding="utf-8"))
    tampered = [c for c in cases["cases"] if c["id"] == "M01"][0]
    # keep expected_row_id=1 (MismatchLoss/Best) but inject a Worst intent:
    # the lookup resolves row 2 — the runner must flag the row mismatch.
    tampered["messages"][0]["offline_draft"] = {
        "metric": "MismatchLoss", "request": "Worst", "scope": "Final node",
        "frequency_selection": "All Frequencies", "unit": "dB",
        "params": {"kind": "none"}}
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps({"cases": [tampered]}), encoding="utf-8")

    report = acceptance.run_manual_suite(engine, offline_service, offline=True,
                                         cases_path=path)
    assert report["counts"]["failed"] == 1
    case = report["cases"][0]
    assert case["outcome"] == acceptance.OUTCOME_FAILED
    assert case["actual_row_id"] == 2 and case["expected_row_id"] == 1
    assert "row 2 != expected 1" in case["reason"]


def test_unknown_expected_row_becomes_test_plan_review(engine, offline_service,
                                                       no_provider_keys,
                                                       tmp_path):
    cases = json.loads(Path(acceptance.DEFAULT_CASES_PATH)
                       .read_text(encoding="utf-8"))
    bad = dict(cases["cases"][0])
    bad["id"] = "BAD-ROW"
    bad["messages"][0]["expected_row_id"] = 999999
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"cases": [bad]}), encoding="utf-8")

    loaded, review = acceptance.load_manual_cases(path, engine)
    assert not loaded and len(review) == 1
    assert review[0]["id"] == "BAD-ROW"


def test_provider_error_is_not_counted_as_model_failure(engine,
                                                        no_provider_keys,
                                                        tmp_path):
    """Provider failures produce PROVIDER_ERROR outcomes with a
    classification, never FAILED/PASSED."""

    class Exploding:
        def complete_structured(self, system, user, schema):
            raise TimeoutError("connection timed out")

    service = RfCatalogueService(engine=engine, llm_client=Exploding())
    cases, _ = acceptance.load_manual_cases(acceptance.DEFAULT_CASES_PATH,
                                            engine)
    m01 = [c for c in cases if c["id"] == "M01"][0]
    m01["messages"][0]["expected_row_id"] = None

    path = tmp_path / "single.json"
    path.write_text(json.dumps({"cases": [m01]}), encoding="utf-8")
    report = acceptance.run_manual_suite(engine, service, offline=False,
                                         cases_path=path)
    m01_result = next(c for c in report["cases"] if c["id"] == "M01")
    assert m01_result["outcome"] == acceptance.OUTCOME_PROVIDER_ERROR
    assert m01_result["reason"] == "timeout"


def _write_single(case: dict) -> Path:
    path = Path(acceptance.DEFAULT_CASES_PATH).parent / "_single_case.json"
    path.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    return path


def test_provider_error_classification():
    classify = acceptance.classify_provider_error
    assert classify("Error code: 402 - insufficient credits") == \
        "insufficient_credits"
    assert classify("401 Unauthorized") == "authentication"
    assert classify("rate limit exceeded (429)") == "rate_limit"
    assert classify("connection timed out") == "timeout"
    assert classify("json_validate_failed on generation") == \
        "malformed_structured_output"
    assert classify("HTTP 503 server error") == "server_error"
    assert classify("weird failure") == "transient_provider_failure"


# ---------------------------------------------------------------------------
# Secrets / reports
# ---------------------------------------------------------------------------


def test_reports_contain_no_secrets(engine, no_provider_keys, tmp_path):
    fake_secret = "gsk_FAKE_SECRET_VALUE_9876"
    no_provider_keys.setenv("GROQ_API_KEY", fake_secret)

    service = RfCatalogueService(engine=engine,
                                 llm_client=FakeLLMClient(responses=[]))
    report = acceptance.run_manual_suite(engine, service, offline=True,
                                         cases_path=acceptance.DEFAULT_CASES_PATH)
    json_text = json.dumps(report, default=str)
    md_path = Path(acceptance.DEFAULT_MD_OUT)
    acceptance.write_md_report(report, md_path)
    md_text = md_path.read_text(encoding="utf-8")

    assert fake_secret not in json_text
    assert fake_secret not in md_text
    for text in (json_text, md_text):
        assert "Authorization" not in text
        assert "Bearer " not in text


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_offline_manual_runs_without_api_key(no_provider_keys, capsys):
    """`--subset manual --offline` works with zero provider configuration."""
    from rf_catalogue.evaluation import main

    rc = main(["--subset", "manual", "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MANUAL ACCEPTANCE TEST" in out
    assert "OFFLINE / MOCKED" in out


def test_cli_live_manual_without_key_reports_unavailable(no_provider_keys,
                                                         monkeypatch, capsys):
    """Without a configured provider key the CLI reports LIVE_UNAVAILABLE
    (exit 3) instead of crashing — simulated by an unconfigured adapter."""
    import rf_catalogue.nlp.llm_client as llm_client_module

    def _no_key(env_file=None):
        raise RuntimeError("No LLM API key configured. "
                           "Set LLM_PROVIDER + the matching key variable.")

    monkeypatch.setattr(llm_client_module, "default_client", _no_key)
    from rf_catalogue.evaluation import main

    rc = main(["--subset", "manual"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "LIVE_UNAVAILABLE" in out
