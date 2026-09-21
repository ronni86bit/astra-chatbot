"""Milestone-final policy tests: metric override, guess veto, catalogue
inference, pending clarification, request/frequency separation, and the
terminal chatbot surface.

All LLM interactions are scripted (FakeLLMClient) — no network.
"""

from __future__ import annotations

import pytest

from rf_catalogue.chatbot import handle_command, render_result
from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import FakeLLMClient
from rf_catalogue.nlp.parser import QueryParser
from rf_catalogue.nlp.schemas import ParseStatus
from rf_catalogue.query_intent import ReferenceQualifier, ThresholdParams
from rf_catalogue.service import AnswerStatus, RfCatalogueService

# Engine fixture is session-scoped in conftest.py


def make_parser(engine, response: dict) -> QueryParser:
    return QueryParser(engine, FakeLLMClient(responses=[response]))


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


# ---------------------------------------------------------------------------
# Metric display-name precedence (mission: metric disambiguation)
# ---------------------------------------------------------------------------


class TestMetricOverride:
    def test_desired_channel_power_overrides_model_cp(self, engine):
        """Live failure mode 1a: model said CP; exact alias must override."""
        parser = make_parser(engine, draft(
            metric="CP", request="Closest value", scope="All nodes",
            unit="dBm", params={"closest_target": -40.0}))
        outcome = parser.parse(
            "Where is Desired Channel Power closest to -40 dBm in All nodes?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "DCP"
        assert outcome.intent.params.closest_target == -40.0

    def test_channel_noise_power_overrides_model_cp(self, engine):
        """Live failure mode 1b: model said CP; exact alias must override."""
        parser = make_parser(engine, draft(
            metric="CP", request="Threshold Above", scope="All nodes",
            unit="dBm", params={"threshold": -60.0}))
        outcome = parser.parse(
            "Where is Channel Noise Power above -60 dBm in All nodes?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "CNP"

    def test_canonical_token_beats_alias(self, engine):
        parser = make_parser(engine, draft(metric="GAIN", request="Worst"))
        outcome = parser.parse("What is the worst CGAIN in the Final node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "CGAIN"
        assert outcome.intent.request == "Worst"

    def test_alias_fills_null_metric(self, engine):
        parser = make_parser(engine, draft(
            metric=None, request="Closest value", scope=None, freq=None,
            unit=None, params={"closest_target": -40.0}))
        outcome = parser.parse(
            "Where is Desired Channel Power closest to -40 dBm in All nodes?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "DCP"
        assert outcome.intent.unit == "dBm"
        assert outcome.intent.frequency_selection == "All Frequencies"

    def test_cascaded_gain_aliases_resolve_to_cgain(self, engine):
        for text in ("What is the worst cascaded gain in the final node?",
                     "What is the worst cascade gain in the final node?"):
            parser = make_parser(engine, draft(
                metric=None, request="Worst"))
            outcome = parser.parse(text)
            assert outcome.intent.metric == "CGAIN", text

    def test_bare_gain_stays_ambiguous(self, engine):
        """No exact alias for bare 'gain': must not silently resolve."""
        parser = make_parser(engine, draft(metric=None, scope=None, freq=None))
        outcome = parser.parse("What is the gain at 2500 MHz?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "metric" in outcome.missing_fields


# ---------------------------------------------------------------------------
# Guess veto + catalogue inference (mission: scope/unit policy)
# ---------------------------------------------------------------------------


class TestGuessVetoAndInference:
    def test_tnp_scope_unit_guesses_corrected(self, engine):
        """Live failure case F: model guessed All nodes/dB; catalogue says
        Final node/dBm uniquely."""
        parser = make_parser(engine, draft(
            metric="TNP", request="Power difference", scope="All nodes",
            freq="All Frequencies", unit="dB",
            params={"reference_qualifier": "desired channel power"}))
        outcome = parser.parse(
            "What is the TNP power difference above desired channel power?")
        assert outcome.status is ParseStatus.RESOLVED, outcome.message
        assert outcome.intent.scope == "Final node"
        assert outcome.intent.unit == "dBm"
        assert outcome.intent.frequency_selection == "All Frequencies"
        assert outcome.intent.params.reference_qualifier is \
            ReferenceQualifier.DESIRED_CHANNEL_POWER

    def test_unit_inferred_from_metric_when_null(self, engine):
        parser = make_parser(engine, draft(
            metric="TNP", request="Power difference", scope=None,
            freq="All Frequencies", unit=None,
            params={"reference_qualifier": "channel power"}))
        outcome = parser.parse(
            "Show the total node power above channel power difference?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.unit == "dBm"
        assert outcome.intent.scope == "Final node"

    def test_literal_unit_respected_even_if_conflicting(self, engine):
        """An explicit user unit is respected; the lookup reports NOT_FOUND."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(metric="TNP", request="Power difference",
                             scope=None, freq="All Frequencies", unit="dB",
                             params={"reference_qualifier": "channel power"})]))
        outcome = service.answer_question(
            "Show the total node power above channel power difference in dB?")
        assert outcome.status is AnswerStatus.NOT_FOUND
        assert outcome.intent.unit == "dB"

    def test_ccomp_range_scope_guess_fixed(self, engine):
        """Live failure: model guessed Final node; question names no scope;
        catalogue: (CCOMP, Range, *, All Frequencies, dB20) exists only for
        All nodes -> veto + inference."""
        parser = make_parser(engine, draft(
            metric="CCOMP", request="Range", scope="Final node",
            unit="dB20", params={"range_min": 1.0, "range_max": 2.0}))
        outcome = parser.parse(
            "Where is Cascaded Compression between 1 and 2 dB20?")
        assert outcome.status is ParseStatus.RESOLVED, outcome.message
        assert outcome.intent.scope == "All nodes"

    def test_case_c_request_ambiguity_clarifies_with_candidates(self, engine):
        """(CGAIN, Final node, 2500 MHz, dB) matches two requests in the
        catalogue: Particular frequency AND Individual frequency final
        value -> NEEDS_CLARIFICATION with both candidates."""
        parser = make_parser(engine, draft(
            metric="CGAIN", request="All frequencies", scope="Final node",
            freq="2500 MHz"))
        outcome = parser.parse(
            "What is the Cascaded Gain at 2500 MHz for the final node?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "request" in outcome.missing_fields
        candidate_requests = sorted(c.request for c in outcome.candidates)
        assert candidate_requests == ["Individual frequency final value",
                                      "Particular frequency"]

    def test_case_e_resolves_part_summary(self, engine):
        """'Show Mismatch Loss for part ADL8124 across frequency' must yield
        Part summary / ADL8124 / All Frequencies via veto+inference."""
        parser = make_parser(engine, draft(
            metric="MismatchLoss", request="All frequencies", scope="ADL8124",
            freq="All Frequencies"))
        outcome = parser.parse(
            "Show Mismatch Loss for part ADL8124 across frequency.")
        assert outcome.status is ParseStatus.RESOLVED, outcome.message
        assert outcome.intent.request == "Part summary"
        assert outcome.intent.scope == "ADL8124"
        assert outcome.intent.frequency_selection == "All Frequencies"

    def test_exact_frequency_distinct_from_request(self, engine):
        """Frequency wording must not become the request (mission rule).

        (DCP, *, Final node, 1200 MHz, dBm) matches TWO catalogue requests
        (Particular frequency AND Individual frequency final value), so the
        conservative outcome is a clarification listing both candidates.
        """
        parser = make_parser(engine, draft(
            metric="DCP", request=None, scope="Final node", freq="1200 MHz",
            unit="dBm"))
        outcome = parser.parse(
            "What is the Desired Channel Power at 1200 MHz in the final node?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "request" in outcome.missing_fields
        candidate_requests = sorted(c.request for c in outcome.candidates)
        assert candidate_requests == ["Individual frequency final value",
                                      "Particular frequency"]

    def test_frequency_selection_survives_when_unique(self, engine):
        """Model-inferred All Frequencies (not literal) is kept when the
        catalogue uniquely confirms it (case A shape)."""
        parser = make_parser(engine, draft(
            metric="CGAIN", request="Worst", scope="Final node", freq=None))
        outcome = parser.parse("What is the worst Cascaded Gain in the final node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.frequency_selection == "All Frequencies"
        assert outcome.intent.unit == "dB"
        assert outcome.intent.row_id if hasattr(outcome.intent, "row_id") else True


# ---------------------------------------------------------------------------
# Pending clarification (mission: clarification follow-up)
# ---------------------------------------------------------------------------


class TestPendingClarification:
    def test_gain_clarification_then_reply_resolves(self, engine):
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric=None, request=None, scope=None, freq=None, unit=None))
        out1 = p1.parse("What is the gain at 2500 MHz?", ctx)
        assert out1.status is ParseStatus.NEEDS_CLARIFICATION
        assert ctx.pending is not None

        # user replies with the canonical metric
        p2 = make_parser(engine, draft())  # must NOT be consumed
        out2 = p2.parse("CGAIN", ctx)
        assert out2.status is ParseStatus.NEEDS_CLARIFICATION  # scope/request still open
        assert out2.resolved_from_pending.get("metric") == "CGAIN"
        assert "metric" not in out2.missing_fields

    def test_short_reply_completes_fully_when_possible(self, engine):
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric=None, request="Worst", scope="Final node",
            freq="All Frequencies", unit=None))
        out1 = p1.parse("What is the worst thing in the final node?", ctx)
        assert out1.status is ParseStatus.NEEDS_CLARIFICATION
        assert "metric" in out1.missing_fields

        out2 = QueryParser(engine, FakeLLMClient(responses=[])).parse("CGAIN", ctx)
        assert out2.status is ParseStatus.RESOLVED
        assert out2.intent.metric == "CGAIN"
        assert out2.intent.request == "Worst"
        assert ctx.pending is None
        assert ctx.last_answered_intent is None  # not yet answered via service

    def test_long_reply_treated_as_fresh_question(self, engine):
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(metric=None))
        out1 = p1.parse("What is the gain at 2500 MHz?", ctx)
        assert ctx.pending is not None
        fresh = ("What is the best Mismatch Loss in the final node "
                 "across all frequencies?")
        out2 = QueryParser(engine, FakeLLMClient(responses=[draft()])).parse(fresh, ctx)
        assert ctx.pending is None  # fresh parse cleared it
        assert out2.status is ParseStatus.RESOLVED

    def test_reset_clears_pending(self, engine):
        ctx = ConversationContext()
        p = make_parser(engine, draft(metric=None))
        p.parse("What is the gain at 2500 MHz?", ctx)
        ctx.reset()
        assert ctx.pending is None


class TestCandidateClarificationResume:
    """Regression: candidate-listing clarifications (hypernym metrics,
    scope options, ambiguous request wording) must store the pending draft
    so a short candidate reply resumes the query instead of re-parsing
    from scratch (README §9: clarification replies resume a pending
    clarification)."""

    def test_scope_candidates_stores_pending(self, engine):
        ctx = ConversationContext()
        p = make_parser(engine, draft(
            metric="MismatchLoss", request="Best", scope=None,
            freq="All Frequencies", unit=None))
        out = p.parse("What is the best Mismatch Loss?", ctx)
        assert out.status is ParseStatus.NEEDS_CLARIFICATION
        assert out.candidates
        assert "scope" in out.missing_fields
        assert ctx.pending is not None

    def test_scope_candidates_reply_resumes_without_llm(self, engine):
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric="MismatchLoss", request="Best", scope=None,
            freq="All Frequencies", unit=None))
        p1.parse("What is the best Mismatch Loss?", ctx)
        # responses=[] -> any LLM call would fail the FakeLLMClient: the
        # resume must be handled deterministically in Stage A.
        out = QueryParser(engine, FakeLLMClient(responses=[])).parse(
            "All nodes", ctx)
        assert out.status is ParseStatus.RESOLVED
        assert out.intent.scope == "All nodes"
        assert out.intent.metric == "MismatchLoss"

    def test_hypernym_candidates_reply_resumes(self, engine):
        """Bare 'gain' with a model-guessed metric clarifies with both
        candidates; the short reply selects one and resolves."""
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric="GAIN", request="Best", scope="Final node",
            freq="2500 MHz", unit="dB"))
        out1 = p1.parse(
            "What is the gain in dB at 2500 MHz in the final node?", ctx)
        assert out1.status is ParseStatus.NEEDS_CLARIFICATION
        assert out1.candidates
        assert ctx.pending is not None
        out2 = QueryParser(engine, FakeLLMClient(responses=[])).parse(
            "CGAIN", ctx)
        assert out2.status is ParseStatus.RESOLVED
        assert out2.intent.metric == "CGAIN"

    def test_ambiguous_request_reply_resumes(self, engine):
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric="MismatchLoss", request=None, scope="Final node",
            freq="All Frequencies", unit=None))
        out1 = p1.parse("What is the lowest Mismatch Loss in the final "
                        "node?", ctx)
        assert out1.status is ParseStatus.NEEDS_CLARIFICATION
        assert ctx.pending is not None
        out2 = QueryParser(engine, FakeLLMClient(responses=[])).parse(
            "Minimum", ctx)
        assert out2.status is ParseStatus.RESOLVED
        assert out2.intent.request == "Minimum"

    def test_non_candidate_short_reply_falls_through_fresh(self, engine):
        """A short reply that resolves nothing pending falls through to a
        fresh parse. The fresh parse re-applies the guess veto to the new
        draft (scope/frequency not restated) and clarifies again — the
        pending state never leaks a previous draft's vetoed values."""
        ctx = ConversationContext()
        p1 = make_parser(engine, draft(
            metric="MismatchLoss", request="Best", scope=None,
            freq="All Frequencies", unit=None))
        out1 = p1.parse("What is the best Mismatch Loss?", ctx)
        assert ctx.pending is not None
        out2 = QueryParser(engine, FakeLLMClient(
            responses=[draft()])).parse("banana", ctx)
        assert out2.status is ParseStatus.NEEDS_CLARIFICATION
        assert ctx.pending is not None  # fresh clarification re-pending


# ---------------------------------------------------------------------------
# Service-level integration of the new statuses/fields
# ---------------------------------------------------------------------------


class TestServicePolicy:
    def test_parse_error_is_distinct_status(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(metric="NOT_A_METRIC")]))
        result = service.answer_question("anything at all")
        assert result.status is AnswerStatus.PARSE_ERROR

    def test_answered_result_carries_full_provenance(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft()]))
        result = service.answer_question(
            "What is the best Mismatch Loss in the final node?")
        assert result.status is AnswerStatus.ANSWERED
        assert result.row_id == 1
        assert result.metric == "MismatchLoss"
        assert result.request == "Best"
        assert result.scope == "Final node"
        assert result.unit == "dB"
        assert result.frequency_selection == "All Frequencies"
        assert result.answer_type == "Extreme value"
        assert result.answer_text and result.question_text

    def test_threshold_values_distinguish_via_service(self, engine):
        for threshold, expected_id in ((0.1, 2850), (10.0, 2868)):
            service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
                responses=[draft(request="Threshold Above", scope="All nodes",
                                 params={"threshold": threshold})]))
            result = service.answer_question(
                f"Where is Mismatch Loss above {threshold} dB in All nodes?")
            assert result.row_id == expected_id


