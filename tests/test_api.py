"""Offline API tests for the FastAPI layer.

All LLM interactions are scripted (FakeLLMClient) — no network, no key.
Uses TestClient against an app built with a deterministic service.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rf_catalogue.api import MAX_MESSAGE_CHARS, create_app
from rf_catalogue.nlp.llm_client import FakeLLMClient
from rf_catalogue.service import RfCatalogueService

fastapi = pytest.importorskip("fastapi")


def draft(metric="MismatchLoss", request="Best", scope="Final node",
          freq="All Frequencies", unit="dB", params=None, **extra):
    d = {
        "metric": metric, "request": request, "scope": scope,
        "frequency_selection": freq, "unit": unit,
        "params": params if params is not None else {"kind": "none"},
        "unsupported_dimension": None, "unsupported_value": None,
        "unresolvable_reason": None,
    }
    d.update(extra)
    return d


@pytest.fixture(scope="module")
def client(engine):
    """Entered TestClient (lifespan runs) with a scripted service."""
    handler = lambda user: draft()  # noqa: E731
    service = RfCatalogueService(
        engine=engine, llm_client=FakeLLMClient(handler=handler))
    app = create_app(service=service, cors_origins=["http://test origin"])
    with TestClient(app) as c:
        yield c


def post_chat(client, message, session_id=None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/api/chat", json=body)


class TestHealthStatusExamples:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_status_reports_catalogue(self, client):
        info = client.get("/api/status").json()
        assert info["catalogue_loaded"] is True
        assert info["catalogue_rows"] > 4000
        # No secret-bearing fields in the status payload.
        blob = str(info).lower()
        assert "api_key" not in blob and "token" not in blob
        assert "gsk_" not in blob

    def test_examples_returns_catalogue_questions(self, client):
        examples = client.get("/api/examples").json()["examples"]
        assert len(examples) >= 2
        assert all(isinstance(q, str) and q for q in examples)

    def test_cors_headers_present(self, client):
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "http://test origin",
                "Access-Control-Request-Method": "GET",
            })
        assert resp.headers.get("access-control-allow-origin") == \
            "http://test origin"

    def test_cors_rejected_for_unknown_origin(self, client):
        resp = client.options(
            "/api/chat",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            })
        assert resp.headers.get("access-control-allow-origin") != \
            "http://evil.example"


class TestChatAnswering:
    def test_answered_includes_verbatim_row_and_provenance(self, client):
        body = post_chat(client, "What is the best Mismatch Loss in the "
                                 "final node?").json()
        result = body["result"]
        assert result["status"] == "ANSWERED"
        assert result["answer_text"]
        assert isinstance(result["row_id"], int)
        assert result["intent"]["metric"] == "MismatchLoss"
        assert result["intent"]["request"] == "Best"

    def test_session_id_issued_and_stable(self, client):
        sid1 = post_chat(client, "question one").json()["session_id"]
        assert sid1
        sid2 = post_chat(client, "question two", session_id=sid1).json()[
            "session_id"]
        assert sid1 == sid2

    def test_unknown_session_id_creates_new_session(self, client):
        body = post_chat(client, "hello",
                         session_id="nonexistent-session-1").json()
        assert body["session_id"] != "nonexistent-session-1"

    def test_validation_rejects_blank_and_oversized(self, client):
        assert client.post("/api/chat", json={"message": ""}).status_code \
            == 422
        assert client.post("/api/chat", json={"message": "x" * (
            MAX_MESSAGE_CHARS + 1)}).status_code == 422

    def test_no_secret_in_any_response(self, client):
        for resp in (client.get("/api/status"),
                     post_chat(client, "any question here")):
            assert "GROQ_API_KEY" not in resp.text
            assert "gsk_" not in resp.text


class TestConversationFollowUps:
    """Follow-up handling must survive the HTTP boundary via sessions.

    Pattern follows tests/test_milestone_final.py
    (test_short_reply_completes_fully_when_possible): the draft omits
    ONLY the metric, so a bare "CGAIN" reply fully completes the pending
    clarification without a new LLM call.
    """

    def test_clarification_then_reply_resolves(self, engine):
        service = RfCatalogueService(
            engine=engine,
            llm_client=FakeLLMClient(handler=lambda u: draft(
                metric=None, request="Worst", scope="Final node",
                freq="All Frequencies", unit=None)))
        app = create_app(service=service)
        with TestClient(app) as c:
            first = post_chat(
                c, "What is the worst thing in the final node?").json()
            assert first["result"]["status"] == "NEEDS_CLARIFICATION"
            assert "metric" in first["result"]["missing_fields"]

            sid = first["session_id"]
            second = post_chat(c, "CGAIN", session_id=sid).json()
            assert second["result"]["status"] == "ANSWERED"
            assert second["result"]["intent"]["metric"] == "CGAIN"
            assert isinstance(second["result"]["row_id"], int)

    def test_candidate_clarification_resumes_via_session(self, engine):
        """Regression: candidate-listing clarifications (scope options) must
        also resume from a short reply (parser now stores pending state)."""
        service = RfCatalogueService(
            engine=engine,
            llm_client=FakeLLMClient(handler=lambda u: draft(
                metric="MismatchLoss", request="Best", scope=None,
                freq="All Frequencies", unit=None)))
        app = create_app(service=service)
        with TestClient(app) as c:
            first = post_chat(c, "What is the best Mismatch Loss?").json()
            assert first["result"]["status"] == "NEEDS_CLARIFICATION"
            assert "scope" in first["result"]["missing_fields"]
            assert first["result"]["candidate_hints"]

            # Clickable candidate chips carry the varying dimension's value.
            labels = [c["label"] for c in first["result"]["candidates"]]
            assert labels == ["All nodes", "Final node"]

            sid = first["session_id"]
            second = post_chat(c, "All nodes", session_id=sid).json()
            assert second["result"]["status"] == "ANSWERED"
            assert second["result"]["intent"]["scope"] == "All nodes"

    def test_sessions_are_isolated(self, engine):
        service = RfCatalogueService(
            engine=engine,
            llm_client=FakeLLMClient(handler=lambda u: draft(
                metric=None, request="Worst", scope="Final node",
                freq="All Frequencies", unit=None)))
        app = create_app(service=service)
        with TestClient(app) as c:
            sid_a = post_chat(
                c, "What is the worst thing in the final node?").json()[
                "session_id"]
            # Session B carries out its own (successful) exchange in between.
            sid_b = post_chat(c, "an unrelated question").json()["session_id"]
            assert sid_a != sid_b
            # A's pending clarification is untouched by B's activity.
            resumed = post_chat(c, "CGAIN", session_id=sid_a).json()
            assert resumed["result"]["status"] == "ANSWERED"
            assert resumed["result"]["intent"]["metric"] == "CGAIN"

    def test_reset_clears_pending(self, engine):
        service = RfCatalogueService(
            engine=engine,
            llm_client=FakeLLMClient(handler=lambda u: draft(
                metric=None, request="Worst", scope="Final node",
                freq="All Frequencies", unit=None)))
        app = create_app(service=service)
        with TestClient(app) as c:
            sid = post_chat(
                c, "What is the worst thing in the final node?").json()[
                "session_id"]
            assert c.post("/api/reset", json={"session_id": sid}).status_code \
                == 200
            # After reset the bare reply is no longer a pending-completion:
            # it re-parses fresh and still lacks the metric -> clarification.
            after = post_chat(c, "CGAIN", session_id=sid).json()
            assert after["result"]["status"] == "NEEDS_CLARIFICATION"

    def test_reset_unknown_session_404(self, client):
        resp = client.post("/api/reset",
                           json={"session_id": "does-not-exist-xx"})
        assert resp.status_code == 404


class TestSlashCommands:
    def test_reset_command_via_chat(self, client):
        sid = post_chat(client, "hello there").json()["session_id"]
        body = post_chat(client, "/reset", session_id=sid).json()
        assert body["result"]["status"] == "ANSWERED"
        assert "cleared" in body["result"]["message"].lower()

    def test_terminal_only_commands_reported_unsupported(self, client):
        for cmd in ("/status", "/debug", "/examples"):
            body = post_chat(client, cmd).json()
            assert body["result"]["status"] == "UNSUPPORTED"

    def test_unknown_command(self, client):
        body = post_chat(client, "/frobnicate").json()
        assert body["result"]["status"] == "UNSUPPORTED"
        assert "Unknown command" in body["result"]["message"]


class TestDegradedMode:
    def test_no_service_returns_explicit_guidance(self, monkeypatch):
        # Force the startup service build to fail as it would without a
        # configured key (never touches .env / network in tests).
        import rf_catalogue.api as api_module

        def _no_key():
            raise RuntimeError("no API key configured")

        monkeypatch.setattr(api_module, "RfCatalogueService", _no_key)
        app = create_app()
        with TestClient(app) as c:
            body = post_chat(c, "any question").json()
            assert body["result"]["status"] == "ERROR"
            assert "not available" in body["result"]["message"]
            # Status endpoint still works.
            info = c.get("/api/status").json()
            assert info["catalogue_loaded"] is False
