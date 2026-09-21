"""Offline tests for the NLP layer (no network, FakeLLMClient only).

Covers: controlled vocabulary grounding, Stage-A normalizer, draft JSON
schema, conversation context, parser clarification states, service flow,
and end-to-end resolution with scripted LLM outputs.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rf_catalogue.engine import CatalogueEngine
from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.nlp.llm_client import FakeLLMClient, StructuredOutputError
from rf_catalogue.nlp.llm_schema import build_draft_schema
from rf_catalogue.nlp.normalizer import Normalizer
from rf_catalogue.nlp.parser import QueryParser
from rf_catalogue.nlp.schemas import ParseStatus, QueryIntentDraft
from rf_catalogue.nlp.vocabulary import (
    METRIC_ALIASES,
    REQUEST_ALIASES,
    ControlledVocabulary,
)
from rf_catalogue.query_intent import ReferenceQualifier, ThresholdParams
from rf_catalogue.service import AnswerStatus, RfCatalogueService


# ---------------------------------------------------------------------------
# Fixtures (session-scoped engine from conftest; parser with scripted LLM)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vocab(engine):
    return ControlledVocabulary.from_engine(engine)


@pytest.fixture(scope="module")
def normalizer(vocab):
    return Normalizer(vocab)


def make_parser(engine, response: dict) -> QueryParser:
    return QueryParser(engine, FakeLLMClient(responses=[response]))


def draft_response(**overrides) -> dict:
    base = {
        "metric": "MismatchLoss",
        "request": "Best",
        "scope": "Final node",
        "frequency_selection": "All Frequencies",
        "unit": "dB",
        "params": {"kind": "none"},
        "unresolvable_reason": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Controlled vocabulary is grounded in the audited catalogue
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_vocab_built_from_catalogue(self, engine, vocab):
        assert vocab.metrics == frozenset(engine.metrics())
        assert vocab.requests == frozenset(engine.requests())
        assert vocab.scopes == frozenset(engine.scopes())
        assert vocab.units == frozenset(engine.units())
        assert vocab.frequency_selections == frozenset(engine.freq_selections())

    def test_no_invented_metrics(self, engine, vocab):
        # every alias target must exist in the catalogue
        for canonical in METRIC_ALIASES.values():
            assert canonical in engine.metrics(), canonical

    def test_no_invented_requests(self, engine, vocab):
        for canonical in REQUEST_ALIASES.values():
            assert canonical in engine.requests(), canonical

    def test_alias_resolution(self, vocab):
        assert vocab.resolve_metric("cascaded gain") == "CGAIN"
        assert vocab.resolve_metric("cascade gain") == "CGAIN"
        assert vocab.resolve_metric("Cascaded Gain") == "CGAIN"
        # canonical request words resolve directly...
        assert vocab.resolve_request("worst") == "Worst"
        assert vocab.resolve_request("minimum") == "Minimum"
        # ...but natural-language variants are deliberately NOT mapped
        assert vocab.resolve_request("lowest") is None
        assert vocab.resolve_request("highest") is None
        assert vocab.resolve_unit("dbm") == "dBm"
        assert vocab.resolve_frequency("all frequencies") == "All Frequencies"
        assert vocab.resolve_reference_qualifier("desired channel power") == \
            "desired channel power"

    def test_request_variants_under_review_documented(self):
        from rf_catalogue.nlp.vocabulary import REQUEST_VARIANTS_UNDER_REVIEW
        variants = {item["variant"] for item in REQUEST_VARIANTS_UNDER_REVIEW}
        assert {"lowest / smallest", "highest / largest"} <= variants
        # and the ambiguous alias table only contains plausible canonical targets
        from rf_catalogue.nlp.vocabulary import AMBIGUOUS_REQUEST_ALIASES
        assert AMBIGUOUS_REQUEST_ALIASES["lowest"] == ("Minimum", "Worst")
        assert AMBIGUOUS_REQUEST_ALIASES["highest"] == ("Maximum", "Best")

    def test_reference_qualifiers_controlled(self, vocab):
        assert vocab.reference_qualifiers == frozenset(
            q.value for q in ReferenceQualifier
            if q is not ReferenceQualifier.NONE
        )

    def test_validate_dimension(self, vocab):
        assert vocab.validate_dimension("metric", "CGAIN")
        assert not vocab.validate_dimension("metric", "RETURNLOSS")
        with pytest.raises(ValueError):
            vocab.validate_dimension("nonsense", "x")


# ---------------------------------------------------------------------------
# Stage A normalizer
# ---------------------------------------------------------------------------


class TestNormalizer:
    def test_ghz_to_mhz(self, normalizer):
        assert normalizer.normalize("gain at 2.5 GHz?") == "gain at 2500 MHz?"
        assert normalizer.normalize("at 1.2ghz") == "at 1200 MHz"

    def test_whitespace(self, normalizer):
        assert normalizer.normalize("  what   is the best   gain? ") == \
            "what is the best gain?"

    def test_unit_casing(self, normalizer):
        assert "dBm" in normalizer.normalize("power in dbm")
        assert "dB20" in normalizer.normalize("compression in db20")

    def test_scope_casing(self, normalizer):
        assert "Final node" in normalizer.normalize("in the final node")
        assert "All nodes" in normalizer.normalize("across all nodes")


# ---------------------------------------------------------------------------
# Draft schema constrains the LLM to catalogue vocabulary
# ---------------------------------------------------------------------------


class TestDraftSchema:
    def test_schema_enums_from_vocabulary(self, vocab):
        schema = build_draft_schema(vocab)
        props = schema["properties"]
        # "" is the cross-provider "unknown" marker (no JSON nulls allowed)
        assert props["metric"]["enum"] == sorted(vocab.metrics) + [""]
        assert props["request"]["enum"] == sorted(vocab.requests) + [""]
        assert props["scope"]["enum"] == sorted(vocab.scopes) + [""]
        assert props["unit"]["enum"] == sorted(vocab.units) + [""]
        assert props["frequency_selection"]["enum"] == \
            sorted(vocab.frequency_selections) + [""]

    def test_qualifier_enum_in_schema(self, vocab):
        schema = build_draft_schema(vocab)
        # params: anyOf[ ...param objects... ] (no null variant)
        param_objects = schema["properties"]["params"]["anyOf"]
        qual = [p for p in param_objects
                if "reference_qualifier" in p.get("properties", {})]
        assert qual and set(qual[0]["properties"]["reference_qualifier"]["enum"]) == \
            set(vocab.reference_qualifiers)

    def test_no_null_variants_in_schema(self, vocab):
        """Groq rejects JSON-null schema variants; the schema must not use
        null types — empty string is the 'unknown' marker instead."""
        schema_text = json.dumps(build_draft_schema(vocab))
        assert '"type": "null"' not in schema_text
        assert '"type":["string","null"]' not in schema_text.replace(" ", "")
        assert '"type": ["string", "null"]' not in schema_text

    def test_params_schema_uses_anyof_strict_compat(self, vocab):
        """Regression: oneOf is rejected by OpenAI strict structured output
        (HTTP 400); the params schema must use anyOf."""
        schema = build_draft_schema(vocab)
        props = schema["properties"]["params"]
        assert "oneOf" not in props and "anyOf" in props
        for branch in props["anyOf"]:
            if branch.get("type") == "object":
                assert branch.get("additionalProperties") is False
                assert "required" in branch

    def test_schema_is_valid_json(self, vocab):
        assert isinstance(json.dumps(build_draft_schema(vocab)), str)


# ---------------------------------------------------------------------------
# Parser: structured output, validation failure, clarification, NOT_FOUND
# ---------------------------------------------------------------------------


class TestParser:
    def test_resolved_intent(self, engine):
        parser = make_parser(engine, draft_response(
            request="Worst", params={"kind": "none"}))
        outcome = parser.parse("What is the worst mismatch loss in the final node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.metric == "MismatchLoss"
        assert outcome.intent.request == "Worst"

    def test_invalid_structured_output_fails(self, engine):
        # dimension value not in the enum -> draft validation must fail
        parser = make_parser(engine, draft_response(metric="MADEUPMETRIC"))
        outcome = parser.parse("some question")
        assert outcome.status is ParseStatus.PARSE_ERROR
        assert outcome.intent is None

    def test_extra_fields_fail_validation(self, engine):
        bad = draft_response()
        bad["sneaky_extra"] = "value"
        parser = make_parser(engine, bad)
        outcome = parser.parse("some question")
        assert outcome.status is ParseStatus.PARSE_ERROR

    def test_unresolvable_reason_is_unsupported(self, engine):
        # Request TYPE outside catalogue capabilities -> UNSUPPORTED,
        # never NOT_FOUND and never clarification.
        parser = make_parser(engine, draft_response(
            unresolvable_reason="plotting graphs is not a catalogue capability"))
        outcome = parser.parse("Plot the CGAIN over time for the final node.")
        assert outcome.status is ParseStatus.UNSUPPORTED

    def test_deterministic_unsupported_frequency_not_found(self, engine):
        parser = make_parser(engine, draft_response(frequency_selection=None))
        outcome = parser.parse("What is the best CGAIN at 3400 MHz?")
        assert outcome.status is ParseStatus.NOT_FOUND
        assert "3400 MHz" in outcome.message

    # -- NOT_FOUND regression tests (review policy 1) ------------------------

    def test_unknown_part_not_found(self, engine):
        client = FakeLLMClient(responses=[])  # must never be called
        parser = QueryParser(engine, client)
        outcome = parser.parse(
            "What is the best mismatch loss for part FOO123?")
        assert outcome.status is ParseStatus.NOT_FOUND
        assert "FOO123" in outcome.message
        assert client.calls == []  # deterministic detection, pre-LLM

    def test_unknown_node_not_found(self, engine):
        parser = QueryParser(engine, FakeLLMClient(responses=[]))
        outcome = parser.parse("What is the best CGAIN in node 99?")
        assert outcome.status is ParseStatus.NOT_FOUND
        assert "99" in outcome.message

    def test_unknown_metric_not_found(self, engine):
        # LLM reports the unknown entity via the structured channel;
        # it must NOT be collapsed into a clarification.
        parser = make_parser(engine, draft_response(
            metric=None, request=None, scope=None, frequency_selection=None,
            unit=None, params=None,
            unsupported_dimension="metric", unsupported_value="return loss"))
        outcome = parser.parse("What is the return loss in the final node?")
        assert outcome.status is ParseStatus.NOT_FOUND
        assert "return loss" in outcome.message

    def test_part_keyword_stopword_guard(self, engine):
        # '...for every frequency part and node' must not trigger the part
        # detector on the stopword 'and'.
        parser = QueryParser(engine, FakeLLMClient(responses=[draft_response(
            request="Complete history", scope=None)]))
        outcome = parser.parse(
            "Show complete mismatch loss history for every frequency part and node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.scope == "All frequencies, parts and nodes"

    def test_missing_metric_clarifies(self, engine):
        parser = make_parser(engine, draft_response(
            metric=None, request="Worst", scope=None, params=None))
        outcome = parser.parse("What is the worst thing in the final node?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "metric" in outcome.missing_fields
        assert any("metric" in h.lower() for h in outcome.candidate_hints)

    def test_genuinely_ambiguous_scope_clarifies(self, engine):
        # 'ADL8124' plausibly means scope 'ADL8124' OR 'ADL8124_1'.
        parser = make_parser(engine, draft_response(
            scope=None, params={"kind": "none"}))
        outcome = parser.parse(
            "What is the best mismatch loss in the ADL8124 family?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "scope" in outcome.missing_fields
        hints = " ".join(outcome.candidate_hints)
        assert "ADL8124" in hints and "ADL8124_1" in hints
        assert {c.scope for c in outcome.candidates} == {"ADL8124", "ADL8124_1"}

    # -- Request-variant policy (review policy 2) -----------------------------

    def test_worst_and_minimum_stay_distinct(self, engine):
        worst = make_parser(engine, draft_response(
            metric="CGAIN", request="Worst", scope="Final node",
            params={"kind": "none"}))
        outcome = worst.parse("What is the worst CGAIN in the final node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.request == "Worst"

        minimum = make_parser(engine, draft_response(
            metric="CGAIN", request="Minimum", scope="Final node",
            params={"kind": "none"}))
        outcome = minimum.parse("What is the minimum CGAIN in the final node?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.request == "Minimum"

    def test_lowest_is_not_silently_mapped(self, engine):
        # 'lowest' has NO established business mapping to Worst or Minimum:
        # it must clarify with both candidates, never auto-conflate.
        parser = make_parser(engine, draft_response(
            metric="CGAIN", request=None, scope="Final node",
            frequency_selection=None, params={"kind": "none"}))
        outcome = parser.parse("Give me the lowest cascade gain at the final node.")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "request" in outcome.missing_fields
        # candidate interpretations are presented in the hints (full candidate
        # intents are impossible while frequency is undetermined)
        joined = " ".join(outcome.candidate_hints)
        assert "Minimum" in joined and "Worst" in joined
        assert outcome.candidates == []  # never silently picks one

    def test_lowest_without_other_dims_lists_hint(self, engine):
        parser = make_parser(engine, draft_response(
            metric=None, request=None, scope=None, frequency_selection=None,
            unit=None, params=None))
        outcome = parser.parse("What is the lowest gain?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION

    def test_gain_ambiguity_clarifies_not_guesses(self, engine):
        # User says just "gain"; LLM correctly leaves metric null.
        parser = make_parser(engine, draft_response(
            metric=None, request="Worst", scope="Final node", params=None))
        outcome = parser.parse("What is the gain at 2500 MHz?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert "metric" in outcome.missing_fields

    def test_missing_threshold_clarifies(self, engine):
        parser = make_parser(engine, draft_response(
            request="Threshold Above", scope="All nodes", params=None))
        # user text has no threshold number -> deterministic fallback fails too
        outcome = parser.parse("Where is Mismatch Loss above the limit in all nodes?")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert any("threshold" in f for f in outcome.missing_fields)

    def test_missing_reference_qualifier_clarifies(self, engine):
        parser = make_parser(engine, draft_response(
            metric="TNP", request="Individual frequency power difference",
            scope="Final node", frequency_selection="1200 MHz", unit="dBm",
            params=None))
        # user text without qualifier wording -> deterministic fallback fails
        outcome = parser.parse("show the tnp power difference at 1200 MHz")
        assert outcome.status is ParseStatus.NEEDS_CLARIFICATION
        assert any("qualifier" in f for f in outcome.missing_fields)

    def test_deterministic_param_fallback_from_user_text(self, engine):
        # LLM leaves params null; the audited grammar extracts it from the text
        parser = make_parser(engine, draft_response(
            request="Threshold Above", params=None))
        outcome = parser.parse("Where is Mismatch Loss above 0.1 dB in All nodes?")
        assert outcome.status is ParseStatus.RESOLVED
        assert outcome.intent.params == ThresholdParams(threshold=0.1)

    def test_irrelevant_params_rejected(self, engine):
        parser = make_parser(engine, draft_response(
            request="Best", params={"threshold": 1.0}))
        outcome = parser.parse("What is the best mismatch loss in the final node?")
        assert outcome.status is ParseStatus.PARSE_ERROR

    def test_structured_output_error(self, engine):
        class BoomClient:
            def complete_structured(self, system, user, schema):
                raise StructuredOutputError("bad JSON")

        parser = QueryParser(engine, BoomClient())
        outcome = parser.parse("anything")
        assert outcome.status is ParseStatus.PARSE_ERROR

    def test_llm_receives_constrained_schema(self, engine):
        client = FakeLLMClient(responses=[draft_response()])
        parser = QueryParser(engine, client)
        parser.parse("best mismatch loss in final node")
        assert client.calls, "LLM was not called"
        schema = client.calls[0]["schema"]
        assert "MismatchLoss" in schema["properties"]["metric"]["enum"]


# ---------------------------------------------------------------------------
# Conversation context (conservative inheritance)
# ---------------------------------------------------------------------------


class TestConversationContext:
    def test_empty_context_inherits_nothing(self):
        ctx = ConversationContext()
        assert ctx.inherit({"metric", "scope", "unit"}) == {}
        assert ctx.summary_for_prompt() == "none"

    def test_inherit_only_requested_dimensions(self, engine):
        ctx = ConversationContext()
        from rf_catalogue.query_intent import QueryIntent

        intent = QueryIntent(
            metric="CGAIN", request="Worst", scope="Final node",
            frequency_selection="All Frequencies", unit="dB",
        )
        ctx.record("worst cgain in final node", intent=intent, row_id=10,
                   answered=True)
        inherited = ctx.inherit({"scope", "unit"})
        assert inherited == {"scope": "Final node", "unit": "dB"}
        # request/params are never inherited
        assert "request" not in ctx.inherit({"metric", "request", "scope", "unit"})

    def test_unanswered_turns_do_not_seed_context(self, engine):
        ctx = ConversationContext()
        from rf_catalogue.query_intent import QueryIntent

        intent = QueryIntent(
            metric="CGAIN", request="Best", scope="Final node",
            frequency_selection="All Frequencies", unit="dB",
        )
        ctx.record("unclear question", intent=intent, answered=False)
        assert ctx.last_answered_intent is None
        assert ctx.inherit({"metric"}) == {}

    def test_reset(self, engine):
        ctx = ConversationContext()
        ctx.record("x", answered=True)
        ctx.reset()
        assert ctx.last_answered_intent is None


# ---------------------------------------------------------------------------
# Service end-to-end (scripted LLM)
# ---------------------------------------------------------------------------


class TestService:
    def test_answered_flow_returns_catalogue_answer_verbatim(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft_response()]))
        ctx = ConversationContext()
        result = service.answer_question(
            "What is the best mismatch loss in the final node?", ctx)
        assert result.status is AnswerStatus.ANSWERED
        assert result.row_id == 1
        assert result.answer_text == engine._index[
            ("MismatchLoss", "Best", "Final node", "All Frequencies", "dB")
        ][0].answer
        assert result.question_text is not None
        # context recorded
        assert ctx.last_answered_intent is not None

    def test_clarification_flow(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft_response(metric=None)]))
        result = service.answer_question("What is the best in the final node?")
        assert result.status is AnswerStatus.NEEDS_CLARIFICATION
        assert result.answer_text is None
        assert result.row_id is None
        assert "metric" in result.missing_fields

    def test_not_found_flow(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[draft_response(frequency_selection=None)]))
        result = service.answer_question("Best MismatchLoss at 3400 MHz?")
        assert result.status is AnswerStatus.NOT_FOUND
        assert result.answer_text is None

    def test_follow_up_inherits_context(self, engine):
        service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
            responses=[
                # turn 1: full intent
                draft_response(metric="CGAIN", request="Worst",
                               scope="Final node"),
                # turn 2: only the changed request; everything else null
                draft_response(metric="CGAIN", request="Best", scope=None,
                               params={"kind": "none"}),
            ]))
        ctx = ConversationContext()
        r1 = service.answer_question("What is the worst CGAIN in the final node?", ctx)
        assert r1.status is AnswerStatus.ANSWERED
        r2 = service.answer_question("What about the best?", ctx)
        assert r2.status is AnswerStatus.ANSWERED, r2.message
        assert r2.intent.scope == "Final node"
        assert r2.intent.metric == "CGAIN"
        assert r2.intent.request == "Best"
        assert r2.row_id != r1.row_id

    def test_error_flow_is_graceful(self, engine):
        class Exploding:
            def complete_structured(self, system, user, schema):
                raise RuntimeError("network down")

        service = RfCatalogueService(engine=engine, llm_client=Exploding())
        result = service.answer_question("What is the best mismatch loss?")
        assert result.status is AnswerStatus.ERROR
        assert "RuntimeError" in result.message  # typed, no crash, no secrets

    def test_ambiguous_417_rows_resolve_exactly_through_service(self, engine):
        """Requirement 10 spot check through the FULL service flow."""
        audited = [
            ("Where is Mismatch Loss above 0.1 dB?", 2850, {"threshold": 0.1}),
            ("Where is Mismatch Loss above 10 dB?", 2868, {"threshold": 10.0}),
            ("Where is Cascaded Compression closest to 0.1 dB20?", 3071,
             {"closest_target": 0.1}),
            ("Where is Cascaded Compression closest to 2 dB20?", 3080,
             {"closest_target": 2.0}),
            ("Where is Cascaded Compression between 0.1 and 0.5 dB20?", 3084,
             {"range_min": 0.1, "range_max": 0.5}),
            ("Where is Cascaded Compression between 2 and 3 dB20?", 3087,
             {"range_min": 2.0, "range_max": 3.0}),
        ]
        for question, expected_id, params in audited:
            service = RfCatalogueService(engine=engine, llm_client=FakeLLMClient(
                handler=lambda user, q=question: draft_response(
                    request=("Threshold Above" if "above" in q.lower()
                             else "Range" if "between" in q.lower()
                             else "Closest value"),
                    metric=("CCOMP" if "Compression" in q else "MismatchLoss"),
                    unit=("dB20" if "dB20" in q else "dB"),
                    scope="All nodes", frequency_selection="All Frequencies",
                    params={**params, **({"kind": "none"} if not params else {})},
                )))
            result = service.answer_question(question)
            assert result.status is AnswerStatus.ANSWERED, (
                f"{question}: {result.message}"
            )
            assert result.row_id == expected_id, (
                f"{question}: expected row {expected_id}, got {result.row_id}"
            )


# ---------------------------------------------------------------------------
# Draft model unit behaviour
# ---------------------------------------------------------------------------


class TestDraftModel:
    def test_missing_dimensions(self):
        draft = QueryIntentDraft(request="Best")
        assert set(draft.missing_dimensions()) == {
            "metric", "scope", "frequency_selection", "unit"}

    def test_qualifier_enum_values(self):
        d = QueryIntentDraft(params={"reference_qualifier": "desired channel power"})
        assert d.params.reference_qualifier.value == "desired channel power"
        with pytest.raises(ValidationError):
            QueryIntentDraft(params={"reference_qualifier": "magic power"})