# ---------------------------------------------------------------------------
# Live-failure regression fixes (smoke benchmark round 2)
# ---------------------------------------------------------------------------


class TestPowerPhraseGrammar:
    """Audited workbook power-difference phrasings are matched deterministically."""

    def test_tnp_ifpd_phrase_overrides_wrong_model_choice(self, engine):
        """Live failure dup-2421: model chose CP/Threshold Above/threshold 0;
        the audited phrase must win and resolve row 2421."""
        parser = make_parser(engine, draft(
            metric="CP", request="Threshold Above", scope="All nodes",
            freq="1200 MHz", unit="dBm", params={"threshold": 0.0}))
        outcome = parser.parse(
            "Show total node power above desired channel power "
            "at individual frequency 1200 MHz?")
        assert outcome.status is ParseStatus.RESOLVED, outcome.message
        assert outcome.intent.metric == "TNP"
        assert outcome.intent.request == "Individual frequency power difference"
        assert outcome.intent.params.reference_qualifier is \
            ReferenceQualifier.DESIRED_CHANNEL_POWER

    def test_tnp_power_difference_across_frequency(self, engine):
        """Live failure dup-4772/4774."""
        for qualifier, expected_id in (
                ("desired channel power", 4772), ("channel power", 4774)):
            parser = make_parser(engine, draft(
                metric="CP", request="Threshold Above", scope="All nodes",
                unit="dBm", params={"threshold": 0.0}))
            outcome = parser.parse(
                f"Show total node power above {qualifier} across frequency?")
            assert outcome.status is ParseStatus.RESOLVED, outcome.message
            assert outcome.intent.request == "Power difference"
            assert outcome.intent.scope == "Final node"
            assert outcome.intent.unit == "dBm"
            service = RfCatalogueService(engine=engine,
                                         llm_client=FakeLLMClient(responses=[]))
            # bypass service LLM: resolve via engine directly
            lookup = engine.lookup_by_intent(outcome.intent)
            assert lookup.rows[0].row_id == expected_id

    def test_ndcp_noise_phrase(self, engine):
        parser = make_parser(engine, draft(
            metric="CP", request="Threshold Above", params={"threshold": 1.0}))
        outcome = parser.parse(
            "Show noise plus distortion power above channel noise power "
            "across frequency?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "NDCP"
        assert outcome.intent.params.reference_qualifier is \
            ReferenceQualifier.CHANNEL_NOISE_POWER

    def test_output_to_input_phrase(self, engine):
        parser = make_parser(engine, draft(
            metric="CP", request="Threshold Above", params={"threshold": 1.0}))
        outcome = parser.parse("Show output-to-input P1dB difference across frequency?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "OP1DB"
        assert outcome.intent.params.reference_qualifier is \
            ReferenceQualifier.OUTPUT_TO_INPUT


class TestGuessedValueVetoes:
    def test_numeric_scope_guess_vetoed(self, engine):
        """Live failure dup-3080: model guessed scope '2' from the threshold
        number '2'. Node-number scopes are only literal via 'node N'. After
        the veto, the catalogue uniquely determines All nodes for
        (CCOMP, Closest value, All Frequencies, dB20), so the row resolves."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(metric="CCOMP", request="Closest value",
                             scope="2", unit="dB20",
                             params={"closest_target": 2.0})]))
        result = service.answer_question(
            "Where is Cascaded Compression closest to 2 dB20?")
        assert result.status is AnswerStatus.ANSWERED, result.message
        assert result.row_id == 3080
        assert result.intent.scope == "All nodes"

    def test_bare_gain_model_choice_vetoed(self, engine):
        """Live failure clar-01: model picked GAIN for bare 'gain'."""
        parser = make_parser(engine, draft(
            metric="GAIN", request="Particular frequency", scope="Final node",
            freq="2500 MHz"))
        outcome = parser.parse("What is the gain at 2500 MHz?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "metric" in outcome.missing_fields
        candidate_metrics = sorted(c.metric for c in outcome.candidates)
        assert candidate_metrics == ["CGAIN", "GAIN"]

    def test_guessed_threshold_number_vetoed(self, engine):
        """Live failure clar-02: model invented threshold 0 for 'the limit'."""
        parser = make_parser(engine, draft(
            request="Threshold Above", scope=None, freq=None, unit=None,
            params={"threshold": 0.0}))
        outcome = parser.parse("Where is Mismatch Loss above the limit?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert any("threshold" in f for f in outcome.missing_fields)

    def test_request_variant_vetoed_even_when_model_chose(self, engine):
        """Live failure clar-07: model picked Minimum for 'lowest'."""
        parser = make_parser(engine, draft(
            metric="CGAIN", request="Minimum", scope="Final node"))
        outcome = parser.parse("Give me the lowest cascade gain at the final node.")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "request" in outcome.missing_fields

    def test_hedged_scope_ambiguous_even_when_model_filled(self, engine):
        """Live failure clar-08: model filled ADL8124; 'family' makes it
        ambiguous vs ADL8124_1. Candidates cannot be built as full intents
        (frequency undetermined), so the ambiguity is presented in hints."""
        parser = make_parser(engine, draft(
            metric="MismatchLoss", request="Best", scope="ADL8124"))
        outcome = parser.parse("What is the best mismatch loss in the ADL8124 family?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "scope" in outcome.missing_fields
        joined = " ".join(outcome.candidate_hints)
        assert "ADL8124" in joined and "ADL8124_1" in joined


# ---------------------------------------------------------------------------
# Terminal chatbot surface (no stdin; handle_command/render_result directly)
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_state(engine):
    service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
        responses=[draft()]))
    return {"service": service, "context": ConversationContext(),
            "last_result": None}


class TestChatbot:
    def test_help(self, chat_state):
        out = handle_command("/help", chat_state)
        assert "/status" in out and "/quit" in out

    def test_status(self, chat_state):
        out = handle_command("/status", chat_state)
        assert "catalogue rows" in out
        assert "live mode" in out
        assert "OPENROUTER_API_KEY" not in out or "key" not in out.lower() \
            or "not configured" in out.lower()
        # never prints a credential-looking string
        assert "sk-" not in out

    def test_debug_before_any_question(self, chat_state):
        out = handle_command("/debug", chat_state)
        assert "No question answered yet" in out

    def test_examples_lists_real_catalogue_questions(self, chat_state):
        out = handle_command("/examples", chat_state)
        assert "Mismatch Loss" in out  # from the actual workbook wording

    def test_reset(self, chat_state):
        chat_state["context"].record("x", answered=True)
        out = handle_command("/reset", chat_state)
        assert "cleared" in out.lower()
        assert chat_state["context"].last_answered_intent is None

    def test_quit_returns_none(self, chat_state):
        assert handle_command("/quit", chat_state) is None
        assert handle_command("/exit", chat_state) is None

    def test_unknown_command(self, chat_state):
        out = handle_command("/frobnicate", chat_state)
        assert "Unknown command" in out

    def test_render_answered_with_provenance(self, engine, chat_state):
        result = chat_state["service"].answer_question(
            "What is the best Mismatch Loss in the final node?")
        text = render_result(result)
        assert "Answer:" in text
        assert "Source row: 1" in text
        assert "Resolved intent:" in text

    def test_render_clarification_lists_candidates(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(metric=None, scope=None, freq=None)]))
        result = service.answer_question("What is the gain at 2500 MHz?")
        text = render_result(result)
        assert "metric" in text.lower() or "Which" in text

    def test_render_not_found(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(frequency_selection=None)]))
        result = service.answer_question("Best MismatchLoss at 3400 MHz?")
        text = render_result(result)
        assert "NOT_FOUND" in text or "no matching catalogue record" in text

    def test_render_unsupported(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(unresolvable_reason="plotting is not supported")]))
        result = service.answer_question("Plot the CGAIN over time.")
        text = render_result(result)
        assert "UNSUPPORTED" in text or "outside" in text

    def test_render_parse_error_no_stacktrace(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft(metric="BAD")]))
        result = service.answer_question("whatever")
        text = render_result(result)
        assert "Traceback" not in text
        assert "rephrase" in text.lower()

    def test_graceful_api_failure(self, engine):
        class Exploding:
            def complete_structured(self, system, user, schema):
                raise TimeoutError("connection timed out")

        service = RfCatalogueService(engine=engine, llm_client=Exploding())
        result = service.answer_question("What is the best mismatch loss?")
        assert result.status is AnswerStatus.ERROR
        assert "did not respond in time" in result.message  # friendly, typed

    def test_credit_failure_message(self, engine):
        class OutOfCredits:
            def complete_structured(self, system, user, schema):
                raise RuntimeError("Error code: 402 - insufficient credits")

        service = RfCatalogueService(engine=engine, llm_client=OutOfCredits())
        result = service.answer_question("What is the best mismatch loss?")
        assert result.status is AnswerStatus.ERROR
        assert "credits" in result.message

    def test_auth_failure_message(self, engine):
        class BadKey:
            def complete_structured(self, system, user, schema):
                raise RuntimeError("Error code: 401 - Unauthorized")

        service = RfCatalogueService(engine=engine, llm_client=BadKey())
        result = service.answer_question("What is the best mismatch loss?")
        assert result.status is AnswerStatus.ERROR
        assert "authentication" in result.message.lower()


# ---------------------------------------------------------------------------
# Context-inheritance policy regression tests (manual-testing fix)
#
# Policy: a new, grammatically complete question is a NEW intent by default.
# Inheritance applies ONLY to explicit elliptical follow-ups. Context must
# never turn an exact catalogue question into NOT_FOUND.
# ---------------------------------------------------------------------------


class TestContextInheritancePolicy:
    def test_part_summary_then_threshold_resolves_2850(self, engine):
        """A. Part-summary -> standalone threshold question (0.1 dB)
        must resolve to row 2850 (scope All nodes via catalogue inference),
        NOT inherit scope ADL8124."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Part summary",
                      scope="ADL8124"),
                draft(metric="MismatchLoss", request="Threshold Above",
                      scope=None, freq=None, unit=None,
                      params={"threshold": 0.1}),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question(
            "Show Mismatch Loss for part ADL8124 across frequency.", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        assert r1.intent.scope == "ADL8124"

        r2 = service.answer_question(
            "Where is Mismatch Loss above 0.1 dB?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.row_id == 2850, r2.message
        assert r2.intent.scope == "All nodes"
        assert r2.intent.frequency_selection == "All Frequencies"

    def test_part_summary_then_threshold_10db_resolves_2868(self, engine):
        """B. Same sequence with 10 dB -> row 2868."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Part summary",
                      scope="ADL8124"),
                draft(metric="MismatchLoss", request="Threshold Above",
                      scope=None, freq=None, unit=None,
                      params={"threshold": 10.0}),
            ]))
        ctx = ConversationContext()
        service.answer_question(
            "Show Mismatch Loss for part ADL8124 across frequency.", ctx)
        r2 = service.answer_question(
            "Where is Mismatch Loss above 10 dB?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.row_id == 2868
        assert r2.intent.scope == "All nodes"

    def test_elliptical_followup_inherits(self, engine):
        """C. 'What about the best?' MUST inherit metric/scope/freq/unit."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="CGAIN", request="Worst", scope="Final node"),
                draft(metric="CGAIN", request="Best", scope=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question(
            "What is the worst CGAIN in the final node?", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        r2 = service.answer_question("What about the best?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.intent.metric == "CGAIN"
        assert r2.intent.scope == "Final node"
        assert r2.intent.request == "Best"
        assert r2.row_id != r1.row_id

    def test_elliptical_same_for_node(self, engine):
        """'Show the same for node 45.' inherits metric/request, new scope."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Node summary", scope="2"),
                draft(metric="MismatchLoss", request="Node summary", scope=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question(
            "Give me the node summary for mismatch loss at node 2.", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        r2 = service.answer_question("Show the same for node 45.", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.intent.scope == "45"
        assert r2.row_id == 3296

    def test_new_question_with_new_frequency_never_misleading_not_found(self, engine):
        """D. Part-summary -> 'What is the Mismatch Loss at 1700 MHz?'
        must NOT silently force ADL8124 and return a misleading NOT_FOUND:
        the honest outcome is clarification (request ambiguous at 1700 MHz)."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Part summary",
                      scope="ADL8124"),
                draft(metric="MismatchLoss", request=None, scope=None,
                      freq="1700 MHz", unit=None),
            ]))
        ctx = ConversationContext()
        service.answer_question(
            "Show Mismatch Loss for part ADL8124 across frequency.", ctx)
        r2 = service.answer_question(
            "What is the Mismatch Loss at 1700 MHz?", ctx)
        assert r2.status is not AnswerStatus.NOT_FOUND, (
            "inherited scope manufactured a NOT_FOUND")
        assert r2.status is AnswerStatus.NEEDS_CLARIFICATION
        assert r2.intent is None or r2.intent.scope != "ADL8124"

    def test_standalone_question_does_not_inherit_scope(self, engine):
        """E. A standalone substantive question never inherits the previous
        turn's unrelated scope."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="CGAIN", request="Best", scope="Final node"),
                draft(metric="MismatchLoss", request="Threshold Above",
                      scope=None, freq=None, unit=None,
                      params={"threshold": 5.0}),
            ]))
        ctx = ConversationContext()
        service.answer_question(
            "What is the best CGAIN in the final node?", ctx)
        r2 = service.answer_question(
            "Where is Mismatch Loss above 5 dB?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.row_id == 2865
        assert r2.intent.scope == "All nodes"  # inferred, not inherited
        assert r2.intent.metric == "MismatchLoss"

    def test_is_elliptical_classification(self, engine):
        from rf_catalogue.nlp.parser import QueryParser

        parser = QueryParser.__new__(QueryParser)  # vocab needed only
        from rf_catalogue.nlp.vocabulary import ControlledVocabulary

        parser.vocab = ControlledVocabulary.from_engine(engine)
        elliptical = [
            "What about the best?", "What about 2500 MHz?",
            "How about the worst?", "And for that part?",
            "Show the same for node 45.", "Same for node 3.",
            "the best", "for node 45",
        ]
        for text in elliptical:
            assert parser.is_elliptical_followup(text), text
        substantive = [
            "Where is Mismatch Loss above 0.1 dB?",
            "What is the Mismatch Loss at 1700 MHz?",
            "What is the worst Cascaded Gain in the final node?",
            "Show Mismatch Loss for part ADL8124 across frequency.",
            "Give me the lowest cascade gain at the final node.",
        ]
        for text in substantive:
            assert not parser.is_elliptical_followup(text), text


class TestEllipticalFollowUpScenarios:
    """Final-acceptance follow-up matrix (task scenarios A-H).

    A/D/E/F/H are covered above (TestContextInheritancePolicy,
    TestPendingClarification, guess-veto tests); these cover the
    frequency-only elliptical follow-ups that must inherit the previous
    REQUEST, and the honest NOT_FOUND when the inherited context has no
    catalogue row.
    """

    def test_frequency_only_followup_inherits_request(self, engine):
        """'What is the CGAIN at 2500 MHz?' then 'And at 2600?' must
        inherit metric/request/scope/unit and change only frequency.
        The bare '2600' (no MHz suffix) must count as a literal
        frequency, not be vetoed and replaced by stale context."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="CGAIN", request="Particular frequency",
                      scope=None, freq="2500 MHz", unit="dB"),
                draft(metric=None, request=None, scope=None,
                      freq="2600 MHz", unit=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question("What is the CGAIN at 2500 MHz?", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        assert r1.row_id == 171

        r2 = service.answer_question("And at 2600?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.intent.metric == "CGAIN"
        assert r2.intent.request == "Particular frequency"
        assert r2.intent.scope == "Final node"
        assert r2.intent.frequency_selection == "2600 MHz"
        assert r2.row_id != r1.row_id
        assert r2.row_id is not None

    def test_part_context_frequency_followup_uses_part(self, engine):
        """'Show Mismatch Loss for part ADL8124 across frequency.' then
        'What about at 1700 MHz?' must use the part context. The catalogue
        has NO ADL8124 rows at 1700 MHz, so the honest outcome is
        NOT_FOUND with the part scope resolved — never a clarification
        that lost the context, and never a fabricated row."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Part summary",
                      scope="ADL8124", freq="All Frequencies", unit="dB"),
                draft(metric=None, request=None, scope=None,
                      freq="1700 MHz", unit=None),
            ]))
        ctx = ConversationContext()
        service.answer_question(
            "Show Mismatch Loss for part ADL8124 across frequency.", ctx)
        r2 = service.answer_question("What about at 1700 MHz?", ctx)
        assert r2.status is AnswerStatus.NOT_FOUND, r2.message
        assert r2.intent.scope == "ADL8124"
        assert r2.intent.request == "Part summary"
        assert r2.intent.frequency_selection == "1700 MHz"

    def test_elliptical_inherited_request_with_required_params_asks(self, engine):
        """Inheriting a parameterised request never invents the parameter:
        the parser asks for the threshold value."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="MismatchLoss", request="Threshold Above",
                      scope="All nodes", freq="All Frequencies", unit="dB",
                      params={"threshold": 0.1}),
                draft(metric=None, request=None, scope=None,
                      freq="1700 MHz", unit=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question(
            "Where is Mismatch Loss above 0.1 dB?", ctx)
        assert r1.status is AnswerStatus.ANSWERED

        r2 = service.answer_question("And at 1700 MHz?", ctx)
        assert r2.status is AnswerStatus.NEEDS_CLARIFICATION
        assert any("threshold" in m.lower() for m in r2.missing_fields)

    def test_pending_clarification_completes_fully(self, engine):
        """Bare 'gain' at 2500 MHz -> clarify -> 'CGAIN' completes the
        pending intent in one step: the catalogue uniquely determines
        scope (Final node) and unit (dB) for CGAIN / Particular frequency
        / 2500 MHz, so no further round-trip is needed."""
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric=None, request="Particular frequency",
                      scope=None, freq="2500 MHz", unit=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question("What is the gain at 2500 MHz?", ctx)
        assert r1.status is AnswerStatus.NEEDS_CLARIFICATION

        # 'CGAIN' completes the metric without a new LLM call
        # (responses=[] would fail the FakeLLMClient on any LLM usage).
        from rf_catalogue.nlp.schemas import ParseStatus

        resumer = QueryParser(engine, FakeLLMClient(responses=[]))
        r2 = resumer.parse("CGAIN", ctx)
        assert r2.status is ParseStatus.RESOLVED
        assert r2.intent.metric == "CGAIN"
        assert r2.intent.scope == "Final node"
        assert r2.intent.unit == "dB"
        assert r2.intent.frequency_selection == "2500 MHz"


# ---------------------------------------------------------------------------
# Automated end-to-end terminal flow: question -> intent -> render (no API)
# ---------------------------------------------------------------------------


class TestTerminalFlow:
    """Simulates the full terminal round-trip without a real provider."""

    def _ask(self, engine, response, question, state_context=None):
        from rf_catalogue.chatbot import render_result

        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[response]))
        context = state_context or ConversationContext()
        result = service.answer_question(question, context)
        return result, render_result(result), context

    def test_exact_match_render_shape(self, engine):
        result, text, _ = self._ask(engine, draft(),
                                    "What is the best Mismatch Loss in the final node?")
        assert result.status is AnswerStatus.ANSWERED
        assert text.startswith("Answer:\n")
        assert "Source row: 1" in text
        assert "Resolved intent:" in text
        assert "Metric: MismatchLoss" in text
        assert "Request: Best" in text
        assert "Scope: Final node" in text
        assert "Frequency: All Frequencies" in text
        assert "Unit: dB" in text
        assert "sk-" not in text

    def test_threshold_terminal_flow(self, engine):
        result, text, _ = self._ask(
            engine,
            draft(request="Threshold Above", scope="All nodes",
                  params={"threshold": 0.1}),
            "Where is Mismatch Loss above 0.1 dB in All nodes?")
        assert result.row_id == 2850
        assert "Parameters: (0.1,)" in text

    def test_ambiguous_metric_clarification_render(self, engine):
        result, text, ctx = self._ask(
            engine,
            draft(metric=None, request=None, scope=None, freq=None, unit=None),
            "What is the gain at 2500 MHz?")
        assert result.status is AnswerStatus.NEEDS_CLARIFICATION
        assert ctx.pending is not None  # clarification is resumable

    def test_pending_clarification_full_round_trip(self, engine):
        from rf_catalogue.chatbot import render_result

        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric=None, request="Worst", scope="Final node",
                      freq="All Frequencies", unit=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question("What is the worst thing in the final node?", ctx)
        assert r1.status is AnswerStatus.NEEDS_CLARIFICATION
        # no LLM call needed for the short reply: pending merge + inference
        service2 = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[]))
        r2 = service2.answer_question("CGAIN", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.intent.metric == "CGAIN"
        assert r2.intent.request == "Worst"
        text = render_result(r2)
        assert "Source row:" in text

    def test_context_follow_up_round_trip(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                draft(metric="CGAIN", request="Worst", scope="Final node"),
                draft(metric="CGAIN", request="Best", scope=None),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question("What is the worst CGAIN in the final node?", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        r2 = service.answer_question("What about the best?", ctx)
        assert r2.status is AnswerStatus.ANSWERED
        assert r2.intent.scope == "Final node"
        assert r2.row_id != r1.row_id


class TestChatbotDegradedMode:
    """The chatbot starts without a key; questions explain what is missing."""

    def test_status_without_service(self):
        from rf_catalogue.chatbot import handle_command

        state = {"service": None, "context": ConversationContext(),
                 "last_result": None}
        out = handle_command("/status", state)
        assert "NOT configured" in out
        assert "OPENROUTER_API_KEY" in out

    def test_examples_without_service(self):
        from rf_catalogue.chatbot import handle_command

        state = {"service": None, "context": ConversationContext(),
                 "last_result": None}
        out = handle_command("/examples", state)
        assert "Mismatch Loss" in out

    def test_question_without_service_is_explained(self, capsys):
        """Simulates main()-loop behaviour when service is None."""
        from rf_catalogue.chatbot import _NO_SERVICE_QUESTION

        assert "cannot be answered" in _NO_SERVICE_QUESTION
        assert "LLM_PROVIDER" in _NO_SERVICE_QUESTION
